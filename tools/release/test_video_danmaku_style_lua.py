"""Ordinary-video styles use the real parser, renderer and menu in Lua 5.1.

The MPV API is simulated, including subtitle tracks and video-filter operations.
These tests cover immediate changes on a paused video, not visual MPV output.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_live_danmaku_lua import LuaRuntime, MOCK_MPV, MODULE_DIR, SESSION


@unittest.skipIf(LuaRuntime is None, "lupa is required for Lua behavior tests")
class VideoDanmakuStyleLuaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.ass_path = self.directory / "uosc-danmaku-1.ass"
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.globals = self.lua.globals()
        self.globals._parse_json = self.parse_json
        self.globals._format_json = lambda value: json.dumps(self.to_python(value), ensure_ascii=False)
        self.lua.execute(MOCK_MPV)
        self.lua.execute(r'''
            package.loaded["mp"] = mp
            package.loaded["mp.utils"].format_json = function(value) return _format_json(value) end
            function mp.get_script_name() return "uosc_danmaku" end
            TEST.props["pause"] = true
            TEST.props["sid"] = 4
            TEST.props["secondary-sid"] = 9
            TEST.props["secondary-sub-ass-override"] = "no"
            TEST.props["secondary-sub-visibility"] = false
            TEST.props["track-list"] = {{id = 4, type = "sub", title = "User subtitles"}}
            TEST.next_sub_id = 100
            local actual_commandv = mp.commandv
            function mp.commandv(...)
                local args = {...}
                actual_commandv(...)
                if args[1] == "sub-add" then
                    TEST.next_sub_id = TEST.next_sub_id + 1
                    table.insert(TEST.props["track-list"], {
                        id = TEST.next_sub_id, type = "sub", title = args[4],
                        ["external-filename"] = args[2],
                    })
                    TEST.props["sid"] = TEST.next_sub_id
                elseif args[1] == "sub-remove" then
                    local tracks = TEST.props["track-list"]
                    for i = #tracks, 1, -1 do
                        if tracks[i].id == args[2] then table.remove(tracks, i) end
                    end
                elseif args[1] == "vf" and args[2] == "remove" then
                    local filters = TEST.props["vf"]
                    for i = #filters, 1, -1 do
                        if "@" .. tostring(filters[i].label) == args[3] then
                            table.remove(filters, i)
                        end
                    end
                elseif args[1] == "vf" and args[2] == "append" then
                    local fps = args[3]:match("^@danmaku_smooth:fps=fps=(.+)$")
                    if fps then
                        table.insert(TEST.props["vf"], {
                            label = "danmaku_smooth", name = "fps", params = {fps = fps},
                        })
                    end
                end
            end
            options.live_fps_mode = "auto"
            options.max_screen_danmaku = 0
            options.merge_tolerance = -1
            options.merge_without_style = false
            options.render_mode = "ass"
            DANMAKU_SWITCH_ON = "user-data/uosc_danmaku/danmaku-switch-on"
        ''')
        self.globals.options.style_path = str(self.directory / "style.json")
        self.globals.DANMAKU_PATH = str(self.directory)
        for name in ("utils.lua", "parse.lua", "live.lua", "render.lua", "style.lua", "menu.lua"):
            self.lua.execute((MODULE_DIR / name).read_text(encoding="utf-8"))
        self.globals.TEST.messages["uosc-version"]()
        self.set_fixture()
        self.globals.ENABLED = True
        self.globals.convert_danmaku_to_ass_events(True)
        self.globals.render()
        self.globals.TEST.commands = self.lua.table()
        self.visibility_writes = self.globals.TEST.visibility_writes

    def to_lua(self, value):
        return self.lua.table_from(value, recursive=True) if isinstance(value, (dict, list)) else value

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

    def set_fixture(self, count=8, types=None):
        types = types or [1] * count
        xml = "<i>" + "".join(
            f'<d p="100,{kind},999,16777215">row {index} x3</d>'
            for index, kind in enumerate(types)
        ) + "</i>"
        data = self.globals.parse_xml_danmaku(xml)
        self.globals.DANMAKU = self.to_lua({"sources": {"fixture.xml": {"data": data, "from": "user_local"}}})

    def change(self, key, text):
        self.globals.TEST.messages["setup-danmaku-style"](key, str(text))

    def style(self):
        content = self.ass_path.read_text(encoding="utf-8")
        return next(line.split(",") for line in content.splitlines() if line.startswith("Style: R2L,"))

    def comments(self):
        return self.to_python(self.globals.COMMENTS)

    def filter_fps(self):
        for filter_ in self.to_python(self.globals.TEST.props["vf"]):
            if filter_["label"] == "danmaku_smooth":
                return filter_["params"]["fps"]
        return None

    def test_paused_video_immediately_applies_every_visual_style(self):
        self.assertTrue(self.globals.TEST.props.pause)
        self.change("fontsize", 30)
        self.change("outline", 2.5)
        self.change("shadow", 3)
        self.change("opacity", 0.4)
        self.globals.TEST.messages["setup-danmaku-style"]('{"type":"activate","index":1}')
        style = self.style()
        self.assertEqual(style[2], "30")
        self.assertEqual(style[3], "&H99FFFFFF")
        self.assertEqual(style[7], "0")
        self.assertEqual(style[16:18], ["2.5", "3"])
        self.assertNotIn(r"\b1", self.ass_path.read_text(encoding="utf-8"))
        self.assertEqual(self.globals.TEST.props["sid"], 4)
        self.assertEqual(self.globals.TEST.visibility_writes, self.visibility_writes)
        self.assertEqual(self.globals.TEST.props["time-pos"], 100)

    def test_speed_and_area_rebuild_cached_static_events(self):
        initial = self.comments()
        self.assertEqual(len(initial), 8)
        self.assertEqual(initial[0]["end_time"] - initial[0]["start_time"], 15)
        self.change("scrolltime", 8)
        changed = self.comments()
        self.assertEqual(changed[0]["end_time"] - changed[0]["start_time"], 8)
        self.change("displayarea", 0.15)
        constrained = self.comments()
        self.assertEqual(len(constrained), 3)
        self.assertLessEqual(max(event["move"][1] for event in constrained), 162)
        self.assertEqual(self.globals.DANMAKU.sources["fixture.xml"].data[1].time, 100)
        self.assertEqual(self.globals.DANMAKU.sources["fixture.xml"].data[1].size, 999)
        self.assertEqual(self.globals.TEST.visibility_writes, self.visibility_writes)

    def test_zero_area_and_empty_supported_events_remove_old_static_comments(self):
        self.change("displayarea", 0)
        self.assertEqual(len(self.globals.COMMENTS), 0)
        self.assertNotIn("Dialogue:", self.ass_path.read_text(encoding="utf-8"))
        self.change("displayarea", 1)
        self.assertGreater(len(self.globals.COMMENTS), 0)
        self.set_fixture(types=[7])
        self.globals.refresh_danmaku_style()
        self.assertEqual(len(self.globals.COMMENTS), 0)
        self.assertNotIn("Dialogue:", self.ass_path.read_text(encoding="utf-8"))

    def test_hidden_video_stays_hidden_after_style_changes(self):
        self.globals.TEST.messages["set"]("show_danmaku", "off")
        self.globals.TEST.commands = self.lua.table()
        visibility_writes = self.globals.TEST.visibility_writes
        old_ass = self.ass_path.read_bytes()
        self.change("fontsize", 60)
        self.change("displayarea", 0.2)
        self.assertFalse(self.globals.ENABLED)
        self.assertFalse(self.globals.TEST.saved_visibility)
        self.assertEqual(self.globals.TEST.visibility_writes, visibility_writes)
        self.assertEqual(self.globals.TEST.props["secondary-sid"], 9)
        self.assertEqual(self.ass_path.read_bytes(), old_ass)
        commands = self.to_python(self.globals.TEST.commands)
        self.assertFalse(any(command[0] == "sub-add" for command in commands))
        self.globals.ENABLED = True
        self.globals.show_danmaku_func()
        self.assertEqual(self.style()[2], "60")

    def test_source_sizes_do_not_override_player_size_and_counter_does_not_force_bold(self):
        self.assertEqual(self.style()[2], "50")
        self.assertNotIn(r"\fs999", self.ass_path.read_text(encoding="utf-8"))
        self.globals.set_danmaku_style_value("bold", False)
        self.assertEqual(self.style()[7], "0")
        self.assertNotIn(r"\b1", self.ass_path.read_text(encoding="utf-8"))

    def test_merged_counter_keeps_player_bold_choice(self):
        xml = ('<i><d p="100,1,25,16777215">repeat</d>'
               '<d p="100.5,1,25,16777215">repeat</d></i>')
        data = self.globals.parse_xml_danmaku(xml)
        self.globals.DANMAKU = self.to_lua({"sources": {"fixture.xml": {"data": data}}})
        self.globals.options.merge_tolerance = 1
        self.globals.set_danmaku_style_value("bold", False)
        self.assertEqual(len(self.globals.COMMENTS), 1)
        self.assertEqual(self.globals.COMMENTS[1].clean_text, "repeatx2")
        self.assertEqual(self.style()[7], "0")
        self.assertNotIn(r"\b1", self.ass_path.read_text(encoding="utf-8"))

    def test_overlay_paused_video_repaints_with_same_settings(self):
        self.globals.options.render_mode = "overlay"
        self.change("fontsize", 30)
        self.change("opacity", 0.5)
        self.change("outline", 2)
        self.change("shadow", 1)
        self.globals.set_danmaku_style_value("bold", False)
        overlay = self.globals.TEST.overlays[1]
        self.assertTrue(overlay.visible)
        self.assertIn(r"\fs30", overlay.data)
        self.assertIn(r"\bord2\shad1\b0", overlay.data)
        self.change("displayarea", 0.04)
        self.assertEqual(len(self.globals.COMMENTS), 1)

    def test_static_fps_choices_apply_in_ass_and_overlay(self):
        self.globals.options.vf_fps = True
        for renderer in ("ass", "overlay"):
            self.globals.options.render_mode = renderer
            for mode, expected in (("80", "80"), ("120", "120"), ("60", "60")):
                self.globals.TEST.messages["set-live-fps-mode"](mode)
                self.assertEqual(self.filter_fps(), expected)
            self.globals.TEST.props["display-fps"] = 144
            self.globals.TEST.messages["set-live-fps-mode"]("auto")
            self.assertEqual(self.filter_fps(), "120")
            self.globals.TEST.messages["set-live-fps-mode"]("display")
            self.assertEqual(self.filter_fps(), "144")

    def test_fps_never_stacks_with_rife_or_other_user_filters(self):
        self.globals.options.vf_fps = True
        self.globals.TEST.messages["set-live-fps-mode"]("80")
        self.assertEqual(self.filter_fps(), "80")
        filters = self.to_lua([
            {"name": "vapoursynth", "label": "rife_runtime", "params": {"file": "fixture.vpy"}},
            {"name": "fps", "label": "danmaku_smooth", "params": {"fps": "80"}},
        ])
        self.globals.TEST.fire_property("vf", filters)
        self.assertIsNone(self.filter_fps())
        self.globals.TEST.messages["set-live-fps-mode"]("120")
        remaining = self.to_python(self.globals.TEST.props["vf"])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["label"], "rife_runtime")
        self.assertEqual(self.globals.TEST.props["user-data/uosc_danmaku/live-fps-target"], "")
        self.globals.TEST.fire_property("vf", self.lua.table())
        self.assertEqual(self.filter_fps(), "120")
        self.globals.TEST.props["user-data/uosc_danmaku/suspend-smooth-fps"] = True
        self.globals.TEST.messages["set-live-fps-mode"]("60")
        self.assertIsNone(self.filter_fps())

    def test_fps_choice_does_not_rebuild_static_comment_library(self):
        self.globals.options.vf_fps = True
        original_track = self.globals.TEST.props["secondary-sid"]
        self.lua.execute('convert_danmaku_to_ass_events = function() error("unneeded rebuild") end')
        self.globals.TEST.messages["set-live-fps-mode"]("80")
        self.assertEqual(self.filter_fps(), "80")
        self.assertEqual(self.globals.TEST.props["secondary-sid"], original_track)

    def test_live_styles_still_use_live_events_without_modifying_static_cache(self):
        static_comments = self.globals.COMMENTS
        static_sources = self.globals.DANMAKU.sources
        self.globals.TEST.fire_property("pause", False)
        self.globals.TEST.messages["live-start"](SESSION)
        self.globals.TEST.messages["live-batch"](SESSION, json.dumps([{"text": "live fixture"}]))
        self.globals.TEST.advance(0.3)
        self.change("fontsize", 25)
        self.change("scrolltime", 9)
        self.globals.TEST.advance(0.3)
        self.assertEqual(self.style()[2], "25")
        event = self.globals.LIVE_DANMAKU.events[1]
        self.assertEqual(event.end_time - event.start_time, 9)
        self.assertTrue(self.lua.eval("rawequal")(static_comments, self.globals.COMMENTS))
        self.assertTrue(self.lua.eval("rawequal")(static_sources, self.globals.DANMAKU.sources))

    def test_zero_area_hides_live_ass_and_restoring_area_uses_retained_events(self):
        static_comments = self.globals.COMMENTS
        self.globals.TEST.fire_property("pause", False)
        self.globals.TEST.messages["live-start"](SESSION)
        self.globals.TEST.messages["live-batch"](SESSION, json.dumps([{"text": "retained live fixture"}]))
        self.globals.TEST.advance(0.3)
        self.assertEqual(len(self.globals.LIVE_DANMAKU.events), 1)
        self.change("displayarea", 0)
        self.globals.TEST.advance(0.3)
        self.assertTrue(self.globals.LIVE_DANMAKU.active)
        self.assertTrue(self.globals.ENABLED)
        self.assertEqual(len(self.globals.LIVE_DANMAKU["items"]), 1)
        self.assertEqual(len(self.globals.LIVE_DANMAKU.events), 0)
        self.assertNotIn("Dialogue:", self.ass_path.read_text(encoding="utf-8"))
        self.change("displayarea", 0.5)
        self.globals.TEST.advance(0.3)
        self.assertEqual(len(self.globals.LIVE_DANMAKU.events), 1)
        self.assertIn("retained live fixture", self.ass_path.read_text(encoding="utf-8"))
        self.assertTrue(self.lua.eval("rawequal")(static_comments, self.globals.COMMENTS))


if __name__ == "__main__":
    unittest.main()
