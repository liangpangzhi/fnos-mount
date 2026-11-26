#!/usr/bin/env python3
"""飞牛 NAS 磁盘映射器入口。

直接运行：启动图形界面
python main.py --selftest   ：命令行自检（扫描设备但不改系统）
python main.py --reconnect  ：恢复已保存的磁盘映射（供计划任务调用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

if sys.stdout is None:  # PyInstaller --windowed 模式下没有标准输出
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from fnos_mount import __version__
from fnos_mount import config, discovery, mount


def run_selftest() -> int:
    """命令行自检：打印网络环境与发现结果。"""
    from fnos_mount.common import get_local_ipv4, local_networks

    print(f"FnosMount {__version__} 自检")
    print("本机地址:", get_local_ipv4())
    print("本地网段:", ", ".join(str(net) for net in local_networks()))
    print("推荐盘符:", mount.suggest_drive())
    print("NFS 客户端:", "已启用" if mount.nfs_feature_enabled() else "未启用")
    print("WebClient 服务:", mount.webdav_service_status())
    print("已保存映射:", len(config.load_config().get("mappings", [])))
    print("-" * 48)
    devices = discovery.discover(timeout=3, deep=False, log=print)
    print(json.dumps([device.to_dict() for device in devices], ensure_ascii=False, indent=2))
    return 0 if devices else 2


def run_reconnect() -> int:
    """恢复所有配置为自动重连的映射。"""
    results = config.reconnect_all(print)
    for drive, ok, message in results:
        print(f"{drive}: {'成功' if ok else '失败'} - {message}")
    return 0 if all(ok for _, ok, _ in results) else 1


def should_run_silent_reconnect(tray: bool, reconnect: bool) -> bool:
    """只有「单独 --reconnect」（计划任务/命令行）才恢复映射后退出。

    开机自启命令是 `--tray --reconnect`，这种组合必须进 GUI 常驻托盘，
    否则用户登录后完全看不到程序（历史 bug）。
    """
    return bool(reconnect and not tray)


def main() -> int:
    parser = argparse.ArgumentParser(description="飞牛 NAS 磁盘映射器")
    parser.add_argument("--selftest", action="store_true", help="命令行自检并扫描局域网")
    parser.add_argument("--reconnect", action="store_true", help="恢复已保存的磁盘映射")
    parser.add_argument("--tray", action="store_true", help="仅在托盘运行，不自动打开浏览器")
    parser.add_argument("--version", action="version", version=f"FnosMount {__version__}")
    args = parser.parse_args()

    if args.selftest:
        return run_selftest()
    # 单独 --reconnect（计划任务/命令行）才走“恢复后退出”的静默路径；
    # --tray --reconnect（开机自启）必须进 GUI，否则用户登录后什么都看不到
    if should_run_silent_reconnect(args.tray, args.reconnect):
        return run_reconnect()

    try:
        from fnos_mount.app import launch
    except ImportError as exc:
        print(f"图形界面启动失败: {exc}", file=sys.stderr)
        return 1
    launch(open_browser=not args.tray, tray_only=args.tray, reconnect=args.reconnect)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
