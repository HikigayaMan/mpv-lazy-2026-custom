import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../external_player.js', import.meta.url), 'utf8');
function between(start, end) {
    const first = source.indexOf(start);
    const last = source.indexOf(end, first + start.length);
    assert.ok(first >= 0 && last > first);
    return source.slice(first, last);
}
const plain = value => JSON.parse(JSON.stringify(value));
function encodedScriptOptionValue(option) {
    const match = String(option).match(/^--script-opts-append="[a-z0-9_-]+=%(\d+)%(.*)"$/i);
    assert.ok(match, 'MPV suboption must use fixed-length quoting before percent-encoded JSON');
    assert.equal(Number(match[1]), new TextEncoder().encode(match[2]).length);
    return match[2];
}
function sandbox(overrides = {}) {
    const context = vm.createContext({ URL, AbortController, TextEncoder, setTimeout, clearTimeout,
        console: { log() {}, warn() {}, error() {} }, currentTryCount: 0, MAX_TRY_COUNT: 1,
        translation: { requireLoginOrVip: 'Login or VIP required' },
        currentConfig: { global: { parser: { bilibiliLive: { preferredQuality: '4', preferredLine: '0' },
            bilibili: { preferredQuality: '127', preferredCodec: '12', preferredSubtitle: 'off' } } } },
        isMpvDanmakuPlayer: player => /^(MPV|MPVNET)$/i.test(player?.name || ''),
        ...overrides });
    vm.runInContext([
        between('const DOUYU_MD5_ROTATIONS =', 'function buildDouyuAuth('),
        between('// BILIBILI_LIVE_DANMAKU_HELPERS_START', '// BILIBILI_LIVE_DANMAKU_HELPERS_END'),
        between('// BILIBILI_STREAM_QUALITY_HELPERS_START', '// BILIBILI_STREAM_QUALITY_HELPERS_END'),
    ].join('\n'), context);
    return context;
}
function liveData(codecs, qnDesc = []) {
    return { live_status: 1, playurl_info: { playurl: { g_qn_desc: qnDesc, stream: [
        { protocol_name: 'http_stream', format: [{ format_name: 'flv', codec: codecs }] }
    ] } } };
}
function liveCodec(qn, name = 'avc', height = 720) {
    return { codec_name: name, current_qn: qn, accept_qn: [10000, 400, 250],
        base_url: `/live-${qn}-${name}.flv`, url_info: [
            { host: 'https://cdn.test', extra: '?signature=private' },
            { host: 'https://backup.test', extra: '?signature=private' }
        ], video_codecs: { base: name }, media_info: { width: height * 16 / 9, height, realtime_avg_bw: 42 } };
}
const descriptions = [
    { qn: 10000, desc: '原画', media_base_desc: { detail_desc: { desc: '1440P 原画' } } },
    { qn: 400, desc: '蓝光', media_base_desc: { detail_desc: { desc: '1080P 蓝光' } } },
    { qn: 250, desc: '超清', media_base_desc: { detail_desc: { desc: '720P 超清' } } }
];

test('authenticated live resolver retains 1440p HEVC even when AVC is only 720p, and probes advertised qualities', async () => {
    const calls = [];
    const context = sandbox({ fetch: async (url, options) => {
        const qn = Number(new URL(url).searchParams.get('qn'));
        calls.push({ qn, credentials: options.credentials });
        const data = qn === 30000 ? liveData([liveCodec(250), liveCodec(10000, 'hevc', 1440)], descriptions) :
            liveData([liveCodec(qn, 'avc', qn === 400 ? 1080 : 720)], descriptions);
        return { ok: true, json: async () => ({ code: 0, data }) };
    } });
    const result = await context.resolveBilibiliLiveStreams('53425', '4', '1');
    assert.deepEqual(calls.map(item => item.qn), [30000, 400]);
    assert.ok(calls.every(item => item.credentials === 'include'));
    assert.equal(result.selected.quality, 10000);
    assert.equal(result.selected.height, 1440);
    assert.equal(result.selected.codec, 'hevc');
    assert.equal(result.selected.title, '1440P 原画');
    assert.match(result.selected.url, /^https:\/\/backup\.test/);
    assert.deepEqual(plain(result.variants.map(item => item.quality)), [10000, 400, 250]);
    assert.ok(!result.selected.bandwidth, 'realtime throughput must not be labelled as the nominal bitrate');
});

test('server-downgraded live requests expose only their actual 720p tier and do not invent source or 4K entries', async () => {
    const context = sandbox({ fetch: async () => ({ ok: true, json: async () => ({ code: 0,
        data: liveData([liveCodec(250)], descriptions) }) }) });
    const result = await context.resolveBilibiliLiveStreams('53425', 'auto');
    assert.equal(result.variants.length, 1);
    assert.equal(result.selected.quality, 250);
    assert.equal(result.selected.title, '720P 超清');
    assert.equal(context.normalizeBilibiliLiveQuality('3'), '150');
    assert.equal(context.normalizeBilibiliLiveQuality('2'), '80');
    assert.equal(context.normalizeBilibiliLiveQuality('4'), 'auto');
});

test('DASH variants sort shuffled tiers, keep the preferred codec per tier and carry separate authenticated audio', () => {
    const context = sandbox();
    const video = (id, codecid, height) => ({ id, codecid, baseUrl: `https://cdn.test/${id}-${codecid}.m4s`,
        width: height * 16 / 9, height, bandwidth: id * 1000, codecs: `${codecid}`, frameRate: '60000/1001' });
    const variants = context.parseBilibiliDashStreams({ support_formats: [{ quality: 120, new_description: '4K 超清' }],
        dash: { audio: [{ base_url: 'https://cdn.test/audio.m4s' }], video: [
            video(64, 7, 720), video(120, 7, 2160), video(80, 7, 1080), video(120, 12, 2160), video(64, 12, 720)
        ] } }, '12');
    assert.deepEqual(plain(variants.map(item => item.quality)), [120, 80, 64]);
    assert.equal(variants[0].codec, '12');
    assert.equal(variants[0].title, '4K 超清');
    assert.equal(variants[0].audio_url, 'https://cdn.test/audio.m4s');
    assert.equal(context.selectBilibiliStreamVariant(variants, '127').quality, 120);
    assert.equal(context.selectBilibiliStreamVariant(variants, '80').quality, 80);
    assert.equal(variants[0].fps, 60000 / 1001);
});

test('VOD uses a signed WBI playurl request and browser credentials, preserving the server-returned authorized formats', async () => {
    const calls = [];
    const context = sandbox({ fetch: async (url, options) => {
        calls.push({ url: new URL(url), credentials: options.credentials });
        return { ok: true, json: async () => url.includes('/nav') ? { code: -101, data: { wbi_img: {
            img_url: 'https://img.test/7cd084941338484aae1ad9425b84077c.png',
            sub_url: 'https://img.test/4932caff0ff746eab6f01bf08b70ac45.png'
        } } } : { code: 0, data: { dash: { video: [] }, quality: 32 } } };
    } });
    const data = await context.fetchBilibiliVideoPlayInfo(100, 200);
    assert.equal(data.quality, 32);
    assert.equal(calls[1].url.pathname, '/x/player/wbi/playurl');
    assert.equal(calls[1].url.searchParams.get('qn'), '127');
    assert.equal(calls[1].url.searchParams.get('avid'), '100');
    assert.match(calls[1].url.searchParams.get('w_rid'), /^[a-f0-9]{32}$/);
    assert.ok(calls.every(item => item.credentials === 'include'));
    assert.ok(calls.every(item => !item.url.searchParams.has('SESSDATA')));
});

test('native VOD parsing passes the selected DASH video, separate audio, real size and all authorized variants to MPV', async () => {
    const context = sandbox({ currentUrl: 'https://www.bilibili.com/video/BVfixture',
        currentPlayer: { name: 'MPV', playEvent: 'media.video; media.audio; media.mpvOptions' },
        currentMedia: { bilibili: {}, mpvOptions: [] }, BaseParser: class {} });
    vm.runInContext(`const PARSER = { ${between('BILIBILI: class Parser extends BaseParser {', 'DOUYU: class Parser extends BaseParser {')} };
        globalThis.VideoParser = PARSER.BILIBILI;`, context);
    const parser = new context.VideoParser();
    parser.getVideoInfoByBvid = async () => ({ aid: 100, cid: 200, title: 'video' });
    context.fetchBilibiliVideoPlayInfo = async () => ({ support_formats: [{ quality: 80, new_description: '1080P 高清' }],
        dash: { audio: [{ baseUrl: 'https://cdn.test/audio.m4s' }], video: [
            { id: 64, codecid: 7, baseUrl: 'https://cdn.test/720.m4s', width: 1280, height: 720 },
            { id: 80, codecid: 12, baseUrl: 'https://cdn.test/1080.m4s', width: 1920, height: 1080 }
        ] } });
    await parser.parseVideo();
    const media = plain(context.currentMedia);
    assert.equal(media.video, 'https://cdn.test/1080.m4s');
    assert.equal(media.audio, 'https://cdn.test/audio.m4s');
    assert.equal(media.bilibili.cid, 200);
    const encoded = encodedScriptOptionValue(media.mpvOptions.find(option => option.includes('quality-menu-stream_variants=')));
    const variants = JSON.parse(decodeURIComponent(encoded));
    assert.equal(variants.length, 2);
    assert.equal(variants[0].height, 1080);
    assert.equal(variants[0].audio_url, media.audio);
});

test('stream metadata is bounded, retains the selected stream and rejects unsafe URLs', () => {
    const context = sandbox();
    const variants = Array.from({ length: 16 }, (_, index) => ({ url: `https://cdn.test/${index}.flv?sig=${'a'.repeat(2000)}`,
        title: '原画', height: 720 + index }));
    variants.push({ url: 'file:///C:/private/video.flv', height: 1080 });
    const selected = variants[0];
    const result = context.encodeBilibiliStreamVariants(variants, selected, 5500);
    assert.ok(result.encoded.length <= 5500);
    assert.ok(result.variants.some(item => item.url === selected.url));
    assert.ok(result.variants.every(item => item.url.startsWith('https://')));
    assert.equal(context.isBilibiliStreamUrl('https://user:secret@cdn.test/live.flv'), false);
    assert.equal(context.isBilibiliStreamUrl('javascript:alert(1)'), false);
    assert.equal(context.isBilibiliStreamUrl('https://cdn.test/live.flv\n--bad-option'), false);
});

test('long legal signed video and audio URLs reduce metadata within the total Windows launch budget', () => {
    const context = sandbox();
    const url = name => `https://cdn.test/${name}.m4s?sig=${'x'.repeat(4020)}`;
    const audio = url('audio');
    const variants = Array.from({ length: 6 }, (_, index) => ({ url: url(`v${index}`),
        audio_url: audio, title: `Tier ${index}`, height: 720 + index }));
    const selected = variants[1];
    const media = { video: selected.url, audio, subtitle: 'https://cdn.test/subtitle.srt',
        title: '视频标题'.repeat(40), referer: 'https://www.bilibili.com/video/BVfixture',
        mpvOptions: ['--input-ipc-server=\\\\.\\pipe\\mpv-lazy-bilibili-test'], ytdlp: {}, bilibili: {} };
    context.attachBilibiliStreamVariants(media, variants, selected);
    const option = media.mpvOptions.find(item => item.includes('quality-menu-stream_variants='));
    assert.ok(option);
    const encoded = encodedScriptOptionValue(option);
    const decoded = JSON.parse(decodeURIComponent(encoded));
    assert.ok(encoded.length <= 20480);
    assert.ok(decoded.some(item => item.url === selected.url));
    assert.ok(decoded.some(item => item.url === variants[0].url), 'retain the highest entry as well as the selected one');
    const basicArgsLength = [media.video, media.audio, media.subtitle, media.title, media.referer, ...media.mpvOptions]
        .reduce((total, value) => total + value.length, 0);
    assert.ok(basicArgsLength + 3000 <= 30000);
    assert.equal(media.video, selected.url, 'metadata trimming must not change the chosen quality');
});

test('when external options exhaust the launch budget, omit the quality list while retaining the highest selected video', () => {
    const context = sandbox();
    const highest = { url: 'https://cdn.test/highest.flv', title: '原画', height: 1440 };
    const media = { video: highest.url, title: 'video', mpvOptions: ['--custom-long-option=' + 'x'.repeat(26990)] };
    context.attachBilibiliStreamVariants(media, [highest, { url: 'https://cdn.test/lower.flv', height: 720 }], highest);
    assert.ok(!media.mpvOptions.some(item => item.includes('quality-menu-stream_variants=')));
    assert.equal(media.video, highest.url);
    assert.ok(media.mpvOptions.includes('--no-ytdl'));
});

test('omit metadata when the selected and highest tiers cannot both fit, rather than dropping the highest alternative', () => {
    const context = sandbox();
    const highest = { url: 'https://cdn.test/high.flv?sig=' + 'x'.repeat(2000), height: 1440 };
    const selected = { url: 'https://cdn.test/low.flv', height: 720 };
    assert.equal(context.encodeBilibiliStreamVariants([highest, selected], selected, 500), undefined);
});

test('MPV receives percent-encoded JSON as one fixed-length quoted suboption instead of parsing its leading %5B', () => {
    const context = sandbox();
    const encoded = encodeURIComponent(JSON.stringify([{ url: 'https://cdn.test/live.flv?sig=private', title: '原画', height: 1440 }]));
    for (const name of ['quality-menu-stream_variants', 'quality-menu-hls_variants']) {
        const option = context.buildMpvEncodedScriptOption(name, encoded);
        assert.match(option, /=%\d+%%5B/);
        assert.equal(encodedScriptOptionValue(option), encoded);
        assert.equal(JSON.parse(decodeURIComponent(encodedScriptOptionValue(option)))[0].title, '原画');
    }
    assert.throws(() => context.buildMpvEncodedScriptOption('invalid,name', encoded));
});

test('both default MPV players append the Bilibili cid without overwriting the previously attached quality list', () => {
    const context = sandbox({ navigator: { language: 'zh-CN', platform: 'Win32' } });
    vm.runInContext(between('const defaultConfig = {', 'if (DEBUG === true)') + '\nglobalThis.defaults = defaultConfig;', context);
    for (const name of ['MPV', 'MPVNET']) {
        const event = context.defaults.players.find(player => player.name === name).playEvent;
        assert.match(event, /--script-opts-append="cid=/);
        assert.ok(!event.includes('--script-opts="cid='));
    }
    assert.ok(!source.includes('`--script-opts="cid=${media.bilibili.cid}"`'), 'CSP fallback must also append cid');
});

test('common handoff upgrades an iframe/sniffer source while retaining custom options and avoiding duplicate resolution', async () => {
    let requests = 0;
    const context = sandbox();
    const selected = { url: 'https://cdn.test/source.flv', quality: 10000, height: 1440, title: '1440P 原画' };
    context.resolveBilibiliLiveStreams = async () => { requests++; return { variants: [selected], selected }; };
    const initial = { video: 'https://cdn.test/captured-low.flv', mpvOptions: ['--custom-option=keep'],
        bilibili: {}, ytdlp: { format: 'keep-custom-format' } };
    const media = await context.prepareBilibiliLiveStreamLaunch(initial, { name: 'MPV' }, '53425');
    assert.equal(media.video, selected.url);
    assert.ok(media.mpvOptions.includes('--custom-option=keep'));
    assert.ok(media.mpvOptions.includes('--no-ytdl'));
    assert.ok(media.mpvOptions.some(option => option.includes('quality-menu-stream_variants=')));
    assert.equal(media.bilibili.liveQualityRoom, '53425');
    assert.equal(media.sensitiveUrl, true);
    assert.equal(media.disableBrowserRelay, true);
    assert.equal(initial.video, 'https://cdn.test/captured-low.flv');
    await context.prepareBilibiliLiveStreamLaunch(media, { name: 'MPV' }, '53425');
    await context.prepareBilibiliLiveStreamLaunch(initial, { name: 'VLC' }, '53425');
    assert.equal(requests, 1);
});

test('sensitive launch logging redacts stream metadata and audio signatures along with the video URL', () => {
    const logged = [];
    const context = sandbox({ console: { log: value => logged.push(value) } });
    vm.runInContext(between('// LAUNCH_LOG_HELPER_START', '// LAUNCH_LOG_HELPER_END'), context);
    context.logLaunchArgs(['https://cdn.test/video?secret', '--audio-file="https://cdn.test/audio?secret"',
        '--script-opts-append="quality-menu-stream_variants=secret"', '--no-ytdl'], { sensitiveUrl: true });
    assert.ok(!JSON.stringify(logged).includes('secret'));
    assert.equal(logged[0].at(-1), '--no-ytdl');
});
