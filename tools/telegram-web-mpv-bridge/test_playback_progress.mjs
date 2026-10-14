import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
function between(start, end) {
    const first = source.indexOf(start), last = source.indexOf(end, first + start.length);
    assert.ok(first >= 0 && last > first, `Missing section ${start}`);
    return source.slice(first, last);
}
const plain = value => JSON.parse(JSON.stringify(value));
function fixture({ url = 'https://www.bilibili.com/video/BVtest?p=1', position = 0, paused = true, duration = 300 } = {}) {
    let now = 2_000_000, time = position;
    const storage = {}, requests = [], posts = [], warnings = [], consoleWarnings = [], documentListeners = new Map();
    const videoListeners = new Map();
    const video = { duration, paused, ended: false,
        get currentTime() { return time; },
        set currentTime(value) { time = value; videoListeners.get('seeked')?.(); },
        getBoundingClientRect: () => ({ width: 1280, height: 720 }),
        closest: () => undefined,
        addEventListener: (event, handler) => videoListeners.set(event, handler),
        removeEventListener: event => videoListeners.delete(event) };
    const bridge = { ensurePlaybackBridge: async () => {},
        requestPlayback: async message => {
            requests.push(plain(message));
            if (message.type === 'startPlaybackProgress') return { type: 'playbackProgressStarted',
                ...message, apiVersion: 1, record: { ...message, source: 'mpv' } };
            return { type: 'playbackProgressState', identity: message.identity, record: bridge.record,
                activeSessionId: bridge.activeSessionId };
        }, sendJson: message => requests.push(plain(message)) };
    const top = {};
    const context = vm.createContext({ console: { ...console, warn: value => consoleWarnings.push(value) }, URL, URLSearchParams, TextEncoder, AbortController, AbortSignal,
        Date: class extends Date { static now() { return now; } }, setTimeout, clearTimeout,
        self: top, top, location: { href: url }, currentParser: { parserKey: 'bilibili' },
        currentConfig: { global: { playbackProgressSync: true, bilibiliHistorySync: true } },
        translation: { handoffHistoryFailed: 'cloud failed', handoffProgressUnavailable: 'bridge failed', handoffProgressUpdateRequired: 'update required' },
        navigator: { userAgent: 'ProgressTest' }, window: { addEventListener() {} },
        document: { cookie: 'bili_jct=browser-only-csrf; SESSDATA=browser-only-session',
            querySelectorAll: () => [video],
            addEventListener: (event, handler) => documentListeners.set(event, handler) },
        GM_getValue: (key, fallback) => structuredClone(storage[key] ?? fallback),
        GM_setValue: (key, value) => { storage[key] = plain(value); },
        bilibiliMediaBridge: bridge, BILIBILI_DANMAKU_PIPE_PREFIX: 'mpv-lazy-bilibili-',
        DOUYU_DANMAKU_TOKEN_MAX_LENGTH: 64,
        getAutoHandoffPageKey: url => { const u = new URL(url); return `${u.origin}${u.pathname}?p=${u.searchParams.get('p') || 1}`; },
        getBilibiliLiveRoomId: url => /^https:\/\/live\.bilibili\.com\/(\d+)/.exec(url)?.[1],
        isRelayPlayer: player => /^(MPV|MPVNET)$/i.test(player?.name || ''),
        createBridgeRequestId: () => 'request-1', showBrowserRelayToast: warning => warnings.push(warning),
        showToast: warning => warnings.push(warning),
        fetch: async (url, options) => {
            if (options?.method === 'POST') { posts.push({ url, options }); return { ok: true, json: async () => ({ code: 0 }) }; }
            if (url.includes('/nav')) return { ok: true, json: async () => ({ code: 0, data: {
                isLogin: true, mid: 7, wbi_img: { img_url: 'https://i.test/7cd084941338484aae1ad9425b84077c.png',
                    sub_url: 'https://i.test/4932caff0ff746eab6f01bf08b70ac45.png' } } }) };
            if (url.includes('/history/cursor')) return { ok: true, json: async () => ({ code: 0, data: { list: [] } }) };
            if (url.includes('/web-interface/view')) return { ok: true, json: async () => ({ code: 0, data: {
                aid: 11, bvid: 'BVtest', cid: 21, pages: [{ cid: 21, duration: 300 }, { cid: 22, duration: 200 }] } }) };
            throw new Error(`Unexpected request ${url}`);
        }
    });
    vm.runInContext([
        between('const DOUYU_MD5_ROTATIONS =', 'function buildDouyuAuth('),
        between('// BILIBILI_LIVE_DANMAKU_HELPERS_START', '// BILIBILI_LIVE_DANMAKU_HELPERS_END'),
        between('// BILIBILI_REQUEST_CACHE_HELPERS_START', '// BILIBILI_REQUEST_CACHE_HELPERS_END'),
        between('// PLAYBACK_PROGRESS_HELPERS_START', '// PLAYBACK_PROGRESS_HELPERS_END'),
        'globalThis.sync = playbackProgressSync; globalThis.Reporter = BilibiliProgressReporter;'
    ].join('\n'), context);
    return { context, sync: context.sync, storage, requests, posts, warnings, consoleWarnings, bridge, video,
        documentListeners, videoListeners, advance: ms => { now += ms; } };
}
const content = { provider: 'bilibili', identity: 'bilibili:11:21', pageUrl: 'https://www.bilibili.com/video/BVtest?p=1', aid: 11, cid: 21, bvid: 'BVtest', duration: 300 };
const record = (position, updatedAt = 1_000_000, extra = {}) => ({ position, duration: 300, updatedAt, paused: false, ended: false, source: 'mpv', ...extra });
const media = (position = 0) => ({ video: 'https://cdn.test/video.m4s', time: position,
    bilibili: { aid: 11, cid: 21, bvid: 'BVtest', duration: 300 }, mpvOptions: ['--no-ytdl'] });
const drain = async () => { await new Promise(resolve => setImmediate(resolve)); };

test('latest timestamp preserves backwards and zero-second seeks and rejects malformed records', () => {
    const { context } = fixture();
    assert.equal(context.newestPlaybackRecord(record(100, 10), record(0, 20)).position, 0);
    assert.equal(context.newestPlaybackRecord(record(-1, 100)), undefined);
    // Valid older values survive invalid newer input.
    assert.equal(context.newestPlaybackRecord(record(100, 10), { position: -1, updatedAt: 100 }).position, 100);
});

test('local storage isolates CIDs and saves completed separately from session end', () => {
    const { context } = fixture();
    context.saveLocalPlaybackRecord('bilibili:11:21', record(50, 100, { ended: true, completed: false }));
    context.saveLocalPlaybackRecord('bilibili:11:22', record(10, 200, { completed: true }));
    context.saveLocalPlaybackRecord('bilibili:11:21', record(90, 90));
    assert.equal(context.readLocalPlaybackRecord('bilibili:11:21').position, 50);
    assert.equal(context.readLocalPlaybackRecord('bilibili:11:21').completed, false);
    assert.equal(context.readLocalPlaybackRecord('bilibili:11:22').completed, true);
});

test('Bilibili identity resolves the selected P and excludes PGC and live pages', async () => {
    const { context } = fixture();
    const first = await context.resolvePlaybackContent({}, 'https://www.bilibili.com/video/BVtest?p=1&t=20');
    const second = await context.resolvePlaybackContent({}, 'https://www.bilibili.com/video/BVtest?p=2');
    assert.equal(first.identity, 'bilibili:11:21'); assert.equal(second.identity, 'bilibili:11:22');
    assert.equal(await context.resolvePlaybackContent({}, 'https://live.bilibili.com/1017'), undefined);
    assert.equal(await context.resolvePlaybackContent({}, 'https://www.bilibili.com/bangumi/play/ep123'), undefined);
});

test('generic HTML video identity survives tracking and time parameters but separates content pages', async () => {
    const { context } = fixture({ url: 'https://example.test/watch?id=first' });
    context.currentParser.parserKey = 'resource';
    const first = await context.resolvePlaybackContent({}, 'https://example.test/watch?id=first&t=30&utm_source=x');
    const same = await context.resolvePlaybackContent({}, 'https://example.test/watch?id=first');
    const next = await context.resolvePlaybackContent({}, 'https://example.test/watch?id=next');
    assert.match(first.identity, /^web:[a-f0-9]{32}$/);
    assert.equal(first.identity, same.identity); assert.notEqual(first.identity, next.identity);
    assert.equal(first.pageUrl, 'https://example.test/watch?id=first');
});

test('new global sync starts MPV at webpage progress despite legacy syncTime false and sends no credentials', async () => {
    const f = fixture({ position: 70, paused: false });
    await f.sync.resetPage(f.context.location.href);
    const prepared = await f.sync.prepareLaunch(media(70), { name: 'MPV', presetEvent: { syncTime: false } });
    assert.equal(prepared.time, 70); assert.equal(prepared.playbackProgressSync, true);
    assert.match(prepared.mpvOptions.join(' '), /--input-ipc-server="\\\\\.\\pipe\\mpv-lazy-progress-request-1"/);
    assert.ok(prepared.mpvOptions.some(option => option.includes('web_progress-identity=bilibili:11:21')));
    const identityOption = prepared.mpvOptions.find(option => option.includes('web_progress-identity='));
    const sessionOption = prepared.mpvOptions.find(option => option.includes('web_progress-session_id='));
    assert.notEqual(identityOption, sessionOption, '-append accepts only one key/value pair');
    assert.ok(!identityOption.includes(','));
    assert.ok(!JSON.stringify(f.requests).includes('csrf')); assert.ok(!JSON.stringify(f.requests).includes('SESSDATA'));
});

test('MPV starts at zero after an explicit backwards webpage seek and removes duplicate IPC options', async () => {
    const f = fixture({ position: 0, paused: true });
    f.context.saveLocalPlaybackRecord(content.identity, record(100));
    await f.sync.resetPage(f.context.location.href);
    f.advance(3000); f.sync.browserTouched = true; f.video.currentTime = 0;
    const input = media(0); input.mpvOptions.push('--input-ipc-server="old-pipe"', '--script-opts-append="web_progress-identity=old"');
    const prepared = await f.sync.prepareLaunch(input, { name: 'MPV', presetEvent: { syncTime: false } });
    assert.equal(prepared.time, 0);
    assert.equal(prepared.mpvOptions.filter(option => option.startsWith('--input-ipc-server=')).length, 1);
    assert.ok(!prepared.mpvOptions.join(' ').includes('identity=old'));
});

test('MPV uses the most recent backend record instead of the highest saved position', async () => {
    const f = fixture(); f.context.saveLocalPlaybackRecord(content.identity, record(120, 500));
    f.bridge.record = record(20, 900);
    await f.sync.resetPage(f.context.location.href);
    const prepared = await f.sync.prepareLaunch(media(), { name: 'MPV', presetEvent: { syncTime: false } });
    assert.equal(prepared.time, 20);
});

test('completed videos restart at zero on the next handoff while a manual close retains its position', async () => {
    const f = fixture(); f.context.saveLocalPlaybackRecord(content.identity, record(300, 1_900_000, { ended: true, completed: true }));
    await f.sync.resetPage(f.context.location.href);
    assert.equal(f.video.currentTime, 0);
    const restarted = await f.sync.prepareLaunch(media(300), { name: 'MPV' });
    assert.equal(restarted.time, 0); assert.equal(f.context.readLocalPlaybackRecord(content.identity).completed, false);
    const other = fixture(); other.context.saveLocalPlaybackRecord(content.identity, record(70, 1_900_000, { ended: true, completed: false }));
    await other.sync.resetPage(other.context.location.href);
    assert.equal((await other.sync.prepareLaunch(media(), { name: 'MPV' })).time, 70);
});

test('global opt-out and other players preserve original handoff behavior; live does not create progress sessions', async () => {
    const f = fixture(); f.context.currentConfig.global.playbackProgressSync = false;
    const input = media(50);
    assert.equal(await f.sync.prepareLaunch(input, { name: 'MPV' }), input);
    f.context.currentConfig.global.playbackProgressSync = true;
    assert.equal(await f.sync.prepareLaunch(input, { name: 'VLC' }), input);
    f.context.location.href = 'https://live.bilibili.com/1017';
    assert.equal(await f.sync.prepareLaunch(input, { name: 'MPV' }), input);
    assert.equal(f.requests.length, 0);
});

test('remote progress checks identity, session and sequence; mirrored seeks never echo back', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href); f.sync.sessionId = 'progress-owned';
    f.requests.length = 0;
    const notification = { type: 'playbackProgress', provider: 'bilibili', identity: content.identity,
        sessionId: 'progress-owned', sequence: 1, ...record(80, 2_000_001) };
    f.sync.handleBridgeMessage({ ...notification, identity: 'bilibili:11:22' });
    f.sync.handleBridgeMessage({ ...notification, sessionId: 'progress-other' });
    assert.equal(f.video.currentTime, 0);
    f.sync.handleBridgeMessage(notification); assert.equal(f.video.currentTime, 80);
    f.sync.handleBridgeMessage({ ...notification, position: 10 }); assert.equal(f.video.currentTime, 80);
    assert.equal(f.requests.length, 0);
    assert.equal(f.context.readLocalPlaybackRecord(content.identity).position, 80);
});

test('paused webpage cannot overwrite active MPV progress; deliberate browser seek takes over', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href); f.sync.sessionId = 'progress-owned';
    f.requests.length = 0; f.sync.saveBrowser(true); assert.equal(f.requests.length, 0);
    f.video.currentTime = 25; await drain();
    const update = f.requests.find(request => request.type === 'updatePlaybackProgress');
    assert.equal(update.position, 25); assert.equal(update.takeover, true); assert.equal(f.sync.sessionId, undefined);
});

test('a user seek or play inside an active remote echo window takes over immediately', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href); f.sync.sessionId = 'progress-owned';
    f.sync.handleBridgeMessage({ type: 'playbackProgress', provider: 'bilibili', identity: content.identity,
        sessionId: 'progress-owned', sequence: 1, ...record(80, 2_000_001) });
    assert.ok(f.sync.echoUntil > f.context.Date.now());
    f.requests.length = 0; f.video.currentTime = 25; await drain();
    assert.equal(f.requests.find(request => request.type === 'updatePlaybackProgress')?.takeover, true);
    assert.equal(f.context.readLocalPlaybackRecord(content.identity).position, 25);
    f.sync.sessionId = 'progress-again'; f.sync.echoUntil = f.context.Date.now() + 1800; f.sync.echoPosition = 25;
    f.video.paused = false; f.videoListeners.get('play')(); await drain();
    assert.equal(f.sync.sessionId, undefined);
    assert.equal(f.requests.filter(request => request.takeover).length, 2);
});

test('turning progress sync off stops its owned MPV session but ordinary navigation does not', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href); f.sync.sessionId = 'progress-owned';
    f.context.currentConfig.global.playbackProgressSync = false;
    await f.sync.resetPage(f.context.location.href);
    assert.deepEqual(f.requests.find(request => request.type === 'stopPlaybackProgress'), { type: 'stopPlaybackProgress', sessionId: 'progress-owned' });
});

test('an established session reconnects without bootstrapping and imports its latest backend position', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href); f.sync.sessionId = 'progress-owned';
    f.bridge.socket = { readyState: 3 }; f.bridge.activeSessionId = 'progress-owned'; f.bridge.record = record(92, 2_000_002);
    await f.sync.tick(); await drain();
    assert.equal(f.video.currentTime, 92); assert.equal(f.sync.sessionId, 'progress-owned');
    assert.equal(f.requests.at(-1).type, 'getPlaybackProgress');
});

test('changing P clears old record and cannot apply old session messages to a new video', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    f.sync.record = record(120); f.sync.sessionId = 'progress-old';
    f.context.location.href = 'https://www.bilibili.com/video/BVtest?p=2';
    f.video.currentTime = 0;
    await f.sync.resetPage(f.context.location.href);
    assert.equal(f.sync.content.identity, 'bilibili:11:22'); assert.equal(f.sync.record, undefined);
    assert.equal(f.video.currentTime, 0);
    f.sync.handleBridgeMessage({ type: 'playbackProgress', identity: content.identity, sessionId: 'progress-old', sequence: 4, ...record(150, 2_000_100) });
    assert.equal(f.video.currentTime, 0);
});

test('initial autoplay cannot supersede saved MPV progress while account history is pending', async () => {
    const f = fixture(); let release;
    const original = f.context.fetch;
    f.context.fetch = async (url, options) => url.includes('/history/cursor') ?
        new Promise(resolve => { release = () => resolve({ ok: true, json: async () => ({ code: 0, data: { list: [] } }) }); }) : original(url, options);
    f.bridge.activeSessionId = 'progress-existing'; f.bridge.record = record(85, 1_900_000);
    const initializing = f.sync.resetPage(f.context.location.href); await drain();
    f.video.paused = false; f.videoListeners.get('play')?.(); f.sync.saveBrowser(true);
    f.sync.handleBridgeMessage({ type: 'playbackProgressStopped', identity: content.identity, sessionId: 'progress-existing' });
    release(); await initializing;
    assert.equal(f.sync.browserTouched, false); assert.equal(f.video.currentTime, 85);
    assert.equal(f.sync.sessionId, undefined);
    assert.equal(f.requests.filter(request => request.type === 'updatePlaybackProgress').length, 0);
});

test('clicking P does not turn native autoplay into an explicit seek, while an actual zero-second seek during initialization is saved', async () => {
    const f = fixture(); let release;
    const original = f.context.fetch;
    f.context.fetch = async (url, options) => url.includes('/history/cursor') ?
        new Promise(resolve => { release = () => resolve({ ok: true, json: async () => ({ code: 0, data: { list: [] } }) }); }) : original(url, options);
    f.sync.userActionAt = f.context.Date.now(); // Prior P-navigation interaction is outside the newly bound video.
    f.bridge.record = record(100, 1_900_000);
    const initializing = f.sync.resetPage(f.context.location.href); await drain();
    f.video.paused = false; f.videoListeners.get('play')();
    assert.equal(f.sync.browserTouched, false);
    f.documentListeners.get('pointerdown')({ target: f.video });
    f.video.currentTime = 0; await drain();
    const prepared = await f.sync.prepareLaunch(media(0), { name: 'MPV' });
    assert.equal(prepared.time, 0);
    release(); await initializing;
    assert.equal(f.context.readLocalPlaybackRecord(content.identity).position, 0);
});

test('account history is CID-specific and newer account progress wins over older local progress', async () => {
    const f = fixture(); const original = f.context.fetch;
    f.context.saveLocalPlaybackRecord(content.identity, record(90, 10));
    f.context.fetch = async (url, options) => url.includes('/history/cursor') ? { ok: true, json: async () => ({ code: 0, data: { list: [
        { history: { oid: 11, cid: 22, business: 'archive' }, progress: 180, view_at: 500 },
        { history: { oid: 11, cid: 21, business: 'archive' }, progress: 25, view_at: 300, duration: 300 }
    ] } }) } : original(url, options);
    await f.sync.resetPage(f.context.location.href);
    assert.equal(f.video.currentTime, 25);
});

test('account heartbeat sends browser-only CSRF and WBI timing parameters; closing early never records completion', async () => {
    const f = fixture(); const reporter = new f.context.Reporter();
    reporter.enqueue(content, record(72, 2_000_000, { ended: true, completed: false }), true);
    await drain();
    assert.equal(f.posts.length, 1);
    const body = new URLSearchParams(f.posts[0].options.body), url = new URL(f.posts[0].url);
    assert.equal(body.get('played_time'), '72'); assert.equal(body.get('play_type'), '4');
    assert.equal(body.get('cid'), '21'); assert.equal(body.get('csrf'), 'browser-only-csrf');
    assert.equal(f.posts[0].options.credentials, 'include');
    assert.equal(url.searchParams.get('w_played_time'), '72'); assert.match(url.searchParams.get('w_rid'), /^[a-f0-9]{32}$/);
    f.advance(16000);
    reporter.enqueue(content, record(300, 2_016_000, { ended: true, completed: true }), true); await drain();
    assert.equal(new URLSearchParams(f.posts[1].options.body).get('played_time'), '-1');
});

test('history errors warn once while preserving local position and allowing media handoff', async () => {
    const f = fixture(); f.context.fetch = async () => { throw new Error('offline'); };
    const reporter = new f.context.Reporter();
    f.context.saveLocalPlaybackRecord(content.identity, record(75, 100));
    reporter.enqueue(content, record(75, 100), true); await drain();
    f.advance(16000); reporter.enqueue(content, record(76, 101), true); await drain();
    assert.equal(f.warnings.length, 1); assert.equal(f.context.readLocalPlaybackRecord(content.identity).position, 75);
    f.bridge.ensurePlaybackBridge = async () => { throw new Error('old bridge'); };
    const prepared = await f.sync.prepareLaunch(media(5), { name: 'MPV' });
    assert.equal(prepared.time, 75); assert.equal(prepared.video, 'https://cdn.test/video.m4s');
});

test('account opt-out and logged-out browsing never send authenticated history updates', async () => {
    const f = fixture(); const reporter = new f.context.Reporter();
    f.context.currentConfig.global.bilibiliHistorySync = false;
    reporter.enqueue(content, record(50), true); await drain(); assert.equal(f.posts.length, 0);
    f.context.currentConfig.global.bilibiliHistorySync = true; f.context.document.cookie = '';
    reporter.enqueue(content, record(50), true); await drain(); assert.equal(f.posts.length, 0);
    assert.equal(f.warnings.length, 0);
});

test('history writes are throttled to fifteen seconds and retain the latest paused final position', async () => {
    const f = fixture(); const reporter = new f.context.Reporter();
    reporter.enqueue(content, record(20, 2_000_000)); await drain(); assert.equal(f.posts.length, 1);
    f.advance(1000); reporter.enqueue(content, record(21, 2_001_000)); await drain(); assert.equal(f.posts.length, 1);
    f.advance(14000); const state = reporter.states.get(content.identity); await reporter.flush(state); await drain();
    assert.equal(f.posts.length, 2);
    reporter.enqueue(content, record(23, 2_015_001, { paused: true }), true); await drain();
    assert.equal(f.posts.length, 3);
    assert.equal(new URLSearchParams(f.posts.at(-1).options.body).get('played_time'), '23');
});

test('a failed terminal account update retries after fifteen seconds without requiring another MPV snapshot', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    const original = f.context.fetch; let fail = true;
    f.context.fetch = async (url, options) => {
        if (options?.method === 'POST' && fail) { fail = false; throw new Error('Temporary network failure'); }
        return original(url, options);
    };
    f.sync.reporter.enqueue(content, record(73, 2_000_000, { ended: true, completed: false }), true);
    await drain(); assert.equal(f.posts.length, 0); assert.ok(f.sync.reporter.states.get(content.identity).pending);
    f.advance(15000); await f.sync.tick(); await drain();
    assert.equal(f.posts.length, 1);
    assert.equal(new URLSearchParams(f.posts[0].options.body).get('played_time'), '73');
});

test('browser takeover cancels an old failed MPV account update instead of rewriting a newer zero-second seek', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    const original = f.context.fetch; let fail = true;
    f.context.fetch = async (url, options) => {
        if (options?.method === 'POST' && fail) { fail = false; throw new Error('Temporary network failure'); }
        return original(url, options);
    };
    f.sync.reporter.enqueue(content, record(73, 2_000_000, { ended: true, completed: false }), true); await drain();
    f.sync.sessionId = 'progress-owned'; f.advance(1); f.video.currentTime = 0; await drain();
    assert.equal(f.context.readLocalPlaybackRecord(content.identity).position, 0);
    f.advance(15000); await f.sync.tick(); await drain();
    assert.equal(f.posts.length, 0); assert.equal(f.sync.reporter.states.get(content.identity).pending, undefined);
});

test('a pending account request rechecks authority after nav and skips POST when browser has taken over', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href); let release;
    const original = f.context.fetch;
    f.context.fetch = async (url, options) => url.includes('/nav') ? new Promise(resolve => {
        release = async () => resolve(await original(url, options));
    }) : original(url, options);
    f.sync.reporter.enqueue(content, record(73, 2_000_000), true); await drain();
    f.sync.sessionId = 'progress-owned'; f.video.currentTime = 10;
    await release(); await drain(); assert.equal(f.posts.length, 0);
});

test('bridge rejects old services without spawning or stopping them and correlates protocol acknowledgements', async () => {
    const f = fixture();
    Object.assign(f.context, { BROWSER_RELAY_WS_URL: 'ws://127.0.0.1:9001/ws',
        BROWSER_RELAY_BOOTSTRAP_COMMAND: 'bootstrap', TELEGRAM_RESPONSE_EVENT: 'telegram', WebSocket: { OPEN: 1 } });
    vm.runInContext(between('class BrowserMediaBridge {', 'const browserMediaBridge =') +
        'globalThis.Bridge = BrowserMediaBridge;', f.context);
    const bridge = new f.context.Bridge(); let bootstraps = 0;
    bridge.bootstrapLocalBridge = async () => { bootstraps++; };
    f.context.fetchPlaybackBridgeHealth = async () => ({ apiVersion: 2 });
    await assert.rejects(bridge.ensurePlaybackBridge(), error => error.code === 'playback_progress_update_required');
    assert.equal(bootstraps, 0);
    bridge.ensurePlaybackBridge = async () => {}; const sent = [];
    bridge.socket = { readyState: 1, send: value => sent.push(JSON.parse(value)) };
    const pending = bridge.requestPlayback({ type: 'getPlaybackProgress', ...content }); await drain();
    bridge.handleMessage({ data: JSON.stringify({ type: 'playbackProgressState', requestId: sent[0].requestId,
        identity: content.identity, record: record(55) }) });
    assert.equal((await pending).record.position, 55);
    assert.equal(bridge.pendingPlaybackRequests.size, 0);
});

const healthyProgressBridge = { ok: true, playbackProgressApiVersion: 1, playbackProgressProviders: ['bilibili', 'web'] };
test('anonymous extension health callbacks and promise-only managers both complete without native requests', async () => {
    const f = fixture(); let nativeRequests = 0, options;
    f.context.fetch = async () => { nativeRequests++; throw new Error('Native transport should not run'); };
    f.context.GM = { xmlHttpRequest: received => { options = received; received.onload({ status: 200, responseText: JSON.stringify(healthyProgressBridge) }); return { abort() {} }; } };
    assert.equal((await f.context.fetchPlaybackBridgeHealth()).playbackProgressApiVersion, 1);
    assert.equal(options.anonymous, true); assert.equal(options.method, 'GET'); assert.equal(options.data, undefined);
    assert.equal(options.headers, undefined); assert.equal(nativeRequests, 0);
    f.context.GM.xmlHttpRequest = () => Promise.resolve({ status: 200, responseText: JSON.stringify(healthyProgressBridge) });
    assert.equal((await f.context.fetchPlaybackBridgeHealth()).playbackProgressApiVersion, 1);
    assert.equal(nativeRequests, 0);
});

test('extension onabort and permissions errors fall back to a bounded credential-free native health request', async () => {
    const f = fixture(); const requests = [];
    f.context.fetch = async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => healthyProgressBridge }; };
    f.context.GM = { xmlHttpRequest: options => { options.onabort(); return { abort() {} }; } };
    assert.equal((await f.context.fetchPlaybackBridgeHealth()).playbackProgressApiVersion, 1);
    f.context.GM.xmlHttpRequest = () => { throw new Error('Sensitive permission failure: signed-url cookie'); };
    await f.context.fetchPlaybackBridgeHealth(); assert.equal(requests.length, 2);
    for (const request of requests) {
        assert.equal(request.url, 'http://127.0.0.1:9002/health');
        assert.equal(request.options.credentials, 'omit'); assert.equal(request.options.mode, 'cors');
        assert.equal(request.options.cache, 'no-store'); assert.ok(request.options.signal);
        assert.equal(request.options.headers, undefined);
    }
});

test('a manager which never invokes callbacks reaches the hard timeout and proceeds to native health', async () => {
    const f = fixture(); const timers = new Map(); let index = 0, aborted = 0;
    f.context.setTimeout = (callback, delay) => { const id = ++index; timers.set(id, { callback, delay }); return id; };
    f.context.clearTimeout = id => timers.delete(id);
    f.context.GM = { xmlHttpRequest: () => ({ abort() { aborted++; } }) };
    f.context.fetch = async () => ({ ok: true, json: async () => healthyProgressBridge });
    const pending = f.context.fetchPlaybackBridgeHealth();
    [...timers.values()].find(timer => timer.delay === 2800).callback();
    assert.equal((await pending).playbackProgressApiVersion, 1); assert.equal(aborted, 1); assert.equal(timers.size, 0);
});

test('native transport times out and reports a fixed code without leaking rejected URLs or credentials', async () => {
    const f = fixture(); const timers = new Map(); let index = 0;
    f.context.setTimeout = (callback, delay) => { const id = ++index; timers.set(id, { callback, delay }); return id; };
    f.context.clearTimeout = id => timers.delete(id);
    f.context.fetch = async (url, options) => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('secret-cookie and private signed URL')));
    });
    const pending = f.context.fetchPlaybackBridgeHealth();
    [...timers.values()].find(timer => timer.delay === 2500).callback();
    await assert.rejects(pending, error => error.code === 'playback_health_unavailable' && !error.message.includes('secret'));
    f.context.currentConfig.global.browserRelayNotifications = false;
    f.sync.notifyUnavailable({ code: 'secret-cookie and private signed URL' });
    assert.deepEqual(f.warnings, ['bridge failed']);
    assert.deepEqual(f.consoleWarnings, ['External Player: playback_progress_unavailable']);
});

test('native health timeout remains active after response headers arrive and bounds JSON body reading', async () => {
    const f = fixture(); const timers = new Map(); let index = 0;
    f.context.setTimeout = (callback, delay) => { const id = ++index; timers.set(id, { callback, delay }); return id; };
    f.context.clearTimeout = id => timers.delete(id);
    f.context.fetch = async (url, options) => ({ ok: true, json: () => new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new Error('Stalled body')));
    }) });
    const pending = f.context.fetchPlaybackBridgeHealth(); await drain();
    assert.equal(timers.size, 1, 'The hard timeout must survive while response.json() is pending');
    [...timers.values()].find(timer => timer.delay === 2500).callback();
    await assert.rejects(pending, error => error.code === 'playback_health_unavailable');
    assert.equal(timers.size, 0);
});

test('native health fallback still rejects malformed capabilities and requires both progress providers', async () => {
    const f = fixture();
    Object.assign(f.context, { BROWSER_RELAY_WS_URL: 'ws://127.0.0.1:9001/ws',
        BROWSER_RELAY_BOOTSTRAP_COMMAND: 'bootstrap', TELEGRAM_RESPONSE_EVENT: 'telegram', WebSocket: { OPEN: 1 } });
    vm.runInContext(between('class BrowserMediaBridge {', 'const browserMediaBridge =') +
        'globalThis.Bridge = BrowserMediaBridge;', f.context);
    const bridge = new f.context.Bridge(); let connections = 0;
    bridge.ensureConnected = async () => { connections++; };
    for (const health of [ { ...healthyProgressBridge, playbackProgressApiVersion: 'oops' },
        { ...healthyProgressBridge, playbackProgressProviders: ['bilibili'] }, { ...healthyProgressBridge, ok: false } ]) {
        f.context.fetchPlaybackBridgeHealth = async () => health;
        await assert.rejects(bridge.ensurePlaybackBridge(false), error => error.code === 'playback_progress_update_required');
    }
    assert.equal(connections, 0);
    f.context.fetchPlaybackBridgeHealth = async () => healthyProgressBridge;
    await bridge.ensurePlaybackBridge(false); assert.equal(connections, 1);
});

test('new defaults enable progress and history, and explicit user opt-outs survive migration', () => {
    const f = fixture(); f.context.navigator.language = 'zh-CN';
    vm.runInContext(between('const defaultConfig = {', 'if (DEBUG === true)') +
        between('function updateConfig(defaultConfig, config) {', 'function matchParser(') +
        'globalThis.defaults = defaultConfig;', f.context);
    assert.equal(f.context.defaults.global.playbackProgressSync, true);
    assert.equal(f.context.defaults.global.bilibiliHistorySync, true);
    const saved = plain(f.context.defaults); saved.global.playbackProgressSync = false; saved.global.bilibiliHistorySync = false;
    const migrated = f.context.updateConfig(f.context.defaults, saved);
    assert.equal(migrated.global.playbackProgressSync, false); assert.equal(migrated.global.bilibiliHistorySync, false);
});

test('normal and CSP MPV launch paths preserve a zero-second --start', () => {
    const starts = source.match(/Number\.isFinite\(Number\(media\.time\)\) && media\.time !== undefined \? `--start=/g);
    assert.equal(starts.length, 4, 'MPV and MPVNET presets and CSP fallbacks must all accept zero');
    assert.ok(source.includes('!player.presetEvent.syncTime && !media.playbackProgressSync'));
});
