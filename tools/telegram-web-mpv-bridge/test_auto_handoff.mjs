import assert from 'node:assert/strict';
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

function fixture() {
    let nextTimer = 1;
    const timers = new Map();
    const launches = [];
    const messages = [];
    const listeners = [];
    const topWindow = {};
    const context = vm.createContext({
        URL, console, navigator: { language: 'zh-CN' },
        self: topWindow, top: topWindow, REFRESH_INTERVAL: 500,
        PROJECT_NAME: 'external-player', isReloading: false,
        location: { href: 'https://www.youtube.com/watch?v=first' },
        currentUrl: 'https://www.youtube.com/watch?v=first',
        currentConfig: { global: { autoHandoff: false, defaultPlayer: 'MPV' }, players: [
            { name: 'MPV', enable: true, presetEvent: { playAuto: false } },
            { name: 'VLC', enable: true, presetEvent: { playAuto: false } }
        ] },
        currentParser: { parserKey: 'ytdlp', play: async player => launches.push(player.name) },
        automaticHandoffState: undefined, automaticHandoffTimer: undefined,
        handoffLaunchInFlight: false, handoffUiReady: true, iframeListenersAttached: false,
        handoffPrimaryButton: undefined, handoffDockPlayButton: undefined, resourceSniffer: undefined,
        document: { querySelectorAll: () => [] },
        window: { addEventListener: (type, handler) => listeners.push(handler) },
        parent: { postMessage: message => messages.push(message) },
        isResourceFallbackParser: () => context.currentParser.isResourceFallback === true,
        isTelegramUrl: () => /^https:\/\/web\.telegram\.org/.test(context.location.href),
        resolveTelegramCaptureSource: () => undefined,
        setHandoffHostDisplay() {}, scheduleHandoffInitialEdgeHide() {},
        updateHandoffStatus() {}, showToast() {}, translation: { loadFail: 'Load failed' },
        setTimeout: (callback, delay) => { const id = nextTimer++; timers.set(id, { callback, delay }); return id; },
        clearTimeout: id => timers.delete(id),
        init() {}
    });
    vm.runInContext([
        between('function getBilibiliLiveRoomId(', 'function createBilibiliDanmakuSessionId('),
        between('function getEffectiveDefaultPlayer(', '// AUTO_HANDOFF_HELPERS_START'),
        between('// AUTO_HANDOFF_HELPERS_START', '// AUTO_HANDOFF_HELPERS_END'),
        between('async function launchWithPlayer(', 'function cacheHandoffElements('),
        between('function showButtonDiv() {', '// ======================================== 开始执行'),
        between('function initIframe() {', 'async function init(url) {')
    ].join('\n'), context);
    async function fireAuto() {
        const entry = [...timers].find(([, timer]) => timer.delay === 1000);
        if (!entry) return false;
        timers.delete(entry[0]);
        await entry[1].callback();
        return true;
    }
    function navigate(url) {
        context.location.href = url;
        context.currentUrl = url;
        context.scheduleAutomaticHandoff();
    }
    return { context, timers, launches, messages, listeners, fireAuto, navigate };
}

test('new and migrated configurations keep automatic default-player handoff off', () => {
    const { context } = fixture();
    vm.runInContext(between('const defaultConfig = {', 'if (DEBUG === true)') +
        between('function updateConfig(defaultConfig, config) {', 'function matchParser(') +
        'globalThis.defaults = defaultConfig;', context);
    assert.equal(context.defaults.global.autoHandoff, false);
    const stored = JSON.parse(JSON.stringify(context.defaults));
    delete stored.global.autoHandoff;
    stored.global.version = '1.2.14.13';
    stored.global.defaultPlayer = 'VLC';
    assert.equal(context.updateConfig(context.defaults, stored).global.autoHandoff, false);
    assert.equal(context.updateConfig(context.defaults, stored).global.defaultPlayer, 'VLC');
});

test('default-off pages do not launch, and enabled mode uses only the default player despite legacy autoplay', async () => {
    const { context, launches, fireAuto } = fixture();
    context.showButtonDiv();
    assert.equal(await fireAuto(), false);
    context.currentConfig.global.autoHandoff = true;
    context.currentConfig.global.defaultPlayer = 'VLC';
    context.currentConfig.players[0].presetEvent.playAuto = true;
    for (let i = 0; i < 6; i++) context.showButtonDiv();
    assert.equal(await fireAuto(), true);
    context.showButtonDiv();
    context.scheduleAutomaticHandoff();
    assert.equal(await fireAuto(), false);
    assert.deepEqual(launches, ['VLC']);
});

test('disabled players are skipped and old per-player autoplay remains bounded and compatible', async () => {
    const { context, launches, fireAuto } = fixture();
    context.currentConfig.players[0].presetEvent.playAuto = true;
    context.currentConfig.players[1].presetEvent.playAuto = true;
    context.currentConfig.players[1].enable = false;
    context.showButtonDiv();
    await fireAuto();
    context.showButtonDiv();
    assert.equal(await fireAuto(), false);
    assert.deepEqual(launches, ['MPV']);
});

test('automatic parsing failure is attempted once; the manual button can still retry', async () => {
    const { context, launches, fireAuto } = fixture();
    context.currentConfig.global.autoHandoff = true;
    context.currentParser.play = async player => { launches.push(player.name); throw new Error('not ready'); };
    context.showButtonDiv();
    await fireAuto();
    for (let i = 0; i < 10; i++) context.scheduleAutomaticHandoff();
    assert.equal(await fireAuto(), false);
    await assert.rejects(context.launchWithPlayer(context.currentConfig.players[0], { disabled: false }), /not ready/);
    assert.deepEqual(launches, ['MPV', 'MPV']);
});

test('manual handoff cancels pending automatic launch, including when another player is chosen', async () => {
    const { context, launches, fireAuto } = fixture();
    context.currentConfig.global.autoHandoff = true;
    context.showButtonDiv();
    await context.launchWithPlayer(context.currentConfig.players[1], { disabled: false });
    assert.equal(await fireAuto(), false);
    assert.deepEqual(launches, ['VLC']);
});

test('SPA content changes launch once each while share/time parameters and live iframe aliases do not repeat', async () => {
    const { context, launches, fireAuto, navigate } = fixture();
    context.currentConfig.global.autoHandoff = true;
    navigate('https://www.bilibili.com/video/BV123?p=1');
    await fireAuto();
    navigate('https://www.bilibili.com/video/BV123?p=1&spm_id_from=tracking&t=10');
    assert.equal(await fireAuto(), false);
    navigate('https://www.bilibili.com/video/BV123?p=10');
    await fireAuto();
    navigate('https://live.bilibili.com/719085');
    await fireAuto();
    navigate('https://live.bilibili.com/blanc/719085?liteVersion=true');
    assert.equal(await fireAuto(), false);
    navigate('https://www.youtube.com/watch?list=old&v=next');
    await fireAuto();
    navigate('https://www.youtube.com/watch?v=next&t=120&list=changed');
    assert.equal(await fireAuto(), false);
    assert.deepEqual(launches, ['MPV', 'MPV', 'MPV', 'MPV']);
});

test('a pending launch revalidates page, enabled setting and currently selected default player', async () => {
    const { context, launches, fireAuto, navigate } = fixture();
    context.currentConfig.global.autoHandoff = true;
    context.showButtonDiv();
    navigate('https://www.youtube.com/watch?v=second');
    context.currentConfig.global.defaultPlayer = 'VLC';
    await fireAuto();
    assert.deepEqual(launches, ['VLC']);
    navigate('https://www.youtube.com/watch?v=third');
    context.currentConfig.global.autoHandoff = false;
    await fireAuto();
    assert.deepEqual(launches, ['VLC']);
});

test('navigation during a running handoff waits for it before attempting the new content', async () => {
    const { context, launches, fireAuto, navigate } = fixture();
    let release;
    context.currentConfig.global.autoHandoff = true;
    context.currentParser.play = async player => { launches.push(player.name); await new Promise(resolve => { release = resolve; }); };
    context.showButtonDiv();
    const first = fireAuto();
    await Promise.resolve();
    navigate('https://www.youtube.com/watch?v=next');
    assert.equal(await fireAuto(), false);
    release();
    await first;
    context.currentParser.play = async player => launches.push(player.name);
    await fireAuto();
    assert.deepEqual(launches, ['MPV', 'MPV']);
});

test('resource fallback waits for a trusted source and Telegram ignores stale capture without a visible video', async () => {
    const { context, launches, fireAuto, navigate } = fixture();
    context.currentConfig.global.autoHandoff = true;
    context.currentParser.isResourceFallback = true;
    context.resourceSniffer = { getBestCandidate: () => undefined };
    context.showButtonDiv();
    assert.equal(await fireAuto(), false);
    context.resourceSniffer.getBestCandidate = () => ({ url: 'https://example.test/video.mp4' });
    context.showButtonDiv();
    await fireAuto();
    context.currentParser.isResourceFallback = false;
    context.resolveTelegramCaptureSource = () => ({ url: 'blob:first' });
    navigate('https://web.telegram.org/k/#peer');
    assert.equal(await fireAuto(), false);
    context.document.querySelectorAll = () => [{ src: 'blob:first', getBoundingClientRect: () => ({ width: 500, height: 300 }) }];
    context.scheduleAutomaticHandoff();
    await fireAuto();
    context.resolveTelegramCaptureSource = () => ({ url: 'blob:second' });
    context.document.querySelectorAll = () => [{ src: 'blob:second', getBoundingClientRect: () => ({ width: 500, height: 300 }) }];
    context.scheduleAutomaticHandoff();
    await fireAuto();
    assert.deepEqual(launches, ['MPV', 'MPV', 'MPV']);
});

test('saving settings suppresses current-content launch and iframe reload installs only one execute listener', async () => {
    const { context, launches, listeners, fireAuto } = fixture();
    context.currentConfig.global.autoHandoff = true;
    context.suppressAutomaticHandoffForCurrentPage();
    context.showButtonDiv();
    assert.equal(await fireAuto(), false);
    let executions = 0;
    context.currentParser.execute = async () => { executions++; };
    context.initIframe();
    context.initIframe();
    assert.equal(listeners.length, 1);
    await listeners[0]({ data: { name: 'external-player', method: 'execute' } });
    assert.equal(executions, 1);
    assert.deepEqual(launches, []);
});

test('quick-setting checkbox persists its explicit value and applies to the next video', async () => {
    const { context, launches, fireAuto, navigate } = fixture();
    const saved = [];
    context.handoffDefaultPlayer = { replaceChildren() {} };
    context.handoffRelayMode = {};
    context.handoffEdgeHide = {};
    context.handoffAutoHandoff = {};
    context.document.createElement = () => ({});
    context.renderQuickQualitySettings = () => {};
    context.persistQuickSettings = () => saved.push(context.currentConfig.global.autoHandoff);
    context.appendPlayButton = () => {};
    vm.runInContext(between('function renderQuickSettings(enabledPlayers)', 'function appendSettingButton()'), context);
    context.renderQuickSettings(context.currentConfig.players);
    assert.equal(context.handoffAutoHandoff.checked, false);
    context.handoffAutoHandoff.checked = true;
    context.handoffAutoHandoff.onchange();
    assert.deepEqual(saved, [true]);
    context.showButtonDiv();
    assert.equal(await fireAuto(), false);
    navigate('https://www.youtube.com/watch?v=next');
    await fireAuto();
    assert.deepEqual(launches, ['MPV']);
    assert.match(source, /<input type="checkbox" id="autoHandoff">/);
});

test('Bilibili list/festival DOM content identity launches new selections without replaying tracking changes', async () => {
    const { context, launches, fireAuto, navigate } = fixture();
    context.currentConfig.global.autoHandoff = true;
    context.window.__INITIAL_STATE__ = { videoData: { aid: 11, cid: 22 } };
    navigate('https://www.bilibili.com/festival/event#section1');
    await fireAuto();
    navigate('https://www.bilibili.com/festival/event?share_source=copy#section2');
    assert.equal(await fireAuto(), false);
    context.window.__INITIAL_STATE__.videoData = { aid: 33, cid: 44 };
    context.scheduleAutomaticHandoff();
    await fireAuto();
    assert.deepEqual(launches, ['MPV', 'MPV']);
    assert.notEqual(context.getAutoHandoffPageKey('https://example.test/player?source=first'),
        context.getAutoHandoffPageKey('https://example.test/player?source=next'));
});

test('automatic launch waits for body and feedback controls instead of consuming its only attempt early', async () => {
    const f = fixture();
    f.context.currentConfig.global.autoHandoff = true;
    f.context.handoffUiReady = false;
    let releaseBody;
    const appended = [];
    f.context.waitForBody = () => new Promise(resolve => { releaseBody = resolve; });
    for (const name of ['appendCss', 'appendToastDiv', 'appendLoadingDiv', 'appendButtonDiv']) {
        f.context[name] = () => appended.push(name);
    }
    vm.runInContext(between('async function appendAll() {', 'async function initTop() {'), f.context);
    const initializing = f.context.appendAll();
    f.context.scheduleAutomaticHandoff();
    assert.equal(await f.fireAuto(), false);
    assert.deepEqual(f.launches, []);
    releaseBody();
    await initializing;
    assert.deepEqual(appended, ['appendCss', 'appendToastDiv', 'appendLoadingDiv', 'appendButtonDiv']);
    f.context.scheduleAutomaticHandoff();
    assert.equal(await f.fireAuto(), true);
    f.context.scheduleAutomaticHandoff();
    assert.equal(await f.fireAuto(), false);
    assert.deepEqual(f.launches, ['MPV']);
});

test('initial resource sniffing includes early performance entries while later SPA pages use a recent window', async () => {
    const { context } = fixture();
    const windows = [];
    context.currentUrl = undefined;
    context.currentConfig.global.language = 'en';
    context.resourceFallbackDisplayPromise = undefined;
    context.installTelegramPageCapture = () => {};
    context.loadConfig = () => context.currentConfig;
    context.translations = { en: {} };
    context.matchParser = () => undefined;
    context.defaultConfig = { global: { parser: {} } };
    context.performance = { now: () => 10000 };
    context.PassiveResourceSniffer = class {
        constructor(callback, startTime) { windows.push(startTime); }
        start() {}
        stop() {}
    };
    context.PARSER = { RESOURCE: class { constructor() { this.isResourceFallback = true; } } };
    context.buttonDiv = undefined;
    context.initTop = async () => {};
    vm.runInContext(between('async function init(url) {', 'setInterval(() => {'), context);
    await context.init('https://example.test/first');
    assert.equal(context.currentUrl, 'https://example.test/first');
    await context.init('https://example.test/second');
    assert.deepEqual(windows, [0, 9000]);
});
