"""Local, account-free resume records and session-bound MPV progress polling.

Only the purpose-built Lua snapshot is read from MPV. Stream URLs, browser
cookies and Bilibili account credentials never enter the progress store.
"""

from __future__ import annotations

import asyncio
from collections import deque
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from bilibili_danmaku import MpvPipe


LOG = logging.getLogger("playback-progress")
API_VERSION = 1
SNAPSHOT_PROPERTY = "user-data/mpv-lazy/playback-progress"
FINALS_PROPERTY = "user-data/mpv-lazy/playback-progress-finals"
MAX_RECORDS = 1000
MAX_STORE_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 366 * 24 * 60 * 60
SAFE_SESSION = re.compile(r"^progress-[A-Za-z0-9_-]{1,43}$")
IDENTITIES = {
    "bilibili": re.compile(r"^bilibili:[1-9][0-9]{0,19}:[1-9][0-9]{0,19}$"),
    "web": re.compile(r"^web:(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{64})$"),
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ipc_error_summary(error: Exception) -> dict[str, Any]:
    """Fixed diagnostics only; IPC exceptions may include a signed media URL."""
    error_number = getattr(error, "errno", None)
    windows_number = getattr(error, "winerror", None)
    missing = isinstance(error, FileNotFoundError) or error_number == 2 or windows_number in (2, 3)
    if missing:
        kind = "pipe_missing"
    elif isinstance(error, TimeoutError):
        kind = "write_timeout" if str(error) == "MPV IPC write timed out" else "response_timeout"
    elif windows_number == 231 or error_number == 16:
        kind = "pipe_busy"
    elif windows_number in (109, 232, 233) or error_number == 32:
        kind = "pipe_disconnected"
    elif isinstance(error, RuntimeError):
        kind = "property_unavailable" if str(error).startswith("MPV IPC command failed: property") else "command_unavailable"
    elif isinstance(error, ValueError):
        kind = "invalid_response"
    else:
        kind = "pipe_error"
    return {"kind": kind, "errno": error_number if type(error_number) is int else None,
            "winerror": windows_number if type(windows_number) is int else None}


class MpvIpcAvailability:
    """Bound retries while distinguishing a missing pipe from transient IPC."""
    def __init__(self, retry_timeout: float = 6) -> None:
        self.retry_timeout = retry_timeout
        self.failures = 0
        self.failure_started = None
        self.last_error = None

    async def failure(self, error: Exception, ipc) -> str:
        self.failures += 1
        self.last_error = ipc_error_summary(error)
        if self.last_error["kind"] == "pipe_missing":
            return "missing"
        if self.failure_started is None:
            self.failure_started = time.monotonic()
        try:
            # A script property can disappear temporarily during on_load. A
            # successful core-property read proves the player is still alive.
            pid = await ipc.command(["get_property", "pid"])
        except (OSError, RuntimeError, ValueError, TimeoutError) as probe_error:
            probe = ipc_error_summary(probe_error)
            if probe["kind"] == "pipe_missing":
                self.last_error = probe
                return "missing"
        else:
            if type(pid) is int and pid > 0:
                self.failure_started = None
                return "alive"
        return "exhausted" if time.monotonic() - self.failure_started >= self.retry_timeout else "retry"

    def success(self) -> int:
        failures = self.failures
        self.failures, self.failure_started = 0, None
        return failures

    def snapshot(self) -> dict[str, Any]:
        return {"failureCount": self.failures, "lastError": self.last_error}


def default_store_path() -> Path:
    # PyInstaller's __file__ points into a temporary extraction directory.
    directory = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return directory / "playback-progress.json"


def validate_identity(payload: dict[str, Any]) -> tuple[str, str]:
    provider, identity = payload.get("provider"), payload.get("identity")
    if not isinstance(provider, str) or provider not in IDENTITIES or not isinstance(identity, str) or not IDENTITIES[provider].fullmatch(identity):
        raise ValueError("invalid playback progress identity")
    return provider, identity


def canonical_page_url(provider: str, value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid playback progress page URL")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("invalid playback progress page URL") from None
    if url.scheme not in ("https", "http") or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("invalid playback progress page URL")
    if provider == "bilibili":
        if url.hostname != "www.bilibili.com" or port not in (None, 80, 443):
            raise ValueError("invalid Bilibili playback progress page")
        if not re.fullmatch(r"/video/(?:BV[A-Za-z0-9]+|av[1-9][0-9]*)/?|/bangumi/play/(?:ep|ss)[1-9][0-9]*/?|/list/[A-Za-z0-9/_-]+/?|/festival/[A-Za-z0-9/_-]+/?", url.path):
            raise ValueError("invalid Bilibili playback progress page")
        query = parse_qs(url.query)
        page = query.get("p", [""])[0]
        suffix = urlencode({"p": page}) if re.fullmatch(r"[1-9][0-9]{0,5}", page) else ""
        return urlunsplit(("https", "www.bilibili.com", url.path.rstrip("/") or "/", suffix, ""))
    # The identity contains the complete content key. Retaining public origin
    # and path is enough to identify the source without saving signed queries.
    host = url.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    authority = host + (f":{port}" if port is not None else "")
    return urlunsplit((url.scheme, authority, url.path or "/", "", ""))


def seconds(value: Any, *, optional: bool = False) -> float:
    if optional and value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= MAX_SECONDS:
        raise ValueError("invalid playback progress time")
    return float(value)


def event_timestamp(value: Any) -> int:
    if value is None:
        return now_ms()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid playback progress timestamp")
    # A browser clock cannot create an indefinitely unbeatable future record.
    return min(int(value), now_ms())


def make_record(payload: dict[str, Any], source: str) -> dict[str, Any]:
    provider, identity = validate_identity(payload)
    duration = seconds(payload.get("duration"), optional=True)
    position = seconds(payload.get("position"))
    if duration:
        position = min(position, duration)
    for field in ("paused", "ended", "completed"):
        if field in payload and not isinstance(payload[field], bool):
            raise ValueError("invalid playback progress state")
    return {
        "provider": provider, "identity": identity,
        "pageUrl": canonical_page_url(provider, payload.get("pageUrl")),
        "position": position, "duration": duration,
        "paused": bool(payload.get("paused", False)), "ended": bool(payload.get("ended", False)),
        "completed": bool(payload.get("completed", False)),
        "updatedAt": event_timestamp(payload.get("updatedAt")), "source": source,
    }


class ProgressStore:
    """A bounded in-memory map with an atomic on-disk representation."""

    def __init__(self, path: Path | str | None = None, *, max_records: int = MAX_RECORDS) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self.max_records = max_records
        self.records: dict[str, dict[str, Any]] = {}
        self.dirty = False
        self.lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            if self.path.stat().st_size > MAX_STORE_BYTES:
                return
            document = json.loads(self.path.read_text(encoding="utf-8"))
            entries = document.get("records") if isinstance(document, dict) else None
            if not isinstance(entries, dict):
                return
            for key, raw in entries.items():
                try:
                    if not isinstance(raw, dict) or raw.get("source") not in ("web", "mpv"):
                        continue
                    record = make_record(raw, raw["source"])
                    if key == record["identity"]:
                        self.records[key] = record
                except ValueError:
                    continue
            self._prune()
        except (OSError, ValueError, UnicodeError):
            # Never rewrite a malformed store merely by starting the relay.
            return

    def _prune(self) -> None:
        if len(self.records) > self.max_records:
            oldest = sorted(self.records, key=lambda key: self.records[key]["updatedAt"])
            for key in oldest[: len(self.records) - self.max_records]:
                self.records.pop(key, None)
        # Long public page paths must not defeat the total file size bound.
        size = len(json.dumps(self.records, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_STORE_BYTES - 128:
            for key in sorted(self.records, key=lambda value: self.records[value]["updatedAt"]):
                raw = self.records.pop(key)
                size -= len(json.dumps({key: raw}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) - 1
                if size <= MAX_STORE_BYTES - 128:
                    break

    def get(self, identity: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.records.get(identity)
            return dict(record) if record is not None else None

    def put(self, record: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        with self.lock:
            previous = self.records.get(record["identity"])
            if previous and not force and previous["updatedAt"] > record["updatedAt"]:
                return dict(previous)
            if record != previous:
                self.records[record["identity"]] = dict(record)
                self._prune()
                self.dirty = True
            return dict(self.records.get(record["identity"], record))

    def flush(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"apiVersion": API_VERSION, "records": self.records}, ensure_ascii=False,
                              separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(data) > MAX_STORE_BYTES:
                raise ValueError("playback progress store exceeds its size limit")
            temporary = self.path.with_name(self.path.name + ".tmp")
            try:
                with temporary.open("wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self.dirty = False
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()


class PlaybackProgressSession:
    def __init__(self, payload: dict[str, Any], owner, store: ProgressStore, *, ipc=None,
                 poll_interval: float = 1.0, attach_timeout: float = 30.0, flush_interval: float = 5.0) -> None:
        self.provider, self.identity = validate_identity(payload)
        self.session_id = payload["sessionId"]
        self.pipe_name = payload["pipeName"]
        self.page_url = canonical_page_url(self.provider, payload["pageUrl"])
        # Only PlaybackProgressManager can inject this manifest, after consulting
        # the live browser-owned registry. Never trust a caller-provided mapping.
        self.playlist_id = payload.get("playlistId")
        self.playlist_entries = payload.get("_playlistEntries", {})
        self.playlist = payload.get("_playlist")
        self.final_sequence = 0
        self.owner = owner
        self.store = store
        self.ipc = ipc or MpvPipe(self.pipe_name)
        self.poll_interval = poll_interval
        self.attach_timeout = attach_timeout
        self.flush_interval = flush_interval
        self.task: asyncio.Task | None = None
        self.record = store.get(self.identity)
        self.sequence = 0
        self.phase = "player-wait"
        self.reason = ""
        self.last_state = None
        self.last_flush = 0.0
        self.attached = False
        self.sample_lock = asyncio.Lock()
        self.ipc_availability = MpvIpcAvailability()

    def start(self) -> None:
        self.task = asyncio.create_task(self.run())

    async def stop(self, reason: str = "stopped") -> None:
        self.reason = reason
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.phase != "stopped":
            self.phase = "stopped"
            await self.send({"type": "playbackProgressStopped", "provider": self.provider,
                             "identity": self.identity, "sessionId": self.session_id, "reason": self.reason,
                             **({"playlistId": self.playlist_id} if self.playlist_id else {})})

    async def send(self, message: dict[str, Any]) -> None:
        owner = self.owner
        if owner is not None:
            try:
                await owner.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            except Exception:
                if self.owner is owner:
                    self.owner = None

    def notification(self) -> dict[str, Any] | None:
        if self.record is None:
            return None
        return {"type": "playbackProgress", **self.record, "sessionId": self.session_id, "sequence": self.sequence,
                **({"playlistId": self.playlist_id} if self.playlist_id else {})}

    async def accept_snapshot(self, snapshot: Any) -> str:
        if not isinstance(snapshot, dict) or isinstance(snapshot.get("apiVersion"), bool) or snapshot.get("apiVersion") != API_VERSION:
            return "invalid" if self.attached else "waiting"
        if snapshot.get("sessionId") != self.session_id:
            return "changed" if self.attached else "waiting"
        identity = snapshot.get("identity")
        if not isinstance(identity, str):
            return "invalid" if self.attached else "waiting"
        if self.playlist_id:
            if snapshot.get("playlistId") != self.playlist_id or identity not in self.playlist_entries:
                return "changed" if self.attached else "waiting"
            page_url = self.playlist_entries[identity]["pageUrl"]
        else:
            if identity != self.identity:
                return "changed" if self.attached else "waiting"
            page_url = self.page_url
        if snapshot.get("active") is not True:
            return "inactive" if self.attached else "waiting"
        if snapshot.get("ready") is not True:
            return "waiting"
        try:
            record = make_record({**snapshot, "provider": self.provider, "identity": identity,
                                  "pageUrl": page_url, "updatedAt": now_ms()}, "mpv")
        except ValueError:
            return "invalid" if self.attached else "waiting"
        # Missing/zero duration may be a loading state or a live stream. This
        # path is only for finite videos, so neither produces resume records.
        if record["duration"] <= 0:
            return "waiting"
        self.attached = True
        self.phase = "player-progress"
        self.identity, self.page_url = identity, page_url
        state = tuple(record[key] for key in ("position", "duration", "paused", "ended", "completed"))
        if self.playlist_id:
            state = (identity,) + state
        changed = state != self.last_state
        old_state = self.last_state
        if changed:
            self.last_state = state
            self.record = self.store.put(record)
            self.sequence += 1
            await self.send(self.notification())
        position_index = 1 if self.playlist_id else 0
        immediate = old_state is None or (changed and (
            (self.playlist_id and state[0] != old_state[0]) or
            state[position_index + 2:] != old_state[position_index + 2:] or
            abs(state[position_index] - old_state[position_index]) > 3))
        if immediate or time.monotonic() - self.last_flush >= self.flush_interval:
            await asyncio.to_thread(self.store.flush)

            self.last_flush = time.monotonic()
        # EOF closes this item, while the same IPC pipe continues with another
        # native playlist entry (or loops the current one).
        return "ended" if record["ended"] and not self.playlist_id else "ok"

    async def accept_finals(self, finals: Any) -> None:
        if not self.playlist_id or not isinstance(finals, list) or len(finals) > 32:
            return
        pending = []
        for item in finals:
            if not isinstance(item, dict):
                continue
            sequence = item.get("finalSequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or not self.final_sequence < sequence <= 2**53 - 1:
                continue
            pending.append((sequence, item.get("snapshot")))
        for sequence, snapshot in sorted(pending, key=lambda item: item[0]):
            if sequence <= self.final_sequence:
                continue
            # Invalid or foreign final snapshots cannot advance the watermark.
            if await self.accept_snapshot(snapshot) == "ok":
                self.final_sequence = sequence

    async def sample_progress(self) -> str:
        # A new on_load request may precede the periodic poll. The lazy resolver
        # uses this same lock to consume EOF finals before choosing resume time.
        async with self.sample_lock:
            if self.playlist_id:
                await self.accept_finals(await self.ipc.command(["get_property", FINALS_PROPERTY]))
            return await self.accept_snapshot(await self.ipc.command(["get_property", SNAPSHOT_PROPERTY]))

    async def run(self) -> None:
        deadline = time.monotonic() + self.attach_timeout
        try:
            while True:
                try:
                    outcome = await self.sample_progress()
                    recovered = self.ipc_availability.success()
                    if recovered:
                        LOG.info("playback progress IPC recovered: session=%s attempts=%s", self.session_id, recovered)
                        if self.reason.startswith("ipc_retry_"):
                            self.reason = ""
                except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                    if self.attached:
                        availability = await self.ipc_availability.failure(error, self.ipc)
                        diagnostic = self.ipc_availability.last_error
                        if self.ipc_availability.failures == 1 or availability in ("missing", "exhausted"):
                            LOG.warning("playback progress IPC unavailable: session=%s kind=%s errno=%s winerror=%s state=%s",
                                        self.session_id, diagnostic["kind"], diagnostic["errno"], diagnostic["winerror"], availability)
                        if availability in ("missing", "exhausted"):
                            self.reason = "player_closed" if availability == "missing" else "player_ipc_unresponsive"
                            return
                        self.reason = "ipc_retry_" + diagnostic["kind"]
                    outcome = "waiting"
                if outcome not in ("ok", "waiting"):
                    self.reason = {"changed": "player_file_changed", "inactive": "player_file_changed",
                                   "invalid": "player_snapshot_invalid", "ended": "playback_ended"}[outcome]
                    return
                if not self.attached and time.monotonic() >= deadline:
                    self.reason = "player_attach_timeout"
                    return
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.reason = "progress_failed"
            LOG.warning("playback progress session failed: session=%s", self.session_id)
        finally:
            self.phase = "stopped"
            self.reason = self.reason or "relay_closed"
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.store.flush)
            await self.send({"type": "playbackProgressStopped", "provider": self.provider,
                             "identity": self.identity, "sessionId": self.session_id, "reason": self.reason,
                             **({"playlistId": self.playlist_id} if self.playlist_id else {}),
                             **({"ipcError": self.ipc_availability.last_error} if self.ipc_availability.last_error else {})})

    def snapshot(self) -> dict[str, Any]:
        return {"provider": self.provider, "identity": self.identity, "sessionId": self.session_id,
                "phase": self.phase, "browserConnected": self.owner is not None,
                "position": self.record.get("position") if self.record else None,
                **({"playlistId": self.playlist_id} if self.playlist_id else {}),
                "reason": self.reason, "ipc": self.ipc_availability.snapshot()}


class PlaybackProgressManager:
    def __init__(self, store: ProgressStore | None = None, *, session_factory=PlaybackProgressSession) -> None:
        self.store = store or ProgressStore()
        self.session_factory = session_factory
        self.sessions: dict[str, PlaybackProgressSession] = {}
        self.lock = asyncio.Lock()
        self.playlist_registry = None

    def _active(self, identity: str) -> list[PlaybackProgressSession]:
        return [session for session in self.sessions.values()
                if (session.identity == identity or identity in session.playlist_entries) and session.phase != "stopped"
                and (session.task is None or not session.task.done())]

    async def get(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        provider, identity = validate_identity(payload)
        canonical_page_url(provider, payload.get("pageUrl"))
        active = self._active(identity)
        session_id = None
        for session in active:
            if session.owner is None:
                session.owner = owner
                if session.playlist_id and self.playlist_registry is not None:
                    await self.playlist_registry.rebind(session.playlist_id, owner)
            if session.owner is owner:
                session_id = session.session_id
        session = self.sessions.get(session_id) if session_id else None
        playlist = None
        if session and session.playlist_id and self.playlist_registry is not None:
            registered = self.playlist_registry.sessions.get(session.playlist_id)
            if registered is not None and registered.owner is owner:
                playlist = registered.description()
        return {"type": "playbackProgressState", "requestId": payload.get("requestId"), "apiVersion": API_VERSION,
                "provider": provider, "identity": identity, "record": self.store.get(identity),
                **({"activeSessionId": session_id} if session_id else {}),
                **({"playlistId": session.playlist_id, "playlist": playlist} if playlist else {})}

    async def update(self, payload: dict[str, Any], owner) -> dict[str, Any]:
        record = make_record(payload, "web")
        if "takeover" in payload and not isinstance(payload["takeover"], bool):
            raise ValueError("invalid playback progress takeover")
        async with self.lock:
            active = self._active(record["identity"])
            if active and not payload.get("takeover"):
                saved = self.store.get(record["identity"])
            else:
                # Await cancellation and its final flush before saving the new
                # web position; old MPV finalizers cannot overwrite takeover.
                for session in active:
                    self.sessions.pop(session.session_id, None)
                    await session.stop("web_takeover")
                saved = self.store.put(record, force=bool(payload.get("takeover")))
                await asyncio.to_thread(self.store.flush)
        return {"type": "playbackProgressSaved", "requestId": payload.get("requestId"),
                "provider": record["provider"], "identity": record["identity"], "record": saved}

    async def start(self, payload: dict[str, Any], owner, *, defer: bool = False) -> dict[str, Any]:
        payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        if payload.get("playlistId") is not None:
            if self.playlist_registry is None:
                raise ValueError("unregistered web playlist progress session")
            entries, description = self.playlist_registry.progress_entries(payload, owner)
            payload = {**payload, "_playlistEntries": entries, "_playlist": description,
                       "pageUrl": entries[payload["identity"]]["pageUrl"]}
        record = make_record(payload, "web")
        session_id, pipe_name = payload.get("sessionId"), payload.get("pipeName")
        if not isinstance(session_id, str) or not SAFE_SESSION.fullmatch(session_id) or pipe_name != "mpv-lazy-" + session_id:
            raise ValueError("invalid playback progress IPC session")
        async with self.lock:
            existing = self.sessions.get(session_id)
            if existing and existing.phase != "stopped" and (existing.task is None or not existing.task.done()):
                if existing.owner not in (None, owner) or (existing.identity != record["identity"] and record["identity"] not in existing.playlist_entries) or existing.playlist_id != payload.get("playlistId"):
                    raise ValueError("playback progress session already exists")
                existing.owner = owner
                return {"type": "playbackProgressStarted", "requestId": payload.get("requestId"),
                        "provider": record["provider"], "identity": record["identity"], "sessionId": session_id,
                        "apiVersion": API_VERSION, "record": self.store.get(record["identity"])}
            for key, session in list(self.sessions.items()):
                if session.task and session.task.done():
                    self.sessions.pop(key, None)
            # Exactly one player owns a resume record at a time.
            for session in self._active(record["identity"]):
                self.sessions.pop(session.session_id, None)
                await session.stop("player_replaced")
            if len(self.sessions) >= 8:
                raise ValueError("too many playback progress sessions")
            self.store.put(record)
            await asyncio.to_thread(self.store.flush)
            session = self.session_factory(payload, owner, self.store)
            self.sessions[session_id] = session
            if not defer:
                session.start()
        return {"type": "playbackProgressStarted", "requestId": payload.get("requestId"),
                "provider": record["provider"], "identity": record["identity"], "sessionId": session_id,
                "apiVersion": API_VERSION, "record": self.store.get(record["identity"])}

    def activate(self, session_id: str, owner) -> None:
        session = self.sessions.get(session_id)
        if session and session.owner is owner and session.task is None and session.phase != "stopped":
            session.start()

    async def stop(self, session_id: str, owner) -> None:
        async with self.lock:
            session = self.sessions.get(session_id)
            if session and owner is not None and session.owner is owner:
                self.sessions.pop(session_id, None)
                await session.stop("browser_stop")

    async def stop_playlist(self, playlist_id: str, reason: str) -> None:
        """Called only by the trusted registry when its player/relay ends."""
        async with self.lock:
            sessions = [session for session in self.sessions.values() if session.playlist_id == playlist_id]
            for session in sessions:
                self.sessions.pop(session.session_id, None)
                await session.stop(reason)

    def detach_owner(self, owner) -> None:
        for session in self.sessions.values():
            if session.owner is owner:
                session.owner = None

    def snapshots(self) -> list[dict[str, Any]]:
        return [session.snapshot() for session in self.sessions.values()
                if session.phase != "stopped" and (session.task is None or not session.task.done())]

    async def close(self) -> None:
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
            for session in sessions:
                await session.stop("relay_closed")
            await asyncio.to_thread(self.store.flush)


class HttpProgressOwner:
    def __init__(self) -> None:
        self.events = deque(maxlen=64)
        self.last_seen = time.monotonic()

    async def send(self, raw: str) -> None:
        message = json.loads(raw)
        if message.get("type") == "playbackProgress":
            self.events = deque((item for item in self.events if not (
                item.get("type") == "playbackProgress" and item.get("sessionId") == message.get("sessionId")
                and item.get("identity") == message.get("identity"))), maxlen=64)
        self.events.append(message)

    def drain(self) -> list[dict]:
        result = list(self.events)
        self.events.clear()
        return result


class HttpProgressTransport:
    """Private, bounded per-client queues for privileged userscript HTTP polling."""
    def __init__(self, manager: PlaybackProgressManager, playlist_manager=None) -> None:
        self.manager = manager
        self.playlist_manager = playlist_manager
        self.clients: dict[str, HttpProgressOwner] = {}

    def sweep(self) -> None:
        for key, owner in list(self.clients.items()):
            if time.monotonic() - owner.last_seen > 120:
                self.manager.detach_owner(owner)
                if self.playlist_manager is not None:
                    self.playlist_manager.detach_owner(owner)
                self.clients.pop(key, None)

    async def request(self, envelope: Any) -> tuple[dict, HttpProgressOwner, str | None]:
        self.sweep()
        if not isinstance(envelope, dict) or not isinstance(envelope.get("clientId"), str) or not re.fullmatch(r"progress-http-(?:[a-f0-9]{32}|[a-f0-9]{64})", envelope["clientId"]):
            raise ValueError("invalid HTTP progress client")
        request = envelope.get("request")
        if not isinstance(request, dict) or request.get("type") not in {
            "getPlaybackProgress", "updatePlaybackProgress", "startPlaybackProgress", "stopPlaybackProgress",
            "pollPlaybackProgress", "detachPlaybackProgress",
            "registerWebPlaylist", "submitWebPlaylistItem", "getWebPlaylist", "stopWebPlaylist",
            "appendWebPlaylistItems", "commitWebPlaylist", "getWebPlaylistItems"}:
            raise ValueError("invalid HTTP progress action")
        client_id, action = envelope["clientId"], request["type"]
        owner = self.clients.get(client_id)
        if owner is None:
            if action in {"pollPlaybackProgress", "stopPlaybackProgress", "detachPlaybackProgress", "submitWebPlaylistItem", "stopWebPlaylist", "appendWebPlaylistItems", "commitWebPlaylist"}:
                raise LookupError("expired HTTP progress client")
            if len(self.clients) >= 32:
                raise ValueError("too many HTTP progress clients")
            owner = self.clients[client_id] = HttpProgressOwner()
        owner.last_seen = time.monotonic()
        reply, activate = None, None
        if action in {"registerWebPlaylist", "submitWebPlaylistItem", "getWebPlaylist", "stopWebPlaylist", "appendWebPlaylistItems", "commitWebPlaylist", "getWebPlaylistItems"}:
            if self.playlist_manager is None:
                raise ValueError("web playlist is unavailable")
            method = {"registerWebPlaylist": self.playlist_manager.register, "submitWebPlaylistItem": self.playlist_manager.submit,
                      "getWebPlaylist": self.playlist_manager.get, "stopWebPlaylist": self.playlist_manager.stop,
                      "appendWebPlaylistItems": self.playlist_manager.append_items, "commitWebPlaylist": self.playlist_manager.commit_upload,
                      "getWebPlaylistItems": self.playlist_manager.get_items}[action]
            reply = await method(request, owner)
        elif action == "getPlaybackProgress":
            provider, identity = validate_identity(request)
            canonical_page_url(provider, request.get("pageUrl"))
            if request.get("rebind") is True:
                for session in self.manager._active(identity):
                    if isinstance(session.owner, HttpProgressOwner) and session.owner is not owner:
                        if session.playlist_id and self.playlist_manager is not None:
                            await self.playlist_manager.rebind(session.playlist_id, owner)
                            continue
                        await session.owner.send(json.dumps({"type": "playbackProgressStopped", "provider": provider,
                            "identity": identity, "sessionId": session.session_id, "reason": "browser_rebound"}))
                        session.owner = owner
            reply = await self.manager.get(request, owner)
        elif action == "updatePlaybackProgress":
            reply = await self.manager.update(request, owner)
        elif action == "startPlaybackProgress":
            reply = await self.manager.start(request, owner, defer=True)
            activate = reply["sessionId"]
        elif action == "stopPlaybackProgress":
            await self.manager.stop(str(request.get("sessionId") or ""), owner)
            reply = {"type": "playbackProgressStopped", "requestId": request.get("requestId"), "sessionId": request.get("sessionId")}
        elif action == "detachPlaybackProgress":
            self.manager.detach_owner(owner)
            if self.playlist_manager is not None:
                self.playlist_manager.detach_owner(owner)
            self.clients.pop(client_id, None)
        return {"reply": reply, "events": owner.drain()}, owner, activate
