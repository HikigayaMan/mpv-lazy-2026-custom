import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
const request = source.slice(source.indexOf('async function fetchBilibiliLiveJson('), source.indexOf('async function resolveBilibiliLiveDanmakuAuth('));
function fixture(fetch) {
    const timers = new Map();
    const context = vm.createContext({ fetch, AbortController,
        setTimeout(callback, ms) { timers.set(1, { callback, ms }); return 1; }, clearTimeout: id => timers.delete(id) });
    vm.runInContext(request, context);
    return { context, timers };
}

test('request classification preserves HTTP status and treats risk control as an HTTP failure', async () => {
    for (const status of [412, 503]) {
        const f = fixture(async () => ({ ok: false, status }));
        await assert.rejects(f.context.fetchBilibiliLiveJson('https://api.bilibili.com/public'), error =>
            error.bilibiliRequestCause === 'request_http' && error.httpStatus === status);
        assert.equal(f.timers.size, 0);
    }
});

test('network exceptions and invalid JSON have separate fixed causes without retaining their sensitive messages', async () => {
    for (const [fetch, cause] of [
        [async () => { throw new Error('private signed URL'); }, 'request_network'],
        [async () => ({ ok: true, json: async () => { throw new Error('secret response body'); } }), 'response_json']
    ]) {
        const f = fixture(fetch);
        await assert.rejects(f.context.fetchBilibiliLiveJson('https://api.bilibili.com/public'), error =>
            error.bilibiliRequestCause === cause && !/private|secret/.test(error.message));
        assert.equal(f.timers.size, 0);
    }
});

test('the six-second per-request deadline reports timeout separately from network/JSON errors', async () => {
    const f = fixture((url, options) => new Promise((resolve, reject) => {
        assert.equal(options.credentials, 'include');
        options.signal.addEventListener('abort', () => reject(new Error('private abort details')));
    }));
    const pending = f.context.fetchBilibiliLiveJson('https://api.bilibili.com/public');
    assert.equal(f.timers.get(1).ms, 6000);
    f.timers.get(1).callback();
    await assert.rejects(pending, error => error.bilibiliRequestCause === 'request_timeout' && !error.message.includes('private'));
    assert.equal(f.timers.size, 0);
});
