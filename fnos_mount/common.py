"""公共网络与 Windows 系统工具。全部基于标准库，无需安装第三方包。"""

from __future__ import annotations

import ctypes
import ipaddress
import os
import re
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

IS_WINDOWS = os.name == "nt"

# 支持的 NAS 品牌指纹。短英文 token 使用词边界，避免误判普通网页。
BRAND_PATTERNS: dict[str, tuple[str, ...]] = {
    "飞牛": (r"fnnas", r"fnos", r"fniu", r"feiniu", r"飞牛"),
    "群晖": (r"synology", r"diskstation", r"disk.?station", r"群晖"),
    "威联通": (r"qnap", r"威联通", r"\bqts\b"),
}

# 飞牛 / 群晖 / 威联通常见 Web 管理端口。
FN_WEB_PORTS = (5005, 5006, 5000, 5001, 5666, 8080, 8081, 80, 443)
FN_SERVICE_PORTS = (445, 2049, 21, 111, 5353, 1900, 3702, 9997, 9998)


def detect_brand(text: str | None) -> str:
    """从一段文本中识别 NAS 品牌，返回品牌名；无法识别返回空字符串。"""
    if not text:
        return ""
    for brand, patterns in BRAND_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                return brand
    return ""


def is_fn_text(text: str | None) -> bool:
    """兼容旧调用：判断文本是否含有飞牛 NAS 特征。"""
    return detect_brand(text) == "飞牛"


def get_local_ipv4() -> str:
    """获取访问外网时使用的本机 IPv4 地址（不会真正发送数据）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def local_networks() -> list[ipaddress.IPv4Network]:
    """解析本机所有 IPv4 网段；解析失败时回退到本机地址所在 /24。"""
    networks: list[ipaddress.IPv4Network] = []
    if IS_WINDOWS:
        try:
            result = subprocess.run(
                ["netsh", "interface", "ip", "show", "config"],
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="ignore",
                timeout=6,
            )
            ip = mask = None
            for line in (result.stdout or "").splitlines():
                stripped = line.strip()
                match = re.match(r"^(?:IP 地址|IP Address|IPv4 地址)\s*[:.]?\s*(\d+\.\d+\.\d+\.\d+)", stripped, re.I)
                if match:
                    ip = match.group(1)
                    continue
                match = re.match(r"^(?:子网掩码|Subnet Mask)\s*[:.]?\s*(\d+\.\d+\.\d+\.\d+)", stripped, re.I)
                if match:
                    mask = match.group(1)
                if ip and mask:
                    try:
                        networks.append(ipaddress.IPv4Network(f"{ip}/{mask}", strict=False))
                    except ValueError:
                        pass
                    ip = mask = None
        except (OSError, subprocess.SubprocessError):
            pass
    if not networks:
        ip = get_local_ipv4()
        try:
            networks.append(ipaddress.IPv4Network(f"{ip}/24", strict=False))
        except ValueError:
            networks.append(ipaddress.IPv4Network("192.168.1.0/24"))
    # 去重，并跳过环回和小网段（避免扫描过多地址）
    unique: list[ipaddress.IPv4Network] = []
    for net in networks:
        if net.is_loopback or net.prefixlen < 8:
            continue
        if net not in unique:
            unique.append(net)
    return unique or [ipaddress.IPv4Network("192.168.1.0/24")]


def open_ports(host: str, ports: Iterable[int], timeout: float = 0.25, workers: int = 32) -> list[int]:
    """并发探测主机端口，返回开放的端口列表。"""

    def probe(port: int) -> int | None:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return port
        except OSError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(probe, list(ports))
        return sorted(port for port in results if port is not None)


def http_probe(host: str, port: int, timeout: float = 1.2, use_tls: bool = False) -> dict:
    """抓取 Web 页面标题、正文片段和响应头，用于识别设备类型。"""
    scheme = "https" if use_tls else "http"
    result = {
        "url": f"{scheme}://{host}:{port}/", "status": 0, "title": "", "text": "", "server": "",
        "is_fn": False, "brand": "",
    }
    context = ssl._create_unverified_context() if use_tls else None  # noqa: SSL-WEAK - 仅用于局域网识别
    request = urllib.request.Request(
        result["url"], headers={"User-Agent": "Mozilla/5.0 (compatible; FnosMount/1.0)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # type: ignore[arg-type]
            raw = response.read(16384)
            result["status"] = getattr(response, "status", 0)
            result["server"] = response.headers.get("Server", "")
            encoding = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(encoding, errors="ignore")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # HTTP 401/403 也是有效指纹：错误页里常有产品名
        if isinstance(exc, urllib.error.HTTPError):
            raw = exc.read(8192)
            result["status"] = exc.code
            result["server"] = exc.headers.get("Server", "")
            text = raw.decode("utf-8", errors="ignore")
        else:
            return result
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if match:
        result["title"] = re.sub(r"\s+", " ", match.group(1)).strip()[:120]
    result["text"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:1500]
    fingerprint = " ".join((result["title"], result["text"], result["server"]))
    result["brand"] = detect_brand(fingerprint)
    result["is_fn"] = result["brand"] == "飞牛"
    return result


def format_win_error(code: int) -> str:
    """把 Windows/网络错误码转换成可读的中文提示。"""
    if not IS_WINDOWS or code == 0:
        return ""
    try:
        result = subprocess.run(
            ["net", "helpmsg", str(code)],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            timeout=3,
        )
        message = (result.stdout or "").strip().splitlines()
        if message and not message[0].startswith("系统错误"):
            return message[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        buffer = ctypes.create_unicode_buffer(512)
        ctypes.windll.kernel32.FormatMessageW(  # type: ignore[attr-defined]
            0x00001000 | 0x00000200,
            None,
            code & 0xFFFFFFFF,
            0,
            buffer,
            512,
            None,
        )
        return buffer.value.strip().splitlines()[0] if buffer.value.strip() else f"错误码 {code}"
    except (AttributeError, OSError, IndexError):
        return f"错误码 {code}"


def free_drive_letters() -> list[str]:
    """返回当前未被占用的盘符，优先从 Z 往前推荐。"""
    if not IS_WINDOWS:
        return []
    used = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    free = [f"{chr(letter)}:" for letter in range(ord("Z"), ord("C"), -1) if not (used & (1 << (letter - ord("A"))))]
    return free


def safe_filename(value: str) -> str:
    """把任意字符串转换成可安全用于文件名/凭据目标的片段。"""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)[:80] or "unknown"


def run_hidden(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """执行子进程且不弹出控制台窗口（Windows）。"""
    flags = 0
    if IS_WINDOWS:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            timeout=timeout,
            creationflags=flags,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except OSError as exc:
        return -1, "", str(exc)


def main() -> None:
    """模块自检：打印本机网络与端口情况。"""
    print("本机地址:", get_local_ipv4())
    print("本地网段:", ", ".join(str(net) for net in local_networks()))
    print("可用盘符:", ", ".join(free_drive_letters()) or "无")


if __name__ == "__main__":
    main()
