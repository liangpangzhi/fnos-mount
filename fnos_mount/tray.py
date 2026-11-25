"""Windows 系统托盘图标（仅用 ctypes，无第三方依赖）。

提供：左键双击打开界面、右键菜单（打开界面 / 立即重连 / 退出程序）、气泡提示。
"""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable

ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fnosmount.ico")

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512
MENU_OPEN, MENU_RECONNECT, MENU_EXIT = 1, 2, 3


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """Shell_NotifyIconW 使用的通知图标数据结构。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class TrayController:
    """管理托盘图标的生命周期。"""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_reconnect: Callable[[], None],
        on_exit: Callable[[], None],
        tip: str = "FnosMount - NAS 磁盘映射器",
    ) -> None:
        self.on_open = on_open
        self.on_reconnect = on_reconnect
        self.on_exit = on_exit
        self.tip = tip
        self._thread: threading.Thread | None = None
        self._hwnd: wintypes.HWND | None = None
        self._ready = threading.Event()
        self._callback: object = None
        self._class_name = "FnosMountTrayWindow"

    @property
    def available(self) -> bool:
        return self._hwnd is not None

    def start(self) -> bool:
        """启动托盘线程。"""
        if self._thread is not None:
            return self.available
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="FnosMountTray")
        self._thread.start()
        self._ready.wait(3)
        return self.available

    def stop(self) -> None:
        """移除托盘图标并结束线程。"""
        if self._hwnd is not None:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        self._hwnd = None

    def notify(self, message: str, title: str = "FnosMount") -> None:
        """显示气泡提示。"""
        if self._hwnd is None:
            return
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = NIF_INFO
        data.szInfo = message[:250]
        data.szInfoTitle = title[:60]
        data.uTimeoutOrVersion = 5000
        data.dwInfoFlags = 0
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    # ------------------------------------------------------------------
    # 线程内部
    # ------------------------------------------------------------------

    def _configure_api(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM if hasattr(wintypes, "ATOM") else wintypes.WORD
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HICON
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR]
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p
        ]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def _load_icon(self) -> wintypes.HICON | None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if os.path.isfile(ICON_PATH):
            icon = user32.LoadImageW(None, ICON_PATH, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            if icon:
                return icon
        default_icon = ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
        return user32.LoadIconW(None, default_icon)

    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        user32.AppendMenuW(menu, MF_STRING, MENU_OPEN, "打开界面")
        user32.AppendMenuW(menu, MF_STRING, MENU_RECONNECT, "立即重连磁盘")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, "退出程序")
        point = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(point)):
            user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, self._hwnd, None)
        user32.DestroyMenu(menu)

    def _run(self) -> None:
        self._configure_api()
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        def wnd_proc(hwnd, message, wparam, lparam):  # type: ignore[no-untyped-def]
            if message == WM_TRAYICON:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                    self.on_open()
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                return 0
            if message == WM_COMMAND:
                command = wparam & 0xFFFF
                if command == MENU_OPEN:
                    self.on_open()
                elif command == MENU_RECONNECT:
                    self.on_reconnect()
                elif command == MENU_EXIT:
                    self.on_exit()
                return 0
            if message == WM_CLOSE:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        self._callback = WNDPROC(wnd_proc)
        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW()
        window_class.style = 0
        window_class.lpfnWndProc = ctypes.cast(self._callback, ctypes.c_void_p).value
        window_class.hInstance = instance
        window_class.lpszClassName = self._class_name
        registered = user32.RegisterClassW(ctypes.byref(window_class))
        if not registered:
            error = kernel32.GetLastError()
            # 1410 = 类已注册，可直接使用
            if error != 1410:
                self._ready.set()
                return

        hwnd = user32.CreateWindowExW(
            0, self._class_name, "FnosMountTray", 0, 0, 0, 0, 0,
            ctypes.c_void_p(-3), None, instance, None,  # HWND_MESSAGE
        )
        if not hwnd:
            self._ready.set()
            return
        self._hwnd = hwnd

        icon = self._load_icon()
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_TRAYICON
        data.hIcon = icon
        data.szTip = self.tip[:127]
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        added = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
        self._ready.set()
        if not added:
            self._hwnd = None
            return

        message = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        user32.DestroyWindow(hwnd)
        self._hwnd = None
