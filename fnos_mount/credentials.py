"""使用 Windows 凭据管理器安全保存 NAS 登录密码。

密码不写入配置文件，而是以当前 Windows 用户的 DPAPI 保护凭据形式存放在
"凭据管理器 -> Windows 凭据" 中，目标名统一以 FnosMount: 开头。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .common import IS_WINDOWS, safe_filename


class CREDENTIALW(ctypes.Structure):
    """Windows CREDENTIALW 结构。"""

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


def credential_target(host: str, share: str, protocol: str = "smb") -> str:
    """生成凭据目标名。"""
    return f"FnosMount:{safe_filename(protocol)}:{safe_filename(host)}:{safe_filename(share)}"


def _advapi32():
    if not IS_WINDOWS:
        return None
    return ctypes.windll.advapi32  # type: ignore[attr-defined]


def save_credentials(target: str, username: str, password: str) -> tuple[bool, str]:
    """保存或更新凭据。返回 (是否成功, 提示信息)。"""
    advapi32 = _advapi32()
    if advapi32 is None:
        return False, "仅支持 Windows 凭据管理器"
    if not username or not password:
        return False, "用户名和密码不能为空"
    blob = password.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(blob)
    credential = CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = ctypes.c_wchar_p(target)
    credential.Comment = ctypes.c_wchar_p("FnosMount 保存的 NAS 登录凭据")
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.c_void_p).value
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = ctypes.c_wchar_p(username)
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL
    ok = advapi32.CredWriteW(ctypes.byref(credential), 0)
    return (True, "凭据已保存") if ok else (False, "凭据保存失败，请检查系统权限")


def read_credentials(target: str) -> tuple[str, str] | None:
    """读取凭据，返回 (用户名, 密码)；不存在时返回 None。"""
    advapi32 = _advapi32()
    if advapi32 is None or not target:
        return None
    pointer = ctypes.POINTER(CREDENTIALW)()
    advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    advapi32.CredReadW.restype = wintypes.BOOL
    if not advapi32.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return None
    try:
        credential = pointer.contents
        password = ""
        if credential.CredentialBlob and credential.CredentialBlobSize:
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            password = raw.decode("utf-16-le", errors="ignore")
        username = credential.UserName or ""
        return username, password
    finally:
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree(ctypes.cast(pointer, ctypes.c_void_p))


def delete_credentials(target: str) -> bool:
    """删除已保存的凭据。"""
    advapi32 = _advapi32()
    if advapi32 is None or not target:
        return False
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    return bool(advapi32.CredDeleteW(target, CRED_TYPE_GENERIC, 0))


def main() -> None:
    target = "FnosMount:test:127.0.0.1:demo"
    print(save_credentials(target, "demo", "P@ssw0rd"))
    print(read_credentials(target))
    print("删除:", delete_credentials(target))


if __name__ == "__main__":
    main()
