#!/usr/bin/env python3
"""Real uosc hover -> cache excerpt -> local thumbnail, using a local HTTP source."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
from http.server import ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from smoke_test_bilibili_live import REPO, MpvPipe, wait_property
from smoke_test_stream_quality import QuietHandler


class CountedHandler(QuietHandler):
    requests = 0
    failures = []

    def send_head(self):
        type(self).requests += 1
        return super().send_head()


def make_preview_fixture(mpv, fixture):
    # dump-cache's recorder needs enough video packets to begin muxing. Use a
    # normal 24-fps video rather than the 1-fps quality-switch test fixture.
    result = subprocess.run([str(mpv), '--no-config', '--terminal=yes', '--force-window=no',
        '--audio=no', '--untimed', '--ovc=png', '--of=mov', '--frames=2160',
        '--o=' + str(fixture), 'av://lavfi:testsrc=size=640x360:rate=24'],
        capture_output=True, encoding='utf-8', errors='replace', timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0 and fixture.is_file(), result.stderr[-1000:]


async def run(args):
    output = args.output_dir.resolve() / ('run-' + uuid.uuid4().hex[:10])
    output.mkdir(parents=True)
    fixture_dir = output / 'fixtures'
    fixture_dir.mkdir()
    fixture = fixture_dir / 'cached.mov'
    await asyncio.to_thread(make_preview_fixture, args.mpv, fixture)
    config = output / 'config'
    config.mkdir()
    name = 'mpv-cached-thumbnails-' + uuid.uuid4().hex
    ipc = MpvPipe(name)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(CountedHandler, directory=str(fixture_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thumb_path = output / 'thumbnail'
    command = [str(args.mpv), '--no-config', '--load-scripts=no', '--no-ytdl', '--osc=no',
               '--vo=gpu-next', '--ao=null', '--force-window=yes', '--autofit=960x540',
               '--idle=yes', '--pause=yes', '--terminal=no', '--cache=yes', '--cache-secs=20',
               '--demuxer-max-bytes=16MiB', '--http-header-fields=X-Quality-Smoke: expected',
               '--input-ipc-server=' + ipc.path, '--log-file=' + str(output / 'mpv.log'),
               '--msg-level=all=warn', '--config-dir=' + str(config),
               '--script=' + str(REPO / 'portable_config/scripts/uosc'),
               '--script=' + str(REPO / 'portable_config/scripts/thumb_engine'),
               '--script-opts=' + ','.join([
                   'thumb_engine-binpath=' + str(args.mpv), 'thumb_engine-min_duration=0',
                   'thumb_engine-cache_only_network=yes', 'thumb_engine-network=no',
                   'thumb_engine-rescale=1', 'thumb_engine-direct_io=no',
                   'thumb_engine-tnpath=' + str(thumb_path), 'uosc-language=zh',
               ]), f'http://127.0.0.1:{server.server_port}/{fixture.name}']
    env = os.environ.copy()
    env['TEMP'] = env['TMP'] = str(output)
    process = subprocess.Popen(command, cwd=args.mpv.parent, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    result = {'passed': False, 'scope': 'Local HTTP network fixture; real MPV/uosc mouse hover.', 'run': str(output)}
    try:
        await wait_property(ipc, 'user-data/mpv/thumbnailer/enabled', True)
        cache = await wait_property(ipc, 'demuxer-cache-state', lambda c: c and c.get('idle') and bool(c.get('seekable-ranges')))
        dimensions = await wait_property(ipc, 'osd-dimensions', lambda d: d and d.get('w', 0) > 0)
        duration = await ipc.command(['get_property', 'duration'])
        before = await ipc.command(['get_property', 'time-pos'])
        initial_requests = CountedHandler.requests
        target = 12.0
        # Use the application's documented mouse command; no OS cursor control.
        await ipc.command(['mouse', round(dimensions['w'] * target / duration), dimensions['h'] - 8])
        preview = await wait_property(ipc, 'user-data/thumb_engine/cache-preview', lambda p: p and p.get('status') == 'ready', timeout=12)
        assert abs(preview['time'] - target) < 0.3, preview
        assert CountedHandler.requests == initial_requests, 'Thumbnail opened a new network request'
        assert not CountedHandler.failures, CountedHandler.failures
        assert await ipc.command(['get_property', 'pause']) is True
        assert abs(await ipc.command(['get_property', 'time-pos']) - before) < 0.1
        bgra = Path(str(thumb_path) + str(process.pid) + '.bgra')
        assert bgra.is_file() and bgra.stat().st_size == 320 * 180 * 4
        assert not list(output.glob('*.cache-*')), 'Excerpt or raw decoder job left temporary files'
        cached_shot = output / 'cached-timeline-thumbnail.png'
        await ipc.command(['screenshot-to-file', str(cached_shot), 'window'])

        # Outside the actual cache, the timeline must clear the old image and
        # must not send a request that downloads the selected position.
        uncached_target = min(duration - 2, cache['seekable-ranges'][0]['end'] + 25)
        await ipc.command(['mouse', round(dimensions['w'] * uncached_target / duration), dimensions['h'] - 8])
        await wait_property(ipc, 'user-data/thumb_engine/cache-preview', lambda p: p and p.get('status') == 'idle')
        assert CountedHandler.requests == initial_requests
        uncached_shot = output / 'uncached-timeline-no-thumbnail.png'
        await ipc.command(['screenshot-to-file', str(uncached_shot), 'window'])
        # Bypassing the UI cannot bypass the engine's cache gate.
        await ipc.command(['script-message-to', 'thumb_engine', 'thumb', str(uncached_target), '100', '100'])
        await wait_property(ipc, 'user-data/thumb_engine/cache-preview', lambda p: p and p.get('status') == 'outside-cache')
        assert CountedHandler.requests == initial_requests

        result.update(passed=True, cachedPreview=preview, cachedRanges=cache['seekable-ranges'],
                      uncachedTime=uncached_target, timelineHidesOutsideCache=True,
                      engineRejectsOutsideCache=True, extraNetworkRequests=0,
                      parentPositionAndPausePreserved=True, localExcerptRemoved=True,
                      requests=initial_requests, rawBytes=bgra.stat().st_size,
                      rawSha256=hashlib.sha256(bgra.read_bytes()).hexdigest(),
                      screenshots=[str(cached_shot), str(uncached_shot)])
    finally:
        if process.poll() is None:
            try: await ipc.command(['quit'])
            except (OSError, RuntimeError, TimeoutError): process.terminate()
            process.wait(timeout=8)
        server.shutdown()
        server.server_close()
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (args.output_dir.resolve() / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mpv', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.mpv = args.mpv.resolve()
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
