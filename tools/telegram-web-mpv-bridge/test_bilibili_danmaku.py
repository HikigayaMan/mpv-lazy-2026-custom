"""Offline protocol and session tests; no Bilibili account/network required."""

import asyncio
from collections import deque
import errno
import json
import os
import struct
import threading
import time
import unittest
import uuid
from unittest.mock import patch
import zlib

import brotli

from bilibili_danmaku import (
    AuthRejected, BilibiliDanmakuManager, BilibiliSession, HEADER, MAX_PENDING,
    MpvPipe, SessionFailure, _write_pipe_request, safe_failure, decode_packets, make_packet, normalize_danmaku, validate_auth, validate_start,
)
from bridge import BRIDGE_VERSION, BridgeState, handle_http_client, ws_handler


def auth():
    return {"uid": 0, "token": "temporary-test-token", "buvid": "temporary-test-client", "userAgent": "Mozilla/5.0 offline-fixture",
            "hostList": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}]}


def start_request():
    return {"type": "startDanmaku", "provider": "bilibili", "requestId": "test-1", "roomId": "545068",
            "sessionId": "bilibili-test-1", "pipeName": "mpv-lazy-bilibili-test-1", "auth": auth()}


def event(text="你好，直播", cmd="DANMU_MSG"):
    return {"cmd": cmd, "info": [[0, 1, 25, 0x12AB34], text, [123, "测试观众"]]}


def json_packet(operation, payload, version=0):
    return make_packet(operation, json.dumps(payload, ensure_ascii=False).encode(), version)


class NonblockingPipeWriteTests(unittest.TestCase):
    """Win32-free backpressure fixtures for the production write loop."""
    class Clock:
        def __init__(self):
            self.value, self.sleeps = 0, []

        def monotonic(self):
            return self.value

        def sleep(self, duration):
            self.sleeps.append(duration)
            self.value += duration

    class Pipe:
        def __init__(self, actions=(), capacity=4096):
            self.actions, self.capacity = deque(actions), capacity
            self.received, self.calls = bytearray(), []

        def write(self, block):
            data = bytes(block)
            self.calls.append(data)
            result = self.actions.popleft() if self.actions else min(len(data), self.capacity)
            if isinstance(result, Exception):
                partial = getattr(result, "characters_written", 0)
                if partial:
                    self.received.extend(data[:partial])
                raise result
            if type(result) is int and 0 < result <= len(data):
                self.received.extend(data[:result])
            return result

    def write(self, pipe, request, deadline=2):
        clock = self.Clock()
        with patch("bilibili_danmaku.time.monotonic", clock.monotonic), patch("bilibili_danmaku.time.sleep", clock.sleep):
            _write_pipe_request(pipe, request, deadline)
        return clock

    def test_large_json_command_uses_4k_chunks_without_repeating_or_losing_bytes(self):
        for size in (4096, 8192, 32768):
            with self.subTest(size=size):
                request = json.dumps({"command": ["script-message-to", "web_playlist", "web-playlist-resolved", "x" * size], "request_id": 1}).encode() + b"\n"
                pipe = self.Pipe(capacity=4096)
                self.write(pipe, request)
                self.assertEqual(bytes(pipe.received), request)
                self.assertGreater(len(pipe.calls), 1)
                self.assertTrue(all(len(call) <= 4096 for call in pipe.calls))

    def test_partial_none_zero_and_eagain_preserve_exact_offset_and_yield_on_backpressure(self):
        request = b"a" * 14000 + b"\n"
        pipe = self.Pipe([128, None, BlockingIOError(errno.EAGAIN, "fixture input buffer full"), 512, 0], capacity=1024)
        clock = self.write(pipe, request)
        self.assertEqual(bytes(pipe.received), request)
        self.assertEqual(pipe.calls[1], request[128:128 + 4096])
        self.assertEqual(pipe.calls[1], pipe.calls[2])
        self.assertEqual(len(clock.sleeps), 3)
        self.assertTrue(all(duration > 0 for duration in clock.sleeps))

    def test_blocking_error_with_partial_bytes_continues_after_the_accepted_prefix(self):
        blocked = BlockingIOError(errno.EAGAIN, "fixture partially accepted write")
        blocked.characters_written = 17
        request = b"b" * 8192 + b"\n"
        pipe = self.Pipe([blocked])
        self.write(pipe, request)
        self.assertEqual(bytes(pipe.received), request)
        self.assertEqual(pipe.calls[1], request[17:17 + 4096])

    def test_utf8_command_is_reassembled_even_when_a_multibyte_character_crosses_chunks(self):
        request = json.dumps({"command": ["script-message", "标题" * 6000], "request_id": 1}, ensure_ascii=False).encode() + b"\n"
        pipe = self.Pipe(capacity=997)
        self.write(pipe, request)
        self.assertEqual(bytes(pipe.received), request)
        self.assertEqual(json.loads(pipe.received.decode())["command"][1], "标题" * 6000)

    def test_non_draining_pipe_expires_with_a_bounded_number_of_yielding_retries(self):
        clock, pipe = self.Clock(), self.Pipe(capacity=0)
        with patch("bilibili_danmaku.time.monotonic", clock.monotonic), patch("bilibili_danmaku.time.sleep", clock.sleep):
            with self.assertRaisesRegex(TimeoutError, "MPV IPC write timed out"):
                _write_pipe_request(pipe, b"not drained\n", 2)
        self.assertLessEqual(len(pipe.calls), 201)
        self.assertEqual(len(pipe.calls), len(clock.sleeps))
        self.assertTrue(all(duration > 0 for duration in clock.sleeps))
        self.assertGreaterEqual(clock.value, 2)

    def test_broken_pipe_is_propagated_and_oversized_command_is_rejected_before_writing(self):
        pipe = self.Pipe([BrokenPipeError(errno.EPIPE, "fixture pipe disconnected")])
        with self.assertRaises(BrokenPipeError):
            self.write(pipe, b"small\n")
        pipe = self.Pipe()
        with self.assertRaisesRegex(ValueError, "oversized MPV command"):
            self.write(pipe, b"x" * 65537)
        self.assertEqual(pipe.calls, [])

    def test_invalid_writer_count_is_rejected_and_expired_deadline_does_not_write(self):
        for value in (-1, 99999, True):
            with self.subTest(value=value), self.assertRaisesRegex(OSError, "invalid MPV IPC write result"):
                self.write(self.Pipe([value]), b"small\n")
        pipe = self.Pipe()
        with self.assertRaises(TimeoutError):
            self.write(pipe, b"small\n", deadline=0)
        self.assertEqual(pipe.calls, [])


class ProtocolTests(unittest.TestCase):
    def test_large_emotes_inline_emotes_and_structured_content_become_text(self):
        def with_emote(raw, options=None, extra=None, dm_type=1):
            payload = event(raw)
            meta = payload["info"][0]
            meta.extend([None] * (16 - len(meta)))
            meta[12], meta[13], meta[15] = dm_type, options or {}, {"extra": json.dumps(extra or {})}
            return payload

        image = "https://i0.hdslb.com/bfs/live/fixture.png"
        cases = [
            (with_emote("欢迎[dog]大家", {"url": image}, {"emots": {"[dog]": {"url": image}}}), "欢迎[dog]大家"),
            (with_emote("", {"emoji": "[大笑]", "url": image}), "[大笑]"),
            (with_emote(None, json.dumps({"name": "笑哭", "url": image})), "笑哭"),
            (with_emote(image, {"descript": "开心", "url": image}), "开心"),
            (with_emote("", {"url": image}, {"content": "[比心]", "emoticon_unique": "official_13"}), "[比心]"),
            (with_emote("", {}, {"emots": {"[笑]": {"url": image}, "[哭]": {"url": image}}}), "[笑][哭]"),
            (with_emote("official_13", {"emoticon_unique": "official_13", "url": image}), "[表情]"),
            (with_emote("", {"url": image}), "[表情]"),
            (with_emote({"text": "颜文字 (^_^)"}), "颜文字 (^_^)"),
            (with_emote('[1.0,0.0,"0.8-0.5",10.0,"辛苦了",0.0]'), "辛苦了"),
            (with_emote('{"url":"https://bad.invalid/a.png"}'), "[表情]"),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected):
                compressed = make_packet(5, brotli.compress(json_packet(5, payload)), 3)
                message = normalize_danmaku(decode_packets(compressed)[0][1])
                self.assertEqual(message["text"], expected)
                self.assertNotIn("https://", message["text"])
                self.assertNotIn('"url"', message["text"])

    def test_empty_malformed_metadata_does_not_invent_normal_danmaku(self):
        self.assertIsNone(normalize_danmaku(event("")))
        payload = event("[大笑]")
        payload["info"][0].extend([None] * 12)
        payload["info"][0][13] = "{malformed"
        payload["info"][0][15] = {"extra": "{malformed"}
        self.assertEqual(normalize_danmaku(payload)["text"], "[大笑]")

    def test_safe_failure_never_copies_raw_exception_details(self):
        secret = "temporary-test-token and SESSDATA=secret"
        for exc in (RuntimeError(secret), ValueError(secret), OSError(secret), TimeoutError(secret)):
            result = safe_failure(exc, "remote-connect")
            self.assertNotIn(secret, json.dumps(result))
            self.assertIn("code", result)
        auth_error = safe_failure(AuthRejected(bilibili_code=-101), "remote-auth")
        self.assertEqual(auth_error["bilibiliCode"], -101)

    def test_auth_and_heartbeat_wire_header(self):
        packet = json_packet(7, {"roomid": 545068}, 1)
        self.assertEqual(HEADER.unpack_from(packet), (len(packet), 16, 1, 7, 1))
        self.assertEqual(HEADER.unpack_from(make_packet(2))[3], 2)

    def test_plain_concatenated_zlib_brotli_and_nested_packets(self):
        inner = json_packet(5, event()) + json_packet(5, event("第二条", "DANMU_MSG:4:0:2:2:2:0"))
        compressed = make_packet(5, zlib.compress(inner), 2)
        brotli_packet = make_packet(5, brotli.compress(compressed), 3)
        packets = decode_packets(json_packet(8, {"code": 0}, 1) + brotli_packet + make_packet(3, struct.pack(">I", 234)))
        self.assertEqual([op for op, _ in packets], [8, 5, 5, 3])
        self.assertEqual(packets[-1], (3, 234))
        self.assertEqual(normalize_danmaku(packets[2][1])["text"], "第二条")

    def test_malformed_packets_and_decompression_bomb_rejected(self):
        for data in (b"short", HEADER.pack(0, 16, 0, 5, 1), HEADER.pack(17, 15, 0, 5, 1) + b"x",
                     HEADER.pack(100, 16, 0, 5, 1), json_packet(5, []), make_packet(5, b"invalid", 2)):
            with self.subTest(data=data[:16]), self.assertRaises((ValueError, zlib.error)):
                decode_packets(data)
        with self.assertRaises(ValueError):
            decode_packets(make_packet(5, zlib.compress(b"a" * (2 * 1024 * 1024 + 1)), 2))
        with self.assertRaises(ValueError):
            decode_packets(make_packet(5, brotli.compress(b"a" * (2 * 1024 * 1024 + 1)), 3))

    def test_normalize_filters_gifts_invalid_fields_and_control_characters(self):
        self.assertIsNone(normalize_danmaku({"cmd": "SEND_GIFT", "data": {}}))
        self.assertIsNone(normalize_danmaku({"cmd": "DANMU_MSG", "info": []}))
        self.assertIsNone(normalize_danmaku(event("\r\n\x00")))
        item = event("你好\n世界\x00")
        self.assertEqual(normalize_danmaku(item), {"text": "你好 世界 ", "nick": "测试观众", "color": 0x12AB34})
        item["info"][0][3] = "{\\pos(0,0)}"
        self.assertEqual(normalize_danmaku(item)["color"], 0xFFFFFF)

    def test_metadata_is_restricted_to_bilibili_and_expected_pipe(self):
        result = validate_start(start_request())
        self.assertEqual(result[3]["urls"], ["wss://broadcastlv.chat.bilibili.com:443/sub"])
        for bad_host in ("localhost", "chat.bilibili.com.attacker.example", "127.0.0.1", "broadcastlv.chat.bilibili.com/path"):
            invalid = auth()
            invalid["hostList"][0]["host"] = bad_host
            with self.subTest(host=bad_host), self.assertRaises(ValueError):
                validate_auth(invalid)
        for field, value in (("pipeName", "other-player-pipe"), ("pipeName", "mpv-lazy-bilibili-../../x"),
                             ("sessionId", "douyu-test"), ("roomId", "0"), ("roomId", "545068?other")):
            invalid = start_request()
            invalid[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_start(invalid)
        with self.assertRaises(ValueError):
            validate_auth({**auth(), "userAgent": "Mozilla/5.0\r\nCookie: injected"})


class FakeOwner:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


class FakeRemote:
    def __init__(self, code=0, messages=None):
        self.code = code
        self.messages = messages or []
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(raw)
        if HEADER.unpack_from(raw)[3] == 7:
            await self.queue.put(json_packet(8, {"code": self.code}, 1))
            for message in self.messages:
                await self.queue.put(message)

    async def recv(self):
        item = await self.queue.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class FakeConnector:
    def __init__(self, *remotes):
        self.remotes = deque(remotes)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.remotes:
            return self.remotes.popleft()
        raise OSError("offline fixture exhausted")


class FakeIpc:
    def __init__(self):
        self.commands = []
        self.active = False
        self.paused = False
        self.path = "https://fixture.bilivideo.com/live.flv"
        self.ready = True
        self.idle = False
        self.session_id = ""
        self.switch_marker = None
        self.switch_ack = None

    async def command(self, command):
        self.commands.append(command)
        if not self.ready:
            raise FileNotFoundError("fixture player has not launched")
        if command[0] == "get_property":
            return {"user-data/uosc_danmaku/live-api-version": 1,
                    "user-data/uosc_danmaku/live-active": self.active,
                    "user-data/uosc_danmaku/live-session": self.session_id,
                    "user-data/quality-menu/stream-switch": self.switch_marker,
                    "user-data/uosc_danmaku/live-switch-ack": self.switch_ack,
                    "idle-active": self.idle, "path": self.path, "pause": self.paused}[command[1]]
        if command[0] == "set_property" and command[1] == "user-data/uosc_danmaku/live-switch-ack":
            self.switch_ack = command[2]
            return
        if command[2] == "live-start":
            self.active = True
            self.session_id = command[3]
        elif command[2] == "live-stop":
            self.active = False
            self.session_id = ""


async def eventually(predicate, timeout=2):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_health_and_status_distinguish_browser_from_telegram_and_failed_start(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 8999)
        state.browser_clients.add(FakeOwner())
        await state.handle_ws_message(json.dumps({"type": "hello", "protocol": 2}), FakeOwner())
        invalid = start_request()
        invalid["auth"]["token"] = ""
        with self.assertRaises(ValueError):
            await state.danmaku.start(invalid, FakeOwner())
        server = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async def request(path):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                await writer.drain()
                raw = await reader.read()
                writer.close()
                await writer.wait_closed()
                return json.loads(raw.split(b"\r\n\r\n", 1)[1])
            health = await request("/health")
            self.assertEqual(health["bridgeVersion"], BRIDGE_VERSION)
            self.assertFalse(health["connected"])
            self.assertTrue(health["browserConnected"])
            self.assertEqual(health["browserClients"], 1)
            status = await request("/status")
            self.assertEqual(status["bridgeVersion"], BRIDGE_VERSION)
            self.assertEqual(status["danmaku"], [])
            self.assertEqual(status["diagnostics"]["helloCount"], 1)
            self.assertEqual(status["diagnostics"]["failedStarts"], 1)
            self.assertEqual(status["diagnostics"]["recentFailures"][0]["error"]["code"], "bilibili_metadata_invalid")
            self.assertNotIn("temporary-test-client", json.dumps(status))
        finally:
            server.close()
            await server.wait_closed()

    async def test_failed_start_survives_empty_active_list_as_safe_diagnostic(self):
        def factory(*args):
            return BilibiliSession(*args, connector=FakeConnector(FakeRemote(code=-101)), ipc=FakeIpc())
        manager = BilibiliDanmakuManager(session_factory=factory)
        with self.assertRaises(SessionFailure) as error:
            await manager.start(start_request(), FakeOwner())
        self.assertEqual(error.exception.code, "bilibili_auth_rejected")
        self.assertEqual(manager.snapshots(), [])
        diagnostics = manager.diagnostics()
        self.assertEqual((diagnostics["startRequests"], diagnostics["startedSessions"], diagnostics["failedStarts"]), (1, 0, 1))
        failure = diagnostics["recentFailures"][0]
        self.assertEqual(failure["error"]["bilibiliCode"], -101)
        self.assertEqual(failure["error"]["phase"], "remote-auth")
        encoded = json.dumps(diagnostics)
        self.assertNotIn("temporary-test-token", encoded)
        self.assertNotIn("temporary-test-client", encoded)
        self.assertNotIn("userAgent", encoded)

    @unittest.skipUnless(os.name == "nt", "Windows named-pipe transport")
    async def test_real_windows_pipe_ignores_events_and_matches_response(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        ipc = MpvPipe("mpv-lazy-bilibili-test-" + uuid.uuid4().hex)
        ready = threading.Event()
        requests, errors = [], []

        def server():
            handle = kernel.CreateNamedPipeW(ipc.path, 3, 0, 1, 65536, 65536, 0, None)
            try:
                if handle == wintypes.HANDLE(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                ready.set()
                if not kernel.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
                    raise ctypes.WinError(ctypes.get_last_error())
                data = ctypes.create_string_buffer(65536)
                size = wintypes.DWORD()
                if not kernel.ReadFile(handle, data, len(data), ctypes.byref(size), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                requests.append(json.loads(data.raw[:size.value]))
                response = b'{"event":"start-file"}\n{"request_id":99,"error":"success","data":false}\n{"request_id":1,"error":"success","data":true}\n'
                if not kernel.WriteFile(handle, response, len(response), ctypes.byref(size), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                kernel.FlushFileBuffers(handle)
            except Exception as exc:
                errors.append(exc)
                ready.set()
            finally:
                kernel.DisconnectNamedPipe(handle)
                kernel.CloseHandle(handle)
        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        self.assertTrue(await asyncio.to_thread(ready.wait, 2))
        self.assertTrue(await ipc.command(["get_property", "pause"]))
        await asyncio.to_thread(thread.join, 2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(requests[0]["command"], ["get_property", "pause"])

    async def test_ack_before_player_launch_then_live_ipc_and_eof_cleanup(self):
        owner, ipc = FakeOwner(), FakeIpc()
        ipc.ready = False
        remote = FakeRemote(messages=[json_packet(5, event())])
        connector = FakeConnector(remote)
        session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1", validate_auth(auth()),
                                  owner, connector=connector, ipc=ipc)
        await session.start()
        self.assertFalse(session.player_attached)
        self.assertEqual(len(session.pending), 1)
        ipc.ready = True
        await eventually(lambda: session.sent == 1)
        live = next(cmd for cmd in ipc.commands if len(cmd) > 2 and cmd[2] == "live-batch")
        self.assertEqual(live[:4], ["script-message-to", "uosc_danmaku", "live-batch", "bilibili-test-1"])
        self.assertEqual(json.loads(live[4])[0]["text"], "你好，直播")
        auth_sent = json.loads(remote.sent[0][HEADER.size:])
        self.assertEqual(auth_sent["protover"], 3)
        self.assertEqual(auth_sent["key"], "temporary-test-token")
        self.assertEqual(connector.calls[0][1]["user_agent_header"], auth()["userAgent"])
        ipc.active = False  # uosc end-file handler clears live-active.
        await asyncio.wait_for(session.task, 2)
        self.assertTrue(remote.closed)
        self.assertEqual(session.auth, {})
        self.assertEqual(owner.messages[-1]["type"], "danmakuStopped")

    async def test_auth_rejected_fails_before_handoff_and_closes(self):
        remote = FakeRemote(code=-101)
        session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1", validate_auth(auth()),
                                  FakeOwner(), connector=FakeConnector(remote), ipc=FakeIpc())
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(session.start(), 1)
        self.assertTrue(remote.closed)
        self.assertTrue(session.task.done())

    async def running_player_session(self):
        ipc, remote = FakeIpc(), FakeRemote()
        session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1", validate_auth(auth()),
                                  FakeOwner(), connector=FakeConnector(remote), ipc=ipc)
        await session.start()
        await eventually(lambda: any(cmd == ["get_property", "user-data/quality-menu/stream-switch"] for cmd in ipc.commands))
        self.addAsyncCleanup(session.stop)
        return session, ipc, remote

    def player_switch_marker(self, session, ipc, **overrides):
        marker = {"generation": 1, "provider": "bilibili", "sessionId": session.session_id,
                  "from": ipc.path, "to": "https://fixture.bilivideo.com/new-quality.flv",
                  "startedAt": 1.0, "expiresAt": int(time.time()) + 15, "stage": "loading"}
        marker.update(overrides)
        ipc.switch_marker = marker
        return marker

    async def test_authorized_switch_survives_idle_and_forwards_only_new_messages(self):
        session, ipc, remote = await self.running_player_session()
        marker = self.player_switch_marker(session, ipc)
        ipc.path = marker["to"]
        ipc.idle = True
        await remote.queue.put(json_packet(5, event("old stream during reload")))
        await eventually(lambda: session.received == 1 and session.dropped == 1)
        self.assertFalse(session.task.done())
        self.assertEqual(session.sent, 0)
        ipc.idle = False
        marker["stage"] = "loaded"
        # Wait for the loaded transition to flush old timestamps before sending.
        await asyncio.sleep(0.3)
        await remote.queue.put(json_packet(5, event("new quality live message")))
        await eventually(lambda: session.sent == 1)
        batches = [json.loads(cmd[4]) for cmd in ipc.commands if len(cmd) > 2 and cmd[2] == "live-batch"]
        self.assertEqual(batches, [[{"text": "new quality live message", "nick": "测试观众", "color": 0x12AB34}]])
        # Leaving the room is not allowed by the old switch marker.
        ipc.path = "https://fixture.bilivideo.com/unrelated.flv"
        await asyncio.wait_for(session.task, 2)
        self.assertEqual(session.ended_reason, "mpv_file_changed")

    async def test_invalid_expired_and_normal_file_changes_stop_session(self):
        cases = [None, {"sessionId": "bilibili-other"}, {"provider": "other"},
                 {"expiresAt": time.time() - 1}, {"expiresAt": time.time() + 100},
                 {"from": "https://fixture.bilivideo.com/wrong-old.flv"}, {"stage": "failed"},
                 {"to": "C:/local-other-video.mkv"}, {"generation": float("inf")}, {"generation": True}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                session, ipc, _ = await self.running_player_session()
                if overrides is not None:
                    self.player_switch_marker(session, ipc, **overrides)
                ipc.path = "https://fixture.bilivideo.com/new-quality.flv"
                await asyncio.wait_for(session.task, 2)
                self.assertEqual(session.ended_reason, "mpv_file_changed")
                self.assertFalse(ipc.active)

    async def test_loaded_marker_racing_with_old_path_read_refreshes_player_state(self):
        session, ipc, remote = await self.running_player_session()
        marker = self.player_switch_marker(session, ipc)
        original_command = ipc.command
        armed = True
        refreshed_reads = 0

        async def racing_command(command):
            nonlocal armed, refreshed_reads
            if command == ["get_property", "path"]:
                if armed:
                    old_path = await original_command(command)
                    ipc.path = marker["to"]
                    marker["stage"] = "loaded"
                    ipc.idle = False
                    armed = False
                    return old_path
                refreshed_reads += 1
            return await original_command(command)

        ipc.command = racing_command
        await eventually(lambda: not armed and refreshed_reads > 0)
        self.assertFalse(session.task.done())
        await remote.queue.put(json_packet(5, event("message after raced switch")))
        await eventually(lambda: session.sent == 1)
        self.assertFalse(session.task.done())
        self.assertIsNone(session.error)
        ipc.path = "https://fixture.bilivideo.com/third-file.flv"
        await asyncio.wait_for(session.task, 2)
        self.assertEqual(session.ended_reason, "mpv_file_changed")

    async def test_loaded_marker_tolerates_transient_idle_and_rechecks_session(self):
        session, ipc, remote = await self.running_player_session()
        marker = self.player_switch_marker(session, ipc, stage="loaded")
        ipc.idle = True
        await remote.queue.put(json_packet(5, event("transient reload")))
        await eventually(lambda: session.dropped == 1)
        self.assertFalse(session.task.done())
        ipc.path = marker["to"]
        ipc.idle = False
        await asyncio.sleep(0.3)
        await remote.queue.put(json_packet(5, event("loaded and playing")))
        await eventually(lambda: session.sent == 1)
        ipc.session_id = "bilibili-other-player-session"
        await asyncio.wait_for(session.task, 2)
        self.assertEqual(session.ended_reason, "mpv_live_session_changed")

    async def test_two_consecutive_quality_switches_acknowledge_each_bound_target(self):
        session, ipc, remote = await self.running_player_session()
        for generation in (1, 2):
            marker = self.player_switch_marker(session, ipc, generation=generation,
                                               to=f"https://fixture.bilivideo.com/quality-{generation}.flv")
            ipc.idle = True
            await remote.queue.put(json_packet(5, event(f"discard loading generation {generation}")))
            await eventually(lambda: session.dropped == generation)
            self.assertNotEqual(ipc.switch_ack, {"sessionId": session.session_id, "generation": generation})
            ipc.path = marker["to"]
            ipc.idle = False
            marker["stage"] = "loaded"
            await eventually(lambda: ipc.switch_ack == {"sessionId": session.session_id, "generation": generation})
            await remote.queue.put(json_packet(5, event(f"new stream generation {generation}")))
            await eventually(lambda: session.sent == generation)
            self.assertFalse(session.task.done())
        acknowledgements = [cmd[2] for cmd in ipc.commands if cmd[:2] == ["set_property", "user-data/uosc_danmaku/live-switch-ack"]]
        self.assertEqual(acknowledgements, [{"sessionId": session.session_id, "generation": 1},
                                            {"sessionId": session.session_id, "generation": 2}])
        self.assertEqual(session.received, 4)
        self.assertEqual(session.dropped, 2)
        self.assertEqual(session.sent, 2)

    async def test_switch_expiry_and_marker_mutation_stop_pending_transition(self):
        for mutation in ("expired", "new-target"):
            with self.subTest(mutation=mutation):
                session, ipc, remote = await self.running_player_session()
                marker = self.player_switch_marker(session, ipc)
                ipc.idle = True
                await remote.queue.put(json_packet(5, event("switching")))
                await eventually(lambda: session.dropped == 1)
                if mutation == "expired":
                    marker["expiresAt"] = time.time() - 1
                else:
                    marker["to"] = "https://fixture.bilivideo.com/unauthorized.flv"
                await asyncio.wait_for(session.task, 2)
                self.assertEqual(session.ended_reason, "mpv_stream_switch_invalid")

    async def test_same_path_reload_and_consumed_generation_cannot_allow_replay(self):
        session, ipc, remote = await self.running_player_session()
        marker = self.player_switch_marker(session, ipc, to=ipc.path, generation=1.0)
        ipc.idle = True
        await remote.queue.put(json_packet(5, event("loading")))
        await eventually(lambda: session.dropped == 1)
        marker["stage"] = "loaded"
        ipc.idle = False
        await asyncio.sleep(0.3)
        await remote.queue.put(json_packet(5, event("continued")))
        await eventually(lambda: session.sent == 1)
        marker["to"] = "https://fixture.bilivideo.com/replayed.flv"
        ipc.path = marker["to"]
        await asyncio.wait_for(session.task, 2)
        self.assertEqual(session.ended_reason, "mpv_file_changed")

    async def test_bounded_queue_age_pause_unicode_and_batch_bytes(self):
        session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1", validate_auth(auth()), FakeOwner(), ipc=FakeIpc())
        session.pending.append((time.monotonic() - 10, {"text": "old"}))
        self.assertEqual(session.take_batch(), [])
        self.assertEqual(session.dropped, 1)
        for i in range(MAX_PENDING + 50):
            session.enqueue({"text": "🙂" * 160, "nick": "观" * 48, "color": 0xFFFFFF})
        self.assertEqual(len(session.pending), MAX_PENDING)
        self.assertEqual(session.dropped, 51)
        batch = session.take_batch()
        self.assertLess(len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode()), 60000)
        self.assertLessEqual(len(batch), 120)
        self.assertEqual(session.take_batch(paused=True), [])
        self.assertEqual(len(session.pending), 0)

    async def test_escaped_danmaku_batches_fit_final_ipc_command_without_losing_items(self):
        for text in ("\\" * 160, '"' * 160, "x\t" * 80, "🙂\\" * 80):
            with self.subTest(text=text[:4]):
                session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1",
                                          validate_auth(auth()), FakeOwner(), ipc=FakeIpc())
                for index in range(120):
                    session.enqueue({"text": text, "nick": "\\" * 48, "color": index})
                delivered = []
                while session.pending:
                    batch = session.take_batch()
                    self.assertTrue(batch)
                    body = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
                    command = ["script-message-to", "uosc_danmaku", "live-batch", session.session_id, body]
                    wire = json.dumps({"command": command, "request_id": 1}, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8") + b"\n"
                    self.assertLessEqual(len(wire), 65536)
                    delivered.extend(batch)
                self.assertEqual([item["color"] for item in delivered], list(range(120)))
                self.assertEqual(session.dropped, 0)

    async def test_disconnect_reconnect_and_cancel_stops_heartbeat(self):
        first, second = FakeRemote(), FakeRemote(messages=[json_packet(5, event("重连后"))])
        connector = FakeConnector(first, second)
        session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1", validate_auth(auth()), FakeOwner(), connector=connector, ipc=FakeIpc())
        await session.start()
        await first.queue.put(OSError("fixture disconnected"))
        await eventually(lambda: session.sent == 1)
        self.assertGreaterEqual(session.reconnects, 1)
        self.assertEqual(len(connector.calls), 2)
        self.assertTrue(first.closed)
        await session.stop()
        self.assertTrue(second.closed)
        self.assertEqual(len(session.pending), 0)

    async def test_auth_refresh_requires_owner_matching_request(self):
        owner = FakeOwner()
        session = BilibiliSession("545068", "bilibili-test-1", "mpv-lazy-bilibili-test-1", validate_auth(auth()), owner, ipc=FakeIpc())
        manager = BilibiliDanmakuManager()
        manager.sessions[session.session_id] = session
        task = asyncio.create_task(session.refresh_auth())
        await eventually(lambda: bool(owner.messages))
        request = owner.messages[0]
        reply = {"type": "updateDanmakuAuth", "provider": "bilibili", "sessionId": session.session_id,
                 "requestId": request["requestId"], "auth": auth()}
        manager.update_auth(reply, FakeOwner())
        self.assertFalse(task.done())
        manager.update_auth({**reply, "requestId": "stale"}, owner)
        self.assertFalse(task.done())
        manager.update_auth(reply, owner)
        await task
        self.assertEqual(request["provider"], "bilibili")
        self.assertIsNone(session.auth_refresh)

    async def test_manager_isolates_players_and_owners_and_omits_secrets(self):
        created = []
        def factory(*args):
            session = BilibiliSession(*args, connector=FakeConnector(FakeRemote()), ipc=FakeIpc())
            created.append(session)
            return session
        manager = BilibiliDanmakuManager(session_factory=factory)
        owner = FakeOwner()
        ack = await manager.start(start_request(), owner)
        self.assertEqual((ack["provider"], ack["apiVersion"]), ("bilibili", 2))
        with self.assertRaises(ValueError):
            await manager.start(start_request(), owner)
        snapshot = json.dumps(manager.snapshots())
        self.assertNotIn("temporary-test-token", snapshot)
        self.assertNotIn("temporary-test-client", snapshot)
        await manager.stop("bilibili-test-1", FakeOwner())
        self.assertEqual(len(manager.sessions), 1)
        await manager.close()
        self.assertTrue(created[0].task.done())

    async def test_bridge_bilibili_hello_does_not_replace_telegram_range_owner(self):
        state = BridgeState(1, 1024, "mpv.exe", "127.0.0.1", 8999)
        telegram, bili = FakeOwner(), FakeOwner()
        await state.set_ws(telegram)
        await state.handle_ws_message(json.dumps({"type": "hello", "protocol": 2}), bili)
        self.assertIs(state.ws, telegram)
        await state.handle_ws_message(json.dumps({"type": "registerVideo", "url": "https://web.telegram.org/stream/1"}), telegram)
        self.assertIs(state.ws, telegram)
        self.assertEqual(state.video.url, "https://web.telegram.org/stream/1")

    async def test_stop_arrives_during_authentication_without_orphan(self):
        class SlowRemote(FakeRemote):
            async def send(self, raw):
                self.sent.append(raw)  # Authentication remains unanswered.
        remote = SlowRemote()
        owner = FakeOwner()
        def factory(*args):
            return BilibiliSession(*args, connector=FakeConnector(remote), ipc=FakeIpc())
        manager = BilibiliDanmakuManager(session_factory=factory)
        startup = asyncio.create_task(manager.start(start_request(), owner))
        await eventually(lambda: "bilibili-test-1" in manager.sessions)
        session = manager.sessions["bilibili-test-1"]
        await manager.stop(session.session_id, owner)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 1)
        self.assertEqual(manager.sessions, {})
        self.assertTrue(session.task.done())


if __name__ == "__main__":
    unittest.main()
