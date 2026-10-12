-- Publish only the web-launched content's progress to its private IPC session.
-- The browser retains site credentials; this script never makes web requests.
local mp = require 'mp'
local options = require 'mp.options'
local opts = {identity = '', session_id = ''}
options.read_options(opts, 'web_progress')

-- .16-.18 sent two pairs in one -append; MPV appends one literal value.
-- Accept that exact legacy form so installed browser scripts keep working.
if opts.session_id == '' then
    local identity, session = opts.identity:match('^(.-),web_progress%-session_id=(progress%-%w[%w_-]*)$')
    if identity and session then opts.identity, opts.session_id = identity, session end
end

local valid_identity = opts.identity:match('^bilibili:%d+:%d+$')
    or (opts.identity:match('^web:%x+$') and (#opts.identity == 36 or #opts.identity == 68))
if not valid_identity or #opts.session_id > 64
    or not opts.session_id:match('^progress%-%w[%w_-]*$') then return end

local PROPERTY = 'user-data/mpv-lazy/playback-progress'
local SWITCH = 'user-data/mpv-lazy/progress-stream-switch'
local first_file = true
local active, ready, ended, completed = true, false, false, false
local last_path, position, duration = nil, 0, 0
local waiting_switch = nil
local waiting_resume = nil

local function finite(value)
    return type(value) == 'number' and value == value and value >= 0 and value < math.huge
end

local function switch_allowed(path)
    local marker = mp.get_property_native(SWITCH)
    return type(marker) == 'table' and marker.sessionId == opts.session_id
        and marker.identity == opts.identity and marker.from == last_path
        and marker.to == path and finite(marker.expiresAt) and marker.expiresAt >= mp.get_time()
end

local function publish(sample)
    if sample and active and waiting_resume then
        local pos = mp.get_property_number('time-pos')
        if finite(pos) and math.abs(pos - waiting_resume.position) <= 0.75 then
            ready, waiting_resume = true, nil
        elseif mp.get_time() > waiting_resume.expiresAt then
            active, ready, ended, waiting_resume = false, false, true, nil
        end
    end
    if sample and ready and active then
        local pos = mp.get_property_number('time-pos')
        local dur = mp.get_property_number('duration')
        if finite(pos) then position = pos end
        if finite(dur) then duration = dur end
    end
    mp.set_property_native(PROPERTY, {
        apiVersion = 1, identity = opts.identity, sessionId = opts.session_id,
        active = active, ready = ready and duration > 0,
        position = position, duration = duration,
        paused = mp.get_property_bool('pause', false), ended = ended, completed = completed,
    })
end

mp.register_event('start-file', function()
    local path = mp.get_property('path')
    local allowed = first_file or (active and (path == last_path or switch_allowed(path)))
    waiting_switch = not first_file and allowed and path or nil
    active, ready, ended, completed = allowed == true, false, false, false
    publish(false)
end)

mp.register_event('file-loaded', function()
    local path = mp.get_property('path')
    local authorized_switch = not first_file and active and switch_allowed(path)
    local marker = authorized_switch and mp.get_property_native(SWITCH) or nil
    if not first_file and path ~= last_path and not (active and switch_allowed(path)) then
        active = false
    end
    first_file = false
    if active then
        last_path, ready, waiting_switch = path, true, nil
        if marker and finite(marker.resumePosition) then
            ready = false
            waiting_resume = {position = marker.resumePosition, expiresAt = marker.expiresAt}
        end
        publish(true)
    else
        ready, waiting_resume = false, nil
        publish(false)
    end
    mp.set_property_native(SWITCH, nil)
end)

mp.register_event('end-file', function(event)
    -- A quality replacement emits end-file before the new stream loads. Keep
    -- the old timestamp while loading, so a temporary zero cannot erase it.
    local marker = mp.get_property_native(SWITCH)
    if active and type(marker) == 'table' and switch_allowed(marker.to)
        and event and event.reason ~= 'error' then
        ready, waiting_switch = false, marker.to
    elseif active then
        ended = true
        completed = event and event.reason == 'eof' or false
        if completed and duration > 0 then position = duration end
        ready = duration > 0
    end
    publish(false)
end)

mp.observe_property('pause', 'bool', function() publish(true) end)
mp.register_event('seek', function() publish(true) end)
mp.register_event('playback-restart', function() publish(true) end)
mp.register_event('shutdown', function()
    ended = true
    publish(true)
end)
mp.add_periodic_timer(1, function()
    if waiting_switch and not switch_allowed(waiting_switch) then
        active, ready, ended, waiting_switch = false, false, true, nil
    end
    publish(true)
end)
publish(false)
