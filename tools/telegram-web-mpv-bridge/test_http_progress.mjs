import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
const bridgeSource = source.slice(source.indexOf('class BrowserMediaBridge {'), source.indexOf('const browserMediaBridge ='));
function fixture({ promiseOnly = false } = {}) {
    let now = 2000000, index = 0, connections = 0;
    const requests = [], messages = [], timers = new Map(), launches = [];
    const health = { ok: true, playbackProgressApiVersion: 1, playbackProgressProviders: ['bilibili', 'web'], playbackProgressHttpApiVersion: 1 };
    const f = { health, timers, requests, messages, launches, response: undefined };
    const context = vm.createContext({ crypto: webcrypto, Uint8Array, console, JSON,
        Date: class extends Date { static now() { return now; } },
        setTimeout: (callback, delay) => { const id = ++index; timers.set(id, { callback, delay }); return id; },
        clearTimeout: id => timers.delete(id), document: { addEventListener() {} },
        BROWSER_RELAY_WS_URL: 'ws://127.0.0.1:9001/ws', BROWSER_RELAY_BOOTSTRAP_COMMAND: 'start-relay',
        BROWSER_RELAY_BOOTSTRAP_TIMEOUT: 10000, BROWSER_RELAY_RETRY_INTERVAL: 500,
        BROWSER_RELAY_BOOTSTRAP_APP: 'powershell', TELEGRAM_RESPONSE_EVENT: 'telegram', WebSocket: { OPEN: 1 },
        createBridgeRequestId: () => `req-${++index}`, fetchPlaybackBridgeHealth: async () => health,
        updateHandoffStatus() {}, compress: command => command, window: { open: url => launches.push(url) },
        sleep: async () => { now += 500; },
        GM: { xmlHttpRequest: options => {
            requests.push(options);
            const request = JSON.parse(options.data).request;
            const response = f.response || { status: 200, responseText: JSON.stringify({ reply: {
                type: request.type === 'startPlaybackProgress' ? 'playbackProgressStarted' : 'playbackProgressState',
                requestId: request.requestId, identity: request.identity, sessionId: request.sessionId, apiVersion: 1 }, events: [] }) };
            if (promiseOnly) return Promise.resolve(response);
            options.onload(response); return { abort() {} };
        } }
    });
    vm.runInContext(bridgeSource + '\nglobalThis.Bridge = BrowserMediaBridge;', context);
    f.bridge = new context.Bridge();
    f.bridge.ensureConnected = async () => { connections++; throw new Error('Blocked socket'); };
    f.bridge.handleMessage = event => messages.push(JSON.parse(event.data));
    f.context = context;
    f.connections = () => connections;
    f.advance = ms => { now += ms; };
    return f;
}
const get = { type: 'getPlaybackProgress', provider: 'bilibili', identity: 'bilibili:11:21', pageUrl: 'https://www.bilibili.com/video/BVtest?p=1' };

test('blocked WebSocket falls back to anonymous JSON HTTP with a random capability and no page credentials', async () => {
    const f = fixture();
    await f.bridge.requestPlayback(get);
    await f.bridge.requestPlayback({ ...get, type: 'updatePlaybackProgress', position: 0 });
    assert.equal(f.connections(), 1);
    assert.equal(f.bridge.playbackTransport, 'http');
    assert.equal(f.bridge.isPlaybackConnected(), true);
    for (const options of f.requests) {
        assert.equal(options.anonymous, true); assert.equal(options.method, 'POST');
        assert.equal(options.url, 'http://127.0.0.1:9002/playback-progress');
        assert.deepEqual(JSON.parse(JSON.stringify(options.headers)), { 'Content-Type': 'application/json' });
        assert.match(JSON.parse(options.data).clientId, /^progress-http-[a-f0-9]{32}$/);
        assert.ok(!options.data.includes('Cookie')); assert.ok(!options.data.includes('csrf'));
    }
    assert.equal(JSON.parse(f.requests[0].data).request.rebind, true);
    assert.equal(f.timers.size, 0);
});

test('promise-only managers work and polling begins after the start acknowledgement', async () => {
    const f = fixture({ promiseOnly: true });
    const reply = await f.bridge.requestPlayback({ ...get, type: 'startPlaybackProgress', sessionId: 'progress-test' });
    assert.equal(reply.sessionId, 'progress-test');
    assert.equal(f.messages.length, 0);
    const poll = [...f.timers.values()].find(timer => timer.delay === 1000);
    assert.ok(poll);
    f.response = { status: 200, responseText: JSON.stringify({ reply: null, events: [
        { type: 'playbackProgress', sessionId: 'progress-test', identity: get.identity, position: 18, sequence: 1 }
    ] }) };
    await poll.callback();
    assert.equal(f.messages[0].position, 18);
    assert.equal(JSON.parse(f.requests.at(-1).data).request.type, 'pollPlaybackProgress');
    f.bridge.detachPlaybackHttp();
    assert.equal(f.bridge.httpPlaybackSessions.size, 0);
});

test('expired poll marks the channel disconnected and the next content read explicitly rebinds', async () => {
    const f = fixture(); await f.bridge.requestPlayback(get);
    f.response = { status: 410, responseText: '{}' };
    await assert.rejects(f.bridge.requestPlaybackHttp({ type: 'pollPlaybackProgress' }), error => error.code === 'playback_progress_client_expired');
    assert.equal(f.bridge.isPlaybackConnected(), false);
    f.response = undefined;
    await f.bridge.requestPlayback(get);
    assert.equal(JSON.parse(f.requests.at(-1).data).request.rebind, true);
    await f.bridge.requestPlayback({ ...get, identity: 'bilibili:11:22' });
    assert.equal(JSON.parse(f.requests.at(-1).data).request.rebind, true);
});

test('stop and page detach use the HTTP owner and never send to a failed socket', async () => {
    const f = fixture(); await f.bridge.requestPlayback({ ...get, type: 'startPlaybackProgress', sessionId: 'progress-test' });
    f.bridge.sendJson({ type: 'stopPlaybackProgress', sessionId: 'progress-test' });
    for (let i = 0; i < 12; i++) await Promise.resolve();
    assert.equal(JSON.parse(f.requests.at(-1).data).request.type, 'stopPlaybackProgress');
    assert.equal(f.bridge.httpPlaybackSessions.size, 0);
    f.bridge.detachPlaybackHttp();
    assert.equal(JSON.parse(f.requests.at(-1).data).request.type, 'detachPlaybackProgress');
});

test('HTTP timeout and malformed replies fail with fixed codes, without retaining request timers', async () => {
    const f = fixture(); await f.bridge.ensurePlaybackBridge(false);
    f.context.GM.xmlHttpRequest = () => ({ abort() {} });
    const pending = f.bridge.requestPlaybackHttp(get);
    [...f.timers.values()].find(timer => timer.delay === 5000).callback();
    await assert.rejects(pending, error => error.code === 'playback_request_timeout');
    assert.equal(f.timers.size, 0);
    f.context.GM.xmlHttpRequest = options => { options.onload({ status: 200, responseText: 'private-invalid-cookie' }); };
    await assert.rejects(f.bridge.requestPlaybackHttp(get), error => error.code === 'playback_http_unavailable' && !error.message.includes('cookie'));
    assert.equal(f.timers.size, 0);
});

test('playback bootstrap succeeds on HTTP health even when browser sockets are blocked', async () => {
    const f = fixture();
    await f.bridge.bootstrapLocalBridge('start-relay -PlaybackProgress');
    assert.equal(f.connections(), 0);
    assert.equal(f.launches.length, 1);
});

test('a delayed browser takeover completes before a newer player starts, and a failed request does not block the queue', async () => {
    const f = fixture();
    let release;
    const order = [];
    f.bridge.performPlaybackRequest = async message => {
        order.push(message.type);
        if (message.type === 'updatePlaybackProgress') await new Promise(resolve => { release = resolve; });
        if (message.type === 'fail') throw new Error('Expected failure');
        return message;
    };
    const save = f.bridge.requestPlayback({ type: 'updatePlaybackProgress', takeover: true });
    const start = f.bridge.requestPlayback({ type: 'startPlaybackProgress' });
    for (let i = 0; i < 6; i++) await Promise.resolve();
    assert.deepEqual(order, ['updatePlaybackProgress']);
    release(); await Promise.all([save, start]);
    assert.deepEqual(order, ['updatePlaybackProgress', 'startPlaybackProgress']);
    await assert.rejects(f.bridge.requestPlayback({ type: 'fail' }));
    assert.equal((await f.bridge.requestPlayback(get)).type, 'getPlaybackProgress');
});
