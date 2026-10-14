import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
const feedback = source.slice(source.indexOf('function appendToastDiv() {'), source.indexOf('function setHandoffHostDisplay('));
function fixture(bodyReady = true) {
    const elements = new Map(), timers = new Map();
    let nextTimer = 0;
    const body = { appendChild(element) { elements.set(element.id, element); } };
    const context = vm.createContext({ PROJECT_NAME: 'external-player', toastDiv: undefined, loadingDiv: undefined,
        loadingId: undefined, translation: { loadTimeout: 'timeout' }, currentConfig: { global: {} },
        document: { body: bodyReady ? body : undefined, getElementById: id => elements.get(id),
            createElement: () => ({ style: {}, appendChild() {} }) },
        setTimeout(callback, ms) { const id = ++nextTimer; timers.set(id, { callback, ms }); return id; },
        clearTimeout: id => timers.delete(id) });
    vm.runInContext(feedback, context);
    return { context, elements, timers, body };
}

test('early feedback calls tolerate a missing body and lazily create exactly one control once it exists', () => {
    const f = fixture(false);
    f.context.showToast('early');
    f.context.showLoading();
    f.context.hideLoading();
    assert.equal(f.elements.size, 0);
    assert.equal(f.timers.size, 0);
    f.context.document.body = f.body;
    f.context.showToast('ready');
    f.context.showLoading(32000);
    f.context.appendToastDiv();
    f.context.appendLoadingDiv();
    assert.equal(f.elements.size, 2);
    assert.equal(f.context.toastDiv.textContent, 'ready');
    assert.equal(f.context.loadingDiv.style.display, 'block');
    assert.equal(f.timers.get(f.context.loadingId).ms, 32000);
});

test('hiding loading cancels its timeout so a completed launch does not produce a later timeout warning', () => {
    const f = fixture();
    f.context.showLoading(32000);
    f.context.hideLoading();
    assert.equal(f.context.loadingDiv.style.display, 'none');
    assert.equal(f.timers.size, 0);
    assert.equal(f.context.loadingId, undefined);
});
