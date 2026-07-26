"""Focused static contract tests for the MPV browser-media bridge controller."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "portable_config"
    / "scripts"
    / "telegram_web_mpv_bridge.lua"
)


class TelegramWebMpvBridgeLuaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_executable_is_preferred_before_python_source_fallback(self) -> None:
        launcher = re.search(
            r"local function bridge_launcher\(\)(.*?)\nend",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(launcher)
        text = launcher.group(1)
        self.assertIn("browser-media-bridge.exe", self.source)
        self.assertLess(text.index("exists(bridge_exe_path)"), text.index("exists(python_path)"))
        self.assertIn("return bridge_exe_path, 'exe'", text)
        self.assertIn("return python_path, 'source'", text)

    def test_source_mode_passes_bridge_py_and_executable_mode_does_not(self) -> None:
        self.assertIn("mode == 'source' and '$bridge, ' or ''", self.source)
        self.assertIn("-FilePath $launcher -ArgumentList $args", self.source)
        self.assertIn("--http-port", self.source)
        self.assertIn("--ws-port", self.source)
        self.assertIn("--mpv-path", self.source)

    def test_stop_scope_is_limited_to_packaged_exe_or_exact_bridge_source(self) -> None:
        stop = re.search(
            r"local function stop_bridge\(\)(.*?)\nend",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(stop)
        text = stop.group(1)
        self.assertIn("browser-media-bridge.exe", text)
        self.assertIn("$_.ExecutablePath -ieq $exe", text)
        self.assertIn("$_.CommandLine.IndexOf($bridge", text)
        self.assertNotIn("*telegram-web-mpv-bridge*bridge.py*", text)

    def test_status_keeps_using_local_http_port_8999(self) -> None:
        self.assertIn("local http_port = 8999", self.source)
        self.assertIn("is_running(function(running)", self.source)
        self.assertIn("port_check_script(http_port)", self.source)

    def test_missing_dependency_hint_and_notification_persistence_are_retained(self) -> None:
        self.assertIn("源码回退也不可用", self.source)
        self.assertIn("show_notifications = false", self.source)
        self.assertIn("persist_notification_option()", self.source)
        self.assertIn("show_notifications=", self.source)


if __name__ == "__main__":
    unittest.main()
