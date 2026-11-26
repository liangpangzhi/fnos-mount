"""开机自启动：写入当前用户的 Run 项，无需管理员权限。"""

from __future__ import annotations

import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "FnosMount"


def startup_command() -> str:
    """生成登录时执行的命令（托盘模式 + 恢复映射）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray --reconnect'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interpreter = pythonw if os.path.isfile(pythonw) else sys.executable
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(project_root, "main.py")
    return f'"{interpreter}" "{main_py}" --tray --reconnect'


def startup_value() -> str:
    """返回当前已注册的自启动命令。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return ""


def is_enabled() -> bool:
    """判断开机自启是否已启用。"""
    return bool(startup_value())


def is_blocked_by_system() -> bool:
    """任务管理器「启动」里被手动禁用时返回 True。

    Run 项还在，但 StartupApproved 里带禁用标记，登录时系统会跳过它。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            data, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    if not data:
        return False
    return bool(data[0] & 1)


def _clear_system_block() -> None:
    """清掉任务管理器「启动」里的禁用记录，否则 Run 项写了也不会执行。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:
        pass


def set_enabled(enabled: bool) -> tuple[bool, str]:
    """启用或关闭开机自启。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
                _clear_system_block()
                return True, "已开启开机自启动（登录后在托盘运行并恢复磁盘）"
            winreg.DeleteValue(key, VALUE_NAME)
            return True, "已关闭开机自启动"
    except OSError as exc:
        if not enabled and not is_enabled():
            return True, "开机自启动未启用"
        return False, f"修改启动项失败: {exc}"
