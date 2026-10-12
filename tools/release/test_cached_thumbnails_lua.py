"""Behavior tests for previews decoded from the parent's cached packets.

The real Lua module runs against deferred MPV callbacks and a memory filesystem.
No player, network source, user configuration or real temporary file is opened.
"""
from __future__ import annotations

import unittest
from pathlib import Path

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None


SCRIPT = Path(__file__).resolve().parents[2] / "portable_config/scripts/thumb_engine/cache_preview.lua"

MOCK = r'''
TEST = {props = {}, commands = {}, aborted = {}, files = {}, frames = {},
        clears = 0, removals = {}, remove_attempts = {}, decoder_calls = 0}
mp = {}
mp.msg = {error = function() end, warn = function() end, info = function() end,
          verbose = function() end, debug = function() end}
function mp.get_property_native(name, fallback)
    local value = TEST.props[name]
    if value == nil then return fallback end
    return value
end
mp.get_property_number = mp.get_property_native
function mp.set_property_native(name, value) TEST.props[name] = value end
function mp.command_native_async(command, callback)
    local id = #TEST.commands + 1
    TEST.commands[id] = {command = command, callback = callback}
    return id
end
function mp.abort_async_command(id) TEST.aborted[#TEST.aborted + 1] = id end
function TEST.complete(id, success, result)
    local callback = TEST.commands[id].callback
    assert(callback, 'unknown async callback')
    callback(success, result)
end
function TEST.decoder()
    TEST.decoder_calls = TEST.decoder_calls + 1
    return 'C:/fixture/mpv.exe'
end
function TEST.on_frame(data, width, height)
    TEST.frames[#TEST.frames + 1] = {data = data, w = width, h = height}
end
function TEST.on_clear() TEST.clears = TEST.clears + 1 end
local utils = {}
function utils.file_info(path)
    local data = TEST.files[path]
    if data ~= nil then return {is_file = true, size = #data} end
    return nil
end
io.open = function(path, mode)
    assert(mode == 'rb', 'unexpected file open mode')
    local data = TEST.files[path]
    if data == nil then return nil end
    return {read = function() return data end, close = function() end}
end
os.remove = function(path)
    TEST.remove_attempts[#TEST.remove_attempts + 1] = path
    if TEST.files[path] == nil then return nil, 'No such file' end
    TEST.files[path] = nil
    TEST.removals[#TEST.removals + 1] = path
    return true
end
package.preload['mp'] = function() return mp end
package.preload['mp.utils'] = function() return utils end
'''


@unittest.skipIf(LuaRuntime is None, "lupa is required for Lua behavior tests")
class CachedThumbnailsLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(MOCK)
        self.globals = self.lua.globals()
        self.test = self.globals.TEST
        self.module = self.lua.execute(SCRIPT.read_text(encoding="utf-8"))
        self.options = self.to_lua({"tnpath": "C:/fixture/thumbnail", "cache_max": 2, "sw_threads": 2})
        self.preview = self.module.new(
            self.options, self.test.decoder, self.test.on_frame, self.test.on_clear
        )
        self.cache((0, 20))
        self.test.props["rebase-start-time"] = True
        self.test.props.path = "https://source.example.test/video?token=fixture-only"

    def to_lua(self, value):
        if isinstance(value, dict):
            return self.lua.table_from({key: self.to_lua(item) for key, item in value.items()})
        if isinstance(value, list):
            return self.lua.table_from([self.to_lua(item) for item in value])
        return value

    @staticmethod
    def sequence(table):
        return [table[index] for index in range(1, len(table) + 1)]

    def cache(self, *ranges):
        self.test.props["demuxer-cache-state"] = self.to_lua({
            "seekable-ranges": [{"start": first, "end": last} for first, last in ranges]
        })

    def request(self, time, width=2, height=3):
        return self.preview.request(self.preview, time, width, height)

    def operation(self, index=None):
        index = len(self.test.commands) if index is None else index
        return self.test.commands[index].command

    def complete_dump(self, index=None, *, success=True, data="cached packets"):
        index = len(self.test.commands) if index is None else index
        command = self.operation(index)
        self.assertEqual(command[1], "dump-cache")
        if data is not None:
            self.test.files[command[4]] = data
        self.test.complete(index, success, self.to_lua({}))

    def complete_decode(self, index=None, *, success=True, code=0, data=None):
        index = len(self.test.commands) if index is None else index
        command = self.operation(index)
        self.assertEqual(command.name, "subprocess")
        args = self.sequence(command.args)
        raw = next(value[len("--o="):] for value in args if value.startswith("--o="))
        if data is None:
            data = "f" * 24
        self.test.files[raw] = data
        self.test.complete(index, success, self.to_lua({"status": code}))

    def finish_frame(self, time, *, data="f" * 24):
        self.assertTrue(self.request(time))
        self.complete_dump()
        self.complete_decode(data=data)

    def status(self):
        return self.test.props["user-data/thumb_engine/cache-preview"].status

    def test_ranges_are_start_inclusive_end_exclusive_and_do_not_bridge_gaps(self):
        cache = self.to_lua({"seekable-ranges": [
            {"start": 10, "end": 20}, {"start": 0, "end": 5},
        ]})
        for time, expected in [(0, (0, 5)), (4.999, (0, 5)), (5, None),
                               (9.999, None), (10, (10, 20)), (20, None)]:
            with self.subTest(time=time):
                self.assertEqual(self.module.range_at(cache, time), expected)

    def test_empty_and_malformed_ranges_fail_closed_without_exception(self):
        values = [None, False, "bad", {}, {"seekable-ranges": "bad"},
                  {"seekable-ranges": []}, {"seekable-ranges": [False, {},
                   {"start": "0", "end": 2}, {"start": 4, "end": 1},
                   {"start": float("nan"), "end": 2}, {"start": 0, "end": float("inf")}]}]
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(self.module.range_at(self.to_lua(value), 1))
        for time in [None, "1", False, float("nan"), float("inf"), float("-inf")]:
            with self.subTest(time=time):
                self.assertIsNone(self.module.range_at(self.test.props["demuxer-cache-state"], time))

    def test_outside_or_missing_cache_does_not_start_dump_or_child(self):
        self.cache((1, 2), (3, 4))
        for time in [0, 2, 2.5, 4]:
            self.assertFalse(self.request(time))
            self.assertEqual(self.status(), "outside-cache")
        self.test.props["demuxer-cache-state"] = None
        self.assertFalse(self.request(1.5))
        self.assertEqual(len(self.test.commands), 0)
        self.assertEqual(self.test.decoder_calls, 0)
        self.assertEqual(len(self.test.frames), 0)

    def test_dump_then_local_child_contains_no_source_url_or_auth_options(self):
        self.assertTrue(self.request(1.2))
        self.assertEqual(self.operation()[1], "dump-cache")
        self.assertEqual(self.test.decoder_calls, 0)
        self.complete_dump()
        child = self.operation()
        args = self.sequence(child.args)
        self.assertEqual(child.name, "subprocess")
        self.assertEqual(args[0], "C:/fixture/mpv.exe")
        self.assertEqual(args[-2], "--")
        self.assertTrue(args[-1].endswith(".mov"))
        for value in args:
            self.assertNotIn("https://", value)
            self.assertNotIn("http://", value)
            self.assertNotIn("token=", value)
            self.assertNotIn("cookie", value.lower())
            self.assertNotIn("http-header", value.lower())
        for option in ["--no-config", "--load-scripts=no", "--ytdl=no", "--cache=no", "--frames=1"]:
            self.assertIn(option, args)
        self.complete_decode()
        self.assertEqual(len(self.test.frames), 1)
        self.assertEqual(self.status(), "ready")

    def test_dump_request_never_exceeds_four_seconds_or_selected_cache_range(self):
        self.cache((1, 1.21), (5, 10))
        for time, beginning, ending in [(1.2, 1, 1.21), (5, 5, 7),
                                        (7.5, 5.5, 9.5), (9.9, 7.9, 10)]:
            self.preview.clear(self.preview)
            self.assertTrue(self.request(time))
            command = self.operation()
            self.assertAlmostEqual(command[2], beginning)
            self.assertAlmostEqual(command[3], ending)
            self.assertGreater(command[3] - command[2], 0)
            self.assertLessEqual(command[3] - command[2], 4 + 1e-9)

    def test_false_rebase_adds_demuxer_start_time_for_cache_and_dump(self):
        self.test.props["rebase-start-time"] = False
        self.test.props["demuxer-start-time"] = 100
        self.cache((100, 102))
        self.assertTrue(self.request(1.25))
        self.assertAlmostEqual(self.operation()[2], 100)
        self.assertAlmostEqual(self.operation()[3], 102)
        self.complete_dump()
        self.complete_decode()
        self.assertEqual(len(self.test.frames), 1)
        self.assertFalse(self.request(2))
        self.assertEqual(self.status(), "outside-cache")

    def test_mov_dump_failure_falls_back_to_mkv_before_local_decode(self):
        self.assertTrue(self.request(2))
        first = self.operation()
        self.assertTrue(first[4].endswith(".mov"))
        self.complete_dump(success=False, data=None)
        second = self.operation()
        self.assertTrue(second[4].endswith(".mkv"))
        self.assertEqual((second[2], second[3]), (first[2], first[3]))
        self.assertEqual(self.test.decoder_calls, 0)
        self.complete_dump()
        self.assertTrue(self.sequence(self.operation().args)[-1].endswith(".mkv"))
        self.complete_decode()
        self.assertEqual(self.status(), "ready")
        self.assertEqual(len(self.test.frames), 1)

    def test_empty_successful_dump_uses_fallback_and_exhaustion_shows_no_old_frame(self):
        self.finish_frame(1)
        shown = len(self.test.frames)
        self.assertTrue(self.request(2))
        self.complete_dump(data="")
        self.assertTrue(self.operation()[4].endswith(".mkv"))
        self.complete_dump(success=False, data=None)
        self.assertEqual(len(self.test.frames), shown)
        self.assertIsNone(self.preview.active)
        self.assertEqual(self.status(), "unavailable")

    def test_incomplete_bgra_or_decoder_failure_is_not_displayed(self):
        for code, data in [(0, "too short"), (1, "f" * 24)]:
            with self.subTest(code=code):
                self.preview.reset(self.preview)
                self.assertTrue(self.request(1))
                self.complete_dump()
                self.complete_decode(code=code, data=data)
                self.assertEqual(len(self.test.frames), 0)
                self.assertEqual(self.status(), "unavailable")

    def test_mousemove_keeps_only_latest_pending_request_and_suppresses_old_frame(self):
        self.assertTrue(self.request(1))
        self.assertTrue(self.request(2))
        self.assertTrue(self.request(3.1))
        self.assertTrue(self.request(3.2))
        self.assertEqual(len(self.test.commands), 1)
        self.complete_dump(1)
        self.assertEqual(len(self.test.frames), 0)
        self.assertEqual(self.operation()[1], "dump-cache")
        self.assertAlmostEqual(self.operation()[2], 1.2)
        self.complete_dump()
        self.complete_decode()
        self.assertEqual(len(self.test.frames), 1)
        self.assertEqual(self.test.props["user-data/thumb_engine/cache-preview"].time, 3.2)

    def test_shrinking_cache_clears_shown_frame_and_evicts_it(self):
        self.finish_frame(1)
        self.assertEqual(len(self.preview.order), 1)
        clears = self.test.clears
        self.cache((2, 4))
        self.preview.update_cache(self.preview)
        self.assertGreater(self.test.clears, clears)
        self.assertEqual(len(self.preview.order), 0)
        self.assertIsNone(self.preview.target)
        self.assertEqual(self.status(), "outside-cache")
        commands = len(self.test.commands)
        self.assertFalse(self.request(1))
        self.assertEqual(len(self.test.commands), commands)

    def test_shrinking_cache_aborts_active_decode_and_late_frame_cannot_return(self):
        self.assertTrue(self.request(1))
        self.complete_dump()
        decoder_id = len(self.test.commands)
        self.cache((2, 4))
        self.preview.update_cache(self.preview)
        self.assertIn(decoder_id, self.sequence(self.test.aborted))
        self.complete_decode(decoder_id)
        self.assertEqual(len(self.test.frames), 0)
        self.assertIsNone(self.preview.active)
        self.assertEqual(self.status(), "outside-cache")

    def test_evicted_frame_is_cleared_even_when_newest_hover_in_same_bin_is_cached(self):
        self.finish_frame(1.1)
        self.assertTrue(self.request(1.2))
        clears = self.test.clears
        # The cursor remains cached, but the reused frame's original packet time
        # has been evicted. It must not remain visible while its replacement runs.
        self.cache((1.15, 3))
        self.preview.update_cache(self.preview)
        self.assertEqual(len(self.preview.order), 0)
        self.assertGreater(self.test.clears, clears)
        self.assertTrue(self.request(1.2))
        self.assertEqual(self.operation()[1], "dump-cache")
        self.assertAlmostEqual(self.operation()[2], 1.15)

    def test_old_generation_dump_callback_cannot_replace_new_active_or_pending(self):
        self.assertTrue(self.request(1))
        old = 1
        self.preview.clear(self.preview)
        self.assertTrue(self.request(2))
        self.assertTrue(self.request(3))
        self.complete_dump(old)
        self.assertEqual(len(self.test.commands), 2)
        self.assertEqual(self.preview.active.time, 2)
        self.assertEqual(self.preview.pending.time, 3)
        self.complete_dump(2)
        self.assertEqual(self.operation()[2], 1)
        self.complete_dump()
        self.complete_decode()
        self.assertEqual(len(self.test.frames), 1)
        self.assertEqual(self.test.props["user-data/thumb_engine/cache-preview"].time, 3)

    def test_old_generation_decoder_callback_cannot_clear_new_active_or_pending(self):
        self.assertTrue(self.request(1))
        self.complete_dump()
        old_decoder = 2
        self.preview.reset(self.preview)
        self.assertTrue(self.request(2))
        self.assertTrue(self.request(3))
        self.complete_decode(old_decoder)
        self.assertEqual(self.preview.active.time, 2)
        self.assertEqual(self.preview.pending.time, 3)
        self.assertEqual(len(self.test.frames), 0)
        self.assertEqual(self.status(), "loading")

    def test_reusing_frame_avoids_new_work_and_dimensions_have_separate_keys(self):
        self.finish_frame(1.1)
        commands = len(self.test.commands)
        self.assertTrue(self.request(1.2))
        self.assertEqual(len(self.test.commands), commands)
        self.assertEqual(len(self.test.frames), 2)
        self.assertTrue(self.request(1.2, width=3, height=2))
        self.assertEqual(len(self.test.commands), commands + 1)
        self.assertEqual(self.status(), "loading")

    def test_memory_frames_are_bounded_and_completed_files_are_fully_untracked(self):
        for time in [1, 2, 3]:
            self.finish_frame(time)
        self.assertEqual(len(self.preview.order), 2)
        self.assertEqual(list(self.test.files.items()), [])
        self.assertEqual(list(self.preview.paths.items()), [])

    def test_reset_removes_files_cancels_job_and_late_completion_cleans_new_output(self):
        self.assertTrue(self.request(1))
        self.complete_dump()
        decoder_id = len(self.test.commands)
        self.assertGreater(len(list(self.test.files.items())), 0)
        self.preview.reset(self.preview)
        self.assertIn(decoder_id, self.sequence(self.test.aborted))
        self.assertEqual(list(self.test.files.items()), [])
        self.assertEqual(len(self.preview.order), 0)
        self.assertIsNone(self.preview.pending)
        self.complete_decode(decoder_id)
        self.assertEqual(list(self.test.files.items()), [])
        self.assertEqual(list(self.preview.paths.items()), [])
        self.assertEqual(len(self.test.frames), 0)
        self.assertEqual(self.status(), "idle")


if __name__ == "__main__":
    unittest.main()
