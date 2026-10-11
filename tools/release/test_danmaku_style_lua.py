"""Behavior coverage for automatic danmaku style persistence through real Lua 5.1.

Uses temporary files and fresh Lua instances to check restart restoration. The
MPV API and rendering callbacks are simulated; the real menu and style code run.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


MODULE_DIR = Path(__file__).resolve().parents[2] / "portable_config/scripts/uosc_danmaku/modules"
DEFAULTS = {
    "bold": True, "fontname": "SimHei", "fontsize": 16,
    "outline": 1, "shadow": 0, "scrolltime": 15,
    "opacity": 0.7, "displayarea": 0.85, "live_fps_mode": "auto",
    "api_server": "https://never-persist.example.test/",
}
STYLE_KEYS = set(DEFAULTS) - {"api_server"}
STATUS = "user-data/uosc_danmaku/style-defaults-status"

MOCK = r'''
TEST = {props = {}, messages = {}, events = {}, commands = {}, shown = {},
        refresh_count = 0, sync_count = 0, load_count = 0, logs = {}}
mp = {}
function mp.get_script_name() return "uosc_danmaku" end
function mp.get_property_native(name, default)
    local value = TEST.props[name]
    if value == nil then return default end
    return value
end
function mp.set_property_native(name, value) TEST.props[name] = value end
mp.set_property_bool = mp.set_property_native
function mp.command_native(args)
    if args[1] == "expand-path" then return args[2] end
end
function mp.commandv(...) TEST.commands[#TEST.commands + 1] = {...} end
function mp.register_script_message(name, callback) TEST.messages[name] = callback end
function mp.register_event(name, callback) TEST.events[name] = callback end
function mp.add_timeout(seconds, callback) return {kill = function() end} end
function refresh_live_danmaku_style()
    TEST.refresh_count = TEST.refresh_count + 1
    return true
end
function sync_smooth_fps_filter() TEST.sync_count = TEST.sync_count + 1 end
function load_danmaku() TEST.load_count = TEST.load_count + 1 end
function show_message(text) TEST.shown[#TEST.shown + 1] = text end
local logger = {}
for _, level in ipairs({"error", "warn", "info", "debug"}) do
    logger[level] = function(text) TEST.logs[#TEST.logs + 1] = text end
end
package.loaded["mp"] = mp
package.loaded["mp.msg"] = logger
package.loaded["mp.utils"] = {
    parse_json = function(value) return _parse_json(value) end,
    format_json = function(value) return _format_json(value) end,
    getpid = function() return 77 end,
}
'''


@unittest.skipIf(LuaRuntime is None, "lupa is required for Lua behavior tests")
class DanmakuStyleLuaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "style.json"
        self.make_runtime()

    def to_lua(self, value):
        if isinstance(value, dict):
            return self.lua.table_from({key: self.to_lua(item) for key, item in value.items()})
        if isinstance(value, list):
            return self.lua.table_from([self.to_lua(item) for item in value])
        return value

    def to_python(self, value):
        if hasattr(value, "items"):
            items = dict(value.items())
            if items and set(items) == set(range(1, len(items) + 1)):
                return [self.to_python(items[index]) for index in range(1, len(items) + 1)]
            return {key: self.to_python(item) for key, item in items.items()}
        return value

    def parse_json(self, value):
        try:
            return self.to_lua(json.loads(value))
        except (ValueError, TypeError):
            return None

    def make_runtime(self) -> None:
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.globals = self.lua.globals()
        self.globals._parse_json = self.parse_json
        self.globals._format_json = lambda value: json.dumps(self.to_python(value), ensure_ascii=False)
        self.lua.execute(MOCK)
        self.globals.options = self.to_lua({**DEFAULTS, "style_path": str(self.path)})
        for name in ("style.lua", "menu.lua"):
            self.lua.execute((MODULE_DIR / name).read_text(encoding="utf-8"))
        self.globals.TEST.messages["uosc-version"]()
        self.globals.TEST.commands = self.lua.table()

    def message(self, name, *args):
        return self.globals.TEST.messages[name](*args)

    def activate(self, index=None, action=None):
        event = {"type": "activate"}
        if index is not None:
            event["index"] = index
        if action is not None:
            event["action"] = action
        return self.message("setup-danmaku-style", json.dumps(event))

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def last_menu(self):
        commands = self.to_python(self.globals.TEST.commands)
        for command in reversed(commands):
            if command[2] in ("open-menu", "update-menu"):
                return json.loads(command[3])
        self.fail("No menu was opened")

    def test_menu_numeric_toggle_and_fps_changes_survive_restart(self):
        self.message("setup-danmaku-style", "fontsize", " 28.9 ")
        self.message("setup-danmaku-style", "outline", "2.5")
        self.message("setup-danmaku-style", "shadow", "3")
        self.message("setup-danmaku-style", "scrolltime", "20")
        self.message("setup-danmaku-style", "opacity", "0.5")
        self.message("setup-danmaku-style", "displayarea", "0.65")
        self.activate(index=1)
        self.activate(index=8)
        expected = {**{key: DEFAULTS[key] for key in STYLE_KEYS}, "fontsize": 28,
                    "outline": 2.5, "shadow": 3, "scrolltime": 20, "opacity": 0.5,
                    "displayarea": 0.65, "bold": False, "live_fps_mode": "60"}
        self.assertEqual(self.saved(), expected)
        self.make_runtime()
        self.assertEqual(self.globals.TEST.props[STATUS], "loaded")
        for key, value in expected.items():
            self.assertEqual(self.globals.options[key], value)
        self.assertEqual(self.globals.options.api_server, DEFAULTS["api_server"])

    def test_direct_live_fps_message_saves_and_refreshes(self):
        self.message("set-live-fps-mode", "display")
        self.assertEqual(self.saved()["live_fps_mode"], "display")
        self.assertEqual(self.globals.TEST.refresh_count, 1)
        self.assertEqual(self.globals.TEST.sync_count, 1)
        self.make_runtime()
        self.assertEqual(self.globals.options.live_fps_mode, "display")

    def test_reset_saves_current_instances_original_without_resetting_on_start(self):
        self.message("setup-danmaku-style", "fontsize", "32")
        self.make_runtime()
        self.assertEqual(self.globals.options.fontsize, 32)
        self.message("setup-danmaku-style", "fontsize", "42")
        self.activate(action="fontsize")
        self.assertEqual(self.saved()["fontsize"], 32)
        self.make_runtime()
        self.assertEqual(self.globals.options.fontsize, 32)

    def test_invalid_inputs_and_unknown_events_never_write_or_mutate(self):
        for key, text in [("fontsize", "0"), ("fontsize", "501"), ("fontsize", "nan"),
                          ("outline", "-1"), ("shadow", "21"), ("scrolltime", "121"),
                          ("opacity", "1.1"), ("displayarea", "-0.01"),
                          ("live_fps_mode", "999"), ("not-a-setting", "18")]:
            self.message("setup-danmaku-style", key, text)
        for event in [{"type": "activate", "index": -1}, {"type": "activate", "index": 99},
                      {"type": "activate", "action": "api_server"}]:
            self.message("setup-danmaku-style", json.dumps(event))
        for key, value in [("fontname", ""), ("fontname", "bad\nfont"),
                           ("api_server", "https://override.example.test"),
                           ("fontsize", float("inf")), ("fontsize", {}), ("bold", "true")]:
            self.assertFalse(self.globals.set_danmaku_style_value(key, self.to_lua(value)))
        self.assertFalse(self.path.exists())
        for key, value in DEFAULTS.items():
            self.assertEqual(self.globals.options[key], value)

    def test_opening_value_input_does_not_save(self):
        self.activate(index=2)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.globals.options.fontsize, DEFAULTS["fontsize"])

    def test_clear_preserves_current_values_but_does_not_recreate_on_shutdown(self):
        self.message("setup-danmaku-style", "fontsize", "35")
        self.activate(index=10)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.globals.options.fontsize, 35)
        self.lua.execute('if TEST.events.shutdown then TEST.events.shutdown() end')
        self.assertFalse(self.path.exists())
        self.make_runtime()
        self.assertEqual(self.globals.options.fontsize, DEFAULTS["fontsize"])

    def test_after_clear_new_explicit_change_can_save_again(self):
        self.message("setup-danmaku-style", "fontsize", "35")
        self.message("clear-danmaku-style-default")
        self.message("setup-danmaku-style", "fontsize", "35")
        self.assertEqual(self.saved()["fontsize"], 35)

    def test_unchanged_value_avoids_disk_write(self):
        self.message("setup-danmaku-style", "fontsize", "28")
        content = self.path.read_bytes()
        self.lua.execute('''
            local actual_open = io.open
            io.open = function(path, mode)
                if mode == "w" then error("unexpected duplicate write") end
                return actual_open(path, mode)
            end
        ''')
        self.message("setup-danmaku-style", "fontsize", "28.0")
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(self.globals.TEST.props[STATUS], "saved")

    def test_manual_save_can_replace_file_changed_by_another_instance(self):
        self.message("setup-danmaku-style", "fontsize", "28")
        changed_elsewhere = {**self.saved(), "fontsize": 42}
        self.path.write_text(json.dumps(changed_elsewhere), encoding="utf-8")
        self.message("save-danmaku-style-default")
        self.assertEqual(self.saved()["fontsize"], 28)

    def test_load_ignores_unknown_and_invalid_fields_without_overwriting_file(self):
        source = {"fontsize": 99999, "opacity": 0.4, "bold": "yes", "live_fps_mode": "80",
                  "api_server": "https://override.example.test/"}
        self.path.write_text(json.dumps(source), encoding="utf-8")
        self.make_runtime()
        self.assertEqual(self.globals.options.fontsize, DEFAULTS["fontsize"])
        self.assertEqual(self.globals.options.bold, DEFAULTS["bold"])
        self.assertEqual(self.globals.options.opacity, 0.4)
        self.assertEqual(self.globals.options.live_fps_mode, "80")
        self.assertEqual(self.globals.options.api_server, DEFAULTS["api_server"])
        self.assertEqual(self.saved(), source)

    def test_bad_json_is_not_replaced_during_startup(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.make_runtime()
        self.assertEqual(self.globals.TEST.props[STATUS], "invalid")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_open_failure_keeps_last_saved_file_and_reports_failure(self):
        self.message("setup-danmaku-style", "fontsize", "28")
        previous = self.path.read_bytes()
        self.lua.execute('''
            local actual_open = io.open
            io.open = function(path, mode)
                if mode == "w" then return nil, "permission denied" end
                return actual_open(path, mode)
            end
        ''')
        self.message("setup-danmaku-style", "fontsize", "40")
        self.assertEqual(self.globals.options.fontsize, 40)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.globals.TEST.props[STATUS], "save-error")
        self.assertIn("自动保存失败", self.last_menu()["title"])
        self.assertIn("自动保存失败", self.globals.TEST.shown[1])

    def test_write_failure_preserves_previous_file(self):
        self.message("setup-danmaku-style", "fontsize", "28")
        previous = self.path.read_bytes()
        self.lua.execute('''
            local actual_open = io.open
            io.open = function(path, mode)
                local file = actual_open(path, mode)
                if mode ~= "w" or not file then return file end
                return {write = function() return nil, "disk full" end,
                        close = function() return file:close() end}
            end
        ''')
        self.message("setup-danmaku-style", "fontsize", "40")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.globals.TEST.props[STATUS], "save-error")
        self.assertEqual(list(self.path.parent.glob("*.tmp.*")), [])

    def test_replace_failure_restores_previous_file(self):
        self.message("setup-danmaku-style", "fontsize", "28")
        previous = self.path.read_bytes()
        self.lua.execute('''
            local actual_rename = os.rename
            os.rename = function(source, target)
                if source:match("%.tmp%.") then return nil, "replace denied" end
                return actual_rename(source, target)
            end
        ''')
        self.message("setup-danmaku-style", "fontsize", "40")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.globals.TEST.props[STATUS], "save-error")
        self.assertEqual(list(self.path.parent.glob("*.tmp.*")), [])
        self.assertEqual(list(self.path.parent.glob("*.bak.*")), [])

    def test_clear_failure_retains_persisted_values_and_error_status(self):
        self.message("setup-danmaku-style", "fontsize", "28")
        self.lua.execute('os.remove = function() return nil, "permission denied" end')
        self.activate(index=10)
        self.assertEqual(self.saved()["fontsize"], 28)
        self.assertEqual(self.globals.TEST.props[STATUS], "clear-error")
        self.assertIn("清除保存样式失败", self.last_menu()["footnote"])

    def test_menu_explains_auto_save_and_retains_manual_actions(self):
        self.message("open_danmaku_style_menu")
        menu = self.last_menu()
        self.assertIn("自动保存", menu["footnote"])
        self.assertEqual(menu["items"][-2]["title"], "立即保存当前样式")
        self.assertEqual(menu["items"][-1]["title"], "清除已保存样式")


if __name__ == "__main__":
    unittest.main()
