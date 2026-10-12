#!/usr/bin/env python3
"""Real MPV, named-pipe, WebSocket and disk resume test; no website credentials."""
from __future__ import annotations
import argparse
import asyncio
from functools import partial
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import quote
import uuid

from smoke_test_bilibili_live import REPO, MpvPipe, require, wait_property
from smoke_test_stream_quality import QuietHandler as RangeHandler
from bridge import BridgeState, ws_handler, handle_http_client
from playback_progress import ProgressStore
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect

PROPERTY = 'user-data/mpv-lazy/playback-progress'


class HttpChannel:
    def __init__(self, port):
        self.port = port
        self.client = 'progress-http-' + uuid.uuid4().hex
        self.messages = []

    async def post(self, request):
        body = json.dumps({'clientId': self.client, 'request': request}).encode()
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(f'POST /playback-progress HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        headers, _, content = raw.partition(b'\r\n\r\n')
        require(b'200 OK' in headers, 'HTTP progress request failed')
        result = json.loads(content)
        if result['reply']:
            self.messages.append(json.dumps(result['reply']))
        self.messages.extend(json.dumps(event) for event in result['events'])

    async def send(self, raw):
        request = json.loads(raw)
        if request['type'] == 'getPlaybackProgress':
            request['rebind'] = True
        await self.post(request)

    async def recv(self):
        while not self.messages:
            await self.post({'type': 'pollPlaybackProgress'})
            if not self.messages:
                await asyncio.sleep(.1)
        return self.messages.pop(0)

    async def close(self):
        await self.post({'type': 'detachPlaybackProgress'})


async def receive(ws, kind, predicate=lambda value: True, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
        if message['type'] == 'error':
            raise AssertionError(message.get('message', 'relay error'))
        if message['type'] == kind and predicate(message):
            return message
    raise AssertionError('Progress event was not received: ' + kind)


async def wait_disk(path, position, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            record = json.loads(path.read_text(encoding='utf-8'))['records'].get('bilibili:1001:2002')
            if record and math.isclose(record['position'], position, abs_tol=.1):
                return record
        await asyncio.sleep(.1)
    raise AssertionError('Progress did not reach disk')


async def run(args):
    require(os.name == 'nt', 'Windows MPV is required')
    mpv, fixture, output = args.mpv.resolve(), args.fixture.resolve(), args.output_dir.resolve()
    require(mpv.is_file() and fixture.is_file(), 'MPV or local fixture is missing')
    require(output.is_relative_to(REPO) and 'build' in output.relative_to(REPO).parts, 'Output must be in the repository build directory')
    run_dir = output / ('run-' + uuid.uuid4().hex[:10])
    fixtures = run_dir / 'fixtures'
    fixtures.mkdir(parents=True)
    for name in ('first.mov', 'second.mov', 'unrelated.mov'):
        shutil.copyfile(fixture, fixtures / name)
    http = ThreadingHTTPServer(('127.0.0.1', 0), partial(RangeHandler, directory=str(fixtures)))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{http.server_port}'
    variants = [{'url': base + '/' + name, 'title': label, 'width': 640, 'height': 360, 'codec': 'PNG'}
                for name, label in (('first.mov', 'one'), ('second.mov', 'two'))]
    encoded = quote(json.dumps(variants, separators=(',', ':')), safe='')
    store_path = run_dir / 'playback-progress.json'
    state = BridgeState(2, 1024, str(mpv), '127.0.0.1', 0, progress_store=ProgressStore(store_path))
    websocket_server = await serve(lambda ws: ws_handler(ws, state), '127.0.0.1', 0)
    progress_http = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), '127.0.0.1', 0)
    url = f'ws://127.0.0.1:{websocket_server.sockets[0].getsockname()[1]}/ws'
    content = {'provider': 'bilibili', 'identity': 'bilibili:1001:2002',
               'pageUrl': 'https://www.bilibili.com/video/BV1fixture?p=2', 'duration': 60, 'paused': True}
    players = []
    ws = None

    async def channel():
        return HttpChannel(progress_http.sockets[0].getsockname()[1]) if args.transport == 'http' else await connect(url)

    async def launch(position):
        sid = 'progress-' + uuid.uuid4().hex
        pipe = 'mpv-lazy-' + sid
        payload = {'type': 'startPlaybackProgress', 'requestId': 'start-' + sid, **content,
                   'sessionId': sid, 'pipeName': pipe, 'position': position, 'updatedAt': int(time.time() * 1000)}
        await ws.send(json.dumps(payload))
        await receive(ws, 'playbackProgressStarted', lambda m: m['sessionId'] == sid)
        ipc = MpvPipe(pipe)
        command = [str(mpv), '--no-config', '--load-scripts=no', '--idle=yes', '--no-ytdl', '--pause=yes',
                   '--vo=null', '--ao=null', '--terminal=no', f'--start={position}', '--input-ipc-server=' + ipc.path,
                   '--http-header-fields=X-Quality-Smoke: expected',
                   '--script=' + str(REPO / 'portable_config/scripts/web_progress.lua'),
                   '--script=' + str(REPO / 'portable_config/scripts/quality-menu.lua'),
                   f'--script-opts-append=web_progress-identity={content["identity"]},web_progress-session_id={sid}' if args.legacy_options else
                   f'--script-opts-append=web_progress-identity={content["identity"]}',
                   *([] if args.legacy_options else [f'--script-opts-append=web_progress-session_id={sid}']),
                   f'--script-opts-append=quality-menu-stream_variants=%{len(encoded)}%{encoded}', variants[0]['url']]
        with (run_dir / f'{sid}.log').open('wb') as log:
            player = subprocess.Popen(command, cwd=run_dir, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        players.append(player)
        snap = await wait_property(ipc, PROPERTY, lambda s: s and s.get('ready') and abs(s['position'] - position) < .1, timeout=15)
        await receive(ws, 'playbackProgress', lambda m: m['sessionId'] == sid and abs(m['position'] - position) < .1)
        return ipc, player, sid, snap

    try:
        ws = await channel()
        ipc, first, sid, snap = await launch(12.5)
        for position in (7.25, 0, 23.9):
            await ipc.command(['seek', position, 'absolute+exact'])
            await receive(ws, 'playbackProgress', lambda m: m['sessionId'] == sid and abs(m['position'] - position) < .1)
        await ipc.command(['script-message-to', 'quality_menu', 'video-format-set', variants[0]['url'], variants[1]['url']])
        await wait_property(ipc, 'path', variants[1]['url'])
        await wait_property(ipc, PROPERTY, lambda s: s and s.get('ready') and abs(s['position'] - 23.9) < .1)
        require(not state.progress.sessions[sid].task.done(), 'Quality switch ended progress session')
        # A disconnected browser must not stop local MPV resume persistence.
        await ws.close()
        await ipc.command(['seek', 31.875, 'absolute+exact'])
        saved = await wait_disk(store_path, 31.875)
        ws = await channel()
        await ws.send(json.dumps({'type': 'getPlaybackProgress', 'requestId': 'reopen', **content}))
        restored = await receive(ws, 'playbackProgressState')
        require(restored.get('activeSessionId') == sid and abs(restored['record']['position'] - 31.875) < .1, 'Page reload did not rebind the active player')
        await ws.send(json.dumps({'type': 'getPlaybackProgress', 'requestId': 'other-part', **content, 'identity': 'bilibili:1001:2003'}))
        other_part = await receive(ws, 'playbackProgressState', lambda m: m['requestId'] == 'other-part')
        require(other_part['record'] is None, 'Another part inherited unrelated progress')
        await ipc.command(['quit'])
        first.wait(timeout=8)
        await receive(ws, 'playbackProgressStopped', lambda m: m['sessionId'] == sid)
        reopened_store = ProgressStore(store_path)
        resume = reopened_store.get(content['identity'])
        ipc2, second, sid2, snap2 = await launch(resume['position'])
        await ipc2.command(['loadfile', base + '/unrelated.mov', 'replace'])
        await receive(ws, 'playbackProgressStopped', lambda m: m['sessionId'] == sid2)
        require(abs(state.progress.store.get(content['identity'])['position'] - 31.875) < .1, 'Different file overwrote source progress')
        result = {'passed': True, 'webToMpvStart': snap['position'], 'backwardAndZeroSeekPassed': True,
                  'transport': args.transport,
                  'legacyOptions': args.legacy_options,
                  'qualitySwitchPosition': 23.9, 'savedWithBrowserClosed': saved['position'],
                  'reopenedMpvPosition': snap2['position'], 'differentPartIsolated': True,
                  'differentFileStopsRecording': True, 'networkScope': 'Local HTTP and real MPV; no actual website history request.'}
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return result
    finally:
        if ws:
            await ws.close()
        await state.progress.close()
        websocket_server.close()
        await websocket_server.wait_closed()
        progress_http.close()
        await progress_http.wait_closed()
        http.shutdown()
        http.server_close()
        for player in players:
            if player.poll() is None:
                player.terminate()
                player.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mpv', required=True, type=Path)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--transport', choices=['ws', 'http'], default='ws')
    parser.add_argument('--legacy-options', action='store_true', help='Verify .16-.18 combined -append compatibility')
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))


if __name__ == '__main__':
    main()
