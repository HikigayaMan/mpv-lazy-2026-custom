"""Exercise a 1001-entry hierarchy in actual MPV with a local browser resolver."""
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
import uuid
import wave

from smoke_test_web_playlist import Browser, FixtureHandler, REPO, MpvPipe, require, wait_property
from bridge import BridgeState, handle_http_client
from playback_progress import ProgressStore
import web_playlist


async def run(args):
    output = args.output_dir.resolve()
    require(output.is_relative_to(REPO) and 'build' in output.relative_to(REPO).parts, 'Use a repository build output directory')
    work = output / ('run-' + uuid.uuid4().hex[:10])
    fixtures = work / 'fixtures'
    fixtures.mkdir(parents=True)
    shutil.copyfile(args.fixture, fixtures / 'video.mov')
    with wave.open(str(fixtures / 'audio.wav'), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0' * (90 * 16000 * 2))
    http = ThreadingHTTPServer(('127.0.0.1', 0), partial(FixtureHandler, directory=str(fixtures)))
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{http.server_port}'
    video, audio = [base + '/' + name + '?test_signature=' + 'x' * 2600 for name in ('video.mov', 'audio.wav')]
    real_media_url = web_playlist.media_url
    def fixture_url(value, *, subtitle=False):
        if value in (video, audio): return value
        return real_media_url(value, subtitle=subtitle)
    web_playlist.media_url = fixture_url
    store = ProgressStore(work / 'playback-progress.json')
    state = BridgeState(3, 1024, str(args.mpv), '127.0.0.1', 0, progress_store=store)
    server = await asyncio.start_server(lambda r, w: handle_http_client(r, w, state), '127.0.0.1', 0)
    state.playlists.http_port = server.sockets[0].getsockname()[1]
    playlist_id, sid = 'playlist-' + uuid.uuid4().hex, 'progress-' + uuid.uuid4().hex
    pipe = 'mpv-lazy-' + sid
    items = []
    for group in range(996):
        parts = 3 if group == 0 else 4 if group == 1 else 1
        aid = str(1001 + group)
        for page in range(1, parts + 1):
            cid = str(2002 + len(items))
            title = 'Multi A' if group == 0 else 'Multi B' if group == 1 else 'Video ' + str(group + 1)
            items.append({'itemId': aid + ':' + cid, 'aid': aid, 'cid': cid, 'bvid': 'BVfixture',
                'p': page, 'title': title + ' / Part ' + str(page), 'duration': 90,
                'pageUrl': 'https://www.bilibili.com/video/BVfixture?p=' + str(page),
                'sectionId': 'default', 'sectionTitle': '', 'videoId': aid, 'videoTitle': title,
                'partTitle': 'Part ' + str(page), 'partCount': parts, 'order': len(items)})
    require(len(items) == 1001, 'Incorrect hierarchy fixture count')
    selected = items[5]
    media = {'video': video, 'audio': audio, 'variants': [
        {'url': video, 'audio_url': audio, 'width': 640, 'height': 360, 'quality': 80, 'title': '360p'}]}
    browser = Browser(state.playlists.http_port, {item['itemId']: media for item in items})
    player = None
    async def register_complete(extend=False):
        await browser.post({'type': 'registerWebPlaylist', 'requestId': 'begin', 'chunked': True,
            'extendExisting': extend,
            'playlistId': playlist_id, 'sessionId': sid, 'pipeName': pipe,
            'collectionId': 'fixture', 'collectionTitle': 'Fixture collection',
            'selectedItemId': selected['itemId'], 'initialPosition': 12.5})
        for chunk, offset in enumerate(range(0, len(items), 50)):
            await browser.post({'type': 'appendWebPlaylistItems', 'requestId': 'chunk-' + str(chunk),
                'playlistId': playlist_id, 'chunkIndex': chunk, 'items': items[offset:offset + 50]})
        complete = (await browser.post({'type': 'commitWebPlaylist', 'requestId': 'commit',
            'playlistId': playlist_id, 'expectedItemCount': len(items)}))['reply']
        require(complete['itemCount'] == 1001 and complete['selectedIndex'] == 5, 'Chunked registration lost entries')
        return complete
    try:
        if args.fast_start:
            registered = (await browser.post({'type': 'registerWebPlaylist', 'requestId': 'bootstrap', 'bootstrap': True,
                'playlistId': playlist_id, 'sessionId': sid, 'pipeName': pipe, 'selectedItemId': selected['itemId'],
                'collectionId': 'fixture', 'collectionTitle': 'Fixture collection', 'items': [selected],
                'initialPosition': 12.5}))['reply']
            require(registered['itemCount'] == 1 and registered['metadataPending'], 'Bootstrap claimed a complete list')
        else:
            registered = await register_complete()
        await browser.post({'type': 'startPlaybackProgress', 'requestId': 'progress', 'playlistId': playlist_id,
            'provider': 'bilibili', 'identity': 'bilibili:' + selected['itemId'], 'pageUrl': selected['pageUrl'],
            'sessionId': sid, 'pipeName': pipe, 'position': 12.5, 'duration': 90})
        if args.early_metadata:
            require(args.fast_start, 'Early metadata requires bootstrap mode')
            await register_complete(extend=True)
        browser.task = asyncio.create_task(browser.poll())
        ipc = MpvPipe(pipe)
        command = [str(args.mpv), '--no-config', '--load-scripts=no', '--idle=yes', '--no-ytdl',
            '--pause=yes', '--vo=null', '--ao=null', '--terminal=no', '--playlist-start=' + ('0' if args.fast_start else '5'),
            '--input-ipc-server=' + ipc.path, '--log-file=' + str(work / 'mpv-details.log'),
            *['--script=' + str(REPO / 'portable_config/scripts' / name) for name in ('web_playlist.lua', 'web_progress.lua', 'quality-menu.lua')],
            '--script=' + str(REPO / 'portable_config/scripts/uosc'),
            '--script-opts-append=web_playlist-id=' + playlist_id, '--script-opts-append=web_playlist-session_id=' + sid,
            '--script-opts-append=web_progress-identity=bilibili:' + selected['itemId'],
            '--script-opts-append=web_progress-session_id=' + sid, registered['playlistUrl']]
        with (work / 'mpv.log').open('wb') as log:
            player = subprocess.Popen(command, stdout=log, stderr=log, cwd=work, creationflags=subprocess.CREATE_NO_WINDOW)
        progress = 'user-data/mpv-lazy/playback-progress'
        status = 'user-data/uosc/web-playlist-menu'
        await wait_property(ipc, progress, lambda s: s and s.get('ready') and abs(s['position'] - 12.5) < .1, timeout=25)
        if args.fast_start and not args.early_metadata:
            require(await ipc.command(['get_property', 'playlist-count']) == 1, 'Startup waited for the complete collection')
            before_entry = (await ipc.command(['get_property', 'playlist']))[0]
            before_tracks = await ipc.command(['get_property', 'track-list'])
            before_current = await ipc.command(['get_property', 'user-data/mpv-lazy/web-playlist/current'])
            await wait_property(ipc, 'user-data/mpv-lazy/web-playlist/manifest-ack',
                lambda s: s and s.get('stage') == 'commit' and s.get('ok') and s.get('itemCount') == 1)
            await register_complete(extend=True)
            await wait_property(ipc, 'user-data/mpv-lazy/web-playlist/extend-ack',
                lambda s: s and s.get('ok') and s.get('itemCount') == 1001, timeout=30)
            current_index = await ipc.command(['get_property', 'playlist-pos'])
            current_entry = {key: await ipc.command(['get_property', f'playlist/{current_index}/{key}'])
                for key in ('id', 'filename')}
            require(current_entry['id'] == before_entry['id'] and current_entry['filename'] == before_entry['filename'],
                'Extending replaced the playing native entry')
            require(await ipc.command(['get_property', 'track-list']) == before_tracks, 'Extending changed audio/subtitle tracks')
            require(abs((await ipc.command(['get_property', 'time-pos'])) - 12.5) < .1 and
                await ipc.command(['get_property', 'pause']) is True, 'Extending changed position or pause')
            require(await ipc.command(['get_property', 'playlist-pos']) == 5, 'Extending lost the full collection index')
            after_current = await ipc.command(['get_property', 'user-data/mpv-lazy/web-playlist/current'])
            require(after_current['generation'] == before_current['generation'] and after_current['video'] == before_current['video'],
                'Extending reloaded or changed quality of the playing media')
            require(len([r for r in browser.requests if r['itemId'] == selected['itemId']]) == 1,
                'Bootstrap source was unnecessarily resolved twice')
        await wait_property(ipc, 'user-data/mpv-lazy/web-playlist/manifest-ack',
            lambda s: s and s.get('stage') == 'commit' and s.get('ok') and s.get('itemCount') == 1001, timeout=30)
        require(await ipc.command(['get_property', 'playlist-count']) == 1001, 'Native playlist truncated a large collection')
        if args.early_metadata:
            c = await ipc.command(['get_property', 'user-data/mpv-lazy/web-playlist/current'])
            require(c['generation'] == 1 and c['itemId'] == selected['itemId'], 'Early metadata reloaded the first media')
            require(abs((await ipc.command(['get_property', 'time-pos'])) - 12.5) < .1 and
                await ipc.command(['get_property', 'pause']) is True, 'Early metadata changed playback state')
            require(len([r for r in browser.requests if r['itemId'] == selected['itemId']]) == 1,
                'Early metadata resolved the bootstrap twice')
        await ipc.command(['script-binding', 'uosc/playlist'])
        menu = await wait_property(ipc, status, lambda s: s and s.get('partCount') == 1001)
        require(menu['videoCount'] == 996, 'Single-part videos created extra parents')
        group_b = next(row for row in menu['rows'] if row['kind'] == 'group' and row['title'] == 'Multi B')
        require(group_b['expanded'] and any(row['depth'] == 1 and row['title'] == 'Part 3' and row['active'] for row in menu['rows']), 'Current ancestors did not expand')
        before = menu['visibleCount']
        async def action(value):
            await ipc.command(['script-message-to', 'uosc', 'web-playlist-menu-action', json.dumps({'type': 'activate', 'value': value})])
        await action(group_b['value'])
        menu = await wait_property(ipc, status, lambda s: s and s['visibleCount'] == before - 4)
        require(await ipc.command(['get_property', 'playlist-count']) == 1001, 'Collapsing removed native entries')
        group_a = next(row for row in menu['rows'] if row['kind'] == 'group' and row['title'] == 'Multi A')
        await action(group_a['value'])
        menu = await wait_property(ipc, status, lambda s: s and s['visibleCount'] == before - 1)
        part_two = next(row for row in menu['rows'] if row['title'] == 'Part 2' and row['kind'] == 'leaf')
        await ipc.command(['playlist-shuffle'])
        await action(part_two['value'])
        await wait_property(ipc, progress, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2003')
        await ipc.command(['playlist-unshuffle'])
        await ipc.command(['script-binding', 'uosc/next'])
        await wait_property(ipc, progress, lambda s: s and s.get('ready') and s['identity'] == 'bilibili:1001:2004')
        require(not browser.errors, str(browser.errors))
        result = {'passed': True, 'nativeEntries': 1001, 'videoParents': 996, 'multiPartCounts': [3, 4],
            'chunkedHttpRegistration': True, 'manifestIpcAck': True, 'currentAncestorsExpand': True,
            'collapsePreservesNativePlaylist': True, 'singlePartHasNoChildFolder': True,
            'selectionAfterNativeShuffle': True, 'nativeNext': True, 'longMediaUrls': True,
            'fastStartBeforeFullMetadata': args.fast_start,
            'earlyMetadataBeforePlayerLaunch': args.early_metadata,
            'extensionPreservesCurrentNativeIdAndTracks': args.fast_start,
            'extensionPreservesPositionPauseAndQuality': args.fast_start,
            'scope': 'Actual MPV with local media and simulated browser; no account or website requests.'}
        (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
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
    parser.add_argument('--fast-start', action='store_true', help='Play one bootstrap entry, then extend it to the complete collection without reloading')
    parser.add_argument('--early-metadata', action='store_true', help='Finish metadata before MPV opens the bootstrap URI')
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))
