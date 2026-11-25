"""大文件传输优化：客户端 SMB 参数与 NFS 读写块大小。

所有修改都是可选且可撤销的；开启前会备份原值，关闭时恢复。
"""

from __future__ import annotations

import winreg

from . import config

SMB_KEY = r"SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters"
SMB_TWEAKS: dict[str, int] = {
    # 关闭 SMB 带宽节流，高延迟链路上大文件传输更稳定
    "DisableBandwidthThrottling": 1,
    # 提高并发未完成命令数，提升大文件顺序读写吞吐
    "MaxCmds": 64,
}
NFS_TUNING = "rsize=131072,wsize=131072"


def nfs_options() -> str:
    """根据当前设置返回 NFS 挂载附加参数。"""
    settings = config.load_config()["settings"]
    return NFS_TUNING if settings.get("large_file_tuning") else ""


def _read_value(key_path: str, name: str) -> int | None:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return int(value)
    except OSError:
        return None


def status() -> dict:
    """返回当前调优状态。"""
    values = {name: _read_value(SMB_KEY, name) for name in SMB_TWEAKS}
    applied = all(values.get(name) == expected for name, expected in SMB_TWEAKS.items())
    return {
        "enabled": bool(config.load_config()["settings"].get("large_file_tuning")),
        "applied": applied,
        "values": values,
        "nfs_options": NFS_TUNING,
    }


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """开启或关闭大文件传输优化（修改 HKLM 需要管理员权限）。"""
    try:
        current = config.load_config()
        backup = current.get("tuning_backup") or {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, SMB_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                if not backup:
                    backup = {name: _read_value(SMB_KEY, name) for name in SMB_TWEAKS}
                for name, value in SMB_TWEAKS.items():
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)
                current["tuning_backup"] = backup
                config.save_config(current)
                return True, "已开启大文件传输优化（SMB 并发增强 + NFS 128KB 读写块）"
            for name, old in backup.items():
                if old is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(old))
            current["tuning_backup"] = {}
            config.save_config(current)
            return True, "已恢复默认传输参数"
    except OSError as exc:
        if not enabled:
            return True, "当前未应用系统级优化，无需恢复"
        return False, f"修改传输参数失败（请以管理员身份运行）: {exc}"
