"""飞牛 NAS 局域网自动发现。

同时使用四种互补手段，避免单一协议被系统服务占用或路由器隔离时完全失效：
1. mDNS / DNS-SD（UDP 5353，飞牛默认广播 fnnas.local）
2. SSDP / UPnP（UDP 1900）
3. WS-Discovery（UDP 3702）
4. 本地网段端口探测 + Web 页面指纹识别（深度扫描）
"""

from __future__ import annotations

import re
import socket
import struct
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .common import (
    FN_WEB_PORTS,
    detect_brand,
    get_local_ipv4,
    http_probe,
    local_networks,
    open_ports,
)

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    """默认日志函数：静默。"""
    return None


@dataclass
class NasDevice:
    """一台被发现的 NAS 设备。"""

    ip: str
    name: str = ""
    score: int = 0
    sources: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    web_url: str = ""
    hostname: str = ""
    brand: str = ""

    @property
    def level(self) -> str:
        if self.score >= 85:
            return f"{self.brand} NAS" if self.brand else "NAS 设备"
        if self.score >= 55:
            return f"疑似{self.brand}" if self.brand else "疑似 NAS"
        return "候选设备"

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "name": self.name or self.hostname or "未命名设备",
            "score": self.score,
            "level": self.level,
            "sources": self.sources,
            "ports": self.ports,
            "web_url": self.web_url,
            "hostname": self.hostname,
            "brand": self.brand,
        }


# ---------------------------------------------------------------------------
# DNS / mDNS 报文解析
# ---------------------------------------------------------------------------


def read_dns_name(data: bytes, offset: int) -> tuple[str, int]:
    """解析 DNS 域名，支持 RFC 1035 压缩指针。"""
    labels: list[str] = []
    end: int | None = None
    index = offset
    jumped = False
    for _ in range(64):  # 防止恶意报文造成死循环
        if index >= len(data):
            break
        length = data[index]
        if length == 0:
            index += 1
            if not jumped:
                end = index
            break
        if length & 0xC0 == 0xC0:
            if index + 1 >= len(data):
                break
            pointer = ((length & 0x3F) << 8) | data[index + 1]
            if not jumped:
                end = index + 2
            index = pointer
            jumped = True
            continue
        index += 1
        if index + length > len(data):
            break
        labels.append(data[index : index + length].decode("utf-8", errors="ignore"))
        index += length
        if not jumped:
            end = index
    return ".".join(labels), end if end is not None else index


def parse_dns_message(data: bytes) -> tuple[list[tuple[str, int]], list[tuple[str, int, int, int, object]]]:
    """解析 DNS 回答报文，返回 (问题列表, 资源记录列表)。"""
    if len(data) < 12:
        return [], []
    qdcount, ancount, nscount, arcount = struct.unpack("!HHHH", data[4:12])
    offset = 12
    questions: list[tuple[str, int]] = []
    for _ in range(min(qdcount, 64)):
        name, offset = read_dns_name(data, offset)
        if offset + 4 > len(data):
            break
        qtype = struct.unpack("!H", data[offset : offset + 2])[0]
        offset += 4
        questions.append((name, qtype))

    answers: list[tuple[str, int, int, int, object]] = []
    for _ in range(min(ancount + nscount + arcount, 512)):
        name, offset = read_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, ttl, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        rdata_start = offset
        rdata = data[rdata_start : rdata_start + rdlength]
        offset += rdlength
        value: object = rdata
        if rtype in (1, 28) and len(rdata) in (4, 16):  # A / AAAA
            value = socket.inet_ntop(socket.AF_INET if rtype == 1 else socket.AF_INET6, rdata)
        elif rtype in (2, 5, 12):  # NS / CNAME / PTR
            value, _ = read_dns_name(data, rdata_start)
        elif rtype == 33 and len(rdata) >= 7:  # SRV
            _, _, port = struct.unpack("!HHH", rdata[:6])
            target, _ = read_dns_name(data, rdata_start + 6)
            value = (port, target)
        elif rtype == 16:  # TXT
            chunks: list[str] = []
            pos = 0
            while pos < len(rdata):
                size = rdata[pos]
                pos += 1
                chunks.append(rdata[pos : pos + size].decode("utf-8", errors="ignore"))
                pos += size
            value = " ".join(chunks)
        answers.append((name, rtype, rclass, ttl, value))
    return questions, answers


def _build_mdns_query(names: list[tuple[str, int]]) -> bytes:
    packet = bytearray(struct.pack("!HHHH", 0, 0, len(names), 0))
    for name, qtype in names:
        for label in name.split("."):
            raw = label.encode("utf-8")
            packet.append(len(raw))
            packet.extend(raw)
        packet.extend(b"\x00")
        packet.extend(struct.pack("!HH", qtype, 1))
    return bytes(packet)


def _multicast_socket(group: str, port: int, bind_port: int, local_ip: str) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", bind_port))
    except OSError:
        try:
            sock.bind(("", 0))
        except OSError:
            sock.close()
            return None
    try:
        mreq = socket.inet_aton(group) + socket.inet_aton(local_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError:
        pass  # 某些无线网卡不支持加入组播，仍可发送查询
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 255))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
    except OSError:
        pass
    sock.settimeout(0.25)
    return sock


def query_mdns(timeout: float = 3.0, log: LogFn = _noop) -> list[dict]:
    """通过 mDNS / DNS-SD 查询飞牛、群晖、威联通 NAS。"""
    local_ip = get_local_ipv4()
    sock = _multicast_socket("224.0.0.251", 5353, 5353, local_ip)
    if sock is None:
        log("mDNS 套接字创建失败，跳过该发现方式")
        return []
    queries = [
        ("_services._dns-sd._udp.local", 12),
        ("_fnnas._tcp.local", 12),
        ("_synology._tcp.local", 12),
        ("_dsf._tcp.local", 12),
        ("_diskstation._tcp.local", 12),
        ("_qnap._tcp.local", 12),
        ("_qnap-nas._tcp.local", 12),
        ("_http._tcp.local", 12),
        ("_smb._tcp.local", 12),
        ("fnnas.local", 1),
    ]
    try:
        sock.sendto(_build_mdns_query(queries), ("224.0.0.251", 5353))
    except OSError as exc:
        log(f"mDNS 查询发送失败: {exc}")
        sock.close()
        return []

    addresses: dict[str, str] = {}
    pointers: dict[str, list[str]] = {}
    services: dict[str, tuple[int, str]] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        _, records = parse_dns_message(data)
        for name, rtype, _, _, value in records:
            lower_name = name.lower().rstrip(".")
            if rtype == 1 and isinstance(value, str):
                addresses[lower_name] = value
            elif rtype == 12 and isinstance(value, str):
                pointers.setdefault(lower_name, []).append(value.lower().rstrip("."))
            elif rtype == 33 and isinstance(value, tuple):
                services[lower_name] = (int(value[0]), str(value[1]).lower().rstrip("."))
    sock.close()

    brand_hosts: dict[str, tuple[str, int, str, list[int]]] = {}
    for host, ip in addresses.items():
        brand = detect_brand(host)
        if brand:
            brand_hosts[ip] = (brand, 95, host, [])
    for service_name, instances in pointers.items():
        for instance in instances:
            port, target = services.get(instance, (0, instance))
            brand = detect_brand(instance) or detect_brand(service_name) or detect_brand(target)
            if brand:
                ip = addresses.get(target)
                if ip:
                    old_brand, score, name, ports = brand_hosts.get(ip, (brand, 90, instance or target, []))
                    if port:
                        ports.append(port)
                    brand_hosts[ip] = (old_brand or brand, max(score, 90), name, ports)

    results: list[dict] = []
    for ip, (brand, score, name, ports) in brand_hosts.items():
        results.append(
            {
                "ip": ip, "name": name, "score": score, "source": "mDNS",
                "ports": sorted(set(ports)), "hostname": name, "brand": brand,
            }
        )
    if results:
        log(f"mDNS 发现 {len(results)} 台设备")
    return results


def _parse_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="ignore").splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _fetch_upnp_description(location: str, timeout: float = 1.5) -> dict:
    """读取 UPnP 设备描述 XML，提取设备名称与厂商。"""
    info: dict[str, str] = {}
    try:
        import urllib.request

        request = urllib.request.Request(location, headers={"User-Agent": "FnosMount/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(32768).decode("utf-8", errors="ignore")
        for tag in ("friendlyName", "modelName", "modelDescription", "manufacturer"):
            match = re.search(rf"<(?:[A-Za-z0-9]+:)?{tag}[^>]*>(.*?)</(?:[A-Za-z0-9]+:)?{tag}>", text, re.I | re.S)
            if match:
                info[tag] = re.sub(r"\s+", " ", match.group(1)).strip()[:120]
    except Exception:  # noqa: BLE001 - 设备描述不一定可访问
        pass
    return info


def query_ssdp(timeout: float = 3.0, log: LogFn = _noop) -> list[dict]:
    """通过 SSDP / UPnP 查询局域网设备。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))
    sock.settimeout(0.25)
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: ssdp:all\r\n\r\n"
    ).encode()
    try:
        sock.sendto(message, ("239.255.255.250", 1900))
        sock.sendto(message, ("239.255.255.250", 1900))
    except OSError as exc:
        log(f"SSDP 查询发送失败: {exc}")
        sock.close()
        return []

    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        headers = _parse_headers(data)
        location = headers.get("location", "")
        host = urllib.parse.urlparse(location).hostname if location else ""
        if not host or not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            continue
        description = _fetch_upnp_description(location) if location else {}
        hint = " ".join(
            [
                headers.get("server", ""),
                headers.get("usn", ""),
                headers.get("st", ""),
                *description.values(),
            ]
        )
        brand = detect_brand(hint)
        score = 85 if brand else 30
        name = description.get("friendlyName") or description.get("modelName") or headers.get("server", "")
        found[host] = {
            "ip": host,
            "name": name,
            "score": score,
            "source": "SSDP/UPnP",
            "ports": [],
            "hostname": "",
            "brand": brand,
        }
    sock.close()
    results = list(found.values())
    if results:
        log(f"SSDP 发现 {len(results)} 台设备")
    return results


def query_wsd(timeout: float = 3.0, log: LogFn = _noop) -> list[dict]:
    """通过 WS-Discovery 查询支持该协议的网络存储设备。"""
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:wsa="http://www.w3.org/2005/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:tdp="http://schemas.xmlsoap.org/ws/2006/02/devprof">
 <env:Header>
  <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</wsa:Action>
  <wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>
  <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
 </env:Header>
 <env:Body><d:Probe><d:Types>tdp:Device</d:Types></d:Probe></env:Body>
</env:Envelope>""".encode("utf-8")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", 0))
    sock.settimeout(0.25)
    try:
        sock.sendto(envelope, ("239.255.255.250", 3702))
    except OSError as exc:
        log(f"WS-Discovery 查询发送失败: {exc}")
        sock.close()
        return []

    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode("utf-8", errors="ignore")
        match = re.search(r"<\w*:?Address[^>]*>\s*([^<]+)\s*</\w*:?Address>", text, re.I)
        if not match:
            continue
        address = match.group(1).strip()
        host = urllib.parse.urlparse(address).hostname if "://" in address else ""
        if not host or not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            continue
        brand = detect_brand(text)
        found[host] = {
            "ip": host,
            "name": "WS-Discovery 设备",
            "score": 80 if brand else 30,
            "source": "WS-Discovery",
            "ports": [],
            "hostname": "",
            "brand": brand,
        }
    sock.close()
    if found:
        log(f"WS-Discovery 发现 {len(found)} 台设备")
    return list(found.values())


# ---------------------------------------------------------------------------
# 候选设备验证与网段扫描
# ---------------------------------------------------------------------------


def verify_candidate(device: dict, log: LogFn = _noop) -> dict:
    """对候选设备做端口与 Web 指纹验证。"""
    ip = device["ip"]
    web_ports = open_ports(ip, FN_WEB_PORTS, timeout=0.35, workers=5)
    service_ports = open_ports(ip, (445, 2049), timeout=0.35, workers=2)
    ports = sorted(set(web_ports + service_ports + list(device.get("ports", []))))
    device["ports"] = ports
    if 445 in ports:
        device.setdefault("smb", True)
    if 2049 in ports:
        device.setdefault("nfs", True)
    for port in web_ports[:4]:
        probe = http_probe(ip, port, timeout=1.0, use_tls=port in (443, 5001))
        brand = device.get("brand") or probe["brand"]
        if brand:
            device["brand"] = brand
            device["score"] = max(device["score"], 95)
            device["name"] = probe["title"] or device.get("name") or f"{brand} NAS"
            device["web_url"] = probe["url"]
            log(f"确认{brand} NAS: {ip}:{port} {probe['title']}")
            break
        if probe["url"] and not device.get("web_url"):
            device["web_url"] = probe["url"]
    return device


def deep_scan(timeout: float = 0.18, log: LogFn = _noop) -> list[dict]:
    """扫描本机各网段，按端口和 Web 指纹寻找 NAS。"""
    targets: list[str] = []
    for net in local_networks():
        hosts = list(net.hosts())
        for host in hosts[:254]:  # 单个网段最多扫描 254 个地址
            targets.append(str(host))
    log(f"深度扫描 {len(targets)} 个地址，请稍候…")
    interesting_ports = tuple(dict.fromkeys(FN_WEB_PORTS + (445, 2049)))
    found: list[dict] = []

    def scan_host(ip: str) -> dict | None:
        ports = open_ports(ip, interesting_ports, timeout=timeout, workers=7)
        if not ports:
            return None
        web_ports = [p for p in ports if p in FN_WEB_PORTS]
        for port in web_ports[:3]:
            probe = http_probe(ip, port, timeout=0.9, use_tls=port in (443, 5001))
            if probe["brand"]:
                return {
                    "ip": ip,
                    "name": probe["title"] or f"{probe['brand']} NAS",
                    "score": 92,
                    "source": "网段扫描",
                    "ports": ports,
                    "web_url": probe["url"],
                    "hostname": "",
                    "brand": probe["brand"],
                }
        if 2049 in ports and 445 in ports:
            return {"ip": ip, "name": "NAS 候选（SMB + NFS）", "score": 58, "source": "网段扫描", "ports": ports, "hostname": ""}
        if any(port in (5000, 5001, 5005, 5006, 5666, 8080, 8081) for port in ports):
            return {"ip": ip, "name": "NAS 候选（管理端口）", "score": 52, "source": "网段扫描", "ports": ports, "hostname": ""}
        return None

    with ThreadPoolExecutor(max_workers=48) as pool:
        for result in pool.map(scan_host, targets):
            if result:
                found.append(result)
    if found:
        log(f"网段扫描找到 {len(found)} 个候选设备")
    return found


def _merge(candidates: list[dict]) -> list[NasDevice]:
    merged: dict[str, NasDevice] = {}
    for item in candidates:
        ip = item.get("ip", "")
        if not ip or ip == "127.0.0.1":
            continue
        device = merged.get(ip)
        if device is None:
            device = NasDevice(
                ip=ip,
                name=item.get("name", ""),
                score=item.get("score", 0),
                sources=[item.get("source", "手动")],
                ports=list(item.get("ports", [])),
                web_url=item.get("web_url", ""),
                hostname=item.get("hostname", ""),
                brand=item.get("brand", ""),
            )
            merged[ip] = device
            continue
        device.ports = sorted(set(device.ports + list(item.get("ports", []))))
        source = item.get("source", "")
        if source and source not in device.sources:
            device.sources.append(source)
        if item.get("brand") and not device.brand:
            device.brand = item["brand"]
        if item.get("score", 0) > device.score:
            device.score = item["score"]
            device.name = item.get("name") or device.name
        if item.get("web_url") and not device.web_url:
            device.web_url = item["web_url"]
        if item.get("hostname") and not device.hostname:
            device.hostname = item["hostname"]
    result = sorted(merged.values(), key=lambda d: (-d.score, d.ip))
    return [d for d in result if d.score >= 50]


def discover(timeout: float = 3.0, deep: bool = False, log: LogFn = _noop) -> list[NasDevice]:
    """执行完整发现流程，返回按可信度排序的设备列表。"""
    log("开始扫描局域网…")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(query_mdns, timeout, log),
            pool.submit(query_ssdp, timeout, log),
            pool.submit(query_wsd, timeout, log),
        ]
        candidates: list[dict] = []
        for future in futures:
            try:
                candidates.extend(future.result())
            except Exception as exc:  # noqa: BLE001 - 单个发现方式失败不影响整体
                log(f"发现方式异常: {exc}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        verified = list(pool.map(verify_candidate, candidates))

    # 部分飞牛设备关闭了 mDNS/UPnP，或电脑与 NAS 之间存在组播隔离。
    # 快速发现没有可靠结果时自动回退到网段扫描，保证"一键就能找到"。
    reliable = [item for item in verified if item.get("score", 0) >= 50]
    if deep or not reliable:
        verified.extend(deep_scan(log=log))
    devices = _merge(verified)
    log(f"扫描完成，共找到 {len(devices)} 台候选设备")
    return devices


def probe_host(host: str, log: LogFn = _noop) -> NasDevice | None:
    """探测用户手动输入的 IP 或主机名。"""
    host = host.strip()
    if not host:
        return None
    ip = host
    if not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        try:
            ip = socket.gethostbyname(host)
        except OSError as exc:
            log(f"无法解析主机 {host}: {exc}")
            return None
    candidate = {"ip": ip, "name": host, "score": 70, "source": "手动指定", "ports": [], "hostname": host if host != ip else ""}
    verify_candidate(candidate, log)
    devices = _merge([candidate])
    return devices[0] if devices else None


def main() -> None:
    import json

    logging = print
    devices = discover(timeout=3.0, deep=False, log=logging)
    print(json.dumps([device.to_dict() for device in devices], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
