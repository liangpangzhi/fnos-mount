"""配置与已保存映射的持久化。密码不落盘，只保存凭据目标名。"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable

from . import credentials
from . import mount as mount_api

LogFn = Callable[[str], None]
CONFIG_LOCK = threading.RLock()


def _locked(func):  # type: ignore[no-untyped-def]
    """串行化配置读写与映射恢复，避免看护线程和用户操作冲突。"""
    def wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        with CONFIG_LOCK:
            return func(*args, **kwargs)
    return wrapper


APP_NAME = "FnosMount"
DEFAULT_CONFIG: dict = {
    "settings": {
        "auto_reconnect": False,
        "deep_scan": False,
        "protocol": "SMB",
        "scan_timeout": 3.0,
        "autostart": False,
        "start_minimized": False,
        "keep_mounted": True,
        "watchdog_interval": 60,
        "large_file_tuning": False,
    },
    "mappings": [],
    "tuning_backup": {},
}


def config_path() -> str:
    """返回配置文件路径。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "config.json")


def load_config() -> dict:
    """读取配置，损坏时回退到默认配置。"""
    path = config_path()
    if not os.path.isfile(path):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("配置格式错误")
        for key, value in DEFAULT_CONFIG.items():
            data.setdefault(key, value)
        data["settings"] = {**DEFAULT_CONFIG["settings"], **data.get("settings", {})}
        data["mappings"] = data.get("mappings") or []
        return data
    except (OSError, ValueError, TypeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))


@_locked
def save_config(data: dict) -> tuple[bool, str]:
    """写入配置文件。"""
    path = config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        return True, path
    except OSError as exc:
        return False, str(exc)


@_locked
def add_mapping(entry: dict) -> dict:
    """新增或更新一条映射记录。"""
    config = load_config()
    mappings = config["mappings"]
    target = entry.get("target", "")
    for index, existing in enumerate(mappings):
        if existing.get("target") == target:
            mappings[index] = {**existing, **entry}
            break
    else:
        entry.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        mappings.append(entry)
    save_config(config)
    return config


@_locked
def remove_mapping(target: str) -> dict:
    """删除一条映射记录。"""
    config = load_config()
    config["mappings"] = [item for item in config["mappings"] if item.get("target") != target]
    save_config(config)
    return config


@_locked
def update_settings(**kwargs) -> dict:
    """更新设置项。"""
    config = load_config()
    config["settings"].update(kwargs)
    save_config(config)
    return config


@_locked
def reconnect_one(entry: dict, log: LogFn = print) -> tuple[str, bool, str]:
    protocol = entry.get("protocol", "SMB").upper()
    drive = entry.get("drive", "")
    if mount_api.is_mounted(drive):
        return drive, True, f"{drive} 已挂载，跳过"
    saved = credentials.read_credentials(entry.get("target", "")) or ("", "")
    username, password = saved if entry.get("remember", True) else ("", "")
    if protocol == "NFS":
        tuned = load_config()["settings"].get("large_file_tuning")
        options = "rsize=131072,wsize=131072" if tuned else ""
        ok, message = mount_api.mount_nfs(entry.get("host", ""), entry.get("share", ""), drive, log, options=options)
    elif protocol == "WEBDAV":
        ok, message = mount_api.mount_webdav(
            entry.get("url", ""), drive, username, password, bool(entry.get("persistent")), log
        )
    else:
        ok, message = mount_api.mount_smb(
            entry.get("host", ""), entry.get("share", ""), drive, username, password, bool(entry.get("persistent")), log
        )
    return drive, ok, message


@_locked
def reconnect_all(log: LogFn = print) -> list[tuple[str, bool, str]]:
    """按保存的配置恢复所有映射。"""
    config = load_config()
    results: list[tuple[str, bool, str]] = []
    for entry in config.get("mappings", []):
        if not entry.get("restore_on_start", True):
            continue
        try:
            results.append(reconnect_one(entry, log))
        except Exception as exc:  # noqa: BLE001 - 单个映射失败不应中断其他映射
            log(f"恢复 {entry.get('drive', '?')} 时出错: {exc}")
            results.append((entry.get("drive", "?"), False, str(exc)))
    return results


def main() -> None:
    print("配置文件:", config_path())
    print("当前映射数:", len(load_config().get("mappings", [])))


if __name__ == "__main__":
    main()
