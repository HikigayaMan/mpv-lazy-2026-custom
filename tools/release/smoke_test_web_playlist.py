"""Real native MPV playlist and lazy IPC resolution, using isolated local fixtures."""
from __future__ import annotations
import argparse
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import wave

from smoke_test_bilibili_live import REPO, MpvPipe, require, wait_property
from smoke_test_stream_quality import QuietHandler
from bridge import BridgeState, handle_http_client
from playback_progress import ProgressStore
import web_playlist


class FixtureHandler(QuietHandler):
    def send_head(self):
        self.headers['X-Quality-Smoke'] = 'expected'
        return super().send_head()


class Browser:
    def __init__(self, port, fixtures):
        self.port, self.fixtures = port, fixtures
        self.client = 'progress-http-' + uuid.uuid4().hex
        self.events, self.requests, self.errors = [], [], []
        self.task = None

    async def post(self, request):
        data = json.dumps({'clientId': self.client, 'request': request}).encode()
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(f'POST /playback-progress HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n\r\n'.encode() + data)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        headers, _, body = raw.partition(b'\r\n\r\n')
        require(b'200 OK' in headers, 'Playlist HTTP request failed: ' + body.decode())
        result = json.loads(body)
        self.events.extend(result['events'])
        return result

    async def poll(self):
        handled = 0
        try:
            while True:
                await self.post({'type': 'pollPlaybackProgress'})
                for event in self.events[handled:]:
                    if event['type'] == 'resolveWebPlaylistItem':
                        self.requests.append(event)
                        media = dict(self.fixtures[event['itemId']])
                        record = event.get('resumeRecord')
                        media['position'] = (0 if record.get('completed') else record['position']) if record else (event.get('initialPosition') or 0)
                        await self.post({'type': 'submitWebPlaylistItem', 'requestId': 'resolve-' + str(event['generation']),
                            'playlistId': event['playlistId'], 'itemId': event['itemId'], 'generation': event['generation'], 'media': media})
                handled = len(self.events)
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(str(error))


async def until(predicate, timeout=15):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError('Native playlist test timed out')
        await asyncio.sleep(.1)


async def run(args):
    output = args.output_dir.resolve()
    require(output.is_relative_to(REPO) and 'build' in output.relative_to(REPO).parts, 'Use a repository build output directory')
    run_dir = output / ('run-' + uuid.uuid4().hex[:10])
    fixtures = run_dir / 'fixtures'
    fixtures.mkdir(parents=True)
    for name in ('first.mov', 'second.mov', 'low.mov'):
        shutil.copyfile(args.fixture, fixtures / name)
    for name in ('first', 'second'):
        with wave.open(str(fixtures / (name + '.wav')), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0' * (90 * 16000 * 2))
        (fixtures / (name + '.srt')).write_text('1\n00:00:00,000 --> 00:00:59,000\n' + name + '\n', encoding='utf-8')
    http = ThreadingHTTPServer(('127.0.0.1', 0), partial(FixtureHandler, directory=str(fixtures)))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{http.server_port}'
    # Real signed Bilibili media replies exceed one Windows pipe buffer.
    # Keep synthetic URLs long enough to exercise bounded partial writes.
    def test_url(name):
        return base + '/' + name + '?test_signature=' + 'x' * 2600
    allowed_urls = {test_url(path.name) for path in fixtures.iterdir()}
    # Only this test server's exact paths are allowed. Production retains its CDN whitelist.
    real_media_url = web_playlist.media_url
    def fixture_url(value, *, subtitle=False):
        if value in allowed_urls:
            return value
        return real_media_url(value, subtitle=subtitle)
    web_playlist.media_url = fixture_url
    store = ProgressStore(run_dir / 'playback-progress.json')
    state = BridgeState(3, 1024, str(args.mpv), '127.0.0.1', 0, progress_store=store)
    server = await asyncio.start_server(lambda r, w: handle_http_client(r, w, state), '127.0.0.1', 0)
    state.playlists.http_port = server.sockets[0].getsockname()[1]
    playlist_id, sid = 'playlist-' + uuid.uuid4().hex, 'progress-' + uuid.uuid4().hex
    pipe = 'mpv-lazy-' + sid
    items = [{'itemId': '1001:' + cid, 'aid': '1001', 'cid': cid, 'bvid': 'BVfixture', 'p': p,
              'title': title, 'duration': 90, 'pageUrl': 'https://www.bilibili.com/video/BVfixture?p=' + str(p)}
             for cid, p, title in [('2002', 1, 'Fixture A'), ('2003', 2, 'Fixture B')]]
    medias = {}
    for item, name in zip(items, ('first', 'second')):
        video, audio = test_url(name + '.mov'), test_url(name + '.wav')
        medias[item['itemId']] = {'video': video, 'audio': audio, 'subtitle': test_url(name + '.srt'),
            'variants': [{'url': video, 'audio_url': audio, 'width': 640, 'height': 360, 'quality': 80, 'title': '360p'}]}
    medias['1001:2002']['variants'].append({'url': test_url('low.mov'), 'audio_url': test_url('first.wav'), 'width': 426, 'height': 240, 'quality': 32, 'title': '240p'})
    browser = Browser(state.playlists.http_port, medias)
    player = None
    try:
        registered = (await browser.post({'type': 'registerWebPlaylist', 'requestId': 'register', 'playlistId': playlist_id,
            'sessionId': sid, 'pipeName': pipe, 'items': items, 'selectedItemId': '1001:2003', 'initialPosition': 12.5}))['reply']
        await browser.post({'type': 'startPlaybackProgress', 'requestId': 'progress', 'playlistId': playlist_id,
            'provider': 'bilibili', 'identity': 'bilibili:1001:2003', 'pageUrl': items[1]['pageUrl'], 'sessionId': sid,
            'pipeName': pipe, 'position': 12.5, 'duration': 90, 'updatedAt': int(time.time() * 1000)})
        browser.task = asyncio.create_task(browser.poll())
        ipc = MpvPipe(pipe)
        command = [str(args.mpv), '--no-config', '--load-scripts=no', '--idle=yes', '--no-ytdl', '--pause=yes', '--vo=null', '--ao=null', '--terminal=no',
            '--input-ipc-server=' + ipc.path, '--playlist-start=1', '--log-file=' + str(run_dir / 'mpv-details.log'),
            *['--script=' + str(REPO / 'portable_config/scripts' / name) for name in ('web_playlist.lua', 'web_progress.lua', 'quality-menu.lua')],
            '--script=' + str(REPO / 'portable_config/scripts/uosc'),
            '--script-opts-append=web_playlist-id=' + playlist_id, '--script-opts-append=web_playlist-session_id=' + sid,
            '--script-opts-append=web_progress-identity=bilibili:1001:2003', '--script-opts-append=web_progress-session_id=' + sid,
            registered['playlistUrl']]
        with (run_dir / 'mpv.log').open('wb') as log:
            player = subprocess.Popen(command, stdout=log, stderr=log, cwd=run_dir, creationflags=subprocess.CREATE_NO_WINDOW)
        ipc = MpvPipe(pipe)
        prop = 'user-data/mpv-lazy/playback-progress'
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2003' and abs(s['position'] - 12.5) < .1, timeout=20)
        require(await ipc.command(['get_property', 'playlist-count']) == 2, 'MPV did not receive two native entries')
        entries = await ipc.command(['get_property', 'playlist'])
        require([entry.get('title') for entry in entries] == ['Fixture A', 'Fixture B'], 'Native playlist titles were not preserved')
        await ipc.command(['set_property', 'playlist-pos', 0])
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2002')
        await ipc.command(['seek', 7.25, 'absolute+exact'])
        await until(lambda: store.get('bilibili:1001:2002') and abs(store.get('bilibili:1001:2002')['position'] - 7.25) < .1)
        await wait_property(ipc, 'user-data/quality-menu/state', lambda s: s and s.get('videoCount') == 2)
        path = await ipc.command(['get_property', 'path'])
        await ipc.command(['script-message-to', 'quality_menu', 'video-format-set', path, test_url('low.mov')])
        await wait_property(ipc, 'user-data/mpv-lazy/web-playlist/current', lambda s: s and s.get('stage') == 'ready' and s.get('video') == test_url('low.mov'))
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and abs(s['position'] - 7.25) < .1)
        require(await ipc.command(['get_property', 'playlist-count']) == 2, 'Quality change erased playlist entries')
        await ipc.command(['set_property', 'playlist-pos', 1])
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2003')
        await wait_property(ipc, 'user-data/quality-menu/state', lambda s: s and s.get('videoCount') == 1)
        await ipc.command(['playlist-shuffle'])
        require(await ipc.command(['get_property', 'playlist-count']) == 2, 'Shuffle erased playlist entries')
        await ipc.command(['playlist-unshuffle'])
        await ipc.command(['script-binding', 'uosc/shuffle'])
        await ipc.command(['script-binding', 'uosc/next'])
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2002')
        await ipc.command(['script-binding', 'uosc/shuffle'])
        await ipc.command(['set_property', 'playlist-pos', 1])
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2003')
        await ipc.command(['set_property', 'loop-file', 'inf'])
        await ipc.command(['seek', (await ipc.command(['get_property', 'duration'])) - .2, 'absolute+exact'])
        await ipc.command(['set_property', 'pause', False])
        looped = await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['position'] < 3)
        await ipc.command(['set_property', 'pause', True])
        require(looped['identity'] == 'bilibili:1001:2003', 'Single-file repeat advanced to another item')
        await ipc.command(['set_property', 'loop-file', 'no'])
        await ipc.command(['set_property', 'loop-playlist', 'inf'])
        await ipc.command(['seek', (await ipc.command(['get_property', 'duration'])) - .2, 'absolute+exact'])
        await ipc.command(['set_property', 'pause', False])
        await wait_property(ipc, prop, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2002')
        await ipc.command(['set_property', 'pause', True])
        require(not browser.errors, str(browser.errors))
        result = {'passed': True, 'nativePlaylistCount': 2, 'selectedSecondEntryStart': 12.5, 'manualSelection': True,
            'shuffle': True, 'uoscShuffleButtonAndNext': True, 'singleFileRepeat': True, 'playlistRepeat': True, 'qualityPreservesPlaylistAndPosition': True,
            'perItemMetadataAndProgress': True, 'longSyntheticMediaUrls': True, 'transport': 'HTTP + actual Windows IPC',
            'scope': 'Isolated local media and simulated browser resolver; no actual website/account request.'}
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        return result
    finally:
        if browser.task:
            browser.task.cancel()
            await asyncio.gather(browser.task, return_exceptions=True)
        await state.playlists.close()
        await state.progress.close()
        server.close()
        await server.wait_closed()
        web_playlist.media_url = real_media_url
        http.shutdown()
        http.server_close()
        if player and player.poll() is None:
            player.terminate()
            player.wait(timeout=8)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mpv', required=True, type=Path)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))
