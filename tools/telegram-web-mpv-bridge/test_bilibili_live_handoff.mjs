import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
function between(start, end) {
    const first = source.indexOf(start);
    const last = source.indexOf(end, first + start.length);
    assert.ok(first >= 0 && last > first, `Missing userscript section ${start}`);
    return source.slice(first, last);
}

function sandbox(overrides = {}) {
    const context = vm.createContext({
        TextEncoder, AbortController, URL, console,
        setTimeout, clearTimeout,
        document: { cookie: 'SESSDATA=must-stay-in-browser; buvid3=device-test', addEventListener() {} },
        navigator: { userAgent: 'Mozilla/5.0 BilibiliBrowserTest/1.0' },
        WebSocket: { OPEN: 1 },
        createBridgeRequestId: () => 'request-1',
        DOUYU_DANMAKU_PIPE_PREFIX: 'mpv-lazy-douyu-',
        DOUYU_DANMAKU_TOKEN_MAX_LENGTH: 64,
        BILIBILI_DANMAKU_PIPE_PREFIX: 'mpv-lazy-bilibili-',
        BROWSER_RELAY_WS_URL: 'ws://127.0.0.1:9000/ws',
        BROWSER_RELAY_BOOTSTRAP_COMMAND: '-File "bridge.ps1"',
        TELEGRAM_RESPONSE_EVENT: 'test-response',
        ...overrides
    });
    vm.runInContext([
        between('const DOUYU_MD5_ROTATIONS =', 'function buildDouyuAuth('),
        between('// DOUYU_DANMAKU_HELPERS_START', '// DOUYU_DANMAKU_HELPERS_END'),
        between('// BILIBILI_LIVE_DANMAKU_HELPERS_START', '// BILIBILI_LIVE_DANMAKU_HELPERS_END'),
        between('// AUTO_HANDOFF_HELPERS_START', '// AUTO_HANDOFF_HELPERS_END'),
        between('class BrowserMediaBridge {', 'const browserMediaBridge ='),
        'globalThis.BrowserMediaBridge = BrowserMediaBridge;'
    ].join('\n'), context);
    return context;
}

const keys = ['7cd084941338484aae1ad9425b84077c', '4932caff0ff746eab6f01bf08b70ac45'];
const auth = { uid: 42, token: 'ephemeral-token', buvid: 'device-test',
    hostList: [{ host: 'broadcastlv.chat.bilibili.com', wss_port: 443 }],
    userAgent: 'Mozilla/5.0 BilibiliBrowserTest/1.0' };
const resolved = { roomId: '48743', auth };
const asPlain = value => JSON.parse(JSON.stringify(value));

test('WBI signature matches the published fixed timestamp example and strips reserved characters', () => {
    const context = sandbox();
    const query = context.buildBilibiliWbiQuery({ foo: '114', bar: '514', zab: 1919810 }, ...keys, 1702204169);
    assert.equal(query, 'bar=514&foo=114&wts=1702204169&zab=1919810&w_rid=8f6f2b5b3d485fe1886cec6a0be8c5d4');
    const encoded = context.buildBilibiliWbiQuery({ text: "中文 !'()*" }, ...keys, 1702204169);
    assert.match(encoded, /^text=%E4%B8%AD%E6%96%87%20&wts=1702204169&w_rid=/);
    const input = 'text=%E4%B8%AD%E6%96%87%20&wts=1702204169ea1db124af3c7062474693fa704f4ff8';
    assert.ok(encoded.endsWith(createHash('md5').update(input).digest('hex')));
});

test('auth resolves short room IDs, works for guests and never forwards login cookies', async () => {
    const calls = [];
    const context = sandbox({ fetch: async (url, options) => {
        calls.push({ url, options });
        let payload;
        if (url.includes('/room_init?')) payload = { code: 0, data: { room_id: 48743 } };
        else if (url.includes('/nav')) payload = { code: -101, data: { isLogin: false,
            wbi_img: { img_url: `https://i0.hdslb.com/bfs/wbi/${keys[0]}.png`,
                sub_url: `https://i0.hdslb.com/bfs/wbi/${keys[1]}.png` } } };
        else payload = { code: 0, data: { token: 'guest-token', host_list: [
            { host: 'broadcastlv.chat.bilibili.com', wss_port: 443, ws_port: 2244 },
            { host: '127.0.0.1', wss_port: 443 },
            { host: 'attacker.example', wss_port: 443 }
        ] } };
        return { ok: true, json: async () => payload };
    } });
    const result = asPlain(await context.resolveBilibiliLiveDanmakuAuth('255'));
    assert.equal(result.roomId, '48743');
    assert.equal(result.auth.uid, 0);
    assert.equal(result.auth.buvid, 'device-test');
    assert.equal(result.auth.userAgent, 'Mozilla/5.0 BilibiliBrowserTest/1.0');
    assert.deepEqual(result.auth.hostList, [{ host: 'broadcastlv.chat.bilibili.com', wss_port: 443 }]);
    assert.ok(calls.every(call => call.options.credentials === 'include' && !call.options.headers));
    assert.match(calls.find(call => call.url.includes('getDanmuInfo')).url, /id=48743&type=0&wts=\d+&w_rid=[a-f0-9]{32}$/);
    assert.ok(!JSON.stringify(result).includes('SESSDATA'));
});

test('successful Bilibili handoff adds a unique mpv pipe only after the correct provider acknowledgement', async () => {
    const context = sandbox();
    const starts = [];
    const bridge = { startDanmaku: async request => {
        starts.push(request);
        return { type: 'danmakuStarted', provider: 'bilibili', apiVersion: 2, sessionId: request.sessionId };
    }, stopDanmaku() { assert.fail('successful session was stopped'); } };
    const media = { video: 'https://stream.example/live.flv', mpvOptions: ['--no-ytdl'] };
    const first = await context.prepareBilibiliLiveDanmakuLaunch(media, 'bilibiliLive', { name: 'MPV' }, '255', bridge, async () => resolved);
    const second = await context.prepareBilibiliLiveDanmakuLaunch(media, 'bilibiliLive', { name: 'MPV' }, '255', bridge, async () => resolved);
    assert.equal(first.started, true);
    assert.equal(starts[0].provider, 'bilibili');
    assert.equal(starts[0].roomId, '48743');
    assert.notEqual(first.sessionId, second.sessionId);
    assert.notEqual(first.pipeName, second.pipeName);
    assert.match(first.media.mpvOptions[1], /^--input-ipc-server=\\\\\.\\pipe\\mpv-lazy-bilibili-[a-z0-9-]+$/);
    assert.deepEqual(media.mpvOptions, ['--no-ytdl']);
});

test('bridge and auth failures preserve playable media and stop a rejected session', async () => {
    const context = sandbox();
    const media = { video: 'https://stream.example/live.flv', mpvOptions: ['--no-ytdl'] };
    const stops = [];
    const bridge = { startDanmaku: async request => ({ type: 'danmakuStarted', provider: 'douyu', sessionId: request.sessionId }),
        stopDanmaku: id => stops.push(id) };
    const rejected = await context.prepareBilibiliLiveDanmakuLaunch(media, 'bilibiliLive', { name: 'MPV' }, '255', bridge, async () => resolved);
    assert.equal(rejected.started, false);
    assert.match(rejected.error.message, /invalid acknowledgement/);
    assert.deepEqual(asPlain(rejected.media), media);
    assert.equal(stops.length, 1);
    const unavailable = await context.prepareBilibiliLiveDanmakuLaunch(media, 'bilibiliLive', { name: 'MPV' }, '255', bridge,
        async () => { throw new Error('API unavailable'); });
    assert.equal(unavailable.started, false);
    assert.deepEqual(asPlain(unavailable.media), media);
});

test('VOD and non-mpv players do not request live danmaku or browser credentials', async () => {
    const context = sandbox();
    const resolver = () => assert.fail('unexpected live authentication request');
    for (const [parser, player] of [['bilibili', 'MPV'], ['bilibiliLive', 'VLC'], ['resource', 'MPV']]) {
        const result = await context.prepareBilibiliLiveDanmakuLaunch({ video: 'test' }, parser, { name: player }, '255', {}, resolver);
        assert.equal(result.started, false);
    }
});

test('bridge rejects stale acknowledgements and keeps Douyu requests compatible', async () => {
    const context = sandbox();
    const bridge = new context.BrowserMediaBridge();
    const sent = [];
    bridge.ensureLocalBridge = async () => {};
    bridge.socket = { readyState: 1, send: message => sent.push(JSON.parse(message)) };
    const pending = bridge.startDanmaku({ provider: 'bilibili', roomId: resolved.roomId,
        sessionId: 'bilibili-test-1', pipeName: 'mpv-lazy-bilibili-test-1', auth: { ...auth, cookie: 'secret' } });
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(!JSON.stringify(sent[0]).includes('secret'));
    assert.equal(sent[0].auth.userAgent, auth.userAgent);
    bridge.handleMessage({ data: JSON.stringify({ type: 'danmakuStarted', provider: 'bilibili',
        apiVersion: 2, requestId: sent[0].requestId, sessionId: 'bilibili-other-1' }) });
    assert.equal(bridge.pendingDanmakuStarts.size, 1);
    bridge.handleMessage({ data: JSON.stringify({ type: 'danmakuStarted', provider: 'bilibili',
        apiVersion: 2, requestId: sent[0].requestId, sessionId: sent[0].sessionId }) });
    assert.equal((await pending).sessionId, 'bilibili-test-1');
    assert.equal(bridge.activeDanmakuSessions.get('bilibili-test-1').roomId, '48743');
    const douyu = bridge.startDanmaku({ roomId: '123', sessionId: 'douyu-test-1', pipeName: 'mpv-lazy-douyu-test-1' });
    await new Promise(resolve => setImmediate(resolve));
    const request = sent.at(-1);
    assert.ok(!Object.hasOwn(request, 'provider'));
    bridge.handleMessage({ data: JSON.stringify({ type: 'danmakuStarted', requestId: request.requestId, sessionId: request.sessionId }) });
    assert.equal((await douyu).sessionId, 'douyu-test-1');
    bridge.stopDanmaku('bilibili-test-1');
    assert.equal(bridge.activeDanmakuSessions.size, 0);
});

test('credential refresh is restricted to the current provider, room and session', async () => {
    const context = sandbox();
    let refreshes = 0;
    context.resolveBilibiliLiveDanmakuAuth = async roomId => {
        refreshes++;
        assert.equal(roomId, '48743');
        return resolved;
    };
    const bridge = new context.BrowserMediaBridge();
    const messages = [];
    bridge.sendJson = message => messages.push(asPlain(message));
    bridge.activeDanmakuSessions.set('bilibili-test-1', { roomId: '48743' });
    const request = { type: 'refreshDanmakuAuth', provider: 'bilibili', sessionId: 'bilibili-test-1',
        roomId: '48743', requestId: 'refresh-1' };
    await bridge.refreshDanmakuAuth({ ...request, provider: 'douyu' });
    await bridge.refreshDanmakuAuth({ ...request, roomId: '99' });
    await bridge.refreshDanmakuAuth({ ...request, sessionId: 'bilibili-old-1' });
    assert.equal(refreshes, 0);
    await bridge.refreshDanmakuAuth(request);
    assert.equal(refreshes, 1);
    assert.deepEqual(messages, [{ type: 'updateDanmakuAuth', provider: 'bilibili',
        sessionId: request.sessionId, requestId: request.requestId, auth }]);
    let release;
    context.resolveBilibiliLiveDanmakuAuth = () => new Promise(resolve => { release = resolve; });
    const delayed = bridge.refreshDanmakuAuth(request);
    bridge.activeDanmakuSessions.delete(request.sessionId);
    release(resolved);
    await delayed;
    assert.equal(messages.length, 1);
});

test('bridge timeout cancels a pending session and a closed socket leaves no pending timers', async () => {
    let timeoutCallback;
    let timeoutMs;
    let cleared = 0;
    const context = sandbox({
        setTimeout: (callback, ms) => { timeoutCallback = callback; timeoutMs = ms; return 1; },
        clearTimeout: () => { cleared++; }
    });
    const bridge = new context.BrowserMediaBridge();
    const sent = [];
    bridge.ensureLocalBridge = async () => {};
    bridge.socket = { readyState: 1, send: message => sent.push(JSON.parse(message)) };
    const details = { provider: 'bilibili', roomId: '48743', sessionId: 'bilibili-timeout-1',
        pipeName: 'mpv-lazy-bilibili-timeout-1', auth };
    const waiting = bridge.startDanmaku(details);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(timeoutMs, 10000);
    timeoutCallback();
    await assert.rejects(waiting, /timed out/);
    assert.equal(bridge.pendingDanmakuStarts.size, 0);
    assert.equal(sent.at(-1).type, 'stopDanmaku');
    bridge.socket.send = () => { throw new Error('socket closed'); };
    await assert.rejects(bridge.startDanmaku(details), /socket closed/);
    assert.equal(bridge.pendingDanmakuStarts.size, 0);
    assert.ok(cleared > 0);
});

test('Bilibili uses an isolated bridge instance and leaves existing Telegram/Douyu configuration intact', () => {
    const context = sandbox();
    const existing = new context.BrowserMediaBridge();
    const bilibili = new context.BrowserMediaBridge({ wsUrl: 'ws://127.0.0.1:9001/ws',
        bootstrapCommand: '-File "bridge.ps1" -Bilibili' });
    assert.equal(existing.wsUrl, 'ws://127.0.0.1:9000/ws');
    assert.equal(existing.bootstrapCommand, '-File "bridge.ps1"');
    assert.equal(bilibili.wsUrl, 'ws://127.0.0.1:9001/ws');
    assert.equal(bilibili.bootstrapCommand, '-File "bridge.ps1" -Bilibili');
    assert.notEqual(existing.pendingDanmakuStarts, bilibili.pendingDanmakuStarts);
    assert.notEqual(existing.activeDanmakuSessions, bilibili.activeDanmakuSessions);
    const commonHandoff = between('class BaseParser {', 'function isRelayPlayer(');
    assert.match(commonHandoff, /bilibiliLiveRoomId, bilibiliMediaBridge/);
});

function iframeHandoffSandbox() {
    const messageListeners = [];
    const documentListeners = new Map();
    const frames = [];
    const videoListeners = new Map();
    const video = { tagName: 'VIDEO', isConnected: true, paused: false, pauseCalls: 0,
        pause() { this.paused = true; this.pauseCalls++; },
        addEventListener(type, listener) { videoListeners.set(type, listener); },
        removeEventListener(type) { videoListeners.delete(type); },
        contains(target) { return target === this; }, closest() { return this; } };
    const launches = [];
    const statuses = [];
    const toasts = [];
    const authRooms = [];
    const starts = [];
    const context = sandbox({
        PROJECT_NAME: 'external-player', REFRESH_INTERVAL: 1, MAX_TRY_COUNT: 1,
        currentUrl: 'https://live.bilibili.com/719085',
        location: { href: 'https://live.bilibili.com/719085', origin: 'https://live.bilibili.com' },
        currentMedia: undefined, currentPlayer: undefined, currentTryCount: 0,
        topListenersAttached: false, isReloading: false, iframe: undefined,
        translation: { handoffDanmakuUnavailable: 'Danmaku unavailable; playing video only', loadFail: 'Load failed' },
        document: { title: 'Bilibili live room', documentElement: {},
            addEventListener(type, listener) { documentListeners.set(type, listener); },
            removeEventListener(type) { documentListeners.delete(type); },
            getElementsByTagName: tag => tag === 'iframe' ? frames : tag === 'video' ? [video] : [],
            querySelectorAll: selector => selector.includes('iframe') ? frames : selector.includes('video') ? [video] : [],
            querySelector: selector => selector.includes('video') ? video : null,
            contains(target) { return target === video || frames.includes(target); } },
        window: { addEventListener: (type, listener) => {
            if (type === 'message') messageListeners.push(listener);
        }, removeEventListener() {}, open: url => launches.push(url) },
        setInterval: () => 1, clearInterval() {},
        MutationObserver: class { observe() {} disconnect() {} },
        appendAll: async () => {}, showButtonDiv() {}, updateHandoffForCurrentParser() {},
        isResourceFallbackParser: () => false, resourceFallbackCanShow: () => false,
        isTelegramUrl: () => false, shouldUseBrowserRelay: () => false,
        showLoading() {}, hideLoading() {}, sleep: async () => {},
        updateHandoffStatus: (...args) => statuses.push(args), showToast: text => toasts.push(text),
        policy: { createScript: text => text }, compress: text => text,
        logLaunchArgs() {},
        prepareBilibiliLiveStreamLaunch: async media => media,
        bilibiliMediaBridge: { startDanmaku: async request => {
            starts.push(asPlain(request));
            return { type: 'danmakuStarted', provider: 'bilibili', apiVersion: 2, sessionId: request.sessionId };
        }, stopDanmaku() {} }
    });
    context.window.self = context.window.top = context.window.parent = context.window;
    context.window.location = context.location;
    context.self = context.top = context.parent = context.window;
    video.ownerDocument = context.document;
    vm.runInContext([
        between('const defaultConfig = {', 'if (DEBUG === true)'),
        between('// WEB_PLAYBACK_PAUSE_HELPERS_START', '// WEB_PLAYBACK_PAUSE_HELPERS_END'),
        between('class BaseParser {', 'function isRelayPlayer('),
        'const PARSER = { ' + between('IFRAME: class Parser extends BaseParser {', 'function compress('),
        between('async function initTop() {', 'function initIframe()'),
        'globalThis.BaseParser = BaseParser; globalThis.defaultConfig = defaultConfig;'
    ].join('\n'), context);
    context.resolveBilibiliLiveDanmakuAuth = async roomId => {
        authRooms.push(roomId);
        return resolved;
    };
    context.currentConfig = asPlain(context.defaultConfig);
    context.currentParser = new context.BaseParser();
    context.currentParser.parserKey = 'bilibiliLive';
    const childMedia = asPlain(context.currentMedia);
    childMedia.video = 'https://fixture.bilivideo.com/live.flv';
    childMedia.mpvOptions = ['--no-ytdl'];
    const emitMessage = async event => {
        for (const listener of messageListeners) await listener(event);
    };
    const child = { postMessage: message => {
        if (message.method === 'execute') void emitMessage({ source: child,
            data: { name: 'external-player', method: 'currentMedia', currentMedia: childMedia }
        });
    } };
    frames.push({ tagName: 'IFRAME', isConnected: true, contentWindow: child });
    return { context, child, emitMessage, launches, statuses, toasts, authRooms, starts, video,
        documentListeners, videoListeners };
}

test('the real iframe init/currentMedia/play route attaches live danmaku using the outer Bilibili room', async () => {
    const fixture = iframeHandoffSandbox();
    const { context, child, emitMessage, starts, authRooms, launches, video } = fixture;
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init', isReloading: false } });
    // This is the actual Bilibili page behavior: IFRAME replaces BILIBILI_LIVE and has no parserKey.
    assert.equal(context.currentParser.parserKey, undefined);
    const player = context.currentConfig.players.find(player => player.name === 'MPV');
    await context.currentParser.play(player);
    assert.deepEqual(authRooms, ['719085']);
    assert.equal(starts.length, 1);
    assert.equal(starts[0].provider, 'bilibili');
    assert.equal(starts[0].roomId, '48743');
    assert.equal(launches.length, 1);
    assert.match(launches[0], /fixture\.bilivideo\.com\/live\.flv/);
    assert.match(launches[0], /--input-ipc-server=\\\\\.\\pipe\\mpv-lazy-bilibili-/);
    assert.match(launches[0], /--fullscreen=yes/);
    assert.equal(video.paused, true, 'the outer live page also pauses when its parser was replaced by IFRAME');
    assert.ok(video.pauseCalls > 0);
});

test('pauseAuto disabled leaves the original live page playing after a successful external launch', async () => {
    const { context, child, emitMessage, launches, video } = iframeHandoffSandbox();
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init' } });
    const player = context.currentConfig.players.find(player => player.name === 'MPV');
    player.presetEvent.pauseAuto = false;
    await context.currentParser.play(player);
    assert.equal(launches.length, 1);
    assert.equal(video.paused, false);
    assert.equal(video.pauseCalls, 0);
});

test('a failed protocol dispatch leaves the original live page playing', async () => {
    const { context, child, emitMessage, video, statuses } = iframeHandoffSandbox();
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init' } });
    context.window.open = () => { throw new Error('launch rejected'); };
    await context.currentParser.play(context.currentConfig.players.find(player => player.name === 'MPV'));
    assert.equal(statuses.at(-1)[0], 'failed');
    assert.equal(video.paused, false);
    assert.equal(video.pauseCalls, 0);
});

test('ordinary web handoff opens MPV and MPVNET fullscreen without changing other player options', async () => {
    for (const name of ['MPV', 'MPVNET', 'PotPlayer']) {
        const { context, launches } = iframeHandoffSandbox();
        context.currentUrl = 'https://www.bilibili.com/video/BVtest/';
        context.currentMedia.video = 'https://fixture.bilivideo.com/video.mp4';
        context.currentMedia.mpvOptions = ['--fs=no', '--fullscreen=no', '--no-ytdl'];
        const initialOptions = context.currentMedia.mpvOptions;
        context.currentParser.check = async () => true;
        const player = context.currentConfig.players.find(player => player.name === name);
        await context.currentParser.play(player);
        assert.equal(launches.length, 1);
        if (name === 'PotPlayer') {
            assert.ok(!launches[0].includes('--fullscreen'));
        } else {
            assert.equal(launches[0].split('--fullscreen=yes').length - 1, 1);
            assert.ok(!launches[0].includes('--fs=no'));
            assert.ok(!launches[0].includes('--fullscreen=no'));
            assert.match(launches[0], /--no-ytdl/);
        }
        assert.deepEqual(initialOptions, ['--fs=no', '--fullscreen=no', '--no-ytdl']);
    }
});

test('CSP fallback retains fullscreen alongside VOD cid and media options', async () => {
    const { context, launches } = iframeHandoffSandbox();
    context.currentUrl = 'https://www.bilibili.com/video/BVtest/';
    context.currentMedia.video = 'https://fixture.bilivideo.com/video.mp4';
    context.currentMedia.mpvOptions = ['--no-ytdl'];
    context.currentMedia.bilibili.cid = '123456';
    context.currentParser.check = async () => true;
    context.policy.createScript = () => { throw new Error('unsafe-eval blocked by CSP'); };
    await context.currentParser.play(context.currentConfig.players.find(player => player.name === 'MPV'));
    assert.equal(launches.length, 1);
    assert.match(launches[0], /--fullscreen=yes/);
    assert.match(launches[0], /--script-opts-append="cid=123456"/);
    assert.match(launches[0], /--no-ytdl/);
});

test('iframe handoff preserves video and the failure reason if live authentication fails', async () => {
    const { context, child, emitMessage, starts, launches, statuses, toasts } = iframeHandoffSandbox();
    context.resolveBilibiliLiveDanmakuAuth = async () => { throw new Error('API code -352'); };
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init' } });
    await context.currentParser.play(context.currentConfig.players.find(player => player.name === 'MPV'));
    assert.equal(starts.length, 0);
    assert.equal(launches.length, 1);
    assert.ok(!launches[0].includes('--input-ipc-server'));
    assert.match(toasts[0], /API code -352/);
    assert.equal(statuses.at(-1)[0], 'danmaku-unavailable');
});

test('SPA navigation during iframe parsing cancels the stale protocol launch', async () => {
    const { context, child, emitMessage, starts, launches } = iframeHandoffSandbox();
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init' } });
    context.currentParser.execute = async () => {
        context.currentMedia.video = 'https://fixture.bilivideo.com/old-room.flv';
        context.location.href = 'https://live.bilibili.com/999999';
    };
    await context.currentParser.play(context.currentConfig.players.find(player => player.name === 'MPV'));
    assert.equal(starts.length, 0);
    assert.equal(launches.length, 0);
});

test('switching Bilibili list content on the same URL during parsing cancels the stale media', async () => {
    const { context, child, emitMessage, launches } = iframeHandoffSandbox();
    context.location.href = context.currentUrl = 'https://www.bilibili.com/list/123';
    context.window.__INITIAL_STATE__ = { videoData: { aid: 11, cid: 22 } };
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init' } });
    context.currentParser.execute = async () => {
        context.currentMedia.video = 'https://fixture.bilivideo.com/old.flv';
        context.window.__INITIAL_STATE__.videoData = { aid: 33, cid: 44 };
    };
    await context.currentParser.play(context.currentConfig.players.find(player => player.name === 'MPV'));
    assert.equal(launches.length, 0);
});

test('the complete iframe MPV handoff replaces a captured low-quality stream and carries variants plus live danmaku', async () => {
    const { context, child, emitMessage, starts, launches } = iframeHandoffSandbox();
    vm.runInContext(between('// BILIBILI_STREAM_QUALITY_HELPERS_START', '// BILIBILI_STREAM_QUALITY_HELPERS_END'), context);
    const highest = { url: 'https://fixture.bilivideo.com/source-1440.flv', quality: 10000, title: '1440P 原画', height: 1440 };
    const lower = { url: 'https://fixture.bilivideo.com/captured-720.flv', quality: 250, title: '720P 超清', height: 720 };
    context.resolveBilibiliLiveStreams = async () => ({ variants: [highest, lower], selected: highest });
    await context.initTop();
    await emitMessage({ source: child, data: { name: 'external-player', method: 'init' } });
    await context.currentParser.play(context.currentConfig.players.find(player => player.name === 'MPV'));
    assert.equal(launches.length, 1);
    assert.match(launches[0], /source-1440\.flv/);
    assert.match(launches[0], /quality-menu-stream_variants=/);
    assert.match(launches[0], /--input-ipc-server=\\\\\.\\pipe\\mpv-lazy-bilibili-/);
    assert.equal(starts.length, 1);
});

test('room detection supports outer and embedded live pages and rejects unrelated/video pages', () => {
    const context = sandbox();
    for (const url of ['https://live.bilibili.com/719085',
        'https://live.bilibili.com/blanc/719085?liteVersion=true', 'https://live.bilibili.com/roomid=719085']) {
        assert.equal(context.getBilibiliLiveRoomId(url), '719085');
    }
    for (const url of ['https://www.bilibili.com/video/BV123', 'https://attacker.example/live.bilibili.com/719085',
        'https://live.bilibili.com.attacker.example/719085', 'https://live.bilibili.com/0']) {
        assert.equal(context.getBilibiliLiveRoomId(url), undefined);
    }
});
