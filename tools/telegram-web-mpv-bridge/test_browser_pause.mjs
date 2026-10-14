import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
const first = source.indexOf('// WEB_PLAYBACK_PAUSE_HELPERS_START');
const last = source.indexOf('// WEB_PLAYBACK_PAUSE_HELPERS_END', first);
assert.ok(first >= 0 && last > first, 'Missing web playback pause helper');
const helper = source.slice(first, last);

// Independent documents and window identities make iframe messages exercise the
// same source checks as a browser. Time advances without waiting on real timers.
function fixture() {
    let activeWindow;
    let now = 1700000000000;
    let nextTimer = 0;
    let nextRequest = 0;
    const nodes = [];
    const messages = [];
    const pendingMessages = [];
    const timers = new Map();
    const run = (node, callback) => {
        const previous = activeWindow;
        activeWindow = node.window;
        try { return callback(); } finally { activeWindow = previous; }
    };

    function eventTarget(node, target = {}) {
        const listeners = new Map();
        target.addEventListener = (type, callback) => {
            if (!listeners.has(type)) listeners.set(type, new Set());
            listeners.get(type).add(callback);
        };
        target.removeEventListener = (type, callback) => listeners.get(type)?.delete(callback);
        target.emit = (type, properties = {}) => {
            const event = { type, target, isTrusted: false, preventDefault() {},
                composedPath: () => [properties.target || target], ...properties };
            for (const callback of [...(listeners.get(type) || [])]) run(node, () => callback(event));
            return event;
        };
        return target;
    }

    function createNode(parentNode, url = 'https://live.bilibili.com/30040584', initializeImmediately = true) {
        const node = { elements: [], observers: [], parentNode };
        nodes.push(node);
        node.window = eventTarget(node);
        node.location = { href: url, origin: new URL(url).origin };
        node.document = eventTarget(node, { defaultView: node.window, activeElement: null, hidden: false });
        const element = (tag, properties = {}) => {
            const item = eventTarget(node, { tagName: tag.toUpperCase(), nodeName: tag.toUpperCase(),
                isConnected: true, ownerDocument: node.document, children: [], className: '',
                ...properties });
            item.matches = selector => String(selector).split(',').some(part => {
                part = part.trim();
                if (part === '*' || part === tag.toLowerCase()) return true;
                if (part.startsWith('.')) return item.className.split(' ').includes(part.slice(1));
                if (part.includes('contenteditable')) return item.isContentEditable === true;
                if (part.startsWith('#')) return item.id === part.slice(1);
                const attribute = part.match(/^\[([\w-]+)(?:="([^"]+)")?\]$/);
                if (attribute) return attribute[2] === undefined ? item[attribute[1]] !== undefined : item[attribute[1]] === attribute[2];
                return false;
            });
            item.contains = candidate => item === candidate || item.children.some(child => child.contains(candidate));
            item.closest = selector => {
                for (let current = item; current; current = current.parentElement) {
                    if (current.matches?.(selector)) return current;
                }
                return null;
            };
            item.querySelectorAll = selector => node.elements.filter(candidate =>
                candidate !== item && item.contains(candidate) && candidate.matches(selector));
            item.querySelector = selector => item.querySelectorAll(selector)[0] || null;
            item.getAttribute = name => item[name] ?? null;
            node.elements.push(item);
            return item;
        };
        const root = element('html');
        node.document.documentElement = root;
        node.document.body = element('body');
        root.children.push(node.document.body);
        node.document.body.parentElement = root;
        node.document.querySelectorAll = selector => node.elements.filter(item => item.isConnected && item.matches(selector));
        node.document.querySelector = selector => node.document.querySelectorAll(selector)[0] || null;
        node.document.getElementsByTagName = tag => node.document.querySelectorAll(tag);
        node.document.contains = candidate => node.elements.includes(candidate) && candidate.isConnected;
        node.window.document = node.document;
        node.window.location = node.location;
        node.window.parent = parentNode?.window || node.window;
        node.window.top = parentNode?.window.top || node.window;
        node.window.self = node.window;
        node.window.postMessage = data => {
            const sender = activeWindow;
            messages.push({ from: sender, to: node.window, data: JSON.parse(JSON.stringify(data)) });
            pendingMessages.push({ node, data, sender });
        };
        const schedule = (callback, ms, interval) => {
            const id = ++nextTimer;
            timers.set(id, { node, callback, ms: Number(ms) || 0, due: now + (Number(ms) || 0), interval });
            return id;
        };
        const timerApi = {
            setTimeout: (callback, ms) => schedule(callback, ms, false),
            setInterval: (callback, ms) => schedule(callback, ms, true),
            clearTimeout: id => timers.delete(id), clearInterval: id => timers.delete(id)
        };
        Object.assign(node.window, timerApi);
        const context = vm.createContext({ console, URL,
            Date: class extends Date { static now() { return now; } },
            PROJECT_NAME: 'external-player', location: node.location, window: node.window,
            document: node.document, parent: node.window.parent, self: node.window, top: node.window.top,
            currentUrl: url, getAutomaticHandoffKey: () => node.location.href,
            createBridgeRequestId: () => `test-${++nextRequest}`,
            getAutoHandoffPageKey: value => String(value), ...timerApi,
            MutationObserver: class {
                constructor(callback) { this.callback = callback; node.observers.push(this); }
                observe() { this.observing = true; }
                disconnect() { this.observing = false; }
            }
        });
        node.context = context;
        node.initialize = () => {
            run(node, () => vm.runInContext(helper +
                '\nglobalThis.guard = webPlaybackPauseGuard; globalThis.WebPlaybackPauseGuard = WebPlaybackPauseGuard;', context));
            node.guard = context.guard;
        };
        if (initializeImmediately) node.initialize();
        node.addVideo = (container = node.document.body) => {
            const video = element('video', { paused: false, pauseCalls: 0,
                pause() { this.pauseCalls++; this.paused = true; },
                play() {
                    this.paused = false;
                    const event = this.emit('play');
                    node.document.emit('play', event);
                    this.emit('playing');
                    node.document.emit('playing', { target: this });
                    return Promise.resolve();
                } });
            video.parentElement = container;
            container.children.push(video);
            return video;
        };
        node.addContainer = () => {
            const host = element('div', { className: 'live-player-ctnr bpx-player-container' });
            host.parentElement = node.document.body;
            node.document.body.children.push(host);
            return host;
        };
        node.addControl = (container, properties = {}) => {
            const control = element('button', properties);
            control.parentElement = container;
            container.children.push(control);
            return control;
        };
        node.addVideoSurface = container => {
            const surface = element('div', { className: 'live-player-mounter' });
            surface.parentElement = container;
            container.children.push(surface);
            return surface;
        };
        node.addInput = () => element('input');
        node.addFrame = (child, container = node.document.body) => {
            const frame = element('iframe', { contentWindow: child.window });
            frame.parentElement = container;
            container.children.push(frame);
            child.frame = frame;
            return frame;
        };
        node.mutate = () => {
            for (const observer of node.observers) if (observer.observing) run(node, () =>
                observer.callback([{ addedNodes: node.elements.slice(-1), removedNodes: [] }]));
        };
        node.gesture = (target, trusted = true) => {
            node.document.emit('pointerdown', { target, isTrusted: trusted, button: 0 });
            node.document.emit('pointerup', { target, isTrusted: trusted, button: 0 });
        };
        return node;
    }

    const flush = () => {
        for (let count = 0; pendingMessages.length; count++) {
            assert.ok(count < 1000, 'unexpected iframe message loop');
            const { node, data, sender } = pendingMessages.shift();
            node.window.emit('message', { data, source: sender, origin: sender?.location?.origin || 'null' });
        }
    };
    const advance = milliseconds => {
        now += milliseconds;
        for (const [id, timer] of [...timers]) {
            if (timer.due > now || !timers.has(id)) continue;
            if (!timer.interval) timers.delete(id);
            else timer.due = now + timer.ms;
            run(timer.node, timer.callback);
        }
        // The userscript's existing page timer invokes tick every 500 ms.
        for (const node of nodes) if (node.guard) run(node, () => node.guard.tick());
        flush();
    };
    return { createNode, run, messages, advance, nodes, timers, flush,
        start(node, options = {}) { const result = run(node, () => node.guard.start(options)); flush(); return result; },
        tick(node) { const result = run(node, () => node.guard.tick()); flush(); return result; },
        post(sender, recipient, data) { run(sender, () => recipient.window.postMessage(data, '*')); flush(); } };
}

test('handoff pauses immediately and blocks site autoplay even after the former ten-second retry window', async () => {
    const f = fixture();
    const page = f.createNode();
    const video = page.addVideo();
    f.start(page, { playerName: 'MPV' });
    assert.equal(video.paused, true);
    f.advance(65000);
    f.tick(page);
    await f.run(page, () => video.play());
    assert.equal(video.paused, true);
    assert.ok(video.pauseCalls >= 2);
    assert.equal(page.guard.isCurrent(), true);
});

test('videos created late or replaced after handoff are also paused', () => {
    const f = fixture();
    const page = f.createNode();
    f.start(page);
    f.advance(15000);
    const late = page.addVideo();
    page.mutate();
    assert.equal(late.paused, true, 'a newly inserted video is paused by the mutation callback');
    late.isConnected = false;
    const replacement = page.addVideo();
    f.tick(page);
    assert.equal(replacement.paused, true);
});

test('chat typing and synthetic player gestures do not release the pause guard', async () => {
    const f = fixture();
    const page = f.createNode();
    const video = page.addVideo(page.addContainer());
    const chat = page.addInput();
    f.start(page);
    page.gesture(chat);
    page.document.emit('keydown', { target: chat, key: ' ', code: 'Space', isTrusted: true });
    await f.run(page, () => video.play());
    assert.equal(video.paused, true);
    page.gesture(video, false);
    await f.run(page, () => video.play());
    assert.equal(video.paused, true);
    assert.equal(page.guard.isCurrent(), true);
});

test('an explicit user replay releases the guard and future site play is allowed', async () => {
    const f = fixture();
    const page = f.createNode();
    const host = page.addContainer();
    const video = page.addVideo(host);
    f.start(page);
    page.gesture(video);
    await f.run(page, () => video.play());
    assert.equal(video.paused, false);
    assert.equal(page.guard.isCurrent(), false);
    const count = video.pauseCalls;
    f.advance(20000);
    await f.run(page, () => video.play());
    assert.equal(video.pauseCalls, count);
});

test('nested and late iframe players receive one pause command per window without repeating every tick', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    const nested = f.createNode(child, 'https://player.bilibili.com/embed.html');
    page.addFrame(child);
    child.addFrame(nested);
    const parentVideo = page.addVideo();
    const childVideo = child.addVideo();
    const nestedVideo = nested.addVideo();
    f.start(page, { playerName: 'MPV' });
    assert.deepEqual([parentVideo, childVideo, nestedVideo].map(video => video.paused), [true, true, true]);
    const initialCommands = f.messages.filter(message => message.data.method === 'pause').length;
    f.tick(page);
    f.tick(child);
    assert.equal(f.messages.filter(message => message.data.method === 'pause').length, initialCommands);
    const late = f.createNode(page, 'https://player.bilibili.com/late.html');
    const lateVideo = late.addVideo();
    page.addFrame(late);
    f.tick(page);
    assert.equal(lateVideo.paused, true);
});

test('a controlled child initialized after handoff receives its missed pause command', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    page.addFrame(child);
    f.start(page);
    const video = child.addVideo();
    video.paused = false;
    f.post(child, page, { name: 'external-player', method: 'init' });
    assert.equal(video.paused, true);
    assert.equal(f.messages.filter(message => message.to === child.window && message.data.method === 'pause').length, 2);
});

test('unrelated frame messages cannot establish or release a pause guard', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    const stranger = f.createNode();
    page.addFrame(child);
    const video = child.addVideo();
    f.post(stranger, child, { name: 'external-player', method: 'pause', pauseToken: 'forged-token' });
    assert.equal(video.paused, false);
    assert.equal(child.guard.isCurrent(), false);
    f.start(page);
    const token = f.messages.find(message => message.to === child.window && message.data.method === 'pause').data.pauseToken;
    f.post(stranger, page, { name: 'external-player', method: 'releasePause', pauseToken: token });
    f.post(child, page, { name: 'external-player', method: 'releasePause', pauseToken: 'stale-token' });
    assert.equal(page.guard.isCurrent(), true);
    assert.equal(child.guard.isCurrent(), true);
});

test('user replay in a nested player releases the parent and every controlled sibling', async () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    const nested = f.createNode(child, 'https://player.bilibili.com/nested.html');
    const sibling = f.createNode(page, 'https://player.bilibili.com/sibling.html');
    page.addFrame(child);
    page.addFrame(sibling);
    child.addFrame(nested);
    const parentVideo = page.addVideo();
    const siblingVideo = sibling.addVideo();
    const nestedVideo = nested.addVideo(nested.addContainer());
    f.start(page);
    nested.gesture(nestedVideo);
    await f.run(nested, () => nestedVideo.play());
    f.flush();
    assert.deepEqual([page, child, nested, sibling].map(node => node.guard.isCurrent()), [false, false, false, false]);
    await f.run(page, () => parentVideo.play());
    await f.run(sibling, () => siblingVideo.play());
    assert.deepEqual([parentVideo.paused, siblingVideo.paused, nestedVideo.paused], [false, false, false]);
});

test('SPA navigation releases the old guard before a new room video is paused', () => {
    const f = fixture();
    const page = f.createNode();
    const original = page.addVideo();
    f.start(page);
    original.isConnected = false;
    page.location.href = 'https://live.bilibili.com/14466268';
    const newRoom = page.addVideo();
    f.tick(page);
    assert.equal(newRoom.paused, false);
    assert.equal(page.guard.isCurrent(), false);
});

test('pagehide releases the iframe tree and disconnects mutation observers', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    page.addFrame(child);
    page.addVideo();
    child.addVideo();
    f.start(page);
    page.window.emit('pagehide');
    f.flush();
    assert.equal(page.guard.isCurrent(), false);
    assert.equal(child.guard.isCurrent(), false);
    assert.equal(f.timers.size, 0);
    assert.ok(page.observers.every(observer => !observer.observing));
    assert.ok(child.observers.every(observer => !observer.observing));
});

test('a media element that initially throws does not block other players and is retried on the next scan', () => {
    const f = fixture();
    const page = f.createNode();
    const loading = page.addVideo();
    const ready = page.addVideo();
    const pause = loading.pause;
    let first = true;
    loading.pause = function () {
        if (first) { first = false; throw new Error('player not ready'); }
        return pause.call(this);
    };
    f.start(page);
    assert.equal(loading.paused, false);
    assert.equal(ready.paused, true);
    f.advance(500);
    assert.equal(loading.paused, true);
});

test('a player gesture that expires before playback cannot permit a later site autoplay', async () => {
    const f = fixture();
    const page = f.createNode();
    const video = page.addVideo();
    f.start(page);
    page.gesture(video);
    f.advance(4000);
    await f.run(page, () => video.play());
    assert.equal(video.paused, true);
    assert.equal(page.guard.isCurrent(), true);
});

test('trusted playback shortcuts can resume the page while synthetic shortcuts cannot', async () => {
    const f = fixture();
    const page = f.createNode();
    const video = page.addVideo();
    f.start(page);
    page.document.emit('keydown', { target: page.document.body, code: 'Space', key: ' ', isTrusted: false });
    await f.run(page, () => video.play());
    assert.equal(video.paused, true);
    page.document.emit('keydown', { target: page.document.body, code: 'Space', key: ' ', isTrusted: true });
    await f.run(page, () => video.play());
    assert.equal(video.paused, false);
    assert.equal(page.guard.isCurrent(), false);
});

test('disabling pauseAuto while the guard is active releases every controlled frame', async () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    page.addFrame(child);
    const video = page.addVideo();
    child.addVideo();
    page.context.currentConfig = { players: [{ name: 'MPV', presetEvent: { pauseAuto: true } }] };
    f.start(page, { playerName: 'MPV' });
    page.context.currentConfig.players[0].presetEvent.pauseAuto = false;
    f.tick(page);
    assert.equal(page.guard.isCurrent(), false);
    assert.equal(child.guard.isCurrent(), false);
    await f.run(page, () => video.play());
    assert.equal(video.paused, false);
});

test('replacing an iframe window releases its old guard while keeping the new player and siblings paused', () => {
    const f = fixture();
    const page = f.createNode();
    const old = f.createNode(page, 'https://player.bilibili.com/old.html');
    const sibling = f.createNode(page, 'https://player.bilibili.com/sibling.html');
    const frame = page.addFrame(old);
    page.addFrame(sibling);
    old.addVideo();
    sibling.addVideo();
    f.start(page);
    const replacement = f.createNode(page, 'https://player.bilibili.com/new.html');
    const video = replacement.addVideo();
    frame.contentWindow = replacement.window;
    f.tick(page);
    assert.equal(old.guard.isCurrent(), false);
    assert.equal(page.guard.isCurrent(), true);
    assert.equal(sibling.guard.isCurrent(), true);
    assert.equal(replacement.guard.isCurrent(), true);
    assert.equal(video.paused, true);
});

test('a stale release from the previous handoff cannot cancel a newer session in the same frame tree', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    page.addFrame(child);
    page.addVideo();
    child.addVideo();
    f.start(page);
    const old = f.messages.find(message => message.to === child.window && message.data.method === 'pause').data.pauseToken;
    f.start(page);
    const current = f.messages.filter(message => message.to === child.window && message.data.method === 'pause').at(-1).data.pauseToken;
    assert.notEqual(current, old);
    f.post(child, page, { name: 'external-player', method: 'releasePause', pauseToken: old });
    f.post(page, child, { name: 'external-player', method: 'releasePause', pauseToken: old });
    assert.equal(page.guard.isCurrent(), true);
    assert.equal(child.guard.isCurrent(), true);
});

test('a child script initialized after the initial pause recovers through readiness without cancelling the parent', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html', false);
    page.addFrame(child);
    const video = child.addVideo();
    f.start(page);
    assert.equal(video.paused, false, 'the first pause arrived before the child installed its listener');
    child.initialize();
    f.run(child, () => child.guard.stop(false));
    f.run(child, () => child.guard.ready());
    f.flush();
    assert.equal(page.guard.isCurrent(), true);
    assert.equal(child.guard.isCurrent(), true);
    assert.equal(video.paused, true);
});

test('a child pagehide or initialization cleanup does not revoke its parent handoff', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    const sibling = f.createNode(page, 'https://player.bilibili.com/sibling.html');
    page.addFrame(child);
    page.addFrame(sibling);
    child.addVideo();
    sibling.addVideo();
    f.start(page);
    child.window.emit('pagehide');
    f.flush();
    assert.equal(page.guard.isCurrent(), true);
    assert.equal(sibling.guard.isCurrent(), true);
    assert.equal(child.guard.isCurrent(), false);
    f.run(child, () => child.guard.ready());
    f.flush();
    assert.equal(child.guard.isCurrent(), true);
    f.run(child, () => child.guard.stop(false));
    f.flush();
    assert.equal(page.guard.isCurrent(), true);
    f.run(child, () => child.guard.ready());
    f.flush();
    assert.equal(child.guard.isCurrent(), true);
});

test('iframe navigation is cleaned locally and readiness rebinds the existing handoff to the new child page', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/old.html');
    page.addFrame(child);
    const old = child.addVideo();
    f.start(page);
    old.isConnected = false;
    child.location.href = 'https://player.bilibili.com/new.html';
    const current = child.addVideo();
    f.tick(child);
    assert.equal(current.paused, false);
    assert.equal(child.guard.isCurrent(), false);
    assert.equal(page.guard.isCurrent(), true);
    f.run(child, () => child.guard.ready());
    f.flush();
    assert.equal(current.paused, true);
    assert.equal(child.guard.isCurrent(), true);
});

test('malformed parent tokens cannot establish a guard or disrupt an active handoff', () => {
    const f = fixture();
    const page = f.createNode();
    const child = f.createNode(page, 'https://player.bilibili.com/live.html');
    page.addFrame(child);
    const video = child.addVideo();
    for (const pauseToken of [null, 7, {}, [], '', 'invalid token', 'x'.repeat(97)]) {
        f.post(page, child, { name: 'external-player', method: 'pause', pauseToken });
        assert.equal(child.guard.isCurrent(), false);
        assert.equal(video.paused, false);
    }
    f.start(page);
    for (const pauseToken of [null, {}, 'invalid token']) {
        f.post(page, child, { name: 'external-player', method: 'pause', pauseToken });
        assert.equal(page.guard.isCurrent(), true);
        assert.equal(child.guard.isCurrent(), true);
    }
});

test('volume, quality and other player-container clicks do not authorize a site autoplay', async () => {
    const f = fixture();
    const page = f.createNode();
    const host = page.addContainer();
    const video = page.addVideo(page.addVideoSurface(host));
    const volume = page.addControl(host, { title: '音量' });
    const quality = page.addControl(host, { title: '清晰度' });
    f.start(page);
    for (const target of [volume, quality, host]) {
        page.gesture(target);
        await f.run(page, () => video.play());
        assert.equal(video.paused, true);
        assert.equal(page.guard.isCurrent(), true);
    }
});

test('a titled play button resumes the original page without being forced to pause again', async () => {
    const f = fixture();
    const page = f.createNode();
    const host = page.addContainer();
    const video = page.addVideo(page.addVideoSurface(host));
    const play = page.addControl(host, { title: '播放' });
    f.start(page);
    page.gesture(play);
    await f.run(page, () => video.play());
    assert.equal(video.paused, false);
    assert.equal(page.guard.isCurrent(), false);
});

test('clicking the video surface permits an explicit replay but a similarly titled button outside the player does not', async () => {
    const f = fixture();
    const page = f.createNode();
    const host = page.addContainer();
    const surface = page.addVideoSurface(host);
    const video = page.addVideo(surface);
    const unrelated = page.addControl(page.document.body, { title: '播放' });
    f.start(page);
    page.gesture(unrelated);
    await f.run(page, () => video.play());
    assert.equal(video.paused, true);
    page.gesture(surface);
    await f.run(page, () => video.play());
    assert.equal(video.paused, false);
    assert.equal(page.guard.isCurrent(), false);
});
