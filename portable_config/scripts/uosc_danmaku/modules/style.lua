local mp = require("mp")
local utils = require("mp.utils")
local msg = require("mp.msg")

local STYLE_PATH_PROPERTY = "user-data/uosc_danmaku/style-defaults-path"
local STYLE_STATUS_PROPERTY = "user-data/uosc_danmaku/style-defaults-status"
local LIVE_FPS_MODE_PROPERTY = "user-data/uosc_danmaku/live-fps-mode"

local STYLE_KEYS = {
    "bold",
    "fontname",
    "fontsize",
    "outline",
    "shadow",
    "scrolltime",
    "opacity",
    "displayarea",
    "live_fps_mode",
}

local LIVE_FPS_MODES = {
    auto = true,
    ["60"] = true,
    ["80"] = true,
    ["120"] = true,
    display = true,
}

local function style_path()
    local configured = tostring(options.style_path or "")
    local path = configured ~= "" and configured or "~~/script-opts/uosc_danmaku_style.json"
    return mp.command_native({ "expand-path", path })
end

local function publish_style_state(status)
    mp.set_property_native(STYLE_PATH_PROPERTY, style_path())
    mp.set_property_native(STYLE_STATUS_PROPERTY, status)
    mp.set_property_native(LIVE_FPS_MODE_PROPERTY, options.live_fps_mode)
end

local function is_finite_number(value)
    return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end

local function valid_style_value(key, value)
    if key == "bold" then
        return type(value) == "boolean"
    end
    if key == "fontname" then
        return type(value) == "string" and #value > 0 and #value <= 256 and not value:match("[%z\r\n]")
    end
    if key == "live_fps_mode" then
        return type(value) == "string" and LIVE_FPS_MODES[value] == true
    end
    if not is_finite_number(value) then
        return false
    end
    local ranges = {
        fontsize = { 1, 500 },
        outline = { 0, 20 },
        shadow = { 0, 20 },
        scrolltime = { 1, 120 },
        opacity = { 0, 1 },
        displayarea = { 0, 1 },
    }
    local range = ranges[key]
    return range and value >= range[1] and value <= range[2]
end

local function normalized_style_value(key, value)
    if key == "bold" or key == "fontname" or key == "live_fps_mode" then
        return value
    end
    return tonumber(value)
end

function load_danmaku_style_defaults()
    local path = style_path()
    local file = io.open(path, "r")
    if not file then
        publish_style_state("missing")
        return false
    end
    local content = file:read("*a")
    file:close()
    local decoded = utils.parse_json(content)
    if type(decoded) ~= "table" then
        msg.warn("danmaku style defaults ignored: invalid JSON")
        publish_style_state("invalid")
        return false
    end

    for _, key in ipairs(STYLE_KEYS) do
        local value = decoded[key]
        if value ~= nil and valid_style_value(key, value) then
            options[key] = normalized_style_value(key, value)
        end
    end
    publish_style_state("loaded")
    return true
end

function save_danmaku_style_defaults()
    local values = {}
    for _, key in ipairs(STYLE_KEYS) do
        local value = normalized_style_value(key, options[key])
        if not valid_style_value(key, value) then
            msg.error("danmaku style defaults not saved: invalid " .. key)
            publish_style_state("save-error")
            return false
        end
        values[key] = value
    end

    local encoded = utils.format_json(values)
    local file = encoded and io.open(style_path(), "w")
    if not file then
        msg.error("danmaku style defaults not saved: cannot write file")
        publish_style_state("save-error")
        return false
    end
    file:write(encoded)
    file:close()
    publish_style_state("saved")
    return true
end

function clear_danmaku_style_defaults()
    local path = style_path()
    local exists = io.open(path, "r")
    if exists then
        exists:close()
        if not os.remove(path) then
            msg.error("danmaku style defaults not cleared: cannot remove file")
            publish_style_state("clear-error")
            return false
        end
    end
    publish_style_state("cleared")
    return true
end

function set_live_fps_mode(mode)
    mode = tostring(mode or "")
    if not LIVE_FPS_MODES[mode] then
        return false
    end
    options.live_fps_mode = mode
    publish_style_state("changed")
    if refresh_live_danmaku_style then
        refresh_live_danmaku_style()
    end
    if sync_smooth_fps_filter then
        sync_smooth_fps_filter()
    end
    return true
end

function get_live_fps_modes()
    return LIVE_FPS_MODES
end

mp.register_script_message("save-danmaku-style-default", function()
    save_danmaku_style_defaults()
end)
mp.register_script_message("clear-danmaku-style-default", function()
    clear_danmaku_style_defaults()
end)
mp.register_script_message("set-live-fps-mode", set_live_fps_mode)

load_danmaku_style_defaults()
