"""Exercise retry ownership and native playlist preservation with Lua 5.1."""
from __future__ import annotations

from pathlib import Path
import unittest

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

SCRIPT = Path(__file__).resolve().parents[2] / "portable_config/scripts/ytdl-retry.lua"
MOCK = r'''
T = {props={}, events={}, observers={}, timers={}, commands={}, logs={}, osd={}}
mp = {}
function mp.get_property_native(k,d) if T.props[k] == nil then return d end return T.props[k] end
mp.get_property = mp.get_property_native
mp.get_property_bool = mp.get_property_native
mp.get_property_number = mp.get_property_native
function mp.register_event(k,f) T.events[k]=f end
function mp.observe_property(k,kind,f) T.observers[k]=f end
function mp.commandv(...) T.commands[#T.commands+1]={...} end
function mp.osd_message(text) T.osd[#T.osd+1]=text end
function mp.add_timeout(seconds,callback)
    local timer={enabled=true,callback=callback}
    function timer:kill() self.enabled=false end
    T.timers[#T.timers+1]=timer
    return timer
end
function T.tick()
    for _,timer in ipairs(T.timers) do
        if timer.enabled then timer.enabled=false; timer.callback() end
    end
end
package.loaded['mp']=mp
package.loaded['mp.msg']={info=function(text) T.logs[#T.logs+1]=text end,
                         warn=function(text) T.logs[#T.logs+1]=text end}
'''


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class YtdlRetryLuaTests(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute(MOCK)
        self.t = self.lua.globals().T
        self.lua.execute(SCRIPT.read_text(encoding="utf-8"))

    def table(self, value):
        return self.lua.table_from(value, recursive=True)

    def start(self, path="https://spankbang.com/example/video/test", id=10, playlist=None):
        self.t.props.path = path
        self.t.props["playlist-pos"] = 0
        self.t.props.playlist = self.table(playlist or [{"id": id, "filename": path, "current": True}])
        self.t.events["start-file"](self.table({"playlist_entry_id": id}))

    def end(self, reason="error", id=10):
        self.t.events["end-file"](self.table({"reason": reason, "playlist_entry_id": id}))

    def commands(self):
        return [list(command.values()) for command in self.t.commands.values()]

    def test_browser_playlist_failure_is_not_retried(self):
        self.start("http://127.0.0.1:9002/web-playlist/playlist-" + "a" * 32 + "/item/123:456")
        self.end()
        self.t.tick()
        self.assertEqual(self.commands(), [])
        self.assertEqual(len(self.t.osd), 0)

    def test_local_and_direct_media_failures_are_not_retried(self):
        for path in ("http://localhost:9002/media/123", "http://127.0.0.2/media/123",
                     "http://[::1]:9002/media/123", "https://cdn.example.test/video.m4s?signature=private",
                     "https://cdn.example.test/stream.m3u8?token=private", "C:/video.mkv"):
            with self.subTest(path=path):
                self.start(path)
                self.end()
                self.t.tick()
                self.assertEqual(self.commands(), [])

    def test_ytdl_disabled_is_not_retried(self):
        self.start()
        self.t.props.ytdl = False
        self.end()
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_ytdl_disabled_while_retry_pending_is_not_retried(self):
        self.start()
        self.end()
        self.t.props.ytdl = False
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_retry_replays_original_native_entry_after_reorder(self):
        url = "https://spankbang.com/example/video/test?private=secret"
        other = {"id": 11, "filename": "C:/other.mp4"}
        self.start(url, playlist=[{"id": 10, "filename": url, "current": True}, other])
        self.end()
        self.t.props["playlist-pos"] = -1
        self.t.props.playlist = self.table([other, {"id": 10, "filename": url}])
        self.t.tick()
        self.assertEqual(self.commands(), [["playlist-play-index", "1"]])
        self.assertEqual([entry.id for entry in self.t.props.playlist.values()], [11, 10])
        self.assertFalse(any("secret" in value or url in value for value in self.t.logs.values()))

    def test_next_file_cancels_retry_even_for_same_url(self):
        self.start()
        self.end()
        previous_timer = self.t.timers[1]
        self.start(id=11)
        self.assertFalse(previous_timer.enabled)
        previous_timer.callback()  # A callback already queued before cancellation is harmless.
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_user_stop_cancels_retry(self):
        self.start()
        self.end()
        self.end("stop")
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_shutdown_cancels_retry(self):
        self.start()
        self.end()
        self.t.events.shutdown()
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_cleared_or_replaced_playlist_cancels_retry(self):
        self.start()
        self.end()
        self.t.props.playlist = self.table([])
        self.t.observers.playlist()
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_new_current_entry_prevents_retry_without_start_event(self):
        url = "https://spankbang.com/example/video/test"
        self.start(url)
        self.end()
        self.t.props.playlist = self.table([{"id": 10, "filename": url},
                                          {"id": 11, "filename": "C:/next.mp4", "current": True}])
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_retry_stops_after_eight_attempts_for_same_entry(self):
        for _ in range(10):
            self.start()
            self.end()
            self.t.tick()
        self.assertEqual(len(self.t.commands), 8)
        self.assertEqual(len(self.t.osd), 8)

    def test_foreign_end_event_does_not_retry_current_file(self):
        self.start()
        self.end(id=11)
        self.t.tick()
        self.assertEqual(self.commands(), [])

    def test_foreign_end_event_does_not_cancel_current_retry(self):
        self.start()
        self.end()
        self.end("stop", id=11)
        self.t.tick()
        self.assertEqual(self.commands(), [["playlist-play-index", "0"]])


if __name__ == "__main__":
    unittest.main()
