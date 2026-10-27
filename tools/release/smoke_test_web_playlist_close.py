"""Close real MPV while EOF auto-advance awaits the next browser resolution."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import wave

from smoke_test_web_playlist import Browser, FixtureHandler
from smoke_test_bilibili_live import REPO, MpvPipe, require, wait_property
from bridge import BridgeState, handle_http_client
from playback_progress import ProgressStore
import web_playlist


class SlowNextBrowser(Browser):
    async def poll(self):
        handled = 0
        try:
            while True:
                await self.post({'type': 'pollPlaybackProgress'})
                for event in self.events[handled:]:
                    if event['type'] != 'resolveWebPlaylistItem':
                        continue
                    self.requests.append(event)
                    if event['itemId'] not in self.fixtures:
                        continue  # Keep the next P's on_load hook deferred.
                    await self.post({'type': 'submitWebPlaylistItem', 'playlistId': event['playlistId'],
                        'itemId': event['itemId'], 'generation': event['generation'],
                        'media': self.fixtures[event['itemId']]})
                handled = len(self.events)
                await asyncio.sleep(.03)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(type(error).__name__)


async def run(args):
    output = args.output_dir.resolve()
    require(output.is_relative_to(REPO) and 'build' in output.relative_to(REPO).parts,
            'Use a repository build output directory')
    run_dir = output / ('run-' + uuid.uuid4().hex[:10])
    fixtures = run_dir / 'fixtures'
    fixtures.mkdir(parents=True)
    shutil.copyfile(args.fixture, fixtures / 'first.mov')
    with wave.open(str(fixtures / 'first.wav'), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0' * (90 * 16000 * 2))
    http = ThreadingHTTPServer(('127.0.0.1', 0), partial(FixtureHandler, directory=str(fixtures)))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{http.server_port}'
    urls = {base + '/first.mov', base + '/first.wav'}
    real_media_url = web_playlist.media_url

    def fixture_url(value, *, subtitle=False):
        return value if value in urls else real_media_url(value, subtitle=subtitle)

    web_playlist.media_url = fixture_url
    store = ProgressStore(run_dir / 'playback-progress.json')
    bridge = BridgeState(3, 1024, str(args.mpv), '127.0.0.1', 0, progress_store=store)
    server = await asyncio.start_server(lambda r, w: handle_http_client(r, w, bridge), '127.0.0.1', 0)
    bridge.playlists.http_port = server.sockets[0].getsockname()[1]
    pid, sid = 'playlist-' + uuid.uuid4().hex, 'progress-' + uuid.uuid4().hex
    pipe = 'mpv-lazy-' + sid
    items = [{'itemId': '1001:' + cid, 'aid': '1001', 'cid': cid, 'bvid': 'BVfixture', 'p': page,
              'title': 'Part ' + str(page), 'duration': 90,
              'pageUrl': 'https://www.bilibili.com/video/BVfixture?p=' + str(page)}
             for cid, page in [('2002', 1), ('2003', 2)]]
    browser = SlowNextBrowser(bridge.playlists.http_port, {'1001:2002': {
        'video': base + '/first.mov', 'audio': base + '/first.wav', 'position': 0}})
    process = None
    try:
        playlist = (await browser.post({'type': 'registerWebPlaylist', 'playlistId': pid, 'sessionId': sid,
            'pipeName': pipe, 'items': items, 'selectedItemId': items[0]['itemId']}))['reply']
        await browser.post({'type': 'startPlaybackProgress', 'playlistId': pid, 'provider': 'bilibili',
            'identity': 'bilibili:1001:2002', 'pageUrl': items[0]['pageUrl'], 'sessionId': sid,
            'pipeName': pipe, 'position': 0, 'duration': 90, 'updatedAt': int(time.time() * 1000)})
        browser.task = asyncio.create_task(browser.poll())
        ipc = MpvPipe(pipe)
        command = [str(args.mpv), '--no-config', '--load-scripts=no', '--idle=yes', '--keep-open=yes',
            '--pause=yes', '--no-ytdl', '--vo=null', '--ao=null', '--terminal=no',
            '--input-ipc-server=' + ipc.path, '--log-file=' + str(run_dir / 'mpv.log'),
            '--script=' + str(args.playlist_script.resolve()), '--script=' + str(REPO / 'portable_config/scripts/web_progress.lua'),
            '--script-opts-append=web_playlist-id=' + pid, '--script-opts-append=web_playlist-session_id=' + sid,
            '--script-opts-append=web_playlist-resolve_timeout=20',
            '--script-opts-append=web_progress-identity=bilibili:1001:2002',
            '--script-opts-append=web_progress-session_id=' + sid, playlist['playlistUrl']]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, cwd=run_dir)
        current = 'user-data/mpv-lazy/web-playlist/current'
        await wait_property(ipc, current, lambda value: value and value.get('stage') == 'ready', timeout=20)
        duration = await ipc.command(['get_property', 'duration'])
        await ipc.command(['seek', duration - .2, 'absolute+exact'])
        await ipc.command(['set_property', 'pause', False])
        request = await wait_property(ipc, 'user-data/mpv-lazy/web-playlist/request',
                                     lambda value: value and value.get('itemId') == '1001:2003', timeout=8)
        require(request['generation'] == 2, 'The next P was not selected by EOF auto-advance')
        require(await ipc.command(['get_property', 'playlist-pos']) == 1, 'The next P is not current')
        started = time.monotonic()
        await ipc.command(['quit'])
        command_seconds = time.monotonic() - started
        exited = True
        try:
            await asyncio.to_thread(process.wait, timeout=args.max_close_seconds)
        except subprocess.TimeoutExpired:
            exited = False
        elapsed = time.monotonic() - started
        first_progress = store.get('bilibili:1001:2002')
        result = {'passed': False, 'autoAdvancedToPendingNextPart': True,
                  'quitReplySeconds': round(command_seconds, 4), 'exitSeconds': round(elapsed, 4),
                  'exitedWithinLimit': exited, 'completedPreviousPartRetained': bool(first_progress and first_progress.get('completed')),
                  'nextPartHasNoFabricatedProgress': store.get('bilibili:1001:2003') is None,
                  'scope': 'Real MPV, local HTTP media and deliberately unanswered browser resolution. No website or account access.'}
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        require(exited and elapsed < args.max_close_seconds, 'Closing during next-P resolution waited for the browser timeout')
        require(result['completedPreviousPartRetained'], 'Closing erased the completed previous part')
        require(result['nextPartHasNoFabricatedProgress'], 'Cancelled next part recorded a fabricated progress entry')
        require(not browser.errors, 'Browser fixture failed: ' + str(browser.errors))
        result['passed'] = True
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return result
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=8)
        if browser.task:
            browser.task.cancel()
            await asyncio.gather(browser.task, return_exceptions=True)
        await bridge.playlists.close()
        await bridge.progress.close()
        server.close()
        await server.wait_closed()
        web_playlist.media_url = real_media_url
        http.shutdown()
        http.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mpv', required=True, type=Path)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--playlist-script', type=Path, default=REPO / 'portable_config/scripts/web_playlist.lua')
    parser.add_argument('--max-close-seconds', type=float, default=2)
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))
