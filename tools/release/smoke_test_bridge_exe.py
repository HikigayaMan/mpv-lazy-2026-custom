#!/usr/bin/env python3
"""Verify a freshly frozen bridge on isolated ports and record public build inputs."""
from __future__ import annotations
import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from PyInstaller.archive.readers import CArchiveReader
from websockets.asyncio.client import connect
from package_custom_update import BRIDGE_INPUTS, sha256


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(port: int, endpoint: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/{endpoint}", timeout=2) as response:
        return json.load(response)


def verify_frozen_web_playlist(http_port: int) -> None:
    """Exercise the frozen HTTP registry and standard M3U without site auth."""
    nonce = uuid.uuid4().hex
    client_id, playlist_id = "progress-http-" + nonce, "playlist-" + nonce
    session_id = "progress-frozen-" + nonce[:16]
    base = f"http://127.0.0.1:{http_port}"

    def action(kind: str, **payload) -> dict:
        probe = urllib.request.Request(base + "/playback-progress",
            data=json.dumps({"clientId": client_id, "request": {"type": kind, **payload}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(probe, timeout=3) as response:
            assert response.status == 200 and "Access-Control-Allow-Origin" not in response.headers
            return json.load(response)["reply"]

    items = [{"itemId": "1001:2002", "aid": "1001", "cid": "2002", "bvid": "BV1fixture", "p": 1,
              "title": "Frozen fixture part 1", "duration": 120,
              "pageUrl": "https://www.bilibili.com/video/BV1fixture?p=1"},
             {"itemId": "1001:2003", "aid": "1001", "cid": "2003", "bvid": "BV1fixture", "p": 2,
              "title": "Frozen fixture part 2", "duration": 180,
              "pageUrl": "https://www.bilibili.com/video/BV1fixture?p=2"}]
    registered = False
    try:
        reply = action("registerWebPlaylist", requestId="frozen-playlist-register", playlistId=playlist_id,
            sessionId=session_id, pipeName="mpv-lazy-" + session_id, items=items,
            selectedItemId="1001:2003", initialPosition=12.5)
        registered = True
        assert reply["type"] == "webPlaylistRegistered" and reply["requestId"] == "frozen-playlist-register"
        assert reply["playlistId"] == playlist_id and reply["sessionId"] == session_id and reply["selectedIndex"] == 1
        assert len(reply["items"]) == 2 and [item["itemId"] for item in reply["items"]] == ["1001:2002", "1001:2003"]
        playlist_url = f"{base}/web-playlist/{playlist_id}/list.m3u"
        assert reply["playlistUrl"] == playlist_url
        expected = ("#EXTM3U\n#EXTINF:120,Frozen fixture part 1\n" +
            f"{base}/web-playlist/{playlist_id}/item/1001:2002\n" +
            "#EXTINF:180,Frozen fixture part 2\n" + f"{base}/web-playlist/{playlist_id}/item/1001:2003\n").encode()
        for method in ("GET", "HEAD"):
            with urllib.request.urlopen(urllib.request.Request(playlist_url, method=method), timeout=3) as response:
                assert response.status == 200
                assert response.headers.get_content_type() == "application/vnd.apple.mpegurl"
                assert response.headers.get("Cache-Control") == "no-store"
                assert "Access-Control-Allow-Origin" not in response.headers
                assert int(response.headers["Content-Length"]) == len(expected)
                assert response.read() == (expected if method == "GET" else b"")
        stopped = action("stopWebPlaylist", playlistId=playlist_id)
        assert stopped["playlistId"] == playlist_id
        registered = False
        for index, item in enumerate(items):
            item.update({"sectionId": "main", "sectionTitle": "Collection section", "videoId": "1001",
                "videoTitle": "Frozen multipart video", "partTitle": f"P{index + 1}", "partCount": 2, "order": index})
        begun = action("registerWebPlaylist", requestId="frozen-hierarchy-begin", chunked=True,
            playlistId=playlist_id, sessionId=session_id, pipeName="mpv-lazy-" + session_id,
            selectedItemId="1001:2003", collectionId="fixture-season", collectionTitle="Frozen collection")
        registered = True
        assert begun["type"] == "webPlaylistUploadStarted" and begun["nextChunkIndex"] == 0
        for index, item in enumerate(items):
            appended = action("appendWebPlaylistItems", playlistId=playlist_id, chunkIndex=index, items=[item])
            assert appended["nextChunkIndex"] == index + 1 and appended["itemCount"] == index + 1
        committed = action("commitWebPlaylist", playlistId=playlist_id, expectedItemCount=2)
        assert committed["type"] == "webPlaylistRegistered" and committed["itemCount"] == 2
        assert "items" not in committed and committed["collectionTitle"] == "Frozen collection"
        page = action("getWebPlaylistItems", playlistId=playlist_id, offset=0, limit=1)
        assert page["nextOffset"] == 1 and page["items"][0]["videoTitle"] == "Frozen multipart video"
        page = action("getWebPlaylistItems", playlistId=playlist_id, offset=1, limit=1)
        assert page["nextOffset"] is None and page["items"][0]["partCount"] == 2
        with urllib.request.urlopen(playlist_url, timeout=3) as response:
            assert response.read() == expected
        action('stopWebPlaylist', playlistId=playlist_id)
        registered = False
        bootstrap = action('registerWebPlaylist', bootstrap=True, playlistId=playlist_id,
            sessionId=session_id, pipeName='mpv-lazy-' + session_id, items=[items[1]],
            selectedItemId=items[1]['itemId'], collectionTitle='Frozen collection')
        registered = True
        assert bootstrap['itemCount'] == 1 and bootstrap['metadataPending'] is True
        assert bootstrap['selectedIndex'] == 0
        action('registerWebPlaylist', chunked=True, extendExisting=True, playlistId=playlist_id,
            sessionId=session_id, pipeName='mpv-lazy-' + session_id, selectedItemId=items[1]['itemId'],
            collectionTitle='Frozen collection')
        action('appendWebPlaylistItems', playlistId=playlist_id, chunkIndex=0, items=items)
        extended = action('commitWebPlaylist', playlistId=playlist_id, expectedItemCount=2)
        assert extended['itemCount'] == 2 and extended['metadataPending'] is True
        # Metadata may finish before MPV attaches. Its first M3U must still
        # contain only the bootstrap CID; expansion waits for actual playback.
        with urllib.request.urlopen(playlist_url, timeout=3) as response:
            body = response.read().decode()
            assert body.count('/item/') == 1 and body.endswith('/item/1001:2003\n')
        failed = action('failWebPlaylistMetadata', playlistId=playlist_id)
        assert failed['metadataPending'] is True and failed['metadataStage'] == 'failed'
    finally:
        if registered:
            stopped = action("stopWebPlaylist", requestId="frozen-playlist-stop", playlistId=playlist_id)
            assert stopped["type"] == "webPlaylistStopped" and stopped["playlistId"] == playlist_id
            assert all(entry["playlistId"] != playlist_id for entry in request(http_port, "status")["webPlaylists"])


async def verify_websocket(http_port: int, ws_port: int) -> None:
    async with connect(f"ws://127.0.0.1:{ws_port}/ws") as websocket:
        await websocket.send(json.dumps({"type": "hello", "provider": "bilibili"}))
        for _ in range(30):
            status = request(http_port, "status")
            if status["diagnostics"]["helloCount"] >= 1 and status["diagnostics"]["browserConnected"]:
                assert status["danmaku"] == []
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("Frozen bridge didn't record the browser hello.")


def notices() -> str:
    sections = ["Third-party components included in bilibili-live-bridge.exe\n\nProject file licenses remain governed by LICENSE.MD and individual source headers.\n"]
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python license file was not found.")
    sections.append(f"\n=== CPython {sys.version.split()[0]} ===\n" + python_license.read_text(encoding="utf-8"))
    for name in ("websockets", "Brotli", "pyinstaller"):
        dist = importlib.metadata.distribution(name)
        files = [f for f in (dist.files or []) if ".dist-info/" in str(f) and ("/licenses/" in str(f) or f.name.upper().startswith(("LICENSE", "COPYING")))]
        if not files:
            raise RuntimeError(f"License files missing for {name}")
        sections.append(f"\n=== {name} {dist.version} ===\n")
        for file in sorted(files):
            sections.append(f"\n{file.name}\n" + Path(dist.locate_file(file)).read_text(encoding="utf-8"))
    return "\n".join(sections)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-working-tree", action="store_true", help="Verify an uncommitted local development build; it cannot be used as a committed release input.")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    executable = args.exe.resolve()
    binary = executable.read_bytes()
    source_inputs = {}
    compiled_inputs = {}
    for name in BRIDGE_INPUTS:
        compiled = (repo / name).read_bytes()
        if args.allow_working_tree:
            committed = compiled.replace(b"\r\n", b"\n")
        else:
            committed = subprocess.check_output(["git", "-C", str(repo), "show", f"HEAD:{name}"])
        if committed.replace(b"\r\n", b"\n") != compiled.replace(b"\r\n", b"\n"):
            raise RuntimeError(f"Build input has uncommitted source changes: {name}")
        source_inputs[name] = sha256(committed)
        compiled_inputs[name] = sha256(compiled)
    toc = CArchiveReader(str(executable)).toc
    brotli_included = any(name.startswith("_brotli") and name.endswith(".pyd") for name in toc)
    assert brotli_included, "Missing native Brotli decoder in frozen executable"
    http_port, ws_port = free_port(), free_port()
    while ws_port == http_port:
        ws_port = free_port()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "frozen-bridge-smoke.log").open("wb") as log:
        process = subprocess.Popen([str(executable), "--host", "127.0.0.1", "--http-port", str(http_port), "--ws-port", str(ws_port)], stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Frozen bridge exited during startup.")
                try:
                    health = request(http_port, "health")
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Frozen bridge failed to start within 20 seconds.")
                    time.sleep(0.1)
            expected_version = re.search(r'BRIDGE_VERSION = "([^"]+)"', (repo / BRIDGE_INPUTS[0]).read_text(encoding="utf-8")).group(1)
            assert health["ok"] and health["bridgeVersion"] == expected_version
            assert health["danmakuApiVersion"] == 2 and health["danmakuProviders"] == ["bilibili"]
            assert type(health.get("playbackProgressApiVersion")) is int and health["playbackProgressApiVersion"] == 1
            assert type(health.get("playbackProgressHttpApiVersion")) is int and health["playbackProgressHttpApiVersion"] == 1
            assert health["playbackProgressProviders"] == ["bilibili", "web"]
            assert type(health.get("webPlaylistApiVersion")) is int and health["webPlaylistApiVersion"] == 1
            assert type(health.get("webPlaylistHierarchyApiVersion")) is int and health["webPlaylistHierarchyApiVersion"] == 1
            assert type(health.get("webPlaylistChunkApiVersion")) is int and health["webPlaylistChunkApiVersion"] == 1
            assert type(health.get('webPlaylistFastStartApiVersion')) is int and health['webPlaylistFastStartApiVersion'] == 1
            probe = urllib.request.Request(f"http://127.0.0.1:{http_port}/playback-progress",
                data=json.dumps({"clientId": "progress-http-" + "a" * 32, "request": {
                    "type": "getPlaybackProgress", "requestId": "frozen-http-test", "provider": "bilibili",
                    "identity": "bilibili:1001:2002", "pageUrl": "https://www.bilibili.com/video/BV1fixture"}}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(probe, timeout=3) as response:
                assert response.status == 200 and "Access-Control-Allow-Origin" not in response.headers
                reply = json.load(response)["reply"]
                assert reply["type"] == "playbackProgressState" and reply["requestId"] == "frozen-http-test"
                assert reply["apiVersion"] == 1 and reply["provider"] == "bilibili" and reply["identity"] == "bilibili:1001:2002"
            verify_frozen_web_playlist(http_port)
            asyncio.run(verify_websocket(http_port, ws_port))
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    if stopped.returncode:
                        raise RuntimeError("Could not stop the owned frozen bridge process tree.")
                else:
                    process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    evidence = {"exeSha256": sha256(binary), "frozenHealthPassed": True, "frozenWebSocketPassed": True,
                "frozenPlaybackHttpPassed": True,
                "frozenWebPlaylistPassed": True,
                "frozenWebPlaylistHierarchyPassed": True,
                "frozenWebPlaylistFastStartPassed": True,
                "brotliBinaryIncluded": brotli_included, "python": sys.version.split()[0],
                "packages": {name: importlib.metadata.version(name) for name in ("websockets", "Brotli", "pyinstaller")},
                "sourceInputs": source_inputs, "compiledSourceInputs": compiled_inputs,
                "sourceRef": "working-tree" if args.allow_working_tree else "HEAD",
                "sourceComparison": "Canonical input bytes; build checkout may differ only by LF/CRLF line endings."}
    (args.output / "bridge-build-provenance.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    (args.output / "BRIDGE-THIRD-PARTY-NOTICES.txt").write_text(notices(), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
