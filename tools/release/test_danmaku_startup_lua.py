"""Real Lua main/file-loaded behavior for shared live and VOD preferences.

External API calls are deferred fixtures; MPV tracks and rendering use the same
mock as the renderer tests. No history flag is prefilled for the default test.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import test_video_danmaku_style_lua as video_fixture


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "portable_config/scripts/uosc_danmaku/main.lua"
LEGACY = ROOT / "portable_config/scripts/bilibiliAssert/main.lua"
SESSION = "bilibili-startup-fixture"


@unittest.skipIf(video_fixture.LuaRuntime is None, "lupa is required for Lua behavior tests")
class DanmakuStartupLuaTests(unittest.TestCase):
    def setUp(self):
        self.players = []
        first = self.create_fixture()
        self.directory = first.directory
        self.history = self.directory / "history.json"
        self.styles = self.directory / "shared-style.json"
        self.player = self.initialize_main(first)

    def create_fixture(self):
        player = video_fixture.VideoDanmakuStyleLuaTests()
        player.setUp()
        self.addCleanup(player.doCleanups)
        self.players.append(player)
        return player

    def initialize_main(self, player):
        player.globals.TEST.fire_event("on_unload")
        player.globals.options.history_path = str(self.history)
        player.globals.options.style_path = str(self.styles)
        player.globals.options.autoload_for_url = True
        player.globals.options.autoload_local_danmaku = False
        player.globals.options.auto_load = False
        player.globals.options.open_search_danmaku_menu_key = "Ctrl+d"
        player.globals.options.show_danmaku_keyboard_key = "j"
        player.globals.load_danmaku_style_defaults()
        player.lua.execute(r'''
            for _, module in ipairs({
                "modules/aes", "modules/base64", "modules/md5", "modules/hash",
                "modules/options", "modules/style", "modules/utils", "modules/parse",
                "modules/live", "modules/guess", "modules/render", "modules/menu",
                "modules/update", "apis/dandanplay", "apis/extra", "sites/bilibili",
                "sites/bahamut", "sites/iqiyi", "sites/mgtv", "sites/tencentvideo", "sites/youku",
            }) do package.loaded[module] = {} end
            TEST.bindings = {}
            TEST.periodic = {}
            TEST.options = {cid = "123456789"}
            TEST.net_count = 0
            TEST.generic_count = 0
            function mp.get_opt(key) return TEST.options[key] end
            function mp.add_key_binding(key, name, callback) TEST.bindings[name] = callback end
            function mp.add_periodic_timer(seconds, callback)
                local timer = {callback = callback, kill = function() end}
                TEST.periodic[#TEST.periodic + 1] = timer
                return timer
            end
            function check_for_update() end
            function load_danmaku_for_bilibili()
                TEST.net_count = TEST.net_count + 1
                TEST.pending_loader = function()
                    local xml = '<i><d p="100,1,999,16777215">ordinary fixture</d></i>'
                    DANMAKU.sources.fixture = {from = "user_custom", data = parse_xml_danmaku(xml)}
                    load_danmaku(false, true)
                end
            end
            function get_danmaku_with_hash() TEST.generic_count = TEST.generic_count + 1 end
            local original_commandv = mp.commandv
            function mp.commandv(...)
                local args = {...}
                original_commandv(...)
                if args[1] == "script-message-to" and args[2] == "uosc_danmaku"
                    and TEST.messages[args[3]] then
                    TEST.messages[args[3]](unpack(args, 4))
                end
            end
        ''')
        player.lua.execute(MAIN.read_text(encoding="utf-8"))
        player.globals.DANMAKU_PATH = str(player.directory)
        player.globals.TEST.props.path = "https://alternative-cdn.example.test/video.m4s"
        player.globals.TEST.props["filename/no-ext"] = "video"
        player.globals.TEST.props["current-tracks/video"] = player.to_lua({"image": False, "albumart": False})
        # Short DASH streams may not have a container-fps value at file-loaded.
        player.globals.TEST.props["container-fps"] = 0
        player.globals.TEST.props.duration = 45
        player.globals.TEST.commands = player.lua.table()
        return player

    def new_player(self):
        return self.initialize_main(self.create_fixture())

    def open_vod(self, player=None):
        player = player or self.player
        player.globals.TEST.fire_event("file-loaded")
        return player

    def complete(self, player=None):
        player = player or self.player
        self.assertIsNotNone(player.globals.TEST.pending_loader)
        player.globals.TEST.pending_loader()

    def message(self, name, *args, player=None):
        return (player or self.player).globals.TEST.messages[name](*args)

    def test_fresh_vod_auto_starts_without_manufacturing_history_false(self):
        self.assertFalse(self.history.exists())
        self.assertTrue(self.player.globals.get_danmaku_visibility())
        self.assertFalse(self.history.exists())
        self.open_vod()
        self.assertTrue(self.player.globals.ENABLED)
        self.assertEqual(self.player.globals.TEST.net_count, 1)
        self.assertEqual(self.player.globals.TEST.generic_count, 0)
        self.complete()
        self.assertEqual(self.player.style()[2], "50")
        self.assertTrue(self.player.globals.TEST.props["secondary-sub-visibility"])
        self.assertFalse(self.history.exists())

    def test_existing_false_is_preserved_in_fresh_vod_and_live(self):
        history = {"show_danmaku": False, "other-video": {"sources": {"sentinel": {"blocked": True}}}}
        self.history.write_text(json.dumps(history), encoding="utf-8")
        player = self.new_player()
        self.assertFalse(player.globals.get_danmaku_visibility())
        self.open_vod(player)
        self.assertFalse(player.globals.ENABLED)
        self.assertEqual(player.globals.TEST.net_count, 0)
        self.message("live-start", SESSION, player=player)
        self.assertTrue(player.globals.LIVE_DANMAKU.active)
        self.assertFalse(player.globals.ENABLED)
        self.assertEqual(json.loads(self.history.read_text(encoding="utf-8")), history)

    def test_live_user_choice_and_style_are_restored_by_fresh_vod(self):
        self.history.write_text('{"show_danmaku":false}', encoding="utf-8")
        live = self.new_player()
        self.message("live-start", SESSION, player=live)
        self.message("show_danmaku_keyboard", player=live)
        self.assertTrue(live.globals.ENABLED)
        live.change("fontsize", 30)
        live.change("opacity", 0.4)
        live.globals.set_danmaku_style_value("bold", False)
        self.message("live-stop", SESSION, player=live)
        self.assertTrue(json.loads(self.history.read_text(encoding="utf-8"))["show_danmaku"])
        vod = self.open_vod(self.new_player())
        self.complete(vod)
        self.assertTrue(vod.globals.ENABLED)
        self.assertEqual(vod.style()[2], "30")
        self.assertEqual(vod.style()[3], "&H99FFFFFF")
        self.assertEqual(vod.style()[7], "0")
        self.assertNotIn(r"\fs999", vod.ass_path.read_text(encoding="utf-8"))

    def test_live_menu_hide_preserves_session_and_shared_hidden_choice(self):
        self.message("live-start", SESSION)
        self.message("set", "show_danmaku", "off")
        self.assertTrue(self.player.globals.LIVE_DANMAKU.active)
        self.assertEqual(self.player.globals.LIVE_DANMAKU.session_id, SESSION)
        self.assertFalse(self.player.globals.ENABLED)
        self.assertFalse(json.loads(self.history.read_text(encoding="utf-8"))["show_danmaku"])
        self.message("set", "show_danmaku", "on")
        self.assertTrue(self.player.globals.LIVE_DANMAKU.active)
        self.assertTrue(self.player.globals.ENABLED)
        self.assertTrue(json.loads(self.history.read_text(encoding="utf-8"))["show_danmaku"])

    def test_render_cleanup_and_late_loader_cannot_overwrite_hidden_choice(self):
        self.open_vod()
        self.message("set", "show_danmaku", "off")
        self.complete()
        previous = self.history.read_bytes()
        self.assertFalse(self.player.globals.ENABLED)
        self.player.globals.render_danmaku(False, True)
        self.player.globals.TEST.fire_event("on_unload")
        self.assertEqual(self.history.read_bytes(), previous)
        next_player = self.open_vod(self.new_player())
        self.assertFalse(next_player.globals.ENABLED)
        self.assertEqual(next_player.globals.TEST.net_count, 0)

    def test_cross_instance_show_preference_updates_existing_player_without_writing_back(self):
        self.history.write_text('{"show_danmaku":false}', encoding="utf-8")
        vod = self.open_vod(self.new_player())
        self.assertFalse(vod.globals.ENABLED)
        live = self.new_player()
        self.message("live-start", SESSION, player=live)
        self.message("show_danmaku_keyboard", player=live)
        previous = self.history.read_bytes()
        self.assertTrue(vod.globals.sync_danmaku_visibility())
        self.complete(vod)
        self.assertTrue(vod.globals.ENABLED)
        self.assertEqual(self.history.read_bytes(), previous)
        self.message("show_danmaku_keyboard", player=live)
        previous = self.history.read_bytes()
        self.assertTrue(vod.globals.sync_danmaku_visibility())
        self.assertFalse(vod.globals.ENABLED)
        self.assertEqual(self.history.read_bytes(), previous)

    def test_other_instance_style_save_and_clear_refresh_existing_vod(self):
        vod = self.open_vod()
        self.complete()
        live = self.new_player()
        self.message("live-start", SESSION, player=live)
        live.change("fontsize", 34)
        live.globals.set_danmaku_style_value("bold", False)
        previous = self.styles.read_bytes()
        self.assertTrue(vod.globals.sync_danmaku_style_defaults(False))
        self.assertEqual(vod.style()[2], "34")
        self.assertEqual(vod.style()[7], "0")
        self.assertEqual(self.styles.read_bytes(), previous)
        self.assertFalse(vod.globals.sync_danmaku_style_defaults(False))
        self.message("clear-danmaku-style-default", player=live)
        self.assertFalse(self.styles.exists())
        self.assertTrue(vod.globals.sync_danmaku_style_defaults(False))
        self.assertEqual(vod.style()[2], "50")
        self.assertTrue(vod.globals.options.bold)
        self.assertFalse(self.styles.exists())

    def test_own_style_clear_keeps_current_video_until_next_file(self):
        self.open_vod()
        self.complete()
        self.player.change("fontsize", 34)
        self.message("clear-danmaku-style-default")
        self.assertFalse(self.player.globals.sync_danmaku_style_defaults(False))
        self.assertEqual(self.player.style()[2], "34")
        self.player.globals.TEST.fire_event("on_unload")
        self.open_vod()
        self.complete()
        self.assertEqual(self.player.style()[2], "50")
        self.assertFalse(self.styles.exists())

    def test_legacy_b_binding_uses_shared_toggle_and_renderer(self):
        self.player.lua.execute(LEGACY.read_text(encoding="utf-8-sig"))
        self.assertIsNotNone(self.player.globals.TEST.bindings.toggle)
        self.open_vod()
        self.complete()
        self.player.change("fontsize", 30)
        self.player.globals.TEST.bindings.toggle()
        self.assertFalse(self.player.globals.ENABLED)
        self.player.globals.TEST.bindings.toggle()
        self.assertTrue(self.player.globals.ENABLED)
        self.assertEqual(self.player.style()[2], "30")
        commands = self.player.to_python(self.player.globals.TEST.commands)
        self.assertTrue(any(command[:3] == ["script-message-to", "uosc_danmaku", "show_danmaku_keyboard"]
                            for command in commands))
        self.assertFalse(any("bilibili.ass" in str(command) or command[0] == "subprocess"
                             for command in commands))

    def test_invalid_history_is_not_destroyed_by_visibility_change(self):
        self.history.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.player.globals.set_danmaku_visibility(False))
        self.assertEqual(self.history.read_text(encoding="utf-8"), "{broken")

    def test_default_show_preference_does_not_enable_fps_before_data_or_after_unload(self):
        self.player.globals.options.vf_fps = True
        self.message("set-live-fps-mode", "120")
        self.assertIsNone(self.player.filter_fps())
        self.open_vod()
        self.assertTrue(self.player.globals.ENABLED)
        self.assertIsNone(self.player.filter_fps())
        self.complete()
        self.assertEqual(self.player.filter_fps(), "120")
        self.player.globals.TEST.fire_event("on_unload")
        self.assertIsNone(self.player.filter_fps())
        self.player.globals.TEST.fire_property("vf", self.player.lua.table())
        self.assertIsNone(self.player.filter_fps())
        self.assertTrue(self.player.globals.get_danmaku_visibility())


if __name__ == "__main__":
    unittest.main()
