-- Dedicated Douyu live chat renderer for the per-launch MPV IPC pipe.
-- It owns one persistent ass-events overlay and never changes subtitles or vf.

local mp = require('mp')
local options = require('mp.options')
local utils = require('mp.utils')

local settings = {
    font = 'SimHei',
    font_size = 16,
    font_scale = 1.0,
    opacity = 0.55,
    outline = 1.0,
    shadow = 0,
    bold = true,
    scroll_duration = 10.0,
    speed_scale = 1.0,
    display_area = 0.25,
    lane_spacing = 8,
    show_nick = true,
    source_colors = true,
    max_pending = 300,
    max_active = 48,
    max_launch_rate = 10,
    max_queue_age = 2.0,
    max_render_fps = 120,
    fallback_fps = 60,
}

options.read_options(settings, 'douyu_danmaku')

local VIRTUAL_WIDTH = 1920
local VIRTUAL_HEIGHT = 1080
local MAX_BATCH_BYTES = 65536
local MAX_TEXT_BYTES = 720
local MIN_RENDER_FPS = 1
local DOUYU_PALETTE = {
    ['1'] = 0xFF0000,
    ['2'] = 0x1E87F0,
    ['3'] = 0x7AC84B,
    ['4'] = 0xFF7F00,
    ['5'] = 0x9B39F4,
    ['6'] = 0xFF69B4,
}

local overlay = mp.create_osd_overlay('ass-events')
local render_timer = nil
local session_id = nil
local pending = {}
local active = {}
local lane_ready_at = {}
local next_lane = 1
local next_launch_at = 0
local motion_time = 0
local overlay_visible = false
local last_tick_at = nil
local last_ass_data = ''
local render_updates = 0
local last_stats_payload = ''
local stats = {
    received = 0,
    shown = 0,
    dropped_stale = 0,
    dropped_overflow = 0,
}
local tick

local function clamp(value, minimum, maximum)
    return math.max(minimum, math.min(maximum, value))
end

local function option_number(name, fallback, minimum, maximum)
    local value = tonumber(settings[name])
    if value == nil then
        value = fallback
    end
    return clamp(value, minimum, maximum)
end

local function is_test_mode()
    return mp.get_property_native('user-data/douyu_danmaku/test-mode') == true
end

local function current_display_fps()
    if is_test_mode() then
        local test_fps = tonumber(mp.get_property_native('user-data/douyu_danmaku/test-display-fps'))
        if test_fps ~= nil then
            return test_fps
        end
    end
    return tonumber(mp.get_property_native('display-fps')) or 0
end

local function target_render_fps()
    local fallback = option_number('fallback_fps', 60, MIN_RENDER_FPS, 240)
    local display_fps = current_display_fps()
    if display_fps <= 0 then
        display_fps = fallback
    end
    local cap = option_number('max_render_fps', 120, MIN_RENDER_FPS, 240)
    return math.min(display_fps, cap)
end

local function publish_route()
    mp.set_property_native('user-data/douyu_danmaku/route', session_id and 'overlay' or 'idle')
end

local function publish_stats()
    local payload = utils.format_json({
        received = stats.received,
        shown = stats.shown,
        dropped_stale = stats.dropped_stale,
        dropped_overflow = stats.dropped_overflow,
        pending = #pending,
        active = #active,
    })
    if payload and payload ~= last_stats_payload then
        mp.set_property_native('user-data/douyu_danmaku/stats', payload)
        last_stats_payload = payload
    end
end

local function publish_test_state()
    if not is_test_mode() then
        return
    end
    local widest_active = 0
    for _, item in ipairs(active) do
        widest_active = math.max(widest_active, item.width or 0)
    end
    local payload = utils.format_json({
        session_id = session_id,
        route = session_id and 'overlay' or nil,
        pending_count = #pending,
        active_count = #active,
        timer_running = render_timer and render_timer:is_enabled() or false,
        timer_fps = target_render_fps(),
        overlay_updates = render_updates,
        overlay_visible = overlay_visible,
        last_ass_data = last_ass_data,
        received_count = stats.received,
        shown_count = stats.shown,
        dropped_stale_count = stats.dropped_stale,
        dropped_overflow_count = stats.dropped_overflow,
        next_launch_at = next_launch_at,
        motion_time = motion_time,
        widest_active = widest_active,
    })
    if payload then
        mp.set_property_native('user-data/douyu_danmaku/test-state', payload)
    end
end

local function normalize_text(value)
    local text = tostring(value or '')
    text = text:gsub('[\r\n]+', ' ')
    if #text > MAX_TEXT_BYTES then
        local cutoff = MAX_TEXT_BYTES
        while cutoff > 0 do
            local byte = text:byte(cutoff)
            if byte and byte >= 0x80 and byte <= 0xBF then
                cutoff = cutoff - 1
            else
                break
            end
        end
        local lead = text:byte(cutoff)
        local expected = lead and (lead >= 0xF0 and 4 or lead >= 0xE0 and 3 or lead >= 0xC0 and 2 or 1) or 1
        if MAX_TEXT_BYTES - cutoff + 1 < expected then
            cutoff = cutoff - 1
        else
            cutoff = MAX_TEXT_BYTES
        end
        text = text:sub(1, math.max(cutoff, 0))
    end
    return text
end

local function ass_escape(value)
    local text = normalize_text(value)
    text = text:gsub('\\', '\\\\')
    text = text:gsub('{', '\\{')
    text = text:gsub('}', '\\}')
    return text
end

local function ass_font_escape(value)
    return ass_escape(value):gsub(',', ' ')
end

local function utf8_codepoint(character)
    local first, second, third, fourth = character:byte(1, 4)
    if not first then
        return 0
    elseif first < 0x80 then
        return first
    elseif first < 0xE0 and second then
        return (first - 0xC0) * 0x40 + (second - 0x80)
    elseif first < 0xF0 and second and third then
        return (first - 0xE0) * 0x1000 + (second - 0x80) * 0x40 + (third - 0x80)
    elseif second and third and fourth then
        return (first - 0xF0) * 0x40000 + (second - 0x80) * 0x1000
            + (third - 0x80) * 0x40 + (fourth - 0x80)
    end
    return first
end

local function character_width_units(character)
    local codepoint = utf8_codepoint(character)
    if codepoint >= 0x1100 then
        return 1.0
    elseif character == ' ' then
        return 0.33
    elseif character:find("[ilI%.,'`!:;|]", 1) then
        return 0.32
    elseif character:find('[MW@#%%&]', 1) then
        return 0.85
    end
    return 0.58
end

local function estimated_text_width(value, font_size)
    local units = 0
    for character in tostring(value or ''):gmatch('[%z\1-\127\194-\244][\128-\191]*') do
        units = units + character_width_units(character)
    end
    local outline = option_number('outline', 1, 0, 20)
    return math.max(font_size * 2, (units * font_size + outline * 2) * 1.15)
end

local function ass_color_from_source(value)
    if not settings.source_colors then
        return 'FFFFFF'
    end
    local raw = tostring(value or '')
    local rgb = DOUYU_PALETTE[raw]
    if not rgb then
        local numeric = tonumber(raw)
        if numeric and numeric >= 1 and numeric <= 0xFFFFFF and raw:match('^%d+$') then
            rgb = math.floor(numeric)
        end
    end
    if not rgb then
        return 'FFFFFF'
    end
    local red = math.floor(rgb / 0x10000) % 0x100
    local green = math.floor(rgb / 0x100) % 0x100
    local blue = rgb % 0x100
    return string.format('%02X%02X%02X', blue, green, red)
end

local function clear_overlay_once()
    if not overlay_visible then
        return
    end
    overlay.res_x = VIRTUAL_WIDTH
    overlay.res_y = VIRTUAL_HEIGHT
    overlay.data = ''
    overlay:update()
    overlay_visible = false
    last_ass_data = ''
    render_updates = render_updates + 1
end

local function stop_timer()
    if render_timer then
        render_timer:stop()
    end
    last_tick_at = nil
end

local function has_work()
    return session_id ~= nil and (#pending > 0 or #active > 0)
end

local function start_timer()
    if not has_work() or mp.get_property_native('pause') == true then
        return
    end
    local fps = target_render_fps()
    local was_running = render_timer and render_timer:is_enabled()
    if not render_timer then
        render_timer = mp.add_periodic_timer(1 / fps, function() tick() end, true)
    end
    render_timer.timeout = 1 / fps
    if not was_running then
        last_tick_at = mp.get_time()
        render_timer:resume()
    end
end

local function line_height()
    local size = option_number('font_size', 16, 8, 160) * option_number('font_scale', 1, 0.25, 4)
    return size + option_number('lane_spacing', 8, 0, 120)
end

local function lane_count()
    local area = option_number('display_area', 0.25, 0.05, 1.0)
    return math.max(1, math.floor((VIRTUAL_HEIGHT * area) / line_height()))
end

local function scroll_speed()
    local duration = option_number('scroll_duration', 10, 2, 60)
    return (VIRTUAL_WIDTH / duration) * option_number('speed_scale', 1, 0.1, 5)
end

local function reserve_lane(now, width, speed)
    local count = lane_count()
    for offset = 0, count - 1 do
        local lane = ((next_lane + offset - 1) % count) + 1
        if (lane_ready_at[lane] or 0) <= now then
            lane_ready_at[lane] = now + (width + option_number('lane_spacing', 8, 0, 120)) / speed
            next_lane = (lane % count) + 1
            return lane
        end
    end
    return nil
end

local function enqueue_message(message)
    local plain_text = normalize_text(message.text)
    local plain_nick = normalize_text(message.nick)
    local text = ass_escape(plain_text)
    if text == '' then
        return
    end
    stats.received = stats.received + 1
    while #pending >= option_number('max_pending', 300, 1, 2000) do
        table.remove(pending, 1)
        stats.dropped_overflow = stats.dropped_overflow + 1
    end
    local received_at = mp.get_time()
    if is_test_mode() and tonumber(message._test_received_at) then
        received_at = tonumber(message._test_received_at)
    else
        local bridge_age = clamp((tonumber(message._bridge_age_ms) or 0) / 1000, 0, 60)
        received_at = received_at - bridge_age
    end
    pending[#pending + 1] = {
        text = text,
        nick = ass_escape(plain_nick),
        plain_text = plain_text,
        plain_nick = plain_nick,
        color = ass_color_from_source(message.color or message.col),
        received_at = received_at,
    }
end

local function drop_stale_pending(now)
    local max_age = option_number('max_queue_age', 2, 0.1, 30)
    while #pending > 0 and now - (pending[1].received_at or now) > max_age do
        table.remove(pending, 1)
        stats.dropped_stale = stats.dropped_stale + 1
    end
end

local function activate_pending(real_now, current_motion_time)
    drop_stale_pending(real_now)
    if #pending == 0 or current_motion_time < next_launch_at then
        return
    end
    if #active >= option_number('max_active', 48, 1, 500) then
        return
    end

    local font_size = option_number('font_size', 16, 8, 160) * option_number('font_scale', 1, 0.25, 4)
    local speed = scroll_speed()
    local message = pending[1]
    local shown = message.text
    local plain_shown = message.plain_text
    if settings.show_nick and message.nick ~= '' then
        shown = message.nick .. ': ' .. shown
        plain_shown = message.plain_nick .. ': ' .. plain_shown
    end
    local width = estimated_text_width(plain_shown, font_size)
    local lane = reserve_lane(current_motion_time, width, speed)
    if not lane then
        return
    end
    active[#active + 1] = {
        text = shown,
        color = message.color,
        x = VIRTUAL_WIDTH + 8,
        y = (lane - 1) * line_height() + font_size,
        width = width,
        speed = speed,
    }
    table.remove(pending, 1)
    stats.shown = stats.shown + 1
    next_launch_at = current_motion_time + 1 / option_number('max_launch_rate', 10, 0.5, 60)
end

local function render_once()
    if #active == 0 then
        clear_overlay_once()
        return
    end
    local alpha = math.floor((1 - option_number('opacity', 0.55, 0, 1)) * 255 + 0.5)
    local font_size = option_number('font_size', 16, 8, 160) * option_number('font_scale', 1, 0.25, 4)
    local outline = option_number('outline', 1, 0, 20)
    local shadow = option_number('shadow', 0, 0, 20)
    local bold = settings.bold and 1 or 0
    local font = ass_font_escape(settings.font)
    local lines = {}
    for _, item in ipairs(active) do
        lines[#lines + 1] = string.format(
            '{\\an7\\pos(%.1f,%.1f)\\fn%s\\fs%.1f\\bord%.1f\\shad%.1f\\b%d\\alpha&H%02X&\\1c&H%s&\\3c&H000000&\\4c&H000000&}%s',
            item.x, item.y, font, font_size, outline, shadow, bold, alpha, item.color, item.text
        )
    end
    overlay.res_x = VIRTUAL_WIDTH
    overlay.res_y = VIRTUAL_HEIGHT
    overlay.data = table.concat(lines, '\n')
    overlay:update()
    overlay_visible = true
    last_ass_data = overlay.data
    render_updates = render_updates + 1
end

tick = function()
    if not has_work() then
        stop_timer()
        clear_overlay_once()
        publish_stats()
        publish_test_state()
        return
    end
    if mp.get_property_native('pause') == true then
        last_tick_at = mp.get_time()
        stop_timer()
        publish_stats()
        publish_test_state()
        return
    end
    local now = mp.get_time()
    local delta = last_tick_at and clamp(now - last_tick_at, 0, 0.2) or 0
    last_tick_at = now
    motion_time = motion_time + delta
    for index = #active, 1, -1 do
        local item = active[index]
        item.x = item.x - item.speed * delta
        if item.x + item.width < 0 then
            table.remove(active, index)
        end
    end
    activate_pending(now, motion_time)
    render_once()
    if not has_work() then
        stop_timer()
    end
    publish_stats()
    publish_test_state()
end

local function receive_batch(id, encoded)
    if not session_id or tostring(id or '') ~= session_id then
        return
    end
    if type(encoded) ~= 'string' or #encoded > MAX_BATCH_BYTES then
        return
    end
    local messages = utils.parse_json(encoded)
    if type(messages) ~= 'table' then
        return
    end
    for _, message in ipairs(messages) do
        if type(message) == 'table' and type(message.text) == 'string' then
            enqueue_message(message)
        end
    end
    publish_stats()
    start_timer()
end

local function clear_session()
    pending = {}
    active = {}
    lane_ready_at = {}
    next_lane = 1
    next_launch_at = 0
    motion_time = 0
    stats = {
        received = 0,
        shown = 0,
        dropped_stale = 0,
        dropped_overflow = 0,
    }
    last_stats_payload = ''
    stop_timer()
    clear_overlay_once()
    publish_stats()
end

local function start(id)
    local next_id = tostring(id or '')
    if next_id == '' then
        return
    end
    if session_id == next_id then
        return
    end
    clear_session()
    session_id = next_id
    publish_route()
    publish_test_state()
end

local function stop(id)
    if not session_id or (id ~= nil and tostring(id) ~= session_id) then
        return
    end
    clear_session()
    session_id = nil
    publish_route()
    publish_test_state()
end

local function ping(id)
    if session_id and tostring(id or '') == session_id then
        publish_test_state()
    end
end

mp.observe_property('display-fps', 'native', function()
    if render_timer and has_work() then
        render_timer.timeout = 1 / target_render_fps()
    end
    publish_test_state()
end)

mp.observe_property('user-data/douyu_danmaku/test-display-fps', 'native', function()
    if is_test_mode() and render_timer and has_work() then
        render_timer.timeout = 1 / target_render_fps()
    end
    publish_test_state()
end)

mp.observe_property('pause', 'native', function(_, paused)
    if paused == true then
        stop_timer()
    elseif has_work() then
        start_timer()
    end
    publish_test_state()
end)

mp.register_script_message('start', start)
mp.register_script_message('stop', stop)
mp.register_script_message('batch', receive_batch)
mp.register_script_message('ping', ping)
mp.register_event('end-file', function() stop() end)
mp.register_event('shutdown', function() stop() end)

publish_route()
