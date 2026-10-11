#!/usr/bin/env python3
"""Verify browser quality handoff, real MPV reloads and live danmaku continuity.

Uses a local HTTP server and offline Bilibili frames. Only its own MPV process
is stopped. The installed executable is read without changing its configuration.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import uuid
import wave
import struct
from urllib.parse import quote

from smoke_test_bilibili_live import (
    REPO, BilibiliSession, MpvPipe, OfflineConnector, OfflineOwner,
    OfflineRemote, chat_frame, eventually, require, validate_auth, wait_property,
)


class QuietHandler(SimpleHTTPRequestHandler):
    failures: list[str] = []

    def log_message(self, *_args):
        pass

    def send_head(self):
        if self.headers.get('X-Quality-Smoke') != 'expected':
            self.failures.append('media request lost its header')
            self.send_error(403)
            return None
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(404)
            return None
        stream = path.open('rb')
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get('Range')
        if requested:
            try:
                first, last = requested.removeprefix('bytes=').split('-', 1)
                start = int(first) if first else max(0, size - int(last))
                end = min(size - 1, int(last)) if last and first else size - 1
                if start > end or start >= size:
                    raise ValueError('invalid range')
            except ValueError:
                stream.close()
                self.send_error(416)
                return None
        self.send_response(206 if requested else 200)
        self.send_header('Content-type', self.guess_type(str(path)))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if requested:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        stream.seek(start)
        self.bytes_remaining = end - start + 1
        return stream

    def copyfile(self, source, destination):
        remaining = self.bytes_remaining
        try:
            while remaining:
                block = source.read(min(65536, remaining))
                if not block:
                    break
                destination.write(block)
                remaining -= len(block)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass


def make_fixture(mpv: Path, target: Path, width: int, height: int) -> None:
    # The user's reduced FFmpeg build supports PNG-in-MOV encoding. This keeps
    # the test independent of downloads, hardware encoding and external ffmpeg.
    result = subprocess.run([
        str(mpv), "--no-config", "--terminal=yes", "--force-window=no",
        "--audio=no", "--untimed", "--ovc=png", "--of=mov", "--frames=90",
        f"--o={target}", f"av://lavfi:testsrc=size={width}x{height}:rate=1",
    ], capture_output=True, encoding="utf-8", errors="replace", timeout=30,
       creationflags=subprocess.CREATE_NO_WINDOW)
    require(result.returncode == 0 and target.is_file(),
            "fixture encoder failed: " + result.stdout[-1000:] + result.stderr[-1000:])


async def run(args) -> dict:
    require(os.name == "nt", "this smoke requires Windows MPV named pipes")
    mpv, output = args.mpv.resolve(), args.output_dir.resolve()
    require(mpv.is_file(), "MPV executable is missing")
    output.mkdir(parents=True, exist_ok=True)
    config = output / "config"
    config.mkdir(exist_ok=True)
    fixtures = output / "fixtures"
    fixtures.mkdir(exist_ok=True)
    resolutions = [(640, 360), (1280, 720), (2560, 1440)]
    for width, height in resolutions:
        target = fixtures / f"{height}.mov"
        if not target.is_file():
            await asyncio.to_thread(make_fixture, mpv, target, width, height)
    audio = fixtures / 'audio.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        second = b''.join(struct.pack('<h', int(1000 * math.sin(2 * math.pi * 440 * i / 16000)))
                          for i in range(16000))
        for _ in range(90):
            stream.writeframesraw(second)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(fixtures)))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{server.server_port}"
    variants = [{"url": f"{base}/{height}.mov", "title": f"{height}p",
                 "width": width, "height": height, "codec": "PNG fixture"}
                for width, height in resolutions]
    for variant in variants[1:]:
        variant['audio_url'] = f'{base}/audio.wav'
    token = uuid.uuid4().hex
    pipe_name, session_id = "mpv-quality-smoke-" + token, "bilibili-quality-" + token
    ipc = MpvPipe(pipe_name)
    remote, owner = OfflineRemote(), OfflineOwner()
    auth = validate_auth({"uid": 0, "token": "offline-quality-token", "buvid": "offline-quality-client",
                          "userAgent": "Mozilla/5.0 offline-quality",
                          "hostList": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}]})
    session = BilibiliSession("545068", session_id, pipe_name, auth, owner,
                              connector=OfflineConnector(remote), ipc=ipc)
    encoded = quote(json.dumps(variants, separators=(",", ":")), safe="")
    # MPV key/value options interpret an initial % as a length quote.
    options = ["uosc_danmaku-render_mode=ass", "uosc_danmaku-opacity=1", "uosc_danmaku-fontsize=35",
               "uosc_danmaku-vf_fps=no", "uosc_danmaku-auto_load=no",
               "uosc_danmaku-autoload_for_url=no", "uosc_danmaku-autoload_local_danmaku=no",
               f"uosc_danmaku-history_path={output / 'history.json'}",
               f"uosc_danmaku-style_path={output / 'style.json'}"]
    command = [str(mpv), "--no-config", f"--config-dir={config}", "--load-scripts=no",
               "--no-ytdl", "--idle=yes", "--terminal=yes",
               "--ao=null", "--vo=gpu-next", "--force-window=yes", "--autofit=960x540",
               "--http-header-fields=X-Quality-Smoke: expected",
               "--input-ipc-server=" + ipc.path,
               "--script=" + str(REPO / "portable_config/scripts/quality-menu.lua"),
               "--script=" + str(REPO / "portable_config/scripts/uosc"),
               "--script=" + str(REPO / "portable_config/scripts/uosc_danmaku"),
               "--script-opts=" + ",".join(options),
               f"--script-opts-append=quality-menu-stream_variants=%{len(encoded)}%{encoded}",
               f"--log-file={output / 'mpv.log'}",
               "--msg-level=all=warn", variants[0]["url"]]
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(output)
    process = None
    samples = []
    try:
        await session.start()
        with (output / "process.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=output, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            await wait_property(ipc, "user-data/uosc_danmaku/live-api-version", 1, timeout=15)
            await eventually(lambda: session.player_attached, "live session attached", timeout=15)
            await wait_property(ipc, "height", 360)
            state = await wait_property(ipc, "user-data/quality-menu/state",
                                        lambda value: value and value.get("status") == "ready")
            require(state["source"] == "browser" and state["videoCount"] == 3,
                    "browser quality list did not reach the real menu")
            await ipc.command(["script-binding", "quality_menu/video_formats_toggle"])
            await asyncio.sleep(0.5)
            await ipc.command(["screenshot-to-file", str(output / "quality-menu.png"), "window"])

            for index, variant in enumerate(variants):
                if index:
                    old_path = variants[index - 1]["url"]
                    await ipc.command(["script-message-to", "quality_menu", "video-format-set", old_path, variant["url"]])
                    await wait_property(ipc, "height", variant["height"], timeout=15)
                    await wait_property(ipc, "user-data/quality-menu/state",
                                        lambda value: value and value.get("activeHeight") == variant["height"])
                    require(await ipc.command(["get_property", "user-data/uosc_danmaku/live-active"]),
                            "quality reload stopped the live danmaku session")
                    require(not session.task.done(), "bridge stopped on the quality reload")
                    await wait_property(ipc, 'user-data/uosc_danmaku/live-switch-ack',
                                        lambda value: value and value.get('generation') == index)
                await remote.queue.put(chat_frame(f"切换到 {variant['height']}p 后的新弹幕 [dog]"))
                await eventually(lambda: session.sent >= index + 1, "new danmaku crossed IPC after switch")
                await wait_property(ipc, "secondary-sub-text", lambda value: str(variant["height"]) + "p" in str(value))
                samples.append({"height": await ipc.command(["get_property", "height"]),
                                "liveActive": await ipc.command(["get_property", "user-data/uosc_danmaku/live-active"]),
                                "sessionId": await ipc.command(["get_property", "user-data/uosc_danmaku/live-session"]),
                                "sent": session.sent})
                tracks = await ipc.command(['get_property', 'track-list'])
                audio_tracks = [track for track in tracks if track.get('type') == 'audio' and track.get('selected')]
                require(bool(audio_tracks) == bool(variant.get('audio_url')), 'external audio selection failed')
                if audio_tracks:
                    require(audio_tracks[0].get('external') and
                            audio_tracks[0].get('external-filename') == variant['audio_url'],
                            'independent audio did not survive quality reload')
            await ipc.command(['script-message-to', 'quality_menu', 'video-format-set',
                               variants[-1]['url'], variants[0]['url']])
            await wait_property(ipc, 'height', 360)
            await wait_property(ipc, 'user-data/uosc_danmaku/live-switch-ack',
                                lambda value: value and value.get('generation') == 3)
            require(not any(track.get('type') == 'audio' for track in
                            await ipc.command(['get_property', 'track-list'])),
                    'muxed variant retained stale separate audio')
            require(all(item["sessionId"] == session_id for item in samples), "quality changed the live session owner")
            await ipc.command(["loadfile", "av://lavfi:testsrc=size=320x180:rate=1", "replace"])
            await wait_property(ipc, "user-data/uosc_danmaku/live-active", False)
            await asyncio.wait_for(session.task, 5)
            require(session.status == "stopped", "ordinary file change did not release live input")
            # With live input released, the same independent-audio route is VOD:
            # changing quality must keep playback position instead of jumping to zero.
            await ipc.command(['loadfile', variants[1]['url'], 'replace'])
            await wait_property(ipc, 'height', 720)
            await ipc.command(['set_property', 'pause', True])
            await ipc.command(['seek', 10, 'absolute+exact'])
            await wait_property(ipc, 'time-pos', lambda value: value is not None and 9.5 <= value <= 11)
            await ipc.command(['script-message-to', 'quality_menu', 'video-format-set',
                               variants[1]['url'], variants[2]['url']])
            await wait_property(ipc, 'height', 1440)
            vod_position = await wait_property(ipc, 'time-pos',
                                               lambda value: value is not None and 9.5 <= value <= 11)
            await ipc.command(['script-binding', 'quality_menu/video_formats_toggle'])
            await asyncio.sleep(0.3)
            await ipc.command(['screenshot-to-file', str(output / 'quality-1440p.png'), 'window'])
            await ipc.command(["quit"])
            await asyncio.to_thread(process.wait, 5)
    except Exception:
        diagnostics = {"processExitCode": process.poll() if process else None,
                       "session": session.snapshot()}
        for prop in ["mpv-version", "options/scripts", "options/load-scripts", "height",
                     "user-data/uosc_danmaku/live-api-version"]:
            try:
                diagnostics[prop] = await ipc.command(["get_property", prop])
            except Exception as error:
                diagnostics[prop] = {"error": str(error)}
        (output / "failure.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        await session.stop()
        if process and process.poll() is None:
            with contextlib.suppress(Exception):
                await ipc.command(["quit"])
                await asyncio.to_thread(process.wait, 3)
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 3)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
    log = (output / "mpv.log").read_text(encoding="utf-8", errors="replace").lower()
    require(not any(item in log for item in ["lua error", "stack traceback", "script failed", "error parsing option"]),
            "real player reported a Lua/option failure")
    require(not QuietHandler.failures, 'HTTP headers were not preserved')
    report = {"passed": True, "network": "local HTTP and offline Bilibili frames",
              "menu": state, "samples": samples, "regularFileChangeStopsDanmaku": True,
              "independentAudioSwitches": True, "requestHeadersPreserved": True,
              "vodResumePosition": vod_position,
              "output": str(output)}
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
