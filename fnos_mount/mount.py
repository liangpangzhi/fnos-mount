"""把 NAS 共享目录映射为 Windows 本地磁盘。

SMB 使用 Windows 原生 WNet API；NFS 使用系统自带的 NFS 客户端；
WebDAV 使用 Windows WebClient 重定向器。三种方式均由 Windows 在系统层面
提供盘符，资源管理器、命令行和普通程序都可以像访问本地磁盘一样使用。
"""

from __future__ import annotations

import ctypes
import re
import winreg
from ctypes import wintypes
from typing import Callable

from .common import IS_WINDOWS, format_win_error, free_drive_letters, run_hidden

LogFn = Callable[[str], None]

NO_ERROR = 0
ERROR_NOT_CONNECTED = 2250
ERROR_ALREADY_ASSIGNED = 85
ERROR_BAD_DEVICE = 1200
ERROR_INVALID_PASSWORD = 86
ERROR_LOGON_FAILURE = 1326

RESOURCETYPE_ANY = 0
RESOURCETYPE_DISK = 1
CONNECT_UPDATE_PROFILE = 0x00000001
CONNECT_INTERACTIVE = 0x00000008
CONNECT_PROMPT = 0x00000010
CONNECT_REDIRECT = 0x00000080


class NETRESOURCEW(ctypes.Structure):
    """WNet 系列函数使用的 NETRESOURCEW 结构。"""

    _fields_ = [
        ("dwScope", wintypes.DWORD),
        ("dwType", wintypes.DWORD),
        ("dwDisplayType", wintypes.DWORD),
        ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR),
        ("lpRemoteName", wintypes.LPWSTR),
        ("lpComment", wintypes.LPWSTR),
        ("lpProvider", wintypes.LPWSTR),
    ]


def _wnet():
    if not IS_WINDOWS:
        return None
    return ctypes.windll.mpr  # type: ignore[attr-defined]


def normalise_drive(drive: str) -> str:
    """把盘符规范成 X: 形式。"""
    drive = drive.strip().rstrip(":").strip()
    return f"{drive.upper()}:" if drive else ""


def smb_path(host: str, share: str) -> str:
    """构造 UNC 路径 \\\\host\\share。"""
    host = host.strip().strip("\\/")
    share = share.strip().strip("\\/").replace("/", "\\")
    if not host or not share:
        return ""
    return f"\\\\{host}\\{share}"


def _configure_api() -> None:
    mpr = _wnet()
    if mpr is None:
        return
    mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(NETRESOURCEW), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    mpr.WNetAddConnection2W.restype = wintypes.DWORD
    mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    mpr.WNetCancelConnection2W.restype = wintypes.DWORD
    mpr.WNetGetConnectionW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    mpr.WNetGetConnectionW.restype = wintypes.DWORD


_configure_api()


def mount_smb(
    host: str,
    share: str,
    drive: str,
    username: str = "",
    password: str = "",
    persistent: bool = False,
    log: LogFn = print,
) -> tuple[bool, str]:
    """用 WNetAddConnection2 把 SMB 共享映射为本地磁盘。"""
    mpr = _wnet()
    if mpr is None:
        return False, "该功能仅支持 Windows"
    remote = smb_path(host, share)
    drive = normalise_drive(drive)
    if not remote or not drive:
        return False, "共享路径或盘符无效"

    resource = NETRESOURCEW()
    resource.dwType = RESOURCETYPE_DISK
    resource.lpLocalName = ctypes.c_wchar_p(drive)
    resource.lpRemoteName = ctypes.c_wchar_p(remote)
    resource.lpProvider = ctypes.c_wchar_p("Microsoft Windows Network")
    flags = CONNECT_INTERACTIVE | CONNECT_REDIRECT
    if persistent:
        flags |= CONNECT_UPDATE_PROFILE

    log(f"正在映射 {remote} -> {drive}")
    result = mpr.WNetAddConnection2W(
        ctypes.byref(resource),
        ctypes.c_wchar_p(password) if password else None,
        ctypes.c_wchar_p(username) if username else None,
        flags,
    )
    if result == NO_ERROR:
        message = f"已将 {remote} 映射为 {drive}"
        log(message)
        return True, message
    if result == ERROR_ALREADY_ASSIGNED:
        return False, f"{drive} 已被占用，请选择其他盘符"
    message = format_win_error(result) or f"Windows 错误 {result}"
    log(f"映射失败: {message}")
    return False, message


def connect_ipc(host: str, username: str, password: str) -> tuple[bool, str]:
    """临时连接 IPC$，用于读取共享列表。"""
    mpr = _wnet()
    if mpr is None:
        return False, "仅支持 Windows"
    resource = NETRESOURCEW()
    resource.dwType = RESOURCETYPE_ANY
    resource.lpRemoteName = ctypes.c_wchar_p(smb_path(host, "IPC$"))
    result = mpr.WNetAddConnection2W(ctypes.byref(resource), ctypes.c_wchar_p(password), ctypes.c_wchar_p(username), CONNECT_INTERACTIVE)
    if result == NO_ERROR:
        return True, ""
    return False, format_win_error(result) or f"Windows 错误 {result}"


def disconnect_ipc(host: str) -> None:
    """断开用于读取共享列表的临时 IPC$ 连接。"""
    mpr = _wnet()
    if mpr is not None:
        mpr.WNetCancelConnection2W(ctypes.c_wchar_p(smb_path(host, "IPC$")), 0, True)


def unmount(drive: str, persistent: bool = False, force: bool = True, log: LogFn = print) -> tuple[bool, str]:
    """断开已映射的网络磁盘。"""
    mpr = _wnet()
    if mpr is None:
        return False, "该功能仅支持 Windows"
    drive = normalise_drive(drive)
    flags = CONNECT_UPDATE_PROFILE if persistent else 0
    result = mpr.WNetCancelConnection2W(ctypes.c_wchar_p(drive), flags, bool(force))
    if result in (NO_ERROR, ERROR_NOT_CONNECTED):
        message = f"已断开 {drive}" if result == NO_ERROR else f"{drive} 未映射"
        log(message)
        return True, message
    message = format_win_error(result) or f"Windows 错误 {result}"
    return False, message


def get_current_mappings() -> list[dict]:
    """列出 Windows 当前所有网络磁盘映射。"""
    mpr = _wnet()
    if mpr is None:
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    mappings: list[dict] = []
    for index in range(26):
        if not mask & (1 << index):
            continue
        drive = f"{chr(ord('A') + index)}:"
        buffer = ctypes.create_unicode_buffer(2048)
        size = wintypes.DWORD(2048)
        result = mpr.WNetGetConnectionW(ctypes.c_wchar_p(drive), buffer, ctypes.byref(size))
        if result != NO_ERROR:
            continue
        remote = buffer.value
        mappings.append(
            {
                "drive": drive,
                "remote": remote,
                "protocol": "WebDAV" if remote.lower().startswith(("http://", "https://")) else "SMB/NFS",
            }
        )
    return mappings


def is_mounted(drive: str) -> bool:
    """判断盘符是否已存在。"""
    if not IS_WINDOWS:
        return False
    mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
    drive = normalise_drive(drive)
    if len(drive) != 2:
        return False
    return bool(mask & (1 << (ord(drive[0].upper()) - ord("A"))))


def suggest_drive() -> str:
    """推荐一个未占用的盘符。"""
    free = free_drive_letters()
    return free[0] if free else ""


# ---------------------------------------------------------------------------
# NFS
# ---------------------------------------------------------------------------


def nfs_feature_enabled() -> bool:
    """检测 Windows NFS 客户端是否已启用（通过服务注册表判断）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\NfsClnt") as key:
            start, _ = winreg.QueryValueEx(key, "Start")
            return int(start) != 4  # 4 = 禁用
    except OSError:
        return False


def enable_nfs_feature() -> tuple[bool, str]:
    """启用 Windows NFS 客户端（需要管理员权限）。"""
    code, output, error = run_hidden(
        ["dism", "/online", "/enable-feature", "/featurename:ClientForNFS-Infrastructure", "/all", "/norestart"],
        timeout=300,
    )
    if code == 0:
        return True, "NFS 客户端已启用，建议重启电脑后使用"
    return False, (error or output or f"DISM 错误 {code}").strip().splitlines()[-1]


def mount_nfs(host: str, path: str, drive: str, log: LogFn = print, options: str = "") -> tuple[bool, str]:
    """用 Windows NFS 客户端挂载远程导出目录。"""
    if not IS_WINDOWS:
        return False, "该功能仅支持 Windows"
    if not nfs_feature_enabled():
        return False, "未启用 Windows NFS 客户端，请在界面中点击“启用 NFS 支持”或手动启用后重启"
    host = host.strip().strip("\\/")
    path = path.strip().replace("\\", "/").strip("/")
    drive = normalise_drive(drive)
    if not host or not path or not drive:
        return False, "请填写 NFS 导出路径（例如 /volume1/data）"
    remote = f"{host}:/{path}"
    extra = options.strip().strip(",")
    mount_options = f"anon,{extra}" if extra else "anon"
    log(f"正在挂载 NFS {remote} -> {drive}（选项: {mount_options}）")
    for candidate in (remote, f"\\\\{host}\\{path.replace('/', '\\')}"):
        code, output, error = run_hidden(["mount", "-o", mount_options, candidate, drive], timeout=30)
        if code == 0:
            message = f"已挂载 {remote} 为 {drive}"
            log(message)
            return True, message
        detail = (error or output).strip().splitlines()
        log(f"NFS 挂载失败: {detail[-1] if detail else code}")
    return False, "NFS 挂载失败，请确认飞牛 NAS 已开启 NFS 服务并允许当前 IP 访问"


def unmount_nfs(drive: str, log: LogFn = print) -> tuple[bool, str]:
    """卸载 NFS 挂载。"""
    code, output, error = run_hidden(["umount", normalise_drive(drive)], timeout=20)
    if code == 0:
        return True, f"已卸载 {normalise_drive(drive)}"
    return False, (error or output or f"错误 {code}").strip().splitlines()[-1]


# ---------------------------------------------------------------------------
# WebDAV
# ---------------------------------------------------------------------------


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    """Windows 服务状态结构。"""

    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
        ("dwProcessId", wintypes.DWORD),
        ("dwStartTickCount", wintypes.DWORD),
    ]


SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STATE_NAMES = {
    1: "STOPPED",
    2: "START_PENDING",
    3: "STOP_PENDING",
    4: "RUNNING",
    5: "CONTINUE_PENDING",
    6: "PAUSE_PENDING",
    7: "PAUSED",
}
WEBCLIENT_REGISTRY = r"SYSTEM\CurrentControlSet\Services\WebClient\Parameters"


def _open_webdav_service(desired_access: int):
    """打开 WebClient 服务句柄，返回 (句柄, advapi32)。"""
    advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
    advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    advapi32.CloseServiceHandle.restype = wintypes.BOOL
    advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenSCManagerW.restype = wintypes.HANDLE
    manager = advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not manager:
        return None, advapi32
    advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    advapi32.OpenServiceW.restype = wintypes.HANDLE
    handle = advapi32.OpenServiceW(manager, "WebClient", desired_access)
    advapi32.CloseServiceHandle(manager)
    return handle or None, advapi32


def webdav_service_status() -> str:
    """返回 WebClient 服务状态。"""
    if not IS_WINDOWS:
        return "不存在"
    handle, advapi32 = _open_webdav_service(SERVICE_QUERY_STATUS)
    if not handle:
        return "不存在"
    try:
        status = SERVICE_STATUS_PROCESS()
        needed = wintypes.DWORD()
        advapi32.QueryServiceStatusEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]
        ok = advapi32.QueryServiceStatusEx(handle, 0, ctypes.byref(status), ctypes.sizeof(status), ctypes.byref(needed))
        return SERVICE_STATE_NAMES.get(status.dwCurrentState, "未知") if ok else "未知"
    finally:
        advapi32.CloseServiceHandle(handle)


def start_webdav_service() -> tuple[bool, str]:
    """启动 WebClient 服务（可能需要管理员权限）。"""
    handle, advapi32 = _open_webdav_service(SERVICE_START | SERVICE_QUERY_STATUS)
    if not handle:
        return False, "无法打开 WebClient 服务（可能需要管理员权限）"
    try:
        advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
        if advapi32.StartServiceW(handle, 0, None):
            return True, "WebClient 服务已启动"
        return False, format_win_error(ctypes.windll.kernel32.GetLastError())  # type: ignore[attr-defined]
    finally:
        advapi32.CloseServiceHandle(handle)


def webdav_basic_auth_level() -> int:
    """读取 WebClient BasicAuthLevel（2 表示允许 HTTP 基本认证）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WEBCLIENT_REGISTRY) as key:
            value, _ = winreg.QueryValueEx(key, "BasicAuthLevel")
            return int(value)
    except OSError:
        return -1


def set_webdav_basic_auth(level: int = 2) -> tuple[bool, str]:
    """修改 WebClient BasicAuthLevel（需要管理员权限）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WEBCLIENT_REGISTRY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "BasicAuthLevel", 0, winreg.REG_DWORD, level)
        return True, "已允许 WebDAV HTTP 基本认证，请重启 WebClient 服务"
    except OSError as exc:
        return False, f"修改失败（请以管理员身份运行）: {exc}"


def mount_webdav(
    url: str,
    drive: str,
    username: str = "",
    password: str = "",
    persistent: bool = False,
    log: LogFn = print,
) -> tuple[bool, str]:
    """用 net use 映射 WebDAV 地址为本地磁盘。"""
    if not IS_WINDOWS:
        return False, "该功能仅支持 Windows"
    url = url.strip()
    drive = normalise_drive(drive)
    if not re.match(r"^https?://", url, re.I) or not drive:
        return False, "WebDAV 地址必须以 http:// 或 https:// 开头"
    if url.lower().startswith("http://") and webdav_basic_auth_level() < 2:
        return False, "HTTP WebDAV 需要管理员权限开启基本认证，请点击“启用 HTTP WebDAV”后重试"

    status = webdav_service_status()
    if status not in ("RUNNING",):
        ok, message = start_webdav_service()
        if not ok:
            return False, f"WebClient 服务未运行: {message}"

    args = ["net", "use", drive, url]
    if username:
        args.append(f"/user:{username}")
    if password:
        args.append(password)
    args.append("/persistent:yes" if persistent else "/persistent:no")
    log(f"正在映射 WebDAV {url} -> {drive}")
    code, output, error = run_hidden(args, timeout=30)
    if code == 0:
        message = f"已将 WebDAV 映射为 {drive}"
        log(message)
        return True, message
    detail = (error or output or "").strip().splitlines()
    message = detail[-1] if detail else (format_win_error(code) or f"net use 错误 {code}")
    return False, message


def main() -> None:
    print("当前网络映射:", get_current_mappings())
    print("推荐盘符:", suggest_drive())
    print("NFS 客户端:", "已启用" if nfs_feature_enabled() else "未启用")
    print("WebClient 服务:", webdav_service_status())


if __name__ == "__main__":
    main()
