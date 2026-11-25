"""读取远程主机上的 SMB 共享目录列表。"""

from __future__ import annotations

import re
from typing import Callable

from .common import run_hidden
from .mount import connect_ipc, disconnect_ipc

LogFn = Callable[[str], None]

# net view 在不同语言系统下的类型列写法
_DISK_TYPES = ("disk", "磁盘", "文件共享")
_SKIP_SHARES = {"ipc$", "print$"}


def parse_net_view(text: str) -> list[str]:
    """解析 net view 输出，返回共享名列表。"""
    shares: list[str] = []
    for line in text.splitlines():
        if not line.strip() or set(line.strip()) <= {"-"}:
            continue
        # 形如:  share_name   Disk     备注信息
        match = re.match(r"^\s*([^\s]+)\s+(\S+)(?:\s+(.*))?$", line)
        if not match:
            continue
        name, share_type = match.group(1), match.group(2).lower()
        if name.lower() in _SKIP_SHARES or name.startswith("$"):
            continue
        if any(kind in share_type for kind in _DISK_TYPES):
            if name not in shares:
                shares.append(name)
    return shares


def list_smb_shares(host: str, username: str = "", password: str = "", log: LogFn = print) -> tuple[list[str], str]:
    """获取远程主机的 SMB 共享列表，返回 (共享名列表, 错误信息)。"""
    host = host.strip().strip("\\/")
    if not host:
        return [], "请先选择或输入 NAS 地址"

    temp_connection = False
    if username and password:
        ok, message = connect_ipc(host, username, password)
        if ok:
            temp_connection = True
        else:
            log(f"IPC$ 连接失败，尝试匿名读取共享列表: {message}")

    try:
        code, stdout, stderr = run_hidden(["net", "view", f"\\\\{host}"], timeout=10)
        shares = parse_net_view(stdout)
        if shares:
            log(f"读取到 {len(shares)} 个共享目录")
            return shares, ""
        if code != 0:
            message = (stderr or stdout or "无法读取共享列表").strip().splitlines()
            return [], f"读取共享列表失败: {message[-1] if message else '未知错误'}"
        return [], "未发现可访问的共享目录，可在下方手动输入共享名"
    finally:
        if temp_connection:
            disconnect_ipc(host)
