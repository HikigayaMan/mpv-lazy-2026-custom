"""Exercise global preference persistence with Lua 5.1 and real temporary files.

The MPV API is simulated. Filesystem reads, writes, renames and player restarts
use the production Lua script; these are not real-player visual checks.
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


SCRIPT = Path(__file__).resolve().parents[2] / "portable_config/scripts/save_global_props.lua"

MOCK = r'''
TEST = {clock = 0, props = {}, messages = {}, events = {}, observers = {},
        timers = {}, options = {}, writes = {}, logs = {}, osd = {}, renames = {}}
mp = {}
function mp.get_property_native(name, fallback)
    local value = TEST.props[name]
    if value == nil then return fallback end
    return value
end
mp.get_property = mp.get_property_native
function mp.set_property_native(name, value)
    if TEST.failed_setters and TEST.failed_setters[name] then
        return nil, 'simulated property error'
    end
    if name == 'vf' then
        for _, filter in ipairs(value) do
            if type(filter.params) == 'table' and next(filter.params) == nil then
                local meta = getmetatable(filter.params)
                if not meta or meta.__mpv_node_type ~= 'map' then
                    TEST.rejected_empty_params = true
                    return nil, 'invalid empty filter params node'
                end
            end
        end
    end
    TEST.props[name] = value
    TEST.writes[#TEST.writes + 1] = {name, value}
    for _, callback in ipairs(TEST.observers[name] or {}) do callback(name, value) end
    return true
end
function mp.command_native(args)
    if args[1] == 'expand-path' then return TEST.save_path end
end
function mp.observe_property(name, kind, callback)
    TEST.observers[name] = TEST.observers[name] or {}
    table.insert(TEST.observers[name], callback)
    callback(name, TEST.props[name])
end
function mp.register_script_message(name, callback) TEST.messages[name] = callback end
function mp.register_event(name, callback)
    TEST.events[name] = TEST.events[name] or {}
    table.insert(TEST.events[name], callback)
end
function mp.add_timeout(timeout, callback)
    local timer = {timeout = timeout, callback = callback, enabled = true,
                   due = TEST.clock + timeout}
    function timer:kill() self.enabled = false end
    TEST.timers[#TEST.timers + 1] = timer
    return timer
end
function mp.osd_message(value) TEST.osd[#TEST.osd + 1] = value end
function TEST.advance(seconds)
    local target = TEST.clock + seconds
    while true do
        local next_timer
        for _, timer in ipairs(TEST.timers) do
            if timer.enabled and timer.due <= target and
                (not next_timer or timer.due < next_timer.due) then next_timer = timer end
        end
        if not next_timer then break end
        TEST.clock = next_timer.due
        next_timer.enabled = false
        next_timer.callback()
    end
    TEST.clock = target
end
function TEST.fire(name)
    for _, callback in ipairs(TEST.events[name] or {}) do callback() end
end
function TEST.change(name, value)
    TEST.props[name] = value
    for _, callback in ipairs(TEST.observers[name] or {}) do callback(name, value) end
end
local real_open, real_rename = io.open, os.rename
io.open = function(filename, mode)
    local file, reason = real_open(filename, mode)
    if not file or mode ~= 'wb' or not (TEST.fail_write or TEST.fail_close) then
        return file, reason
    end
    return {
        write = function(_, value)
            if TEST.fail_write then error('simulated write error') end
            return file:write(value)
        end,
        close = function()
            local closed = file:close()
            if TEST.fail_close then return nil, 'simulated close error' end
            return closed
        end,
    }
end
os.rename = function(source, target)
    TEST.renames[#TEST.renames + 1] = {source, target}
    if TEST.fail_promote and target == TEST.save_path and source:find('.tmp-', 1, true)
        then return nil, 'simulated promotion error' end
    if TEST.fail_backup_move and source == TEST.save_path and target == TEST.save_path .. '.bak'
        then return nil, 'simulated backup error' end
    if TEST.windows_rename then
        local existing = real_open(target, 'rb')
        if existing then existing:close(); return nil, 'simulated Windows target exists' end
    end
    return real_rename(source, target)
end
package.loaded['mp'] = mp
package.loaded['mp.options'] = {read_options = function(options)
    for key, value in pairs(TEST.options) do options[key] = value end
end}
package.loaded['mp.utils'] = {
    getpid = function() return TEST.pid end,
    parse_json = function(value) return _parse_json(value) end,
    format_json = function(value)
        if TEST.fail_format then error('simulated JSON error') end
        return _format_json(value)
    end,
}
package.loaded['mp.msg'] = {}
for _, level in ipairs({'warn', 'error', 'info'}) do
    package.loaded['mp.msg'][level] = function(value) TEST.logs[#TEST.logs + 1] = value end
end
'''


class Player:
    def __init__(self, path: Path, pid: int, props=None, options=None, failed_setters=None):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.g = self.lua.globals()
        self.g._parse_json = self.parse_json
        self.g._format_json = self.format_json
        self.lua.execute(MOCK)
        self.state = self.g.TEST
        self.state.save_path = path.as_posix()
        self.state.pid = pid
        self.state.failed_setters = self.native({name: True for name in (failed_setters or [])})
        defaults = {"volume": 50, "mute": False, "vf": [], "glsl-shaders": [],
                    "deband": False, "brightness": 0, "hwdec": "auto",
                    "watch-later-options": "start,aid,sid"}
        defaults.update(props or {})
        for name, value in defaults.items():
            self.state.props[name] = self.native(value)
        for name, value in (options or {}).items():
            self.state.options[name] = self.native(value)
        self.lua.execute(SCRIPT.read_text(encoding="utf-8"))

    def native(self, value):
        if not isinstance(value, (dict, list)):
            return value
        # MPV native/JSON nodes carry a metatable to distinguish an empty map
        # from an empty array. A plain empty Lua table serializes as an array.
        table = self.lua.table()
        items = value.items() if isinstance(value, dict) else enumerate(value, 1)
        for key, item in items:
            table[key] = self.native(item)
        node_kind = "map" if isinstance(value, dict) else "array"
        self.g.setmetatable(table, self.lua.table_from({"__mpv_node_type": node_kind}))
        return table

    def python(self, value, key=None):
        if not hasattr(value, "items"):
            return value
        items = dict(value.items())
        if not items:
            meta = self.g.getmetatable(value)
            return {} if meta is not None and meta["__mpv_node_type"] == "map" else []
        if items and all(isinstance(k, int) for k in items) and set(items) == set(range(1, len(items) + 1)):
            return [self.python(items[i]) for i in range(1, len(items) + 1)]
        return {k: self.python(v, k) for k, v in items.items()}

    def parse_json(self, value):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return self.native(parsed)

    def format_json(self, value):
        return json.dumps(self.python(value), ensure_ascii=False)

    def change(self, name, value):
        self.state.change(name, self.native(value))

    def remember(self, *names):
        self.state.messages.remember(",".join(names))

    def advance(self, seconds=0.25):
        self.state.advance(seconds)

    def fire(self, name):
        self.state.fire(name)

    def prop(self, name):
        return self.python(self.state.props[name], name)

    @property
    def status(self):
        return self.prop("user-data/save_global_props/status")


@unittest.skipIf(LuaRuntime is None, "lupa is required")
class SaveGlobalPropsLuaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mpv-settings-")
        self.path = Path(self.directory.name) / "saved-props.json"
        self.pid = 1000

    def tearDown(self):
        self.directory.cleanup()

    def player(self, props=None, options=None, path=None, failed_setters=None):
        self.pid += 1
        return Player(path or self.path, self.pid, props=props, options=options,
                      failed_setters=failed_setters)

    def write(self, value, path=None):
        (path or self.path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def read(self, path=None):
        return json.loads((path or self.path).read_text(encoding="utf-8"))

    def test_explicit_menu_preferences_restore_across_new_player(self):
        first = self.player()
        chosen = {"hwdec": "no", "deband": True, "brightness": 15,
                  "glsl-shaders": ["C:/Shaders/FSRCNNX.glsl"],
                  "vf": [{"name": "hflip", "params": {}}]}
        for name, value in chosen.items():
            first.change(name, value)
        first.remember(*chosen)
        first.advance()
        self.assertEqual(self.read(), chosen)
        second = self.player(props={"brightness": -30, "deband": False})
        for name, value in chosen.items():
            self.assertEqual(second.prop(name), value)
        self.assertEqual(second.status, "loaded")

    def test_false_and_empty_filter_and_shader_choices_survive_restart(self):
        self.write({"deband": True, "vf": [{"name": "hflip"}],
                    "glsl-shaders": ["C:/Shaders/old.glsl"]})
        first = self.player()
        for name, value in {"deband": False, "vf": [], "glsl-shaders": []}.items():
            first.change(name, value)
            first.remember(name)
        first.advance()
        self.assertEqual(self.read(), {"deband": False, "vf": [], "glsl-shaders": []})
        second = self.player(props={"deband": True, "vf": [{"name": "vflip"}],
                                    "glsl-shaders": ["C:/Shaders/default.glsl"]})
        self.assertFalse(second.prop("deband"))
        self.assertEqual(second.prop("vf"), [])
        self.assertEqual(second.prop("glsl-shaders"), [])

    def test_empty_filter_parameter_map_keeps_native_node_type_across_restart(self):
        first = self.player()
        first.change("vf", [{"name": "hflip", "params": {}}])
        first.remember("vf")
        first.advance()
        self.assertEqual(self.read(), {"vf": [{"name": "hflip", "params": {}}]})
        second = self.player()
        self.assertIsNone(second.state.rejected_empty_params)
        self.assertEqual(second.prop("vf"), [{"name": "hflip", "params": {}}])
        self.assertEqual(len(second.state.logs), 0)

    def test_nil_property_error_warns_and_does_not_interrupt_other_restores(self):
        self.write({"deband": True, "brightness": 8})
        player = self.player(failed_setters=["deband"])
        self.assertFalse(player.prop("deband"))
        self.assertEqual(player.prop("brightness"), 8)
        self.assertEqual(len(player.state.logs), 1)
        self.assertEqual(player.status, "loaded")

    def test_explicit_current_default_is_saved_against_future_profiles(self):
        first = self.player()
        first.remember("deband")
        first.advance()
        self.assertEqual(self.read(), {"deband": False})
        self.assertFalse(self.player(props={"deband": True}).prop("deband"))

    def test_automatic_profile_changes_without_menu_request_do_not_write(self):
        player = self.player()
        player.change("deband", True)
        player.change("brightness", -10)
        player.change("vf", [{"name": "fps", "params": {"fps": "60"}}])
        player.fire("file-loaded")
        player.advance()
        player.fire("shutdown")
        self.assertFalse(self.path.exists())
        self.assertTrue(player.prop("deband"))

    def test_file_loaded_restores_explicit_values_after_profiles_and_keeps_temporary_filters(self):
        self.write({"deband": False, "vf": [{"name": "hflip"}], "brightness": 5})
        player = self.player()
        temporary = [{"name": "sub", "label": "danmaku"},
                     {"name": "fps", "label": "@danmaku_smooth", "params": {"fps": "60"}}]
        player.change("deband", True)
        player.change("brightness", -10)
        player.change("vf", [{"name": "vflip"}] + temporary)
        player.fire("file-loaded")
        self.assertTrue(player.prop("deband"))
        player.advance(0.09)
        self.assertTrue(player.prop("deband"))
        player.advance(0.02)
        self.assertFalse(player.prop("deband"))
        self.assertEqual(player.prop("brightness"), 5)
        self.assertEqual(player.prop("vf"), [{"name": "hflip"}] + temporary)

    def test_temporary_danmaku_filters_are_excluded_from_disk(self):
        player = self.player()
        player.change("vf", [{"name": "hflip"}, {"name": "sub", "label": "danmaku"},
                             {"name": "fps", "label": "@danmaku_smooth"}])
        player.remember("vf")
        player.advance()
        self.assertEqual(self.read()["vf"], [{"name": "hflip"}])
        restored = self.player(props={"vf": [{"name": "sub", "label": "danmaku"}]})
        self.assertEqual(restored.prop("vf"), [{"name": "hflip"}, {"name": "sub", "label": "danmaku"}])

    def test_old_flat_json_volume_and_mute_restore_and_continue_autosaving(self):
        self.write({"volume": 71, "mute": True})
        player = self.player()
        self.assertEqual(player.prop("volume"), 71)
        self.assertTrue(player.prop("mute"))
        player.change("volume", 35)
        player.change("mute", False)
        player.advance()
        self.assertEqual(self.read(), {"volume": 35, "mute": False})

    def test_dirty_keys_merge_between_players_and_idle_shutdown_preserves_newer_data(self):
        self.write({"volume": 60, "brightness": 2})
        idle = self.player()
        first, second = self.player(), self.player()
        first.change("brightness", 12)
        first.remember("brightness")
        second.change("deband", True)
        second.remember("deband")
        first.advance()
        second.advance()
        idle.fire("shutdown")
        self.assertEqual(self.read(), {"volume": 60, "brightness": 12, "deband": True})

    def test_reselecting_same_local_value_overrides_another_players_newer_choice(self):
        self.write({"deband": False})
        first, second = self.player(), self.player()
        second.change("deband", True)
        second.remember("deband")
        second.advance()
        self.assertEqual(self.read(), {"deband": True})
        first.remember("deband")
        first.advance()
        self.assertEqual(self.read(), {"deband": False})

    def test_tracked_shutdown_only_mode_still_flushes_changes(self):
        player = self.player(options={"save_mode": 1})
        player.change("volume", 31)
        player.advance()
        self.assertFalse(self.path.exists())
        player.fire("shutdown")
        self.assertEqual(self.read(), {"volume": 31})

    def test_debounce_saves_latest_choice_once(self):
        player = self.player()
        player.change("brightness", 2)
        player.remember("brightness")
        player.advance(0.15)
        self.assertFalse(self.path.exists())
        player.change("brightness", 9)
        player.remember("brightness")
        player.advance(0.1)
        self.assertFalse(self.path.exists())
        player.advance(0.11)
        self.assertEqual(self.read(), {"brightness": 9})
        self.assertEqual(len(player.state.renames), 1)
        self.assertEqual(player.status, "saved")

    def test_shutdown_flushes_pending_change(self):
        player = self.player()
        player.change("gamma", 8)
        player.remember("gamma")
        self.assertFalse(self.path.exists())
        player.fire("shutdown")
        self.assertEqual(self.read(), {"gamma": 8})

    def test_clear_cancels_save_and_restore_and_shutdown_does_not_resurrect(self):
        self.write({"brightness": 15, "deband": True})
        backup = self.path.with_suffix(".json.bak")
        self.write({"brightness": 20}, backup)
        player = self.player()
        player.change("brightness", 5)
        player.remember("brightness")
        player.fire("file-loaded")
        player.state.messages.clean_data()
        self.assertEqual(player.status, "cleared")
        self.assertFalse(backup.exists())
        player.advance(1)
        player.fire("shutdown")
        self.assertIn(self.read(), ({}, []))
        self.assertEqual(player.prop("brightness"), 5)
        self.assertEqual(self.player(props={"brightness": -5}).prop("brightness"), -5)

    def test_unknown_properties_and_invalid_values_are_ignored(self):
        player = self.player()
        player.change("path", "https://example.test/secret")
        player.change("brightness", 101)
        player.change("glsl-shaders", ["unsafe\nshader"])
        player.change("vf", [{"label": "missing-name"}])
        player.remember("path", "brightness", "glsl-shaders", "vf")
        player.advance()
        player.fire("shutdown")
        self.assertFalse(self.path.exists())

    def test_unwritable_destination_reports_error_without_raising_and_can_retry(self):
        missing = self.path.parent / "missing" / "saved-props.json"
        player = self.player(path=missing)
        player.change("brightness", 6)
        player.remember("brightness")
        player.advance()
        self.assertEqual(player.status, "save-error")
        self.assertGreater(len(player.state.osd), 0)
        missing.parent.mkdir()
        player.fire("shutdown")
        self.assertEqual(self.read(missing), {"brightness": 6})
        self.assertEqual(player.status, "saved")

    def test_json_encoding_exception_keeps_original_and_allows_retry(self):
        self.write({"brightness": 3})
        player = self.player()
        player.state.fail_format = True
        player.change("brightness", 8)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), {"brightness": 3})
        self.assertEqual(player.status, "save-error")
        player.state.fail_format = False
        player.fire("shutdown")
        self.assertEqual(self.read(), {"brightness": 8})

    def test_file_write_exception_keeps_original_and_removes_temporary_file(self):
        self.write({"brightness": 3})
        player = self.player()
        player.state.fail_write = True
        player.change("brightness", 8)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), {"brightness": 3})
        self.assertEqual(player.status, "save-error")
        self.assertFalse(list(self.path.parent.glob("*.tmp-*")))
        player.state.fail_write = False
        player.fire("shutdown")
        self.assertEqual(self.read(), {"brightness": 8})

    def test_file_close_failure_keeps_original_and_removes_temporary_file(self):
        self.write({"brightness": 3})
        player = self.player()
        player.state.fail_close = True
        player.change("brightness", 8)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), {"brightness": 3})
        self.assertEqual(player.status, "save-error")
        self.assertFalse(list(self.path.parent.glob("*.tmp-*")))

    def test_corrupt_primary_recovers_backup_and_preserves_it_when_merging(self):
        self.path.write_text("{broken", encoding="utf-8")
        self.write({"deband": True, "volume": 42}, self.path.with_suffix(".json.bak"))
        player = self.player()
        self.assertTrue(player.prop("deband"))
        self.assertEqual(player.prop("volume"), 42)
        player.change("brightness", 7)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), {"deband": True, "volume": 42, "brightness": 7})

    def test_oversized_primary_uses_backup_instead_of_parsing_unbounded_data(self):
        self.path.write_text(" " * (1024 * 1024 + 1), encoding="utf-8")
        self.write({"volume": 42}, self.path.with_suffix(".json.bak"))
        player = self.player()
        self.assertEqual(player.prop("volume"), 42)

    def test_windows_existing_target_is_backed_up_before_successful_promotion(self):
        original = {"brightness": 3}
        self.write(original)
        player = self.player()
        player.state.windows_rename = True
        player.change("brightness", 14)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), {"brightness": 14})
        self.assertEqual(self.read(self.path.with_suffix(".json.bak")), original)
        self.assertEqual(player.status, "saved")
        self.assertFalse(list(self.path.parent.glob("*.tmp-*")))

    def test_windows_promotion_failure_rolls_back_and_retains_dirty_choice_for_retry(self):
        original = {"brightness": 3, "volume": 40}
        self.write(original)
        player = self.player()
        player.state.windows_rename = True
        player.state.fail_promote = True
        player.change("brightness", 14)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), original)
        self.assertEqual(player.status, "save-error")
        self.assertFalse(list(self.path.parent.glob("*.tmp-*")))
        player.state.fail_promote = False
        player.fire("shutdown")
        self.assertEqual(self.read(), {"brightness": 14, "volume": 40})
        self.assertEqual(player.status, "saved")

    def test_windows_backup_move_failure_keeps_original_file(self):
        original = {"brightness": 3}
        self.write(original)
        player = self.player()
        player.state.windows_rename = True
        player.state.fail_backup_move = True
        player.change("brightness", 14)
        player.remember("brightness")
        player.advance()
        self.assertEqual(self.read(), original)
        self.assertEqual(player.status, "save-error")
        self.assertFalse(list(self.path.parent.glob("*.tmp-*")))


if __name__ == "__main__":
    unittest.main()
