"""Ephemeral Bilibili live WebSocket input for MPV's uosc_danmaku live API.

Only the browser obtains account-bound connection metadata. No cookies, tokens,
or chat history are written to disk. Packet format verified against blivedm's
clients/ws_base.py and clients/web.py (see docs/scripts/bilibili-live-danmaku.md).
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import json
import logging
import math
import os
import re
import struct
import time
import zlib
from typing import Any

import brotli
from websockets.asyncio.client import connect


LOG = logging.getLogger("bilibili-live-danmaku")
HEADER = struct.Struct(">IHHII")
MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_PACKET_COUNT = 4096
MAX_PENDING = 300
MAX_BATCH = 120
MAX_AGE = 2.0
STREAM_SWITCH_PROPERTY = "user-data/quality-menu/stream-switch"
STREAM_SWITCH_ACK_PROPERTY = "user-data/uosc_danmaku/live-switch-ack"
STREAM_SWITCH_SECONDS = 15
SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SAFE_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+chat\.bilibili\.com\Z")


class SessionFailure(RuntimeError):
    """A fixed, public error message/code with no remote payload contents."""

    def __init__(self, code: str, message: str, *, bilibili_code: int | None = None) -> None:
        super().__init__(message)
        self.code, self.bilibili_code = code, bilibili_code


class AuthRejected(SessionFailure):
    def __init__(self, message: str = "Bilibili rejected danmaku authentication", *, bilibili_code: int | None = None) -> None:
        super().__init__("bilibili_auth_rejected", message, bilibili_code=bilibili_code)


def safe_failure(exc: BaseException, phase: str) -> dict[str, Any]:
    """Keep only controlled messages and numeric error codes, never str(exc)."""
    if isinstance(exc, SessionFailure):
        result = {"code": exc.code, "message": str(exc), "phase": phase}
        if exc.bilibili_code is not None:
            result["bilibiliCode"] = exc.bilibili_code
        return result
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        code = "mpv_attach_timeout" if phase.startswith("player") else "bilibili_connection_timeout"
        message = "MPV live API attachment timed out" if phase.startswith("player") else "Bilibili connection timed out"
    elif isinstance(exc, OSError):
        code = "mpv_ipc_unavailable" if phase.startswith("player") else "bilibili_network_error"
        message = "MPV IPC pipe is unavailable" if phase.startswith("player") else "Bilibili network connection failed"
    elif isinstance(exc, ValueError):
        code, message = "bilibili_packet_invalid", "Bilibili returned an unsupported or invalid packet"
    else:
        code = "mpv_live_api_error" if phase.startswith("player") else "bilibili_connection_error"
        message = "MPV live API request failed" if phase.startswith("player") else "Bilibili connection failed"
    result = {"code": code, "message": message, "phase": phase, "exceptionType": type(exc).__name__}
    for name in ("errno", "winerror"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            result[name] = value
    return result


def make_packet(operation: int, body: bytes = b"", version: int = 1) -> bytes:
    return HEADER.pack(HEADER.size + len(body), HEADER.size, version, operation, 1) + body


def _inflate_limited(body: bytes, version: int) -> bytes:
    if version == 2:
        decoder = zlib.decompressobj()
        result = decoder.decompress(body, MAX_FRAME_BYTES + 1)
        if len(result) > MAX_FRAME_BYTES or decoder.unconsumed_tail or not decoder.eof:
            raise ValueError("invalid or oversized zlib packet")
        return result
    decoder = brotli.Decompressor()
    chunks: list[bytes] = []
    size = 0
    # Incremental decoding avoids accepting a huge compressed packet into RAM.
    for offset in range(0, len(body), 1024):
        chunk = decoder.process(body[offset:offset + 1024])
        size += len(chunk)
        if size > MAX_FRAME_BYTES:
            raise ValueError("oversized brotli packet")
        chunks.append(chunk)
    if not decoder.is_finished():
        raise ValueError("truncated brotli packet")
    return b"".join(chunks)


def decode_packets(data: bytes, depth: int = 0, budget: list[int] | None = None) -> list[tuple[int, Any]]:
    """Split concatenated packets, recursively unpacking zlib/Brotli containers."""
    if depth > 4 or len(data) > MAX_FRAME_BYTES:
        raise ValueError("oversized or deeply nested Bilibili packet")
    budget = budget if budget is not None else [MAX_PACKET_COUNT]
    packets: list[tuple[int, Any]] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise ValueError("truncated Bilibili header")
        length, header_size, version, operation, _sequence = HEADER.unpack_from(data, offset)
        if header_size < HEADER.size or length < header_size or offset + length > len(data):
            raise ValueError("invalid Bilibili packet length")
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("too many Bilibili packets")
        body = data[offset + header_size:offset + length]
        offset += length
        if operation == 5 and version in (2, 3):
            packets.extend(decode_packets(_inflate_limited(body, version), depth + 1, budget))
        elif operation in (5, 8) and version in (0, 1):
            payload = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(payload, dict):
                raise ValueError("Bilibili message must be an object")
            packets.append((operation, payload))
        elif operation == 3:
            packets.append((operation, int.from_bytes(body[:4], "big")))
        # Gift/room metadata and unsupported packet versions never enter IPC.
    return packets


def _metadata_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= 16384:
        with contextlib.suppress(ValueError):
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
    return {}


def _plain_text(value: Any, *, metadata: bool = False, depth: int = 0) -> str:
    if depth > 2:
        return ""
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00\r\n]", " ", value)
    text = cleaned.strip()
    if not text:
        return ""
    if text[:1] in ("{", "[") and len(text) <= 16384:
        with contextlib.suppress(ValueError):
            structured = json.loads(text)
            if isinstance(structured, list):
                # Mode 7 advanced danmaku stores the visible string at index 4.
                return _plain_text(structured[4], metadata=True, depth=depth + 1) if len(structured) > 4 else ""
            if isinstance(structured, dict):
                return _emoticon_label(structured)
        if text.startswith('{"') or text.startswith('[{"') or text.startswith('["'):
            return ""
    if metadata and re.match(r"^(?:https?:)?//", text, re.I):
        return ""
    if re.match(r"^(?:data:image/|blob:|<img\b)", text, re.I):
        return ""
    if re.match(r"^(?:https?:)?//\S+\.(?:png|gif|jpe?g|webp|avif|svg)(?:[?#].*)?$", text, re.I):
        return ""
    return (text if metadata else cleaned)[:160]


def _emoticon_label(options: dict[str, Any]) -> str:
    for key in ("emoji", "text", "name", "descript", "alt", "label", "content"):
        value = options.get(key)
        # Labels are scalar text only; don't recursively traverse arbitrary data.
        if isinstance(value, str) and not value.lstrip().startswith(("{", "[\"")):
            text = _plain_text(value, metadata=True)
            if text:
                return text
    return ""


def danmaku_text(info: list[Any]) -> str:
    """Render Bilibili large/inline emotes as text, never image URLs or JSON.

    The live model uses info[1] for text, info[0][12] for dm_type,
    info[0][13] for large-emote options and info[0][15].extra for inline emots.
    See xfgryujk/blivedm models/web.py DanmakuMessage for those wire fields.
    """
    meta = info[0] if info and isinstance(info[0], list) else []
    emoticon = _metadata_object(meta[13]) if len(meta) > 13 else {}
    mode_info = _metadata_object(meta[15]) if len(meta) > 15 else {}
    extra = _metadata_object(mode_info.get("extra"))
    raw = info[1] if len(info) > 1 else None
    text = _plain_text(raw)
    unique = emoticon.get("emoticon_unique") or extra.get("emoticon_unique")
    if text and text != unique:
        return text
    text = _plain_text(extra.get("content"), metadata=True) or _emoticon_label(emoticon)
    if text == unique:
        text = ""
    if not text and isinstance(raw, dict):
        text = _emoticon_label(raw)
    if text:
        return text
    emots = extra.get("emots")
    if isinstance(emots, dict):
        labels = [_plain_text(key, metadata=True) for key in list(emots)[:8]]
        text = "".join(label for label in labels if label)
        if text:
            return text[:160]
    is_emoticon = (len(meta) > 12 and meta[12] == 1) or extra.get("dm_type") == 1 or bool(emoticon) or bool(emots)
    return "[表情]" if is_emoticon else ""


def normalize_danmaku(payload: dict[str, Any]) -> dict[str, Any] | None:
    if str(payload.get("cmd", "")).split(":", 1)[0] != "DANMU_MSG":
        return None
    info = payload.get("info")
    if not isinstance(info, list) or len(info) < 2:
        return None
    text = danmaku_text(info)
    if not text.strip():
        return None
    user = info[2] if len(info) > 2 and isinstance(info[2], list) else []
    nick = re.sub(r"[\x00\r\n]", " ", user[1])[:48] if len(user) > 1 and isinstance(user[1], str) else ""
    meta = info[0] if isinstance(info[0], list) else []
    color = meta[3] if len(meta) > 3 else 0xFFFFFF
    if not isinstance(color, int) or isinstance(color, bool) or not 0 <= color <= 0xFFFFFF:
        color = 0xFFFFFF
    return {"text": text, "nick": nick, "color": color}


def validate_auth(auth: Any) -> dict[str, Any]:
    if not isinstance(auth, dict):
        raise ValueError("Bilibili connection metadata is required")
    token = auth.get("token")
    buvid = auth.get("buvid", "")
    uid = auth.get("uid", 0)
    user_agent = auth.get("userAgent")
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise ValueError("invalid Bilibili token")
    if not isinstance(buvid, str) or len(buvid) > 256 or any(ord(c) < 32 for c in buvid):
        raise ValueError("invalid Bilibili browser identifier")
    if not isinstance(uid, int) or isinstance(uid, bool) or not 0 <= uid <= 2**53 - 1:
        raise ValueError("invalid Bilibili uid")
    # The WebSocket token can bind to the User-Agent used by getDanmuInfo.
    if not isinstance(user_agent, str) or not 1 <= len(user_agent) <= 1024 or any(not 32 <= ord(c) <= 126 for c in user_agent):
        raise ValueError("invalid Bilibili browser User-Agent")
    hosts = auth.get("hostList")
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 16:
        raise ValueError("invalid Bilibili host list")
    urls = []
    for entry in hosts:
        if not isinstance(entry, dict):
            raise ValueError("invalid Bilibili host entry")
        host, port = entry.get("host"), entry.get("wss_port", 443)
        if not isinstance(host, str) or not SAFE_HOST.fullmatch(host.lower()):
            raise ValueError("Bilibili host must be a chat.bilibili.com server")
        if not isinstance(port, int) or isinstance(port, bool) or port not in (443, 2245):
            raise ValueError("invalid Bilibili secure WebSocket port")
        urls.append(f"wss://{host.lower()}:{port}/sub")
    return {"uid": uid, "token": token, "buvid": buvid, "userAgent": user_agent, "urls": list(dict.fromkeys(urls))}


def validate_start(payload: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    room_id = payload.get("roomId")
    session_id = payload.get("sessionId")
    pipe_name = payload.get("pipeName")
    if not isinstance(room_id, str) or not re.fullmatch(r"[1-9][0-9]{0,14}", room_id):
        raise ValueError("invalid canonical Bilibili room id")
    if not isinstance(session_id, str) or not SAFE_TOKEN.fullmatch(session_id) or not session_id.startswith("bilibili-"):
        raise ValueError("invalid Bilibili session id")
    if not isinstance(pipe_name, str) or not SAFE_TOKEN.fullmatch(pipe_name) or not pipe_name.startswith("mpv-lazy-bilibili-"):
        raise ValueError("invalid Bilibili IPC pipe name")
    return room_id, session_id, pipe_name, validate_auth(payload.get("auth"))


class MpvPipe:
    """Small request/response client with bounded Windows pipe reads.

    Opening a fresh connection per command prevents an event backlog. Blocking
    Win32 I/O runs in a worker, and PIPE_NOWAIT bounds reads and writes even if a
    player hangs. The per-session path can only be the validated named pipe.
    """

    def __init__(self, pipe_name: str) -> None:
        self.path = "\\\\.\\pipe\\" + pipe_name
        self.lock = asyncio.Lock()

    async def command(self, command: list[Any]) -> Any:
        async with self.lock:
            return await asyncio.to_thread(self._exchange, command)

    def _exchange(self, command: list[Any]) -> Any:
        if os.name != "nt":
            raise RuntimeError("Bilibili MPV handoff requires Windows named pipes")
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p, ctypes.c_void_p]
        kernel.SetNamedPipeHandleState.restype = wintypes.BOOL
        kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        request = json.dumps({"command": command, "request_id": 1}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(request) > 65536:
            raise ValueError("oversized MPV command")
        with open(self.path, "r+b", buffering=0) as pipe:
            handle = wintypes.HANDLE(msvcrt.get_osfhandle(pipe.fileno()))
            mode = wintypes.DWORD(1)  # PIPE_READMODE_BYTE | PIPE_NOWAIT
            if not kernel.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            written = pipe.write(request)
            if written != len(request):
                raise OSError("incomplete MPV IPC command")
            deadline = time.monotonic() + 2.0
            response = b""
            while time.monotonic() < deadline:
                available = wintypes.DWORD()
                if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if available.value:
                    response += pipe.read(min(available.value, 65536)) or b""
                    if len(response) > 131072:
                        raise ValueError("oversized MPV IPC response")
                    while b"\n" in response:
                        line, response = response.split(b"\n", 1)
                        payload = json.loads(line)
                        if payload.get("request_id") == 1:
                            if payload.get("error") != "success":
                                raise RuntimeError("MPV IPC command failed: " + str(payload.get("error")))
                            return payload.get("data")
                time.sleep(0.01)
        raise TimeoutError("MPV IPC response timed out")


class BilibiliSession:
    def __init__(self, room_id: str, session_id: str, pipe_name: str, auth: dict[str, Any], owner, *, connector=connect, ipc=None) -> None:
        self.room_id, self.session_id, self.owner = room_id, session_id, owner
        self.auth = auth
        self.connector = connector
        self.ipc = ipc or MpvPipe(pipe_name)
        self.pending: deque[tuple[float, dict[str, Any]]] = deque(maxlen=MAX_PENDING)
        self.started = asyncio.get_running_loop().create_future()
        self.task: asyncio.Task | None = None
        self.remote_task: asyncio.Task | None = None
        self.status = "authenticating"
        self.phase = "remote-auth"
        self.error: dict[str, Any] | None = None
        self.last_connection_error: dict[str, Any] | None = None
        self.ended_reason: str | None = None
        self.frames = self.danmaku_events = self.ignored_danmaku = 0
        self.received = self.sent = self.dropped = self.reconnects = 0
        self.player_attached = False
        self.auth_refresh: asyncio.Future | None = None
        self.auth_refresh_id = ""

    def snapshot(self) -> dict[str, Any]:
        return {"provider": "bilibili", "sessionId": self.session_id, "roomId": self.room_id,
                "status": self.status, "received": self.received, "sent": self.sent,
                "dropped": self.dropped, "pending": len(self.pending), "reconnects": self.reconnects,
                "phase": self.phase, "playerAttached": self.player_attached, "frames": self.frames,
                "danmakuEvents": self.danmaku_events, "ignoredDanmaku": self.ignored_danmaku,
                "error": self.error, "lastConnectionError": self.last_connection_error,
                "endedReason": self.ended_reason}

    def enqueue(self, item: dict[str, Any]) -> None:
        self.received += 1
        if len(self.pending) == MAX_PENDING:
            self.dropped += 1
        self.pending.append((time.monotonic(), item))

    def take_batch(self, paused: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if paused:
            self.dropped += len(self.pending)
            self.pending.clear()
            return []
        while self.pending and now - self.pending[0][0] > MAX_AGE:
            self.pending.popleft()
            self.dropped += 1
        batch = []
        encoded_size = 2
        while self.pending and len(batch) < MAX_BATCH:
            item = self.pending[0][1]
            item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
            if encoded_size + item_size > 60000:
                break
            batch.append(self.pending.popleft()[1])
            encoded_size += item_size
        return batch

    async def start(self) -> None:
        self.task = asyncio.create_task(self.run())
        try:
            await asyncio.wait_for(asyncio.shield(self.started), 8.0)
        except BaseException as exc:
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                self.error = safe_failure(SessionFailure("bilibili_auth_start_timeout", "Bilibili authentication did not complete within 8 seconds"), "remote-auth")
                self.status = "failed"
            await self.stop()
            if self.error and not isinstance(exc, asyncio.CancelledError):
                raise SessionFailure(self.error["code"], self.error["message"], bilibili_code=self.error.get("bilibiliCode")) from None
            raise

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _heartbeat(self, ws) -> None:
        while True:
            await ws.send(make_packet(2, b"[object Object]"))
            await asyncio.sleep(30)

    async def refresh_auth(self) -> None:
        self.auth_refresh_id = f"refresh-{time.monotonic_ns()}"
        self.auth_refresh = asyncio.get_running_loop().create_future()
        await self.owner.send(json.dumps({"type": "refreshDanmakuAuth", "provider": "bilibili", "sessionId": self.session_id,
                                         "roomId": self.room_id, "requestId": self.auth_refresh_id}))
        try:
            self.auth = await asyncio.wait_for(self.auth_refresh, 15)
        finally:
            self.auth_refresh = None

    def accept_auth(self, payload: dict[str, Any]) -> None:
        if not self.auth_refresh or self.auth_refresh.done() or payload.get("requestId") != self.auth_refresh_id:
            return
        if payload.get("type") == "danmakuAuthError":
            self.auth_refresh.set_exception(AuthRejected("Bilibili connection metadata refresh failed"))
        else:
            try:
                self.auth_refresh.set_result(validate_auth(payload.get("auth")))
            except ValueError as exc:
                self.auth_refresh.set_exception(exc)

    async def remote_loop(self) -> None:
        failures = 0
        ever_authenticated = False
        while True:
            remote_phase = "remote-connect"
            url = self.auth["urls"][failures % len(self.auth["urls"])]
            try:
                async with self.connector(url, origin="https://live.bilibili.com", open_timeout=3,
                                          close_timeout=1, ping_interval=None, max_size=MAX_FRAME_BYTES,
                                          user_agent_header=self.auth["userAgent"]) as ws:
                    auth = {"uid": self.auth["uid"], "roomid": int(self.room_id), "protover": 3,
                            "platform": "web", "type": 2, "key": self.auth["token"], "buvid": self.auth["buvid"]}
                    await ws.send(make_packet(7, json.dumps(auth, separators=(",", ":")).encode("utf-8")))
                    authenticated = False
                    remote_phase = "remote-auth"
                    heartbeat = None
                    auth_deadline = time.monotonic() + 3
                    try:
                        while True:
                            timeout = 65 if authenticated else max(0.01, auth_deadline - time.monotonic())
                            frame = await asyncio.wait_for(ws.recv(), timeout)
                            self.frames += 1
                            if not isinstance(frame, bytes):
                                raise ValueError("Bilibili WebSocket sent a non-binary frame")
                            for operation, payload in decode_packets(frame):
                                if operation == 8:
                                    if payload.get("code") != 0:
                                        code = payload.get("code")
                                        raise AuthRejected(bilibili_code=code if isinstance(code, int) and not isinstance(code, bool) else None)
                                    authenticated = ever_authenticated = True
                                    failures = 0
                                    self.status = "connected"
                                    remote_phase = "remote-stream"
                                    if not self.started.done():
                                        self.started.set_result(True)
                                    if heartbeat is None:
                                        heartbeat = asyncio.create_task(self._heartbeat(ws))
                                elif operation == 5 and authenticated:
                                    if str(payload.get("cmd", "")).split(":", 1)[0] == "DANMU_MSG":
                                        self.danmaku_events += 1
                                    message = normalize_danmaku(payload)
                                    if message:
                                        self.enqueue(message)
                                    elif str(payload.get("cmd", "")).split(":", 1)[0] == "DANMU_MSG":
                                        self.ignored_danmaku += 1
                            if heartbeat and heartbeat.done():
                                heartbeat.result()
                    finally:
                        if heartbeat:
                            heartbeat.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await heartbeat
            except asyncio.CancelledError:
                raise
            except AuthRejected as exc:
                self.last_connection_error = safe_failure(exc, remote_phase)
                if not ever_authenticated:
                    raise
                self.status = "refreshing-auth"
                await self.refresh_auth()
                failures = 0
            except Exception as exc:
                # Never log token-bearing URL/body/exception contents.
                LOG.info("Bilibili connection interrupted (%s), session=%s", type(exc).__name__, self.session_id)
                self.last_connection_error = safe_failure(exc, remote_phase)
                failures += 1
                self.reconnects += 1
                self.status = "reconnecting"
                self.dropped += len(self.pending)
                self.pending.clear()
                await asyncio.sleep(min(15, 0.5 * 2 ** min(failures - 1, 5)))

    async def attach_player(self) -> None:
        self.phase = "player-wait"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.remote_task.done():
                self.remote_task.result()
            try:
                version = await self.ipc.command(["get_property", "user-data/uosc_danmaku/live-api-version"])
                path = await self.ipc.command(["get_property", "path"])
                if version == 1 and path:
                    await self.ipc.command(["script-message-to", "uosc_danmaku", "live-start", self.session_id])
                    await asyncio.sleep(0.05)
                    if await self.ipc.command(["get_property", "user-data/uosc_danmaku/live-active"]):
                        self.player_attached = True
                        self.phase = "player-stream"
                        return
            except (OSError, RuntimeError, TimeoutError):
                pass
            await asyncio.sleep(0.25)
        raise TimeoutError("MPV did not expose the uosc_danmaku live API within 30 seconds")

    async def player_loop(self) -> None:
        await self.attach_player()
        playback_seen = False
        initial_path = await self.ipc.command(["get_property", "path"])
        active_switch = None
        last_switch_generation = 0
        while True:
            if self.remote_task.done():
                self.remote_task.result()
            live_active = await self.ipc.command(["get_property", "user-data/uosc_danmaku/live-active"])
            if not live_active:
                self.ended_reason = "mpv_live_session_ended"
                return
            live_session = await self.ipc.command(["get_property", "user-data/uosc_danmaku/live-session"])
            if live_session != self.session_id:
                self.ended_reason = "mpv_live_session_changed"
                return
            idle = await self.ipc.command(["get_property", "idle-active"])
            path = await self.ipc.command(["get_property", "path"])
            try:
                raw_switch = await self.ipc.command(["get_property", STREAM_SWITCH_PROPERTY])
            except RuntimeError:
                raw_switch = None  # Absent user-data property is normal outside a switch.
            marker = self.valid_stream_switch(raw_switch)
            if active_switch:
                if not marker or time.monotonic() > active_switch["deadline"] or any(marker[key] != active_switch[key] for key in ("generation", "from", "to", "startedAt", "expiresAt")):
                    self.ended_reason = "mpv_stream_switch_invalid"
                    return
            elif marker and marker["generation"] > last_switch_generation and marker["from"] == initial_path:
                if path and path not in (marker["from"], marker["to"]):
                    self.ended_reason = "mpv_file_changed"
                    return
                active_switch = {key: marker[key] for key in ("generation", "from", "to", "startedAt", "expiresAt")}
                active_switch["deadline"] = time.monotonic() + min(STREAM_SWITCH_SECONDS, marker["expiresAt"] - time.time())
                last_switch_generation = marker["generation"]
            if active_switch:
                if marker["stage"] == "loaded":
                    # The prior path/idle reads may predate file-loaded while
                    # the marker read already sees its new stage. Refresh the
                    # player state before deciding whether the target loaded.
                    path = await self.ipc.command(["get_property", "path"])
                    idle = await self.ipc.command(["get_property", "idle-active"])
                    if not await self.ipc.command(["get_property", "user-data/uosc_danmaku/live-active"]):
                        self.ended_reason = "mpv_live_session_ended"
                        return
                    if await self.ipc.command(["get_property", "user-data/uosc_danmaku/live-session"]) != self.session_id:
                        self.ended_reason = "mpv_live_session_changed"
                        return
                if path and path not in (active_switch["from"], active_switch["to"]):
                    self.ended_reason = "mpv_file_changed"
                    return
                # Messages timestamped against the old stream must not reappear
                # at zero when a live quality switch restarts MPV's time-pos.
                self.take_batch(paused=True)
                if marker["stage"] == "loading" or idle or not path or path != active_switch["to"]:
                    await asyncio.sleep(0.25)
                    continue
                initial_path = path
                await self.ipc.command(["set_property", STREAM_SWITCH_ACK_PROPERTY,
                                        {"sessionId": self.session_id, "generation": active_switch["generation"]}])
                active_switch = None
            if path:
                if initial_path and path != initial_path:
                    self.ended_reason = "mpv_file_changed"
                    return
                initial_path = path
            if not idle:
                playback_seen = True
            elif playback_seen:
                self.ended_reason = "mpv_became_idle"
                return
            paused = await self.ipc.command(["get_property", "pause"])
            batch = self.take_batch(paused or not playback_seen)
            if batch:
                await self.ipc.command(["script-message-to", "uosc_danmaku", "live-batch", self.session_id,
                                        json.dumps(batch, ensure_ascii=False, separators=(",", ":"))])
                self.sent += len(batch)
            await asyncio.sleep(0.25)

    def valid_stream_switch(self, marker: Any) -> dict[str, Any] | None:
        """Accept only a bounded, session-bound local quality transition.

        Stream URLs remain in memory and are never included in diagnostics.
        A generation authorizes exactly one old-to-new path transition.
        """
        if not isinstance(marker, dict) or marker.get("provider") != "bilibili" or marker.get("sessionId") != self.session_id:
            return None
        generation, expires, started = (marker.get(key) for key in ("generation", "expiresAt", "startedAt"))
        if isinstance(generation, bool) or not isinstance(generation, (int, float)) or not math.isfinite(generation) or generation <= 0 or generation != int(generation):
            return None
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (expires, started)):
            return None
        remaining = expires - time.time()
        if not 0 < remaining <= STREAM_SWITCH_SECONDS + 1 or started < 0 or marker.get("stage") not in ("loading", "loaded"):
            return None
        for key in ("from", "to"):
            value = marker.get(key)
            if not isinstance(value, str) or not re.match(r"^https?://[^/\s]+", value) or len(value) > 16384 or any(char in value for char in ("\x00", "\r", "\n")):
                return None
        return marker

    async def run(self) -> None:
        self.remote_task = asyncio.create_task(self.remote_loop())
        def remote_done(task):
            if not task.cancelled():
                exception = task.exception()
                if exception:
                    self.error = safe_failure(exception, "remote-stream" if self.started.done() else "remote-auth")
            if not self.started.done() and not task.cancelled():
                self.started.set_exception(RuntimeError("Bilibili danmaku authentication failed") if exception else RuntimeError("Bilibili connection ended"))
        self.remote_task.add_done_callback(remote_done)
        try:
            # Authentication completes before handoff; IPC only exists after launch.
            await asyncio.shield(self.started)
            await self.player_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "failed"
            self.error = self.error or safe_failure(exc, self.phase)
            LOG.warning("Bilibili session failed: session=%s room=%s diagnostic=%s", self.session_id, self.room_id, json.dumps(self.error))
            if not self.started.done():
                self.started.set_exception(RuntimeError("Bilibili danmaku connection failed"))
        finally:
            if not self.started.done():
                self.started.cancel()
            self.remote_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.remote_task
            if self.player_attached:
                with contextlib.suppress(Exception):
                    await self.ipc.command(["script-message-to", "uosc_danmaku", "live-stop", self.session_id])
            self.pending.clear()
            self.auth = {}
            if self.status != "failed":
                self.status = "stopped"
                self.ended_reason = self.ended_reason or "session_cancelled"
            with contextlib.suppress(Exception):
                await self.owner.send(json.dumps({"type": "danmakuStopped", "provider": "bilibili",
                                                  "sessionId": self.session_id, "status": self.status,
                                                  "error": self.error, "endedReason": self.ended_reason}))


class BilibiliDanmakuManager:
    def __init__(self, *, session_factory=BilibiliSession) -> None:
        self.sessions: dict[str, BilibiliSession] = {}
        self.session_factory = session_factory
        self.recent_failures: deque[dict[str, Any]] = deque(maxlen=8)
        self.start_requests = self.started_sessions = self.failed_starts = 0

    def snapshots(self) -> list[dict[str, Any]]:
        return [session.snapshot() for session in self.sessions.values()]

    def diagnostics(self) -> dict[str, Any]:
        return {"startRequests": self.start_requests, "startedSessions": self.started_sessions,
                "failedStarts": self.failed_starts, "recentFailures": list(self.recent_failures)}

    async def start(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        self.start_requests += 1
        try:
            room_id, session_id, pipe_name, auth = validate_start(payload)
        except ValueError as exc:
            self.failed_starts += 1
            self.recent_failures.append({"error": {"code": "bilibili_metadata_invalid", "phase": "request-validation", "message": str(exc)}, "timestamp": time.time()})
            raise
        LOG.info("Bilibili danmaku start requested: session=%s room=%s", session_id, room_id)
        for existing_id, session in list(self.sessions.items()):
            if session.task and session.task.done():
                self.sessions.pop(existing_id, None)
        if session_id in self.sessions:
            raise ValueError("Bilibili session id already exists")
        if len(self.sessions) >= 8:
            raise ValueError("too many active Bilibili sessions")
        session = self.session_factory(room_id, session_id, pipe_name, auth, owner)
        self.sessions[session_id] = session
        try:
            await session.start()
            self.started_sessions += 1
            return {"type": "danmakuStarted", "provider": "bilibili", "requestId": payload.get("requestId"),
                    "roomId": room_id, "sessionId": session_id, "apiVersion": 2}
        except BaseException as exc:
            self.failed_starts += 1
            self.recent_failures.append({**session.snapshot(), "timestamp": time.time()})
            if self.sessions.get(session_id) is session:
                self.sessions.pop(session_id, None)
            raise

    async def stop(self, session_id: str, owner=None) -> None:
        session = self.sessions.get(session_id)
        if session and (owner is None or session.owner is owner):
            self.sessions.pop(session_id, None)
            await session.stop()

    async def stop_owner(self, owner) -> None:
        for session_id, session in list(self.sessions.items()):
            if session.owner is owner:
                await self.stop(session_id, owner)

    async def close(self) -> None:
        for session_id in list(self.sessions):
            await self.stop(session_id)

    def update_auth(self, payload: dict[str, Any], owner) -> None:
        session = self.sessions.get(payload.get("sessionId"))
        if session and session.owner is owner:
            session.accept_auth(payload)
