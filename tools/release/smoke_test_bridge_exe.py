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
