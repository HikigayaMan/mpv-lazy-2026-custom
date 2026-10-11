-- Real-time danmaku input. It deliberately keeps live data out of persisted
-- source/history state: it is an ephemeral input source for streams.
local msg = require("mp.msg")
local utils = require("mp.utils")

local LIVE_WINDOW_SECONDS = 90
local LIVE_EVENT_LIMIT = 800
local LIVE_BATCH_LIMIT = 120
local LIVE_JSON_LIMIT = 65536
local LIVE_TEXT_LIMIT = 160
local LIVE_NICK_LIMIT = 48
local LIVE_REFRESH_DELAY = 0.25
local LIVE_ACTIVE_PROPERTY = "user-data/uosc_danmaku/live-active"
local LIVE_INPUT_COUNT_PROPERTY = "user-data/uosc_danmaku/live-input-count"
local LIVE_SESSION_PROPERTY = "user-data/uosc_danmaku/live-session"
local STREAM_SWITCH_PROPERTY = "user-data/quality-menu/stream-switch"
local STREAM_SWITCH_ACK_PROPERTY = "user-data/uosc_danmaku/live-switch-ack"
local STREAM_SWITCH_SECONDS = 15

LIVE_DANMAKU = {
    active = false,
    session_id = nil,
    items = {},
    events = {},
    last_time = 0,
    dropped_while_paused = 0,
}

local refresh_timer = nil
local switch_timer = nil
local pending_switch = nil
local last_switch_generation = 0
local stream_path = nil

local function valid_session_id(session_id)
    return type(session_id) == "string" and session_id:match("^[%w_-]+$") and #session_id <= 64
end

local function current_time()
    return mp.get_property_number("time-pos", 0) or 0
end

local function valid_stream_path(path)
    return type(path) == "string" and #path > 0 and #path <= 16384
        and path:match("^https?://[^/%s]+") and not path:find("[%z\r\n]")
end

local function stream_switch_marker()
    local marker = mp.get_property_native(STREAM_SWITCH_PROPERTY)
    if type(marker) ~= "table" or marker.provider ~= "bilibili"
        or marker.sessionId ~= LIVE_DANMAKU.session_id then return nil end
    local generation = marker.generation
    local started = marker.startedAt
    local expires = marker.expiresAt
    local now = mp.get_time()
    local epoch = os.time()
    if type(generation) ~= "number" or generation <= 0 or generation == math.huge or generation ~= math.floor(generation)
        or type(started) ~= "number" or started ~= started or started > now + 0.1
        or now - started > STREAM_SWITCH_SECONDS
        or type(expires) ~= "number" or expires ~= expires or expires <= epoch
        or expires > epoch + STREAM_SWITCH_SECONDS + 1
        or not valid_stream_path(marker.from) or not valid_stream_path(marker.to)
        or (marker.stage ~= "loading" and marker.stage ~= "loaded") then return nil end
    return marker
end

local function cancel_stream_switch()
    if switch_timer then switch_timer:kill(); switch_timer = nil end
    pending_switch = nil
end

-- Lua slices bytes, so retain whole UTF-8 characters at the input limit.
local function bounded_text(value, limit)
    local text = value:gsub("[%z\r\n]", " ")
    if #text <= limit then return text end
    local finish = limit
    while finish > 0 do
        local byte = text:byte(finish)
        if byte < 0x80 or byte >= 0xC0 then break end
        finish = finish - 1
    end
    local lead = text:byte(finish)
    local size = lead and (lead >= 0xF0 and 4 or lead >= 0xE0 and 3 or lead >= 0xC0 and 2 or 1) or 1
    if finish + size - 1 > limit then finish = finish - 1 else finish = limit end
    return text:sub(1, finish)
end

local function clear_live_items()
    LIVE_DANMAKU.items = {}
    LIVE_DANMAKU.events = {}
    LIVE_DANMAKU.last_time = 0
    LIVE_DANMAKU.dropped_while_paused = 0
    mp.set_property_native(LIVE_INPUT_COUNT_PROPERTY, 0)
end

function is_live_danmaku_active()
    return LIVE_DANMAKU.active == true
end

function get_live_danmaku_events()
    return LIVE_DANMAKU.events
end

local function prune_live_items(now)
    local cutoff = now - LIVE_WINDOW_SECONDS
    local first = 1
    while first <= #LIVE_DANMAKU.items and LIVE_DANMAKU.items[first].time < cutoff do
        first = first + 1
    end
    if first > 1 then
        for i = first, #LIVE_DANMAKU.items do
            LIVE_DANMAKU.items[i - first + 1] = LIVE_DANMAKU.items[i]
        end
        for i = #LIVE_DANMAKU.items - first + 2, #LIVE_DANMAKU.items do
            LIVE_DANMAKU.items[i] = nil
        end
    end
    while #LIVE_DANMAKU.items > LIVE_EVENT_LIMIT do
        table.remove(LIVE_DANMAKU.items, 1)
    end
    mp.set_property_native(LIVE_INPUT_COUNT_PROPERTY, #LIVE_DANMAKU.items)
end

function refresh_live_danmaku_now()
    if not LIVE_DANMAKU.active then return end
    prune_live_items(current_time())
    LIVE_DANMAKU.events = build_live_ass_events(LIVE_DANMAKU.items)
    if ENABLED and render then
        render()
    end
end

function schedule_live_danmaku_refresh()
    -- A busy room must still render while more batches are arriving.
    if refresh_timer then return end
    refresh_timer = mp.add_timeout(LIVE_REFRESH_DELAY, function()
        refresh_timer = nil
        refresh_live_danmaku_now()
    end)
end

function refresh_live_danmaku_style()
    if is_live_danmaku_active() then
        schedule_live_danmaku_refresh()
        return true
    end
    return false
end

local function live_start(session_id)
    if not valid_session_id(session_id) then
        msg.warn("live-start rejected: invalid session id")
        return
    end

    if LIVE_DANMAKU.active and LIVE_DANMAKU.session_id == session_id then return end
    if LIVE_DANMAKU.active then
        clear_live_items()
    end
    LIVE_DANMAKU.active = true
    LIVE_DANMAKU.session_id = session_id
    cancel_stream_switch()
    last_switch_generation = 0
    stream_path = mp.get_property("path")
    mp.set_property_bool(LIVE_ACTIVE_PROPERTY, true)
    mp.set_property_native(LIVE_SESSION_PROPERTY, session_id)
    mp.set_property_native(STREAM_SWITCH_ACK_PROPERTY, nil)
    ENABLED = true
    toggle_danmaku_switch("on")
    show_danmaku_func(true)
    msg.info("live danmaku started: " .. session_id)
end

function toggle_live_danmaku()
    if not LIVE_DANMAKU.active then return false end
    ENABLED = not ENABLED
    if ENABLED then
        toggle_danmaku_switch("on")
        show_danmaku_func(true)
    else
        clear_live_items()
        if refresh_timer then
            refresh_timer:kill()
            refresh_timer = nil
        end
        toggle_danmaku_switch("off")
        hide_danmaku_func(true)
    end
    return true
end

local function live_batch(session_id, json)
    if not LIVE_DANMAKU.active or session_id ~= LIVE_DANMAKU.session_id then
        msg.warn("live-batch rejected: inactive or mismatched session")
        return
    end
    if type(json) ~= "string" or #json > LIVE_JSON_LIMIT then
        msg.warn("live-batch rejected: invalid payload")
        return
    end
    if not ENABLED or pending_switch then return end
    if mp.get_property_bool("pause", false) then
        LIVE_DANMAKU.dropped_while_paused = LIVE_DANMAKU.dropped_while_paused + 1
        return
    end

    local batch = utils.parse_json(json)
    if type(batch) ~= "table" then
        msg.warn("live-batch rejected: invalid JSON")
        return
    end

    local now = current_time()
    local accepted = 0
    for _, item in ipairs(batch) do
        if accepted >= LIVE_BATCH_LIMIT then break end
        if type(item) == "table" and type(item.text) == "string" then
            local text = bounded_text(item.text, LIVE_TEXT_LIMIT)
            if text:match("%S") then
                local nick = type(item.nick) == "string" and bounded_text(item.nick, LIVE_NICK_LIMIT) or ""
                local display = nick ~= "" and (nick .. ": " .. text) or text
                local color = tonumber(item.color) or 0xFFFFFF
                if color ~= color or color == math.huge or color == -math.huge then color = 0xFFFFFF end
                color = math.max(0, math.min(math.floor(color), 0xFFFFFF))
                local event_time = math.max(now + 0.10, LIVE_DANMAKU.last_time + 0.005)
                LIVE_DANMAKU.items[#LIVE_DANMAKU.items + 1] = {
                    time = event_time,
                    orig_time = event_time,
                    type = 1,
                    size = tonumber(options.fontsize) or 50,
                    color = color,
                    text = display,
                    source = "live",
                }
                LIVE_DANMAKU.last_time = event_time
                accepted = accepted + 1
            end
        end
    end

    if accepted > 0 then
        prune_live_items(now)
        schedule_live_danmaku_refresh()
    end
end

function stop_live_danmaku(session_id, quiet)
    if not LIVE_DANMAKU.active then return end
    if session_id and session_id ~= LIVE_DANMAKU.session_id then
        msg.warn("live-stop rejected: mismatched session")
        return
    end
    if refresh_timer then
        refresh_timer:kill()
        refresh_timer = nil
    end
    cancel_stream_switch()
    clear_live_items()
    LIVE_DANMAKU.active = false
    LIVE_DANMAKU.session_id = nil
    stream_path = nil
    mp.set_property_native(LIVE_SESSION_PROPERTY, "")
    mp.set_property_native(STREAM_SWITCH_ACK_PROPERTY, nil)
    mp.set_property_bool(LIVE_ACTIVE_PROPERTY, false)
    LIVE_DANMAKU.stopping = true
    ENABLED = false
    hide_danmaku_func(true)
    LIVE_DANMAKU.stopping = false
    toggle_danmaku_switch("off")
    if not quiet then
        msg.info("live danmaku stopped")
    end
end

mp.set_property_native("user-data/uosc_danmaku/live-api-version", 1)
mp.set_property_bool(LIVE_ACTIVE_PROPERTY, false)
mp.set_property_native(LIVE_SESSION_PROPERTY, "")
mp.set_property_native(STREAM_SWITCH_ACK_PROPERTY, nil)
mp.set_property_native(LIVE_INPUT_COUNT_PROPERTY, 0)
mp.register_script_message("live-start", live_start)
mp.register_script_message("live-batch", live_batch)
mp.register_script_message("live-ping", function(session_id)
    if LIVE_DANMAKU.active and session_id == LIVE_DANMAKU.session_id then
        -- The message intentionally has no side effects; it is a liveness probe.
    end
end)
mp.register_script_message("live-stop", stop_live_danmaku)
mp.register_event("end-file", function(event)
    if not LIVE_DANMAKU.active then return end
    local marker = stream_switch_marker()
    local path = mp.get_property("path")
    if marker and marker.stage == "loading" and not pending_switch
        and marker.generation > last_switch_generation
        -- MPV may clear path while the old file unloads during loadfile replace.
        -- The cached stream_path still binds this stop to the authorized source;
        -- file-loaded below must confirm the exact target before resuming input.
        and marker.from == stream_path
        and (path == nil or path == "" or path == marker.from or path == marker.to)
        and (event.reason == "stop" or event.reason == "restart") then
        pending_switch = { generation = marker.generation, from = marker.from, to = marker.to,
            startedAt = marker.startedAt, expiresAt = marker.expiresAt }
        last_switch_generation = marker.generation
        if refresh_timer then refresh_timer:kill(); refresh_timer = nil end
        clear_live_items()
        hide_danmaku_func(true)
        switch_timer = mp.add_timeout(math.max(0.01, STREAM_SWITCH_SECONDS - (mp.get_time() - marker.startedAt)), function()
            switch_timer = nil
            if pending_switch then stop_live_danmaku(nil, true) end
        end)
        return
    end
    stop_live_danmaku(nil, true)
end)
mp.register_event("file-loaded", function()
    if not pending_switch or not LIVE_DANMAKU.active then return end
    local marker = stream_switch_marker()
    local path = mp.get_property("path")
    if not marker or marker.generation ~= pending_switch.generation
        or marker.from ~= pending_switch.from or marker.to ~= pending_switch.to
        or marker.startedAt ~= pending_switch.startedAt or marker.expiresAt ~= pending_switch.expiresAt
        or path ~= pending_switch.to then
        stop_live_danmaku(nil, true)
        return
    end
    stream_path = path
    cancel_stream_switch()
    clear_live_items()
    if ENABLED then show_danmaku_func(true) end
end)
