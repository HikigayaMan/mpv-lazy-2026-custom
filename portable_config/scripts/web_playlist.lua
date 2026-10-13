-- Keep stable entries in MPV's native playlist and resolve each media on load.
local mp = require 'mp'
local utils = require 'mp.utils'
local options = require 'mp.options'
local opts = {id = '', session_id = '', resolve_timeout = 20}
options.read_options(opts, 'web_playlist')
if #opts.id ~= 41 or not opts.id:match('^playlist%-%x+$')
    or #opts.session_id > 64 or not opts.session_id:match('^progress%-%w[%w_-]*$') then return end

local REQUEST = 'user-data/mpv-lazy/web-playlist/request'
local CURRENT = 'user-data/mpv-lazy/web-playlist/current'
local STATE = 'user-data/mpv-lazy/web-playlist/state'
-- Keep the large manifest outside the small parent read by the IPC watcher.
local MANIFEST = 'user-data/mpv-lazy/web-playlist-manifest'
local MANIFEST_ACK = 'user-data/mpv-lazy/web-playlist/manifest-ack'
local generation, pending, current, quality_reload = 0, nil, nil, nil
local manifest_upload, manifest_revision = nil, 0

local function integer(value, minimum)
    return type(value) == 'number' and value >= (minimum or 0) and value <= 9007199254740991 and value == math.floor(value)
end
local function label(value, optional, maximum)
    if optional and value == nil then return true end
    return type(value) == 'string' and (optional or #value > 0) and #value <= (maximum or 360)
        and not value:find('[%z\1-\31\127]')
end
local manifest_fields = {'itemId', 'title', 'duration', 'sectionId', 'sectionTitle', 'videoId', 'videoTitle', 'partTitle', 'p', 'partCount', 'order'}
local function valid_manifest_item(item)
    if type(item) ~= 'table' or type(item.itemId) ~= 'string' or not item.itemId:match('^[1-9]%d*:[1-9]%d*$')
        or #item.itemId > 41 or not label(item.title) then return false end
    for _, key in ipairs({'sectionTitle', 'videoTitle', 'partTitle'}) do if not label(item[key], true) then return false end end
    for _, key in ipairs({'sectionId', 'videoId'}) do if not label(item[key], true, 128) then return false end end
    for _, key in ipairs({'p', 'partCount'}) do if item[key] ~= nil and not integer(item[key], 1) then return false end end
    if item.order ~= nil and not integer(item.order) then return false end
    if item.duration ~= nil and (type(item.duration) ~= 'number' or item.duration < 0 or item.duration ~= item.duration or item.duration == math.huge) then return false end
    return true
end
local function manifest_ack(data, stage, ok, code, count)
    mp.set_property_native(MANIFEST_ACK, {playlistId = opts.id, revision = data.revision, stage = stage,
        chunkIndex = data.chunkIndex, itemCount = count or (stage == 'chunk' and manifest_upload and #manifest_upload.items or data.itemCount),
        ok = ok, error = code})
end
local function decode_manifest(raw)
    if type(raw) ~= 'string' or #raw > 32768 then return nil end
    local data = utils.parse_json(raw)
    if type(data) ~= 'table' or data.playlistId ~= opts.id or not integer(data.revision, 1) then return nil end
    return data
end
mp.register_script_message('web-playlist-manifest-begin', function(raw)
    local data = decode_manifest(raw)
    if not data then return end
    if data.revision < manifest_revision or not integer(data.itemCount, 1)
        or not label(data.collectionId, true, 128) or not label(data.collectionTitle, true) then
        manifest_ack(data, 'begin', false, 'invalid_manifest'); return
    end
    if manifest_upload and data.revision == manifest_upload.revision then
        local same = data.itemCount == manifest_upload.itemCount and data.collectionId == manifest_upload.collectionId
            and data.collectionTitle == manifest_upload.collectionTitle
        manifest_ack(data, 'begin', same, not same and 'manifest_conflict' or nil); return
    end
    manifest_revision = data.revision
    manifest_upload = {revision = data.revision, itemCount = data.itemCount, collectionId = data.collectionId,
        collectionTitle = data.collectionTitle, items = {}, seen = {}, chunks = {}, nextChunk = 0, bytes = 0}
    manifest_ack(data, 'begin', true)
end)
mp.register_script_message('web-playlist-manifest-chunk', function(raw)
    local data = decode_manifest(raw)
    if not data then return end
    local upload = manifest_upload
    if not upload or data.revision ~= upload.revision or not integer(data.chunkIndex) or type(data.items) ~= 'table'
        or #data.items < 1 or data.chunkIndex > upload.nextChunk then
        manifest_ack(data, 'chunk', false, 'invalid_manifest_chunk'); return
    end
    local previous = upload.chunks[data.chunkIndex]
    if previous then
        local same = #data.items == previous.count
        for index, item in ipairs(data.items) do
            if type(item) ~= 'table' then same = false; break end
            local saved = upload.items[previous.offset + index]
            for _, key in ipairs(manifest_fields) do if item[key] ~= saved[key] then same = false end end
        end
        manifest_ack(data, 'chunk', same, not same and 'manifest_conflict' or nil, previous.offset + previous.count); return
    end
    if upload.committed or upload.bytes + #raw > 32 * 1024 * 1024 or #upload.items + #data.items > upload.itemCount then
        manifest_ack(data, 'chunk', false, 'manifest_size'); return
    end
    local seen = {}
    for _, item in ipairs(data.items) do
        if not valid_manifest_item(item) or upload.seen[item.itemId] or seen[item.itemId] then
            manifest_ack(data, 'chunk', false, 'invalid_manifest_item'); return
        end
        seen[item.itemId] = true
    end
    upload.chunks[data.chunkIndex] = {offset = #upload.items, count = #data.items}
    for _, item in ipairs(data.items) do
        local copy = {}
        for _, key in ipairs(manifest_fields) do copy[key] = item[key] end
        upload.items[#upload.items + 1], upload.seen[item.itemId] = copy, true
    end
    upload.bytes, upload.nextChunk = upload.bytes + #raw, upload.nextChunk + 1
    manifest_ack(data, 'chunk', true)
end)
mp.register_script_message('web-playlist-manifest-commit', function(raw)
    local data = decode_manifest(raw)
    if not data then return end
    local upload = manifest_upload
    if not upload or data.revision ~= upload.revision or data.itemCount ~= upload.itemCount
        or #upload.items ~= upload.itemCount or data.chunkCount ~= upload.nextChunk then
        manifest_ack(data, 'commit', false, 'incomplete_manifest'); return
    end
    if not upload.committed then
        mp.set_property_native(MANIFEST, {playlistId = opts.id, revision = upload.revision, itemCount = upload.itemCount,
            collectionId = upload.collectionId, collectionTitle = upload.collectionTitle, items = upload.items})
        upload.committed = true
    end
    manifest_ack(data, 'commit', true)
end)
mp.set_property_native('user-data/mpv-lazy/web-playlist/manifest-api-version', 1)

local function finite(value)
    return type(value) == 'number' and value == value and value >= 0 and value < math.huge
end
local function item_from_path(path)
    if type(path) ~= 'string' then return nil end
    local port, id, item = path:match('^http://127%.0%.0%.1:(%d+)/web%-playlist/(playlist%-%x+)/item/(%d+:%d+)$')
    if id ~= opts.id or not port or tonumber(port) < 1 or tonumber(port) > 65535 then return nil end
    return item
end
local function http_url(value)
    return type(value) == 'string' and #value <= 32768
        and value:match('^https?://[^%s]+$') and not value:find('[\r\n]')
end
local function state(stage, item, code)
    mp.set_property_native(STATE, {playlistId = opts.id, itemId = item, generation = generation,
        stage = stage, error = code})
end
local function release_pending(code)
    local request = pending
    if not request then return end
    pending = nil
    if request.timer then request.timer:kill() end
    mp.set_property_native(REQUEST, {})
    if code then
        current = nil
        mp.set_property_native(CURRENT, nil)
        -- Fail this entry without trying its loopback placeholder as media.
        mp.set_property('stream-open-filename', 'memory://')
        state('failed', request.itemId, code)
        mp.commandv('show-text', '列表视频解析失败，请保留原网页并重试或切换其他条目', 4000)
    end
    request.hook:cont()
end
local function apply_media(media, item)
    if type(media) ~= 'table' or media.identity ~= 'bilibili:' .. item
        or tostring(media.cid or '') ~= item:match(':(%d+)$') or not http_url(media.video)
        or media.audio and not http_url(media.audio) or media.subtitle and not http_url(media.subtitle)
        or not finite(tonumber(media.position)) then return false end
    mp.set_property('stream-open-filename', media.video)
    mp.set_property_bool('file-local-options/ytdl', false)
    mp.set_property_native('file-local-options/audio-files', media.audio and {media.audio} or {})
    mp.set_property_native('file-local-options/sub-files', media.subtitle and {media.subtitle} or {})
    mp.set_property('file-local-options/force-media-title', tostring(media.title or item))
    mp.set_property('file-local-options/start', tostring(media.position))
    mp.set_property_native('file-local-options/http-header-fields', {'Referer: https://www.bilibili.com/'})
    local script_opts = mp.get_property_native('options/script-opts') or {}
    local per_file = {}
    for key, value in pairs(script_opts) do per_file[key] = value end
    per_file.cid = tostring(media.cid)
    mp.set_property_native('file-local-options/script-opts', per_file)
    current = {}
    for key, value in pairs(media) do current[key] = value end
    current.playlistId, current.sessionId, current.itemId = opts.id, opts.session_id, item
    current.path, current.generation, current.stage = mp.get_property('path'), generation, 'loading'
    mp.set_property_native(CURRENT, current)
    state('loading', item)
    return true
end

mp.add_hook('on_load', 5, function(hook)
    local item = item_from_path(mp.get_property('path'))
    if not item then
        if pending then release_pending() end
        current, quality_reload = nil, nil
        mp.set_property_native(CURRENT, nil)
        mp.set_property_native(REQUEST, {})
        return
    end
    if pending then release_pending('web_playlist_replaced') end
    generation = generation + 1
    if quality_reload and quality_reload.itemId == item and quality_reload.expiresAt >= mp.get_time() then
        local reload = quality_reload
        quality_reload = nil
        if apply_media(reload.media, item) then
            mp.set_property_bool('pause', reload.paused)
            return
        end
    end
    quality_reload, current = nil, nil
    mp.set_property_native(CURRENT, nil)
    hook:defer()
    pending = {hook = hook, itemId = item, generation = generation}
    local expected_generation = generation
    pending.timer = mp.add_timeout(math.min(60, math.max(5, tonumber(opts.resolve_timeout) or 20)), function()
        if pending and pending.generation == expected_generation then release_pending('web_playlist_timeout') end
    end)
    state('resolving', item)
    mp.set_property_native(REQUEST, {playlistId = opts.id, itemId = item, generation = generation})
end)

mp.register_script_message('web-playlist-resolved', function(raw)
    local result = utils.parse_json(raw or '')
    if not pending or type(result) ~= 'table' or result.playlistId ~= opts.id
        or result.itemId ~= pending.itemId or result.generation ~= pending.generation then return end
    if result.error or not apply_media(result.media, pending.itemId) then
        release_pending('web_playlist_resolution_failed')
    else
        release_pending()
    end
end)

mp.register_script_message('web-playlist-quality', function(target_url)
    if pending or not current or current.path ~= mp.get_property('path') then return end
    local selected
    for _, variant in ipairs(current.variants or {}) do
        if variant.url == target_url then selected = variant break end
    end
    if not selected then return end
    local media = {}
    for key, value in pairs(current) do media[key] = value end
    media.video, media.audio = selected.url, selected.audio_url
    media.position = mp.get_property_number('time-pos', 0)
    quality_reload = {itemId = current.itemId, media = media, expiresAt = mp.get_time() + 15,
        paused = mp.get_property_bool('pause', false)}
    mp.set_property_native('user-data/mpv-lazy/web-playlist/quality-switch', {
        playlistId = opts.id, itemId = current.itemId, identity = current.identity,
        position = media.position, expiresAt = quality_reload.expiresAt})
    mp.commandv('playlist-play-index', 'current')
end)

mp.register_event('file-loaded', function()
    if not current or current.path ~= mp.get_property('path') then return end
    -- Resume applies once. Native loop-file must repeat the entire video.
    mp.set_property('file-local-options/start', '0')
    current.stage = 'ready'
    mp.set_property_native(CURRENT, current)
    state('ready', current.itemId)
    mp.set_property_native('user-data/mpv-lazy/web-playlist/quality-switch', nil)
end)
mp.register_event('end-file', function(event)
    if event and event.reason == 'error' then state('failed', current and current.itemId, 'web_playlist_media_failed') end
end)
mp.register_event('shutdown', function() release_pending('web_playlist_player_closed') end)
mp.set_property_native(REQUEST, {})
