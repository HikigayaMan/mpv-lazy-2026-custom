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

local last_saved_values = nil

local function same_style(left, right)
    if not left or not right then return false end
    for _, key in ipairs(STYLE_KEYS) do
        if left[key] ~= right[key] then return false end
    end
    return true
end

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
    if type(value) ~= "number" and type(value) ~= "string" then return nil end
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

    local loaded_values = {}
    local complete = true
    for _, key in ipairs(STYLE_KEYS) do
        local value = decoded[key]
        if value ~= nil and valid_style_value(key, value) then
            options[key] = normalized_style_value(key, value)
            loaded_values[key] = options[key]
        else
            complete = false
        end
    end
    last_saved_values = complete and loaded_values or nil
    publish_style_state("loaded")
    return true
end

function save_danmaku_style_defaults(force)
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

    if not force and same_style(values, last_saved_values) then
        publish_style_state("saved")
        return true
    end

    local encoded_ok, encoded = pcall(utils.format_json, values)
    local path = style_path()
    local suffix = tostring(utils.getpid())
    local temporary_path = path .. ".tmp." .. suffix
    local backup_path = path .. ".bak." .. suffix
    local file = encoded_ok and type(encoded) == "string" and io.open(temporary_path, "w")
    if not file then
        msg.error("danmaku style defaults not saved: cannot write file")
        publish_style_state("save-error")
        return false
    end
    local write_ok, written = pcall(file.write, file, encoded)
    local close_ok, closed = pcall(file.close, file)
    if not write_ok or not written or not close_ok or not closed then
        os.remove(temporary_path)
        msg.error("danmaku style defaults not saved: incomplete file write")
        publish_style_state("save-error")
        return false
    end

    local renamed = os.rename(temporary_path, path)
    if not renamed then
        -- Windows cannot rename over an existing file. Keep the previous file
        -- recoverable until the completely written replacement is installed.
        local existing = io.open(path, "r")
        local backup = io.open(backup_path, "r")
        if existing then existing:close() end
        if backup then backup:close() end
        if existing and not backup and os.rename(path, backup_path) then
            renamed = os.rename(temporary_path, path)
            if renamed then
                os.remove(backup_path)
            else
                os.rename(backup_path, path)
            end
        end
    end
    if not renamed then
        os.remove(temporary_path)
        msg.error("danmaku style defaults not saved: cannot replace file")
        publish_style_state("save-error")
        return false
    end
    last_saved_values = values
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
    last_saved_values = nil
    publish_style_state("cleared")
    return true
end

function set_danmaku_style_value(key, value)
    value = normalized_style_value(key, value)
    if not valid_style_value(key, value) then
        return false
    end
    options[key] = value
    local saved = save_danmaku_style_defaults()
    if refresh_danmaku_style then
        refresh_danmaku_style(key)
    end
    return true, saved
end

function set_live_fps_mode(mode)
    local changed, saved = set_danmaku_style_value("live_fps_mode", tostring(mode or ""))
    if not changed then return false end
    if not refresh_danmaku_style and refresh_live_danmaku_style then
        refresh_live_danmaku_style()
    end
    if sync_smooth_fps_filter then
        sync_smooth_fps_filter()
    end
    return true, saved
end

function get_live_fps_modes()
    return LIVE_FPS_MODES
end

mp.register_script_message("save-danmaku-style-default", function()
    if not save_danmaku_style_defaults(true) and show_message then
        show_message("保存样式失败；请检查配置目录是否可写", 3)
    end
end)
mp.register_script_message("clear-danmaku-style-default", function()
    if not clear_danmaku_style_defaults() and show_message then
        show_message("清除保存样式失败；请检查配置目录是否可写", 3)
    end
end)
mp.register_script_message("set-live-fps-mode", function(mode)
    local changed, saved = set_live_fps_mode(mode)
    if changed and not saved and show_message then
        show_message("弹幕帧率已更改，自动保存失败；请检查配置目录是否可写", 3)
    end
end)

load_danmaku_style_defaults()
