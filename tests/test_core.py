"""核心逻辑单元测试：解析、路径处理、配置与凭据。"""

from __future__ import annotations

import os
import socket
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fnos_mount import autostart, config, credentials, mount, performance  # noqa: E402
from fnos_mount.common import IS_WINDOWS, detect_brand, format_win_error, free_drive_letters, is_fn_text  # noqa: E402
from fnos_mount.discovery import _merge, parse_dns_message, read_dns_name  # noqa: E402
from fnos_mount.shares import parse_net_view  # noqa: E402


def encode_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode("utf-8") for label in name.split(".")) + b"\x00"


class TestDiscoveryParsing(unittest.TestCase):
    def test_dns_compressed_name(self) -> None:
        first = encode_name("fnnas.local")
        data = first + b"\xc0\x00"  # 指针指回偏移量 0
        name, end = read_dns_name(data, len(first))
        self.assertEqual(name, "fnnas.local")
        self.assertEqual(end, len(data))

    def test_parse_dns_a_record(self) -> None:
        question = b"\x03fnn\x04test\x00"
        answer_name = b"\xc0\x0c"  # 压缩指针指向偏移量 12 的问题名
        packet = struct.pack("!HHHHHH", 0, 0x8180, 1, 1, 0, 0)
        packet += question + struct.pack("!HH", 1, 1)
        packet += answer_name + struct.pack("!HHIH", 1, 1, 120, 4) + socket.inet_aton("192.168.5.11")
        questions, answers = parse_dns_message(packet)
        self.assertEqual(questions, [("fnn.test", 1)])
        self.assertEqual(answers[0][1], 1)
        self.assertEqual(answers[0][4], "192.168.5.11")

    def test_merge_deduplicates_and_scores(self) -> None:
        devices = _merge(
            [
                {"ip": "192.168.1.10", "name": "mDNS", "score": 95, "source": "mDNS", "ports": [445]},
                {"ip": "192.168.1.10", "name": "Web", "score": 92, "source": "网段扫描", "ports": [2049]},
                {"ip": "192.168.1.11", "name": "低分设备", "score": 20, "source": "SSDP"},
            ]
        )
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].score, 95)
        self.assertEqual(devices[0].ports, [445, 2049])

    def test_is_fn_text(self) -> None:
        self.assertTrue(is_fn_text("飞牛 fnOS"))
        self.assertTrue(is_fn_text("FNNas Storage"))
        self.assertFalse(is_fn_text("Synology NAS"))

    def test_detect_other_brands(self) -> None:
        self.assertEqual(detect_brand("Synology DiskStation DSM"), "群晖")
        self.assertEqual(detect_brand("群晖 NAS"), "群晖")
        self.assertEqual(detect_brand("QNAP QTS 5.1"), "威联通")
        self.assertEqual(detect_brand("威联通 TS-464"), "威联通")
        self.assertEqual(detect_brand("飞牛 fnOS"), "飞牛")
        self.assertEqual(detect_brand("Synology"), "群晖")


class TestShareParsing(unittest.TestCase):
    def test_chinese_net_view(self) -> None:
        text = "共享名       类型     使用为  注释\n--------------------------------\nphoto        磁盘\nmusic        磁盘\nIPC$         IPC     远程 IPC\n"
        self.assertEqual(parse_net_view(text), ["photo", "music"])

    def test_english_net_view(self) -> None:
        text = "Share name   Type   Used as  Comment\n------------------------------------\ndata         Disk\nprinter      Printer\n"
        self.assertEqual(parse_net_view(text), ["data"])


class TestPaths(unittest.TestCase):
    def test_smb_path(self) -> None:
        self.assertEqual(mount.smb_path("192.168.5.11", "/photo/2024"), "\\\\192.168.5.11\\photo\\2024")

    def test_normalise_drive(self) -> None:
        self.assertEqual(mount.normalise_drive(" z "), "Z:")
        self.assertEqual(mount.normalise_drive(""), "")

    def test_free_drives(self) -> None:
        if IS_WINDOWS:
            self.assertTrue(free_drive_letters())
        self.assertEqual(format_win_error(0), "")


class TestConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.temp.name

    def tearDown(self) -> None:
        if self.old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.old_appdata
        self.temp.cleanup()

    def test_roundtrip(self) -> None:
        data = config.load_config()
        self.assertEqual(data["settings"]["protocol"], "SMB")
        data = config.add_mapping({"target": "t1", "drive": "Z:", "protocol": "SMB"})
        self.assertEqual(len(data["mappings"]), 1)
        self.assertEqual(config.load_config()["mappings"][0]["drive"], "Z:")
        data = config.remove_mapping("t1")
        self.assertEqual(data["mappings"], [])

    def test_nfs_tuning_option(self) -> None:
        self.assertEqual(performance.nfs_options(), "")
        config.update_settings(large_file_tuning=True)
        self.assertEqual(performance.nfs_options(), performance.NFS_TUNING)

    def test_autostart_command(self) -> None:
        command = autostart.startup_command()
        self.assertIn("--tray", command)
        self.assertIn("--reconnect", command)


@unittest.skipUnless(IS_WINDOWS, "凭据管理器仅支持 Windows")
class TestCredentials(unittest.TestCase):
    def test_save_read_delete(self) -> None:
        target = credentials.credential_target("127.0.0.1", "unit-test", "smb")
        ok, _ = credentials.save_credentials(target, "tester", "P@ss w0rd")
        self.assertTrue(ok)
        self.assertEqual(credentials.read_credentials(target), ("tester", "P@ss w0rd"))
        self.assertTrue(credentials.delete_credentials(target))
        self.assertIsNone(credentials.read_credentials(target))


if __name__ == "__main__":
    unittest.main()
