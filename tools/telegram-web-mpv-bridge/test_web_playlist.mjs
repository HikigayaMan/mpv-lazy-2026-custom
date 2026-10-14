import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
function between(start, end) {
    const first = source.indexOf(start), last = source.indexOf(end, first + start.length);
    assert.ok(first >= 0 && last > first, `Missing section ${start}`);
    return source.slice(first, last);
}
const pages = { aid: 11, bvid: 'BVtest', cid: 21, title: 'Album', pages: [
    { cid: 21, page: 1, part: 'MISMATCH', duration: 194 },
    { cid: 22, page: 2, part: 'Movie Star', duration: 158 }
] };
const playlistId = `playlist-${'a'.repeat(32)}`;
const sessionId = 'progress-list-test';
const manifest = () => ({ playlistId, sessionId, selectedIndex: 0, initialPosition: 70,
    items: plain(pages.pages).map(page => ({ itemId: `11:${page.cid}`, aid: 11, cid: page.cid, bvid: 'BVtest',
        p: page.page, title: page.part, duration: page.duration, pageUrl: `https://www.bilibili.com/video/BVtest/?p=${page.page}` })) });
const media = () => ({ video: 'https://v.bilivideo.com/first.m4s', audio: 'https://v.bilivideo.com/audio.m4s',
    subtitle: 'https://www.lckp.top/common/bilibili/jsonToSrt/?url=old', title: 'Album', time: 70,
    bilibili: { aid: 11, cid: 21, bvid: 'BVtest', duration: 194 }, ytdlp: {},
    mpvOptions: ['--fullscreen=yes', '--script-opts-append="quality-menu-stream_variants=old"',
        '--audio-file=old', '--script-opts-append="cid=21"', '--start=70'] });
function fixture({ url = 'https://www.bilibili.com/video/BVtest/?p=1', sync = true, fastStart = false } = {}) {
    let now = 2_000_000, position = 70;
    const storage = {}, requests = [], streamCalls = [], subtitles = [], posts = [], warnings = [], diagnostics = [];
    let upload;
    const videoListeners = new Map();
    const video = { duration: 194, paused: true, ended: false,
        get currentTime() { return position; }, set currentTime(value) { position = value; videoListeners.get('seeked')?.(); },
        getBoundingClientRect: () => ({ width: 1280, height: 720 }), closest() {},
        addEventListener: (event, handler) => videoListeners.set(event, handler), removeEventListener: event => videoListeners.delete(event) };
    const bridge = { playbackTransport: 'http', bootstrapCommand: 'start-relay', httpWebPlaylists: new Set(), polls: 0,
        ensurePlaybackBridge: async () => {}, bootstrapLocalBridge: async () => {},
        isPlaybackConnected: () => true, schedulePlaybackHttpPoll() { this.polls++; },
        sendJson: message => requests.push(plain(message)),
        requestPlayback: async message => {
            requests.push(plain(message));
            if (message.type === 'registerWebPlaylist') {
                if (message.bootstrap) {
                    upload = { ...message, items: plain(message.items), nextChunkIndex: 0 };
                    return { ...message, type: 'webPlaylistRegistered', itemCount: 1, selectedIndex: 0,
                        currentItem: message.items[0], revision: 1, metadataPending: true,
                        playlistUrl: `http://127.0.0.1:9002/web-playlist/${message.playlistId}/list.m3u` };
                }
                upload = { ...message, items: [], nextChunkIndex: 0 };
                return { type: 'webPlaylistUploadStarted', playlistId: message.playlistId, sessionId: message.sessionId, nextChunkIndex: 0 };
            }
            if (message.type === 'appendWebPlaylistItems') {
                upload.items.push(...message.items); upload.nextChunkIndex++;
                return { type: 'webPlaylistItemsAppended', playlistId: message.playlistId,
                    nextChunkIndex: upload.nextChunkIndex, itemCount: upload.items.length };
            }
            if (message.type === 'commitWebPlaylist') {
                const selectedIndex = upload.items.findIndex(item => item.itemId === upload.selectedItemId);
                return { type: 'webPlaylistRegistered', playlistId: upload.playlistId, sessionId: upload.sessionId,
                    itemCount: upload.items.length, selectedIndex, currentItem: upload.items[selectedIndex], revision: 2, metadataPending: false,
                    playlistUrl: `http://127.0.0.1:9002/web-playlist/${upload.playlistId}/list.m3u` };
            }
            if (message.type === 'getWebPlaylistItems') {
                const all = bridge.restored?.items || upload?.items || [];
                const items = all.slice(message.offset, message.offset + message.limit);
                return { type: 'webPlaylistItems', playlistId: message.playlistId, items, itemCount: all.length,
                    nextOffset: message.offset + items.length === all.length ? null : message.offset + items.length, revision: 1 };
            }
            if (message.type === 'startPlaybackProgress') return { ...message, type: 'playbackProgressStarted',
                apiVersion: 1, record: { position: message.position, duration: message.duration, updatedAt: message.updatedAt, source: 'mpv' } };
            if (message.type === 'getWebPlaylist') return { type: 'webPlaylistState', playlist: bridge.restored };
            if (message.type === 'failWebPlaylistMetadata') return { ...message, type: 'webPlaylistMetadataFailed' };
            return { type: 'playbackProgressState', record: undefined };
        } };
    const topWindow = {};
    const context = vm.createContext({ console: { ...console, warn: value => warnings.push(value), debug: (...values) => diagnostics.push(values) }, URL, URLSearchParams,
        crypto: webcrypto, Uint8Array, Date: class extends Date { static now() { return now; } }, TextEncoder,
        AbortController, setTimeout, clearTimeout, self: topWindow, top: topWindow, location: { href: url },
        document: { cookie: 'bili_jct=browser-only; SESSDATA=browser-only', querySelectorAll: () => [video], addEventListener() {} },
        window: { addEventListener() {} }, currentParser: { parserKey: 'bilibili', bilibiliVideoData: plain(pages) },
        currentConfig: { global: { playbackProgressSync: sync, bilibiliHistorySync: true,
            parser: { bilibili: { preferredCodec: '12', preferredQuality: '127', preferredSubtitle: 'off' } } } },
        translation: { handoffPlaylistUnavailable: 'playlist fallback', handoffPlaylistUpdateRequired: 'playlist update' },
        showToast: message => warnings.push(message), fetchPlaybackBridgeHealth: async () => ({ ok: true, webPlaylistApiVersion: 1 }),
        bilibiliMediaBridge: bridge, createBridgeRequestId: () => 'list-test',
        isRelayPlayer: player => /^(MPV|MPVNET)$/i.test(player?.name || ''), getBilibiliLiveRoomId: () => undefined,
        getAutoHandoffPageKey: value => { const parsed = new URL(value); return `${parsed.origin}${parsed.pathname}?p=${parsed.searchParams.get('p') || 1}`; },
        GM_getValue: (key, fallback) => structuredClone(storage[key] ?? fallback), GM_setValue: (key, value) => { storage[key] = plain(value); },
        fetchBilibiliLiveJson: async requestUrl => {
            if (requestUrl.includes('history/cursor')) return { code: 0, data: { list: [] } };
            if (requestUrl.includes('/nav')) return { code: 0, data: { isLogin: false } };
            return { code: 0, data: plain(pages) };
        },
        fetchBilibiliVideoPlayInfo: async (aid, cid) => { streamCalls.push([aid, cid]); return { selected: cid }; },
        parseBilibiliDashStreams: (data, codec) => [ { url: `https://v.bilivideo.com/${data.selected}-1080.m4s`,
            audio_url: `https://v.bilivideo.com/${data.selected}-audio.m4s`, quality: 80, title: '1080P', preferred: codec === '12' },
            { url: `https://v.bilivideo.com/${data.selected}-720.m4s`, quality: 64, title: '720P' } ],
        selectBilibiliStreamVariant: (variants, quality) => variants.find(item => item.quality <= Number(quality)) || variants[0],
        PARSER: { BILIBILI: class { async getSubtitle(aid, cid) { subtitles.push([aid, cid]); return `https://www.lckp.top/common/bilibili/jsonToSrt/?url=${cid}`; } } }
    });
    vm.runInContext([
        between('function isBilibiliStreamUrl(', 'function normalizeBilibiliLiveQuality('),
        between('function encodeBilibiliStreamVariants(', 'function buildMpvEncodedScriptOption('),
        between('// BILIBILI_REQUEST_CACHE_HELPERS_START', '// BILIBILI_REQUEST_CACHE_HELPERS_END'),
        between('// BILIBILI_WEB_PLAYLIST_HELPERS_START', '// BILIBILI_WEB_PLAYLIST_HELPERS_END'),
        between('// PLAYBACK_PROGRESS_HELPERS_START', '// PLAYBACK_PROGRESS_HELPERS_END'),
        'globalThis.playlists = bilibiliWebPlaylists; globalThis.sync = playbackProgressSync;'
    ].join('\n'), context);
    context.fetchPlaybackBridgeHealth = async () => ({ ok: true, webPlaylistApiVersion: 1,
        webPlaylistHierarchyApiVersion: 1, webPlaylistChunkApiVersion: 1, ...(fastStart ? { webPlaylistFastStartApiVersion: 1 } : {}) });
    return { context, playlists: context.playlists, sync: context.sync, bridge, storage, requests, streamCalls, subtitles,
        posts, warnings, diagnostics, video, advance: ms => { now += ms; } };
}
async function drain() { for (let index = 0; index < 24; index++) await Promise.resolve(); }
function requestFor(itemId = '11:21', generation = 1, first = false) {
    return { type: 'resolveWebPlaylistItem', playlistId, itemId, generation, first };
}

test('the full collection contains every video and current parts instead of selecting only the current two parts', () => {
    const f = fixture();
    const data = { ...pages, ugc_season: { id: 7, title: 'Full album', sections: [{ id: 8, title: 'Tracks', episodes: [
        { aid: 11, bvid: 'BVtest', title: 'Album', arc: { videos: 2 } },
        { aid: 99, bvid: 'BVother', cid: 999, title: 'Other', arc: { videos: 1 } }
    ] }] } };
    const found = f.context.discoverBilibiliWebPlaylist(data, 'https://www.bilibili.com/video/BVtest/?p=2', 22);
    assert.deepEqual(plain(found.items).map(item => item.title), ['Album / MISMATCH', 'Album / Movie Star', 'Other']);
    assert.equal(found.collectionTitle, 'Full album'); assert.equal(found.items[0].partCount, 2);
    assert.equal(found.items[2].partCount, 1); assert.equal(found.items[2].videoTitle, 'Other');
    assert.equal(found.selectedIndex, 1); assert.equal(found.selectedItemId, '11:22');
    assert.equal(found.items[1].pageUrl, 'https://www.bilibili.com/video/BVtest?p=2');
});

test('UGC collections preserve section order, expand explicit parts and deduplicate CID identities', () => {
    const f = fixture();
    const data = { ...pages, pages: [pages.pages[0]], ugc_season: { sections: [
        { episodes: [{ aid: 11, bvid: 'BVtest', cid: 21, title: 'First', page: { cid: 21, duration: 194 } }] },
        { episodes: [{ aid: 12, bvid: 'BVnext', title: 'Second', pages: [
            { cid: 31, part: 'A', duration: 20 }, { cid: 32, part: 'B', duration: 30 } ] },
            { aid: 11, bvid: 'BVtest', cid: 21, title: 'Duplicate' },
            { aid: 13, bvid: 'https://evil.test', cid: 33, title: 'Invalid' }] }
    ] } };
    const found = f.context.discoverBilibiliWebPlaylist(data, f.context.location.href, 21);
    assert.deepEqual(plain(found.items).map(item => item.itemId), ['11:21', '12:31', '12:32']);
    assert.deepEqual(plain(found.items).map(item => item.title), ['First', 'Second / A', 'Second / B']);
});

function collectionData(count = 8) {
    return { ...plain(pages), ugc_season: { id: 7, title: 'Complete collection', ep_count: count,
        sections: [{ id: 8, title: 'Videos', episodes: Array.from({ length: count }, (_, index) => ({
            aid: index === 0 ? 11 : 100 + index, bvid: index === 0 ? 'BVtest' : `BVother${index}`,
            title: `Video ${index}`, arc: { videos: index === 1 ? 1 : 2 },
            page: { cid: index === 0 ? 21 : 1000 + index, page: 1, part: 'Preview', duration: 10 } })) }] } };
}

test('metadata used by parser, resume recovery and playlist recovery shares one request despite search tracking parameters', async () => {
    const f = fixture(); let calls = 0;
    f.context.fetchBilibiliLiveJson = async () => { calls++; return { code: 0, data: plain(pages) }; };
    const [data, recovered, another] = await Promise.all([
        f.context.fetchBilibiliVideoData('BVtest'),
        f.context.resolvePlaybackContent({}, 'https://www.bilibili.com/video/BVtest/?p=2&spm_id_from=search'),
        f.context.fetchBilibiliVideoData('BVtest')
    ]);
    assert.equal(calls, 1); assert.equal(data, another); assert.equal(recovered.identity, 'bilibili:11:22');
    f.advance(60001); await f.context.fetchBilibiliVideoData('BVtest'); assert.equal(calls, 2);
});

test('page entry warms only the selected view; handoff intent starts complete discovery without duplicate view or detail requests', async () => {
    const f = fixture(), data = collectionData(3), calls = [];
    let release;
    f.context.currentParser.bilibiliVideoData = undefined;
    f.context.fetchBilibiliLiveJson = async url => {
        const parsed = new URL(url), id = parsed.searchParams.get('bvid') || `av${parsed.searchParams.get('aid')}`; calls.push(id);
        if (id === 'BVtest') return { code: 0, data };
        assert.equal(parsed.pathname, '/x/player/pagelist');
        return new Promise(resolve => { release = () => resolve({ code: 0, data: [
            { cid: 1002, page: 1, part: 'First' }, { cid: 2002, page: 2, part: 'Second' } ] }); });
    };
    f.context.preloadBilibiliPlaylistPage(f.context.location.href);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, ['BVtest']);
    const launching = f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, ['BVtest', 'av102']); release();
    const launched = await launching;
    assert.equal(launched.webPlaylist.items.length, 5);
    assert.deepEqual(plain(launched.webPlaylist.items).map(item => item.itemId), ['11:21', '11:22', '101:1001', '102:1002', '102:2002']);
    assert.deepEqual(calls, ['BVtest', 'av102']);
    const warmedLaunch = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    assert.equal(warmedLaunch.webPlaylist.items.length, 5); assert.equal(warmedLaunch.webPlaylist.metadataPending, undefined);
    assert.deepEqual(calls, ['BVtest', 'av102']);
});

test('view API errors, mismatched identities and missing selected P never enter the metadata cache', async () => {
    const f = fixture(); let calls = 0;
    f.context.fetchBilibiliLiveJson = async () => { calls++; return calls === 1 ? { code: -404 } : { code: 0, data: plain(pages) }; };
    await assert.rejects(f.context.fetchBilibiliVideoData('BVtest'), /metadata unavailable/);
    assert.equal((await f.context.fetchBilibiliVideoData('BVtest')).bvid, 'BVtest'); assert.equal(calls, 2);
    await assert.rejects(f.context.fetchBilibiliVideoData('BVother'), /metadata unavailable/);
    assert.equal(await f.context.resolvePlaybackContent({}, 'https://www.bilibili.com/video/BVtest/?p=999'), undefined);
});

function blockedLargeCollection(f, count = 325) {
    const data = collectionData(count);
    for (const episode of data.ugc_season.sections[0].episodes) delete episode.arc.videos;
    const full = plain(data);
    for (const [index, episode] of full.ugc_season.sections[0].episodes.entries()) {
        episode._playlistPages = index === 0 ? plain(pages.pages) : [
            { cid: 1000 + index, page: 1, part: 'First' }, { cid: 2000 + index, page: 2, part: 'Second' } ];
    }
    let resolve, reject;
    f.context.currentParser.bilibiliVideoData = data;
    f.context.completeBilibiliPlaylistData = () => new Promise((yes, no) => { resolve = yes; reject = no; });
    return { release: () => resolve(full), fail: () => reject(new Error('Private URL secret must not reach errors')), full };
}

test('a 325-video collection starts the selected CID before any detail completes, then uploads every P in the same session after dispatch', async () => {
    const f = fixture({ fastStart: true }), collection = blockedLargeCollection(f);
    let launch;
    const preparing = f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} }).then(value => { launch = value; });
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(launch, 'playable bootstrap must finish while all 325 details remain pending');
    await preparing;
    const bootstrap = f.requests.find(request => request.bootstrap);
    assert.equal(bootstrap.items.length, 1); assert.equal(bootstrap.items[0].itemId, '11:21');
    assert.equal(launch.webPlaylist.metadataPending, true);
    assert.equal(f.requests.filter(request => request.extendExisting).length, 0);
    const state = f.playlists.playlists.get(launch.webPlaylist.playlistId);
    f.playlists.handleMessage({ type: 'resolveWebPlaylistItem', playlistId: state.playlistId, sessionId: state.sessionId,
        itemId: '11:21', generation: 1, first: true });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.requests.find(request => request.type === 'submitWebPlaylistItem').media.cid, '21');
    const progressLaunch = await f.sync.prepareLaunch(launch, { name: 'MPV' });
    assert.equal(progressLaunch.webPlaylist.sessionId, state.sessionId);
    assert.equal(f.requests.find(request => request.type === 'startPlaybackProgress').playlistId, state.playlistId);
    f.playlists.onLaunchDispatched(progressLaunch); f.playlists.onLaunchDispatched(progressLaunch);
    assert.equal(f.requests.filter(request => request.extendExisting).length, 0);
    collection.release(); await state.metadataTask;
    const extending = f.requests.filter(request => request.extendExisting);
    assert.equal(extending.length, 1); assert.equal(extending[0].playlistId, state.playlistId);
    assert.equal(extending[0].sessionId, state.sessionId); assert.equal(extending[0].selectedItemId, '11:21');
    const uploaded = f.requests.filter(request => request.type === 'appendWebPlaylistItems').flatMap(request => request.items);
    assert.equal(uploaded.length, 650); assert.equal(new Set(uploaded.map(item => item.itemId)).size, 650);
    assert.equal(state.itemMap.size, 650); assert.equal(state.completeManifest, true);
    assert.equal(state.metadataPending, false); assert.equal(f.requests.some(request => request.type === 'stopWebPlaylist'), false);
    assert.ok(!JSON.stringify(uploaded).includes('first.m4s'));
});

test('metadata failure reports a fixed error while the already registered CID and progress session remain usable', async () => {
    const f = fixture({ fastStart: true }), collection = blockedLargeCollection(f);
    const launch = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    const state = f.playlists.playlists.get(launch.webPlaylist.playlistId);
    f.playlists.onLaunchDispatched(launch); collection.fail(); await state.metadataTask;
    const failure = f.requests.find(request => request.type === 'failWebPlaylistMetadata');
    assert.equal(failure.playlistId, state.playlistId); assert.equal(failure.sessionId, state.sessionId);
    assert.equal(failure.error, 'playlist_metadata_failed');
    assert.equal(f.playlists.playlists.get(state.playlistId), state); assert.equal(state.itemMap.size, 1);
    assert.equal(f.requests.some(request => request.type === 'stopWebPlaylist'), false);
    assert.ok(!JSON.stringify(f.warnings).includes('Private URL'));
    f.playlists.handleMessage({ type: 'resolveWebPlaylistItem', playlistId: state.playlistId, itemId: '11:21', generation: 2 });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.requests.findLast(request => request.type === 'submitWebPlaylistItem').media.cid, '21');
});

test('failed extension chunks and SPA navigation stop further commits without stopping the first video', async () => {
    for (const navigation of [false, true]) {
        const f = fixture({ fastStart: true }), collection = blockedLargeCollection(f);
        const launch = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
        const state = f.playlists.playlists.get(launch.webPlaylist.playlistId), original = f.bridge.requestPlayback;
        f.bridge.requestPlayback = async request => {
            if (request.type === 'appendWebPlaylistItems') {
                if (!navigation) throw new Error('network failed with a private URL');
                f.context.location.href = 'https://www.bilibili.com/video/BVnext/';
            }
            return original(request);
        };
        f.playlists.onLaunchDispatched(launch); collection.release(); await state.metadataTask;
        assert.equal(f.requests.filter(request => request.type === 'commitWebPlaylist').length, 0);
        assert.equal(f.requests.filter(request => request.type === 'failWebPlaylistMetadata').length, 1);
        assert.equal(f.requests.some(request => request.type === 'stopWebPlaylist'), false);
        assert.equal(f.playlists.playlists.get(state.playlistId), state);
        assert.equal(state.itemMap.size, 1);
    }
});

test('a supported old bridge completes every entry before handoff rather than receiving a partial bootstrap', async () => {
    const f = fixture(), collection = blockedLargeCollection(f);
    let launch;
    const preparing = f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} }).then(value => { launch = value; });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(launch, undefined); assert.equal(f.requests.some(request => request.bootstrap), false);
    collection.release(); await preparing;
    assert.equal(launch.webPlaylist.items.length, 650); assert.equal(launch.webPlaylist.metadataPending, undefined);
    assert.equal(f.requests.find(request => request.type === 'commitWebPlaylist').expectedItemCount, 650);
});

test('a lost extension commit reply recovers its committed owner instead of aborting native list delivery', async () => {
    const f = fixture({ fastStart: true }), collection = blockedLargeCollection(f);
    const launch = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    const state = f.playlists.playlists.get(launch.webPlaylist.playlistId), original = f.bridge.requestPlayback;
    f.bridge.requestPlayback = async request => {
        const reply = await original(request);
        if (request.type === 'commitWebPlaylist') {
            f.bridge.restored = { ...reply, metadataPending: true, metadataStage: 'extending' };
            throw Object.assign(new Error('Lost HTTP reply'), { code: 'playback_request_timeout' });
        }
        return reply;
    };
    f.playlists.onLaunchDispatched(launch); collection.release(); await state.metadataTask;
    assert.equal(state.itemMap.size, 650); assert.equal(state.completeManifest, true);
    assert.equal(state.metadataStage, 'extending'); assert.equal(state.pendingMetadata, undefined);
    assert.equal(f.requests.filter(request => request.type === 'getWebPlaylist').length, 1);
    assert.equal(f.requests.some(request => request.type === 'failWebPlaylistMetadata'), false);
    assert.equal(f.requests.some(request => request.type === 'stopWebPlaylist'), false);
});

test('refresh restores and completes a pending bootstrap without creating another playlist or replaying the first CID', async () => {
    const f = fixture({ fastStart: true }), collection = blockedLargeCollection(f);
    const initialItem = plain(manifest().items[0]);
    f.bridge.restored = { playlistId, sessionId, selectedIndex: 0, currentItemId: initialItem.itemId,
        currentItem: initialItem, itemCount: 1, metadataPending: true, metadataStage: 'pending', revision: 1,
        playlistUrl: `http://127.0.0.1:9002/web-playlist/${playlistId}/list.m3u` };
    await f.playlists.restore();
    const state = f.playlists.playlists.get(playlistId);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(state.pendingMetadata.started, true); assert.equal(state.sessionId, sessionId);
    assert.equal(f.requests.some(request => request.bootstrap), false);
    collection.release(); await state.metadataTask;
    assert.equal(state.itemMap.size, 650); assert.equal(f.requests.find(request => request.extendExisting).playlistId, playlistId);
    assert.equal(f.requests.some(request => request.type === 'startPlaybackProgress'), false);
    assert.equal(f.requests.some(request => request.type === 'stopWebPlaylist'), false);
});

test('metadata lifecycle events accept only the current session and revision and show a single fixed failure notice', () => {
    const f = fixture(), state = f.playlists.adopt({ ...manifest(), revision: 10, metadataPending: true });
    state.pendingMetadata = { isCurrent: () => true };
    f.playlists.handleMessage({ ...manifest(), type: 'webPlaylistMetadataReady', revision: 9, metadataPending: false });
    assert.equal(state.metadataPending, true); assert.equal(state.revision, 10);
    f.playlists.handleMessage({ ...manifest(), type: 'webPlaylistMetadataReady', revision: 11,
        sessionId: 'progress-foreign', metadataPending: false });
    assert.equal(state.metadataPending, true); assert.equal(state.revision, 10);
    f.playlists.handleMessage({ ...manifest(), type: 'webPlaylistMetadataFailed', revision: 10,
        metadataPending: true, metadataStage: 'failed', metadataError: 'playlist_metadata_ipc_failed' });
    f.playlists.handleMessage({ ...manifest(), type: 'webPlaylistMetadataFailed', revision: 10,
        metadataPending: true, metadataStage: 'failed', metadataError: 'playlist_metadata_ipc_failed' });
    assert.equal(state.metadataError, 'playlist_metadata_ipc_failed');
    assert.equal(f.warnings.filter(message => message === 'playlist fallback').length, 1);
    f.playlists.handleMessage({ ...manifest(), type: 'webPlaylistMetadataReady', revision: 11,
        metadataPending: false, metadataStage: 'ready' });
    assert.equal(state.metadataPending, false); assert.equal(state.metadataStage, 'ready');
});

test('full metadata discovery fetches missing multi-parts with at most three requests, retaining section/video/part order', async () => {
    const f = fixture(), data = collectionData();
    let active = 0, peak = 0;
    const requested = [];
    f.context.fetchBilibiliLiveJson = async url => {
        requested.push(url); active++; peak = Math.max(peak, active);
        await new Promise(resolve => setTimeout(resolve, 1)); active--;
        assert.equal(new URL(url).pathname, '/x/player/pagelist');
        const index = Number(new URL(url).searchParams.get('aid')) - 100;
        return { code: 0, data: [
            { cid: 1000 + index, page: 1, part: 'A', duration: 10 },
            { cid: 2000 + index, page: 2, part: 'B', duration: 20 } ] };
    };
    const completed = await f.context.completeBilibiliPlaylistData(data, f.context.location.href);
    const found = f.context.discoverBilibiliWebPlaylist(completed, f.context.location.href, 22);
    assert.equal(peak, 3); assert.equal(requested.length, 6); // Current and known single-P avoid extra API calls.
    assert.equal(found.items.length, 15); assert.equal(found.selectedIndex, 1);
    assert.deepEqual(plain(found.items).slice(0, 5).map(item => [item.videoTitle, item.partTitle, item.partCount]), [
        ['Video 0', 'MISMATCH', 2], ['Video 0', 'Movie Star', 2], ['Video 1', 'Preview', 1],
        ['Video 2', 'A', 2], ['Video 2', 'B', 2] ]);
    assert.equal(found.items[3].sectionId, '8'); assert.equal(data.ugc_season.sections[0].episodes[2]._playlistPages, undefined);
});

test('unknown part counts request the complete lightweight page array and repeated videos share one bounded API request', async () => {
    const f = fixture(), data = collectionData(3);
    const repeated = data.ugc_season.sections[0].episodes[2];
    delete repeated.arc.videos; repeated.pages = [repeated.page];
    data.ugc_season.sections[0].episodes.push({ ...repeated }); data.ugc_season.ep_count = 4;
    let requests = 0;
    f.context.fetchBilibiliLiveJson = async url => { requests++; assert.equal(new URL(url).pathname, '/x/player/pagelist');
        return { code: 0, data: [ { cid: 1002, page: 1, part: 'First' }, { cid: 2002, page: 2, part: 'Hidden second' } ] }; };
    const completed = await f.context.completeBilibiliPlaylistData(data, f.context.location.href);
    const found = f.context.discoverBilibiliWebPlaylist(completed, f.context.location.href, 21);
    assert.equal(requests, 1); assert.equal(found.items.length, 5);
    assert.ok(found.items.some(item => item.partTitle === 'Hidden second'));
});

test('metadata errors and explicit incomplete seasons stop discovery rather than registering a silently truncated collection', async () => {
    const f = fixture(), incomplete = collectionData(3); incomplete.ugc_season.ep_count = 4;
    await assert.rejects(f.context.completeBilibiliPlaylistData(incomplete, f.context.location.href), /Incomplete Bilibili collection/);
    const data = collectionData(20); let calls = 0;
    f.context.fetchBilibiliLiveJson = async () => { calls++; return { code: -404 }; };
    await assert.rejects(f.context.completeBilibiliPlaylistData(data, f.context.location.href), /metadata unavailable/);
    assert.ok(calls <= 3);
    await assert.rejects(f.context.completeBilibiliPlaylistData(data, f.context.location.href, () => false), /page changed/);
});

test('background details retry one transport timeout while risk control and API failures fail once', async () => {
    for (const failure of [{ bilibiliRequestCause: 'request_timeout' },
        { bilibiliRequestCause: 'request_http', httpStatus: 503 }]) {
        const f = fixture(); let calls = 0;
        f.context.setTimeout = callback => { queueMicrotask(callback); return 1; };
        f.context.fetchBilibiliLiveJson = async () => {
            if (++calls === 1) throw Object.assign(new Error('private signed URL must not appear'), failure);
            return { code: 0, data: plain(pages.pages) };
        };
        assert.equal((await f.context.fetchBilibiliPartData(11, 'BVtest')).aid, '11');
        assert.equal(calls, 2);
        assert.ok(!JSON.stringify(f.diagnostics).includes('private signed URL'));
    }
    for (const failure of [{ bilibiliRequestCause: 'request_http', httpStatus: 412 }, { apiCode: -352 }, { apiCode: -404 }]) {
        const f = fixture(); let calls = 0;
        f.context.fetchBilibiliLiveJson = async () => {
            calls++;
            if (failure.apiCode) return { code: failure.apiCode };
            throw Object.assign(new Error('private exception'), failure);
        };
        await assert.rejects(f.context.fetchBilibiliPartData(11, 'BVtest'));
        assert.equal(calls, 1);
    }
});

test('collection failure identifies public video/API code and request counts without exception text or cookies', async () => {
    const f = fixture(), data = collectionData(20);
    f.context.fetchBilibiliLiveJson = async () => ({ code: -352, message: 'secret cookie and signed URL' });
    await assert.rejects(f.context.completeBilibiliPlaylistData(data, f.context.location.href));
    const record = f.diagnostics.filter(([name]) => name === 'External Player: playlist_metadata_diagnostic')
        .map(([, value]) => JSON.parse(value)).find(value => value.phase === 'collection-failed');
    assert.equal(record.cause, 'api_rejected');
    assert.equal(record.apiCode, -352);
    assert.match(record.aid, /^[1-9]\d*$/);
    assert.equal(record.listedVideos, 20);
    assert.ok(record.requestedVideos > 0 && record.requestedVideos <= 3);
    assert.equal(record.completedVideos, 0);
    assert.ok(!JSON.stringify(f.diagnostics).includes('secret'));
    assert.ok(!JSON.stringify(f.diagnostics).includes('signed URL'));
});

test('navigation cancellation and incomplete parts retain different fixed diagnostic causes', async () => {
    const f = fixture();
    await assert.rejects(f.context.completeBilibiliPlaylistData(collectionData(3), f.context.location.href, () => false),
        error => error.metadataCause === 'page_changed');
    f.context.fetchBilibiliLiveJson = async () => ({ code: 0, data: [{ cid: 1002, page: 1, part: 'Only first' }] });
    await assert.rejects(f.context.completeBilibiliPlaylistData(collectionData(3), f.context.location.href),
        error => error.metadataCause === 'parts_incomplete');
    const causes = f.diagnostics.filter(([name]) => name === 'External Player: playlist_metadata_diagnostic')
        .map(([, value]) => JSON.parse(value).cause);
    assert.deepEqual(causes, ['page_changed', 'parts_incomplete']);
});

test('pagelist rejects missing P, repeated CID and mismatched known CID without caching a truncated result', async () => {
    for (const [returned, known, count, cause] of [
        [[{ cid: 21, page: 1 }], [{ cid: 21, page: 1 }], 2, 'parts_incomplete'],
        [[{ cid: 21, page: 1 }, { cid: 22, page: 3 }], [], 0, 'parts_incomplete'],
        [[{ cid: 21, page: 1 }, { cid: 21, page: 2 }], [], 0, 'parts_incomplete'],
        [[{ cid: 999, page: 1 }], [{ cid: 21, page: 1 }], 0, 'part_identity_mismatch']
    ]) {
        const f = fixture(); let calls = 0;
        f.context.fetchBilibiliLiveJson = async () => { calls++; return { code: 0, data: returned }; };
        for (let attempt = 0; attempt < 2; attempt++) {
            await assert.rejects(f.context.fetchBilibiliPartData(11, 'BVtest', known, count), error => error.metadataCause === cause);
        }
        assert.equal(calls, 2);
    }
});

test('cached pagelist validates each caller known CID and retains all 1001 P without video-view requests', async () => {
    const f = fixture(); let calls = 0;
    const returned = Array.from({ length: 1001 }, (_, index) => ({ cid: 21 + index, page: index + 1, part: `P${index + 1}` }));
    f.context.fetchBilibiliLiveJson = async url => {
        calls++;
        assert.equal(new URL(url).pathname, '/x/player/pagelist');
        return { code: 0, data: returned };
    };
    const [first, duplicate] = await Promise.all([
        f.context.fetchBilibiliPartData(11, 'BVtest', [{ cid: 21, page: 1 }], 1001),
        f.context.fetchBilibiliPartData(11, 'BVtest', [{ cid: 22, page: 2 }], 1001)
    ]);
    assert.equal(calls, 1);
    assert.equal(first, duplicate);
    assert.equal(first.pages.length, 1001);
    await assert.rejects(f.context.fetchBilibiliPartData(11, 'BVtest', [{ cid: 999999, page: 1 }]),
        error => error.metadataCause === 'part_identity_mismatch');
    assert.equal(calls, 1);
    f.advance(60001);
    await f.context.fetchBilibiliPartData(11, 'BVtest');
    assert.equal(calls, 2);
});

test('only public collection schema is logged without account details or media URLs', () => {
    const f = fixture(), data = collectionData(3);
    data.cookie = 'secret-cookie'; data.ugc_season.sections[0].episodes[0].video = 'https://private.test/?signature=secret';
    f.context.debugBilibiliPlaylistData(data);
    const logged = JSON.stringify(f.diagnostics);
    assert.match(logged, /archiveVideos/); assert.match(logged, /listedVideos/);
    assert.ok(!logged.includes('secret-cookie')); assert.ok(!logged.includes('signature=secret')); assert.ok(!logged.includes('private.test'));
});

test('handoff phase diagnostics expose only public identifiers, counts and elapsed time', () => {
    const f = fixture(), started = f.context.bilibiliTimingNow(); f.advance(125);
    f.context.traceBilibiliHandoff('protocol-dispatched', started, { bvid: 'BVtest', itemCount: 1, videoCount: 325,
        video: 'https://private.test/video.m4s?signature=secret', cookie: 'account-secret', mid: 12345, token: 'private-token' });
    f.context.traceBilibiliHandoff('metadata-ready', started, { bvid: 'https://private.test', itemCount: Infinity });
    const messages = f.diagnostics.map(([, text]) => JSON.parse(text));
    assert.deepEqual(messages, [ { phase: 'protocol-dispatched', ms: 125, bvid: 'BVtest', itemCount: 1, videoCount: 325 },
        { phase: 'metadata-ready', ms: 125 } ]);
    assert.ok(!JSON.stringify(f.diagnostics).includes('secret')); assert.ok(!JSON.stringify(f.diagnostics).includes('private'));
});

test('single videos, recommendations and absent current entries do not create playlists, while more than 500 parts remain complete', () => {
    const f = fixture(), url = f.context.location.href;
    assert.equal(f.context.discoverBilibiliWebPlaylist({ ...pages, pages: [pages.pages[0]], related: pages.pages }, url, 21), undefined);
    assert.equal(f.context.discoverBilibiliWebPlaylist(pages, url, 999), undefined);
    assert.equal(f.context.discoverBilibiliWebPlaylist({ ...pages, pages: Array.from({ length: 501 }, (_, index) =>
        ({ page: index + 1, cid: index + 21, duration: 1 })) }, url, 21).items.length, 501);
    for (const route of ['https://live.bilibili.com/1017', 'https://www.bilibili.com/bangumi/play/ep1', 'https://www.bilibili.com/list/1', 'https://evil.test/video/BVtest']) {
        assert.equal(f.context.discoverBilibiliWebPlaylist(pages, route, 21), undefined);
    }
});

test('playlist titles respect backend UTF-8 byte limits without splitting emoji and preserve large numeric identifiers', () => {
    const f = fixture();
    const item = f.context.bilibiliPlaylistItem('9007199254740993', 'BVtest', { cid: '9007199254740995',
        part: '💬中文'.repeat(100), duration: 20 }, 'Fallback');
    assert.equal(item.aid, '9007199254740993'); assert.equal(item.cid, '9007199254740995');
    assert.ok(new TextEncoder().encode(item.title).length <= 360);
    assert.ok(!item.title.includes('\ufffd'));
    assert.equal(f.context.bilibiliPlaylistItem(11, 'BVtest', { cid: 21, page: 1000000 }, 'Too large'), undefined);
});

test('long signed quality URLs retain the selected source within the bounded per-item IPC payload', () => {
    const f = fixture();
    const variants = Array.from({ length: 12 }, (_, index) => ({
        url: `https://v.bilivideo.com/${index}.m4s?sign=${'a'.repeat(3000)}`,
        audio_url: `https://v.bilivideo.com/${index}-audio.m4s?sign=${'b'.repeat(2000)}`,
        quality: 127 - index, title: '中文画质'.repeat(200), codec: 'avc1'.repeat(100) }));
    const bounded = f.context.bilibiliPlaylistVariants(variants, variants[0]);
    assert.ok(bounded.some(item => item.url === variants[0].url));
    assert.ok(new TextEncoder().encode(JSON.stringify(bounded)).length < 20480);
    assert.ok(bounded.every(item => new TextEncoder().encode(item.title).length <= 360 && item.codec.length <= 64));
});

test('registration sends only bounded content metadata and launches a titled standard playlist without global track options', async () => {
    const f = fixture();
    const prepared = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    const registration = f.requests.find(request => request.type === 'registerWebPlaylist');
    assert.match(registration.playlistId, /^playlist-[a-f0-9]{32}$/);
    assert.equal(registration.initialPosition, 70); assert.equal(registration.chunked, true);
    assert.equal(registration.items, undefined);
    assert.equal(f.requests.find(request => request.type === 'appendWebPlaylistItems').items.length, 2);
    assert.ok(!JSON.stringify(registration).includes('browser-only'));
    assert.ok(!JSON.stringify(registration).includes('.m4s'));
    assert.equal(prepared.audio, undefined); assert.equal(prepared.subtitle, undefined); assert.equal(prepared.title, undefined);
    assert.equal(prepared.bilibili.cid, undefined);
    assert.match(prepared.video, /^http:\/\/127\.0\.0\.1:9002\/web-playlist\/playlist-[a-f0-9]{32}\/list\.m3u$/);
    assert.equal(prepared.mpvOptions.filter(option => option.includes('input-ipc-server')).length, 1);
    assert.ok(prepared.mpvOptions.includes('--playlist-start=0'));
    assert.ok(prepared.mpvOptions.includes('--fullscreen=yes'));
    assert.ok(!prepared.mpvOptions.some(option => /quality-menu|^--start|^--audio-file|cid=/.test(option)));
});

test('more than 500 entries upload every item in ordered byte-bounded chunks before committing the playlist', async () => {
    const f = fixture();
    f.context.currentParser.bilibiliVideoData = { ...plain(pages), pages: Array.from({ length: 701 }, (_, index) => ({
        cid: 21 + index, page: index + 1, part: '中文💬'.repeat(100), duration: 5 })) };
    const prepared = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    const chunks = f.requests.filter(request => request.type === 'appendWebPlaylistItems');
    assert.ok(prepared.webPlaylist); assert.equal(chunks.flatMap(chunk => chunk.items).length, 701);
    chunks.forEach((chunk, index) => {
        assert.equal(chunk.chunkIndex, index); assert.ok(chunk.items.length <= 100);
        assert.ok(new TextEncoder().encode(JSON.stringify(chunk.items)).length <= 48 * 1024);
    });
    assert.equal(f.requests.find(request => request.type === 'commitWebPlaylist').expectedItemCount, 701);
    const state = f.playlists.playlists.get(prepared.webPlaylist.playlistId);
    assert.equal(state.itemMap.size, 701); assert.equal(state.completeManifest, true);
});

test('interrupted chunk uploads cancel their staging record and preserve the selected single-video fallback', async () => {
    const f = fixture(), normal = f.bridge.requestPlayback;
    f.bridge.requestPlayback = async request => {
        if (request.type === 'appendWebPlaylistItems') throw new Error('Upload interrupted');
        return normal(request);
    };
    const original = media();
    assert.equal(await f.context.prepareBilibiliPlaylistLaunch(original, { name: 'MPV' }), original);
    assert.equal(f.requests.at(-1).type, 'stopWebPlaylist');
    assert.ok(!f.requests.some(request => request.type === 'commitWebPlaylist'));
});

test('a summary restores its complete large manifest through sequential bounded metadata pages', async () => {
    const f = fixture(), all = Array.from({ length: 601 }, (_, index) => ({ ...manifest().items[0],
        cid: 21 + index, itemId: `11:${21 + index}`, p: index + 1, order: index,
        pageUrl: `https://www.bilibili.com/video/BVtest?p=${index + 1}` }));
    f.bridge.restored = { ...manifest(), items: all };
    const state = f.playlists.adopt({ playlistId, sessionId, itemCount: all.length, currentItem: all[0], revision: 1 });
    for (let index = 0; index < 8; index++) await drain();
    assert.equal(state.itemMap.size, 601); assert.equal(state.items.length, 601); assert.equal(state.completeManifest, true);
    const requests = f.requests.filter(request => request.type === 'getWebPlaylistItems');
    assert.deepEqual(requests.map(request => request.offset), [0, 100, 200, 300, 400, 500, 600]);
    assert.ok(requests.every(request => request.limit === 100));
});

test('trusted resolve metadata can start the next item before the paginated manifest has finished loading', async () => {
    const f = fixture();
    const original = f.bridge.requestPlayback;
    f.bridge.requestPlayback = request => request.type === 'getWebPlaylistItems' ? new Promise(() => {}) : original(request);
    const items = manifest().items;
    f.playlists.adopt({ playlistId, sessionId, itemCount: 2, currentItem: items[0], revision: 1 });
    f.playlists.handleMessage({ ...requestFor('11:22'), sessionId, item: items[1] }); await drain();
    assert.equal(f.requests.at(-1).media.identity, 'bilibili:11:22');
    assert.equal(f.playlists.playlists.get(playlistId).itemMap.size, 2);
});

test('metadata pagination rejects non-advancing cursors and changed revisions instead of looping or accepting a mixed manifest', async () => {
    for (const invalid of ['cursor', 'revision']) {
        const f = fixture(), all = manifest().items;
        let calls = 0;
        f.bridge.requestPlayback = async request => { calls++; return { type: 'webPlaylistItems', playlistId, itemCount: 2,
            revision: invalid === 'revision' ? 2 : 1, items: all, nextOffset: invalid === 'cursor' ? 0 : null }; };
        const state = f.playlists.adopt({ playlistId, sessionId, itemCount: 2, currentItem: all[0], revision: 1 });
        await drain();
        assert.equal(calls, 1); assert.equal(state.completeManifest, false); assert.equal(state.hydrationFailed, true);
    }
});

test('metadata hydrate and trusted resolve from a foreign session cannot populate a displaced owner manifest', async () => {
    const f = fixture(); let release;
    f.bridge.requestPlayback = request => request.type === 'getWebPlaylistItems' ? new Promise(resolve => { release = resolve; }) : Promise.resolve({});
    const all = manifest().items;
    const state = f.playlists.adopt({ playlistId, sessionId, itemCount: 2, currentItem: all[0], revision: 1 });
    f.playlists.handleMessage({ ...requestFor('11:22'), sessionId: 'progress-foreign', item: all[1] });
    assert.equal(state.itemMap.size, 1);
    f.playlists.handleMessage({ type: 'webPlaylistStopped', playlistId, reason: 'browser_rebound' });
    release({ type: 'webPlaylistItems', playlistId, itemCount: 2, revision: 1, items: all, nextOffset: null }); await drain();
    assert.equal(state.itemMap.size, 1); assert.equal(f.streamCalls.length, 0);
});

test('unsupported players and single-item videos keep the original source; old bridges provide a visible single-video fallback', async () => {
    const f = fixture();
    const original = media();
    assert.equal(await f.context.prepareBilibiliPlaylistLaunch(original, { name: 'VLC' }), original);
    f.context.currentParser.bilibiliVideoData.pages = [pages.pages[0]];
    assert.equal(await f.context.prepareBilibiliPlaylistLaunch(original, { name: 'MPV' }), original);
    f.context.currentParser.bilibiliVideoData = plain(pages);
    f.context.fetchPlaybackBridgeHealth = async () => ({ ok: true });
    assert.equal(await f.context.prepareBilibiliPlaylistLaunch(original, { name: 'MPV' }), original);
    assert.ok(f.warnings.includes('playlist update'));
});

test('malformed registration URLs cannot redirect MPV and cancel their registered playlist', async () => {
    const f = fixture();
    f.bridge.requestPlayback = async request => ({ ...request, type: 'webPlaylistRegistered', selectedIndex: 0, playlistUrl: 'https://evil.test/private' });
    const original = media();
    assert.equal(await f.context.prepareBilibiliPlaylistLaunch(original, { name: 'MPV' }), original);
    assert.equal(f.requests.at(-1).type, 'stopWebPlaylist');
    assert.equal(f.playlists.playlists.size, 0);
});

test('media is resolved only on selection, with actual authorized quality, separate audio and matching subtitle CID', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    assert.equal(f.streamCalls.length, 0);
    f.context.currentConfig.global.parser.bilibili.preferredSubtitle = 'zh';
    f.playlists.handleMessage(requestFor('11:22'));
    await drain();
    assert.deepEqual(f.streamCalls, [['11', '22']]); assert.deepEqual(f.subtitles, [['11', '22']]);
    const submitted = f.requests.at(-1);
    assert.equal(submitted.type, 'submitWebPlaylistItem'); assert.equal(submitted.media.identity, 'bilibili:11:22');
    assert.equal(submitted.media.video, 'https://v.bilivideo.com/22-1080.m4s');
    assert.equal(submitted.media.audio, 'https://v.bilivideo.com/22-audio.m4s');
    assert.equal(submitted.media.title, 'Movie Star'); assert.equal(submitted.media.position, 0);
    assert.equal(submitted.media.variants[0].quality, 80);
    assert.ok(!JSON.stringify(submitted).includes('browser-only'));
});

test('a stalled optional subtitle cannot hold a playable item beyond its resolution deadline', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    f.context.currentConfig.global.parser.bilibili.preferredSubtitle = 'zh';
    f.context.PARSER.BILIBILI.prototype.getSubtitle = () => new Promise(() => {});
    let timeout;
    f.context.setTimeout = (callback, delay) => { assert.equal(delay, 3000); timeout = callback; return 1; };
    f.playlists.handleMessage(requestFor('11:22')); await drain();
    assert.equal(f.requests.length, 0); assert.ok(timeout);
    timeout(); await drain();
    assert.equal(f.requests[0].media.video, 'https://v.bilivideo.com/22-1080.m4s');
    assert.equal(f.requests[0].media.subtitle, undefined);
});

test('the newest timestamp including zero wins, unfinished parts resume and completed repeats restart at zero', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    f.context.saveLocalPlaybackRecord('bilibili:11:21', { position: 0, updatedAt: 500, duration: 194 });
    f.playlists.handleMessage({ ...requestFor('11:21', 1, true), resumeRecord: { position: 90, updatedAt: 100, duration: 194 } });
    await drain(); assert.equal(f.requests.at(-1).media.position, 0);
    f.context.saveLocalPlaybackRecord('bilibili:11:21', { position: 90, updatedAt: 600, duration: 194 });
    f.playlists.handleMessage(requestFor('11:21', 2, false));
    await drain(); assert.equal(f.requests.at(-1).media.position, 90);
    f.playlists.handleMessage({ ...requestFor('11:21', 3, false), resumeRecord:
        { position: 194, updatedAt: 700, duration: 194, completed: true } });
    await drain(); assert.equal(f.requests.at(-1).media.position, 0);
});

test('progress opt-out keeps playlist resolution working while ignoring previously remembered positions', async () => {
    const f = fixture({ sync: false }); f.playlists.adopt({ ...manifest(), initialPosition: 0 });
    f.context.saveLocalPlaybackRecord('bilibili:11:21', { position: 90, updatedAt: 700, duration: 194 });
    f.playlists.handleMessage({ ...requestFor('11:21', 1, true), initialPosition: 0,
        resumeRecord: { position: 100, updatedAt: 800, duration: 194 } });
    await drain(); assert.equal(f.requests.at(-1).media.position, 0);
});

test('completed first items restart without losing a remembered non-completed initial position', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    f.playlists.handleMessage(requestFor('11:21', 1, true)); await drain();
    assert.equal(f.requests.at(-1).media.position, 70);
    f.context.saveLocalPlaybackRecord('bilibili:11:21', { position: 194, duration: 194, updatedAt: 500, completed: true });
    f.playlists.handleMessage(requestFor('11:21', 2, true)); await drain();
    assert.equal(f.requests.at(-1).media.position, 0);
});

test('late and duplicate selections, unknown items and detached owners cannot submit stale streams', async () => {
    const f = fixture(), releases = [];
    f.context.fetchBilibiliVideoPlayInfo = async () => new Promise(resolve => releases.push(resolve));
    f.playlists.adopt(manifest());
    f.playlists.handleMessage(requestFor('11:21', 1));
    f.playlists.handleMessage(requestFor('11:21', 1));
    f.playlists.handleMessage(requestFor('11:99', 1));
    f.playlists.handleMessage(requestFor('11:21', 2));
    assert.equal(releases.length, 2);
    releases[1]({ selected: 21 }); await drain();
    assert.equal(f.requests.length, 1); assert.equal(f.requests[0].generation, 2);
    releases[0]({ selected: 21 }); await drain(); assert.equal(f.requests.length, 1);
    f.playlists.handleMessage(requestFor('11:22', 3));
    f.playlists.handleMessage({ type: 'webPlaylistStopped', playlistId });
    releases[2]({ selected: 22 }); await drain(); assert.equal(f.requests.length, 1);
});

test('a failed submission can accept the same pending generation after reconnect instead of timing the entry out', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    let deliveries = 0;
    const normal = f.bridge.requestPlayback;
    f.bridge.requestPlayback = async request => {
        if (request.type === 'submitWebPlaylistItem' && ++deliveries === 1) throw new Error('Owner connection lost');
        return normal(request);
    };
    f.playlists.handleMessage(requestFor('11:22', 7)); await drain();
    assert.equal(f.requests.length, 0);
    f.playlists.adopt(manifest()); // Same-page reconnect preserves the owned manifest object.
    f.playlists.handleMessage(requestFor('11:22', 7)); await drain();
    assert.equal(deliveries, 2); assert.equal(f.requests[0].generation, 7);
    assert.deepEqual(f.streamCalls, [['11', '22'], ['11', '22']]);
    f.playlists.handleMessage(requestFor('11:22', 7)); await drain();
    assert.equal(deliveries, 2); // An acknowledged result remains deduplicated.
});

test('a single lost HTTP submission retries cached media once without requiring a backend resend or resolving again', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    let deliveries = 0;
    const normal = f.bridge.requestPlayback;
    f.bridge.requestPlayback = async request => {
        if (request.type === 'submitWebPlaylistItem' && ++deliveries === 1) {
            throw Object.assign(new Error('Delivery timed out'), { code: 'playback_request_timeout' });
        }
        return normal(request);
    };
    f.playlists.handleMessage(requestFor('11:22', 7)); await drain();
    assert.equal(deliveries, 2); assert.equal(f.requests[0].generation, 7);
    assert.equal(f.requests[0].media.video, 'https://v.bilivideo.com/22-1080.m4s');
    assert.deepEqual(f.streamCalls, [['11', '22']]);
});

test('expired transport owners rebind the exact playlist before retrying its cached pending media', async () => {
    const f = fixture(); f.playlists.adopt(manifest()); f.bridge.restored = manifest();
    const order = [], normal = f.bridge.requestPlayback;
    f.bridge.requestPlayback = async request => {
        order.push(request.type);
        if (order.length === 1) throw Object.assign(new Error('Owner expired'), { code: 'playback_progress_client_expired' });
        return normal(request);
    };
    f.playlists.handleMessage(requestFor('11:22', 7)); await drain();
    assert.deepEqual(order, ['submitWebPlaylistItem', 'getWebPlaylist', 'submitWebPlaylistItem']);
    assert.equal(f.requests[0].playlistId, playlistId); assert.equal(f.requests[0].identity, 'bilibili:11:22');
    assert.equal(f.requests[0].rebind, true); assert.equal(f.requests[1].generation, 7);
    assert.equal(f.streamCalls.length, 1);
});

test('invalid or expired generations do not retry, and repeated transport failures remain bounded', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    let deliveries = 0;
    f.bridge.requestPlayback = async () => {
        deliveries++; throw Object.assign(new Error('Invalid generation'), { code: 'playback_http_invalid_request' });
    };
    f.playlists.handleMessage(requestFor('11:22', 7)); await drain(); assert.equal(deliveries, 1);
    f.bridge.requestPlayback = async () => {
        deliveries++; throw Object.assign(new Error('Network down'), { code: 'playback_http_unavailable' });
    };
    f.playlists.handleMessage(requestFor('11:22', 8)); await drain(); assert.equal(deliveries, 3);
});

test('failure of an older submission cannot clear a newer in-flight generation', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    let rejectOld, finishNew;
    const normal = f.bridge.requestPlayback;
    f.bridge.requestPlayback = async request => {
        if (request.generation === 4) await new Promise((resolve, reject) => { rejectOld = reject; });
        if (request.generation === 5) await new Promise(resolve => { finishNew = resolve; });
        return normal(request);
    };
    f.playlists.handleMessage(requestFor('11:22', 4)); await drain();
    f.playlists.handleMessage(requestFor('11:22', 5)); await drain();
    rejectOld(new Error('Old delivery failed')); await drain();
    f.playlists.handleMessage(requestFor('11:22', 5)); await drain();
    assert.equal(f.streamCalls.length, 2);
    finishNew(); await drain(); assert.equal(f.requests[0].generation, 5);
});

test('resolution errors send fixed codes without secrets or a stale playable source', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    f.context.fetchBilibiliVideoPlayInfo = async () => { throw new Error('Cookie=private; signed source'); };
    f.playlists.handleMessage(requestFor()); await drain();
    assert.deepEqual(f.requests[0], { type: 'submitWebPlaylistItem', playlistId, itemId: '11:21', generation: 1,
        media: null, error: 'playlist_resolve_failed' });
});

test('refresh can restore a registered playlist with progress disabled, without stopping its owner on navigation', async () => {
    const f = fixture({ sync: false }); f.bridge.restored = manifest();
    await f.playlists.restore();
    assert.equal(f.requests[0].type, 'getWebPlaylist'); assert.equal(f.requests[0].rebind, true);
    assert.equal(f.playlists.playlists.size, 1); assert.ok(f.bridge.httpWebPlaylists.has(playlistId));
    assert.ok(f.bridge.polls > 0);
    f.playlists.handleMessage(requestFor('11:22')); await drain();
    assert.equal(f.requests.at(-1).type, 'submitWebPlaylistItem');
});

test('a browser rebound cannot repeatedly steal the playlist back from the new page', async () => {
    const f = fixture(); f.playlists.adopt(manifest()); f.bridge.restored = manifest();
    f.playlists.handleMessage({ type: 'webPlaylistStopped', playlistId, reason: 'browser_rebound' });
    await f.playlists.restore(); assert.equal(f.requests.length, 0);
    assert.equal(f.bridge.httpWebPlaylists.size, 0);
});

test('playlist rebound revokes queued and in-flight history for every CID while the original webpage stays on P1', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    f.playlists.adopt(manifest()); f.sync.sessionId = sessionId; f.sync.sequence = 12;
    const cancelled = [], reporter = f.sync.reporter;
    for (const identity of ['bilibili:11:21', 'bilibili:11:22', 'bilibili:99:99']) {
        reporter.states.set(identity, { generation: 0, pending: { record: { position: 50 } },
            controller: { abort: () => cancelled.push(identity) } });
    }
    f.playlists.handleMessage({ type: 'webPlaylistStopped', playlistId, sessionId, reason: 'browser_rebound' });
    assert.deepEqual(cancelled, ['bilibili:11:21', 'bilibili:11:22']);
    assert.equal(reporter.states.get('bilibili:11:22').pending, undefined);
    assert.equal(reporter.states.get('bilibili:11:22').generation, 1);
    assert.ok(reporter.states.get('bilibili:99:99').pending);
    assert.equal(f.sync.sessionId, undefined); assert.equal(f.sync.sequence, -1);
    assert.equal(f.playlists.playlists.size, 0);
});

test('normal player closure retains the terminal history retry, and rebound never clears a newer unrelated player session', async () => {
    const f = fixture(); f.playlists.adopt(manifest());
    const state = { generation: 0, pending: { record: { position: 50 } } };
    f.sync.reporter.states.set('bilibili:11:22', state); f.sync.sessionId = 'progress-newer';
    f.playlists.handleMessage({ type: 'webPlaylistStopped', playlistId, sessionId, reason: 'player_closed' });
    assert.ok(state.pending); assert.equal(state.generation, 0);
    f.playlists.adopt(manifest());
    f.playlists.handleMessage({ type: 'webPlaylistStopped', playlistId, sessionId, reason: 'browser_rebound' });
    assert.equal(f.sync.sessionId, 'progress-newer');
});

test('progress setup reuses the playlist session and unique IPC pipe, preserving its selected CID', async () => {
    const f = fixture();
    await f.sync.resetPage(f.context.location.href);
    const registered = await f.context.prepareBilibiliPlaylistLaunch(media(), { name: 'MPV', presetEvent: {} });
    const prepared = await f.sync.prepareLaunch(registered, { name: 'MPV', presetEvent: {} });
    const started = f.requests.find(request => request.type === 'startPlaybackProgress');
    assert.equal(started.sessionId, registered.webPlaylist.sessionId); assert.equal(started.playlistId, registered.webPlaylist.playlistId);
    assert.equal(started.identity, 'bilibili:11:21');
    assert.equal(prepared.mpvOptions.filter(option => option.includes('input-ipc-server')).length, 1);
});

test('other playlist CIDs are saved and reported separately without seeking the original webpage', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    f.sync.sessionId = sessionId; f.playlists.adopt(manifest());
    const reports = []; f.sync.reporter.enqueue = (content, record, force) => reports.push({ content: plain(content), record: plain(record), force });
    f.sync.handleBridgeMessage({ type: 'playbackProgress', source: 'mpv', playlistId, sessionId,
        identity: 'bilibili:11:22', position: 30, duration: 158, updatedAt: 3000000, sequence: 1, paused: true });
    assert.equal(f.video.currentTime, 70); assert.equal(reports[0].content.cid, '22');
    assert.equal(f.context.readLocalPlaybackRecord('bilibili:11:22').position, 30);
    assert.equal(f.context.readLocalPlaybackRecord('bilibili:11:21'), undefined);
    f.sync.handleBridgeMessage({ type: 'playbackProgress', source: 'mpv', playlistId, sessionId: 'progress-foreign',
        identity: 'bilibili:11:22', position: 90, duration: 158, updatedAt: 4000000, sequence: 2 });
    assert.equal(reports.length, 1);
    f.sync.handleBridgeMessage({ type: 'playbackProgress', source: 'mpv', playlistId, sessionId,
        identity: 'bilibili:11:22', position: 90, duration: 158, updatedAt: 4000000, sequence: 1 });
    assert.equal(reports.length, 1);
});

test('native playlist preparation occurs before progress setup and old saved play events cannot add a global start', () => {
    const parser = between('class BaseParser {', 'function isRelayPlayer(');
    assert.ok(parser.indexOf('prepareBilibiliPlaylistLaunch(media, player)') < parser.indexOf('playbackProgressSync.prepareLaunch(media, player)'));
    assert.ok(parser.indexOf('if (media.webPlaylist) media.time = undefined') > parser.indexOf('playbackProgressSync.prepareLaunch(media, player)'));
    assert.match(source, /@version\s+1\.2\.14\.27/);
});

test('closing on another playlist CID syncs only the original page cached position', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    f.sync.sessionId = sessionId; f.playlists.adopt(manifest());
    f.sync.handleBridgeMessage({ type: 'playbackProgress', source: 'mpv', playlistId, sessionId,
        identity: 'bilibili:11:21', position: 84, duration: 194, updatedAt: 3000000, sequence: 1, paused: false });
    assert.equal(f.video.currentTime, 70);
    f.sync.handleBridgeMessage({ type: 'playbackProgress', source: 'mpv', playlistId, sessionId,
        identity: 'bilibili:11:22', position: 30, duration: 158, updatedAt: 3000001, sequence: 2, paused: true });
    assert.equal(f.video.currentTime, 70);
    f.sync.handleBridgeMessage({ type: 'playbackProgressStopped', playlistId: `playlist-${'b'.repeat(32)}`, sessionId,
        identity: 'bilibili:11:22', reason: 'player_closed' });
    assert.equal(f.video.currentTime, 70); assert.equal(f.sync.sessionId, sessionId);
    f.sync.handleBridgeMessage({ type: 'playbackProgressStopped', playlistId, sessionId,
        identity: 'bilibili:11:22', reason: 'player_closed' });
    assert.equal(f.video.currentTime, 84); assert.equal(f.sync.sessionId, undefined);
    assert.equal(f.context.readLocalPlaybackRecord('bilibili:11:22').position, 30);
});

test('same-millisecond queued progress for other playlist CIDs saves the latest sequence without moving the page', async () => {
    const f = fixture(); await f.sync.resetPage(f.context.location.href);
    f.sync.sessionId = sessionId; f.playlists.adopt(manifest());
    for (const [sequence, position, paused] of [[1, 40, true], [2, 30, true], [3, 32, false]]) {
        f.sync.handleBridgeMessage({ type: 'playbackProgress', source: 'mpv', playlistId, sessionId,
            identity: 'bilibili:11:22', position, duration: 158, updatedAt: 3000000, sequence, paused });
    }
    assert.equal(f.context.readLocalPlaybackRecord('bilibili:11:22').position, 32);
    assert.equal(f.context.readLocalPlaybackRecord('bilibili:11:22').paused, false);
    assert.equal(f.video.currentTime, 70);
});
