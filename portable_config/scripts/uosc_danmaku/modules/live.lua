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

LIVE_DANMAKU = {
    active = false,
    session_id = nil,
    items = {},
    events = {},
    last_time = 0,
    dropped_while_paused = 0,
}

local refresh_timer = nil

local function valid_session_id(session_id)
    return type(session_id) == "string" and session_id:match("^[%w_-]+$") and #session_id <= 64
end

local function current_time()
    return mp.get_property_number("time-pos", 0) or 0
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
    mp.set_property_bool(LIVE_ACTIVE_PROPERTY, true)
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
    if not ENABLED then return end
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
    clear_live_items()
    LIVE_DANMAKU.active = false
    LIVE_DANMAKU.session_id = nil
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
mp.set_property_native(LIVE_INPUT_COUNT_PROPERTY, 0)
mp.register_script_message("live-start", live_start)
mp.register_script_message("live-batch", live_batch)
mp.register_script_message("live-ping", function(session_id)
    if LIVE_DANMAKU.active and session_id == LIVE_DANMAKU.session_id then
        -- The message intentionally has no side effects; it is a liveness probe.
    end
end)
mp.register_script_message("live-stop", stop_live_danmaku)
mp.register_event("end-file", function()
    stop_live_danmaku(nil, true)
end)
