"""兼容入口：界面已改为纯 Win32 原生窗口，不再使用网页。"""

from .native_ui import NativeUI, launch

__all__ = ["NativeUI", "launch"]
