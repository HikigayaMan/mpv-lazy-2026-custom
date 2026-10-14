"""Progress protocol regression tests with temporary files and fake MPV IPC."""

import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import BridgeState, handle_http_client, ws_handler
from playback_progress import (
    API_VERSION, FINALS_PROPERTY, SNAPSHOT_PROPERTY, PlaybackProgressManager, PlaybackProgressSession,
    ProgressStore, canonical_page_url, default_store_path, make_record,
)


def request(**updates):
    payload = {
        "requestId": "request-1", "provider": "bilibili", "identity": "bilibili:1001:2002",
        "pageUrl": "https://www.bilibili.com/video/BV1fixture?p=2&token=must-not-persist#progress",
        "sessionId": "progress-fixture", "pipeName": "mpv-lazy-progress-fixture",
        "position": 25, "duration": 120, "paused": False, "updatedAt": 1700000000000,
    }
    payload.update(updates)
    return payload


def snapshot(**updates):
    payload = {"apiVersion": 1, "sessionId": "progress-fixture", "identity": "bilibili:1001:2002",
               "active": True, "ready": True, "position": 25, "duration": 120,
               "paused": False, "ended": False, "completed": False}
    payload.update(updates)
    return payload


class Owner:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


class Ipc:
    def __init__(self):
        self.value = snapshot()
        self.commands = []
        self.closed = False
        self.errors = {}
        self.finals = []

    async def command(self, command):
        self.commands.append(command)
        if self.closed:
            raise FileNotFoundError(2, "fixture named pipe missing")
        if self.errors.get(command[1]):
            raise self.errors[command[1]].pop(0)
        if command == ["get_property", "pid"]:
            return 12345
        if command == ["get_property", FINALS_PROPERTY]:
            return copy.deepcopy(self.finals)
        return copy.deepcopy(self.value)


async def eventually(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("progress fixture timed out")
        await asyncio.sleep(0.002)


class StoreTests(unittest.TestCase):
    def test_restart_retains_exact_backward_seek_and_never_saves_signed_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "playback-progress.json"
            store = ProgressStore(path)
            store.put(make_record(request(position=80), "web"))
            store.put(make_record(request(position=10, updatedAt=1700000001000), "web"))
            store.flush()
            restored = ProgressStore(path).get("bilibili:1001:2002")
            self.assertEqual(restored["position"], 10)
            self.assertEqual(restored["pageUrl"], "https://www.bilibili.com/video/BV1fixture?p=2")
            self.assertNotIn("must-not-persist", path.read_text())
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_stale_web_record_does_not_replace_newer_mpv_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProgressStore(Path(directory) / "resume.json")
            recent = make_record(request(position=55, updatedAt=1700000002000), "mpv")
            store.put(recent)
            self.assertEqual(store.put(make_record(request(position=0), "web")), recent)
            self.assertEqual(store.get(recent["identity"])["position"], 55)

    def test_corrupt_file_is_not_rewritten_on_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.json"
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(ProgressStore(path).get("bilibili:1001:2002"))
            self.assertEqual(path.read_text(), "{broken")

    def test_failed_atomic_replace_keeps_original_record_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "resume.json"
            store = ProgressStore(path)
            store.put(make_record(request(position=50), "web"))
            store.flush()
            store.put(make_record(request(position=60, updatedAt=1700000001000), "web"))
            with patch("playback_progress.os.replace", side_effect=OSError("fixture write rejected")):
                with self.assertRaises(OSError):
                    store.flush()
            self.assertEqual(ProgressStore(path).get("bilibili:1001:2002")["position"], 50)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
            self.assertTrue(store.dirty)

    def test_record_limit_keeps_most_recent_content(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProgressStore(Path(directory) / "resume.json", max_records=2)
            for cid in range(1, 4):
                store.put(make_record(request(identity=f"bilibili:1001:{cid}", updatedAt=1700000000000 + cid), "web"))
            store.flush()
            self.assertIsNone(store.get("bilibili:1001:1"))
            self.assertEqual(len(ProgressStore(store.path).records), 2)

    def test_identity_url_and_times_reject_live_invalid_and_nonfinite_values(self):
        cases = [
            {"identity": "bilibili:1001:0"}, {"provider": "live"}, {"identity": "bilibili:room:1001"},
            {"pageUrl": "https://live.bilibili.com/1001"}, {"pageUrl": "https://www.bilibili.com.example.org/video/BV1fixture"},
            {"pageUrl": "https://account:secret@www.bilibili.com/video/BV1fixture"},
            {"pageUrl": "file:///video/BV1fixture"}, {"position": float("nan")}, {"duration": float("inf")},
            {"position": -1}, {"position": True}, {"paused": "true"}, {"updatedAt": float("nan")},
        ]
        for update in cases:
            with self.subTest(update=update), self.assertRaises(ValueError):
                make_record(request(**update), "web")

    def test_web_record_strips_query_userinfo_and_fragment(self):
        value = request(provider="web", identity="web:" + "a" * 64,
                        pageUrl="https://video.example.org/watch?token=fixture&v=12#t=30")
        self.assertEqual(make_record(value, "web")["pageUrl"], "https://video.example.org/watch")
        self.assertEqual(canonical_page_url("bilibili", "https://www.bilibili.com/bangumi/play/ep123?from=secret"),
                         "https://www.bilibili.com/bangumi/play/ep123")
        with self.assertRaises(ValueError):
            make_record({**value, "pageUrl": "https://account:secret@video.example.org/watch"}, "web")

    def test_frozen_store_uses_executable_directory(self):
        with patch("playback_progress.sys.frozen", True, create=True), patch("playback_progress.sys.executable", "C:/fixture/bridge.exe"):
            self.assertEqual(default_store_path().as_posix(), "C:/fixture/playback-progress.json")


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ProgressStore(Path(self.temporary.name) / "resume.json")
        self.owner = Owner()
        self.ipc = Ipc()
        self.sessions = []

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.stop()
        self.temporary.cleanup()

    def session(self, **updates):
        self.store.put(make_record(request(), "web"))
        session = PlaybackProgressSession(request(), self.owner, self.store, ipc=self.ipc,
                                          poll_interval=0.005, flush_interval=0.01, attach_timeout=0.2, **updates)
        self.sessions.append(session)
        return session

    async def test_paused_unchanged_snapshots_keep_timestamp_and_sequence(self):
        session = self.session()
        self.ipc.value = snapshot(paused=True)
        await session.accept_snapshot(self.ipc.value)
        first = self.store.get(session.identity)
        await asyncio.sleep(0.005)
        await session.accept_snapshot(self.ipc.value)
        self.assertEqual(self.store.get(session.identity)["updatedAt"], first["updatedAt"])
        self.assertEqual(session.sequence, 1)
        self.assertEqual(len(self.owner.messages), 1)

    async def test_quick_pause_then_resume_is_consumed_before_current_playback_once(self):
        session = self.session()
        self.ipc.value = snapshot(position=27, pauseEventsApiVersion=1)
        self.ipc.finals = [{"finalSequence": 1, "snapshot": snapshot(position=25.5, paused=True)}]
        self.assertEqual(await session.sample_progress(), "ok")
        self.assertEqual([(item["position"], item["paused"]) for item in self.owner.messages],
                         [(25.5, True), (27, False)])
        self.assertEqual(self.ipc.commands, [["get_property", SNAPSHOT_PROPERTY], ["get_property", FINALS_PROPERTY]])
        self.assertEqual(session.final_sequence, 1)
        self.assertEqual(self.store.get(session.identity)["position"], 27)
        self.assertEqual(await session.sample_progress(), "ok")
        self.assertEqual(len(self.owner.messages), 2)

    async def test_legacy_snapshot_never_reads_absent_pause_queue(self):
        session = self.session()
        for version in (None, True, "1", 2):
            self.ipc.value = snapshot(**({} if version is None else {"pauseEventsApiVersion": version}))
            self.assertEqual(await session.sample_progress(), "ok")
        self.assertEqual(self.ipc.commands, [["get_property", SNAPSHOT_PROPERTY]] * 4)

    async def test_foreign_and_invalid_pause_finals_cannot_advance_watermark(self):
        session = self.session()
        await session.accept_snapshot(snapshot())
        for updates in ({"identity": "bilibili:1001:9999"}, {"sessionId": "progress-other"},
                        {"active": False}, {"ready": False}, {"paused": "true"}):
            await session.accept_finals([{"finalSequence": 9, "snapshot": snapshot(**{"paused": True, **updates})}])
            self.assertEqual(session.final_sequence, 0)
        await session.accept_finals([{"finalSequence": 1, "snapshot": snapshot(position=12, paused=True)}])
        self.assertEqual(session.final_sequence, 1)
        self.assertEqual(self.store.get(session.identity)["position"], 12)
        self.assertIsNone(self.store.get("bilibili:1001:9999"))

    async def test_pause_finals_accept_valid_sequence_order_and_reject_unbounded_queue(self):
        session = self.session()
        finals = [{"finalSequence": 2, "snapshot": snapshot(position=7, paused=True)},
                  {"finalSequence": 1, "snapshot": snapshot(position=12, paused=True)}]
        await session.accept_finals(finals)
        self.assertEqual([(item["position"], item["paused"]) for item in self.owner.messages],
                         [(12, True), (7, True)])
        self.assertEqual(session.final_sequence, 2)
        await session.accept_finals(finals)
        await session.accept_finals([{"finalSequence": True, "snapshot": snapshot(paused=True)}])
        await session.accept_finals(finals * 17)
        self.assertEqual(len(self.owner.messages), 2)
        self.assertEqual(session.final_sequence, 2)

    async def test_snapshot_identity_and_session_change_never_poll_other_player_position(self):
        session = self.session()
        session.start()
        await eventually(lambda: session.attached)
        self.ipc.value = snapshot(identity="bilibili:1001:9999", position=100)
        await eventually(lambda: session.task.done())
        self.assertEqual(session.reason, "player_file_changed")
        self.assertEqual(self.store.get(session.identity)["position"], 25)
        self.assertTrue(all(command == ["get_property", SNAPSHOT_PROPERTY] for command in self.ipc.commands))

    async def test_loading_snapshot_does_not_overwrite_or_emit_zero(self):
        session = self.session()
        await session.accept_snapshot(snapshot())
        await session.accept_snapshot(snapshot(ready=False, position=0, duration=0))
        self.assertEqual(self.store.get(session.identity)["position"], 25)
        self.assertEqual(session.sequence, 1)
        self.assertEqual(await session.accept_snapshot(snapshot(position=26)), "ok")
        self.assertEqual(self.store.get(session.identity)["position"], 26)

    async def test_seek_backward_pause_and_end_are_saved_and_notified(self):
        session = self.session()
        await session.accept_snapshot(snapshot(position=80))
        await session.accept_snapshot(snapshot(position=12, paused=True))
        self.assertEqual(ProgressStore(self.store.path).get(session.identity)["position"], 12)
        self.assertEqual(await session.accept_snapshot(snapshot(position=120, ended=True, completed=True)), "ended")
        final = self.owner.messages[-1]
        self.assertTrue(final["ended"])
        self.assertEqual(final["source"], "mpv")
        self.assertEqual(final["position"], 120)

    async def test_ipc_closure_flushes_last_record(self):
        session = self.session()
        session.start()
        await eventually(lambda: session.attached)
        self.ipc.value = snapshot(position=26)
        await eventually(lambda: self.store.get(session.identity)["position"] == 26)
        self.ipc.closed = True
        await eventually(lambda: session.task.done())
        self.assertEqual(ProgressStore(self.store.path).get(session.identity)["position"], 26)
        self.assertEqual(session.reason, "player_closed")

    async def test_eof_stops_with_playback_ended_instead_of_player_closed(self):
        for reason in (None, "eof", "stop", "player_closed"):
            with self.subTest(reason=reason):
                session = self.session()
                self.ipc.value = snapshot(position=120, ended=True, completed=True,
                                          **({} if reason is None else {"terminalReason": reason}))
                session.start()
                await eventually(lambda: session.task.done())
                self.assertEqual(session.reason, "playback_ended")
                self.assertEqual(self.owner.messages[-1]["reason"], "playback_ended")
                self.assertEqual(ProgressStore(self.store.path).get(session.identity)["position"], 120)

    async def test_verified_graceful_quit_and_shutdown_snapshot_report_player_closed(self):
        for reason in ("quit", "shutdown"):
            with self.subTest(reason=reason):
                session = self.session()
                self.ipc.value = snapshot(position=44.5, ended=True, terminalReason=reason)
                session.start()
                await eventually(lambda: session.task.done())
                self.assertEqual(session.reason, "player_closed")
                self.assertEqual(self.owner.messages[-1]["reason"], "player_closed")
                self.assertEqual(self.owner.messages[-2]["position"], 44.5)
                self.assertEqual(ProgressStore(self.store.path).get(session.identity)["position"], 44.5)
                self.assertNotIn("terminalReason", self.store.path.read_text())

    async def test_unattached_foreign_or_unready_shutdown_marker_cannot_report_player_closed(self):
        session = self.session()
        for updates in ({"identity": "bilibili:1001:9999"}, {"sessionId": "progress-other"},
                        {"active": False}, {"ready": False}, {"duration": 0}):
            with self.subTest(updates=updates):
                self.assertEqual(await session.accept_snapshot(snapshot(ended=True, terminalReason="shutdown", **updates)),
                                 "waiting")
        self.assertFalse(session.attached)
        self.assertEqual(self.owner.messages, [])

    async def test_shutdown_marker_without_ended_does_not_stop_live_playback(self):
        session = self.session()
        for reason in ("quit", "shutdown"):
            self.assertEqual(await session.accept_snapshot(snapshot(terminalReason=reason)), "ok")
        self.assertFalse(self.owner.messages[-1]["ended"])

    async def test_missing_lua_snapshot_times_out_without_false_progress(self):
        session = self.session()
        self.ipc.value = None
        session.start()
        await eventually(lambda: session.task.done())
        self.assertEqual(session.reason, "player_attach_timeout")
        self.assertFalse(any(message["type"] == "playbackProgress" for message in self.owner.messages))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ProgressStore(Path(self.temporary.name) / "resume.json")
        self.ipc = Ipc()
        def factory(*args):
            return PlaybackProgressSession(*args, ipc=self.ipc, poll_interval=0.005,
                                           attach_timeout=0.2, flush_interval=0.01)
        self.manager = PlaybackProgressManager(self.store, session_factory=factory)
        self.owner = Owner()

    async def asyncTearDown(self):
        await self.manager.close()
        self.temporary.cleanup()

    async def test_start_ack_is_immediate_and_validates_exact_session_pipe_pair(self):
        self.ipc.value = None
        ack = await self.manager.start(request(), self.owner)
        self.assertEqual(ack["type"], "playbackProgressStarted")
        self.assertEqual(ack["apiVersion"], API_VERSION)
        self.assertFalse(self.manager.sessions["progress-fixture"].attached)
        for updates in ({"sessionId": "bilibili-fixture"}, {"pipeName": "mpv-lazy-progress-other"},
                        {"pipeName": "mpv"}, {"sessionId": "progress-../other"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                await self.manager.start(request(**updates), self.owner)

    async def test_bridge_sends_ack_before_polling_under_websocket_backpressure(self):
        gate = asyncio.Event()
        sending = asyncio.Event()
        class SlowOwner(Owner):
            async def send(owner_self, raw):
                if json.loads(raw)["type"] == "playbackProgressStarted":
                    sending.set()
                    await gate.wait()
                await super().send(raw)
        owner = SlowOwner()
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        state.progress = self.manager
        task = asyncio.create_task(state.handle_ws_message(json.dumps({"type": "startPlaybackProgress", **request()}), owner))
        await asyncio.wait_for(sending.wait(), 1)
        await asyncio.sleep(0.01)
        self.assertEqual(self.ipc.commands, [])
        self.assertEqual(owner.messages, [])
        gate.set()
        await task
        await eventually(lambda: len(owner.messages) >= 2)
        self.assertEqual([message["type"] for message in owner.messages[:2]],
                         ["playbackProgressStarted", "playbackProgress"])

    async def test_stop_before_polling_still_acknowledges_and_removes_session(self):
        await self.manager.start(request(), self.owner, defer=True)
        await self.manager.stop("progress-fixture", self.owner)
        self.assertEqual(self.manager.sessions, {})
        self.assertEqual(self.owner.messages[-1]["type"], "playbackProgressStopped")
        self.assertEqual(self.owner.messages[-1]["reason"], "browser_stop")

    async def test_browser_disconnect_keeps_player_saving_and_new_page_can_subscribe(self):
        await self.manager.start(request(), self.owner)
        session = self.manager.sessions["progress-fixture"]
        await eventually(lambda: session.attached)
        self.manager.detach_owner(self.owner)
        self.ipc.value = snapshot(position=48)
        await eventually(lambda: self.store.get(session.identity)["position"] == 48)
        self.assertFalse(session.task.done())
        new_owner = Owner()
        state = await self.manager.get(request(), new_owner)
        self.assertEqual(state["activeSessionId"], "progress-fixture")
        self.assertIs(session.owner, new_owner)
        self.ipc.value = snapshot(position=49)
        await eventually(lambda: bool(new_owner.messages))
        self.assertEqual(new_owner.messages[-1]["position"], 49)

    async def test_get_cannot_steal_subscribed_session_and_stop_requires_owner(self):
        await self.manager.start(request(), self.owner)
        other = Owner()
        state = await self.manager.get(request(), other)
        self.assertNotIn("activeSessionId", state)
        self.assertIs(self.manager.sessions["progress-fixture"].owner, self.owner)
        await self.manager.stop("progress-fixture", other)
        self.assertIn("progress-fixture", self.manager.sessions)
        await self.manager.stop("progress-fixture", self.owner)
        self.assertEqual(self.manager.sessions, {})

    async def test_background_paused_webpage_cannot_overwrite_running_player(self):
        await self.manager.start(request(), self.owner)
        await eventually(lambda: self.manager.sessions["progress-fixture"].attached)
        ack = await self.manager.update(request(position=0, paused=True, updatedAt=time.time_ns() // 1_000_000), self.owner)
        self.assertEqual(ack["record"]["position"], 25)
        self.assertEqual(ack["record"]["source"], "mpv")

    async def test_explicit_web_takeover_is_written_after_player_finalizer(self):
        await self.manager.start(request(), self.owner)
        old_session = self.manager.sessions["progress-fixture"]
        await eventually(lambda: old_session.attached)
        ack = await self.manager.update(request(position=5, takeover=True, updatedAt=time.time_ns() // 1_000_000), self.owner)
        self.assertTrue(old_session.task.done())
        self.assertEqual(old_session.reason, "web_takeover")
        self.assertEqual(self.manager.sessions, {})
        self.assertEqual(ack["record"]["source"], "web")
        self.assertEqual(ProgressStore(self.store.path).get(old_session.identity)["position"], 5)

    async def test_new_player_replaces_only_same_content_session(self):
        await self.manager.start(request(), self.owner)
        old_session = self.manager.sessions["progress-fixture"]
        await self.manager.start(request(sessionId="progress-second", pipeName="mpv-lazy-progress-second"), self.owner)
        self.assertTrue(old_session.task.done())
        self.assertEqual(old_session.reason, "player_replaced")
        self.assertEqual(list(self.manager.sessions), ["progress-second"])

    async def test_bridge_routes_protocol_and_health_advertises_api(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        await state.handle_ws_message(json.dumps({"type": "updatePlaybackProgress", **request(position=35)}), self.owner)
        self.assertEqual(self.owner.messages[-1]["type"], "playbackProgressSaved")
        await state.handle_ws_message(json.dumps({"type": "getPlaybackProgress", **request()}), self.owner)
        self.assertEqual(self.owner.messages[-1]["record"]["position"], 35)
        server = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            raw = await reader.read()
            writer.close()
            await writer.wait_closed()
            health = json.loads(raw.split(b"\r\n\r\n", 1)[1])
            self.assertEqual(health["playbackProgressApiVersion"], 1)
            self.assertEqual(health["playbackProgressProviders"], ["bilibili", "web"])
        finally:
            server.close()
            await server.wait_closed()
            await state.progress.close()

    async def test_health_only_allows_credential_free_cross_origin_capability_checks(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 9002, progress_store=self.store)
        server = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            async def http(method, path):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nOrigin: https://www.bilibili.com\r\nAccess-Control-Request-Method: GET\r\nAccess-Control-Request-Private-Network: true\r\n\r\n".encode())
                await writer.drain()
                raw = await reader.read()
                writer.close()
                await writer.wait_closed()
                return raw.partition(b"\r\n\r\n")
            headers, _, body = await http("GET", "/health")
            self.assertIn(b"HTTP/1.1 200 OK", headers)
            self.assertIn(b"Access-Control-Allow-Origin: *", headers)
            self.assertNotIn(b"Access-Control-Allow-Credentials", headers)
            self.assertEqual(json.loads(body)["playbackProgressApiVersion"], 1)
            headers, _, body = await http("OPTIONS", "/health")
            self.assertIn(b"HTTP/1.1 204 No Content", headers)
            self.assertIn(b"Access-Control-Allow-Origin: *", headers)
            self.assertIn(b"Access-Control-Allow-Methods: GET, OPTIONS", headers)
            self.assertIn(b"Access-Control-Allow-Private-Network: true", headers)
            self.assertIn(b"Content-Length: 0", headers)
            self.assertEqual(body, b"")
            for method, path in (("GET", "/status"), ("OPTIONS", "/status"), ("OPTIONS", "/stream/current"), ("POST", "/health")):
                headers, _, _ = await http(method, path)
                self.assertNotIn(b"Access-Control-Allow-Origin", headers)
                self.assertNotIn(b"Access-Control-Allow-Private-Network", headers)
        finally:
            server.close()
            await server.wait_closed()
            await state.progress.close()


if __name__ == "__main__":
    unittest.main()
