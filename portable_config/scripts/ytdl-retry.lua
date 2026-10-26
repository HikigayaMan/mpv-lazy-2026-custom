-- ytdl-retry.lua: auto-retry when yt-dlp extraction fails (e.g. SpankBang 403)
-- Place in portable_config/scripts/

local mp = require("mp")
local msg = require("mp.msg")

local max_retries = 8
local retry_delay = 2  -- seconds between retries
local retry_count = 0
local current, retry_timer = nil, nil
local generation = 0

local function cancel_retry()
    if retry_timer then retry_timer:kill(); retry_timer = nil end
end

-- Only retry URLs that go through yt-dlp (http/https, not direct media files)
local function is_ytdl_url(path)
    if type(path) ~= 'string' or mp.get_property_bool('ytdl', true) == false then return false end
    if not path:match("^https?://") then return false end
    -- The browser bridge resolves local playlist entries itself. Retrying them
    -- here can race its next P and must never replace the native playlist.
    local host = path:match('^https?://([^/]+)')
    host = host and host:lower()
    if host and (host:match('^127%.') or host:match('^localhost:')
        or host == 'localhost' or host:match('^%[::1%]')) then return false end
    -- Skip direct media file URLs
    local ext = path:match('^([^?#]+)'):match("%.(%w+)$")
    if ext then
        local media_exts = {
            mp4=1, mkv=1, webm=1, avi=1, flv=1, ts=1, m4s=1,
            mp3=1, m4a=1, flac=1, wav=1, ogg=1,
            m3u8=1, mpd=1,
        }
        if media_exts[ext:lower()] then return false end
    end
    return true
end

local function find_entry(id, path)
    for index, entry in ipairs(mp.get_property_native('playlist', {})) do
        if entry.id == id and entry.filename == path then return index - 1 end
    end
end

local function on_start_file(event)
    cancel_retry()
    generation = generation + 1
    local path = mp.get_property("path")
    local id = event.playlist_entry_id
    if id == nil then
        local playlist = mp.get_property_native('playlist', {})
        local entry = playlist[mp.get_property_number('playlist-pos', -1) + 1]
        id = entry and entry.id
    end
    if not current or path ~= current.path or id ~= current.id then retry_count = 0 end
    current = {path = path, id = id}
end

local function on_end_file(event)
    if current and event.playlist_entry_id ~= nil and event.playlist_entry_id ~= current.id then return end
    cancel_retry()
    if event.reason ~= "error" then return end
    local failed, token = current, generation
    if not failed or failed.id == nil or not is_ytdl_url(failed.path)
        or find_entry(failed.id, failed.path) == nil then return end
    if retry_count >= max_retries then
        msg.warn("ytdl-retry: gave up after " .. max_retries .. " retries")
        return
    end

    retry_count = retry_count + 1
    msg.info("ytdl-retry: attempt " .. retry_count .. "/" .. max_retries ..
             " in " .. retry_delay .. "s")
    mp.osd_message("Retrying (" .. retry_count .. "/" .. max_retries .. ")...", retry_delay)

    local scheduled
    scheduled = mp.add_timeout(retry_delay, function()
        if retry_timer ~= scheduled then return end
        retry_timer = nil
        if generation ~= token or current ~= failed or not is_ytdl_url(failed.path) then return end
        local index = find_entry(failed.id, failed.path)
        if index == nil then return end
        for _, entry in ipairs(mp.get_property_native('playlist', {})) do
            if entry.current and entry.id ~= failed.id then return end
        end
        -- Reopen the same native entry, preserving the complete list and IDs.
        mp.commandv('playlist-play-index', tostring(index))
    end)
    retry_timer = scheduled
end

mp.register_event("start-file", on_start_file)
mp.register_event("end-file", on_end_file)
mp.register_event('shutdown', cancel_retry)
mp.observe_property('playlist', 'native', function()
    if retry_timer and current and find_entry(current.id, current.path) == nil then cancel_retry() end
end)
