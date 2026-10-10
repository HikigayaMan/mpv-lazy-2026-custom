"""Behavior tests for the real live-input, parser and renderer Lua modules.

Run with the bridge development environment (lupa is a development dependency):
  tools/telegram-web-mpv-bridge/.venv/Scripts/python.exe -m unittest discover \
      -s tools/release -p test_live_danmaku_lua.py -v

The MPV API is simulated; these tests do not claim real-player visual coverage.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    # MPV's Lua code targets Lua 5.1/LuaJIT, including its format conversions.
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


MODULE_DIR = (
    Path(__file__).resolve().parents[2]
    / "portable_config/scripts/uosc_danmaku/modules"
)
SESSION = "bilibili-test-session"


MOCK_MPV = r'''
TEST = {
    clock = 0, timers = {}, messages = {}, events = {}, observers = {},
    overlays = {}, commands = {}, warnings = {}, switches = {},
    build_count = 0, visibility_writes = 0, saved_visibility = true,
    props = {
        ["time-pos"] = 100, pause = false,
        ["osd-width"] = 1920, ["osd-height"] = 1080,
        ["display-fps"] = 60, ["estimated-vf-fps"] = 60,
        vf = {}, ["track-list"] = {},
    },
}

mp = {}
function mp.get_property_native(name, fallback)
    local value = TEST.props[name]
    if value == nil then return fallback end
    return value
end
mp.get_property_bool = mp.get_property_native
mp.get_property_number = mp.get_property_native
mp.get_property = mp.get_property_native
function mp.set_property_native(name, value) TEST.props[name] = value end
mp.set_property_bool = mp.set_property_native
function mp.command_native(args)
    if args[1] == "expand-path" then return args[2] end
    return nil
end
function mp.commandv(...) TEST.commands[#TEST.commands + 1] = {...} end
function mp.register_script_message(name, callback) TEST.messages[name] = callback end
function mp.register_event(name, callback)
    TEST.events[name] = TEST.events[name] or {}
    table.insert(TEST.events[name], callback)
end
function mp.add_hook(name, priority, callback)
    mp.register_event(name, callback)
end
function mp.observe_property(name, kind, callback)
    TEST.observers[name] = TEST.observers[name] or {}
    table.insert(TEST.observers[name], callback)
    callback(name, TEST.props[name])
end
function mp.unobserve_property(callback)
    for _, callbacks in pairs(TEST.observers) do
        for i = #callbacks, 1, -1 do
            if callbacks[i] == callback then table.remove(callbacks, i) end
        end
    end
end
function mp.create_osd_overlay(kind)
    local overlay = {data = "", updates = 0, removals = 0, visible = false}
    function overlay:update()
        self.updates = self.updates + 1
        self.visible = self.data ~= ""
    end
    function overlay:remove()
        self.removals = self.removals + 1
        self.visible = false
    end
    TEST.overlays[#TEST.overlays + 1] = overlay
    return overlay
end
function mp.add_timeout(timeout, callback, disabled)
    local timer = {timeout = timeout, callback = callback, enabled = not disabled,
                   due = TEST.clock + timeout}
    function timer:kill() self.enabled = false end
    timer.stop = timer.kill
    function timer:resume()
        self.enabled = true
        self.due = TEST.clock + self.timeout
    end
    function timer:is_enabled() return self.enabled end
    TEST.timers[#TEST.timers + 1] = timer
    return timer
end
function TEST.advance(seconds)
    local target = TEST.clock + seconds
    while true do
        local next_timer = nil
        for _, timer in ipairs(TEST.timers) do
            if timer.enabled and timer.due <= target
                and (not next_timer or timer.due < next_timer.due) then
                next_timer = timer
            end
        end
        if not next_timer then break end
        if not TEST.props.pause then
            TEST.props["time-pos"] = TEST.props["time-pos"] + next_timer.due - TEST.clock
        end
        TEST.clock = next_timer.due
        next_timer.enabled = false
        next_timer.callback()
    end
    if not TEST.props.pause then
        TEST.props["time-pos"] = TEST.props["time-pos"] + target - TEST.clock
    end
    TEST.clock = target
end
function TEST.fire_property(name, value)
    TEST.props[name] = value
    local snapshot = {}
    for _, callback in ipairs(TEST.observers[name] or {}) do
        snapshot[#snapshot + 1] = callback
    end
    for _, callback in ipairs(snapshot) do callback(name, value) end
end
function TEST.fire_event(name)
    for _, callback in ipairs(TEST.events[name] or {}) do callback({}) end
end
function TEST.pending_timers()
    local count = 0
    for _, timer in ipairs(TEST.timers) do
        if timer.enabled then count = count + 1 end
    end
    return count
end
function TEST.overlay_updates()
    return TEST.overlays[1].updates + TEST.overlays[2].updates
end
function TEST.overlay_visible()
    return TEST.overlays[1].visible or TEST.overlays[2].visible
end

local logger = {}
for _, level in ipairs({"warn", "info", "error", "verbose", "debug"}) do
    logger[level] = function(value)
        if level == "warn" then TEST.warnings[#TEST.warnings + 1] = value end
    end
end
package.loaded["mp.msg"] = logger
package.loaded["mp.utils"] = {
    parse_json = function(value) return _parse_json(value) end,
    file_info = function() return nil end,
    join_path = function(left, right) return left .. "/" .. right end,
    getpid = function() return 1 end,
}
package.loaded["dicts/s2t_chars"] = {}
package.loaded["dicts/t2s_chars"] = {}

options = {
    blacklist_path = "nonexistent.txt", chConvert = 0,
    fontname = "SimHei", fontsize = 50, opacity = 1,
    scrolltime = 15, fixtime = 5, displayarea = 1,
    outline = 1, shadow = 0, bold = true,
    render_mode = "overlay", vf_fps = false, fps = 60,
    message_anlignment = 7, message_x = 10, message_y = 10,
}
ENABLED = false
HAS_DANMAKU = "user-data/uosc_danmaku/has-danmaku"
DELAY_PROPERTY = "user-data/uosc_danmaku/delay"
DANMAKU_COUNT = "user-data/uosc_danmaku/count"
DELAY = 0
PID = 1
DANMAKU_PATH = "unused"
COMMENTS = {{sentinel = "static-comments"}}
STATIC_COMMENTS = COMMENTS
DANMAKU = {sources = {persisted = {from = "test"}}}
STATIC_SOURCES = DANMAKU.sources

function toggle_danmaku_switch(value)
    TEST.switches[#TEST.switches + 1] = value
end
function get_danmaku_visibility() return TEST.saved_visibility end
function set_danmaku_visibility(value)
    TEST.visibility_writes = TEST.visibility_writes + 1
    TEST.saved_visibility = value
end
'''


@unittest.skipIf(LuaRuntime is None, "lupa is required for Lua behavior tests")
class LiveDanmakuLuaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.globals = self.lua.globals()
        self.globals._parse_json = self.parse_json
        self.lua.execute(MOCK_MPV)
        for name in ("utils.lua", "parse.lua", "render.lua", "live.lua"):
            self.lua.execute((MODULE_DIR / name).read_text(encoding="utf-8"))
        # Count conversions while preserving the real parser and lane algorithm.
        self.lua.execute('''
            local actual_builder = build_live_ass_events
            function build_live_ass_events(items)
                TEST.build_count = TEST.build_count + 1
                return actual_builder(items)
            end
        ''')
        self.test_state = self.globals.TEST
        self.state = self.globals.LIVE_DANMAKU

    def parse_json(self, value):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return self.lua.table_from(parsed, recursive=True) if isinstance(parsed, (list, dict)) else parsed

    def message(self, name, *args) -> None:
        self.test_state.messages[name](*args)

    def start(self, session=SESSION) -> None:
        self.message("live-start", session)

    def batch(self, items, session=SESSION) -> None:
        self.message("live-batch", session, json.dumps(items, ensure_ascii=False))

    def test_start_batch_render_stop_preserves_static_sources(self) -> None:
        self.start()
        self.assertTrue(self.state.active)
        self.assertEqual(self.state.session_id, SESSION)
        self.assertTrue(self.globals.ENABLED)
        self.assertEqual(self.test_state.props["user-data/uosc_danmaku/live-api-version"], 1)
        self.batch([{"text": "你好弹幕", "nick": "观众", "color": 0x112233}])
        self.test_state.advance(0.3)
        self.assertEqual(len(self.state["items"]), 1)
        self.assertEqual(len(self.state.events), 1)
        self.assertEqual(self.state.events[1].clean_text, "观众: 你好弹幕")
        self.assertIn(r"\c&H332211&", self.state.events[1].text)
        self.assertTrue(self.test_state.overlay_visible())
        self.assertTrue(self.lua.eval("rawequal(COMMENTS, STATIC_COMMENTS)"))
        self.assertTrue(self.lua.eval("rawequal(DANMAKU.sources, STATIC_SOURCES)"))
        self.assertEqual(self.test_state.visibility_writes, 0)
        self.message("live-stop", SESSION)
        self.assertFalse(self.state.active)
        self.assertFalse(self.globals.ENABLED)
        self.assertEqual(len(self.state["items"]), 0)
        self.assertEqual(len(self.state.events), 0)
        self.assertFalse(self.test_state.overlay_visible())

    def test_invalid_sessions_and_wrong_session_batches_are_rejected(self) -> None:
        for invalid in ("", "bad/session", "x" * 65):
            self.start(invalid)
            self.assertFalse(self.state.active)
        self.start()
        self.batch([{"text": "wrong room"}], session="bilibili-other")
        self.message("live-stop", "bilibili-other")
        self.assertEqual(len(self.state["items"]), 0)
        self.assertTrue(self.state.active)
        self.assertEqual(self.state.session_id, SESSION)

    def test_repeated_start_preserves_hidden_choice_and_new_session_resets_items(self) -> None:
        self.start()
        self.batch([{"text": "old room"}])
        self.assertTrue(self.globals.toggle_live_danmaku())
        self.assertFalse(self.globals.ENABLED)
        self.start()
        self.assertFalse(self.globals.ENABLED)
        self.assertEqual(self.state.session_id, SESSION)
        self.start("bilibili-new-session")
        self.assertTrue(self.globals.ENABLED)
        self.assertEqual(len(self.state["items"]), 0)

    def test_toggle_keeps_session_and_hidden_batches_do_not_reappear(self) -> None:
        self.start()
        self.batch([{"text": "first"}])
        self.test_state.advance(0.3)
        self.assertTrue(self.test_state.overlay_visible())
        self.assertTrue(self.globals.toggle_live_danmaku())
        self.assertTrue(self.state.active)
        self.assertEqual(self.state.session_id, SESSION)
        self.assertFalse(self.globals.ENABLED)
        self.assertEqual(len(self.state["items"]), 0)
        self.assertFalse(self.test_state.overlay_visible())
        hidden_updates = self.test_state.overlay_updates()
        self.batch([{"text": "discard while hidden"}])
        self.test_state.fire_property("time-pos", 101)
        self.globals.refresh_live_danmaku_style()
        self.test_state.advance(0.3)
        self.globals.render()
        self.assertEqual(len(self.state["items"]), 0)
        self.assertEqual(self.test_state.overlay_updates(), hidden_updates)
        self.assertFalse(self.test_state.overlay_visible())
        self.assertTrue(self.globals.toggle_live_danmaku())
        self.batch([{"text": "new visible message"}])
        self.test_state.advance(0.3)
        self.assertTrue(self.globals.ENABLED)
        self.assertEqual(len(self.state["items"]), 1)
        self.assertTrue(self.test_state.overlay_visible())
        self.assertEqual(self.test_state.visibility_writes, 0)

    def test_pause_discards_incoming_batches_without_replay(self) -> None:
        self.start()
        self.test_state.fire_property("pause", True)
        self.batch([{"text": "paused"}])
        self.assertEqual(len(self.state["items"]), 0)
        self.assertEqual(self.state.dropped_while_paused, 1)
        self.assertEqual(self.test_state.pending_timers(), 0)
        self.test_state.fire_property("pause", False)
        self.batch([{"text": "resumed"}])
        self.test_state.advance(0.3)
        self.assertEqual(len(self.state["items"]), 1)
        self.assertEqual(self.state.events[1].clean_text, "resumed")

    def test_hidden_ass_mode_does_not_write_subtitle_or_enable_fps_filter(self) -> None:
        self.start()
        self.globals.toggle_live_danmaku()
        self.globals.options.render_mode = "ass"
        self.globals.options.vf_fps = True
        self.lua.execute('''
            TEST.file_opens = 0
            io.open = function()
                TEST.file_opens = TEST.file_opens + 1
                return nil
            end
        ''')
        self.globals.render()
        self.globals.sync_smooth_fps_filter()
        self.assertEqual(self.test_state.file_opens, 0)
        self.assertEqual(len(self.test_state.commands), 0)

    def test_live_ass_refresh_preserves_primary_and_secondary_selection(self) -> None:
        self.lua.execute('''
            options.render_mode = "ass"
            TEST.props.sid = false
            TEST.props["secondary-sid"] = false
            TEST.props["secondary-sub-visibility"] = true
            TEST.props["secondary-sub-ass-override"] = "strip"
            TEST.primary_option = false
            TEST.secondary_option = false
            TEST.next_sub_id = 6
            io.open = function()
                return {write = function() end, close = function() end}
            end
            local actual_set = mp.set_property_native
            function mp.set_property_native(name, value)
                -- Reload changes the current track without updating the
                -- selection option. Restoring the same option is a no-op.
                if name == "sid" then
                    if value == TEST.primary_option then return end
                    TEST.primary_option = value
                elseif name == "secondary-sid" then
                    if value == TEST.secondary_option then return end
                    TEST.secondary_option = value
                end
                -- MPV cannot select the same track as both primary/secondary.
                if name == "secondary-sid" and value == TEST.props.sid then
                    actual_set(name, false)
                else
                    actual_set(name, value)
                end
            end
            function mp.commandv(...)
                local command = {...}
                TEST.commands[#TEST.commands + 1] = command
                if command[1] == "sub-add" then
                    TEST.next_sub_id = TEST.next_sub_id + 1
                    table.insert(TEST.props["track-list"], {id = TEST.next_sub_id, type = "sub",
                        title = "uosc_danmaku", ["external-filename"] = command[2]})
                elseif command[1] == "sub-reload" then
                    -- Observed with real MPV: reload promotes the track to
                    -- primary and clears its prior secondary selection.
                    TEST.props.sid = command[2]
                    TEST.props["secondary-sid"] = false
                    TEST.props["track-list"][1].title = "uosc-danmaku-1.ass"
                elseif command[1] == "sub-remove" then
                    for i = #TEST.props["track-list"], 1, -1 do
                        if TEST.props["track-list"][i].id == command[2] then
                            table.remove(TEST.props["track-list"], i)
                        end
                    end
                    if TEST.props["secondary-sid"] == command[2] then
                        TEST.props["secondary-sid"] = false
                    end
                end
            end
        ''')
        self.start()
        self.assertEqual(self.test_state.props["secondary-sid"], 7)
        self.batch([{"text": "reload fixture"}])
        self.test_state.advance(0.3)
        commands = [command[1] for _, command in self.test_state.commands.items()]
        self.assertNotIn("sub-reload", commands)
        self.assertIn("sub-remove", commands)
        self.assertEqual(commands.count("sub-add"), 2)
        self.assertFalse(self.test_state.props.sid)
        self.assertEqual(self.test_state.props["secondary-sid"], self.test_state.props["track-list"][1].id)
        self.assertEqual(self.test_state.props["secondary-sub-ass-override"], "yes")
        self.assertTrue(self.test_state.props["secondary-sub-visibility"])
        self.message("live-stop", SESSION)
        self.assertFalse(self.test_state.props["secondary-sid"])
        self.assertEqual(self.test_state.props["secondary-sub-ass-override"], "strip")

        # A pre-existing primary and secondary subtitle selection must survive
        # live updates and be restored when live danmaku is hidden/stopped.
        self.lua.execute('''
            TEST.props["track-list"] = {
                {id = 42, type = "sub", title = "primary"},
                {id = 99, type = "sub", title = "user-secondary"},
            }
            mp.set_property_native("sid", 42)
            mp.set_property_native("secondary-sid", 99)
            TEST.props["secondary-sub-visibility"] = false
        ''')
        next_session = "bilibili-with-primary"
        self.start(next_session)
        self.batch([{"text": "preserve primary"}], session=next_session)
        self.test_state.advance(0.3)
        self.assertEqual(self.test_state.props.sid, 42)
        self.assertEqual(self.test_state.props["secondary-sid"], self.test_state.next_sub_id)
        self.globals.toggle_live_danmaku()
        self.assertEqual(self.test_state.props.sid, 42)
        self.assertEqual(self.test_state.props["secondary-sid"], 99)
        self.assertFalse(self.test_state.props["secondary-sub-visibility"])
        self.assertEqual(self.test_state.props["secondary-sub-ass-override"], "strip")
        self.message("live-stop", next_session)
        self.assertEqual(self.test_state.props.sid, 42)
        self.assertEqual(self.test_state.props["secondary-sid"], 99)

    def test_sustained_batches_do_not_postpone_the_pending_refresh(self) -> None:
        self.start()
        self.batch([{"text": "first"}])
        timer_count = len(self.test_state.timers)
        for number in range(4):
            self.test_state.advance(0.05)
            self.batch([{"text": f"message {number}"}])
        self.assertEqual(len(self.test_state.timers), timer_count)
        self.assertEqual(self.test_state.pending_timers(), 1)
        self.assertEqual(self.test_state.build_count, 0)
        self.test_state.advance(0.06)
        self.assertEqual(self.test_state.build_count, 1)
        self.assertGreater(len(self.state.events), 0)
        self.assertEqual(self.test_state.pending_timers(), 0)

    def test_batch_item_and_retained_window_limits(self) -> None:
        self.start()
        self.batch([{"text": str(number)} for number in range(150)])
        self.assertEqual(len(self.state["items"]), 120)
        for _ in range(8):
            self.batch([{"text": "bounded"}] * 120)
        self.assertEqual(len(self.state["items"]), 800)
        self.assertEqual(self.test_state.props["user-data/uosc_danmaku/live-input-count"], 800)
        self.test_state.props["time-pos"] = 300
        self.globals.refresh_live_danmaku_now()
        self.assertEqual(len(self.state["items"]), 0)
        self.assertEqual(self.test_state.props["user-data/uosc_danmaku/live-input-count"], 0)

    def test_text_limits_preserve_complete_utf8_characters(self) -> None:
        self.start()
        # 160 bytes falls inside a Chinese character; 48 bytes falls inside
        # an emoji after the leading ASCII nick character.
        self.batch([{"text": "中文" * 100, "nick": "a" + "🙂" * 30}])
        display = self.state["items"][1].text  # Invalid UTF-8 raises on this read.
        nick, text = display.split(": ", 1)
        self.assertEqual(text, ("中文" * 100)[:53])
        self.assertEqual(nick, "a" + "🙂" * 11)
        self.assertLessEqual(len(text.encode("utf-8")), 160)
        self.assertLessEqual(len(nick.encode("utf-8")), 48)
        self.batch([{"text": "  \n\r\u0000 "}, {"text": "normal\nmessage\u0000"}])
        self.assertEqual(len(self.state["items"]), 2)
        self.assertEqual(self.state["items"][2].text, "normal message ")

    def test_live_text_uses_parser_ass_escaping(self) -> None:
        self.start()
        self.batch([{"text": r"{\pos(10,20)}viewer text"}])
        self.test_state.advance(0.3)
        self.assertEqual(self.state.events[1].clean_text, r"{\pos(10,20)}viewer text")
        self.assertIn(r"\{\\pos(10,20)\}viewer text", self.state.events[1].text)

    def test_oversize_malformed_and_non_text_payloads_do_not_enqueue(self) -> None:
        self.start()
        self.message("live-batch", SESSION, "x" * 65537)
        self.message("live-batch", SESSION, "not JSON")
        self.message("live-batch", SESSION, "null")
        self.batch([{"text": 1}, None, "not an object", {"text": " "}])
        self.assertEqual(len(self.state["items"]), 0)
        self.assertEqual(self.test_state.pending_timers(), 0)

    def test_end_file_cancels_pending_work_and_late_input(self) -> None:
        self.start()
        self.batch([{"text": "pending"}])
        self.assertEqual(self.test_state.pending_timers(), 1)
        self.test_state.fire_event("end-file")
        self.assertFalse(self.state.active)
        self.assertEqual(self.test_state.pending_timers(), 0)
        self.batch([{"text": "late"}])
        self.test_state.advance(1)
        self.assertEqual(self.test_state.build_count, 0)
        self.assertEqual(len(self.state["items"]), 0)
        self.assertFalse(self.test_state.overlay_visible())
        self.assertFalse(self.globals.toggle_live_danmaku())


if __name__ == "__main__":
    unittest.main()
