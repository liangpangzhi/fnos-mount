"""纯 Win32 原生 GUI（无网页、无第三方依赖），苹果风格圆角界面。

所有业务能力复用 discovery / shares / mount / config 等模块；
后台线程通过 PostMessage 把结果送回 UI 线程。
选择框使用自绘圆角按钮 + 弹出菜单实现，规避本环境原生 ComboBox 异常。
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from ctypes import wintypes
from typing import Callable

from . import autostart, config, credentials, discovery, mount, performance, shares, watchdog

# ---------------- Win32 常量 ----------------
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_BORDER = 0x00800000
WS_VSCROLL = 0x00200000
WS_TABSTOP = 0x00010000
WS_CLIPCHILDREN = 0x02000000
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
BS_PUSHBUTTON = 0x00000000
BS_DEFPUSHBUTTON = 0x00000001
BS_LEFT = 0x00000100
BS_OWNERDRAW = 0x0000000F
ODS_SELECTED = 0x0001
ODS_DISABLED = 0x0004
ODS_HOVER = 0x0040
ODS_FOCUS = 0x0010
DT_CENTER = 0x00000001
DT_VCENTER = 0x00000004
DT_SINGLELINE = 0x00000020
TRANSPARENT = 1
HOLLOW_BRUSH = 5
BS_AUTOCHECKBOX = 0x00000003
SS_LEFT = 0x00000000
ES_MULTILINE = 0x00000004
ES_PASSWORD = 0x00000020
ES_READONLY = 0x00000800
ES_AUTOHSCROLL = 0x00000080
LBS_NOTIFY = 0x00000001
LBS_HASSTRINGS = 0x00000040
BN_CLICKED = 0
LBN_SELCHANGE = 1
BM_GETCHECK = 0x00F0
BM_SETCHECK = 0x00F1
BST_CHECKED = 1
LB_ADDSTRING = 0x0180
LB_RESETCONTENT = 0x0184
LB_GETCURSEL = 0x0188
LB_GETTEXT = 0x0189
LB_GETCOUNT = 0x018B
LB_SETCURSEL = 0x0186
LB_GETTEXTLEN = 0x018A
WM_CREATE = 0x0001
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_SETFONT = 0x0030
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DRAWITEM = 0x002B
WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLOREDIT = 0x0133
WM_CTLCOLORLISTBOX = 0x0134
WM_CTLCOLORBTN = 0x0135
WM_APP = 0x8000
SW_HIDE = 0
SW_SHOW = 5
DEFAULT_GUI_FONT = 17
SM_CXSCREEN = 0
SM_CYSCREEN = 1
MF_STRING = 0x00000000
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0000
TPM_TOPALIGN = 0x0000
EM_SETSEL = 0x00B1
WM_VSCROLL = 0x0115
SB_BOTTOM = 7
WM_GETFONT = 0x0031
# 下拉弹窗
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WM_ACTIVATE = 0x0006
WM_KEYDOWN = 0x0100
WM_LBUTTONDOWN = 0x0201
WM_MOUSEMOVE = 0x0200
WM_SETCURSOR = 0x0020
WA_INACTIVE = 0
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
LB_SETITEMHEIGHT = 0x01A0
LB_GETITEMHEIGHT = 0x01A1
LBS_NOINTEGRALHEIGHT = 0x0100
LB_ITEMFROMPOINT = 0x01A9
WM_LBUTTONUP = 0x0202
WM_TIMER = 0x0113
GWLP_WNDPROC = -4
PS_SOLID = 0
TIMER_HOVER = 1
SW_SHOWNA = 8
SW_RESTORE = 9
WM_APP_ACTIVATE = WM_APP + 1
ID_POPUP_LIST = 9901
POPUP_CLASS = "FnosMountDropdownPopup"
# 布局尺寸（客户区）
CLIENT_W, CLIENT_H = 1000, 772

# 控件 ID
ID_SCAN, ID_DEEP, ID_TRAY, ID_ADD = 1001, 1002, 1003, 1004
ID_MANUAL, ID_STATUS = 1005, 1006
ID_DEVICES = 1010
ID_HOST, ID_PROTOCOL, ID_DRIVE, ID_SHARE, ID_USER, ID_PASS = 1020, 1021, 1022, 1023, 1024, 1025
ID_SHARE_LIST = 1026
ID_LOAD_SHARES, ID_MAP, ID_REMEMBER, ID_PERSIST, ID_HINT = 1030, 1031, 1032, 1033, 1034
ID_MAPPINGS, ID_UNMAP = 1040, 1041
ID_AUTOSTART, ID_START_MIN, ID_KEEP, ID_TUNE, ID_INTERVAL, ID_CHECK = 1050, 1051, 1052, 1053, 1054, 1055
ID_LOG = 1060

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


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


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = [
        ("CtlType", wintypes.UINT),
        ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT),
        ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT),
        ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC),
        ("rcItem", wintypes.RECT),
        ("itemData", ctypes.c_size_t),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_ubyte * 32),
    ]


def _configure_api() -> None:
    u = ctypes.windll.user32  # type: ignore[attr-defined]
    k = ctypes.windll.kernel32  # type: ignore[attr-defined]
    g = ctypes.windll.gdi32  # type: ignore[attr-defined]
    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u.RegisterClassW.restype = wintypes.WORD
    u.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    u.CreateWindowExW.restype = wintypes.HWND
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.SendMessageW.restype = ctypes.c_ssize_t
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    u.SetWindowTextW.restype = wintypes.BOOL
    u.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    u.GetMessageW.restype = ctypes.c_int
    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.PostQuitMessage.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int
    u.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE
    # 绘制相关
    g.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
    ]
    g.CreateFontW.restype = wintypes.HFONT
    g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    g.CreateSolidBrush.restype = wintypes.HBRUSH
    g.GetStockObject.argtypes = [ctypes.c_int]
    g.GetStockObject.restype = wintypes.HANDLE
    g.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    g.SelectObject.restype = wintypes.HANDLE
    g.DeleteObject.argtypes = [wintypes.HANDLE]
    g.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    g.RoundRect.restype = wintypes.BOOL
    u.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    u.FillRect.restype = ctypes.c_int
    g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    g.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    g.SetBkColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    g.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
    g.TextOutW.restype = wintypes.BOOL
    g.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.SIZE)]
    g.GetTextExtentPoint32W.restype = wintypes.BOOL
    g.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    g.CreateRoundRectRgn.restype = wintypes.HRGN
    g.GetStockObject.argtypes = [ctypes.c_int]
    g.GetStockObject.restype = wintypes.HANDLE
    u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetDlgCtrlID.argtypes = [wintypes.HWND]
    u.GetDlgCtrlID.restype = ctypes.c_int
    u.IsWindowEnabled.argtypes = [wintypes.HWND]
    u.IsWindowEnabled.restype = wintypes.BOOL
    try:
        uxtheme = ctypes.windll.uxtheme  # type: ignore[attr-defined]
        uxtheme.SetWindowTheme.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        uxtheme.SetWindowTheme.restype = ctypes.c_long
    except Exception:  # noqa: BLE001
        pass
    u.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    u.BeginPaint.restype = wintypes.HDC
    u.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
    u.InvalidateRect.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
    u.CreateMenu.restype = wintypes.HMENU
    u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_ssize_t, wintypes.LPCWSTR]
    u.AppendMenuW.restype = wintypes.BOOL
    u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
    u.TrackPopupMenu.restype = wintypes.BOOL
    u.DestroyMenu.argtypes = [wintypes.HMENU]
    # 窗口尺寸换算（客户区 <-> 窗口区）
    u.AdjustWindowRect.argtypes = [ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL]
    u.AdjustWindowRect.restype = wintypes.BOOL
    u.GetDC.argtypes = [wintypes.HWND]
    u.GetDC.restype = wintypes.HDC
    u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u.SetFocus.argtypes = [wintypes.HWND]
    u.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    k.GetCurrentThreadId.restype = wintypes.DWORD
    # 悬停动画 / 控件子类化
    u.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    u.SetTimer.restype = ctypes.c_size_t
    u.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.WindowFromPoint.restype = wintypes.HWND
    u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    u.SetWindowLongPtrW.restype = ctypes.c_void_p
    u.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u.CallWindowProcW.restype = ctypes.c_ssize_t
    g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
    g.CreatePen.restype = wintypes.HPEN


class NativeUI:
    """原生窗口 + 业务逻辑（苹果风格圆角界面）。"""

    def __init__(self) -> None:
        _configure_api()
        self.u = ctypes.windll.user32  # type: ignore[attr-defined]
        self.g = ctypes.windll.gdi32  # type: ignore[attr-defined]
        self.settings = config.load_config()
        self.devices: list[discovery.NasDevice] = []
        self.selected_device = ""
        self.selected_mapping = ""
        self.scanning = False
        self.log_lines: list[str] = []
        self.results: dict[int, tuple[str, object]] = {}
        self._token = 0
        self.hwnd: wintypes.HWND | None = None
        self.controls: dict[int, wintypes.HWND] = {}
        self.watchdog = watchdog.MappingWatchdog(
            int(self.settings["settings"].get("watchdog_interval") or 60), self.log
        )
        if self.settings["settings"].get("keep_mounted", True):
            self.watchdog.start()
        self.tray = None
        self._callback: object = None
        # 界面样式数据
        self.fonts = self._create_fonts()
        self.brushes = {
            "page": self.g.CreateSolidBrush(self._rgb(245, 245, 247)),
            "card": self.g.CreateSolidBrush(self._rgb(255, 255, 255)),
            "shadow": self.g.CreateSolidBrush(self._rgb(232, 233, 238)),
            "input": self.g.CreateSolidBrush(self._rgb(255, 255, 255)),
            # 按钮标签 Static 的背景画刷（需与按钮自绘背景一致，否则白字在白底上看不见）
            "btn_primary": self.g.CreateSolidBrush(self._rgb(0, 122, 255)),
            "btn_secondary": self.g.CreateSolidBrush(self._rgb(255, 255, 255)),
            "btn_danger": self.g.CreateSolidBrush(self._rgb(255, 255, 255)),
            "btn_dropdown": self.g.CreateSolidBrush(self._rgb(255, 255, 255)),
        }
        self.button_text: dict[int, str] = {}
        self.button_kind: dict[int, str] = {}
        self.dropdown_items: dict[int, list[str]] = {}
        self.cards: list[tuple[int, int, int, int]] = []
        # 自绘下拉弹窗状态
        self.popup_hwnd: wintypes.HWND | None = None
        self.popup_list: wintypes.HWND | None = None
        self.popup_target: int = 0
        self.popup_items: list[str] = []
        self.popup_opened_at: float = 0.0
        self.popup_closed_at: float = 0.0
        self.popup_closed_target: int = 0
        self.popup_pending: str = ""
        self._popup_class_registered = False
        self._popup_callback: object = WNDPROC(self._popup_proc)
        # 悬停高亮动画：每个可交互控件 0~10 的进度
        self.hover_progress: dict[int, int] = {}
        self.hover_id: int = 0
        self.hover_ids: tuple[int, ...] = ()
        self._listbox_callback: object = WNDPROC(self._listbox_proc)
        self._listbox_orig_proc: int = 0

    # ---------------- 样式工具 ----------------

    @staticmethod
    def _rgb(red: int, green: int, blue: int) -> int:
        return red | (green << 8) | (blue << 16)

    def _create_fonts(self) -> dict[str, wintypes.HFONT]:
        def make(height: int, weight: int) -> wintypes.HFONT:
            return self.g.CreateFontW(
                height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei"
            )
        return {
            "title": make(-25, 700),
            "header": make(-17, 700),
            "body": make(-15, 400),
            "button": make(-15, 500),
        }

    # ---------------- 基础工具 ----------------

    def _next_token(self) -> int:
        self._token += 1
        return self._token

    def post_result(self, kind: str, data: object) -> None:
        if self.hwnd is None:
            return
        token = self._next_token()
        self.results[token] = (kind, data)
        self.u.PostMessageW(self.hwnd, WM_APP, token, 0)

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{stamp}] {message}")
        self.log_lines = self.log_lines[-200:]
        self.post_result("log", None)

    def text(self, control_id: int) -> str:
        handle = self.controls.get(control_id)
        if not handle:
            return ""
        buffer = ctypes.create_unicode_buffer(2048)
        self.u.GetWindowTextW(handle, buffer, 2048)
        return buffer.value.strip()

    def set_text(self, control_id: int, value: str) -> None:
        handle = self.controls.get(control_id)
        if handle:
            self.u.SetWindowTextW(handle, value)
        if control_id in self.button_text:
            self.button_text[control_id] = value
            display = value + "  ▾" if self.button_kind.get(control_id) == "dropdown" else value
            if handle:
                self.u.SetWindowTextW(handle, display)

    def checked(self, control_id: int) -> bool:
        handle = self.controls.get(control_id)
        return bool(handle and self.u.SendMessageW(handle, BM_GETCHECK, 0, 0) == BST_CHECKED)

    def set_checked(self, control_id: int, value: bool) -> None:
        handle = self.controls.get(control_id)
        if handle:
            self.u.SendMessageW(handle, BM_SETCHECK, BST_CHECKED if value else 0, 0)

    @staticmethod
    def _string_lparam(value: str) -> int:
        return ctypes.cast(ctypes.c_wchar_p(value), ctypes.c_void_p).value or 0

    def combo_add(self, control_id: int, values: list[str], selected: str = "") -> None:
        """自绘下拉框：保存选项并设置当前值。"""
        self.dropdown_items[control_id] = list(values)
        value = selected if selected in values else (values[0] if values else "")
        self.set_text(control_id, value)

    def combo_current(self, control_id: int) -> str:
        # 从 button_text 读取纯值（窗口文本带 ▾ 箭头后缀，不能直接用于逻辑判断）
        return self.button_text.get(control_id, "").strip()

    def list_add(self, control_id: int, values: list[str], selected: str = "") -> None:
        handle = self.controls.get(control_id)
        if not handle:
            return
        self.u.SendMessageW(handle, LB_RESETCONTENT, 0, 0)
        for value in values:
            buffer = ctypes.c_wchar_p(value)
            self.u.SendMessageW(handle, LB_ADDSTRING, 0, ctypes.cast(buffer, ctypes.c_void_p).value or 0)
        if selected in values:
            self.u.SendMessageW(handle, LB_SETCURSEL, values.index(selected), 0)

    def list_selection(self, control_id: int) -> str:
        handle = self.controls.get(control_id)
        if not handle:
            return ""
        index = self.u.SendMessageW(handle, LB_GETCURSEL, 0, 0)
        if index < 0:
            return ""
        length = self.u.SendMessageW(handle, LB_GETTEXTLEN, index, 0)
        buffer = ctypes.create_unicode_buffer(int(length) + 1)
        self.u.SendMessageW(handle, LB_GETTEXT, index, ctypes.cast(buffer, ctypes.c_void_p).value or 0)
        return buffer.value

    def _measure_text(self, text_value: str, font) -> int:
        hdc = self.u.GetDC(self.hwnd)
        if not hdc:
            return 0
        old = self.g.SelectObject(hdc, font)
        size = wintypes.SIZE()
        self.g.GetTextExtentPoint32W(hdc, text_value, len(text_value), ctypes.byref(size))
        self.g.SelectObject(hdc, old)
        self.u.ReleaseDC(self.hwnd, hdc)
        return int(size.cx)

    def _register_window_class(self, klass: "WNDCLASSW") -> int:
        """注册窗口类。

        冻结（PyInstaller）环境下 RegisterClassW 的 argtypes 校验偶发报
        “expected LP_WNDCLASSW instance” —— 改用 void* 传递，绕过类型副本比较。
        """
        fn = self.u.RegisterClassW
        saved_types = getattr(fn, "argtypes", None)
        saved_restype = getattr(fn, "restype", None)
        try:
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = wintypes.WORD
            return int(fn(ctypes.cast(ctypes.byref(klass), ctypes.c_void_p)))
        finally:
            if saved_types is not None:
                fn.argtypes = saved_types
            if saved_restype is not None:
                fn.restype = saved_restype

    def _ensure_popup_class(self) -> None:
        if self._popup_class_registered:
            return
        klass = WNDCLASSW()
        klass.style = 0
        klass.lpfnWndProc = ctypes.cast(self._popup_callback, ctypes.c_void_p).value
        klass.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
        klass.lpszClassName = POPUP_CLASS
        klass.hbrBackground = 5
        registered = self._register_window_class(klass)
        if not registered and ctypes.windll.kernel32.GetLastError() != 1410:
            raise OSError(f"注册下拉弹窗类失败: {ctypes.windll.kernel32.GetLastError()}")
        self._popup_class_registered = True

    def show_dropdown(self, control_id: int) -> None:
        """弹出选项列表（自绘弹窗 + 原生 ListBox，避免系统菜单量宽失效）。"""
        if self.popup_hwnd:
            same = self.popup_target == control_id
            self.close_dropdown()
            if same:
                return
        elif time.time() - self.popup_closed_at < 0.18 and control_id == self.popup_closed_target:
            # 刚刚因为点击同一个按钮而关闭：视为“再次点击收起”，不重新展开
            return
        items = self.dropdown_items.get(control_id, [])
        if not items:
            self.set_text(ID_STATUS, "请先点击“读取共享目录”获取列表" if control_id == ID_SHARE_LIST else "没有可选项")
            return
        try:
            self._open_dropdown(control_id, items)
        except OSError as exc:
            self.log(f"打开下拉列表失败: {exc}")

    def _open_dropdown(self, control_id: int, items: list[str]) -> None:
        handle = self.controls.get(control_id)
        if not handle:
            return
        self._ensure_popup_class()
        rect = wintypes.RECT()
        if not self.u.GetWindowRect(handle, ctypes.byref(rect)):
            return
        row_h = 28
        pad = 6
        rows = min(len(items), 9)
        width = max(rect.right - rect.left, 180)
        for item in items[:9]:
            width = max(width, self._measure_text(item, self.fonts["body"]) + 48)
        width = min(width, 460)
        height = rows * row_h + pad * 2
        x = rect.left
        y = rect.bottom + 2
        screen_w = self.u.GetSystemMetrics(SM_CXSCREEN)
        screen_h = self.u.GetSystemMetrics(SM_CYSCREEN)
        if y + height > screen_h - 8:
            y = max(8, rect.top - height - 2)
        if x + width > screen_w - 8:
            x = max(8, screen_w - width - 8)

        popup = self.u.CreateWindowExW(
            WS_EX_TOOLWINDOW, POPUP_CLASS, "", WS_POPUP,
            x, y, width, height, self.hwnd, None,
            ctypes.windll.kernel32.GetModuleHandleW(None), None,
        )
        if not popup:
            raise OSError(f"创建下拉弹窗失败: {ctypes.windll.kernel32.GetLastError()}")
        rgn = self.g.CreateRoundRectRgn(0, 0, width + 1, height + 1, 14, 14)
        if rgn:
            self.u.SetWindowRgn(popup, rgn, True)

        list_style = WS_CHILD | WS_VISIBLE | LBS_NOTIFY | LBS_HASSTRINGS | LBS_NOINTEGRALHEIGHT
        if len(items) > rows:
            list_style |= WS_VSCROLL
        listbox = self.u.CreateWindowExW(
            0, "ListBox", "", list_style,
            pad, pad, width - pad * 2, rows * row_h, popup,
            ctypes.c_void_p(ID_POPUP_LIST), ctypes.windll.kernel32.GetModuleHandleW(None), None,
        )
        if not listbox:
            self.u.DestroyWindow(popup)
            raise OSError("创建下拉列表失败")
        self.u.SendMessageW(listbox, WM_SETFONT, self.fonts["body"], 0)
        self.u.SendMessageW(listbox, LB_SETITEMHEIGHT, 0, row_h)
        for item in items:
            buffer = ctypes.c_wchar_p(item)
            self.u.SendMessageW(listbox, LB_ADDSTRING, 0, ctypes.cast(buffer, ctypes.c_void_p).value or 0)
        # 添加条目后重新定尺寸：列表框会按整行高自适应，需强制成 rows 行高度
        self.u.MoveWindow(listbox, pad, pad, width - pad * 2, rows * row_h, True)
        current = self.button_text.get(control_id, "")
        if current in items:
            self.u.SendMessageW(listbox, LB_SETCURSEL, items.index(current), 0)

        self.popup_hwnd = popup
        self.popup_list = listbox
        self.popup_target = control_id
        self.popup_items = list(items)
        self.popup_pending = ""
        self.popup_opened_at = time.time()
        self.u.ShowWindow(popup, SW_SHOW)
        self.u.SetForegroundWindow(popup)
        self.u.SetFocus(listbox)
        # 子类化列表：鼠标移动高亮底色（悬停）、松开鼠标才提交选择
        previous = self.u.SetWindowLongPtrW(
            listbox, GWLP_WNDPROC, ctypes.cast(self._listbox_callback, ctypes.c_void_p)
        )
        self._listbox_orig_proc = int(previous or 0)

    def close_dropdown(self) -> None:
        popup = self.popup_hwnd
        self.popup_hwnd = None
        self.popup_list = None
        self._listbox_orig_proc = 0
        self.popup_closed_target = self.popup_target
        self.popup_target = 0
        self.popup_items = []
        self.popup_pending = ""
        if popup:
            self.popup_closed_at = time.time()
            self.u.ShowWindow(popup, SW_HIDE)
            self.u.DestroyWindow(popup)
            if self.hwnd:
                self.u.SetForegroundWindow(self.hwnd)

    def _popup_selection(self) -> str:
        if not self.popup_list:
            return ""
        index = self.u.SendMessageW(self.popup_list, LB_GETCURSEL, 0, 0)
        if index is None or index < 0:
            return ""
        length = self.u.SendMessageW(self.popup_list, LB_GETTEXTLEN, index, 0)
        buffer = ctypes.create_unicode_buffer(int(length) + 1)
        self.u.SendMessageW(self.popup_list, LB_GETTEXT, index, ctypes.cast(buffer, ctypes.c_void_p).value or 0)
        return buffer.value

    def _listbox_index_at(self, hwnd, lparam: int) -> int:
        result = int(self.u.SendMessageW(hwnd, LB_ITEMFROMPOINT, 0, lparam))
        if (result >> 16) & 0xFFFF:  # 高 16 位为 1 表示光标不在条目上
            return -1
        return result & 0xFFFF

    def _listbox_proc(self, hwnd, message, wparam, lparam):  # type: ignore[no-untyped-def]
        """下拉列表：鼠标划过的条目显示高亮底色，松开鼠标才确认选择。"""
        if not self.popup_list or int(hwnd) != int(self.popup_list):
            return self.u.DefWindowProcW(hwnd, message, wparam, lparam)
        if message == WM_MOUSEMOVE:
            index = self._listbox_index_at(hwnd, int(lparam))
            if index >= 0:
                current = int(self.u.SendMessageW(hwnd, LB_GETCURSEL, 0, 0))
                if current != index:
                    self.u.SendMessageW(hwnd, LB_SETCURSEL, index, 0)
        elif message == WM_LBUTTONUP:
            index = self._listbox_index_at(hwnd, int(lparam))
            if index >= 0:
                self.u.SendMessageW(hwnd, LB_SETCURSEL, index, 0)
            value = self._popup_selection()
            if value and self.popup_hwnd:
                self.popup_pending = value
                self.u.PostMessageW(self.popup_hwnd, WM_CLOSE, 0, 0)
            return 0
        return self.u.CallWindowProcW(self._listbox_orig_proc, hwnd, message, wparam, lparam)

    def _popup_proc(self, hwnd, message, wparam, lparam):  # type: ignore[no-untyped-def]
        """下拉弹窗的消息处理：选中即回填，失去焦点/ESC/点击空白关闭。"""
        if message == WM_ACTIVATE:
            if (int(wparam) & 0xFFFF) == WA_INACTIVE and time.time() - self.popup_opened_at > 0.15:
                self.close_dropdown()
            return 0
        if message == WM_ERASEBKGND:
            return 1
        if message == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = self.u.BeginPaint(hwnd, ctypes.byref(ps))
            client = wintypes.RECT()
            self.u.GetClientRect(hwnd, ctypes.byref(client))
            self.u.FillRect(hdc, ctypes.byref(client), self.brushes["card"])
            # 描一圈浅灰边框，避免白底弹窗叠在白卡片上看不出边界
            pen = self.g.CreatePen(PS_SOLID, 1, self._rgb(206, 209, 216))
            if pen:
                old_pen = self.g.SelectObject(hdc, pen)
                old_brush = self.g.SelectObject(hdc, self.g.GetStockObject(HOLLOW_BRUSH))
                self.g.RoundRect(hdc, 0, 0, client.right, client.bottom, 14, 14)
                self.g.SelectObject(hdc, old_brush)
                self.g.SelectObject(hdc, old_pen)
                self.g.DeleteObject(pen)
            self.u.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if message in (WM_CTLCOLORLISTBOX, WM_CTLCOLOREDIT):
            return self._on_ctl_color(message, wparam, lparam)
        if message == WM_KEYDOWN:
            if wparam == VK_ESCAPE:
                self.close_dropdown()
                return 0
            if wparam == VK_RETURN:
                value = self._popup_selection()
                if value:
                    self.popup_pending = value
                    self.u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return 0
        if message == WM_LBUTTONDOWN:
            self.close_dropdown()
            return 0
        if message == WM_CLOSE:
            value = self.popup_pending
            target = self.popup_target
            self.close_dropdown()
            if target and value:
                self.set_text(target, value)
                if target == ID_PROTOCOL:
                    self.on_protocol_changed()
                elif target == ID_SHARE_LIST:
                    self.set_text(ID_SHARE, value)
            return 0
        return self.u.DefWindowProcW(hwnd, message, wparam, lparam)

    def busy(self, value: bool) -> None:
        for control_id in (ID_SCAN, ID_DEEP, ID_MAP, ID_LOAD_SHARES, ID_UNMAP, ID_ADD):
            handle = self.controls.get(control_id)
            if handle:
                self.u.EnableWindow(handle, 0 if value else 1)
        self.set_text(ID_STATUS, "正在处理，请稍候…" if value else "就绪")

    # ---------------- 控件创建 ----------------

    def _control(self, cls: str, cid: int, text: str, style: int, x: int, y: int, w: int, h: int, ex: int = 0):
        handle = self.u.CreateWindowExW(
            ex, cls, text, WS_CHILD | WS_VISIBLE | style,
            x, y, w, h, self.hwnd, ctypes.c_void_p(cid),
            ctypes.windll.kernel32.GetModuleHandleW(None), None,
        )
        if not handle:
            raise OSError(f"创建控件失败: id={cid}, err={ctypes.windll.kernel32.GetLastError()}")
        self.controls[cid] = handle
        if cid == 1:
            font = self.fonts["title"]
        elif cid in (3, 4, 11, 13, 15):
            font = self.fonts["header"]
        else:
            font = self.fonts["body"]
        self.u.SendMessageW(handle, WM_SETFONT, font, 0)
        # 输入/列表控件做圆角裁剪
        if cls in ("Edit", "ListBox") and cid != ID_LOG:
            radius = 16 if cls == "ListBox" else 12
            rgn = self.g.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
            if rgn:
                self.u.SetWindowRgn(handle, rgn, True)
        # 复选框禁用视觉主题，否则 WM_CTLCOLORBTN 被忽略、文字背后有灰块
        if cls == "Button" and (style & 0x0000000F) == BS_AUTOCHECKBOX:
            try:
                ctypes.windll.uxtheme.SetWindowTheme(handle, "", "")
            except Exception:  # noqa: BLE001
                pass
        return handle

    def _button(self, cid: int, text: str, x: int, y: int, w: int, h: int, kind: str = "secondary") -> None:
        self.button_text[cid] = text
        self.button_kind[cid] = kind
        # 标准按钮：系统字体渲染中文，100% 可靠；自带按下反馈
        # 主按钮用 BS_DEFPUSHBUTTON，蓝色高亮边框更突出、点击反馈更强
        style = BS_DEFPUSHBUTTON if kind == "primary" else BS_PUSHBUTTON
        self._control("Button", cid, text, style | WS_TABSTOP, x, y, w, h)

    def _dropdown(self, cid: int, value: str, x: int, y: int, w: int, h: int = 30) -> None:
        self.button_text[cid] = value
        self.button_kind[cid] = "dropdown"
        # BS_LEFT：文字靠左（和输入框对齐），更像标准选择框
        self._control("Button", cid, value + "  ▾", BS_PUSHBUTTON | BS_LEFT | WS_TABSTOP, x, y, w, h)

    def _create_controls(self) -> None:
        # 卡片区域（x, y, w, h）：纵向铺满客户区，日志卡片吃掉剩余高度
        log_top = 582
        log_h = CLIENT_H - log_top - 16
        self.cards = [
            (16, 76, 380, 320), (410, 76, 574, 320),
            (16, 404, 620, 170), (650, 404, 334, 170),
            (16, log_top, 968, log_h),
        ]
        # 标题（独占一行）
        self._control("Static", 1, "FnosMount · NAS 磁盘映射器", SS_LEFT, 24, 10, 420, 34)
        # 操作按钮行
        self._button(ID_SCAN, "扫描局域网", 24, 46, 104, 30, "primary")
        self._button(ID_DEEP, "深度扫描", 136, 46, 96, 30)
        self._button(ID_TRAY, "最小化到托盘", 240, 46, 116, 30)
        # 状态文字与手动 IP 区
        self._control("Static", ID_STATUS, "就绪", SS_LEFT, 372, 54, 220, 22)
        self._control("Static", 2, "手动 IP", SS_LEFT, 600, 55, 52, 20)
        self._control("Edit", ID_MANUAL, "", WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 656, 48, 130, 28, 0x200)
        self._button(ID_ADD, "添加", 794, 46, 60, 30)
        # 分割线区域从卡片开始（y=86）

        # 设备列表卡片
        self._control("Static", 3, "局域网设备", SS_LEFT, 30, 84, 200, 24)
        self._control(
            "ListBox", ID_DEVICES, "", WS_BORDER | WS_VSCROLL | WS_TABSTOP | LBS_NOTIFY | LBS_HASSTRINGS,
            28, 112, 356, 276, 0x200,
        )

        # 连接与映射卡片
        self._control("Static", 4, "连接与映射", SS_LEFT, 424, 84, 200, 24)
        self._control("Static", 5, "NAS 地址", SS_LEFT, 424, 118, 62, 18)
        self._control("Edit", ID_HOST, "", WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 492, 114, 210, 26, 0x200)
        self._control("Static", 6, "协议", SS_LEFT, 716, 118, 36, 18)
        self._dropdown(ID_PROTOCOL, "SMB", 752, 112, 100)
        self._control("Static", 7, "盘符", SS_LEFT, 868, 118, 36, 18)
        self._dropdown(ID_DRIVE, "Z:", 904, 112, 70)
        self._control("Static", 8, "共享路径", SS_LEFT, 424, 152, 62, 18)
        self._control("Edit", ID_SHARE, "", WS_BORDER | WS_TABSTOP | ES_AUTOHSCROLL, 492, 148, 482, 26, 0x200)
        self._control("Static", 16, "读取到的共享目录（点击选择）", SS_LEFT, 424, 184, 240, 18)
        # 共享目录下拉框 + 「读取共享目录」按钮放在同一行
        self._dropdown(ID_SHARE_LIST, "点击选择共享目录…", 492, 204, 352, 30)
        self._button(ID_LOAD_SHARES, "读取共享目录", 852, 204, 122, 30)
        self._control("Static", 9, "用户名", SS_LEFT, 424, 250, 62, 18)
        self._control("Edit", ID_USER, "", WS_BORDER | WS_TABSTOP, 492, 246, 180, 26, 0x200)
        self._control("Static", 10, "密码", SS_LEFT, 686, 250, 36, 18)
        self._control("Edit", ID_PASS, "", WS_BORDER | WS_TABSTOP | ES_PASSWORD, 724, 246, 250, 26, 0x200)
        self._control("Button", ID_REMEMBER, "记住密码（凭据管理器）", BS_AUTOCHECKBOX | WS_TABSTOP, 424, 280, 200, 22)
        self._control("Button", ID_PERSIST, "登录后自动重连（持久映射）", BS_AUTOCHECKBOX | WS_TABSTOP, 640, 280, 260, 22)
        self._button(ID_MAP, "映射为本地磁盘", 424, 312, 176, 34, "primary")
        self._control("Static", ID_HINT, "点击设备填入地址，再读取共享目录。", SS_LEFT, 424, 352, 540, 20)

        # 当前网络磁盘卡片
        self._control("Static", 11, "当前网络磁盘", SS_LEFT, 30, 412, 200, 24)
        self._control(
            "ListBox", ID_MAPPINGS, "", WS_BORDER | WS_VSCROLL | WS_TABSTOP | LBS_NOTIFY | LBS_HASSTRINGS,
            28, 440, 470, 122, 0x200,
        )
        self._button(ID_UNMAP, "断开所选磁盘", 508, 440, 116, 34, "danger")
        self._control("Static", 12, "选中列表中的磁盘后点击右侧按钮", SS_LEFT, 508, 482, 116, 60)

        # 系统设置卡片
        self._control("Static", 13, "系统设置", SS_LEFT, 664, 412, 200, 24)
        self._control("Button", ID_AUTOSTART, "开机自动启动", BS_AUTOCHECKBOX | WS_TABSTOP, 664, 440, 300, 22)
        self._control("Button", ID_START_MIN, "开机启动后直接进入托盘", BS_AUTOCHECKBOX | WS_TABSTOP, 664, 462, 300, 22)
        self._control("Button", ID_KEEP, "保持映射（后台自动重连）", BS_AUTOCHECKBOX | WS_TABSTOP, 664, 484, 300, 22)
        self._control("Button", ID_TUNE, "大文件传输优化（需管理员）", BS_AUTOCHECKBOX | WS_TABSTOP, 664, 506, 300, 22)
        self._control("Static", 14, "检查间隔(秒)", SS_LEFT, 664, 536, 88, 18)
        self._dropdown(ID_INTERVAL, "60", 752, 530, 60, 26)
        self._button(ID_CHECK, "立即检查", 822, 528, 92, 30)

        # 日志卡片（高度跟随客户区，避免被窗口底边裁切）
        self._control("Static", 15, "运行日志", SS_LEFT, 30, log_top + 8, 200, 24)
        self._control(
            "Edit", ID_LOG, "", WS_BORDER | WS_VSCROLL | ES_MULTILINE | ES_READONLY,
            28, log_top + 34, 944, log_h - 42, 0x200,
        )

        self.combo_add(ID_PROTOCOL, ["SMB", "NFS", "WebDAV"], "SMB")
        self.combo_add(ID_DRIVE, mount.free_drive_letters() or ["Z:"], mount.suggest_drive())
        self.combo_add(ID_INTERVAL, ["30", "60", "120", "300"], str(self.settings["settings"].get("watchdog_interval", 60)))
        self.set_checked(ID_REMEMBER, True)
        self.set_checked(ID_PERSIST, True)
        self.set_checked(ID_AUTOSTART, bool(autostart.is_enabled()))
        self.set_checked(ID_START_MIN, bool(self.settings["settings"].get("start_minimized")))
        self.set_checked(ID_KEEP, bool(self.settings["settings"].get("keep_mounted", True)))
        self.set_checked(ID_TUNE, bool(self.settings["settings"].get("large_file_tuning")))
        # 参与悬停高亮动画的控件（按钮 + 下拉框）
        self.hover_ids = (
            ID_SCAN, ID_DEEP, ID_TRAY, ID_ADD, ID_LOAD_SHARES, ID_MAP, ID_UNMAP, ID_CHECK,
            ID_PROTOCOL, ID_DRIVE, ID_INTERVAL, ID_SHARE_LIST,
        )
        for control_id in self.hover_ids:
            self.hover_progress[control_id] = 0
        self.set_text(ID_LOG, "\r\n".join(self.log_lines))
        self.scroll_log_to_end()

    # ---------------- 窗口生命周期 ----------------

    def create_window(self) -> None:
        self._callback = WNDPROC(self._wnd_proc)
        instance = ctypes.windll.kernel32.GetModuleHandleW(None)
        klass = WNDCLASSW()
        klass.style = 0
        klass.lpfnWndProc = ctypes.cast(self._callback, ctypes.c_void_p).value
        klass.hInstance = instance
        klass.lpszClassName = "FnosMountNativeWindow"
        klass.hbrBackground = 5
        registered = self._register_window_class(klass)
        if not registered and ctypes.windll.kernel32.GetLastError() != 1410:
            raise OSError("注册窗口类失败")
        width, height = CLIENT_W, CLIENT_H
        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_CLIPCHILDREN
        # 按客户区尺寸反算窗口尺寸，保证底部日志卡片不被标题栏挤掉
        frame = wintypes.RECT(0, 0, CLIENT_W, CLIENT_H)
        self.u.AdjustWindowRect(ctypes.byref(frame), style, False)
        win_w = frame.right - frame.left
        win_h = frame.bottom - frame.top
        x = (self.u.GetSystemMetrics(SM_CXSCREEN) - win_w) // 2
        y = (self.u.GetSystemMetrics(SM_CYSCREEN) - win_h) // 2
        hwnd = self.u.CreateWindowExW(
            0, klass.lpszClassName, "FnosMount - NAS 磁盘映射器", style,
            x, y, win_w, win_h, None, None, instance, None,
        )
        if not hwnd:
            raise OSError(f"创建窗口失败: {ctypes.windll.kernel32.GetLastError()}")
        self.hwnd = hwnd

    def show(self) -> None:
        if self.hwnd:
            self.u.ShowWindow(self.hwnd, SW_RESTORE)
            self.u.BringWindowToTop(self.hwnd)
            self.u.SetForegroundWindow(self.hwnd)
            self.u.InvalidateRect(self.hwnd, None, True)

    def hide(self) -> None:
        if self.hwnd:
            self.close_dropdown()
            self.u.ShowWindow(self.hwnd, SW_HIDE)

    def request_exit(self) -> None:
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def run(self, enable_tray: bool = True, start_hidden: bool = False) -> None:
        self.create_window()
        self.u.SetTimer(self.hwnd, TIMER_HOVER, 40, None)
        if enable_tray:
            from .tray import TrayController
            self.tray = TrayController(
                on_open=self.show, on_reconnect=self.auto_reconnect, on_exit=self.request_exit
            )
            if self.tray.start():
                self.log("系统托盘已就绪")
        if start_hidden:
            self.hide()
        else:
            self.show()
        self.refresh_mappings()
        if self.settings["settings"].get("auto_reconnect"):
            self.auto_reconnect()
        else:
            self.start_scan(False)
        message = wintypes.MSG()
        while True:
            result = self.u.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:
                break
            self.u.TranslateMessage(ctypes.byref(message))
            self.u.DispatchMessageW(ctypes.byref(message))
        if self.tray:
            self.tray.stop()
        if self.hwnd:
            self.u.KillTimer(self.hwnd, TIMER_HOVER)
        self.watchdog.stop()

    # ---------------- 绘制 ----------------

    def _round_rect(self, hdc, rect: wintypes.RECT, brush, radius: int = 18) -> None:
        old_brush = self.g.SelectObject(hdc, brush)
        null_pen = self.g.GetStockObject(8)  # NULL_PEN：去掉默认黑边
        old_pen = self.g.SelectObject(hdc, null_pen)
        self.g.RoundRect(hdc, rect.left, rect.top, rect.right, rect.bottom, radius, radius)
        self.g.SelectObject(hdc, old_pen)
        self.g.SelectObject(hdc, old_brush)

    # ---------------- 悬停高亮动画 ----------------

    def _control_rect_client(self, control_id: int) -> wintypes.RECT | None:
        handle = self.controls.get(control_id)
        if not handle:
            return None
        rect = wintypes.RECT()
        if not self.u.GetWindowRect(handle, ctypes.byref(rect)):
            return None
        # ScreenToClient 只转换传进去的那个 POINT，矩形要按两个点分别转换
        top_left = wintypes.POINT(rect.left, rect.top)
        bottom_right = wintypes.POINT(rect.right, rect.bottom)
        self.u.ScreenToClient(self.hwnd, ctypes.byref(top_left))
        self.u.ScreenToClient(self.hwnd, ctypes.byref(bottom_right))
        return wintypes.RECT(top_left.x, top_left.y, bottom_right.x, bottom_right.y)

    def _invalidate_control(self, control_id: int) -> None:
        rect = self._control_rect_client(control_id)
        if not rect:
            return
        rect.left -= 8
        rect.top -= 8
        rect.right += 8
        rect.bottom += 8
        self.u.InvalidateRect(self.hwnd, ctypes.byref(rect), False)

    def tick_hover(self) -> None:
        """定时轮询光标下的控件，平滑推进悬停高亮进度（约 150ms 渐入淡出）。"""
        pt = wintypes.POINT()
        self.u.GetCursorPos(ctypes.byref(pt))
        under = self.u.WindowFromPoint(pt)
        target = 0
        if under:
            cid = int(self.u.GetDlgCtrlID(under))
            if cid in self.hover_progress and self.u.IsWindowEnabled(under):
                target = cid
        self.hover_id = target
        dirty: list[int] = []
        for cid, value in self.hover_progress.items():
            goal = 10 if cid == target else 0
            if value < goal:
                value = min(goal, value + 2)
            elif value > goal:
                value = max(goal, value - 3)
            if value != self.hover_progress[cid]:
                self.hover_progress[cid] = value
                dirty.append(cid)
        for cid in dirty:
            self._invalidate_control(cid)

    def _paint_hover(self, hdc) -> None:
        """在按钮/下拉框外围画一圈渐显的高亮描边，形成轻量的悬停动画。"""
        accent = (0, 122, 255)
        soft = (226, 228, 235)
        for cid, progress in self.hover_progress.items():
            if progress <= 0:
                continue
            rect = self._control_rect_client(cid)
            if not rect:
                continue
            ratio = progress / 10.0
            # 由浅灰渐变到品牌蓝，同时描边由外向内收拢，形成“亮起来”的动效
            color = self._rgb(
                int(soft[0] + (accent[0] - soft[0]) * ratio),
                int(soft[1] + (accent[1] - soft[1]) * ratio),
                int(soft[2] + (accent[2] - soft[2]) * ratio),
            )
            inset = 5 - int(3 * ratio)
            pen = self.g.CreatePen(PS_SOLID, 2, color)
            if not pen:
                continue
            old_pen = self.g.SelectObject(hdc, pen)
            old_brush = self.g.SelectObject(hdc, self.g.GetStockObject(HOLLOW_BRUSH))
            self.g.RoundRect(
                hdc, rect.left - inset, rect.top - inset,
                rect.right + inset + 1, rect.bottom + inset + 1, 12, 12,
            )
            self.g.SelectObject(hdc, old_brush)
            self.g.SelectObject(hdc, old_pen)
            self.g.DeleteObject(pen)

    def _paint_background(self, hdc) -> None:
        client = wintypes.RECT()
        self.u.GetClientRect(self.hwnd, ctypes.byref(client))
        self.u.FillRect(hdc, ctypes.byref(client), self.brushes["page"])
        for x, y, w, h in self.cards:
            shadow = wintypes.RECT(x + 2, y + 3, x + w + 2, y + h + 3)
            self._round_rect(hdc, shadow, self.brushes["shadow"], 22)
            card = wintypes.RECT(x, y, x + w, y + h)
            self._round_rect(hdc, card, self.brushes["card"], 20)
        self._paint_hover(hdc)

    def _on_ctl_color(self, message, wparam, lparam) -> int:
        hdc = wintypes.HDC(wparam)
        if message == WM_CTLCOLORSTATIC:
            # 文字背景必须与所在区域同色：页面标题贴合页面底色，卡片内标题贴合卡片白色，
            # 否则文字后面会留下一块可见的色块（灰底/白底）
            self.g.SetBkMode(hdc, TRANSPARENT)
            title_handle = self.controls.get(1)
            on_page = bool(title_handle) and int(lparam) == int(title_handle)
            brush = self.brushes["page"] if on_page else self.brushes["card"]
            return ctypes.cast(brush, ctypes.c_void_p).value or 0
        # 输入框 / 列表框：必须返回实色白画刷并设白底，
        # 否则背景不被擦拭会露出黑色块（鼠标移入触发重绘才变白）
        self.g.SetBkColor(hdc, self._rgb(255, 255, 255))
        self.g.SetTextColor(hdc, self._rgb(29, 29, 31))
        return ctypes.cast(self.brushes["input"], ctypes.c_void_p).value or 0

    # ---------------- 消息处理 ----------------

    def _wnd_proc(self, hwnd, message, wparam, lparam):  # type: ignore[no-untyped-def]
        if message == WM_CREATE:
            self.hwnd = hwnd
            try:
                self._create_controls()
            except OSError as exc:
                self.log(f"界面初始化失败: {exc}")
            return 0
        if message == WM_ERASEBKGND:
            return 1
        if message == WM_PAINT:
            ps = PAINTSTRUCT()
            hdc = self.u.BeginPaint(hwnd, ctypes.byref(ps))
            self._paint_background(hdc)
            self.u.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        if message in (WM_CTLCOLORSTATIC, WM_CTLCOLOREDIT, WM_CTLCOLORLISTBOX):
            return self._on_ctl_color(message, wparam, lparam)
        if message == WM_CTLCOLORBTN:
            # 复选框背景用卡片白色，去掉系统默认灰色底块
            hdc = wintypes.HDC(wparam)
            self.g.SetBkMode(hdc, TRANSPARENT)
            self.g.SetBkColor(hdc, self._rgb(255, 255, 255))
            return ctypes.cast(self.brushes["input"], ctypes.c_void_p).value or 0
        if message == WM_COMMAND:
            control = wparam & 0xFFFF
            notification = (wparam >> 16) & 0xFFFF
            self.on_command(control, notification)
            return 0
        if message == WM_APP_ACTIVATE:
            # 第二次启动程序时，由外部进程触发：把主页带到最前
            self.show()
            return 0
        if message == WM_APP:
            token = int(wparam)
            item = self.results.pop(token, None)
            if item:
                self.on_result(item[0], item[1])
            return 0
        if message == WM_TIMER:
            if int(wparam) == TIMER_HOVER:
                self.tick_hover()
                return 0
        if message == WM_CLOSE:
            self.close_dropdown()
            self.u.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self.u.PostQuitMessage(0)
            return 0
        return self.u.DefWindowProcW(hwnd, message, wparam, lparam)

    def on_command(self, control: int, notification: int) -> None:
        # 兜底捕获异常并写进运行日志：打包后没有控制台，异常否则会被静默吞掉
        try:
            self.dispatch_command(control, notification)
        except Exception as exc:  # noqa: BLE001
            self.log(f"操作出错({control}): {exc}")
            try:
                import traceback

                traceback.print_exc()
            except Exception:  # noqa: BLE001
                pass

    def dispatch_command(self, control: int, notification: int) -> None:
        if control == ID_SCAN and notification == BN_CLICKED:
            self.start_scan(False)
        elif control == ID_DEEP and notification == BN_CLICKED:
            self.start_scan(True)
        elif control == ID_ADD and notification == BN_CLICKED:
            self.add_host()
        elif control == ID_TRAY and notification == BN_CLICKED:
            self.minimize_to_tray()
        elif control == ID_LOAD_SHARES and notification == BN_CLICKED:
            self.load_shares()
        elif control == ID_MAP and notification == BN_CLICKED:
            self.do_map()
        elif control == ID_UNMAP and notification == BN_CLICKED:
            self.do_unmap()
        elif control == ID_CHECK and notification == BN_CLICKED:
            self.check_now()
        elif control in (ID_PROTOCOL, ID_DRIVE, ID_INTERVAL, ID_SHARE_LIST) and notification == BN_CLICKED:
            self.show_dropdown(control)
        elif control == ID_DEVICES and notification == LBN_SELCHANGE:
            self.on_device_selected()
        elif control in (ID_AUTOSTART, ID_START_MIN, ID_KEEP, ID_TUNE) and notification == BN_CLICKED:
            self.on_setting_changed(control)

    def scroll_log_to_end(self) -> None:
        """把日志框滚到最新一行（只读多行 Edit 不会自动跟随）。"""
        handle = self.controls.get(ID_LOG)
        if not handle:
            return
        try:
            self.u.SendMessageW(handle, EM_SETSEL, 0xFFFFFFFF, 0xFFFFFFFF)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.u.SendMessageW(handle, WM_VSCROLL, SB_BOTTOM, 0)
        except Exception:  # noqa: BLE001
            pass

    def on_result(self, kind: str, data: object) -> None:
        if kind == "log":
            self.set_text(ID_LOG, "\r\n".join(self.log_lines))
            self.scroll_log_to_end()
        elif kind == "scan":
            devices = data if isinstance(data, list) else []
            self.devices = devices
            self.scanning = False
            self.list_add(
                ID_DEVICES,
                [f"{d.level} | {d.name or d.hostname} | {d.ip} | 端口: {', '.join(map(str, d.ports[:5])) or '-'}" for d in devices],
            )
            self.busy(False)
            self.set_text(ID_STATUS, f"扫描完成：{len(devices)} 台设备")
        elif kind == "shares":
            found, error = data if isinstance(data, tuple) else ([], str(data))
            if found:
                self.combo_add(ID_SHARE_LIST, list(found), found[0])
                self.set_text(ID_SHARE, found[0])
            else:
                self.combo_add(ID_SHARE_LIST, ["未发现共享"], "未发现共享")
            self.busy(False)
            if error:
                self.log(str(error))
        elif kind == "operation":
            ok, message = data if isinstance(data, tuple) else (False, str(data))
            self.busy(False)
            self.set_text(ID_STATUS, message)
            self.log(message)
            if ok:
                self.refresh_mappings()
            else:
                self.u.MessageBoxW(self.hwnd, message, "操作失败", 0x00000030)
        elif kind == "refresh":
            self.refresh_mappings()

    # ---------------- 业务动作 ----------------

    def start_scan(self, deep: bool) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.busy(True)
        self.log("开始扫描局域网" + ("（深度模式）" if deep else ""))

        def work() -> None:
            try:
                self.post_result("scan", discovery.discover(timeout=3, deep=deep, log=self.log))
            except Exception as exc:  # noqa: BLE001
                self.log(f"扫描失败: {exc}")
                self.post_result("scan", [])

        threading.Thread(target=work, daemon=True).start()

    def add_host(self) -> None:
        host = self.text(ID_MANUAL)
        if not host:
            return
        self.busy(True)

        def work() -> None:
            device = discovery.probe_host(host, self.log)
            if device:
                self.devices = [d for d in self.devices if d.ip != device.ip] + [device]
                self.post_result("scan", self.devices)
                self.set_text(ID_HOST, device.ip)
            else:
                self.post_result("operation", (False, f"无法访问 {host}"))

        threading.Thread(target=work, daemon=True).start()

    def on_device_selected(self) -> None:
        selected = self.list_selection(ID_DEVICES)
        if not selected:
            return
        parts = [part.strip() for part in selected.split("|")]
        if len(parts) >= 3:
            self.set_text(ID_HOST, parts[2])

    def on_protocol_changed(self) -> None:
        protocol = self.combo_current(ID_PROTOCOL) or "SMB"
        hints = {
            "SMB": "点击“读取共享目录”列出飞牛/群晖/威联通的共享文件夹，也可手动填写。",
            "NFS": "填写 NFS 导出路径，例如 /volume1/data；需先启用 Windows NFS 客户端。",
            "WebDAV": "填写完整 WebDAV 地址，例如 http://192.168.5.11:5005/dav。",
        }
        self.set_text(ID_HINT, hints.get(protocol, ""))
        host = self.text(ID_HOST)
        if protocol == "NFS" and not self.text(ID_SHARE):
            self.set_text(ID_SHARE, "/volume1/")
        if protocol == "WebDAV" and host and not self.text(ID_SHARE):
            self.set_text(ID_SHARE, f"http://{host}:5005/")

    def load_shares(self) -> None:
        host = self.text(ID_HOST)
        if not host:
            self.u.MessageBoxW(self.hwnd, "请先选择或输入 NAS 地址", "提示", 0x40)
            return
        self.combo_add(ID_PROTOCOL, ["SMB", "NFS", "WebDAV"], "SMB")
        self.busy(True)

        def work() -> None:
            found, error = shares.list_smb_shares(
                host, self.text(ID_USER), self._password(), log=self.log
            )
            self.post_result("shares", (found, error))

        threading.Thread(target=work, daemon=True).start()

    def _password(self) -> str:
        buffer = ctypes.create_unicode_buffer(2048)
        self.u.GetWindowTextW(self.controls[ID_PASS], buffer, 2048)
        return buffer.value

    def do_map(self) -> None:
        protocol = self.combo_current(ID_PROTOCOL) or "SMB"
        host = self.text(ID_HOST)
        target = self.text(ID_SHARE)
        drive = self.combo_current(ID_DRIVE) or mount.suggest_drive()
        username = self.text(ID_USER)
        password = self._password()
        remember = self.checked(ID_REMEMBER)
        persistent = self.checked(ID_PERSIST)
        if not host or not target or not drive:
            self.u.MessageBoxW(self.hwnd, "请填写 NAS 地址、共享路径和盘符", "提示", 0x30)
            return
        credential_target = credentials.credential_target(host, target, protocol)
        if remember:
            if username and password:
                ok, message = credentials.save_credentials(credential_target, username, password)
                self.log(message if ok else f"凭据保存失败: {message}")
            elif not credentials.read_credentials(credential_target):
                self.log("未填写密码且没有已保存凭据，将尝试匿名访问")
        self.busy(True)

        def work() -> None:
            self.log(f"开始映射 {protocol}: {target} -> {drive}")
            if protocol == "NFS":
                ok, message = mount.mount_nfs(host, target, drive, self.log, options=performance.nfs_options())
            elif protocol == "WebDAV":
                ok, message = mount.mount_webdav(target, drive, username, password, persistent, self.log)
            else:
                ok, message = mount.mount_smb(host, target, drive, username, password, persistent, self.log)
            self.log(message)
            if ok:
                config.add_mapping({
                    "target": credential_target, "protocol": protocol, "host": host,
                    "share": target if protocol == "SMB" else "",
                    "url": target if protocol == "WebDAV" else "",
                    "drive": drive, "username": username, "remember": remember,
                    "persistent": persistent, "restore_on_start": persistent,
                })
            self.post_result("operation", (ok, message))

        threading.Thread(target=work, daemon=True).start()

    def refresh_mappings(self) -> None:
        rows = mount.get_current_mappings()
        self.list_add(ID_MAPPINGS, [f"{row['drive']}  ->  {row['remote']}   [{row['protocol']}]" for row in rows])
        letters = [row["drive"] for row in rows] + [d for d in mount.free_drive_letters() if d not in [r["drive"] for r in rows]]
        current = self.combo_current(ID_DRIVE)
        self.combo_add(ID_DRIVE, letters, current or (mount.suggest_drive() or letters[0] if letters else ""))

    def do_unmap(self) -> None:
        selected = self.list_selection(ID_MAPPINGS)
        if not selected:
            self.u.MessageBoxW(self.hwnd, "请先在“当前网络磁盘”列表中选择一个磁盘", "提示", 0x40)
            return
        drive = selected.split()[0]
        match = re.search(r"\[([^\]]+)\]", selected)
        protocol = match.group(1) if match else "SMB"
        if protocol not in ("NFS", "WebDAV"):
            protocol = "SMB"
        self.busy(True)

        def work() -> None:
            if protocol == "NFS":
                ok, message = mount.unmount_nfs(drive, log=self.log)
            else:
                ok, message = mount.unmount(drive, persistent=True, log=self.log)
            self.log(message)
            if ok:
                updated = config.load_config()
                for item in updated.get("mappings", []):
                    if item.get("drive") == drive:
                        item["restore_on_start"] = False
                config.save_config(updated)
            self.post_result("operation", (ok, message))

        threading.Thread(target=work, daemon=True).start()

    def auto_reconnect(self) -> None:
        def work() -> None:
            self.log("正在恢复上次的网络磁盘…")
            for drive, ok, message in config.reconnect_all(self.log):
                self.log(f"{drive}: {'成功' if ok else '失败'} - {message}")
            self.post_result("refresh", None)

        threading.Thread(target=work, daemon=True).start()

    def check_now(self) -> None:
        restored = self.watchdog.check_once()
        self.set_text(ID_STATUS, f"检查完成，恢复 {restored} 个映射")
        self.log(f"映射检查完成，恢复 {restored} 个映射")
        self.refresh_mappings()

    def minimize_to_tray(self) -> None:
        if self.tray and self.tray.available:
            self.hide()
            self.tray.notify("程序已最小化到托盘，双击图标可重新打开")
            self.log("已最小化到托盘")
        else:
            self.u.MessageBoxW(self.hwnd, "托盘不可用", "提示", 0x40)

    def on_setting_changed(self, control: int) -> None:
        if control == ID_AUTOSTART:
            ok, message = autostart.set_enabled(self.checked(ID_AUTOSTART))
            self.settings = config.update_settings(autostart=ok and self.checked(ID_AUTOSTART))
        elif control == ID_START_MIN:
            self.settings = config.update_settings(start_minimized=self.checked(ID_START_MIN))
            message = "启动方式已更新"
        elif control == ID_KEEP:
            enabled = self.checked(ID_KEEP)
            self.settings = config.update_settings(keep_mounted=enabled)
            if enabled:
                self.watchdog.start(int(self.settings["settings"].get("watchdog_interval") or 60))
            else:
                self.watchdog.stop()
            message = "映射看护已更新"
        else:
            enabled = self.checked(ID_TUNE)
            ok, message = performance.set_enabled(enabled)
            if ok:
                self.settings = config.update_settings(large_file_tuning=enabled)
        self.log(message)
        self.set_text(ID_STATUS, message)

    def update_settings(self, payload: dict) -> None:
        self.settings = config.update_settings(**payload)


_MUTEX_HANDLE: object = None  # 持有单实例互斥量，进程存活期间不释放


def _acquire_single_instance() -> bool:
    """用命名互斥量保证只运行一个实例；返回 True 表示本进程是唯一实例。"""
    global _MUTEX_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, "Local\\FnosMount_SingleInstance")
    _MUTEX_HANDLE = handle
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _activate_existing_window() -> bool:
    """把已运行实例的主页窗口带到前台；成功返回 True。

    前台权限限制下 SetForegroundWindow 常被系统忽略，因此同时用
    AttachThreadInput 挂到目标窗口线程，并给目标进程发消息让它自己显示。
    """
    u = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    try:
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    except Exception:  # noqa: BLE001
        pass
    hwnd = u.FindWindowW("FnosMountNativeWindow", None)
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    target_thread = u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    # 让本实例自己把窗口显示出来（跨进程自定义消息）
    u.PostMessageW(hwnd, WM_APP_ACTIVATE, 0, 0)
    u.ShowWindow(hwnd, SW_RESTORE)
    current_thread = kernel32.GetCurrentThreadId()
    attached = False
    if target_thread and target_thread != current_thread:
        attached = bool(u.AttachThreadInput(current_thread, target_thread, True))
    try:
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        u.FlashWindow(hwnd, True)
    finally:
        if attached:
            u.AttachThreadInput(current_thread, target_thread, False)
    return True


def launch(open_browser: bool = True, tray_only: bool = False) -> None:
    """启动原生 GUI。参数保留兼容旧命令行。"""
    if not _acquire_single_instance():
        # 已有实例在运行：激活它的主页窗口后直接退出，不再启动第二个
        _activate_existing_window()
        return
    ui = NativeUI()
    # 手动双击启动：始终直接展示主页；开机自启（--tray）时按设置静默进托盘
    hidden = bool(tray_only and ui.settings["settings"].get("start_minimized", True))
    ui.run(enable_tray=True, start_hidden=hidden)
