# Telegram Web MPV Bridge

Local bridge for the legacy Telegram Web range userscript and Bilibili live danmaku in MPV.

## Bilibili Live Danmaku

Use this checkout's `external_player.js` (version `1.2.14.15`), updated `uosc_danmaku` and `quality-menu.lua`, and this bridge together. Web handoffs to MPV/MPVNET start fullscreen, with optional automatic default-player handoff on video/live pages. The browser resolves the canonical live room and passes short-lived chat authentication to the loopback bridge; `bilibili_danmaku.py` connects to Bilibili and forwards bounded text batches to a per-launch MPV Windows IPC pipe. Gift/system messages are omitted, pause drops queued messages, and player exit or an unrelated file change stops the session. Keeping the page open permits authentication refresh on reconnect.

The browser also hands account-available stream qualities to MPV. A same-room quality switch keeps the danmaku session and visibility choice, drops messages received during loading, and clears the old playback timeline. The quality menu waits for the bridge to acknowledge the new stream before permitting another switch. Invalid, failed, or expired transitions still stop the session; the authorization is limited to one transition and 15 seconds. Signed stream URLs remain in memory and are not included in session diagnostics.

Install `requirements.txt` in `.venv` and run `start_browser_relay.ps1 -Bilibili -Source`, or build `bilibili-live-bridge.exe` with `build_windows.ps1 -InstallDependencies`. The dedicated Bilibili sidecar uses WebSocket 9001 and HTTP 9002 and runs alongside the existing `browser-media-bridge.exe` on 8999/9000. It includes the Bilibili client and requires no Node installation. See [the complete Chinese installation guide](../../docs/scripts/bilibili-live-danmaku.md).

This checkout's Python source also retains the legacy Telegram `/stream/current` implementation; the separate upstream media/HLS/Douyu EXE remains in place for those workflows.

Offline quality and danmaku checks, from the repository root:

```powershell
node --test tools/telegram-web-mpv-bridge/test_bilibili_live_handoff.mjs tools/telegram-web-mpv-bridge/test_bilibili_stream_quality.mjs
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools\telegram-web-mpv-bridge -p "test_*.py"
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools\release -p "test_quality_menu_lua.py"
```

Lua behavior checks require the development dependency `lupa`.

## What It Does

- A userscript runs inside `web.telegram.org` where you are already logged in.
- A local Python bridge exposes `http://127.0.0.1:8999/stream/current` for MPV.
- MPV sends HTTP Range requests to the bridge.
- The bridge asks the userscript to fetch the same byte range from Telegram Web's current video source.
- The userscript returns binary bytes over WebSocket, and the bridge replies to MPV with `206 Partial Content`.

## Setup

```powershell
cd F:\mpv_2026\mpv-lazy\tools\telegram-web-mpv-bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip websockets
.\.venv\Scripts\python.exe .\bridge.py
```

In mpv-lazy 2026, the native menu also provides `工具 > Telegram Web MPV Bridge 开关` and `工具 > Telegram Web MPV Bridge 状态`.

Install `telegram-web-mpv-bridge.user.js` in Tampermonkey/Violentmonkey, then fully refresh Telegram Web K and play a video. The script runs at `document-start` so it can capture Telegram Web's internal `/stream/...` URLs before they become `blob:` URLs.

## Manual Test

1. Start the bridge.
2. Open `https://web.telegram.org/k/` and play the target video.
3. Confirm the floating `MPV` button appears.
4. Click `MPV` once to register the current video and ask the local bridge to launch MPV. The button should change briefly to `Starting...` and then `MPV started`; it should not open a new browser tab.
5. If manual launch is needed, run:

```powershell
F:\mpv_2026\mpv-lazy\mpv.exe "http://127.0.0.1:8999/stream/current"
```

Status endpoint:

```powershell
Invoke-RestMethod http://127.0.0.1:8999/status
```

Manual launch endpoint:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8999/play/current
```

## Constraints

- Telegram Web must stay open and logged in.
- The current video source must remain valid in that browser page.
- This is a bridge, not a downloader; it does not persist full files.
- Large binary ranges cross WebSocket, so performance depends on browser and local loopback throughput.
- v1 supports single-range requests. Multi-range requests are treated as the first range.
