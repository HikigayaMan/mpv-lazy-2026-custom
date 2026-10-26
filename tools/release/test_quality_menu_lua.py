"""Behavior coverage for browser stream qualities and bounded discovery.

Uses the real quality-menu Lua source with MPV's API simulated by Lua 5.1.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import quote

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

SCRIPT = Path(__file__).resolve().parents[2] / "portable_config/scripts/quality-menu.lua"
VIDEO_720 = "https://media.example.test/720.m4s?token=a%2Cb"
VIDEO_1080 = "https://media.example.test/1080.m4s?token=c"
AUDIO = "https://media.example.test/audio.m4s?token=d"


def dash_edl(video, audio):
    return (f"memory://# mpv EDL v0\n!no_clip;!no_chapters;%{len(video.encode('utf-8'))}%{video}"
            f";!new_stream;!no_clip;!no_chapters;%{len(audio.encode('utf-8'))}%{audio}")


MOCK = r'''
TEST = {clock = 0, props = {}, commands = {}, messages = {}, events = {},
        observers = {}, timers = {}, bindings = {}, logs = {}, options = {}}
mp = {}
function mp.get_script_name() return 'quality_menu' end
function mp.get_time() return TEST.clock end
function mp.get_property_native(name, fallback)
    local value = TEST.props[name]
    if value == nil then return fallback end
    return value
end
function mp.get_property(name, fallback)
    local value = mp.get_property_native(name, fallback)
    -- MPV's user-data strings are JSON nodes: string access includes quotes.
    if name:match('^user%-data/') and type(value) == 'string' then
        return '"' .. value .. '"'
    end
    return value
end
mp.get_property_number = mp.get_property_native
mp.get_property_bool = mp.get_property_native
function mp.set_property_native(name, value) TEST.props[name] = value end
mp.set_property = mp.set_property_native
mp.set_property_bool = mp.set_property_native
function mp.commandv(...) TEST.commands[#TEST.commands + 1] = {...} end
function mp.command(value) TEST.commands[#TEST.commands + 1] = {value} end
function mp.add_key_binding(key, name, callback) TEST.bindings[name] = callback end
mp.add_forced_key_binding = mp.add_key_binding
function mp.remove_key_binding(name) TEST.bindings[name] = nil end
function mp.register_script_message(name, callback) TEST.messages[name] = callback end
function mp.register_event(name, callback)
    TEST.events[name] = TEST.events[name] or {}
    table.insert(TEST.events[name], callback)
end
function mp.unregister_event(callback)
    for _, events in pairs(TEST.events) do
        for i = #events, 1, -1 do if events[i] == callback then table.remove(events, i) end end
    end
end
function mp.add_hook(name, priority, callback) mp.register_event(name, callback) end
function mp.observe_property(name, kind, callback)
    TEST.observers[name] = callback
end
function mp.unobserve_property() end
function mp.get_osd_size() return 1920, 1080, 16 / 9 end
function mp.create_osd_overlay()
    local overlay = {data = '', hidden = true}
    function overlay:update() TEST.osd = self.data end
    return overlay
end
function mp.add_timeout(timeout, callback)
    local timer = {timeout = timeout, callback = callback, enabled = true, due = TEST.clock + timeout}
    function timer:kill() self.enabled = false end
    function timer:resume() self.enabled = true; self.due = TEST.clock + self.timeout end
    TEST.timers[#TEST.timers + 1] = timer
    return timer
end
function TEST.advance(seconds)
    local target = TEST.clock + seconds
    while true do
        local next_timer
        for _, timer in ipairs(TEST.timers) do
            if timer.enabled and timer.due <= target and (not next_timer or timer.due < next_timer.due)
                then next_timer = timer end
        end
        if not next_timer then break end
        TEST.clock = next_timer.due; next_timer.enabled = false; next_timer.callback()
    end
    TEST.clock = target
end
function TEST.fire(name, event)
    local snapshot = {}
    for _, callback in ipairs(TEST.events[name] or {}) do snapshot[#snapshot + 1] = callback end
    for _, callback in ipairs(snapshot) do callback(event or {}) end
end
package.loaded['mp'] = mp
package.loaded['mp.msg'] = {}
for _, level in ipairs({'warn', 'error', 'info', 'verbose'}) do
    package.loaded['mp.msg'][level] = function(value) TEST.logs[#TEST.logs + 1] = value end
end
package.loaded['mp.options'] = {read_options = function(opts)
    for key, value in pairs(TEST.options) do opts[key] = value end
end}
package.loaded['mp.utils'] = {
    parse_json = function(value) return _parse_json(value) end,
    format_json = function(value) TEST.menu = value; return 'menu-json' end,
}
package.loaded['mp.assdraw'] = {ass_new = function()
    local ass = {text = ''}
    function ass:append(value) self.text = self.text .. value end
    function ass:pos() end
    function ass:draw_start() end
    function ass:draw_stop() end
    function ass:rect_cw() end
    function ass:new_event() end
    return ass
end}
'''


@unittest.skipIf(LuaRuntime is None, "lupa is required")
class QualityMenuLuaTests(unittest.TestCase):
    def load(self, path=VIDEO_720, variants=None, **options):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.g = self.lua.globals()
        self.g._parse_json = self.parse_json
        self.lua.execute(MOCK)
        self.state = self.g.TEST
        self.state.props.path = path
        self.state.props["ytdl"] = False
        self.state.props["video-params"] = self.lua.table_from({"w": 1280, "h": 720})
        for key, value in options.items():
            self.state.options[key] = value
        if variants is not None:
            self.state.options.stream_variants = quote(json.dumps(variants, ensure_ascii=False), safe="")
        self.lua.execute(SCRIPT.read_text(encoding="utf-8"))
        self.state.messages["uosc-version"]("5.0.0")
        self.state.fire("start-file")

    def parse_json(self, value):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
        return self.lua.table_from(value, recursive=True) if isinstance(value, (list, dict)) else value

    def variants(self):
        return [
            {"url": VIDEO_720, "audio_url": AUDIO, "title": "720P", "width": 1280, "height": 720},
            {"url": VIDEO_1080, "audio_url": AUDIO, "title": "1080P", "width": 1920, "height": 1080, "fps": 60},
        ]

    def open(self):
        self.state.bindings.video_formats_toggle()

    def menu_items(self):
        return list(self.state.menu["items"].values())

    def commands(self):
        return [list(value.values()) for value in self.state.commands.values()]

    def test_direct_flv_without_variants_shows_current_quality_without_spinner(self):
        self.load("https://media.example.test/live.flv?token=e")
        self.state.fire("file-loaded")
        self.open()
        items = self.menu_items()
        self.assertEqual(items[-1].title, "720p（当前源）")
        self.assertNotIn("spinner", [item.icon for item in items])
        self.assertIn("网页", items[-1].hint)

    def test_browser_dash_qualities_sort_select_and_keep_audio_and_headers(self):
        self.load(variants=self.variants())
        self.state.fire("file-loaded")
        headers = self.lua.table_from(["referer: https://www.bilibili.com/", "cookie: private"])
        self.state.props["http-header-fields"] = headers
        playlist = self.lua.table_from([{"id": 7, "filename": VIDEO_720},
                                        {"id": 8, "filename": VIDEO_1080}], recursive=True)
        self.state.props.playlist = playlist
        self.open()
        items = self.menu_items()
        self.assertEqual([item.title for item in items[1:]], ["1080P", "720P"])
        self.assertTrue(items[-1].active)
        diagnostic = self.state.props["user-data/quality-menu/state"]
        self.assertEqual(diagnostic.status, "ready")
        self.assertEqual(diagnostic.source, "browser")
        self.assertEqual(diagnostic.videoCount, 2)
        self.assertEqual(diagnostic.activeHeight, 720)
        self.assertNotIn("url", diagnostic.video[1])
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        self.assertIn(["loadfile", VIDEO_1080, "replace"], self.commands())
        self.state.props.path = VIDEO_1080
        self.state.fire("start-file")
        diagnostic = self.state.props["user-data/quality-menu/state"]
        self.assertEqual(diagnostic.status, "loading")
        self.assertIsNone(diagnostic.activeHeight)
        self.state.fire("on_load")
        self.assertEqual(len(self.state.props["file-local-options/audio-files"]), 0)
        self.assertEqual(self.state.props["stream-open-filename"], dash_edl(VIDEO_1080, AUDIO))
        self.assertEqual(self.state.props.path, VIDEO_1080)
        self.assertEqual([entry.id for entry in self.state.props.playlist.values()], [7, 8])
        self.assertFalse(self.state.props["file-local-options/ytdl"])
        self.assertEqual(list(self.state.props["http-header-fields"].values()),
                         ["referer: https://www.bilibili.com/", "cookie: private"])
        self.state.fire("file-loaded")
        self.open()
        self.assertTrue(self.menu_items()[1].active)

    def test_muxed_quality_clears_external_dash_audio_for_that_file(self):
        variants = self.variants()
        variants[1].pop("audio_url")
        self.load(variants=variants)
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        self.state.props.path = VIDEO_1080
        self.state.fire("on_load")
        self.assertEqual(len(self.state.props["file-local-options/audio-files"]), 0)
        self.assertEqual(self.state.props["stream-open-filename"], VIDEO_1080)

    def test_initial_browser_dash_edl_quotes_signed_delimiters_and_utf8_bytes(self):
        video = "https://media.example.test/画面.m4s?sig=a,b;!new_stream&token=%25"
        audio = "https://media.example.test/声音.m4s?sig=c,d;!no_clip"
        self.load(video, variants=[{"url": video, "audio_url": audio}])
        self.state.props["file-local-options/audio-files"] = self.lua.table_from([audio])
        self.state.fire("on_load")
        self.assertEqual(self.state.props["stream-open-filename"], dash_edl(video, audio))
        self.assertEqual(self.state.props.path, video)
        self.assertEqual(len(self.state.props["file-local-options/audio-files"]), 0)
        self.assertFalse(any(command[0] in ("loadfile", "audio-add") for command in self.commands()))

    def test_initial_bilibili_pair_without_optional_variants_uses_edl(self):
        video = "https://upos-sz.example.bilivideo.com/video.m4s?sig=a,b;c"
        audio = "https://upos-hz.example.bilivideo.cn/audio.m4s?sig=d,e;f"
        for option in ("file-local-options/audio-files", "options/audio-files"):
            with self.subTest(option=option):
                self.load(video)
                self.state.props[option] = self.lua.table_from([audio])
                headers = self.lua.table_from(["referer: https://www.bilibili.com/"])
                self.state.props["http-header-fields"] = headers
                self.state.fire("on_load")
                self.assertEqual(self.state.props["stream-open-filename"], dash_edl(video, audio))
                self.assertEqual(len(self.state.props["file-local-options/audio-files"]), 0)
                self.assertEqual(list(self.state.props["http-header-fields"].values()),
                                 ["referer: https://www.bilibili.com/"])
                self.assertEqual(self.state.props.path, video)

    def test_bilibili_pair_fallback_preserves_unrelated_or_multiple_external_audio(self):
        video = "https://upos.example.bilivideo.com/video.m4s"
        audio = "https://upos.example.bilivideo.com/audio.m4s"
        for path, audios, ytdl in ((VIDEO_720, [AUDIO], False),
                                   (video, [AUDIO], False),
                                   (video, [audio, audio + "?second"], False),
                                   (video, [audio], True),
                                   ("https://bilivideo.com.evil.test/video.m4s", [audio], False),
                                   ("https://user@bilivideo.com/video.m4s", [audio], False)):
            with self.subTest(path=path, audios=audios, ytdl=ytdl):
                self.load(path)
                self.state.props.ytdl = ytdl
                self.state.props["file-local-options/audio-files"] = self.lua.table_from(audios)
                self.state.fire("on_load")
                self.assertIsNone(self.state.props["stream-open-filename"])
                self.assertEqual(list(self.state.props["file-local-options/audio-files"].values()), audios)

    def test_vod_switch_resumes_position_but_live_switch_does_not_seek(self):
        for duration, live_active, expected_seek in [(120, False, True), (None, False, False), (120, True, False)]:
            with self.subTest(duration=duration, live_active=live_active):
                self.load(variants=self.variants())
                self.state.props.duration = duration
                self.state.props["user-data/uosc_danmaku/live-active"] = live_active
                self.state.props["time-pos"] = "40.5"
                self.open()
                self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
                self.state.props.path = VIDEO_1080
                self.state.fire("file-loaded")
                self.assertEqual(["seek", "40.5", "absolute+exact"] in self.commands(), expected_seek)

    def test_generic_reload_resumes_only_its_own_file(self):
        self.load()
        self.state.props.duration = 120
        self.state.props["time-pos"] = "40.5"
        self.state.bindings.reload()
        self.state.fire("file-loaded")
        self.assertIn(["seek", "40.5", "absolute+exact"], self.commands())
        self.state.props.path = "https://media.example.test/unrelated.mp4"
        self.state.fire("file-loaded")
        self.assertEqual(sum(command[0] == "seek" for command in self.commands()), 1)

    def test_failed_generic_reload_does_not_seek_the_next_unrelated_video(self):
        self.load()
        self.state.props.duration = 120
        self.state.props["time-pos"] = "40.5"
        self.state.bindings.reload()
        self.state.fire("end-file", self.lua.table_from({"reason": "error"}))
        self.state.props.path = "https://media.example.test/unrelated.mp4"
        self.state.fire("file-loaded")
        self.assertFalse(any(command[0] == "seek" for command in self.commands()))

    def test_generic_reload_timeout_does_not_seek_a_late_file_loaded(self):
        self.load()
        self.state.props.duration = 120
        self.state.props["time-pos"] = "40.5"
        self.state.bindings.reload()
        self.state.advance(16)
        self.state.fire("file-loaded")
        self.assertFalse(any(command[0] == "seek" for command in self.commands()))

    def test_abandoned_browser_quality_reload_does_not_leave_a_stale_seek(self):
        self.load(variants=self.variants())
        self.state.props.duration = 120
        self.state.props["time-pos"] = "40.5"
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        self.state.props.path = "https://media.example.test/unrelated.mp4"
        self.state.fire("file-loaded")
        self.state.props.path = VIDEO_1080
        self.state.fire("file-loaded")
        self.assertFalse(any(command[0] == "seek" for command in self.commands()))

    def test_repeated_quality_clicks_wait_for_inflight_switch_and_do_not_replace_marker(self):
        self.load(variants=self.variants())
        self.state.fire("file-loaded")
        self.state.props["user-data/uosc_danmaku/live-active"] = True
        self.state.props["user-data/uosc_danmaku/live-session"] = "bilibili-live-test"
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        first_marker = self.state.props["user-data/quality-menu/stream-switch"]
        generation = first_marker.generation
        self.state.props.path = VIDEO_1080
        self.state.fire("start-file")
        self.state.messages["video-format-set"](VIDEO_1080, VIDEO_720)
        self.assertEqual(self.state.props["user-data/quality-menu/stream-switch"].generation, generation)
        self.assertEqual(self.state.props["user-data/quality-menu/stream-switch"].to, VIDEO_1080)
        self.assertEqual(sum(command[0] == "loadfile" for command in self.commands()), 1)
        self.assertIn("请稍候", self.state.osd)
        self.state.bindings.reload()
        self.assertNotIn(["playlist-play-index current"], self.commands())
        self.state.fire("file-loaded")
        # The file is ready but the bridge has not yet observed generation 1.
        self.state.messages["video-format-set"](VIDEO_1080, VIDEO_720)
        self.assertEqual(sum(command[0] == "loadfile" for command in self.commands()), 1)
        self.assertEqual(self.state.props["user-data/quality-menu/state"].status, "ready")
        self.assertEqual(self.state.props["user-data/quality-menu/state"].activeHeight, 1080)
        for session, ack_generation in [("bilibili-wrong-session", generation), ("bilibili-live-test", generation - 1)]:
            self.state.props["user-data/uosc_danmaku/live-switch-ack"] = self.lua.table_from({
                "sessionId": session, "generation": ack_generation,
            })
            self.state.messages["video-format-set"](VIDEO_1080, VIDEO_720)
            self.assertEqual(sum(command[0] == "loadfile" for command in self.commands()), 1)
        self.state.props["user-data/uosc_danmaku/live-switch-ack"] = self.lua.table_from({
            "sessionId": "bilibili-live-test", "generation": generation,
        })
        self.state.messages["video-format-set"](VIDEO_1080, VIDEO_720)
        self.assertEqual(self.state.props["user-data/quality-menu/stream-switch"].generation, generation + 1)
        self.assertEqual(sum(command[0] == "loadfile" for command in self.commands()), 2)

    def test_vod_switch_accepts_next_quality_after_file_loaded_without_live_ack(self):
        self.load(variants=self.variants())
        self.state.fire("file-loaded")
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        self.state.props.path = VIDEO_1080
        self.state.fire("start-file")
        self.state.fire("file-loaded")
        self.state.messages["video-format-set"](VIDEO_1080, VIDEO_720)
        self.assertEqual(sum(command[0] == "loadfile" for command in self.commands()), 2)
        self.assertIsNone(self.state.props["user-data/uosc_danmaku/live-switch-ack"])

    def test_live_switch_marker_owned_bounded_and_only_marks_loaded_after_success(self):
        self.load(variants=self.variants())
        self.state.props["user-data/uosc_danmaku/live-active"] = True
        self.state.props["user-data/uosc_danmaku/live-session"] = "bilibili-live-test"
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        marker = self.state.props["user-data/quality-menu/stream-switch"]
        self.assertEqual(marker.sessionId, "bilibili-live-test")
        self.assertEqual(marker["from"], VIDEO_720)
        self.assertEqual(marker.to, VIDEO_1080)
        self.assertEqual(marker.stage, "loading")
        self.state.fire("file-loaded")
        self.assertEqual(marker.stage, "loading")
        self.state.props.path = VIDEO_1080
        self.state.fire("file-loaded")
        self.assertEqual(marker.stage, "loaded")
        self.state.advance(15)
        self.assertIsNone(self.state.props["user-data/quality-menu/stream-switch"])

    def test_unknown_or_untrusted_variant_cannot_load_another_url(self):
        self.load(variants=self.variants())
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, "https://untrusted.example.test/file.flv")
        self.assertFalse(any(command[0] == "loadfile" for command in self.commands()))
        self.load(variants=[{"url": VIDEO_720, "audio_url": "file:///private.key"}])
        self.open()
        self.assertEqual(self.menu_items()[-1].title, "720p（当前源）")

    def test_yt_dlp_failure_and_invalid_result_replace_spinner(self):
        for result in [{"status": 1, "stdout": ""}, {"status": 0, "stdout": "invalid"},
                       {"status": 0, "stdout": '{"formats": "bad"}'},
                       {"status": 0, "stdout": '{"formats": [1]}'}]:
            with self.subTest(result=result):
                self.load("https://www.example.test/watch?v=1")
                self.state.props.ytdl = True
                self.open()
                self.assertEqual(self.menu_items()[0].icon, "spinner")
                self.state.observers["user-data/mpv/ytdl/json-subprocess-result"]("result", self.lua.table_from(result))
                self.assertNotIn("spinner", [item.icon for item in self.menu_items()])
                self.assertTrue(any("清晰度" in (item.title or "") for item in self.menu_items()))

    def test_missing_result_times_out_and_late_success_recovers(self):
        self.load("https://www.example.test/watch?v=1", fetch_timeout=2)
        self.state.props.ytdl = True
        self.open()
        self.state.advance(2)
        self.assertEqual(self.state.props["user-data/quality-menu/state"].status, "unavailable")
        self.assertTrue(any("超时" in (item.title or "") for item in self.menu_items()))
        result = {"status": 0, "stdout": json.dumps({"formats": [{"format_id": "1080", "vcodec": "h264", "acodec": "aac", "height": 1080, "width": 1920}]})}
        self.state.observers["user-data/mpv/ytdl/json-subprocess-result"]("result", self.lua.table_from(result))
        self.open()  # Close the previous menu.
        self.open()
        self.assertTrue(any(item.value and item.value[5] == "1080" for item in self.menu_items()))

    def test_failed_quality_switch_reports_a_terminal_error(self):
        self.load(variants=self.variants())
        self.state.fire("file-loaded")
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        self.state.props.path = VIDEO_1080
        self.state.fire("start-file")
        self.state.fire("end-file", self.lua.table_from({"reason": "error"}))
        self.assertIn("切换清晰度失败", self.state.osd)
        self.assertEqual(self.state.props["user-data/quality-menu/state"].status, "unavailable")
        self.state.messages["video-format-set"](VIDEO_1080, VIDEO_1080)
        self.assertEqual(self.commands().count(["loadfile", VIDEO_1080, "replace"]), 2)

    def test_stalled_quality_switch_reports_timeout(self):
        self.load(variants=self.variants())
        self.open()
        self.state.messages["video-format-set"](VIDEO_720, VIDEO_1080)
        self.state.advance(15)
        self.assertIn("切换清晰度超时", self.state.osd)

    def test_legacy_hls_variant_option_still_loads_child_playlist(self):
        low, high = "https://media.example.test/low.m3u8", "https://media.example.test/high.m3u8"
        self.load(low, hls_variants=quote(json.dumps([{"url": low, "height": 720}, {"url": high, "height": 1080}]), safe=""))
        self.open()
        self.assertEqual(self.state.props["user-data/quality-menu/state"].source, "hls")
        self.assertEqual(self.menu_items()[1].title, "1080p")
        self.state.messages["video-format-set"](low, high)
        self.assertIn(["loadfile", high, "replace"], self.commands())

    def test_append_preserved_fixed_length_wrapper_keeps_browser_quality_list(self):
        payload = quote(json.dumps(self.variants(), ensure_ascii=False), safe="")
        self.load(stream_variants=f"%{len(payload.encode('utf-8'))}%{payload}")
        self.state.fire("file-loaded")
        self.open()
        self.assertEqual(self.state.props["user-data/quality-menu/state"].source, "browser")
        self.assertEqual([item.title for item in self.menu_items()[1:]], ["1080P", "720P"])

    def test_append_preserved_wrapper_also_supports_legacy_hls(self):
        low, high = "https://media.example.test/low.m3u8", "https://media.example.test/high.m3u8"
        payload = quote(json.dumps([{"url": low, "height": 720}, {"url": high, "height": 1080}]), safe="")
        self.load(low, hls_variants=f"%{len(payload)}%{payload}")
        self.open()
        self.assertEqual(self.state.props["user-data/quality-menu/state"].source, "hls")
        self.assertEqual(self.menu_items()[1].title, "1080p")
        self.state.messages["video-format-set"](low, high)
        self.assertIn(["loadfile", high, "replace"], self.commands())

    def test_bad_fixed_length_or_nested_prefix_is_rejected(self):
        payload = quote(json.dumps(self.variants()), safe="")
        wrapped = f"%{len(payload)}%{payload}"
        invalid_values = [f"%{len(payload) - 1}%{payload}", f"%{len(payload) + 1}%{payload}",
                          f"%{len(wrapped)}%{wrapped}", f"%999999999999999999999999999%{payload}"]
        for value in invalid_values:
            with self.subTest(value=value[:32]):
                self.load(stream_variants=value)
                self.open()
                self.assertEqual(self.state.props["user-data/quality-menu/state"].source, "direct")
                self.assertEqual(len(self.menu_items()), 2)
        low = "https://media.example.test/low.m3u8"
        payload = quote(json.dumps([{"url": low, "height": 720}]), safe="")
        self.load(low, hls_variants=f"%{len(payload) - 1}%{payload}")
        self.open()
        self.assertTrue(any("invalid fixed-length" in message for message in self.state.logs.values()))
        self.assertFalse(any(item.title == "720p" for item in self.menu_items()))

    def test_wrapper_overhead_does_not_change_encoded_payload_limit(self):
        payload = quote(json.dumps(self.variants()), safe="")
        padded = payload + " " * (24576 - len(payload))
        self.load(stream_variants=f"%{len(padded)}%{padded}")
        self.open()
        self.assertEqual(self.state.props["user-data/quality-menu/state"].source, "browser")
        over_limit = padded + " "
        self.load(stream_variants=f"%{len(over_limit)}%{over_limit}")
        self.open()
        self.assertEqual(self.state.props["user-data/quality-menu/state"].source, "direct")

    def test_hls_editions_remain_available_and_apply_before_reload(self):
        path = "https://media.example.test/master.m3u8?token=secret"
        self.load(path)
        self.state.props["edition-list"] = self.lua.table_from([
            {"id": 0, "title": "1280x720 4.0Mbps"},
            {"id": 1, "title": "1920x1080 8.0Mbps"},
        ], recursive=True)
        self.state.props["current-edition"] = 0
        self.state.fire("file-loaded")
        self.open()
        self.assertEqual(self.menu_items()[1].title, "1080p")
        self.state.messages["video-format-set"](path, "1")
        self.assertIn(["playlist-play-index current"], self.commands())
        self.state.fire("on_load")
        self.assertEqual(self.state.props["file-local-options/edition"], "1")

    def test_live_edition_reload_does_not_seek_even_with_positive_duration(self):
        path = "https://media.example.test/master.m3u8"
        self.load(path)
        self.state.props["edition-list"] = self.lua.table_from([
            {"id": 0, "title": "1280x720"}, {"id": 1, "title": "1920x1080"},
        ], recursive=True)
        self.state.props.duration = 120
        self.state.props["time-pos"] = "40.5"
        self.state.props["user-data/uosc_danmaku/live-active"] = True
        self.state.props["user-data/uosc_danmaku/live-session"] = "bilibili-live-test"
        self.state.fire("file-loaded")
        self.open()
        self.state.messages["video-format-set"](path, "1")
        self.assertIn(["playlist-play-index current"], self.commands())
        self.state.fire("file-loaded")
        self.assertFalse(any(command[0] == "seek" for command in self.commands()))

    def test_plain_text_menu_reports_timeout_and_has_no_fake_disabled_for_direct_stream(self):
        self.load("https://media.example.test/live.flv")
        self.state.messages["uosc-version"]("1.0.0")
        self.open()
        self.assertIn("当前源", self.state.osd)
        self.assertNotIn("Disabled", self.state.osd)
        self.load("https://www.example.test/watch?v=1", fetch_timeout=2)
        self.state.props.ytdl = True
        self.state.messages["uosc-version"]("1.0.0")
        self.open()
        self.state.advance(2)
        self.assertIn("超时", self.state.osd)


if __name__ == "__main__":
    unittest.main()
