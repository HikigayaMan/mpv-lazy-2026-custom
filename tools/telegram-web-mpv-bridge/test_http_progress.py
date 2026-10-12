"""HTTP fallback tests, isolated from real player processes and account data."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from bridge import BridgeState, handle_http_client
from playback_progress import HttpProgressOwner, HttpProgressTransport, PlaybackProgressManager, PlaybackProgressSession, ProgressStore
from test_playback_progress import Ipc, Owner, eventually, request, snapshot

CLIENT = 'progress-http-' + 'a' * 32
OTHER = 'progress-http-' + 'b' * 32


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ProgressStore(Path(self.directory.name) / 'resume.json')
        self.ipc = Ipc()
        self.manager = PlaybackProgressManager(self.store, session_factory=lambda payload, owner, store:
            PlaybackProgressSession(payload, owner, store, ipc=self.ipc, poll_interval=.005, flush_interval=.01))
        self.transport = HttpProgressTransport(self.manager)

    async def asyncTearDown(self):
        await self.manager.close()
        self.directory.cleanup()

    async def action(self, kind, client=CLIENT, **updates):
        return await self.transport.request({'clientId': client, 'request': {'type': kind, **request(**updates)}})

    async def test_deferred_ack_then_poll_delivers_progress_and_coalesces_latest(self):
        result, owner, sid = await self.action('startPlaybackProgress')
        self.assertEqual(result['reply']['type'], 'playbackProgressStarted')
        self.assertEqual(result['events'], [])
        self.assertEqual(self.ipc.commands, [])
        self.manager.activate(sid, owner)
        await eventually(lambda: self.manager.sessions[sid].attached)
        self.ipc.value = snapshot(position=0, paused=True)
        await eventually(lambda: self.store.get(request()['identity'])['position'] == 0)
        result, _, _ = await self.action('pollPlaybackProgress')
        events = [event for event in result['events'] if event['type'] == 'playbackProgress']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['position'], 0)
        self.assertEqual((await self.action('pollPlaybackProgress'))[0]['events'], [])

    async def test_new_page_can_explicitly_rebind_http_owner_but_not_websocket_owner(self):
        _, owner, sid = await self.action('startPlaybackProgress')
        self.assertNotIn('activeSessionId', (await self.action('getPlaybackProgress', OTHER))[0]['reply'])
        result, new_owner, _ = await self.action('getPlaybackProgress', OTHER, rebind=True)
        self.assertEqual(result['reply']['activeSessionId'], sid)
        self.assertIs(self.manager.sessions[sid].owner, new_owner)
        events = (await self.action('pollPlaybackProgress'))[0]['events']
        self.assertEqual(events[-1]['reason'], 'browser_rebound')
        self.assertEqual(events[-1]['sessionId'], sid)
        websocket_owner = Owner()
        self.manager.sessions[sid].owner = websocket_owner
        result, _, _ = await self.action('getPlaybackProgress', rebind=True)
        self.assertNotIn('activeSessionId', result['reply'])
        self.assertIs(self.manager.sessions[sid].owner, websocket_owner)

    async def test_detach_keeps_mpv_saving_and_other_client_cannot_stop(self):
        _, owner, sid = await self.action('startPlaybackProgress')
        self.manager.activate(sid, owner)
        await eventually(lambda: self.manager.sessions[sid].attached)
        await self.action('getPlaybackProgress', OTHER)
        await self.action('stopPlaybackProgress', OTHER)
        self.assertIn(sid, self.manager.sessions)
        await self.action('detachPlaybackProgress')
        self.ipc.value = snapshot(position=9)
        await eventually(lambda: self.store.get(request()['identity'])['position'] == 9)
        self.assertIsNone(self.manager.sessions[sid].owner)
        with self.assertRaises(LookupError):
            await self.action('pollPlaybackProgress')

    async def test_expired_clients_are_detached_and_bounded(self):
        await self.action('getPlaybackProgress')
        self.transport.clients[CLIENT].last_seen = time.monotonic() - 121
        with self.assertRaises(LookupError):
            await self.action('pollPlaybackProgress')
        self.assertEqual(self.transport.clients, {})
        for number in range(32):
            await self.action('getPlaybackProgress', 'progress-http-' + f'{number:032x}')
        with self.assertRaises(ValueError):
            await self.action('getPlaybackProgress', OTHER)

    async def test_http_rejects_unrelated_relay_actions_and_weak_client_ids(self):
        for envelope in ({'clientId': 'short', 'request': {'type': 'getPlaybackProgress'}},
                         {'clientId': CLIENT, 'request': {'type': 'startDanmaku'}},
                         {'clientId': CLIENT, 'request': {'type': 'registerSource'}}):
            with self.assertRaises(ValueError):
                await self.transport.request(envelope)
        self.assertEqual(self.transport.clients, {})

    async def test_real_http_endpoint_has_no_cors_and_rejects_non_json_requests(self):
        state = BridgeState(1, 1024, 'mpv.exe', '127.0.0.1', 0, progress_store=self.store)
        state.progress = self.manager
        state.progress_http = self.transport
        server = await asyncio.start_server(lambda reader, writer: handle_http_client(reader, writer, state), '127.0.0.1', 0)
        try:
            async def http(method, data, content_type='application/json'):
                reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
                body = json.dumps(data).encode()
                writer.write(f'{method} /playback-progress HTTP/1.1\r\nHost: localhost\r\nOrigin: https://www.bilibili.com\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
                await writer.drain()
                raw = await reader.read()
                writer.close()
                await writer.wait_closed()
                return raw.partition(b'\r\n\r\n')[::2]
            envelope = {'clientId': CLIENT, 'request': {'type': 'updatePlaybackProgress', **request(position=0)}}
            headers, body = await http('POST', envelope)
            self.assertIn(b'200 OK', headers)
            self.assertEqual(json.loads(body)['reply']['record']['position'], 0)
            for method, content_type, code in [('GET', 'application/json', b'405'), ('OPTIONS', 'application/json', b'405'), ('POST', 'text/plain', b'400')]:
                headers, _ = await http(method, envelope, content_type)
                self.assertIn(code, headers)
                self.assertNotIn(b'Access-Control-Allow-Origin', headers)
            headers, _ = await http('POST', {'clientId': OTHER, 'request': {'type': 'pollPlaybackProgress'}})
            self.assertIn(b'410 Gone', headers)
            self.assertNotIn('progress-http-', self.store.path.read_text())
        finally:
            server.close()
            await server.wait_closed()

    async def test_aborted_http_ack_still_activates_registered_session(self):
        state = BridgeState(1, 1024, 'mpv.exe', '127.0.0.1', 0, progress_store=self.store)
        state.progress = self.manager
        state.progress_http = self.transport
        body = json.dumps({'clientId': CLIENT, 'request': {'type': 'startPlaybackProgress', **request()}}).encode()
        reader = asyncio.StreamReader()
        reader.feed_data(f'POST /playback-progress HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body)
        reader.feed_eof()
        class AbortedWriter:
            def write(self, raw):
                pass
            async def drain(self):
                raise ConnectionResetError('Browser aborted the acknowledgement')
            def close(self):
                pass
            async def wait_closed(self):
                pass
        await handle_http_client(reader, AbortedWriter(), state)
        await eventually(lambda: self.manager.sessions['progress-fixture'].attached)
        self.assertIsNotNone(self.manager.sessions['progress-fixture'].task)


if __name__ == '__main__':
    unittest.main()
