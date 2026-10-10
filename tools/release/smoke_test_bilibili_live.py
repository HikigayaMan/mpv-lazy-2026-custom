#!/usr/bin/env python3
"""Exercise Bilibili packets through real MPV IPC and the complete Lua script.

Uses offline WebSocket fixtures and a locally generated lavfi test video. It
does not access a Bilibili account or room. Only processes started here are
stopped; logs and rendered ASS files are retained in the output directory.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
BRIDGE_DIR = REPO / "tools/telegram-web-mpv-bridge"
sys.path.insert(0, str(BRIDGE_DIR))

import brotli

from bilibili_danmaku import BilibiliSession, HEADER, MpvPipe, make_packet, validate_auth


def packet(operation: int, payload: dict, version: int = 0) -> bytes:
    return make_packet(operation, json.dumps(payload, ensure_ascii=False).encode("utf-8"), version)


def chat_frame(text: str, compressed: bool = True) -> bytes:
    frame = packet(5, {"cmd": "DANMU_MSG", "info": [[0, 1, 25, 0x12AB34], text, [1, "测试观众"]]})
    return make_packet(5, brotli.compress(frame), 3) if compressed else frame


class OfflineRemote:
    def __init__(self) -> None:
        self.queue = asyncio.Queue()
        self.sent: list[bytes] = []
        self.closed = False

    async def send(self, raw: bytes) -> None:
        self.sent.append(raw)
        if HEADER.unpack_from(raw)[3] == 7:
            await self.queue.put(packet(8, {"code": 0}, 1))

    async def recv(self) -> bytes:
        return await self.queue.get()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        self.closed = True


class OfflineConnector:
    def __init__(self, remote: OfflineRemote) -> None:
        self.remote = remote
        self.calls = 0

    def __call__(self, _url, **_options):
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("offline smoke unexpectedly reconnected")
        return self.remote


class OfflineOwner:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, raw: str) -> None:
        self.messages.append(json.loads(raw))


def require(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


async def eventually(predicate, detail: str, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out: " + detail)
        await asyncio.sleep(0.05)


async def wait_property(ipc: MpvPipe, name: str, expected, timeout: float = 8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = await ipc.command(["get_property", name])
            if (expected(last) if callable(expected) else last == expected):
                return last
        except (OSError, RuntimeError, TimeoutError):
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {name}: last={last!r}")


def has_danmaku_track(tracks: list[dict]) -> bool:
    # MPV may reset an external track's title to its filename on sub-reload.
    return any(track.get("type") == "sub" and
               (track.get("title") == "uosc_danmaku" or
                Path(track.get("external-filename", "")).name.startswith("uosc-danmaku-"))
               for track in tracks)


async def wait_ass(path: Path, expected_text: str) -> str:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            content = path.read_text(encoding="utf-8")
            if expected_text in content and "Dialogue:" in content:
                return content
        await asyncio.sleep(0.05)
    raise AssertionError(f"rendered ASS did not contain {expected_text!r}: {path}")


async def run_mode(mpv: Path, mode: str, output: Path) -> dict:
    run_dir = output / (mode + "-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True)
    config = run_dir / "config"
    config.mkdir()
    input_conf = config / "input.conf"
    input_conf.write_text("j script-message-to uosc_danmaku show_danmaku_keyboard\n", encoding="utf-8")
    suffix = uuid.uuid4().hex
    pipe_name = "mpv-lazy-bilibili-smoke-" + suffix
    session_id = "bilibili-smoke-" + suffix
    ipc = MpvPipe(pipe_name)
    remote, owner = OfflineRemote(), OfflineOwner()
    connector = OfflineConnector(remote)
    auth = validate_auth({"uid": 0, "token": "offline-smoke-token", "buvid": "offline-smoke-client",
                          "userAgent": "Mozilla/5.0 offline-smoke",
                          "hostList": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}]})
    session = BilibiliSession("545068", session_id, pipe_name, auth, owner, connector=connector, ipc=ipc)
    script = REPO / "portable_config/scripts/uosc_danmaku"
    options = {
        "render_mode": mode, "opacity": "1", "vf_fps": "no",
        "auto_load": "no", "autoload_for_url": "no", "autoload_local_danmaku": "no",
        "history_path": str(config / "history.json"),
        "style_path": str(config / "style.json"),
    }
    command = [
        str(mpv), "--no-config", f"--config-dir={config}", f"--script={script}",
        "--vo=null", "--ao=null", "--idle=yes", "--force-window=no", "--terminal=no",
        "--input-default-bindings=no", f"--input-conf={input_conf}",
        "--input-ipc-server=" + ipc.path,
        "--script-opts=" + ",".join(f"uosc_danmaku-{key}={value}" for key, value in options.items()),
        f"--log-file={run_dir / 'mpv.log'}", "--msg-level=all=warn,uosc_danmaku=debug",
        "av://lavfi:testsrc=size=1280x720:rate=30",
    ]
    environment = os.environ.copy()
    environment["TEMP"] = environment["TMP"] = str(run_dir)
    process = None
    stdout_path = run_dir / "process.log"
    started_before_player = False
    with stdout_path.open("wb") as stdout:
        try:
            await session.start()
            started_before_player = not session.player_attached
            process = subprocess.Popen(command, cwd=run_dir, env=environment, stdout=stdout,
                                       stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            await wait_property(ipc, "user-data/uosc_danmaku/live-api-version", 1)
            await wait_property(ipc, "path", bool)
            await eventually(lambda: session.player_attached, "real MPV live API attached")
            await wait_property(ipc, "user-data/uosc_danmaku/live-active", True)
            await wait_property(ipc, "user-data/uosc_danmaku/has-danmaku", True)

            await remote.queue.put(chat_frame("第一条真实播放器弹幕"))
            await eventually(lambda: session.sent >= 1, "compressed packet reached MPV")
            await wait_property(ipc, "user-data/uosc_danmaku/live-input-count", 1)
            ass_path = run_dir / f"uosc-danmaku-{process.pid}.ass"
            if mode == "ass":
                await wait_property(ipc, "track-list", has_danmaku_track)
                await wait_ass(ass_path, "第一条真实播放器弹幕")
                await wait_property(ipc, "secondary-sid", lambda sid: sid not in (False, "no", None))

            # Session IDs isolate subsequent player invocations.
            await ipc.command(["script-message-to", "uosc_danmaku", "live-batch", "bilibili-wrong-session", '[{"text":"wrong"}]'])
            await ipc.command(["script-message-to", "uosc_danmaku", "live-stop", "bilibili-wrong-session"])
            await asyncio.sleep(0.1)
            require(await ipc.command(["get_property", "user-data/uosc_danmaku/live-input-count"]) == 1,
                    "wrong session injected a message")
            require(await ipc.command(["get_property", "user-data/uosc_danmaku/live-active"]), "wrong session stopped live input")

            await ipc.command(["keypress", "j"])
            await wait_property(ipc, "user-data/uosc_danmaku/danmaku-switch-on", False)
            await wait_property(ipc, "user-data/uosc_danmaku/has-danmaku", False)
            await wait_property(ipc, "user-data/uosc_danmaku/live-input-count", 0)
            require(await ipc.command(["get_property", "user-data/uosc_danmaku/live-active"]),
                    "hiding danmaku disconnected its live session")
            await remote.queue.put(chat_frame("隐藏期间应丢弃"))
            await eventually(lambda: session.sent >= 2, "hidden batch crossed IPC")
            await asyncio.sleep(0.35)
            require(await ipc.command(["get_property", "user-data/uosc_danmaku/live-input-count"]) == 0,
                    "hidden messages remained queued")
            if mode == "ass":
                require(not has_danmaku_track(await ipc.command(["get_property", "track-list"])), "hidden ASS track remained loaded")

            await ipc.command(["keypress", "j"])
            await wait_property(ipc, "user-data/uosc_danmaku/danmaku-switch-on", True)
            await remote.queue.put(chat_frame("重新启用后收到弹幕", compressed=False))
            await eventually(lambda: session.sent >= 3, "resumed packet crossed IPC")
            await wait_property(ipc, "user-data/uosc_danmaku/live-input-count", 1)
            if mode == "ass":
                content = await wait_ass(ass_path, "重新启用后收到弹幕")
                require("隐藏期间应丢弃" not in content, "hidden message reappeared after enabling")

            await ipc.command(["set_property", "pause", True])
            await wait_property(ipc, "pause", True)
            await remote.queue.put(chat_frame("暂停期间应丢弃"))
            await eventually(lambda: session.received >= 4 and session.dropped >= 1, "paused message discarded")
            require(await ipc.command(["get_property", "user-data/uosc_danmaku/live-input-count"]) == 1,
                    "paused message entered Lua input")
            await ipc.command(["set_property", "pause", False])
            await remote.queue.put(chat_frame("暂停结束后的新弹幕"))
            await eventually(lambda: session.sent >= 4, "post-pause packet crossed IPC")
            await wait_property(ipc, "user-data/uosc_danmaku/live-input-count", 2)
            if mode == "ass":
                content = await wait_ass(ass_path, "暂停结束后的新弹幕")
                require("暂停期间应丢弃" not in content, "paused message reappeared")
                shutil.copyfile(ass_path, run_dir / "rendered-danmaku.ass")

            if mode == "ass":
                await ipc.command(["script-message-to", "uosc_danmaku", "live-stop", session_id])
            else:
                await ipc.command(["stop"])
            await wait_property(ipc, "user-data/uosc_danmaku/live-active", False)
            await asyncio.wait_for(session.task, 5)
            require(remote.closed and session.auth == {} and not session.pending,
                    "session did not clear its WebSocket, connection metadata and queue")
            require(owner.messages[-1]["type"] == "danmakuStopped" and session.status == "stopped",
                    "session did not report a clean stop")
            await ipc.command(["quit"])
            await asyncio.to_thread(process.wait, 5)
        finally:
            await session.stop()
            if process and process.poll() is None:
                with contextlib.suppress(Exception):
                    await ipc.command(["quit"])
                    await asyncio.to_thread(process.wait, 3)
                if process.poll() is None:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 3)

    log = (run_dir / "mpv.log").read_text(encoding="utf-8", errors="replace")
    fatal = [pattern for pattern in ("lua error", "stack traceback", "script failed", "error parsing option") if pattern in log.lower()]
    require(not fatal, "MPV Lua failure: " + ", ".join(fatal))
    result = {"mode": mode, "passed": True, "authenticated_before_player_launch": started_before_player,
              "received": session.received, "sent": session.sent, "dropped": session.dropped,
              "stop_trigger": "live-stop" if mode == "ass" else "end-file", "artifacts": str(run_dir),
              "mpv_exit_code": process.returncode}
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


async def main_async(args) -> dict:
    require(os.name == "nt", "this integration smoke requires Windows named pipes")
    mpv = args.mpv.resolve()
    require(mpv.is_file(), "MPV executable does not exist: " + str(mpv))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for mode in (["ass", "overlay"] if args.mode == "both" else [args.mode]):
        results.append(await run_mode(mpv, mode, output))
    report = {"passed": True, "network": "offline fixtures only", "player": str(mpv), "results": results}
    (output / "smoke-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv", type=Path, required=True)
    parser.add_argument("--mode", choices=("ass", "overlay", "both"), default="both")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(tempfile.gettempdir()) / ("bilibili-live-smoke-" + uuid.uuid4().hex[:8]))
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    report = asyncio.run(main_async(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
