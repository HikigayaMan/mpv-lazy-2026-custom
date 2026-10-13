#!/usr/bin/env python3
"""Local Telegram Web to MPV range bridge.

The HTTP side speaks enough Range semantics for mpv. The WebSocket side is
used by a userscript running inside Telegram Web, which performs credentialed
fetches against Telegram Web's own service-worker stream URLs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from bilibili_danmaku import BilibiliDanmakuManager, SessionFailure
from playback_progress import HttpProgressTransport, PlaybackProgressManager, ProgressStore
from web_playlist import WebPlaylistManager


LOG = logging.getLogger("telegram-web-mpv-bridge")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8999
DEFAULT_WS_PORT = 9000
DEFAULT_OPEN_ENDED_CHUNK = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0
BRIDGE_VERSION = "2026.10.12"
DEFAULT_MPV_PATH = r"F:\mpv_2026\mpv-lazy\mpv.exe"


@dataclass
class VideoInfo:
    url: str = ""
    title: str = "Telegram Web video"
    content_type: str = "video/mp4"
    size: int | None = None
    updated_at: float = 0.0


@dataclass
class PendingRequest:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    meta: dict[str, Any] | None = None
    data: bytes | None = None
    error: str | None = None


class BridgeState:
    def __init__(self, timeout: float, open_ended_chunk: int, mpv_path: str, http_host: str, http_port: int,
                 progress_store: ProgressStore | None = None) -> None:
        self.timeout = timeout
        self.open_ended_chunk = open_ended_chunk
        self.mpv_path = mpv_path
        self.http_host = http_host
        self.http_port = http_port
        self.video = VideoInfo()
        self.ws = None
        self.pending: dict[str, PendingRequest] = {}
        self.lock = asyncio.Lock()
        self.danmaku = BilibiliDanmakuManager()
        self.progress = PlaybackProgressManager(progress_store)
        self.playlists = WebPlaylistManager(self.progress, http_port)
        self.progress.playlist_registry = self.playlists
        self.progress_http = HttpProgressTransport(self.progress, self.playlists)
        self.browser_clients: set = set()
        self.browser_hello_count = 0

    @property
    def connected(self) -> bool:
        return self.ws is not None

    async def set_ws(self, ws) -> None:
        async with self.lock:
            self.ws = ws

    async def clear_ws(self, ws) -> None:
        async with self.lock:
            if self.ws is ws:
                self.ws = None

    async def register_video(self, payload: dict[str, Any]) -> None:
        url = str(payload.get("url") or "")
        if not url:
            raise ValueError("register payload missing url")
        size = payload.get("size")
        self.video = VideoInfo(
            url=url,
            title=str(payload.get("title") or "Telegram Web video"),
            content_type=str(payload.get("contentType") or "video/mp4"),
            size=int(size) if isinstance(size, (int, float)) and size > 0 else None,
            updated_at=time.time(),
        )
        LOG.info("registered video: title=%r size=%s type=%s", self.video.title, self.video.size, self.video.content_type)

    async def request_range(self, start: int, end: int | None) -> tuple[dict[str, Any], bytes]:
        ws = self.ws
        if ws is None:
            raise RuntimeError("Telegram Web userscript is not connected")
        if not self.video.url:
            raise RuntimeError("No Telegram Web video has been registered")

        request_id = uuid.uuid4().hex
        pending = PendingRequest()
        self.pending[request_id] = pending
        try:
            message: dict[str, Any] = {
                "type": "fetchRange",
                "id": request_id,
                "url": self.video.url,
                "start": start,
                "end": end,
                "openEndedChunk": self.open_ended_chunk,
            }
            await ws.send(json.dumps(message, separators=(",", ":")))
            await asyncio.wait_for(pending.event.wait(), timeout=self.timeout)
            if pending.error:
                raise RuntimeError(pending.error)
            if pending.meta is None or pending.data is None:
                raise RuntimeError("userscript returned incomplete response")
            return pending.meta, pending.data
        finally:
            self.pending.pop(request_id, None)

    async def handle_ws_message(self, raw: str | bytes, websocket=None) -> None:
        if isinstance(raw, bytes):
            if len(raw) < 4:
                LOG.warning("ignoring short binary frame")
                return
            header_len = int.from_bytes(raw[:4], "big")
            header = json.loads(raw[4 : 4 + header_len].decode("utf-8"))
            data = raw[4 + header_len :]
            request_id = str(header.get("id") or "")
            pending = self.pending.get(request_id)
            if pending is None:
                LOG.debug("binary response for expired request %s", request_id)
                return
            pending.meta = header
            pending.data = data
            pending.event.set()
            return

        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("WebSocket payload must be an object")
        msg_type = payload.get("type")
        if msg_type == "hello":
            self.browser_hello_count += 1
            LOG.info("userscript connected: %s", payload.get("userAgent", "unknown"))
        elif msg_type == "registerVideo":
            if websocket is not None:
                await self.set_ws(websocket)
            await self.register_video(payload)
        elif msg_type == "startDanmaku":
            if payload.get("provider") != "bilibili":
                raise ValueError("this bridge supports live danmaku provider bilibili")
            if websocket is None:
                raise ValueError("danmaku start requires a browser connection")
            # ws_handler dispatches this call concurrently so stop/refresh can
            # arrive while remote authentication is still in progress.
            reply = await self.danmaku.start(payload, websocket)
            await websocket.send(json.dumps(reply))
        elif msg_type == "stopDanmaku":
            await self.danmaku.stop(str(payload.get("sessionId") or ""), websocket)
        elif msg_type in ("updateDanmakuAuth", "danmakuAuthError"):
            if payload.get("provider") == "bilibili":
                self.danmaku.update_auth(payload, websocket)
        elif msg_type == "startPlaybackProgress":
            if websocket is None:
                raise ValueError("playback progress requires a browser connection")
            reply = await self.progress.start(payload, websocket, defer=True)
            # Even with WebSocket backpressure, the browser receives the
            # registration ACK before a first paused-player notification.
            try:
                await websocket.send(json.dumps(reply, ensure_ascii=False))
            finally:
                self.progress.activate(reply["sessionId"], websocket)
        elif msg_type in ("getPlaybackProgress", "updatePlaybackProgress"):
            if websocket is None:
                raise ValueError("playback progress requires a browser connection")
            method = {"getPlaybackProgress": self.progress.get, "updatePlaybackProgress": self.progress.update}[msg_type]
            await websocket.send(json.dumps(await method(payload, websocket), ensure_ascii=False))
        elif msg_type == "stopPlaybackProgress":
            await self.progress.stop(str(payload.get("sessionId") or ""), websocket)
        elif msg_type in ("registerWebPlaylist", "submitWebPlaylistItem", "getWebPlaylist", "stopWebPlaylist", "appendWebPlaylistItems", "commitWebPlaylist", "getWebPlaylistItems"):
            if websocket is None:
                raise ValueError("web playlist requires a browser connection")
            method = {"registerWebPlaylist": self.playlists.register, "submitWebPlaylistItem": self.playlists.submit,
                      "getWebPlaylist": self.playlists.get, "stopWebPlaylist": self.playlists.stop,
                      "appendWebPlaylistItems": self.playlists.append_items, "commitWebPlaylist": self.playlists.commit_upload,
                      "getWebPlaylistItems": self.playlists.get_items}[msg_type]
            await websocket.send(json.dumps(await method(payload, websocket), ensure_ascii=False))
        elif msg_type == "error":
            request_id = str(payload.get("id") or "")
            pending = self.pending.get(request_id)
            if pending is not None:
                pending.error = str(payload.get("message") or "userscript error")
                pending.event.set()
            else:
                LOG.warning("userscript error: %s", payload.get("message"))
        else:
            LOG.debug("ignored ws message type=%r", msg_type)

    def play_current(self) -> dict[str, Any]:
        if not self.video.url:
            raise RuntimeError("No Telegram Web video has been registered")
        if not os.path.exists(self.mpv_path):
            raise RuntimeError(f"mpv executable not found: {self.mpv_path}")
        stream_url = f"http://{self.http_host}:{self.http_port}/stream/current"
        args = [
            self.mpv_path,
            "--force-window=immediate",
            f"--force-media-title={self.video.title or 'Telegram Web video'}",
            stream_url,
        ]
        process = subprocess.Popen(args, cwd=os.path.dirname(self.mpv_path) or None)
        LOG.info("launched mpv pid=%s url=%s", process.pid, stream_url)
        return {"ok": True, "pid": process.pid, "url": stream_url, "title": self.video.title}


def parse_range(value: str | None, size: int | None, open_ended_chunk: int) -> tuple[int, int | None]:
    if not value:
        return 0, min(open_ended_chunk - 1, size - 1) if size else open_ended_chunk - 1
    if not value.startswith("bytes="):
        raise ValueError("unsupported Range header")
    first_range = value[len("bytes=") :].split(",", 1)[0].strip()
    start_text, _, end_text = first_range.partition("-")
    if not start_text:
        if not size:
            raise ValueError("suffix ranges require known size")
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        return max(0, size - suffix), size - 1
    start = int(start_text)
    if start < 0:
        raise ValueError("invalid range start")
    if end_text:
        end = int(end_text)
        if end < start:
            raise ValueError("invalid range end")
        return start, min(end, size - 1) if size else end
    if size:
        return start, min(size - 1, start + open_ended_chunk - 1)
    return start, start + open_ended_chunk - 1


def content_range_from_meta(meta: dict[str, Any], start: int, data_len: int, fallback_size: int | None) -> tuple[str, int | None, int, int]:
    content_range = meta.get("contentRange") or ""
    if isinstance(content_range, str) and content_range.startswith("bytes ") and "/" in content_range:
        try:
            range_part, total_part = content_range[6:].split("/", 1)
            start_text, end_text = range_part.split("-", 1)
            total = None if total_part == "*" else int(total_part)
            return content_range, total, int(start_text), int(end_text)
        except ValueError:
            pass
    end = start + max(0, data_len - 1)
    total_text = str(fallback_size) if fallback_size else "*"
    return f"bytes {start}-{end}/{total_text}", fallback_size, start, end


def http_response(status: int, reason: str, headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    headers = dict(headers or {})
    headers.setdefault("Content-Length", str(len(body)))
    headers.setdefault("Connection", "close")
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1") + body


async def handle_http_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: BridgeState) -> None:
    try:
        request_line = await reader.readline()
        if not request_line:
            return
        method, target, _version = request_line.decode("iso-8859-1").strip().split(" ", 2)
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("iso-8859-1").partition(":")
            headers[key.lower()] = value.strip()

        parsed = urlparse(target)
        if parsed.path == "/health":
            # Public capability checks carry no site credentials or playback
            # records. Keep this opt-in confined to /health; /status and all
            # media/progress traffic retain their existing access boundaries.
            health_headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
            }
            if method.upper() == "OPTIONS":
                health_headers["Access-Control-Allow-Private-Network"] = "true"
                writer.write(http_response(204, "No Content", health_headers))
                return
            if method.upper() != "GET":
                writer.write(http_response(405, "Method Not Allowed", {"Allow": "GET, OPTIONS"}))
                return
            body = json.dumps({"ok": True, "connected": state.connected, "protocol": 2,
                               "bridgeVersion": BRIDGE_VERSION,
                               "danmakuApiVersion": 2, "danmakuProviders": ["bilibili"],
                               "playbackProgressApiVersion": 1, "playbackProgressProviders": ["bilibili", "web"],
                               "playbackProgressHttpApiVersion": 1,
                               "webPlaylistApiVersion": 1,
                               "webPlaylistHierarchyApiVersion": 1, "webPlaylistChunkApiVersion": 1,
                               "browserConnected": bool(state.browser_clients),
                               "browserClients": len(state.browser_clients)}).encode("utf-8")
            writer.write(http_response(200, "OK", {"Content-Type": "application/json", **health_headers}, body))
            return
        if parsed.path == "/playback-progress":
            if method.upper() != "POST":
                writer.write(http_response(405, "Method Not Allowed", {"Allow": "POST"}))
                return
            status, body, activation = 200, None, None
            try:
                length = int(headers.get("content-length", "0"))
                if not 0 < length <= 256 * 1024 or headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                    raise ValueError("invalid progress request")
                envelope = json.loads(await asyncio.wait_for(reader.readexactly(length), 5))
                result, owner, session = await state.progress_http.request(envelope)
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                if session:
                    activation = (session, owner)
            except LookupError:
                status, body = 410, b'{"error":{"code":"playback_progress_client_expired"}}'
            except (ValueError, TypeError, asyncio.IncompleteReadError, TimeoutError):
                status, body = 400, b'{"error":{"code":"playback_progress_http_invalid"}}'
            except Exception:
                status, body = 503, b'{"error":{"code":"playback_progress_http_failed"}}'
            try:
                writer.write(http_response(status, HTTPStatus(status).phrase, {"Content-Type": "application/json"}, body))
                await writer.drain()
            except (ConnectionError, OSError):
                return
            finally:
                if activation:
                    state.progress.activate(*activation)
            return
        if parsed.path.startswith("/web-playlist/"):
            if method.upper() not in ("GET", "HEAD"):
                writer.write(http_response(405, "Method Not Allowed", {"Allow": "GET, HEAD"}))
                return
            components = parsed.path.split("/")
            if len(components) == 4 and components[3] == "list.m3u":
                try:
                    body = state.playlists.m3u(components[2])
                    response_headers = {"Content-Type": "application/vnd.apple.mpegurl; charset=utf-8",
                                        "Content-Length": str(len(body)), "Cache-Control": "no-store"}
                    writer.write(http_response(200, "OK", response_headers, body if method.upper() == "GET" else b""))
                except LookupError:
                    writer.write(http_response(404, "Not Found", body=b"web playlist unavailable"))
            else:
                # Stable item URLs are resolved exclusively by the installed
                # Lua on_load hook, never redirected to credentials or media.
                writer.write(http_response(410, "Gone", body=b"web playlist item requires MPV lazy resolution"))
            return
        if parsed.path == "/status":
            body = json.dumps({"connected": state.connected, "video": state.video.__dict__,
                               "bridgeVersion": BRIDGE_VERSION,
                               "danmaku": state.danmaku.snapshots(),
                               "playbackProgress": state.progress.snapshots(),
                               "webPlaylists": state.playlists.snapshots(),
                               "diagnostics": {"browserConnected": bool(state.browser_clients),
                                               "browserClients": len(state.browser_clients), "helloCount": state.browser_hello_count,
                                               **state.danmaku.diagnostics()}}, ensure_ascii=False).encode("utf-8")
            writer.write(http_response(200, "OK", {"Content-Type": "application/json; charset=utf-8"}, body))
            return
        if parsed.path == "/play/current":
            try:
                result = state.play_current()
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                writer.write(http_response(200, "OK", {"Content-Type": "application/json; charset=utf-8"}, body))
            except Exception as exc:  # noqa: BLE001 - local diagnostic endpoint
                body = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8")
                writer.write(http_response(500, "Internal Server Error", {"Content-Type": "application/json; charset=utf-8"}, body))
            return
        if parsed.path == "/stream/current.m3u":
            port = writer.get_extra_info("sockname")[1]
            body = f"#EXTM3U\nhttp://127.0.0.1:{port}/stream/current\n".encode("utf-8")
            writer.write(http_response(200, "OK", {"Content-Type": "audio/x-mpegurl"}, body))
            return
        if parsed.path != "/stream/current":
            writer.write(http_response(404, "Not Found", body=b"not found"))
            return

        query = parse_qs(parsed.query)
        video = state.video
        if not video.url:
            writer.write(http_response(503, "Service Unavailable", body=b"no video registered"))
            return
        start, end = parse_range(headers.get("range"), video.size, state.open_ended_chunk)
        meta, data = await state.request_range(start, end)
        content_range, total, actual_start, actual_end = content_range_from_meta(meta, start, len(data), video.size)
        if total and not video.size:
            video.size = total

        status = int(meta.get("status") or 206)
        if status == 200 and headers.get("range"):
            status = 206
        reason = HTTPStatus(status).phrase if status in HTTPStatus._value2member_map_ else "OK"
        response_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": str(meta.get("contentType") or video.content_type or "application/octet-stream"),
            "Content-Length": str(len(data)),
            "Content-Range": content_range,
            "X-Bridge-Range": f"{actual_start}-{actual_end}",
        }
        if "download" in query:
            response_headers["Content-Disposition"] = f'attachment; filename="{video.title or "telegram-video"}.mp4"'
        if method.upper() == "HEAD":
            writer.write(http_response(status, reason, response_headers, b""))
        else:
            writer.write(http_response(status, reason, response_headers, data))
        await writer.drain()
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic local bridge
        LOG.exception("http request failed: %s", exc)
        writer.write(http_response(500, "Internal Server Error", body=str(exc).encode("utf-8", "replace")))
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def ws_handler(websocket, state: BridgeState) -> None:
    state.browser_clients.add(websocket)
    LOG.info("browser relay WebSocket connected; clients=%s", len(state.browser_clients))
    tasks: set[asyncio.Task] = set()
    async def handle_message(message) -> None:
        try:
            await state.handle_ws_message(message, websocket)
        except Exception as exc:
            # Metadata validation exceptions contain only fixed diagnostic
            # strings; auth and connection exceptions are intentionally generic.
            payload = {}
            if isinstance(message, str):
                with contextlib.suppress(ValueError):
                    decoded = json.loads(message)
                    if isinstance(decoded, dict):
                        payload = decoded
            error = str(exc) if isinstance(exc, (ValueError, SessionFailure)) else "local bridge request failed"
            error_code = exc.code if isinstance(exc, SessionFailure) else "bridge_request_failed"
            with contextlib.suppress(ConnectionClosed):
                await websocket.send(json.dumps({"type": "error", "requestId": payload.get("requestId"),
                                                  "id": payload.get("id"), "message": error, "code": error_code}))
    try:
        async for message in websocket:
            payload = None
            if isinstance(message, str):
                with contextlib.suppress(ValueError):
                    payload = json.loads(message)
            if isinstance(payload, dict) and payload.get("type") == "startDanmaku":
                if len(tasks) >= 8:
                    await websocket.send(json.dumps({"type": "error", "requestId": payload.get("requestId"),
                                                      "message": "too many pending danmaku requests"}))
                    continue
                task = asyncio.create_task(handle_message(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            else:
                await handle_message(message)
    except ConnectionClosed:
        pass
    finally:
        state.browser_clients.discard(websocket)
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.clear_ws(websocket)
        state.progress.detach_owner(websocket)
        state.playlists.detach_owner(websocket)
        LOG.info("userscript disconnected")


async def main_async(args: argparse.Namespace) -> None:
    state = BridgeState(
        timeout=args.timeout,
        open_ended_chunk=args.open_ended_chunk,
        mpv_path=args.mpv_path,
        http_host=args.host,
        http_port=args.http_port,
    )
    http_server = await asyncio.start_server(
        lambda reader, writer: handle_http_client(reader, writer, state),
        args.host,
        args.http_port,
    )
    ws_server = await serve(lambda ws: ws_handler(ws, state), args.host, args.ws_port, max_size=args.max_ws_size)
    LOG.info("HTTP bridge: http://%s:%s", args.host, args.http_port)
    LOG.info("WebSocket bridge: ws://%s:%s/ws", args.host, args.ws_port)
    LOG.info("MPV URL: http://%s:%s/stream/current", args.host, args.http_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        await state.danmaku.close()
        await state.playlists.close()
        await state.progress.close()
        http_server.close()
        ws_server.close()
        await http_server.wait_closed()
        await ws_server.wait_closed()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge Telegram Web video and Bilibili live danmaku to local MPV")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT)
    parser.add_argument("--open-ended-chunk", type=int, default=DEFAULT_OPEN_ENDED_CHUNK)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--max-ws-size", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--mpv-path", default=os.environ.get("MPV_PATH", DEFAULT_MPV_PATH))
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # websockets DEBUG dumps frames, including the ephemeral auth metadata.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
