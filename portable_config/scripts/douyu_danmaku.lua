-- Lightweight Douyu live chat overlay for the per-launch MPV IPC pipe.
-- It deliberately uses only ass-events; it never changes vf, fps, subtitles, or filters.

local mp = require('mp')
local utils = require('mp.utils')

local overlay = mp.create_osd_overlay('ass-events')
local MAX_QUEUE = 300
local MAX_PENDING_BATCHES = 32
local MAX_BATCH_BYTES = 65536
local UOSC_API_WAIT_SECONDS = 1.5
local MAX_ACTIVE = 80
local MAX_LANES = 18
local MAX_TEXT_BYTES = 720
local FONT_SIZE = 28
local LANE_HEIGHT = 38
local GAP = 72
local BASE_SPEED = 160

local session_id = nil
local session_route = nil
local route_timer = nil
local pending_batches = {}
local pending = {}
local active = {}
local lane_ready_at = {}
local uosc_danmaku_active = false
local last_tick = mp.get_time()

local function live_api_v1_available()
    local value = mp.get_property_native('user-data/uosc_danmaku/live-api-version')
    if type(value) == 'number' then
        return value >= 1 and value < 2
    end
    if type(value) ~= 'string' then
        return false
    end
    return value == '1' or value == 'v1' or value:match('^v?1%.%d+$') ~= nil
end

local function publish_test_state()
    if not mp.get_property_native('user-data/douyu_danmaku/test-mode') then
        return
    end
    local payload = utils.format_json({
        session_id = session_id,
        route = session_route,
        pending_batch_count = #pending_batches,
        pending_count = #pending,
        active_count = #active,
    })
    if payload then
        mp.set_property_native('user-data/douyu_danmaku/test-state', payload)
    end
end

local function publish_route()
    mp.set_property_native('user-data/douyu_danmaku/route', session_route or 'idle')
end

local function clear_route_timer()
    if route_timer then
        route_timer:kill()
        route_timer = nil
    end
end

local function ass_escape(text)
    text = tostring(text or '')
    text = text:gsub('\\', '\\\\')
    text = text:gsub('{', '\\{')
    text = text:gsub('}', '\\}')
    text = text:gsub('[\r\n]+', ' ')
    if #text > MAX_TEXT_BYTES then
        text = text:sub(1, MAX_TEXT_BYTES)
        while #text > 0 do
            local byte = text:byte(-1)
            if byte and byte >= 0x80 and byte <= 0xBF then
                text = text:sub(1, -2)
            else
                break
            end
        end
    end
    return text
end

local function clear_overlay()
    pending = {}
    active = {}
    lane_ready_at = {}
    overlay.data = ''
    overlay:remove()
    publish_test_state()
end

local function clear_session_state()
    clear_route_timer()
    pending_batches = {}
    clear_overlay()
end

local function forward_live(message, id, payload)
    if payload == nil then
        mp.commandv('script-message-to', 'uosc_danmaku', message, id)
    else
        mp.commandv('script-message-to', 'uosc_danmaku', message, id, payload)
    end
end

local function end_session(send_live_stop)
    local id = session_id
    local route = session_route
    session_id = nil
    session_route = nil
    clear_session_state()
    if send_live_stop and route == 'uosc' and id then
        forward_live('live-stop', id)
    end
    publish_route()
    publish_test_state()
end

local function reserve_lane(now, width, speed, lane_count)
    for lane = 1, lane_count do
        if (lane_ready_at[lane] or 0) <= now then
            lane_ready_at[lane] = now + (width + GAP) / speed
            return lane
        end
    end
    return nil
end

local function add_message(message, now, osd_width, osd_height, lane_count, top)
    if #active >= MAX_ACTIVE then
        return false
    end
    local text = ass_escape(message.text)
    if text == '' then
        return true
    end
    local nick = ass_escape(message.nick)
    local shown = nick ~= '' and (nick .. ': ' .. text) or text
    local estimated_width = math.min(osd_width * 0.8, math.max(160, #shown * FONT_SIZE * 0.62))
    local speed = BASE_SPEED + math.min(110, estimated_width * 0.08)
    local lane = reserve_lane(now, estimated_width, speed, lane_count)
    if not lane then
        return false
    end
    local y = top + (lane - 1) * LANE_HEIGHT
    active[#active + 1] = {
        text = shown,
        x = osd_width + 8,
        y = y,
        width = estimated_width,
        speed = speed,
    }
    return true
end

local function render()
    if uosc_danmaku_active or (#active == 0 and #pending == 0) then
        overlay:remove()
        return
    end
    local lines = {}
    for _, item in ipairs(active) do
        lines[#lines + 1] = string.format('{\\an7\\pos(%.1f,%.1f)\\fs%d\\bord2\\shad1\\1c&HFFFFFF&\\3c&H202020&}%s', item.x, item.y, FONT_SIZE, item.text)
    end
    overlay.res_x, overlay.res_y = mp.get_osd_size()
    overlay.data = table.concat(lines, '\n')
    overlay:update()
end

local function tick()
    if session_route ~= 'fallback' or uosc_danmaku_active then
        return
    end
    local now = mp.get_time()
    local delta = math.min(0.2, math.max(0, now - last_tick))
    last_tick = now
    local osd_width, osd_height = mp.get_osd_size()
    if not osd_width or not osd_height or osd_width <= 0 or osd_height <= 0 then
        return
    end
    local top = math.min(36, math.max(0, osd_height - LANE_HEIGHT))
    local lane_count = math.max(1, math.min(MAX_LANES, math.floor(math.max(LANE_HEIGHT, osd_height - top) / LANE_HEIGHT)))
    for index = #active, 1, -1 do
        local item = active[index]
        item.x = item.x - item.speed * delta
        if item.x + item.width < 0 then
            table.remove(active, index)
        end
    end
    while #pending > 0 do
        if not add_message(pending[1], now, osd_width, osd_height, lane_count, top) then
            break
        end
        table.remove(pending, 1)
    end
    render()
    publish_test_state()
end

local function queue_fallback_batch(encoded)
    local messages = utils.parse_json(encoded or '')
    if type(messages) ~= 'table' then
        return
    end
    for _, message in ipairs(messages) do
        if #pending >= MAX_QUEUE then
            break
        end
        if type(message) == 'table' and type(message.text) == 'string' then
            pending[#pending + 1] = { text = message.text, nick = message.nick }
        end
    end
end

local function select_uosc_route()
    if (session_route ~= 'pending' and session_route ~= 'fallback')
        or not session_id
        or not live_api_v1_available()
    then
        return false
    end
    local previous_route = session_route
    clear_route_timer()
    if previous_route == 'fallback' then
        pending_batches = {}
        clear_overlay()
    end
    session_route = 'uosc'
    publish_route()
    forward_live('live-start', session_id)
    if previous_route == 'pending' then
        for _, encoded in ipairs(pending_batches) do
            forward_live('live-batch', session_id, encoded)
        end
    end
    pending_batches = {}
    publish_test_state()
    return true
end

local function select_fallback_route()
    if session_route ~= 'pending' or not session_id then
        return
    end
    clear_route_timer()
    session_route = 'fallback'
    publish_route()
    for _, encoded in ipairs(pending_batches) do
        queue_fallback_batch(encoded)
    end
    pending_batches = {}
    publish_test_state()
end

local function start(id)
    local next_id = tostring(id or '')
    if session_id == next_id then
        return
    end
    if session_id then
        end_session(true)
    end
    session_id = next_id
    last_tick = mp.get_time()
    clear_session_state()
    session_route = 'pending'
    publish_route()
    route_timer = mp.add_timeout(UOSC_API_WAIT_SECONDS, select_fallback_route)
    select_uosc_route()
    publish_test_state()
end

local function stop(id)
    if id == nil or tostring(id) == session_id then
        end_session(true)
    end
end

local function receive_batch(id, encoded)
    if not session_id or tostring(id or '') ~= session_id then
        return
    end
    if type(encoded) ~= 'string' or #encoded > MAX_BATCH_BYTES then
        return
    end
    if session_route == 'pending' then
        if #pending_batches < MAX_PENDING_BATCHES then
            pending_batches[#pending_batches + 1] = encoded
        end
        publish_test_state()
        return
    end
    if session_route == 'uosc' then
        if not live_api_v1_available() then
            end_session(true)
            return
        end
        forward_live('live-batch', session_id, encoded or '')
        return
    end
    if session_route ~= 'fallback' or uosc_danmaku_active then
        return
    end
    queue_fallback_batch(encoded)
    publish_test_state()
end

local function ping(id)
    if not session_id or tostring(id or '') ~= session_id then
        return
    end
    if session_route == 'uosc' then
        if not live_api_v1_available() then
            end_session(true)
            return
        end
        forward_live('live-ping', session_id)
    end
    -- The bridge uses this no-op to verify that the per-launch MPV pipe is alive.
end

mp.observe_property('user-data/uosc_danmaku/has-danmaku', 'native', function(_, value)
    uosc_danmaku_active = value == true or value == 'yes'
    if uosc_danmaku_active then
        clear_overlay()
    end
end)

mp.observe_property('user-data/uosc_danmaku/live-api-version', 'native', function()
    if session_route == 'pending' or session_route == 'fallback' then
        select_uosc_route()
    elseif session_route == 'uosc' and not live_api_v1_available() then
        end_session(true)
    end
end)

mp.register_script_message('start', start)
mp.register_script_message('stop', stop)
mp.register_script_message('batch', receive_batch)
mp.register_script_message('ping', ping)
mp.register_event('end-file', function() end_session(true) end)
mp.register_event('shutdown', function() end_session(true) end)
mp.add_periodic_timer(1 / 30, tick)
publish_route()
