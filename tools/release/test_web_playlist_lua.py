"""Playlist resolution and per-item isolation against the production Lua scripts.

The player API is simulated in Lua 5.1; no CDN URLs or browser credentials are
used. The hooks model MPV's explicit defer/cont contract, including late replies.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
import unittest

try:
    from lupa.lua51 import LuaRuntime
except ImportError:
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[2]
PLAYLIST = ROOT / "portable_config/scripts/web_playlist.lua"
PROGRESS = ROOT / "portable_config/scripts/web_progress.lua"
BILIBILI = ROOT / "portable_config/scripts/uosc_danmaku/sites/bilibili.lua"
PLAYLIST_ID = "playlist-" + "a" * 32
SESSION_ID = "progress-playlist-test"
CURRENT = "user-data/mpv-lazy/web-playlist/current"
REQUEST = "user-data/mpv-lazy/web-playlist/request"
STATE = "user-data/mpv-lazy/web-playlist/state"
FINAL = "user-data/mpv-lazy/playback-progress-finals"
SNAPSHOT = "user-data/mpv-lazy/playback-progress"
QUALITY_SWITCH = "user-data/mpv-lazy/web-playlist/quality-switch"
MANIFEST = "user-data/mpv-lazy/web-playlist-manifest"
MANIFEST_ACK = "user-data/mpv-lazy/web-playlist/manifest-ack"
EXTEND_ACK = "user-data/mpv-lazy/web-playlist/extend-ack"
METADATA_STATE = "user-data/mpv-lazy/web-playlist/metadata-state"


def edl_sources(source):
    """Read length-quoted EDL URLs as bytes, including literal separators."""
    data = source.encode("utf-8").removeprefix(b"memory://# mpv EDL v0\n")
    sources = []
    while data:
        data = data.removeprefix(b"!no_clip;!no_chapters;")
        prefix = re.match(rb"%(\d+)%", data)
        if not prefix:
            raise AssertionError("missing EDL URL byte length")
        size = int(prefix[1])
        data = data[prefix.end():]
        sources.append(data[:size].decode("utf-8"))
        data = data[size:]
        if data:
            if not data.startswith(b";!new_stream;"):
                raise AssertionError("invalid EDL stream boundary")
            data = data[len(b";!new_stream;"):]
    return sources

MOCK = r'''
T = {props={}, opts={}, option_values={}, events={}, observers={}, hooks={},
     messages={}, timers={}, commands={}, clock=0, downloads={}, saved={}, loaded=0, async={}}
mp = {}
function mp.get_property_native(k,d) if T.props[k] == nil then return d end return T.props[k] end
mp.get_property = mp.get_property_native
mp.get_property_number = mp.get_property_native
mp.get_property_bool = mp.get_property_native
function mp.set_property_native(k,v) T.props[k]=v end
mp.set_property = mp.set_property_native
mp.set_property_bool = mp.set_property_native
mp.set_property_number = mp.set_property_native
function mp.get_opt(k) return T.option_values[k] end
function mp.get_time() return T.clock end
function mp.register_script_message(k,f) T.messages[k]=f end
function mp.register_event(k,f)
    T.events[k]=T.events[k] or {}; table.insert(T.events[k],f)
end
function mp.observe_property(k,kind,f) T.observers[k]=f end
function mp.add_hook(k,priority,f)
    T.hooks[k]=T.hooks[k] or {}; table.insert(T.hooks[k],{priority=priority,callback=f})
end
function mp.commandv(...)
    local args={...}; table.insert(T.commands,args)
    if not T.simulate_playlist then return end
    local playlist=T.props.playlist
    if args[1]=='playlist-remove' then table.remove(playlist,tonumber(args[2])+1)
    elseif args[1]=='playlist-move' then
        local from,to=tonumber(args[2]),tonumber(args[3])
        local entry=table.remove(playlist,from+1)
        if from<to then to=to-1 end
        table.insert(playlist,to+1,entry)
    end
end
function mp.command_native(args) return args[2] end
function mp.command_native_async(args,callback)
    T.async[#T.async+1]={args=args,callback=callback}; return #T.async
end
function mp.abort_async_command(id) T.async[id].aborted=true end
function mp.add_timeout(seconds,f)
    local timer={due=T.clock+seconds,enabled=true,callback=f,seconds=seconds}
    function timer:kill() self.enabled=false end
    T.timers[#T.timers+1]=timer; return timer
end
function mp.add_periodic_timer(seconds,f)
    local timer=mp.add_timeout(seconds,f); timer.period=seconds; return timer
end
function T.advance(seconds)
    local target=T.clock+seconds
    while true do
        local next_timer
        for _,timer in ipairs(T.timers) do
            if timer.enabled and timer.due <= target and
                (not next_timer or timer.due < next_timer.due) then next_timer=timer end
        end
        if not next_timer then break end
        T.clock=next_timer.due
        next_timer.enabled=next_timer.period ~= nil
        if next_timer.period then next_timer.due=T.clock+next_timer.period end
        next_timer.callback()
    end
    T.clock=target
end
function T.event(k,reason)
    for _,f in ipairs(T.events[k] or {}) do f({reason=reason}) end
end
function T.observe(k)
    if T.observers[k] then T.observers[k](k,T.props[k]) end
end
function T.hook(k)
    local hook={deferred=0,continued=0}
    function hook:defer() self.deferred=self.deferred+1 end
    function hook:cont() self.continued=self.continued+1 end
    local hooks=T.hooks[k] or {}
    table.sort(hooks,function(a,b) return a.priority < b.priority end)
    for _,item in ipairs(hooks) do item.callback(hook) end
    return hook
end
package.loaded['mp']=mp
package.loaded['mp.options']={read_options=function(o,namespace)
    for k,v in pairs(T.opts[namespace] or {}) do o[k]=v end
end}
package.loaded['mp.utils']={parse_json=function(v) return _parse_json(v) end}
package.loaded['mp.msg']={warn=function() end,error=function() end}
options={}
function call_cmd_async(args,callback) T.downloads[#T.downloads+1]={args=args,callback=callback} end
function save_danmaku_xml(path,content) T.saved[#T.saved+1]={path=path,content=content} end
function load_danmaku() T.loaded=T.loaded+1 end
function show_message() end
'''


class LuaPlayerHarness:
    def setUp(self):
        self.lua = LuaRuntime(unpack_returned_tuples=True)
        self.g = self.lua.globals()
        self.g._parse_json = self.parse_json
        self.lua.execute(MOCK)
        self.t = self.g.T

    def to_lua(self, value):
        return self.lua.table_from(value, recursive=True) if isinstance(value, (dict, list)) else value

    def parse_json(self, value):
        try:
            return self.to_lua(json.loads(value))
        except (ValueError, TypeError):
            return None

    def load(self, timeout=20, playlist_id=PLAYLIST_ID, session=SESSION_ID):
        self.t.opts.web_playlist = self.to_lua({"id": playlist_id, "session_id": session,
                                             "resolve_timeout": timeout})
        self.lua.execute(PLAYLIST.read_text(encoding="utf-8"))

    def path(self, item="123:456"):
        return f"http://127.0.0.1:9002/web-playlist/{PLAYLIST_ID}/item/{item}"

    def begin(self, item="123:456", path=None):
        self.t.props.path = path or self.path(item)
        return self.t.hook("on_load")

    def media(self, item="123:456", **changes):
        value = {"identity": f"bilibili:{item}", "cid": item.split(":")[1],
                 "video": "https://media.example.test/video.m4s", "audio": "https://media.example.test/audio.m4s",
                 "subtitle": "https://media.example.test/subtitles.ass", "title": f"Video {item}",
                 "position": 12.5, "duration": 100,
                 "variants": [{"url": "https://media.example.test/video.m4s", "audio_url": "https://media.example.test/audio.m4s", "height": 720},
                              {"url": "https://media.example.test/high.m4s", "height": 1080}]}
        value.update(changes)
        return value

    def resolve(self, media=None, **changes):
        pending = self.t.props[REQUEST]
        value = {"playlistId": PLAYLIST_ID, "itemId": pending.itemId, "generation": pending.generation,
                 "media": media or self.media(pending.itemId)}
        value.update(changes)
        self.t.messages["web-playlist-resolved"](json.dumps(value))
        return value

    def commands(self):
        return [list(value.values()) for value in self.t.commands.values()]

    def assert_no_request(self):
        value = self.t.props[REQUEST]
        self.assertTrue(value is None or len(value) == 0)


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class WebPlaylistLuaTests(LuaPlayerHarness, unittest.TestCase):
    def test_unconfigured_or_foreign_playlist_is_not_intercepted(self):
        self.load(playlist_id="playlist-invalid")
        hook = self.begin()
        self.assertEqual(hook.deferred, 0)
        self.assert_no_request()

    def test_only_own_loopback_item_entries_are_deferred(self):
        self.load()
        for path in ("https://www.bilibili.com/video/BV123", self.path().replace("127.0.0.1", "localhost"),
                     self.path().replace(PLAYLIST_ID, "playlist-" + "b" * 32),
                     self.path().replace(":9002/", ":65536/"), self.path() + "?extra=1"):
            with self.subTest(path=path):
                self.assertEqual(self.begin(path=path).deferred, 0)
                self.assert_no_request()

    def test_load_waits_for_matching_resolution_and_continues_once(self):
        self.load()
        hook = self.begin()
        self.assertEqual((hook.deferred, hook.continued), (1, 0))
        self.assertEqual(self.t.props[REQUEST].itemId, "123:456")
        self.assertIsNone(self.t.props["stream-open-filename"])
        response = self.resolve()
        self.assertEqual((hook.deferred, hook.continued), (1, 1))
        self.assert_no_request()
        self.assertTrue(self.t.props["stream-open-filename"].startswith("memory://# mpv EDL v0\n"))
        self.assertEqual(edl_sources(self.t.props["stream-open-filename"]),
                         [response["media"]["video"], response["media"]["audio"]])
        self.assertEqual(self.t.props.path, self.path())
        self.t.messages["web-playlist-resolved"](json.dumps(response))
        self.t.advance(30)
        self.assertEqual(hook.continued, 1)

    def test_foreign_identity_generation_and_playlist_replies_cannot_resume_hook(self):
        self.load()
        hook = self.begin()
        for override in ({"playlistId": "playlist-" + "b" * 32}, {"itemId": "123:789"}, {"generation": 99}):
            self.resolve(**override)
            self.assertEqual(hook.continued, 0)
            self.assertIsNone(self.t.props["stream-open-filename"])
        self.resolve()
        self.assertEqual(hook.continued, 1)

    def test_timeout_releases_hook_and_late_reply_cannot_load_failed_entry(self):
        self.load()
        hook = self.begin()
        response = {"playlistId": PLAYLIST_ID, "itemId": "123:456", "generation": 1, "media": self.media()}
        self.t.advance(20)
        self.assertEqual(hook.continued, 1)
        self.assertEqual(self.t.props[STATE].error, "web_playlist_timeout")
        self.assertEqual(self.t.props["stream-open-filename"], "memory://")
        self.t.messages["web-playlist-resolved"](json.dumps(response))
        self.assertEqual(self.t.props["stream-open-filename"], "memory://")
        self.assertIsNone(self.t.props[CURRENT])
        self.assertEqual(hook.continued, 1)

    def test_timeout_is_bounded(self):
        for timeout, expected in ((-20, 5), (120, 60)):
            with self.subTest(timeout=timeout):
                self.setUp()
                self.load(timeout=timeout)
                hook = self.begin()
                self.t.advance(expected - 0.1)
                self.assertEqual(hook.continued, 0)
                self.t.advance(0.1)
                self.assertEqual(hook.continued, 1)

    def test_new_entry_releases_old_hook_and_ignores_old_reply(self):
        self.load()
        first = self.begin()
        old = {"playlistId": PLAYLIST_ID, "itemId": "123:456", "generation": 1, "media": self.media()}
        second = self.begin("123:789")
        self.assertEqual(first.continued, 1)
        self.assertEqual((second.deferred, second.continued), (1, 0))
        self.t.messages["web-playlist-resolved"](json.dumps(old))
        self.assertEqual(second.continued, 0)
        self.resolve()
        self.assertEqual(second.continued, 1)
        self.assertEqual(self.t.props[CURRENT].identity, "bilibili:123:789")

    def test_old_timeout_already_queued_cannot_release_new_entry(self):
        self.load()
        first = self.begin()
        timeout = self.t.timers[1]
        second = self.begin("123:789")
        # MPV may already have queued a timer before kill() is observed.
        timeout.callback()
        self.assertEqual(first.continued, 1)
        self.assertEqual(second.continued, 0)
        self.assertEqual(self.t.props[REQUEST].itemId, "123:789")
        self.resolve()
        self.assertEqual(second.continued, 1)

    def test_switch_to_unrelated_file_releases_old_hook_and_rejects_late_reply(self):
        self.load()
        first = self.begin()
        old = {"playlistId": PLAYLIST_ID, "itemId": "123:456", "generation": 1, "media": self.media()}
        second = self.begin(path="https://media.example.test/ordinary.mp4")
        self.assertEqual(first.continued, 1)
        self.assertEqual(second.deferred, 0)
        self.t.messages["web-playlist-resolved"](json.dumps(old))
        self.assertIsNone(self.t.props[CURRENT])
        self.assertNotEqual(self.t.props["stream-open-filename"], old["media"]["video"])
        self.t.advance(30)
        self.assertEqual(first.continued, 1)

    def test_explicit_browser_resolution_error_releases_without_using_placeholder(self):
        self.load()
        hook = self.begin()
        self.resolve(error="web_playlist_browser_closed")
        self.assertEqual(hook.continued, 1)
        self.assertEqual(self.t.props["stream-open-filename"], "memory://")
        self.assertEqual(self.t.props[STATE].stage, "failed")
        self.assertIsNone(self.t.props[CURRENT])

    def test_two_items_replace_file_local_audio_subtitle_cid_title_and_start(self):
        self.load()
        self.t.props["options/script-opts"] = self.to_lua({"cid": "stale", "other-script-setting": "keep"})
        self.begin()
        self.resolve()
        self.assertEqual(edl_sources(self.t.props["stream-open-filename"]),
                         [self.media()["video"], self.media()["audio"]])
        self.assertEqual(len(self.t.props["file-local-options/audio-files"]), 0)
        self.assertEqual(list(self.t.props["file-local-options/sub-files"].values()), [self.media()["subtitle"]])
        self.begin("123:789")
        media = self.media("123:789", audio=None, subtitle=None, title="Movie Star", position=0)
        self.resolve(media)
        self.assertEqual(len(self.t.props["file-local-options/audio-files"]), 0)
        self.assertEqual(len(self.t.props["file-local-options/sub-files"]), 0)
        self.assertEqual(self.t.props["file-local-options/script-opts"].cid, "789")
        self.assertEqual(self.t.props["options/script-opts"].cid, "stale")
        self.assertEqual(self.t.props["file-local-options/script-opts"]["other-script-setting"], "keep")
        self.assertEqual(self.t.props["file-local-options/force-media-title"], "Movie Star")
        # MPV's time option accepts a textual timestamp through set_property;
        # set_property_number is rejected by the real player for this option.
        self.assertEqual(float(self.t.props["file-local-options/start"]), 0)
        self.assertFalse(self.t.props["file-local-options/ytdl"])
        self.assertEqual(list(self.t.props["file-local-options/http-header-fields"].values()), ["Referer: https://www.bilibili.com/"])

    def test_dash_edl_preserves_signed_url_delimiters_and_utf8_bytes(self):
        self.load()
        self.begin()
        media = self.media(video="https://media.example.test/视频.m4s?token=one,two;!new_stream;%25",
                           audio="https://media.example.test/声音.m4s?token=three,four;!no_clip")
        self.resolve(media)
        self.assertEqual(edl_sources(self.t.props["stream-open-filename"]), [media["video"], media["audio"]])
        self.assertEqual(self.t.props.path, self.path())
        self.assertEqual(self.t.props[CURRENT].audio, media["audio"])

    def test_quality_reload_replaces_both_edl_streams_without_external_audio(self):
        self.load()
        self.begin()
        media = self.media()
        media["variants"][1]["audio_url"] = "https://media.example.test/high-audio.m4s?sig=a,b;c"
        self.resolve(media)
        self.t.event("file-loaded")
        self.t.props["time-pos"] = 47.25
        self.t.messages["web-playlist-quality"]("https://media.example.test/high.m4s")
        self.begin()
        self.assertEqual(edl_sources(self.t.props["stream-open-filename"]),
                         ["https://media.example.test/high.m4s", media["variants"][1]["audio_url"]])
        self.assertEqual(len(self.t.props["file-local-options/audio-files"]), 0)
        self.assertEqual(float(self.t.props["file-local-options/start"]), 47.25)

    def test_invalid_media_fails_closed_and_releases_hook(self):
        for changes in ({"identity": "bilibili:123:789"}, {"cid": "789"}, {"video": "file:///private"},
                        {"audio": "https://media.example.test/x\r\nCookie: private"},
                        {"subtitle": "data:text/plain,abc"}, {"position": -1}):
            with self.subTest(changes=changes):
                self.setUp()
                self.load()
                hook = self.begin()
                self.resolve(self.media(**changes))
                self.assertEqual(hook.continued, 1)
                self.assertEqual(self.t.props["stream-open-filename"], "memory://")
                self.assertIsNone(self.t.props[CURRENT])

    def test_quality_reload_keeps_stable_entry_and_current_pause_position(self):
        self.load()
        self.begin()
        self.resolve()
        self.t.event("file-loaded")
        self.t.props["time-pos"] = 47.25
        self.t.props.pause = True
        self.t.messages["web-playlist-quality"]("https://media.example.test/high.m4s")
        self.assertIn(["playlist-play-index", "current"], self.commands())
        self.assertFalse(any(command[0] == "loadfile" for command in self.commands()))
        hook = self.begin()
        self.assertEqual(hook.deferred, 0)
        self.assertEqual(self.t.props["stream-open-filename"], "https://media.example.test/high.m4s")
        self.assertEqual(float(self.t.props["file-local-options/start"]), 47.25)
        self.assertTrue(self.t.props.pause)
        self.assertEqual(len(self.t.props["file-local-options/audio-files"]), 0)
        self.t.event("file-loaded")
        self.assertIsNone(self.t.props[QUALITY_SWITCH])
        self.assertEqual(self.t.props[CURRENT].stage, "ready")

    def test_quality_click_during_initial_load_does_not_replace_saved_resume_with_zero(self):
        self.load()
        self.begin()
        self.resolve(self.media(position=47.25))
        self.t.props["time-pos"] = None
        self.t.messages["web-playlist-quality"]("https://media.example.test/high.m4s")
        self.assertEqual(self.commands(), [])
        self.assertEqual(float(self.t.props["file-local-options/start"]), 47.25)
        self.assertIsNone(self.t.props[QUALITY_SWITCH])

    def test_rapid_quality_clicks_only_schedule_one_reload_before_on_load(self):
        self.load()
        self.begin()
        self.resolve()
        self.t.event("file-loaded")
        self.t.props["time-pos"] = 47.25
        for target in ("high", "video", "high"):
            self.t.messages["web-playlist-quality"](f"https://media.example.test/{target}.m4s")
        self.assertEqual(self.commands(), [["playlist-play-index", "current"]])
        self.assertEqual(self.t.props[QUALITY_SWITCH].position, 47.25)

    def test_quality_click_during_reload_waits_until_file_loaded(self):
        self.load()
        self.begin()
        self.resolve()
        self.t.event("file-loaded")
        self.t.props["time-pos"] = 47.25
        self.t.messages["web-playlist-quality"]("https://media.example.test/high.m4s")
        self.begin()
        self.t.props["time-pos"] = None
        self.t.messages["web-playlist-quality"]("https://media.example.test/video.m4s")
        self.assertEqual(self.commands(), [["playlist-play-index", "current"]])
        self.assertEqual(float(self.t.props["file-local-options/start"]), 47.25)
        self.t.event("file-loaded")
        self.t.props["time-pos"] = 48
        self.t.messages["web-playlist-quality"]("https://media.example.test/video.m4s")
        self.assertEqual(self.commands(), [["playlist-play-index", "current"], ["playlist-play-index", "current"]])
        self.assertEqual(self.t.props[QUALITY_SWITCH].position, 48)

    def test_unknown_quality_does_not_replace_playlist_entry(self):
        self.load()
        self.begin()
        self.resolve()
        self.t.messages["web-playlist-quality"]("https://attacker.example.test/unknown")
        self.assertFalse(any(command[0] == "playlist-play-index" for command in self.commands()))
        self.assertIsNone(self.t.props[QUALITY_SWITCH])

    def test_shutdown_releases_pending_hook_once(self):
        self.load()
        hook = self.begin()
        self.t.event("shutdown")
        self.t.advance(60)
        self.assertEqual(hook.continued, 1)


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class WebPlaylistManifestLuaTests(LuaPlayerHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.load()

    def item(self, number=1):
        return {"itemId": f"{1000 + number}:{2000 + number}", "title": f"Video {number} / Part 1",
                "duration": 90, "sectionId": "main", "sectionTitle": "Main section",
                "videoId": str(1000 + number), "videoTitle": f"Video {number}",
                "partTitle": "Part 1", "p": 1, "partCount": 1, "order": number}

    def message(self, stage, revision=1, **values):
        data = {"playlistId": PLAYLIST_ID, "revision": revision, **values}
        self.t.messages[f"web-playlist-manifest-{stage}"](json.dumps(data))
        return self.t.props[MANIFEST_ACK]

    def begin_manifest(self, count=2, revision=1):
        return self.message("begin", revision, itemCount=count, collectionId="collection-1", collectionTitle="Complete collection")

    def chunk(self, items, index=0, revision=1):
        return self.message("chunk", revision, chunkIndex=index, items=items)

    def commit(self, count=2, chunks=1, revision=1):
        return self.message("commit", revision, itemCount=count, chunkCount=chunks)

    def test_duplicate_begin_chunk_and_commit_are_idempotent(self):
        self.assertTrue(self.begin_manifest().ok)
        self.assertTrue(self.begin_manifest().ok)
        items = [self.item(1), self.item(2)]
        self.assertTrue(self.chunk(items).ok)
        self.assertTrue(self.chunk(items).ok)
        self.assertTrue(self.commit().ok)
        self.assertTrue(self.commit().ok)
        self.assertEqual(len(self.t.props[MANIFEST]["items"]), 2)

    def test_old_revision_cannot_replace_committed_manifest(self):
        self.begin_manifest(revision=2)
        self.chunk([self.item(1), self.item(2)], revision=2)
        self.commit(revision=2)
        self.assertFalse(self.begin_manifest(revision=1).ok)
        self.assertFalse(self.chunk([self.item(3), self.item(4)], revision=1).ok)
        self.assertFalse(self.commit(revision=1).ok)
        self.assertEqual(self.t.props[MANIFEST].revision, 2)
        self.assertEqual(self.t.props[MANIFEST]["items"][1].itemId, self.item(1)["itemId"])

    def test_incomplete_commit_does_not_publish_partial_manifest(self):
        self.begin_manifest()
        self.chunk([self.item(1)])
        self.assertFalse(self.commit().ok)
        self.assertIsNone(self.t.props[MANIFEST])
        self.chunk([self.item(2)], index=1)
        self.assertTrue(self.commit(chunks=2).ok)
        self.assertEqual(len(self.t.props[MANIFEST]["items"]), 2)

    def test_duplicate_cid_is_rejected_atomically_within_and_across_chunks(self):
        self.begin_manifest()
        self.assertFalse(self.chunk([self.item(1), self.item(1)]).ok)
        self.assertTrue(self.chunk([self.item(1)]).ok)
        self.assertFalse(self.chunk([self.item(1)], index=1).ok)
        self.assertTrue(self.chunk([self.item(2)], index=1).ok)
        self.assertTrue(self.commit(chunks=2).ok)
        self.assertEqual(len(self.t.props[MANIFEST]["items"]), 2)

    def test_same_chunk_with_changed_public_metadata_conflicts(self):
        self.begin_manifest()
        self.chunk([self.item(1), self.item(2)])
        changed = {**self.item(1), "partTitle": "Changed part"}
        self.assertFalse(self.chunk([changed, self.item(2)]).ok)
        self.assertTrue(self.commit().ok)
        self.assertEqual(self.t.props[MANIFEST]["items"][1].partTitle, "Part 1")

    def test_manifest_upload_does_not_mutate_pending_request_or_current_media(self):
        hook = self.begin()
        generation = self.t.props[REQUEST].generation
        self.begin_manifest()
        self.chunk([self.item(1), self.item(2)])
        self.commit()
        self.assertEqual(hook.continued, 0)
        self.assertEqual(self.t.props[REQUEST].generation, generation)
        self.assertEqual(self.t.props[REQUEST].itemId, "123:456")
        self.assertIsNone(self.t.props[CURRENT])
        self.resolve()
        current = self.t.props[CURRENT]
        self.begin_manifest(revision=2)
        self.chunk([self.item(3), self.item(4)], revision=2)
        self.commit(revision=2)
        self.assertEqual(self.t.props[CURRENT].identity, current.identity)
        self.assertEqual(self.t.props[CURRENT].generation, current.generation)
        self.assert_no_request()

    def test_1001_full_schema_items_publish_to_sibling_property_without_item_cap(self):
        items = [self.item(number) for number in range(1, 1002)]
        self.begin_manifest(count=len(items))
        chunks = [items[start:start + 25] for start in range(0, len(items), 25)]
        for index, chunk in enumerate(chunks):
            self.assertTrue(self.chunk(chunk, index=index).ok)
        self.assertTrue(self.commit(count=len(items), chunks=len(chunks)).ok)
        manifest = self.t.props[MANIFEST]
        self.assertEqual(len(manifest["items"]), 1001)
        self.assertEqual(manifest.itemCount, 1001)
        self.assertEqual(self.t.props["user-data/mpv-lazy/web-playlist/manifest-api-version"], 1)
        for index in (1, 501, 1001):
            self.assertEqual(dict(manifest["items"][index].items()), items[index - 1])
        self.assertIsNone(self.t.props["user-data/mpv-lazy/web-playlist/manifest"])

    def test_same_revision_replay_after_commit_does_not_republish_manifest(self):
        items = [self.item(1), self.item(2)]
        self.begin_manifest()
        self.chunk(items)
        self.commit()
        self.lua.execute("T.manifestWrites=0; local original=mp.set_property_native; function mp.set_property_native(k,v) if k=='user-data/mpv-lazy/web-playlist-manifest' then T.manifestWrites=T.manifestWrites+1 end; original(k,v) end")
        self.assertTrue(self.begin_manifest().ok)
        self.assertTrue(self.chunk(items).ok)
        self.assertTrue(self.commit().ok)
        self.assertEqual(self.t.manifestWrites, 0)

    def test_bootstrap_manifest_is_explicitly_pending_until_full_manifest_commits(self):
        args = {"itemCount": 1, "collectionId": "collection-1", "collectionTitle": "Complete collection"}
        self.assertTrue(self.message("begin", metadataPending=True, **args).ok)
        self.chunk([self.item(1)])
        self.assertTrue(self.commit(count=1).ok)
        self.assertTrue(self.t.props[MANIFEST].metadataPending)
        self.assertEqual(self.t.props[METADATA_STATE].status, "pending")
        self.assertFalse(self.message("begin", metadataPending=False, **args).ok)
        self.assertTrue(self.message("begin", revision=2, metadataPending=False, **args).ok)
        self.chunk([self.item(1)], revision=2)
        self.assertTrue(self.commit(count=1, revision=2).ok)
        self.assertFalse(self.t.props[MANIFEST].metadataPending)
        self.assertEqual(self.t.props[METADATA_STATE].status, "ready")

    def test_invalid_pending_field_is_rejected_without_publishing_manifest(self):
        self.assertFalse(self.message("begin", itemCount=1, metadataPending="true").ok)
        self.assertIsNone(self.t.props[MANIFEST])

    def test_late_bootstrap_commit_cannot_replace_metadata_failure_with_loading(self):
        self.assertTrue(self.message('begin', itemCount=1, metadataPending=True).ok)
        self.chunk([self.item(1)])
        self.t.messages['web-playlist-metadata-error'](json.dumps({
            'playlistId': PLAYLIST_ID, 'revision': 1, 'error': 'playlist_metadata_failed'}))
        self.assertEqual(self.t.props[METADATA_STATE].status, 'failed')
        self.assertTrue(self.commit(count=1).ok)
        self.assertEqual(self.t.props[METADATA_STATE].status, 'failed')
        self.assertTrue(self.message('begin', itemCount=1, metadataPending=False, revision=2).ok)
        self.chunk([self.item(1)], revision=2)
        self.assertTrue(self.commit(count=1, revision=2).ok)
        self.assertEqual(self.t.props[METADATA_STATE].status, 'ready')


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class WebPlaylistExtensionTests(LuaPlayerHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.load()
        self.t.simulate_playlist = True
        self.original = {"id": 17, "filename": self.path("123:789"), "current": True}
        self.t.props.path = self.original["filename"]
        self.t.props.playlist = self.to_lua([self.original])
        self.begin("123:789")
        self.resolve(self.media("123:789"))
        self.t.event("file-loaded")
        self.t.props["time-pos"] = 47.25
        self.t.props.pause = True
        self.t.props["file-local-options/audio-files"] = self.to_lua(["audio-sentinel"])
        self.t.props["file-local-options/sub-files"] = self.to_lua(["ass-sentinel"])
        self.t.props[CURRENT] = self.to_lua({**self.media("123:789"), "stage": "ready", "generation": 8})

    def extend(self, revision=2, count=3, url=None, **changes):
        data = {"playlistId": PLAYLIST_ID, "revision": revision, "itemCount": count,
                "playlistUrl": url or f"http://127.0.0.1:9002/web-playlist/{PLAYLIST_ID}/list.m3u"}
        data.update(changes)
        self.t.messages["web-playlist-extend"](json.dumps(data))
        return self.t.props[EXTEND_ACK]

    def complete(self, items=("123:456", "123:789", "123:999"), success=True):
        native = [self.original, *[{"id": 20 + index, "filename": self.path(item), "current": False}
                                 for index, item in enumerate(items)]]
        self.t.props.playlist = self.to_lua(native)
        self.t["async"][1].callback(success)
        return self.t.props[EXTEND_ACK]

    def native(self):
        return [dict(entry.items()) for entry in self.t.props.playlist.values()]

    def test_complete_list_moves_original_current_entry_without_reload_or_lost_state(self):
        self.extend()
        self.assertIsNone(self.t.props[EXTEND_ACK])
        self.assertEqual(list(self.t["async"][1].args.values()),
                         ["loadlist", f"http://127.0.0.1:9002/web-playlist/{PLAYLIST_ID}/list.m3u", "append"])
        ack = self.complete()
        self.assertTrue(ack.ok)
        self.assertEqual([entry["filename"] for entry in self.native()],
                         [self.path(item) for item in ("123:456", "123:789", "123:999")])
        self.assertEqual(self.native()[1]["id"], 17)
        self.assertTrue(self.native()[1]["current"])
        self.assertEqual(self.t.props["time-pos"], 47.25)
        self.assertTrue(self.t.props.pause)
        self.assertEqual(list(self.t.props["file-local-options/audio-files"].values()), ["audio-sentinel"])
        self.assertEqual(list(self.t.props["file-local-options/sub-files"].values()), ["ass-sentinel"])
        self.assertEqual(self.t.props[CURRENT].generation, 8)
        self.assertEqual(self.t.props[METADATA_STATE].status, "pending")
        self.assertEqual(self.commands(), [["playlist-remove", "2"], ["playlist-move", "0", "2"]])

    def test_current_entry_can_be_first_or_last_in_full_collection(self):
        for items in (("123:789", "123:456", "123:999"), ("123:456", "123:999", "123:789")):
            with self.subTest(items=items):
                self.setUp()
                self.extend()
                self.assertTrue(self.complete(items).ok)
                self.assertEqual([entry["filename"] for entry in self.native()], [self.path(item) for item in items])
                self.assertEqual(next(entry["id"] for entry in self.native() if entry["current"]), 17)

    def test_same_revision_retries_during_and_after_extension_do_not_append_again(self):
        self.extend()
        self.extend()
        self.assertEqual(len(self.t["async"]), 1)
        self.assertIsNone(self.t.props[EXTEND_ACK])
        self.complete()
        self.assertTrue(self.extend().ok)
        self.assertEqual(len(self.t["async"]), 1)
        self.assertFalse(self.extend(revision=1).ok)
        self.assertFalse(self.extend(count=4).ok)
        self.assertEqual(len(self.t["async"]), 1)

    def test_early_extension_waits_for_initial_file_loaded_without_re_resolving(self):
        self.begin("123:789")
        self.resolve(self.media("123:789", position=47.25))
        self.extend()
        self.assertEqual(len(self.t["async"]), 0)
        self.assertIsNone(self.t.props[EXTEND_ACK])
        self.extend()
        self.assertEqual(len(self.t["async"]), 0)
        self.t.event("file-loaded")
        self.assertEqual(len(self.t["async"]), 1)
        self.assertTrue(self.complete().ok)
        self.assertEqual(self.t.props["time-pos"], 47.25)

    def test_foreign_port_or_playlist_cannot_append(self):
        self.assertFalse(self.extend(url=f"http://127.0.0.1:9003/web-playlist/{PLAYLIST_ID}/list.m3u").ok)
        self.extend(playlistId="playlist-" + "b" * 32)
        self.assertEqual(len(self.t["async"]), 0)

    def test_user_modified_initial_native_list_is_preserved(self):
        self.t.props.playlist = self.to_lua([self.original, {"id": 30, "filename": "user-added.mp4"}])
        self.assertFalse(self.extend().ok)
        self.assertEqual(len(self.t["async"]), 0)
        self.assertEqual(self.native()[1]["filename"], "user-added.mp4")

    def test_incomplete_or_duplicate_append_rolls_back_only_added_entries(self):
        for items in (("123:456", "123:789"), ("123:456", "123:789", "123:456")):
            with self.subTest(items=items):
                self.setUp()
                self.extend()
                self.assertFalse(self.complete(items).ok)
                self.assertEqual(self.native(), [self.original])
                self.assertEqual(self.t.props[METADATA_STATE].status, "failed")

    def test_failed_append_and_timeout_leave_original_current_playing(self):
        self.extend()
        self.assertFalse(self.complete(success=False).ok)
        self.assertEqual(self.native(), [self.original])
        self.setUp()
        self.extend()
        self.t.advance(16)
        self.assertFalse(self.t.props[EXTEND_ACK].ok)
        self.assertTrue(self.t["async"][1].aborted)
        before = self.commands()
        self.t["async"][1].callback(True)
        self.assertEqual(self.commands(), before)
        self.assertEqual(self.native(), [self.original])

    def test_unrelated_media_added_during_append_is_not_removed_on_failure(self):
        self.extend()
        other = {"id": 99, "filename": "user-added.mp4", "current": False}
        self.t.props.playlist = self.to_lua([self.original, other,
                                            {"id": 20, "filename": self.path("123:789")}])
        self.t["async"][1].callback(True)
        self.assertFalse(self.t.props[EXTEND_ACK].ok)
        self.assertEqual(self.native(), [self.original, other])

    def test_current_native_id_replacement_is_rejected_even_for_same_path(self):
        self.extend()
        replacement = {**self.original, "id": 90}
        self.t.props.playlist = self.to_lua([replacement,
                                            {"id": 20, "filename": self.path("123:456")},
                                            {"id": 21, "filename": self.path("123:789")},
                                            {"id": 22, "filename": self.path("123:999")}])
        self.t["async"][1].callback(True)
        self.assertFalse(self.t.props[EXTEND_ACK].ok)
        self.assertEqual(self.native(), [replacement])

    def test_metadata_error_is_scoped_and_does_not_change_playback(self):
        error = {"playlistId": PLAYLIST_ID, "revision": 2, "error": "playlist_metadata_failed"}
        self.t.messages["web-playlist-metadata-error"](json.dumps(error))
        self.assertEqual(self.t.props[METADATA_STATE].status, "failed")
        self.assertEqual(self.native(), [self.original])
        self.assertEqual(self.t.props["time-pos"], 47.25)
        self.assertTrue(self.t.props.pause)
        self.extend(revision=3)
        self.complete()
        self.t.messages["web-playlist-metadata-error"](json.dumps(error))
        self.assertEqual(self.t.props[METADATA_STATE].status, "pending")


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class PlaylistDanmakuIsolationTests(LuaPlayerHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.t.props["track-list"] = self.to_lua([])
        self.t.option_values.cid = "456"
        self.lua.execute(BILIBILI.read_text(encoding="utf-8"))

    def context(self, item="123:456", generation=1):
        value = self.media(item)
        value.update({"playlistId": PLAYLIST_ID, "sessionId": SESSION_ID,
                      "path": self.path(item), "itemId": item, "generation": generation})
        self.t.props[CURRENT] = self.to_lua(value)
        self.t.props.path = value["path"]
        return value["path"]

    def test_playlist_current_cid_overrides_initial_launch_cid(self):
        path = self.context("123:789")
        self.g.load_danmaku_for_bilibili(path, lambda _: None)
        self.assertEqual(self.t.downloads[1].args[len(self.t.downloads[1].args)], "https://comment.bilibili.com/789.xml")

    def test_automatic_load_without_callback_handles_failed_cid_request(self):
        path = "https://www.bilibili.com/video/BV1CabT6vEdp/"
        self.t.option_values.cid = None
        self.t.props.path = path
        self.g.load_danmaku_for_bilibili(path)
        self.assertEqual(len(self.t.downloads), 1)
        self.t.downloads[1].callback("network failure", None)
        self.assertEqual(len(self.t.saved), 0)
        self.assertEqual(self.t.loaded, 0)

    def test_automatic_load_without_callback_handles_empty_cid_api_data(self):
        path = "https://www.bilibili.com/video/BV1CabT6vEdp/"
        self.t.option_values.cid = None
        self.t.props.path = path
        self.g.load_danmaku_for_bilibili(path)
        self.t.downloads[1].callback(None, '{"code":-404}')
        self.assertEqual(len(self.t.saved), 0)
        self.assertEqual(self.t.loaded, 0)

    def test_danmaku_response_for_prior_item_is_discarded_after_switch(self):
        result = []
        self.g.load_danmaku_for_bilibili(self.context(), result.append)
        self.context("123:789", 2)
        self.t.downloads[1].callback(None, "<i>old CID rows</i>")
        self.assertEqual(result, [False])
        self.assertEqual(len(self.t.saved), 0)
        self.assertEqual(self.t.loaded, 0)

    def test_danmaku_response_for_prior_loop_generation_is_discarded(self):
        result = []
        self.g.load_danmaku_for_bilibili(self.context(), result.append)
        self.context(generation=2)
        self.t.downloads[1].callback(None, "<i>stale loop rows</i>")
        self.assertEqual(result, [False])
        self.assertEqual(len(self.t.saved), 0)

    def test_matching_danmaku_response_is_saved_to_item_path(self):
        result = []
        path = self.context()
        self.g.load_danmaku_for_bilibili(path, result.append)
        self.t.downloads[1].callback(None, "<i>current CID rows</i>")
        self.assertEqual(result, [True])
        self.assertEqual(self.t.saved[1].path, path)
        self.assertEqual(self.t.loaded, 1)

    def test_ordinary_video_late_response_is_discarded_after_entering_playlist(self):
        result = []
        old_path = "https://media.example.test/ordinary-old.mp4"
        self.t.props.path = old_path
        self.t.props[CURRENT] = None
        self.g.load_danmaku_for_bilibili(old_path, result.append)
        self.context("123:789", 2)
        self.t.downloads[1].callback(None, "<i>old ordinary CID rows</i>")
        self.assertEqual(result, [False])
        self.assertEqual(len(self.t.saved), 0)
        self.assertEqual(self.t.loaded, 0)

    def test_quality_same_stable_path_rejects_old_generation_but_accepts_new_response(self):
        old_result, new_result = [], []
        path = self.context()
        self.g.load_danmaku_for_bilibili(path, old_result.append)
        self.context(generation=2)
        self.g.load_danmaku_for_bilibili(path, new_result.append)
        self.t.downloads[1].callback(None, "<i>prior quality rows</i>")
        self.t.downloads[2].callback(None, "<i>current quality rows</i>")
        self.assertEqual(old_result, [False])
        self.assertEqual(new_result, [True])
        self.assertEqual(len(self.t.saved), 1)
        self.assertEqual(self.t.saved[1].path, path)
        self.assertEqual(self.t.loaded, 1)


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class PlaylistProgressLuaTests(LuaPlayerHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.t.opts.web_progress = self.to_lua({"identity": "bilibili:123:456", "session_id": SESSION_ID})
        self.t.option_values["web_playlist-id"] = PLAYLIST_ID
        self.lua.execute(PROGRESS.read_text(encoding="utf-8"))

    def current(self, item="123:456", generation=1, position=12.5, duration=100):
        value = self.media(item, position=position, duration=duration)
        value.update({"playlistId": PLAYLIST_ID, "sessionId": SESSION_ID,
                      "path": self.path(item), "itemId": item, "generation": generation})
        self.t.props.path = value["path"]
        self.t.props[CURRENT] = self.to_lua(value)
        self.t.observe(CURRENT)

    def play(self, item="123:456", generation=1, position=12.5, duration=100):
        self.t.props.path = self.path(item)
        self.t.event("start-file")
        self.current(item, generation, position, duration)
        self.t.props["time-pos"] = position
        self.t.props.duration = duration
        self.t.event("file-loaded")

    def snapshot(self):
        return self.t.props[SNAPSHOT]

    def test_playlist_progress_adopts_each_cid_and_resume_position(self):
        self.play(position=28)
        self.assertEqual(self.snapshot().identity, "bilibili:123:456")
        self.t.event("end-file", "stop")
        self.play("123:789", 2, 3.25, 220)
        self.assertEqual(self.snapshot().identity, "bilibili:123:789")
        self.assertEqual(self.snapshot().position, 3.25)
        self.assertEqual(self.snapshot().duration, 220)
        self.assertFalse(self.snapshot().completed)
        self.assertFalse(self.snapshot().ended)
        self.assertEqual(self.t.props[FINAL][1].snapshot.identity, "bilibili:123:456")
        self.assertEqual(self.t.props[FINAL][1].snapshot.position, 28)

    def test_eof_final_is_retained_after_next_item_is_loaded(self):
        self.play(position=99)
        self.t.props["time-pos"] = None
        self.t.event("end-file", "eof")
        self.play("123:789", 2, 0, 200)
        final = self.t.props[FINAL][1]
        self.assertEqual(final.snapshot.identity, "bilibili:123:456")
        self.assertTrue(final.snapshot.completed)
        self.assertEqual(final.snapshot.position, 100)
        self.assertEqual(self.snapshot().identity, "bilibili:123:789")
        self.assertFalse(self.snapshot().completed)

    def test_same_cid_loop_resets_completion_and_position(self):
        self.play(position=99)
        self.t.event("end-file", "eof")
        self.play(generation=2, position=0)
        self.assertTrue(self.snapshot().ready)
        self.assertEqual(self.snapshot().position, 0)
        self.assertFalse(self.snapshot().completed)
        self.assertFalse(self.snapshot().ended)
        self.assertTrue(self.t.props[FINAL][1].snapshot.completed)

    def test_final_queue_is_bounded_with_monotonic_sequences(self):
        for generation in range(1, 42):
            self.play(generation=generation, position=generation)
            self.t.event("end-file", "stop")
        finals = self.t.props[FINAL]
        self.assertEqual(len(finals), 32)
        self.assertEqual([record.finalSequence for record in finals.values()], list(range(10, 42)))
        self.assertEqual(finals[1].snapshot.position, 10)
        self.assertEqual(finals[32].snapshot.position, 41)

    def test_quality_reload_is_not_recorded_as_finished_video(self):
        self.play(position=47.25)
        self.t.props[QUALITY_SWITCH] = self.to_lua({"playlistId": PLAYLIST_ID, "itemId": "123:456",
                                                  "identity": "bilibili:123:456", "expiresAt": 15,
                                                  "position": 47.25})
        self.t.hook("on_unload")
        self.t.event("end-file", "stop")
        self.assertTrue(self.t.props[FINAL] is None or len(self.t.props[FINAL]) == 0)
        self.assertFalse(self.snapshot().ended)
        self.assertFalse(self.snapshot().completed)
        self.assertEqual(self.snapshot().position, 47.25)

    def test_expired_quality_marker_does_not_hide_real_eof(self):
        self.play(position=99)
        self.t.props[QUALITY_SWITCH] = self.to_lua({"playlistId": PLAYLIST_ID, "itemId": "123:456",
                                                  "identity": "bilibili:123:456", "expiresAt": 1})
        self.t.advance(2)
        self.t.event("end-file", "eof")
        self.assertTrue(self.t.props[FINAL][1].snapshot.completed)
        self.assertEqual(self.t.props[FINAL][1].snapshot.position, 100)

    def test_foreign_context_does_not_reassign_current_progress_identity(self):
        self.play(position=25)
        self.t.props[CURRENT] = self.to_lua({"playlistId": PLAYLIST_ID, "sessionId": "progress-foreign",
                                           "path": self.path(), "itemId": "123:789",
                                           "identity": "bilibili:123:789", "generation": 2})
        self.t.observe(CURRENT)
        self.assertEqual(self.snapshot().identity, "bilibili:123:456")
        self.assertEqual(self.snapshot().position, 25)

    def test_unrelated_file_does_not_write_previous_cid_progress(self):
        self.play(position=25)
        self.t.event("end-file", "stop")
        self.t.props.path = "https://media.example.test/unrelated.mp4"
        self.t.event("start-file")
        self.t.props["time-pos"] = 88
        self.t.props.duration = 300
        self.t.event("file-loaded")
        self.t.advance(2)
        self.assertFalse(self.snapshot().active)
        self.assertFalse(self.snapshot().ready)
        self.assertEqual(self.snapshot().position, 25)

    def test_quality_resume_waits_for_actual_seek_before_publishing_zero(self):
        self.play(position=47.25)
        self.t.props[QUALITY_SWITCH] = self.to_lua({"playlistId": PLAYLIST_ID, "itemId": "123:456",
                                                  "identity": "bilibili:123:456", "expiresAt": 15,
                                                  "position": 47.25})
        self.t.event("end-file", "stop")
        self.t.event("start-file")
        self.current(generation=2, position=47.25)
        self.t.props["time-pos"] = 0
        self.t.event("file-loaded")
        self.t.props[QUALITY_SWITCH] = None
        self.t.advance(1)
        self.assertEqual(self.snapshot().position, 47.25)
        self.assertFalse(self.snapshot().ready)
        self.t.props["time-pos"] = 47.25
        self.t.event("playback-restart")
        self.assertTrue(self.snapshot().ready)
        self.assertEqual(self.snapshot().position, 47.25)

    def test_failed_initial_resume_does_not_overwrite_saved_position_with_zero(self):
        self.t.props.path = self.path()
        self.t.event("start-file")
        self.current(position=42)
        self.t.props["time-pos"] = 0
        self.t.props.duration = 100
        self.t.event("file-loaded")
        self.t.advance(16)
        self.assertFalse(self.snapshot().active)
        self.assertFalse(self.snapshot().ready)
        self.assertTrue(self.snapshot().ended)
        self.assertEqual(self.snapshot().position, 42)
        self.assertEqual(len(self.t.props[FINAL]), 0)


@unittest.skipIf(LuaRuntime is None, "lupa.lua51 is required")
class PlaylistQualityMenuTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing complete quality-menu API simulator without
        # inheriting its tests or making assumptions about private locals.
        from test_quality_menu_lua import QualityMenuLuaTests
        self.helper = QualityMenuLuaTests()
        self.path = f"http://127.0.0.1:9002/web-playlist/{PLAYLIST_ID}/item/123:456"
        self.helper.load(path=self.path)
        self.lua, self.state = self.helper.lua, self.helper.state
        self.lua.globals()._format_playlist_json = lambda value: json.dumps(self.to_python(value))
        self.lua.execute("package.loaded['mp.utils'].format_json=function(value) TEST.menu=value; return _format_playlist_json(value) end")

    def to_python(self, value):
        if hasattr(value, "items"):
            items = dict(value.items())
            if items and set(items) == set(range(1, len(items) + 1)):
                return [self.to_python(items[index]) for index in range(1, len(items) + 1)]
            return {key: self.to_python(item) for key, item in items.items()}
        return value

    def current(self, variants, video=None, item="123:456"):
        self.path = f"http://127.0.0.1:9002/web-playlist/{PLAYLIST_ID}/item/{item}"
        self.state.props.path = self.path
        self.state.props[CURRENT] = self.lua.table_from({"path": self.path, "playlistId": PLAYLIST_ID,
                                                        "sessionId": SESSION_ID, "itemId": item,
                                                        "identity": "bilibili:" + item,
                                                        "video": video or variants[0]["url"], "variants": variants}, recursive=True)
        self.state.fire("start-file")
        self.state.fire("on_load")
        self.state.fire("file-loaded")

    def test_quality_menu_sends_stable_playlist_switch_without_loadfile(self):
        variants = [{"url": "https://media.example.test/low.m4s", "height": 720, "title": "720P"},
                    {"url": "https://media.example.test/high.m4s", "height": 1080, "title": "1080P"}]
        self.current(variants)
        self.helper.open()
        self.assertEqual([item.title for item in self.helper.menu_items()[1:]], ["1080P", "720P"])
        self.state.messages["video-format-set"](self.path, variants[1]["url"])
        self.assertIn(["script-message-to", "web_playlist", "web-playlist-quality", variants[1]["url"]], self.helper.commands())
        self.assertFalse(any(command[0] == "loadfile" for command in self.helper.commands()))

    def test_each_playlist_item_has_its_own_quality_list(self):
        self.current([{"url": "https://media.example.test/first.m4s", "height": 1080, "title": "First 1080P"}])
        self.helper.open()
        self.assertEqual(self.helper.menu_items()[1].title, "First 1080P")
        self.current([{"url": "https://media.example.test/second.m4s", "height": 480, "title": "Second 480P"}], item="123:789")
        self.helper.open()
        self.assertEqual(self.helper.menu_items()[1].title, "Second 480P")
        self.assertFalse(any(item.title == "First 1080P" for item in self.helper.menu_items()))

    def test_signed_variant_percent_escapes_are_preserved_exactly(self):
        variants = [{"url": "https://media.example.test/low.m4s?token=a%2Cb", "height": 720, "title": "720P"},
                    {"url": "https://media.example.test/high.m4s?token=c%3Dd", "height": 1080, "title": "1080P"}]
        self.current(variants)
        self.helper.open()
        self.assertTrue(self.helper.menu_items()[-1].active)
        self.state.messages["video-format-set"](self.path, variants[1]["url"])
        self.assertIn(["script-message-to", "web_playlist", "web-playlist-quality", variants[1]["url"]], self.helper.commands())


if __name__ == "__main__":
    unittest.main()
