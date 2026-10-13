"""Native playlist metadata, lazy resolution, and per-item progress isolation."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import BridgeState, handle_http_client
from playback_progress import FINALS_PROPERTY, SNAPSHOT_PROPERTY, HttpProgressTransport, PlaybackProgressManager, PlaybackProgressSession, ProgressStore
from test_playback_progress import Owner, eventually
from web_playlist import DATA_PROPERTY, REQUEST_PROPERTY, MANIFEST_API_PROPERTY, MANIFEST_ACK_PROPERTY, WebPlaylistManager, WebPlaylistSession, media_url, resolved_media, validate_items

PID = "playlist-" + "a" * 32
CLIENT = "progress-http-" + "a" * 32
OTHER_CLIENT = "progress-http-" + "b" * 32
SID = "progress-playlist-fixture"
SOURCE = "https://upos-fixture.bilivideo.com/video.m4s?deadline=fixture-token"
AUDIO = "https://upos-fixture.bilivideo.cn/audio.m4s?token=audio-private"


def item(cid=2002, page=1, **updates):
    result = {"itemId": f"1001:{cid}", "aid": 1001, "cid": cid, "bvid": "BV1fixture", "p": page,
              "title": f"Part {page}", "duration": 120, "pageUrl": f"https://www.bilibili.com/video/BV1fixture?p={page}"}
    result.update(updates)
    return result


def registration(**updates):
    result = {"requestId": "playlist-request", "playlistId": PID, "sessionId": SID, "pipeName": "mpv-lazy-" + SID,
              "items": [item(), item(2003, 2)], "selectedItemId": "1001:2002", "initialPosition": 18.5}
    result.update(updates)
    return result


def progress_request(identity="bilibili:1001:2002", **updates):
    result = {"requestId": "progress-request", "provider": "bilibili", "identity": identity,
              "pageUrl": "https://www.bilibili.com/video/BV1fixture?p=1", "playlistId": PID, "sessionId": SID,
              "pipeName": "mpv-lazy-" + SID, "position": 18.5, "duration": 120, "paused": True,
              "updatedAt": 1700000000000}
    result.update(updates)
    return result


def snapshot(identity="bilibili:1001:2002", **updates):
    result = {"apiVersion": 1, "playlistId": PID, "sessionId": SID, "identity": identity,
              "active": True, "ready": True, "position": 18.5, "duration": 120,
              "paused": True, "ended": False, "completed": False}
    result.update(updates)
    return result


def lazy_request(item_id="1001:2002", generation=1):
    return {"playlistId": PID, "itemId": item_id, "generation": generation}


def submission(item_id="1001:2002", generation=1, **updates):
    result = {"requestId": "resolved-request", **lazy_request(item_id, generation),
              "media": {"video": SOURCE, "audio": AUDIO, "position": 18.5, "variants": [
                  {"url": SOURCE, "audio_url": AUDIO, "quality": 80, "height": 1080, "title": "1080P", "codec": "avc1", "preferred": True}]}}
    result.update(updates)
    return result


class Ipc:
    def __init__(self):
        self.props = {REQUEST_PROPERTY: None, SNAPSHOT_PROPERTY: None, FINALS_PROPERTY: []}
        self.commands = []
        self.closed = False
        self.errors = {}
        self.manifest_api = False
        self.manifest_items = []
        self.manifest_seen = set()
        self.manifest_drop_once = False

    async def command(self, command):
        self.commands.append(copy.deepcopy(command))
        if self.closed:
            raise FileNotFoundError(2, "fixture named pipe missing")
        if command[0] == "get_property":
            if self.errors.get(command[1]):
                raise self.errors[command[1]].pop(0)
            if command[1] == "pid":
                return 12345
            if command[1] == DATA_PROPERTY:
                return {"request": copy.deepcopy(self.props.get(REQUEST_PROPERTY)), "state": {"stage": "ready"}}
            if command[1] == MANIFEST_API_PROPERTY:
                return 1 if self.manifest_api else None
            return copy.deepcopy(self.props.get(command[1]))
        if command[0] == "script-message-to" and command[2].startswith("web-playlist-manifest-"):
            body = json.loads(command[3])
            stage = command[2].rsplit("-", 1)[1]
            if stage == "begin":
                self.manifest_items, self.manifest_seen = [], set()
            elif stage == "chunk":
                key = (body["revision"], body["chunkIndex"])
                if key not in self.manifest_seen:
                    self.manifest_items.extend(copy.deepcopy(body["items"]))
                    self.manifest_seen.add(key)
                    if self.manifest_drop_once:
                        self.manifest_drop_once = False
                        return None
            self.props[MANIFEST_ACK_PROPERTY] = {"playlistId": body["playlistId"], "revision": body["revision"],
                "stage": stage, "chunkIndex": body.get("chunkIndex"), "itemCount": body["itemCount"] if stage == "begin" else len(self.manifest_items), "ok": True}
        return None


class ValidationTests(unittest.TestCase):
    def test_canonical_manifest_selected_item_and_m3u_safe_titles(self):
        entries, index = validate_items(registration(selectedItemId="1001:2003"))
        self.assertEqual(index, 1)
        self.assertEqual(entries[0]["identity"], "bilibili:1001:2002")
        self.assertEqual(entries[1]["pageUrl"], "https://www.bilibili.com/video/BV1fixture?p=2")
        for bad in ("New\n#EXTINF:0,Injected", "Title\rhttp://localhost", "Title\x00x", "a" * 361):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_items(registration(items=[item(title=bad), item(2003, 2)]))

    def test_registration_rejects_mismatched_identity_page_duplicate_and_secret_fields(self):
        bad_items = [item(itemId="1001:9999"), item(cid=True), item(p=0), item(p=True),
                     item(pageUrl="https://www.bilibili.com/video/BVother?p=1"),
                     item(pageUrl="https://www.bilibili.com/video/BV1fixture?p=2"), item(identity="bilibili:1001:9999"),
                     item(cookie="must-not-persist")]
        for bad in bad_items:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_items(registration(items=[bad, item(2003, 2)]))
        for updates in ({"items": [item()]}, {"items": [item(), item()]}, {"selectedItemId": "1001:9999"},
                        {"pipeName": "mpv-lazy-progress-foreign"}, {"playlistId": "playlist-public"},
                        {"sessionId": "progress-../local"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                validate_items(registration(**updates))

    def test_legacy_registration_has_no_500_item_count_cutoff(self):
        entries = [item(cid=number, page=number) for number in range(1, 1502)]
        self.assertEqual(len(validate_items(registration(items=entries, selectedItemId="1001:1501"))[0]), 1501)

    def test_media_rejects_local_network_paths_credentials_and_other_domains(self):
        for bad in ("file:///C:/private/video.mp4", "http://127.0.0.1:9002/status", "http://localhost/video.mp4",
                    "https://192.168.1.1/video", "https://[::1]/video", "https://login:secret@upos-fixture.bilivideo.com/video",
                    "https://bilivideo.com.evil.example/video", "https://media.example/video", "https://upos-fixture.bilivideo.com/a\nb"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                media_url(bad)
        self.assertEqual(media_url(SOURCE), SOURCE)
        self.assertEqual(media_url("https://aisubtitle.hdslb.com/source.json", subtitle=True), "https://aisubtitle.hdslb.com/source.json")
        converter = "https://www.lckp.top/common/bilibili/jsonToSrt/?url=https://aisubtitle.hdslb.com/source.json&lan=zh"
        self.assertEqual(media_url(converter, subtitle=True), converter)
        with self.assertRaises(ValueError):
            media_url(converter.replace("https://aisubtitle.hdslb.com", "http://127.0.0.1:9002"), subtitle=True)

    def test_resolved_media_metadata_is_from_manifest_and_options_are_bounded(self):
        entry = validate_items(registration())[0][0]
        media = resolved_media({**submission()["media"], "title": "foreign title", "duration": 999}, entry)
        for field in ("aid", "cid", "bvid", "p", "identity", "pageUrl", "duration", "title"):
            self.assertEqual(media[field], entry[field])
        self.assertNotIn("preferred", media["variants"][0])
        for update in ({"cid": "9999"}, {"identity": "bilibili:1001:9999"}, {"position": float("nan")},
                       {"headers": {"Cookie": "secret"}}, {"variants": [{"url": SOURCE, "cookie": "secret"}]},
                       {"variants": [{"url": SOURCE, "fps": float("inf")}]},
                       {"variants": [{"url": SOURCE + str(n)} for n in range(17)]}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                resolved_media({**submission()["media"], **update}, entry)


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ProgressStore(Path(self.directory.name) / "progress.json")
        self.ipc = Ipc()
        self.owner = Owner()
        self.progress = PlaybackProgressManager(self.store, session_factory=lambda payload, owner, store:
            PlaybackProgressSession(payload, owner, store, ipc=self.ipc, poll_interval=.005, attach_timeout=.25))
        self.playlists = WebPlaylistManager(self.progress, 9002, session_factory=lambda manager, payload, items, index, owner:
            WebPlaylistSession(manager, payload, items, index, owner, ipc=self.ipc, poll_interval=.005, attach_timeout=.25, resolve_timeout=.1))
        self.progress.playlist_registry = self.playlists
        self.transport = HttpProgressTransport(self.progress, self.playlists)

    async def asyncTearDown(self):
        await self.playlists.close()
        await self.progress.close()
        self.directory.cleanup()

    async def register(self, owner=None, **updates):
        return await self.playlists.register(registration(**updates), owner or self.owner)

    async def chunked_register(self, entries, **updates):
        payload = registration(chunked=True, selectedItemId=entries[-1]["itemId"], **updates)
        payload.pop("items")
        await self.playlists.register(payload, self.owner)
        for index, offset in enumerate(range(0, len(entries), 50)):
            await self.playlists.append_items({"playlistId": PID, "chunkIndex": index, "items": entries[offset:offset + 50]}, self.owner)
        return await self.playlists.commit_upload({"playlistId": PID, "expectedItemCount": len(entries)}, self.owner)

    async def test_chunked_1501_leaf_manifest_is_complete_and_restored_in_bounded_pages(self):
        entries = [item(cid=number, page=number, partCount=1501, videoId="1001", videoTitle="完整投稿", partTitle=f"P{number}",
            sectionId="section-main", sectionTitle="正片", order=number - 1) for number in range(1, 1502)]
        reply = await self.chunked_register(entries, collectionId="season-1", collectionTitle="完整合集")
        self.assertEqual(reply["itemCount"], 1501)
        self.assertEqual(reply["selectedIndex"], 1500)
        self.assertEqual(reply["currentItem"]["partTitle"], "P1501")
        self.assertEqual(reply["collectionTitle"], "完整合集")
        self.assertNotIn("items", reply)
        self.assertLess(len(json.dumps(reply).encode()), 16384)
        self.assertEqual(self.playlists.m3u(PID).count(b"#EXTINF:"), 1501)
        recovered, offset = [], 0
        while offset is not None:
            page = await self.playlists.get_items({"playlistId": PID, "offset": offset, "limit": 100}, self.owner)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()), 48 * 1024)
            recovered.extend(page["items"])
            offset = page["nextOffset"]
        self.assertEqual([entry["itemId"] for entry in recovered], [entry["itemId"] for entry in entries])
        state = await self.playlists.get({"identity": "bilibili:1001:1501"}, self.owner)
        self.assertEqual(state["itemCount"], 1501)
        self.assertNotIn("items", state)

    async def test_upload_chunks_are_contiguous_atomic_idempotent_and_owner_bound(self):
        payload = registration(chunked=True)
        payload.pop("items")
        begun = await self.playlists.register(payload, self.owner)
        self.assertEqual(begun["type"], "webPlaylistUploadStarted")
        first = {"playlistId": PID, "chunkIndex": 0, "items": [item()]}
        with self.assertRaises(ValueError):
            await self.playlists.append_items({**first, "chunkIndex": 1}, self.owner)
        await self.playlists.append_items(first, self.owner)
        retry = await self.playlists.append_items(first, self.owner)
        self.assertEqual((retry["nextChunkIndex"], retry["itemCount"]), (1, 1))
        for bad, owner in (({**first, "items": [item(title="changed retry")]}, self.owner),
                           ({**first, "chunkIndex": 1}, self.owner), (first, Owner())):
            with self.assertRaises(ValueError):
                await self.playlists.append_items(bad, owner)
        with self.assertRaises(ValueError):
            await self.playlists.commit_upload({"playlistId": PID, "expectedItemCount": 2}, self.owner)
        self.assertNotIn(PID, self.playlists.sessions)
        await self.playlists.append_items({"playlistId": PID, "chunkIndex": 1, "items": [item(2003, 2)]}, self.owner)
        reply = await self.playlists.commit_upload({"playlistId": PID, "expectedItemCount": 2}, self.owner)
        retry = await self.playlists.commit_upload({"playlistId": PID, "expectedItemCount": 2}, self.owner)
        self.assertEqual(reply["revision"], retry["revision"])
        self.assertNotIn(PID, self.playlists.uploads)

    async def test_upload_budget_or_invalid_hierarchy_rejects_entire_chunk_without_truncation(self):
        payload = registration(chunked=True)
        payload.pop("items")
        await self.playlists.register(payload, self.owner)
        for entries in ([item(partCount=0)], [item(order=True)], [item(sectionTitle="bad\nsection")],
                        [item(cookie="must-not-store")], [item(cid=number, page=number) for number in range(1, 102)]):
            with self.assertRaises(ValueError):
                await self.playlists.append_items({"playlistId": PID, "chunkIndex": 0, "items": entries}, self.owner)
            self.assertEqual(self.playlists.uploads[PID]["items"], [])
        with patch("web_playlist.MAX_MANIFEST_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "too large"):
                await self.playlists.append_items({"playlistId": PID, "chunkIndex": 0, "items": [item()]}, self.owner)
        self.assertEqual(self.playlists.uploads[PID]["items"], [])
        self.assertNotIn(PID, self.playlists.sessions)

    async def test_commit_rejects_missing_parts_in_a_video_even_when_total_leaf_count_matches(self):
        payload = registration(chunked=True)
        payload.pop("items")
        await self.playlists.register(payload, self.owner)
        await self.playlists.append_items({"playlistId": PID, "chunkIndex": 0,
            "items": [item(partCount=3), item(2003, 3, partCount=3)]}, self.owner)
        with self.assertRaisesRegex(ValueError, "parts are incomplete"):
            await self.playlists.commit_upload({"playlistId": PID, "expectedItemCount": 2}, self.owner)
        self.assertNotIn(PID, self.playlists.sessions)
        self.assertIn(PID, self.playlists.uploads)

    async def test_upload_rebind_expiry_and_stop_do_not_create_partial_native_playlist(self):
        payload = registration(chunked=True)
        payload.pop("items")
        await self.playlists.register(payload, self.owner)
        await self.playlists.append_items({"playlistId": PID, "chunkIndex": 0, "items": [item()]}, self.owner)
        self.playlists.detach_owner(self.owner)
        new = Owner()
        resumed = await self.playlists.register(payload, new)
        self.assertEqual(resumed["nextChunkIndex"], 1)
        self.assertNotIn(PID, self.playlists.sessions)
        with self.assertRaises(LookupError):
            self.playlists.m3u(PID)
        await self.playlists.stop({"playlistId": PID}, self.owner)
        self.assertIn(PID, self.playlists.uploads)
        await self.playlists.stop({"playlistId": PID}, new)
        self.assertNotIn(PID, self.playlists.uploads)
        await self.playlists.register(payload, new)
        self.playlists.uploads[PID]["lastSeen"] = time.monotonic() - 301
        self.playlists.sweep_uploads()
        self.assertNotIn(PID, self.playlists.uploads)

    async def test_hierarchy_metadata_batches_fit_32k_wire_and_duplicate_ack_retry_is_idempotent(self):
        self.ipc.manifest_api = True
        self.ipc.manifest_drop_once = True
        entries = [item(cid=number, page=1, title='\"\\' * 150, partCount=1, videoId=f"video-{number}",
            videoTitle="单P投稿", partTitle="单P", order=number - 1) for number in range(1, 702)]
        await self.chunked_register(entries, collectionId="large-season", collectionTitle="大合集")
        web = self.playlists.sessions[PID]
        web.manifest_ack_timeout = .01
        await eventually(lambda: web.manifest_stage == "ready")
        self.assertEqual(len(self.ipc.manifest_items), 701)
        self.assertEqual(len({entry["itemId"] for entry in self.ipc.manifest_items}), 701)
        self.assertTrue(all(entry["partCount"] == 1 for entry in self.ipc.manifest_items))
        commands = [command for command in self.ipc.commands if command[0] == "script-message-to" and command[2].startswith("web-playlist-manifest-")]
        for command in commands:
            wire = json.dumps({"command": command, "request_id": 1}, ensure_ascii=False).encode()
            self.assertLessEqual(len(wire), 32768)
        chunks = [json.loads(command[3]) for command in commands if command[2].endswith("-chunk")]
        self.assertGreater(len(chunks), len(self.ipc.manifest_seen))
        self.assertEqual(self.playlists.snapshots()[0]["manifest"]["sentItemCount"], 701)
        self.assertTrue(all("duration" in entry for entry in self.ipc.manifest_items))

    async def test_manifest_ack_wait_does_not_block_first_media_resolution(self):
        self.ipc.manifest_api = True
        gate = asyncio.Event()
        original = WebPlaylistSession.manifest_exchange
        async def wait_for_manifest(session, *args):
            await gate.wait()
            return await original(session, *args)
        self.ipc.props[REQUEST_PROPERTY] = lazy_request()
        with patch.object(WebPlaylistSession, "manifest_exchange", wait_for_manifest):
            await self.register()
            web = self.playlists.sessions[PID]
            await eventually(lambda: web.manifest_stage == "begin")
            self.assertEqual(self.owner.messages[-1]["type"], "resolveWebPlaylistItem")
            await self.playlists.submit(submission(), self.owner)
            self.assertIsNone(web.pending)
            self.assertEqual(web.phase, "player-progress")
            gate.set()
            await eventually(lambda: web.manifest_stage == "ready")

    async def test_chunk_protocol_routes_over_http_transport_and_websocket_with_paged_owner_checks(self):
        payload = registration(chunked=True)
        payload.pop("items")
        result, owner, _ = await self.transport.request({"clientId": CLIENT, "request": {"type": "registerWebPlaylist", **payload}})
        self.assertEqual(result["reply"]["type"], "webPlaylistUploadStarted")
        await self.transport.request({"clientId": CLIENT, "request": {"type": "appendWebPlaylistItems", "playlistId": PID,
            "chunkIndex": 0, "items": [item(), item(2003, 2)]}})
        result, _, _ = await self.transport.request({"clientId": CLIENT, "request": {"type": "commitWebPlaylist", "playlistId": PID, "expectedItemCount": 2}})
        self.assertNotIn("items", result["reply"])
        result, _, _ = await self.transport.request({"clientId": CLIENT, "request": {"type": "getWebPlaylistItems", "playlistId": PID, "offset": 1, "limit": 1}})
        self.assertEqual(result["reply"]["items"][0]["partCount"], 2)
        with self.assertRaises(ValueError):
            await self.playlists.get_items({"playlistId": PID}, Owner())
        with self.assertRaises(ValueError):
            await self.playlists.get_items({"playlistId": PID, "offset": True}, owner)
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        state.playlists, state.progress = self.playlists, self.progress
        await state.handle_ws_message(json.dumps({"type": "getWebPlaylistItems", "requestId": "page-route", "playlistId": PID}), owner)
        queued = owner.drain()
        self.assertEqual(queued[-1]["type"], "webPlaylistItems")

    async def prepare_resolution(self, owner=None):
        await self.register(owner)
        session = self.playlists.sessions[PID]
        await session.accept_request(lazy_request())
        return session

    async def test_registry_m3u_preserves_order_titles_selected_index_and_never_contains_media(self):
        reply = await self.register(selectedItemId="1001:2003")
        self.assertEqual(reply["playlistUrl"], f"http://127.0.0.1:9002/web-playlist/{PID}/list.m3u")
        self.assertEqual(reply["selectedIndex"], 1)
        playlist = self.playlists.m3u(PID).decode()
        self.assertEqual(playlist.count("#EXTINF:"), 2)
        self.assertLess(playlist.index("/item/1001:2002"), playlist.index("/item/1001:2003"))
        self.assertNotIn("deadline=", playlist)
        self.assertNotIn("Cookie", playlist)
        self.assertFalse(self.store.path.exists())

    async def test_register_is_idempotent_but_cannot_reuse_another_owner_or_pipe(self):
        await self.register()
        first = self.playlists.sessions[PID]
        await self.register()
        self.assertIs(self.playlists.sessions[PID], first)
        for owner, updates in ((Owner(), {}), (self.owner, {"selectedItemId": "1001:2003"}),
                               (self.owner, {"playlistId": "playlist-" + "b" * 32})):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                await self.playlists.register(registration(**updates), owner)

    async def test_registration_registry_is_bounded_to_eight_player_sessions(self):
        for number in range(8):
            session_id = f"progress-list-{number}"
            await self.register(playlistId="playlist-" + f"{number:032x}", sessionId=session_id, pipeName="mpv-lazy-" + session_id)
        with self.assertRaises(ValueError):
            await self.register(playlistId="playlist-" + "f" * 32, sessionId="progress-ninth", pipeName="mpv-lazy-progress-ninth")
        self.assertEqual(len(self.playlists.sessions), 8)

    async def test_lazy_request_emits_once_and_submit_delivers_matching_trusted_metadata(self):
        session = await self.prepare_resolution()
        requests = [message for message in self.owner.messages if message["type"] == "resolveWebPlaylistItem"]
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["first"])
        self.assertEqual(requests[0]["initialPosition"], 18.5)
        await session.accept_request(lazy_request())
        self.assertEqual(len(self.owner.messages), 1)
        reply = await self.playlists.submit(submission(), self.owner)
        self.assertEqual(reply["type"], "webPlaylistItemSubmitted")
        command = [command for command in self.ipc.commands if command[0] == "script-message-to"][-1]
        self.assertEqual(command[:3], ["script-message-to", "web_playlist", "web-playlist-resolved"])
        envelope = json.loads(command[3])
        self.assertEqual(envelope["media"]["identity"], "bilibili:1001:2002")
        self.assertEqual(envelope["media"]["pageUrl"], "https://www.bilibili.com/video/BV1fixture?p=1")
        self.assertEqual(envelope["media"]["video"], SOURCE)
        await session.accept_request(lazy_request())
        self.assertEqual(len([message for message in self.owner.messages if message["type"] == "resolveWebPlaylistItem"]), 1)

    async def test_fast_switch_ignores_stale_generation_and_other_owner(self):
        session = await self.prepare_resolution()
        await session.accept_request(lazy_request("1001:2003", 2))
        with self.assertRaises(ValueError):
            await self.playlists.submit(submission(), self.owner)
        with self.assertRaises(ValueError):
            await self.playlists.submit(submission("1001:2003", 2), Owner())
        await self.playlists.submit(submission("1001:2003", 2), self.owner)
        envelope = json.loads([command for command in self.ipc.commands if command[0] == "script-message-to"][-1][3])
        self.assertEqual(envelope["media"]["cid"], "2003")
        self.assertEqual(envelope["media"]["p"], 2)
        self.assertFalse(self.owner.messages[-1]["first"])

    async def test_resolution_submit_cannot_erase_a_new_request_waiting_for_ipc(self):
        session = await self.prepare_resolution()
        gate, entered = asyncio.Event(), asyncio.Event()
        original = self.ipc.command
        async def blocked(command):
            if command[0] == "script-message-to":
                entered.set()
                await gate.wait()
            return await original(command)
        self.ipc.command = blocked
        submit = asyncio.create_task(self.playlists.submit(submission(), self.owner))
        await entered.wait()
        next_request = asyncio.create_task(session.accept_request(lazy_request("1001:2003", 2)))
        await asyncio.sleep(.005)
        gate.set()
        await submit
        await next_request
        self.assertEqual(session.pending["generation"], 2)
        self.assertEqual(session.pending["itemId"], "1001:2003")

    async def test_timeout_and_fixed_browser_error_release_matching_hook(self):
        session = await self.prepare_resolution()
        session.pending["deadline"] = time.monotonic() - 1
        await session.accept_request(lazy_request())
        envelope = json.loads([command for command in self.ipc.commands if command[0] == "script-message-to"][-1][3])
        self.assertEqual(envelope["error"], "playlist_resolve_timeout")
        self.assertIsNone(session.pending)
        await session.accept_request(lazy_request("1001:2003", 2))
        with self.assertRaises(ValueError):
            await self.playlists.submit(submission("1001:2003", 2, error="private URL failed"), self.owner)
        await self.playlists.submit(submission("1001:2003", 2, error="playlist_media_unavailable"), self.owner)
        self.assertIn("playlist_media_unavailable", self.ipc.commands[-1][-1])

    async def test_detach_rebind_preserves_registry_and_renotifies_resolution(self):
        session = await self.prepare_resolution()
        self.playlists.detach_owner(self.owner)
        self.assertIsNone(session.owner)
        self.assertIn(PID, self.playlists.sessions)
        new = Owner()
        reply = await self.playlists.get({"identity": "bilibili:1001:2003"}, new)
        self.assertEqual(reply["playlistId"], PID)
        await session.accept_request(lazy_request())
        self.assertEqual(new.messages[-1]["type"], "resolveWebPlaylistItem")
        self.assertFalse(session.task.done())

    async def test_explicit_rebind_notifies_old_owner_and_transfers_progress_owner(self):
        await self.register()
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot()
        await self.progress.start(progress_request(), self.owner)
        await eventually(lambda: self.progress.sessions[SID].attached)
        new = Owner()
        absent = await self.playlists.get({"identity": "bilibili:1001:2003"}, new)
        self.assertIsNone(absent["playlistId"])
        rebound = await self.playlists.get({"identity": "bilibili:1001:2003", "rebind": True}, new)
        self.assertEqual(rebound["sessionId"], SID)
        self.assertIs(self.progress.sessions[SID].owner, new)
        self.assertIs(self.playlists.sessions[PID].owner, new)
        notices = [message for message in self.owner.messages if message.get("reason") == "browser_rebound"]
        self.assertEqual({message["type"] for message in notices}, {"webPlaylistStopped", "playbackProgressStopped"})
        self.assertIn(PID, self.playlists.sessions)

    async def test_stop_requires_owner_and_releases_pending_hook_without_killing_player(self):
        session = await self.prepare_resolution()
        await self.playlists.stop({"playlistId": PID}, Owner())
        self.assertIn(PID, self.playlists.sessions)
        await self.playlists.stop({"playlistId": PID}, self.owner)
        self.assertNotIn(PID, self.playlists.sessions)
        self.assertTrue(session.task.done())
        self.assertTrue(any(command[0] == "script-message-to" and "playlist_stopped" in command[-1] for command in self.ipc.commands))
        self.assertFalse(any(command[0] == "quit" for command in self.ipc.commands))

    async def test_player_close_cleans_playlist_registry_and_progress_watcher(self):
        await self.register()
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot()
        await self.progress.start(progress_request(), self.owner)
        await eventually(lambda: self.progress.sessions[SID].attached)
        self.ipc.closed = True
        await eventually(lambda: PID not in self.playlists.sessions and SID not in self.progress.sessions)
        self.assertTrue(any(message["type"] == "webPlaylistStopped" and message["reason"] == "player_closed" for message in self.owner.messages))

    async def test_removed_request_property_after_load_does_not_look_like_a_closed_player(self):
        session = await self.prepare_resolution()
        await self.playlists.submit(submission(), self.owner)
        self.ipc.props.pop(REQUEST_PROPERTY, None)
        await asyncio.sleep(.03)
        self.assertIn(PID, self.playlists.sessions)
        self.assertFalse(session.task.done())
        self.assertTrue(any(command == ["get_property", DATA_PROPERTY] for command in self.ipc.commands))

    async def test_live_playlist_and_progress_survive_busy_pipe_timeout_and_unavailable_property(self):
        await self.register()
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot()
        await self.progress.start(progress_request(), self.owner)
        web = self.playlists.sessions[PID]
        progress = self.progress.sessions[SID]
        await eventually(lambda: web.attached and progress.attached)
        web.ipc_availability.retry_timeout = progress.ipc_availability.retry_timeout = .01
        with self.assertLogs(level="INFO") as captured:
            for name in (DATA_PROPERTY, SNAPSHOT_PROPERTY):
                self.ipc.errors[name] = [TimeoutError("signed-secret-url must not be logged"),
                    OSError(16, "fixture busy pipe with private token"),
                    RuntimeError("MPV IPC command failed: property unavailable signed-secret-url")]
            await eventually(lambda: all(not errors for errors in self.ipc.errors.values()) and
                             web.ipc_availability.failures == progress.ipc_availability.failures == 0)
        self.assertIn(PID, self.playlists.sessions)
        self.assertFalse(web.task.done() or progress.task.done())
        self.assertFalse(any(message["type"] in ("webPlaylistStopped", "playbackProgressStopped") for message in self.owner.messages))
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot(position=45)
        await eventually(lambda: self.store.get("bilibili:1001:2002")["position"] == 45)
        await web.accept_request(lazy_request("1001:2003", 1))
        self.assertEqual(self.owner.messages[-1]["type"], "resolveWebPlaylistItem")
        self.assertNotIn("signed-secret-url", "\n".join(captured.output))
        self.assertNotIn("private token", "\n".join(captured.output))

    async def test_script_property_can_be_unavailable_past_retry_window_while_core_pid_is_alive(self):
        await self.register()
        web = self.playlists.sessions[PID]
        await eventually(lambda: web.attached)
        web.ipc_availability.retry_timeout = .02
        self.ipc.errors[DATA_PROPERTY] = [RuntimeError("MPV IPC command failed: property not found") for _ in range(30)]
        await eventually(lambda: web.ipc_availability.failures >= 6)
        self.assertFalse(web.task.done())
        self.assertIn(PID, self.playlists.sessions)
        diagnostic = self.playlists.snapshots()[0]
        self.assertEqual(diagnostic["ipc"]["lastError"]["kind"], "property_unavailable")
        self.assertEqual(diagnostic["reason"], "ipc_retry_property_unavailable")
        self.ipc.errors[DATA_PROPERTY].clear()
        await eventually(lambda: web.ipc_availability.failures == 0)
        self.assertEqual(web.reason, "")

    async def test_pending_resolution_expires_even_when_script_property_is_unavailable_and_pid_alive(self):
        web = await self.prepare_resolution()
        await eventually(lambda: web.attached)
        self.ipc.errors[DATA_PROPERTY] = [RuntimeError("MPV IPC command failed: property not found") for _ in range(30)]
        web.pending["deadline"] = time.monotonic() - 1
        await eventually(lambda: web.pending is None)
        replies = [json.loads(command[3]) for command in self.ipc.commands if command[0] == "script-message-to"]
        self.assertEqual(replies[-1]["error"], "playlist_resolve_timeout")
        self.assertFalse(web.task.done())
        self.assertIn(PID, self.playlists.sessions)

    async def test_failed_timeout_reply_keeps_live_registry_and_relies_on_lua_hook_deadline(self):
        web = await self.prepare_resolution()
        original = self.ipc.command
        async def reply_unavailable(command):
            if command[0] == "script-message-to":
                raise TimeoutError("fixture private media URL")
            return await original(command)
        self.ipc.command = reply_unavailable
        web.pending["deadline"] = time.monotonic() - 1
        with self.assertLogs("web-playlist", level="WARNING") as captured:
            await web.accept_request(None)
        self.assertIsNone(web.pending)
        self.assertIn(PID, self.playlists.sessions)
        self.assertFalse(web.task.done())
        self.assertNotIn("private media", "\n".join(captured.output))

    async def test_unresponsive_pipe_and_core_probe_exhaust_bounded_retry_without_false_completion(self):
        await self.register()
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot()
        await self.progress.start(progress_request(), self.owner)
        web, progress = self.playlists.sessions[PID], self.progress.sessions[SID]
        await eventually(lambda: web.attached and progress.attached)
        web.ipc_availability.retry_timeout = progress.ipc_availability.retry_timeout = .03
        original = self.ipc.command
        async def unresponsive(command):
            if command[0] == "get_property":
                raise TimeoutError("private fixture media URL must not be logged")
            return await original(command)
        self.ipc.command = unresponsive
        with self.assertLogs(level="WARNING") as captured:
            await eventually(lambda: PID not in self.playlists.sessions and SID not in self.progress.sessions)
        notices = [message for message in self.owner.messages if message["type"] == "webPlaylistStopped"]
        self.assertEqual(notices[-1]["reason"], "player_ipc_unresponsive")
        self.assertEqual(notices[-1]["ipcError"]["kind"], "response_timeout")
        record = self.store.get("bilibili:1001:2002")
        self.assertFalse(record["completed"] or record["ended"])
        self.assertEqual(record["position"], 18.5)
        self.assertNotIn("private fixture", "\n".join(captured.output))

    async def test_missing_named_pipe_still_closes_session_immediately_with_safe_errno(self):
        await self.register()
        web = self.playlists.sessions[PID]
        await eventually(lambda: web.attached)
        self.ipc.closed = True
        await eventually(lambda: PID not in self.playlists.sessions)
        notice = [message for message in self.owner.messages if message["type"] == "webPlaylistStopped"][-1]
        self.assertEqual(notice["reason"], "player_closed")
        self.assertEqual(notice["ipcError"], {"kind": "pipe_missing", "errno": 2, "winerror": None})
        self.assertFalse(any(command == ["get_property", "pid"] for command in self.ipc.commands))

    async def test_submit_diagnostics_distinguish_received_rejected_and_accepted_without_sensitive_payloads(self):
        session = await self.prepare_resolution()
        with self.assertLogs("web-playlist", level="INFO") as captured:
            bad = submission(media={"video": "http://127.0.0.1/private?Cookie=secret-token", "position": 0})
            with self.assertRaises(ValueError):
                await self.playlists.submit(bad, self.owner)
            await self.playlists.submit(submission(), self.owner)
        diagnostic = self.playlists.snapshots()[0]["submissions"]
        self.assertEqual((diagnostic["received"], diagnostic["accepted"], diagnostic["rejected"]), (2, 1, 1))
        self.assertEqual(diagnostic["lastError"], {"kind": "validation_error", "code": "media_host_unsupported"})
        logged = "\n".join(captured.output)
        self.assertIn("submit received", logged)
        self.assertIn("submit rejected", logged)
        self.assertIn("submit accepted", logged)
        self.assertNotIn("Cookie", logged)
        self.assertNotIn("secret-token", logged)
        self.assertIsNone(session.pending)

    async def test_untrusted_manifest_in_progress_request_is_never_accepted(self):
        with self.assertRaises(ValueError):
            await self.progress.start(progress_request(_playlistEntries={"bilibili:1001:2002": item()}), self.owner)
        await self.register()
        for update in ({"identity": "bilibili:1001:9999"}, {"pipeName": "mpv-lazy-progress-other"},
                       {"sessionId": "progress-other", "pipeName": "mpv-lazy-progress-other"}, {"playlistId": "playlist-" + "c" * 32}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                await self.progress.start(progress_request(**update), self.owner)
        with self.assertRaises(ValueError):
            await self.progress.start(progress_request(), Owner())

    async def test_dynamic_progress_and_eof_are_independent_for_each_item(self):
        await self.register()
        await self.progress.start(progress_request(), self.owner, defer=True)
        session = self.progress.sessions[SID]
        await session.accept_snapshot(snapshot(position=31))
        self.assertEqual(await session.accept_snapshot(snapshot(position=120, ended=True, completed=True)), "ok")
        self.assertEqual(await session.accept_snapshot(snapshot("bilibili:1001:2003", position=7)), "ok")
        first, second = self.store.get("bilibili:1001:2002"), self.store.get("bilibili:1001:2003")
        self.assertTrue(first["completed"])
        self.assertEqual(first["position"], 120)
        self.assertEqual(second["position"], 7)
        self.assertEqual(second["pageUrl"], "https://www.bilibili.com/video/BV1fixture?p=2")
        self.assertEqual(self.owner.messages[-1]["playlistId"], PID)
        self.assertEqual(self.owner.messages[-1]["identity"], "bilibili:1001:2003")
        state = await self.progress.get(progress_request(), self.owner)
        self.assertEqual(state["activeSessionId"], SID)
        self.assertEqual(state["playlist"]["items"][1]["cid"], "2003")
        self.assertEqual(await session.accept_snapshot(snapshot("bilibili:1001:9999", position=90)), "changed")
        self.assertIsNone(self.store.get("bilibili:1001:9999"))

    async def test_final_snapshots_are_ordered_once_and_saved_before_live_current_item(self):
        await self.register()
        await self.progress.start(progress_request(), self.owner, defer=True)
        session = self.progress.sessions[SID]
        finals = [{"finalSequence": 2, "snapshot": snapshot("bilibili:1001:2003", position=49, ended=True)},
                  {"finalSequence": 1, "snapshot": snapshot(position=120, ended=True, completed=True)}]
        await session.accept_finals(finals)
        self.assertEqual(session.final_sequence, 2)
        self.assertEqual(self.store.get("bilibili:1001:2002")["position"], 120)
        self.assertEqual(self.store.get("bilibili:1001:2003")["position"], 49)
        count = len(self.owner.messages)
        await session.accept_finals(finals)
        self.assertEqual(len(self.owner.messages), count)
        await session.accept_snapshot(snapshot("bilibili:1001:2003", position=0, paused=False))
        self.assertEqual(self.store.get("bilibili:1001:2003")["position"], 0)
        for bad in ([{"finalSequence": 3, "snapshot": snapshot("bilibili:1001:9999")}],
                    [{"finalSequence": True, "snapshot": snapshot()}], finals * 17):
            await session.accept_finals(bad)
        self.assertEqual(session.final_sequence, 2)

    async def test_playlist_run_reads_finals_then_current_and_keeps_watching_after_eof(self):
        await self.register()
        self.ipc.props[FINALS_PROPERTY] = [{"finalSequence": 1, "snapshot": snapshot(position=120, ended=True, completed=True)}]
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot("bilibili:1001:2003", position=3)
        await self.progress.start(progress_request(), self.owner)
        await eventually(lambda: self.store.get("bilibili:1001:2003") is not None)
        session = self.progress.sessions[SID]
        self.assertFalse(session.task.done())
        self.assertTrue(self.store.get("bilibili:1001:2002")["completed"])
        # Only manifest metadata and positions are persisted, never stream URLs.
        await asyncio.to_thread(self.store.flush)
        persisted = self.store.path.read_text()
        self.assertNotIn("deadline=", persisted)
        self.assertNotIn("audio-private", persisted)
        self.assertNotIn(PID, persisted)

    async def test_loop_reload_consumes_eof_final_before_resolver_reads_resume_record(self):
        await self.register()
        await self.progress.start(progress_request(position=119.8), self.owner, defer=True)
        self.ipc.props[FINALS_PROPERTY] = [{"finalSequence": 1, "snapshot": snapshot(position=120, ended=True, completed=True)}]
        self.ipc.props[SNAPSHOT_PROPERTY] = snapshot(ready=False, position=0, duration=0)
        await self.playlists.sessions[PID].accept_request(lazy_request())
        resolve = [message for message in self.owner.messages if message["type"] == "resolveWebPlaylistItem"][-1]
        self.assertTrue(resolve["resumeRecord"]["completed"])
        self.assertEqual(resolve["resumeRecord"]["position"], 120)
        self.assertEqual(self.progress.sessions[SID].final_sequence, 1)

    async def test_http_events_keep_distinct_cids_and_rebind_resolution_queue(self):
        result, owner, _ = await self.transport.request({"clientId": CLIENT, "request": {"type": "registerWebPlaylist", **registration()}})
        self.assertEqual(result["reply"]["type"], "webPlaylistRegistered")
        await self.progress.start(progress_request(), owner, defer=True)
        session = self.progress.sessions[SID]
        await session.accept_snapshot(snapshot(position=19))
        await session.accept_snapshot(snapshot("bilibili:1001:2003", position=2))
        events = (await self.transport.request({"clientId": CLIENT, "request": {"type": "pollPlaybackProgress"}}))[0]["events"]
        self.assertEqual({event["identity"] for event in events if event["type"] == "playbackProgress"}, {"bilibili:1001:2002", "bilibili:1001:2003"})
        result, new, _ = await self.transport.request({"clientId": OTHER_CLIENT,
            "request": {"type": "getWebPlaylist", "identity": "bilibili:1001:2003", "rebind": True}})
        self.assertEqual(result["reply"]["playlistId"], PID)
        self.assertIs(self.playlists.sessions[PID].owner, new)
        await self.playlists.sessions[PID].accept_request(lazy_request("1001:2003", 1))
        events = (await self.transport.request({"clientId": OTHER_CLIENT, "request": {"type": "pollPlaybackProgress"}}))[0]["events"]
        self.assertEqual(events[-1]["type"], "resolveWebPlaylistItem")

    async def test_http_detach_and_expiry_detach_both_owners(self):
        _, owner, _ = await self.transport.request({"clientId": CLIENT, "request": {"type": "registerWebPlaylist", **registration()}})
        await self.progress.start(progress_request(), owner, defer=True)
        await self.transport.request({"clientId": CLIENT, "request": {"type": "detachPlaybackProgress"}})
        self.assertIsNone(self.playlists.sessions[PID].owner)
        self.assertIsNone(self.progress.sessions[SID].owner)
        _, new, _ = await self.transport.request({"clientId": OTHER_CLIENT,
            "request": {"type": "getWebPlaylist", "identity": "bilibili:1001:2003", "rebind": True}})
        new.last_seen = time.monotonic() - 121
        self.transport.sweep()
        self.assertIsNone(self.playlists.sessions[PID].owner)
        self.assertIsNone(self.progress.sessions[SID].owner)

    async def test_real_http_registration_m3u_health_and_no_cors(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        state.progress, state.playlists, state.progress_http = self.progress, self.playlists, self.transport
        server = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            self.playlists.http_port = port
            async def http(method, path, payload=None):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                body = json.dumps(payload).encode() if payload is not None else b""
                writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nOrigin: https://www.bilibili.com\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
                raw = await reader.read()
                writer.close()
                await writer.wait_closed()
                return raw.partition(b"\r\n\r\n")[::2]
            headers, body = await http("GET", "/health")
            self.assertEqual(json.loads(body)["webPlaylistApiVersion"], 1)
            headers, body = await http("POST", "/playback-progress", {"clientId": CLIENT, "request": {"type": "registerWebPlaylist", **registration()}})
            self.assertIn(b"200 OK", headers)
            self.assertNotIn(b"Access-Control-Allow-Origin", headers)
            playlist_path = json.loads(body)["reply"]["playlistUrl"].split(str(port), 1)[1]
            headers, body = await http("GET", playlist_path)
            self.assertIn(b"200 OK", headers)
            self.assertNotIn(b"Access-Control-Allow-Origin", headers)
            self.assertEqual(body.count(b"#EXTINF:"), 2)
            headers, body = await http("HEAD", playlist_path)
            self.assertIn(b"200 OK", headers)
            self.assertEqual(body, b"")
            headers, _ = await http("GET", playlist_path.replace("list.m3u", "item/1001:2002"))
            self.assertIn(b"410 Gone", headers)
            headers, _ = await http("GET", f"/web-playlist/playlist-{'f' * 32}/list.m3u")
            self.assertIn(b"404 Not Found", headers)
            headers, body = await http("GET", "/status")
            self.assertEqual(json.loads(body)["webPlaylists"][0]["itemCount"], 2)
            self.assertNotIn(b"deadline=", body)
        finally:
            server.close()
            await server.wait_closed()

    async def test_real_http_accepts_500_item_manifest_larger_than_legacy_64k_limit(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        state.progress, state.playlists, state.progress_http = self.progress, self.playlists, self.transport
        server = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), "127.0.0.1", 0)
        try:
            entries = [item(cid=number, page=number) for number in range(1, 501)]
            payload = {"clientId": CLIENT, "request": {"type": "registerWebPlaylist", **registration(items=entries, selectedItemId="1001:1")}}
            body = json.dumps(payload).encode()
            self.assertGreater(len(body), 65536)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(f"POST /playback-progress HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            headers, _, body = response.partition(b"\r\n\r\n")
            self.assertIn(b"200 OK", headers)
            self.assertEqual(len(json.loads(body)["reply"]["items"]), 500)
        finally:
            server.close()
            await server.wait_closed()

    async def test_websocket_playlist_dispatch_uses_same_registry(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        state.progress, state.playlists = self.progress, self.playlists
        await state.handle_ws_message(json.dumps({"type": "registerWebPlaylist", **registration()}), self.owner)
        self.assertEqual(self.owner.messages[-1]["type"], "webPlaylistRegistered")
        await state.handle_ws_message(json.dumps({"type": "getWebPlaylist", "identity": "bilibili:1001:2003"}), self.owner)
        self.assertEqual(self.owner.messages[-1]["playlistId"], PID)
        await state.handle_ws_message(json.dumps({"type": "stopWebPlaylist", "playlistId": PID}), self.owner)
        self.assertEqual(self.owner.messages[-1]["type"], "webPlaylistStopped")
        self.assertNotIn(PID, self.playlists.sessions)


if __name__ == "__main__":
    unittest.main()
