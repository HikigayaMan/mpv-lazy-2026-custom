"""Browser-authenticated lazy resolution of native MPV web playlists.

Only public content metadata is retained in the registry. Signed media URLs
live in the matching IPC reply and never enter a playlist file or resume store.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bilibili_danmaku import MpvPipe
from playback_progress import SAFE_SESSION, MpvIpcAvailability, canonical_page_url, ipc_error_summary, seconds, validate_identity

LOG = logging.getLogger("web-playlist")
API_VERSION = 1
REQUEST_PROPERTY = "user-data/mpv-lazy/web-playlist/request"
DATA_PROPERTY = "user-data/mpv-lazy/web-playlist"
PLAYLIST_ID = re.compile(r"playlist-[a-f0-9]{32}\Z")
ITEM_ID = re.compile(r"[1-9][0-9]{0,19}:[1-9][0-9]{0,19}\Z")
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
INLINE_MANIFEST_BYTES = 192 * 1024
PAGE_BYTES = 48 * 1024
MANIFEST_API_PROPERTY = "user-data/mpv-lazy/web-playlist/manifest-api-version"
MANIFEST_ACK_PROPERTY = "user-data/mpv-lazy/web-playlist/manifest-ack"
BASE_FIELDS = ("itemId", "aid", "cid", "bvid", "p", "identity", "title", "duration", "pageUrl")
HIERARCHY_FIELDS = ("sectionId", "sectionTitle", "videoId", "videoTitle", "partTitle", "partCount", "order")
COMPACT_FIELDS = ("itemId", "title", "duration", "sectionId", "sectionTitle", "videoId", "videoTitle", "partTitle", "p", "partCount", "order")
MAX_MEDIA_BYTES = 48 * 1024
MEDIA_DOMAINS = ("bilivideo.com", "bilivideo.cn", "bilivideo.net", "hdslb.com", "akamaized.net")
ERROR_CODES = {"playlist_resolve_failed", "playlist_resolve_timeout", "playlist_browser_disconnected",
               "playlist_media_unavailable", "playlist_stopped", "playlist_ipc_failed"}
SUBMIT_VALIDATION_CODES = {
    "web playlist request owner does not match": "owner_mismatch",
    "expired web playlist resolution": "generation_expired",
    "invalid web playlist resolved media": "media_invalid",
    "web playlist resolved identity does not match entry": "identity_mismatch",
    "invalid web playlist media URL": "media_url_invalid",
    "unsupported web playlist media host": "media_host_unsupported",
    "invalid web playlist variants": "variants_invalid",
    "invalid web playlist variant": "variant_invalid",
    "invalid web playlist variant metadata": "variant_metadata_invalid",
    "web playlist selected source is not in variants": "selected_variant_missing",
    "web playlist resolved media is too large": "media_too_large",
    "web playlist IPC reply is too large": "ipc_reply_too_large",
    "invalid web playlist resolution error": "error_code_invalid",
    "invalid web playlist text": "metadata_invalid",
    "invalid playback progress time": "position_invalid",
}


def public_text(value: Any, maximum: int = 360, *, optional: bool = False) -> str:
    if optional and value is None:
        return ""
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("invalid web playlist text")
    return value


def positive_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError("invalid web playlist content identifier")
    return str(value)


def media_url(value: Any, *, subtitle: bool = False) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("invalid web playlist media URL")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError("invalid web playlist media URL") from None
    if parsed.scheme not in ("http", "https") or not host or parsed.username is not None or parsed.password is not None or not parsed.path.startswith("/"):
        raise ValueError("invalid web playlist media URL")
    host = host.lower()
    if any(host == domain or host.endswith("." + domain) for domain in MEDIA_DOMAINS):
        return value
    if subtitle and host == "www.lckp.top" and parsed.scheme == "https" and port in (None, 443) and parsed.path == "/common/bilibili/jsonToSrt/":
        source = parse_qs(parsed.query).get("url", [])
        if len(source) == 1:
            media_url(source[0])
            return value
    raise ValueError("unsupported web playlist media host")


def validate_registration(payload: dict[str, Any]) -> None:
    playlist_id, session_id = payload.get("playlistId"), payload.get("sessionId")
    if not isinstance(playlist_id, str) or not PLAYLIST_ID.fullmatch(playlist_id):
        raise ValueError("invalid web playlist identifier")
    if not isinstance(session_id, str) or not SAFE_SESSION.fullmatch(session_id) or payload.get("pipeName") != "mpv-lazy-" + session_id:
        raise ValueError("invalid web playlist IPC session")


def optional_text(value: Any, default: str, maximum: int = 360) -> str:
    if value is None:
        return default
    if value == "":
        return ""
    return public_text(value, maximum)


def validate_leaf_items(raw_items: Any, *, offset: int = 0) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("invalid web playlist items")
    entries, seen = [], set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict) or set(raw) - set(BASE_FIELDS + HIERARCHY_FIELDS):
            raise ValueError("invalid web playlist entry")
        aid, cid = positive_id(raw.get("aid")), positive_id(raw.get("cid"))
        item_id = aid + ":" + cid
        if raw.get("itemId") != item_id or item_id in seen or ("identity" in raw and raw["identity"] != "bilibili:" + item_id):
            raise ValueError("invalid web playlist entry identity")
        bvid = raw.get("bvid")
        if not isinstance(bvid, str) or not re.fullmatch(r"BV[A-Za-z0-9]{1,30}", bvid):
            raise ValueError("invalid web playlist BV identifier")
        page = raw.get("p", 1)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 999999:
            raise ValueError("invalid web playlist page")
        page_url = canonical_page_url("bilibili", raw.get("pageUrl"))
        parsed = urlsplit(page_url)
        if parsed.path not in ("/video/" + bvid, "/video/av" + aid) or int(parse_qs(parsed.query).get("p", ["1"])[0]) != page:
            raise ValueError("web playlist page does not match entry")
        title = public_text(raw.get("title"))
        part_count, order = raw.get("partCount", page), raw.get("order", offset + index)
        if type(part_count) is not int or not page <= part_count <= 2**53 - 1 or type(order) is not int or not 0 <= order <= 2**53 - 1:
            raise ValueError("invalid web playlist hierarchy order")
        entries.append({"itemId": item_id, "aid": aid, "cid": cid, "bvid": bvid, "p": page,
                        "identity": "bilibili:" + item_id, "title": title,
                        "duration": seconds(raw.get("duration"), optional=True), "pageUrl": page_url,
                        "sectionId": optional_text(raw.get("sectionId"), "default", 128),
                        "sectionTitle": optional_text(raw.get("sectionTitle"), ""),
                        "videoId": optional_text(raw.get("videoId"), aid, 128),
                        "videoTitle": optional_text(raw.get("videoTitle"), title),
                        "partTitle": optional_text(raw.get("partTitle"), title), "partCount": part_count, "order": order})
        seen.add(item_id)
    return entries


def validate_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    validate_registration(payload)
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or len(raw_items) < 2:
        raise ValueError("invalid web playlist items")
    entries = validate_leaf_items(raw_items)
    seen = {entry["itemId"] for entry in entries}
    if len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ValueError("web playlist manifest is too large")
    selected = payload.get("selectedItemId")
    if selected not in seen:
        raise ValueError("web playlist selected item is missing")
    return entries, next(index for index, item in enumerate(entries) if item["itemId"] == selected)


def resolved_media(raw: Any, entry: dict[str, Any]) -> dict[str, Any]:
    allowed = set(BASE_FIELDS + HIERARCHY_FIELDS) | {"video", "audio", "subtitle", "position", "variants"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("invalid web playlist resolved media")
    for key in ("itemId", "aid", "cid", "bvid", "p", "identity", "pageUrl"):
        if key in raw and str(raw[key]) != str(entry[key]):
            raise ValueError("web playlist resolved identity does not match entry")
    media = {**entry, "video": media_url(raw.get("video")), "position": seconds(raw.get("position", 0))}
    if entry["duration"]:
        media["position"] = min(media["position"], entry["duration"])
    for key in ("audio", "subtitle"):
        if raw.get(key) is not None and raw.get(key) != "":
            media[key] = media_url(raw[key], subtitle=key == "subtitle")
    variants = raw.get("variants", [])
    if not isinstance(variants, list) or len(variants) > 16:
        raise ValueError("invalid web playlist variants")
    cleaned, seen = [], set()
    for raw_variant in variants:
        if not isinstance(raw_variant, dict) or set(raw_variant) - {"url", "audio_url", "quality", "title", "width", "height", "bandwidth", "fps", "codec", "preferred"}:
            raise ValueError("invalid web playlist variant")
        url = media_url(raw_variant.get("url"))
        if url in seen:
            continue
        variant = {"url": url}
        if raw_variant.get("audio_url"):
            variant["audio_url"] = media_url(raw_variant["audio_url"])
        for key, maximum in (("quality", 100000), ("width", 16384), ("height", 16384), ("bandwidth", 1000000000), ("fps", 240)):
            value = raw_variant.get(key)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum or (key != "fps" and int(value) != value):
                    raise ValueError("invalid web playlist variant metadata")
                variant[key] = value
        for key, maximum in (("title", 360), ("codec", 64)):
            if raw_variant.get(key):
                variant[key] = public_text(raw_variant[key], maximum)
        cleaned.append(variant)
        seen.add(url)
    if cleaned and media["video"] not in seen:
        raise ValueError("web playlist selected source is not in variants")
    media["variants"] = cleaned
    if len(json.dumps(media, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_MEDIA_BYTES:
        raise ValueError("web playlist resolved media is too large")
    return media


class WebPlaylistSession:
    def __init__(self, manager, payload: dict[str, Any], items: list[dict[str, Any]], selected_index: int, owner, *, ipc=None,
                 poll_interval: float = .35, attach_timeout: float = 40, resolve_timeout: float = 18) -> None:
        self.manager = manager
        self.playlist_id, self.session_id, self.pipe_name = payload["playlistId"], payload["sessionId"], payload["pipeName"]
        self.items, self.selected_index, self.owner = items, selected_index, owner
        self.entries = {item["itemId"]: item for item in items}
        self.initial_position = seconds(payload.get("initialPosition", 0))
        self.ipc = ipc or MpvPipe(self.pipe_name)
        self.poll_interval, self.attach_timeout, self.resolve_timeout = poll_interval, attach_timeout, resolve_timeout
        self.task = None
        self.attached = False
        self.phase, self.reason = "player-wait", ""
        self.pending = None
        self.generation = 0
        self.last_sent_owner = None
        self.request_count = 0
        self.current_item_id = items[selected_index]["itemId"]
        self.resolve_lock = asyncio.Lock()
        self.ipc_availability = MpvIpcAvailability()
        self.submissions_received = self.submissions_accepted = self.submissions_rejected = 0
        self.last_submit_error = None
        self.chunked = payload.get("chunked") is True
        self.revision = manager.next_revision()
        self.collection_id = optional_text(payload.get("collectionId"), items[selected_index]["aid"], 128)
        self.collection_title = optional_text(payload.get("collectionTitle"), items[selected_index]["videoTitle"])
        self.manifest_task = None
        self.manifest_stage, self.manifest_sent = "waiting", 0
        self.manifest_ack_timeout = 2.5

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    def description(self) -> dict[str, Any]:
        current = self.entries.get(self.current_item_id, self.items[self.selected_index])
        result = {"playlistId": self.playlist_id, "sessionId": self.session_id, "pipeName": self.pipe_name,
                "playlistUrl": self.manager.playlist_url(self.playlist_id), "selectedIndex": self.selected_index,
                "currentItemId": self.current_item_id, "currentItem": dict(current), "itemCount": len(self.items),
                "revision": self.revision, "collectionId": self.collection_id, "collectionTitle": self.collection_title,
                "hasHierarchy": True, "hierarchyApiVersion": 1}
        if not self.chunked:
            legacy = [{key: item[key] for key in BASE_FIELDS} for item in self.items]
            if len(json.dumps(legacy, ensure_ascii=False).encode()) <= INLINE_MANIFEST_BYTES:
                result["items"] = legacy
        return result

    async def manifest_exchange(self, name: str, envelope: dict[str, Any], stage: str) -> None:
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        command = ["script-message-to", "web_playlist", name, encoded]
        if len(json.dumps({"command": command, "request_id": 1}, ensure_ascii=False).encode()) > 32768:
            raise ValueError("web playlist manifest IPC chunk is too large")
        for _ in range(3):
            try:
                await self.ipc.command(command)
                deadline = time.monotonic() + self.manifest_ack_timeout
                while time.monotonic() < deadline:
                    ack = await self.ipc.command(["get_property", MANIFEST_ACK_PROPERTY])
                    if isinstance(ack, dict) and ack.get("playlistId") == self.playlist_id and ack.get("revision") == self.revision:
                        if ack.get("ok") is False:
                            raise ValueError("web playlist manifest was rejected")
                        expected_count = self.manifest_sent + len(envelope["items"]) if stage == "chunk" else envelope["itemCount"]
                        if ack.get("ok") is True and ack.get("stage") == stage and ack.get("itemCount") == expected_count and (stage != "chunk" or ack.get("chunkIndex") == envelope["chunkIndex"]):
                            return
                    await asyncio.sleep(.03)
            except (OSError, RuntimeError, TimeoutError):
                pass
        raise TimeoutError("web playlist manifest acknowledgement timed out")

    def manifest_chunks(self):
        chunk, index = [], 0
        for item in self.items:
            compact = {key: item[key] for key in COMPACT_FIELDS}
            candidate = chunk + [compact]
            envelope = {"playlistId": self.playlist_id, "revision": self.revision, "chunkIndex": index, "items": candidate}
            encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            wire = json.dumps({"command": ["script-message-to", "web_playlist", "web-playlist-manifest-chunk", encoded], "request_id": 1}, ensure_ascii=False).encode()
            if chunk and (len(wire) > 32768 or len(candidate) > 100):
                yield chunk
                index += 1
                chunk = [compact]
            else:
                chunk = candidate
        if chunk:
            yield chunk

    async def publish_manifest(self) -> None:
        try:
            deadline = time.monotonic() + 6
            while True:
                try:
                    version = await self.ipc.command(["get_property", MANIFEST_API_PROPERTY])
                    if not isinstance(version, bool) and version == 1:
                        break
                except (OSError, RuntimeError, ValueError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    self.manifest_stage = "unsupported"
                    return
                await asyncio.sleep(.1)
            shared = {"playlistId": self.playlist_id, "revision": self.revision}
            self.manifest_stage = "begin"
            await self.manifest_exchange("web-playlist-manifest-begin", {**shared, "itemCount": len(self.items),
                "collectionId": self.collection_id, "collectionTitle": self.collection_title}, "begin")
            chunks = 0
            for index, items in enumerate(self.manifest_chunks()):
                self.manifest_stage = "chunk"
                await self.manifest_exchange("web-playlist-manifest-chunk", {**shared, "chunkIndex": index, "items": items}, "chunk")
                self.manifest_sent += len(items)
                chunks += 1
                await asyncio.sleep(0)
            self.manifest_stage = "commit"
            await self.manifest_exchange("web-playlist-manifest-commit", {**shared, "itemCount": len(self.items), "chunkCount": chunks}, "commit")
            self.manifest_stage = "ready"
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, TimeoutError) as error:
            self.manifest_stage = "failed"
            diagnostic = ipc_error_summary(error)
            LOG.warning("web playlist manifest unavailable: session=%s kind=%s errno=%s winerror=%s", self.session_id,
                        diagnostic["kind"], diagnostic["errno"], diagnostic["winerror"])

    async def send(self, message: dict[str, Any]) -> None:
        owner = self.owner
        if owner is not None:
            try:
                await owner.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            except Exception:
                if self.owner is owner:
                    self.owner = None

    async def resolve_reply(self, item_id: str, generation: int, *, media=None, error=None) -> None:
        envelope = {"playlistId": self.playlist_id, "itemId": item_id, "generation": generation}
        envelope.update({"media": media} if media is not None else {"error": error or "playlist_resolve_failed"})
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        command = ["script-message-to", "web_playlist", "web-playlist-resolved", encoded]
        wire = json.dumps({"command": command, "request_id": 1}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(wire) > 65536:
            raise ValueError("web playlist IPC reply is too large")
        await self.ipc.command(command)

    async def accept_request(self, request: Any) -> None:
        async with self.resolve_lock:
            # A missing script property does not imply a dead player. Still
            # expire the last known hook request at its own bounded deadline.
            if self.pending is not None and time.monotonic() >= self.pending["deadline"]:
                pending = self.pending
                error = "playlist_resolve_timeout" if self.owner is not None else "playlist_browser_disconnected"
                try:
                    await self.resolve_reply(pending["itemId"], pending["generation"], error=error)
                except (OSError, RuntimeError, ValueError, TimeoutError) as ipc_error:
                    diagnostic = ipc_error_summary(ipc_error)
                    LOG.warning("web playlist timeout reply unavailable: session=%s kind=%s errno=%s winerror=%s",
                                self.session_id, diagnostic["kind"], diagnostic["errno"], diagnostic["winerror"])
                    # The Lua hook has a separate 20-second timeout. A failed
                    # timeout reply must not tear down an otherwise live list.
                self.pending = None
                self.phase = "player-progress"
            await self._accept_request(request)

    async def _accept_request(self, request: Any) -> None:
        if not isinstance(request, dict) or request.get("playlistId") != self.playlist_id:
            return
        item_id, generation = request.get("itemId"), request.get("generation")
        if not isinstance(item_id, str) or item_id not in self.entries or isinstance(generation, bool) or not isinstance(generation, int) or not 1 <= generation <= 2**53 - 1:
            return
        if generation < self.generation:
            return
        if generation > self.generation:
            self.generation = generation
            self.request_count += 1
            self.current_item_id = item_id
            self.pending = {"itemId": item_id, "generation": generation, "deadline": time.monotonic() + self.resolve_timeout,
                            "first": self.request_count == 1}
            self.last_sent_owner = None
        if self.pending is None or self.pending["itemId"] != item_id:
            return
        if self.owner is not None and self.last_sent_owner is not self.owner:
            entry = self.entries[item_id]
            progress = self.manager.progress.sessions.get(self.session_id)
            if progress is not None and progress.playlist_id == self.playlist_id:
                with contextlib.suppress(OSError, RuntimeError, ValueError, TimeoutError):
                    await progress.sample_progress()
            resume = self.manager.progress.store.get(entry["identity"])
            await self.send({"type": "resolveWebPlaylistItem", "playlistId": self.playlist_id, "sessionId": self.session_id,
                             "itemId": item_id, "generation": generation, "item": dict(entry), "resumeRecord": resume,
                             "first": self.pending["first"], "initialPosition": self.initial_position if self.pending["first"] else None})
            self.last_sent_owner = self.owner
            self.phase = "browser-resolve"

    async def submit(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        item = payload.get("itemId")
        item = item if isinstance(item, str) and ITEM_ID.fullmatch(item) else "invalid"
        generation = payload.get("generation")
        generation = generation if type(generation) is int and 1 <= generation <= 2**53 - 1 else None
        self.submissions_received += 1
        LOG.info("web playlist submit received: session=%s item=%s generation=%s", self.session_id, item, generation)
        try:
            async with self.resolve_lock:
                reply = await self._submit(payload, owner)
        except (OSError, RuntimeError, ValueError, TypeError, TimeoutError) as error:
            self.submissions_rejected += 1
            if isinstance(error, (ValueError, TypeError)):
                diagnostic = {"kind": "validation_error", "code": SUBMIT_VALIDATION_CODES.get(str(error), "invalid_request")}
            else:
                diagnostic = ipc_error_summary(error)
            self.last_submit_error = diagnostic
            LOG.warning("web playlist submit rejected: session=%s item=%s generation=%s kind=%s code=%s errno=%s winerror=%s",
                        self.session_id, item, generation, diagnostic["kind"], diagnostic.get("code"),
                        diagnostic.get("errno"), diagnostic.get("winerror"))
            raise
        self.submissions_accepted += 1
        LOG.info("web playlist submit accepted: session=%s item=%s generation=%s count=%s",
                 self.session_id, item, generation, self.submissions_accepted)
        return reply

    async def _submit(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        if owner is None or self.owner is not owner:
            raise ValueError("web playlist request owner does not match")
        item_id, generation = payload.get("itemId"), payload.get("generation")
        if self.pending is None or item_id != self.pending["itemId"] or isinstance(generation, bool) or generation != self.pending["generation"]:
            raise ValueError("expired web playlist resolution")
        if time.monotonic() >= self.pending["deadline"]:
            raise ValueError("expired web playlist resolution")
        if "error" in payload:
            if not isinstance(payload["error"], str) or payload["error"] not in ERROR_CODES:
                raise ValueError("invalid web playlist resolution error")
            await self.resolve_reply(item_id, generation, error=payload["error"])
        else:
            await self.resolve_reply(item_id, generation, media=resolved_media(payload.get("media"), self.entries[item_id]))
        self.pending = None
        self.phase = "player-progress"
        return {"type": "webPlaylistItemSubmitted", "requestId": payload.get("requestId"), "playlistId": self.playlist_id,
                "sessionId": self.session_id, "itemId": item_id, "generation": generation}

    async def run(self) -> None:
        deadline = time.monotonic() + self.attach_timeout
        try:
            while True:
                try:
                    data = await self.ipc.command(["get_property", DATA_PROPERTY])
                    request = data.get("request") if isinstance(data, dict) else None
                    self.attached = True
                    recovered = self.ipc_availability.success()
                    if recovered:
                        LOG.info("web playlist IPC recovered: session=%s attempts=%s", self.session_id, recovered)
                        if self.reason.startswith("ipc_retry_"):
                            self.reason = ""
                except (OSError, RuntimeError, ValueError, TimeoutError) as error:
                    if self.attached:
                        availability = await self.ipc_availability.failure(error, self.ipc)
                        diagnostic = self.ipc_availability.last_error
                        if self.ipc_availability.failures == 1 or availability in ("missing", "exhausted"):
                            LOG.warning("web playlist IPC unavailable: session=%s kind=%s errno=%s winerror=%s state=%s",
                                        self.session_id, diagnostic["kind"], diagnostic["errno"], diagnostic["winerror"], availability)
                        if availability in ("missing", "exhausted"):
                            self.reason = "player_closed" if availability == "missing" else "player_ipc_unresponsive"
                            return
                        self.reason = "ipc_retry_" + diagnostic["kind"]
                    request = None
                await self.accept_request(request)
                if self.attached and self.manifest_task is None:
                    # The first media resolve is emitted before metadata starts.
                    # Manifest acknowledgements never block playback polling.
                    self.manifest_task = asyncio.create_task(self.publish_manifest())
                if not self.attached and time.monotonic() >= deadline:
                    self.reason = "player_attach_timeout"
                    return
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.reason = "playlist_failed"
            LOG.warning("web playlist failed: session=%s", self.session_id)
        finally:
            if self.manifest_task is not None and not self.manifest_task.done():
                self.manifest_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.manifest_task
            self.phase = "stopped"
            self.reason = self.reason or "relay_closed"
            if self.manager.sessions.get(self.playlist_id) is self:
                self.manager.sessions.pop(self.playlist_id, None)
            await self.manager.progress.stop_playlist(self.playlist_id, self.reason)
            await self.send({"type": "webPlaylistStopped", "playlistId": self.playlist_id,
                             "sessionId": self.session_id, "reason": self.reason,
                             **({"ipcError": self.ipc_availability.last_error} if self.ipc_availability.last_error else {})})

    async def stop(self, reason="browser_stop") -> None:
        self.reason = reason
        if self.pending is not None:
            with contextlib.suppress(Exception):
                await self.resolve_reply(self.pending["itemId"], self.pending["generation"], error="playlist_stopped")
            self.pending = None
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class WebPlaylistManager:
    def __init__(self, progress, http_port: int, *, session_factory=WebPlaylistSession) -> None:
        self.progress, self.http_port, self.session_factory = progress, http_port, session_factory
        self.sessions: dict[str, WebPlaylistSession] = {}
        self.lock = asyncio.Lock()
        self.uploads = {}
        self._revision = 0

    def next_revision(self) -> int:
        self._revision = max(self._revision + 1, time.time_ns() // 1000)
        return self._revision

    def sweep_uploads(self) -> None:
        for key, upload in list(self.uploads.items()):
            if time.monotonic() - upload["lastSeen"] > 300:
                self.uploads.pop(key, None)

    async def begin_upload(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        validate_registration(payload)
        selected = payload.get("selectedItemId")
        if not isinstance(selected, str) or not ITEM_ID.fullmatch(selected):
            raise ValueError("web playlist selected item is missing")
        initial = seconds(payload.get("initialPosition", 0))
        collection_id = optional_text(payload.get("collectionId"), "", 128)
        collection_title = optional_text(payload.get("collectionTitle"), "")
        saved = {key: payload[key] for key in ("playlistId", "sessionId", "pipeName", "selectedItemId")}
        saved.update({"initialPosition": initial, "collectionId": collection_id, "collectionTitle": collection_title, "chunked": True})
        self.sweep_uploads()
        async with self.lock:
            playlist_id = payload["playlistId"]
            if playlist_id in self.sessions:
                raise ValueError("web playlist is already committed")
            upload = self.uploads.get(playlist_id)
            if upload is not None:
                if upload["owner"] not in (None, owner) or upload["payload"] != saved:
                    raise ValueError("web playlist upload belongs to another session")
                upload["owner"] = owner
            else:
                if len(self.sessions) + len(self.uploads) >= 8 or any(session.session_id == payload["sessionId"] for session in self.sessions.values()) or any(item["payload"]["sessionId"] == payload["sessionId"] for item in self.uploads.values()):
                    raise ValueError("web playlist session limit or IPC conflict")
                upload = self.uploads[playlist_id] = {"payload": saved, "owner": owner, "items": [], "seen": set(),
                    "chunks": {}, "bytes": 0, "lastSeen": time.monotonic()}
            upload["lastSeen"] = time.monotonic()
        return {"type": "webPlaylistUploadStarted", "requestId": payload.get("requestId"), "playlistId": playlist_id,
                "sessionId": payload["sessionId"], "nextChunkIndex": len(upload["chunks"]), "itemCount": len(upload["items"])}

    async def append_items(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        self.sweep_uploads()
        async with self.lock:
            upload = self.uploads.get(payload.get("playlistId"))
            if upload is None or owner is None or upload["owner"] is not owner:
                raise ValueError("web playlist upload owner does not match")
            index = payload.get("chunkIndex")
            if type(index) is not int or not 0 <= index <= len(upload["chunks"]):
                raise ValueError("invalid web playlist chunk index")
            raw = payload.get("items")
            if not isinstance(raw, list) or not 1 <= len(raw) <= 100 or len(json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode()) > PAGE_BYTES:
                raise ValueError("web playlist upload chunk is too large")
            # Hash the public raw chunk: retries retain their original offsets.
            digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if index < len(upload["chunks"]):
                if upload["chunks"].get(index) != digest:
                    raise ValueError("web playlist chunk retry does not match")
            else:
                entries = validate_leaf_items(raw, offset=len(upload["items"]))
                if any(entry["itemId"] in upload["seen"] for entry in entries):
                    raise ValueError("duplicate web playlist item")
                added_bytes = len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode())
                if upload["bytes"] + added_bytes > MAX_MANIFEST_BYTES:
                    raise ValueError("web playlist manifest is too large")
                upload["items"].extend(entries)
                upload["seen"].update(entry["itemId"] for entry in entries)
                upload["bytes"] += added_bytes
                upload["chunks"][index] = digest
            upload["lastSeen"] = time.monotonic()
        return {"type": "webPlaylistItemsAppended", "requestId": payload.get("requestId"), "playlistId": payload["playlistId"],
                "nextChunkIndex": len(upload["chunks"]), "itemCount": len(upload["items"])}

    async def commit_upload(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        self.sweep_uploads()
        if owner is None:
            raise ValueError("web playlist requires a browser owner")
        async with self.lock:
            session = self.sessions.get(payload.get("playlistId"))
            expected = payload.get("expectedItemCount")
            if type(expected) is not int or expected < 2:
                raise ValueError("invalid web playlist expected item count")
            if session is not None:
                if session.owner is not owner or len(session.items) != expected:
                    raise ValueError("web playlist commit does not match")
            else:
                upload = self.uploads.get(payload.get("playlistId"))
                if upload is None or upload["owner"] is not owner or len(upload["items"]) != expected:
                    raise ValueError("web playlist upload is incomplete")
                entries = upload["items"]
                selected = upload["payload"]["selectedItemId"]
                index = next((index for index, item in enumerate(entries) if item["itemId"] == selected), None)
                if index is None:
                    raise ValueError("web playlist selected item is missing")
                counts = {}
                pages = {}
                for item in entries:
                    key = (item["sectionId"], item["videoId"])
                    counts[key] = max(counts.get(key, 1), item["partCount"], item["p"])
                    pages.setdefault(key, []).append(item["p"])
                for key, numbers in pages.items():
                    if len(numbers) != counts[key] or len(set(numbers)) != len(numbers) or sorted(numbers) != list(range(1, counts[key] + 1)):
                        raise ValueError("web playlist video parts are incomplete")
                for item in entries:
                    item["partCount"] = counts[(item["sectionId"], item["videoId"])]
                session = self.session_factory(self, upload["payload"], entries, index, owner)
                self.sessions[session.playlist_id] = session
                self.uploads.pop(session.playlist_id, None)
                session.start()
        return {"type": "webPlaylistRegistered", "requestId": payload.get("requestId"), "apiVersion": API_VERSION, **session.description()}

    async def get_items(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        session = self.sessions.get(payload.get("playlistId"))
        if session is None or owner is None or session.owner is not owner:
            raise ValueError("web playlist page owner does not match")
        offset, limit = payload.get("offset", 0), payload.get("limit", 100)
        if type(offset) is not int or not 0 <= offset <= len(session.items) or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid web playlist page range")
        items = []
        header = {"type": "webPlaylistItems", "requestId": payload.get("requestId"), "playlistId": session.playlist_id,
                  "revision": session.revision, "itemCount": len(session.items), "offset": offset, "nextOffset": None, "items": []}
        size = len(json.dumps(header, ensure_ascii=False).encode())
        if size > PAGE_BYTES - 4096:
            raise ValueError("web playlist page request is too large")
        for item in session.items[offset:offset + limit]:
            item_size = len(json.dumps(item, ensure_ascii=False).encode()) + (2 if items else 0)
            if items and size + item_size > PAGE_BYTES - 16:
                break
            items.append(dict(item))
            size += item_size
        next_offset = offset + len(items)
        return {**header, "nextOffset": next_offset if next_offset < len(session.items) else None, "items": items}

    def playlist_url(self, playlist_id: str) -> str:
        return f"http://127.0.0.1:{self.http_port}/web-playlist/{playlist_id}/list.m3u"

    def m3u(self, playlist_id: str) -> bytes:
        session = self.sessions.get(playlist_id)
        if session is None or session.phase == "stopped":
            raise LookupError("web playlist is unavailable")
        base = self.playlist_url(playlist_id).rsplit("/", 1)[0]
        lines = ["#EXTM3U"]
        for item in session.items:
            lines.extend([f'#EXTINF:{int(item["duration"])},{item["title"]}', base + "/item/" + item["itemId"]])
        return ("\n".join(lines) + "\n").encode("utf-8")

    async def register(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        if owner is None:
            raise ValueError("web playlist requires a browser owner")
        self.sweep_uploads()
        if payload.get("chunked") is True:
            return await self.begin_upload(payload, owner)
        items, index = validate_items(payload)
        seconds(payload.get("initialPosition", 0))
        async with self.lock:
            session = self.sessions.get(payload["playlistId"])
            if session is not None:
                if session.owner is not owner or session.session_id != payload["sessionId"] or session.items != items or session.selected_index != index:
                    raise ValueError("web playlist already belongs to another session")
            else:
                if len(self.sessions) + len(self.uploads) >= 8 or any(item.session_id == payload["sessionId"] for item in self.sessions.values()) or any(upload["payload"]["sessionId"] == payload["sessionId"] for upload in self.uploads.values()):
                    raise ValueError("web playlist session limit or IPC conflict")
                session = self.session_factory(self, payload, items, index, owner)
                self.sessions[session.playlist_id] = session
                session.start()
        return {"type": "webPlaylistRegistered", "requestId": payload.get("requestId"), "apiVersion": API_VERSION, **session.description()}

    async def submit(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        session = self.sessions.get(payload.get("playlistId"))
        if session is None:
            raise ValueError("web playlist is unavailable")
        return await session.submit(payload, owner)

    async def rebind(self, playlist_id: str, owner) -> None:
        session = self.sessions.get(playlist_id)
        if session is None or owner is None or session.owner is owner:
            return
        previous = session.owner
        if previous is not None:
            await session.send({"type": "webPlaylistStopped", "playlistId": playlist_id, "sessionId": session.session_id,
                                "reason": "browser_rebound"})
        session.owner = owner
        session.last_sent_owner = None
        progress = self.progress.sessions.get(session.session_id)
        if progress is not None and progress.playlist_id == playlist_id:
            if previous is not None and progress.owner is previous:
                await progress.send({"type": "playbackProgressStopped", "playlistId": playlist_id,
                                     "sessionId": session.session_id, "identity": progress.identity,
                                     "provider": progress.provider, "reason": "browser_rebound"})
            progress.owner = owner

    async def get(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        identity = payload.get("identity")
        validate_identity({"provider": "bilibili", "identity": identity})
        requested = payload.get("playlistId")
        if requested is not None and (not isinstance(requested, str) or not PLAYLIST_ID.fullmatch(requested)):
            raise ValueError("invalid web playlist identifier")
        candidates = [session for session in self.sessions.values() if session.phase != "stopped" and
                      any(item["identity"] == identity for item in session.items) and (requested is None or requested == session.playlist_id)]
        # Prefer this tab's session when two independent players contain the same video.
        candidates.sort(key=lambda session: session.owner is not owner)
        for session in candidates:
            if session.owner is None or payload.get("rebind") is True:
                await self.rebind(session.playlist_id, owner)
            if session.owner is owner:
                return {"type": "webPlaylistState", "requestId": payload.get("requestId"), "apiVersion": API_VERSION,
                        "identity": identity, **session.description()}
        return {"type": "webPlaylistState", "requestId": payload.get("requestId"), "apiVersion": API_VERSION,
                "identity": identity, "playlistId": None}

    def progress_entries(self, payload: dict[str, Any], owner) -> tuple[dict[str, dict], dict]:
        playlist_id = payload.get("playlistId")
        session = self.sessions.get(playlist_id)
        if session is None or session.owner is not owner or session.session_id != payload.get("sessionId") or session.pipe_name != payload.get("pipeName"):
            raise ValueError("unregistered web playlist progress session")
        entries = {item["identity"]: dict(item) for item in session.items}
        if payload.get("identity") not in entries:
            raise ValueError("web playlist progress identity is missing")
        return entries, session.description()

    def detach_owner(self, owner) -> None:
        for session in self.sessions.values():
            if session.owner is owner:
                session.owner = None
                session.last_sent_owner = None
        for upload in self.uploads.values():
            if upload["owner"] is owner:
                upload["owner"] = None

    async def stop(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        playlist_id = payload.get("playlistId")
        session = self.sessions.get(playlist_id)
        upload = self.uploads.get(playlist_id)
        if upload is not None and owner is not None and upload["owner"] is owner:
            self.uploads.pop(playlist_id, None)
        if session is not None and owner is not None and session.owner is owner:
            await session.stop("browser_stop")
            self.sessions.pop(playlist_id, None)
        return {"type": "webPlaylistStopped", "requestId": payload.get("requestId"), "playlistId": playlist_id,
                "sessionId": session.session_id if session else upload["payload"]["sessionId"] if upload else payload.get("sessionId"), "reason": "browser_stop"}

    def snapshots(self) -> list[dict[str, Any]]:
        return [{"playlistId": session.playlist_id, "sessionId": session.session_id, "phase": session.phase,
                 "itemCount": len(session.items), "currentItemId": session.current_item_id,
                 "browserConnected": session.owner is not None, "reason": session.reason,
                 "ipc": session.ipc_availability.snapshot(),
                 "submissions": {"received": session.submissions_received, "accepted": session.submissions_accepted,
                                 "rejected": session.submissions_rejected, "lastError": session.last_submit_error},
                 "manifest": {"revision": session.revision, "stage": session.manifest_stage, "sentItemCount": session.manifest_sent}}
                for session in self.sessions.values()]

    async def close(self) -> None:
        async with self.lock:
            sessions = list(self.sessions.values())
            for session in sessions:
                await session.stop("relay_closed")
            self.sessions.clear()
            self.uploads.clear()
