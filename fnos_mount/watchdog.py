"""持久映射看护：定期检查并恢复被断开的网络磁盘。"""

from __future__ import annotations

import threading
from typing import Callable

from . import config, mount

LogFn = Callable[[str], None]


class MappingWatchdog:
    """后台线程，发现持久映射掉线后自动重连。"""

    def __init__(self, interval: int = 60, log: LogFn = print) -> None:
        self.interval = max(10, int(interval))
        self.log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, interval: int | None = None) -> bool:
        """启动看护线程。"""
        if interval is not None:
            self.interval = max(10, int(interval))
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="FnosMountWatchdog")
        self._thread.start()
        self.log(f"映射看护已启动，每 {self.interval} 秒检查一次")
        return True

    def stop(self) -> None:
        """停止看护线程。"""
        self._stop.set()
        self._thread = None

    def check_once(self) -> int:
        """立即检查一次，返回本次恢复的映射数量。"""
        settings = config.load_config()["settings"]
        if not settings.get("keep_mounted", True):
            return 0
        restored = 0
        for entry in config.load_config().get("mappings", []):
            if not entry.get("restore_on_start", True):
                continue
            drive = entry.get("drive", "")
            if not drive or mount.is_mounted(drive):
                continue
            _, ok, message = config.reconnect_one(entry, self.log)
            if ok:
                restored += 1
                self.log(f"看护线程已恢复 {drive}: {message}")
        return restored

    def _run(self) -> None:
        # 启动后先快速检查一次，再进入周期检查
        while not self._stop.wait(self.interval):
            try:
                self.check_once()
            except Exception as exc:  # noqa: BLE001
                self.log(f"映射看护检查失败: {exc}")
