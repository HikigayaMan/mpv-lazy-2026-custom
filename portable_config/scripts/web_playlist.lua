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
local METADATA_STATE = 'user-data/mpv-lazy/web-playlist/metadata-state'
local EXTEND_ACK = 'user-data/mpv-lazy/web-playlist/extend-ack'
local generation, pending, current, quality_reload = 0, nil, nil, nil
local manifest_upload, manifest_revision = nil, 0
local extending, last_extension = nil, nil

local function metadata_state(status, revision, code)
    local old = mp.get_property_native(METADATA_STATE)
    if type(old) == 'table' and old.playlistId == opts.id and old.revision > revision then return end
    -- A late bootstrap commit can arrive after its metadata failure notice.
    -- Only a newer metadata transaction may replace that failure with loading.
    if type(old) == 'table' and old.playlistId == opts.id and old.revision == revision
        and old.status == 'failed' and status == 'pending' then return end
    mp.set_property_native(METADATA_STATE, {playlistId = opts.id, revision = revision, status = status, error = code})
end

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
        or not label(data.collectionId, true, 128) or not label(data.collectionTitle, true)
        or data.metadataPending ~= nil and type(data.metadataPending) ~= 'boolean' then
        manifest_ack(data, 'begin', false, 'invalid_manifest'); return
    end
    if manifest_upload and data.revision == manifest_upload.revision then
        local same = data.itemCount == manifest_upload.itemCount and data.collectionId == manifest_upload.collectionId
            and data.collectionTitle == manifest_upload.collectionTitle
            and (data.metadataPending == true) == manifest_upload.metadataPending
        manifest_ack(data, 'begin', same, not same and 'manifest_conflict' or nil); return
    end
    manifest_revision = data.revision
    manifest_upload = {revision = data.revision, itemCount = data.itemCount, collectionId = data.collectionId,
        collectionTitle = data.collectionTitle, metadataPending = data.metadataPending == true,
        items = {}, seen = {}, chunks = {}, nextChunk = 0, bytes = 0}
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
            collectionId = upload.collectionId, collectionTitle = upload.collectionTitle,
            metadataPending = upload.metadataPending, items = upload.items})
        upload.committed = true
        metadata_state(upload.metadataPending and 'pending' or 'ready', upload.revision)
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

local function extend_ack(data, ok, code)
    mp.set_property_native(EXTEND_ACK, {playlistId = opts.id, revision = data.revision,
        itemCount = data.itemCount, ok = ok, error = code})
end
local function native_entry(playlist, id)
    for index, entry in ipairs(playlist) do if entry.id == id then return index, entry end end
end
local function rollback_extension(request)
    -- Only remove this session's appended entries. Never remove the entry now
    -- playing or media that the user added independently while the URL loaded.
    local playlist = mp.get_property_native('playlist') or {}
    for index = #playlist, 1, -1 do
        local entry = playlist[index]
        if entry.id ~= request.nativeId and not entry.current and item_from_path(entry.filename)
            and not request.before[entry.id] then mp.commandv('playlist-remove', tostring(index - 1)) end
    end
end
local function finish_extension(request, ok, code)
    if extending ~= request then return end
    extending = nil
    if request.timer then request.timer:kill() end
    if not ok then
        rollback_extension(request)
        metadata_state('failed', request.data.revision, code)
    else
        -- The full public manifest follows the native list ACK separately.
        metadata_state('pending', request.data.revision)
        last_extension = request.data
    end
    extend_ack(request.data, ok, code)
end
local function arrange_extension(request)
    local playlist = mp.get_property_native('playlist') or {}
    local original_index, original = native_entry(playlist, request.nativeId)
    if not original or not original.current or original.filename ~= request.path
        or mp.get_property('path') ~= request.path then return false, 'playlist_extension_player_changed' end
    if #playlist ~= request.data.itemCount + 1 then return false, 'playlist_extension_incomplete' end
    local ordered, seen, duplicate, target = {}, {}, nil, nil
    for _, entry in ipairs(playlist) do
        if entry.id ~= request.nativeId then
            local item = item_from_path(entry.filename)
            if not item or entry.id == nil or request.before[entry.id] or seen[item] then
                return false, 'playlist_extension_invalid'
            end
            seen[item] = true
            ordered[#ordered + 1] = item
            if entry.filename == request.path then duplicate, target = entry.id, #ordered - 1 end
        end
    end
    if not duplicate then return false, 'playlist_extension_missing_current' end
    local duplicate_index = native_entry(playlist, duplicate)
    mp.commandv('playlist-remove', tostring(duplicate_index - 1))
    playlist = mp.get_property_native('playlist') or {}
    original_index, original = native_entry(playlist, request.nativeId)
    if not original or not original.current then return false, 'playlist_extension_player_changed' end
    local from = original_index - 1
    if from ~= target then mp.commandv('playlist-move', tostring(from), tostring(target + (from < target and 1 or 0))) end
    playlist = mp.get_property_native('playlist') or {}
    if #playlist ~= request.data.itemCount then return false, 'playlist_extension_incomplete' end
    for index, entry in ipairs(playlist) do
        if item_from_path(entry.filename) ~= ordered[index] then return false, 'playlist_extension_invalid' end
    end
    local _, preserved = native_entry(playlist, request.nativeId)
    if not preserved or not preserved.current or mp.get_property('path') ~= request.path then
        return false, 'playlist_extension_player_changed'
    end
    return true
end
local function start_extension(request)
    if extending ~= request or request.command then return end
    -- A tiny collection can finish its metadata before the initial video has
    -- opened. Let that deferred on_load finish and its first file-loaded fire.
    if not current or current.path ~= request.path or current.stage ~= 'ready' then return end
    local _, original = native_entry(mp.get_property_native('playlist') or {}, request.nativeId)
    if not original or not original.current or mp.get_property('path') ~= request.path then
        finish_extension(request, false, 'playlist_extension_player_changed'); return
    end
    request.command = mp.command_native_async({'loadlist', request.data.playlistUrl, 'append'}, function(success)
        if extending ~= request then return end
        if not success then finish_extension(request, false, 'playlist_extension_load_failed'); return end
        local ok, code = arrange_extension(request)
        finish_extension(request, ok, code)
    end)
end
mp.register_script_message('web-playlist-extend', function(raw)
    local data = decode_manifest(raw)
    if not data or not integer(data.itemCount, 1) then return end
    local port, id
    if type(data.playlistUrl) == 'string' then
        port, id = data.playlistUrl:match('^http://127%.0%.0%.1:(%d+)/web%-playlist/(playlist%-%x+)/list%.m3u$')
    end
    if id ~= opts.id or not port or tonumber(port) < 1 or tonumber(port) > 65535 then
        extend_ack(data, false, 'playlist_extension_invalid'); return
    end
    local prior = extending and extending.data or last_extension
    if prior and data.revision <= prior.revision then
        local same = data.revision == prior.revision and data.itemCount == prior.itemCount and data.playlistUrl == prior.playlistUrl
        if not same then extend_ack(data, false, 'playlist_extension_stale')
        elseif not extending then extend_ack(data, true) end
        return
    end
    if extending or data.revision <= manifest_revision then extend_ack(data, false, 'playlist_extension_stale'); return end
    local playlist = mp.get_property_native('playlist') or {}
    local original = playlist[1]
    if #playlist ~= 1 or not original or original.id == nil or not original.current
        or not item_from_path(original.filename) or mp.get_property('path') ~= original.filename then
        extend_ack(data, false, 'playlist_extension_player_changed'); return
    end
    if data.playlistUrl ~= original.filename:gsub('/item/%d+:%d+$', '/list.m3u') then
        extend_ack(data, false, 'playlist_extension_invalid'); return
    end
    local request = {data = data, nativeId = original.id, path = original.filename, before = {[original.id] = true}}
    extending = request
    metadata_state('pending', data.revision)
    request.timer = mp.add_timeout(15, function()
        if extending ~= request then return end
        if request.command and mp.abort_async_command then mp.abort_async_command(request.command) end
        finish_extension(request, false, 'playlist_extension_timeout')
    end)
    start_extension(request)
end)
mp.register_script_message('web-playlist-metadata-error', function(raw)
    local data = decode_manifest(raw)
    if not data or data.error ~= 'playlist_metadata_failed' or data.revision < manifest_revision
        or last_extension and data.revision < last_extension.revision
        or extending and data.revision < extending.data.revision then return end
    metadata_state('failed', data.revision, data.error)
    mp.commandv('show-text', '完整合集加载失败；当前视频继续播放，请保留原网页并重试', 5000)
end)
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
    local source = media.video
    if media.audio then
        -- Separate audio-files seek to the requested timestamp while a fast
        -- video seek can land on an earlier keyframe, producing seconds of
        -- silence. EDL gives both DASH tracks one seek/cache timeline, as in
        -- MPV's ytdl hook. Length quoting preserves signed URL delimiters.
        -- A network playlist may only reference network-origin streams. Supply
        -- EDL as in-memory data so its HTTPS children keep that restriction;
        -- the edl:// protocol itself is classified as a filesystem source.
        source = 'memory://# mpv EDL v0\n!no_clip;!no_chapters;%' .. #media.video .. '%' .. media.video
            .. ';!new_stream;!no_clip;!no_chapters;%' .. #media.audio .. '%' .. media.audio
    end
    mp.set_property('stream-open-filename', source)
    mp.set_property_bool('file-local-options/ytdl', false)
    mp.set_property_native('file-local-options/audio-files', {})
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
    -- Do not restart a stream that has not reached file-loaded yet. Its
    -- time-pos can still be absent/zero, including during a quality reload.
    -- Reserve one reload immediately, before MPV dispatches its on_load hook.
    if pending or quality_reload or not current or current.stage ~= 'ready'
        or current.path ~= mp.get_property('path') then return end
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
    if extending then start_extension(extending) end
end)
mp.register_event('end-file', function(event)
    if event and event.reason == 'error' then state('failed', current and current.itemId, 'web_playlist_media_failed') end
end)
mp.register_event('shutdown', function() release_pending('web_playlist_player_closed') end)
mp.set_property_native(REQUEST, {})
