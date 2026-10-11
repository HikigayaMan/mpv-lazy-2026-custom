-- Keep user preferences across MPV processes. Menu commands explicitly name
-- their changed properties so automatic profiles and danmaku filters are not
-- mistaken for user choices. Existing flat saved-props.json files still work.
local mp = require "mp"
local utils = require "mp.utils"
local msg = require "mp.msg"
local opt = {
    load = true, save_mode = 2, props = "volume,mute", dup_block = false,
    cache_dir = "~~/", save_delay = 0.2,
    menu_props = "vf,glsl-shaders,hwdec,deband,deinterlace,icc-profile-auto,correct-pts,contrast,brightness,gamma,saturation,hue",
}
require("mp.options").read_options(opt)
if not opt.load then return end

local function split(value)
    local result, seen = {}, {}
    for name in tostring(value or ""):gmatch("[^,]+") do
        name = name:match("^%s*(.-)%s*$")
        if name ~= "" and not seen[name] then
            seen[name] = true
            result[#result + 1] = name
        end
    end
    return result, seen
end
local tracked, tracked_set = split(opt.props)
local menu_props, menu_set = split(opt.menu_props)
local _, watch_later = split(mp.get_property("watch-later-options", ""))
for _, name in ipairs(tracked) do
    if watch_later[name] then
        msg.warn("全局保存与 watch-later-options 重合：" .. name)
        if opt.dup_block then return end
    end
end

local path = mp.command_native({"expand-path", opt.cache_dir .. "saved-props.json"})
local backup_path = path .. ".bak"
local temp_path = path .. ".tmp-" .. tostring(utils.getpid())
local saved, dirty, observed = {}, {}, {}
local save_timer, restore_timer
local STATUS_PROPERTY = "user-data/save_global_props/status"
local TEMP_FILTERS = {danmaku = true, danmaku_smooth = true}
mp.set_property_native("user-data/save_global_props/path", path)

local function clone(value)
    if type(value) ~= "table" then return value end
    local result = {}
    for key, item in pairs(value) do result[key] = clone(item) end
    -- MPV tags native maps/arrays with metatables. Preserve the tag so empty
    -- filter parameter maps remain JSON objects and can be restored natively.
    return setmetatable(result, getmetatable(value))
end
local function equal(left, right)
    if type(left) ~= type(right) then return false end
    if type(left) ~= "table" then return left == right end
    for key, value in pairs(left) do if not equal(value, right[key]) then return false end end
    for key in pairs(right) do if left[key] == nil then return false end end
    return true
end
local function finite(value)
    return type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge
end
local function temporary_filter(filter)
    return type(filter) == "table" and TEMP_FILTERS[tostring(filter.label or ""):gsub("^@", "")] == true
end
local function preference(name, value)
    if value == nil then return nil end
    if name == "vf" then
        if type(value) ~= "table" then return nil end
        local filters = {}
        for _, filter in ipairs(value) do
            if type(filter) ~= "table" or type(filter.name) ~= "string" then return nil end
            if not temporary_filter(filter) then filters[#filters + 1] = clone(filter) end
        end
        return filters
    end
    if name == "glsl-shaders" then
        if type(value) ~= "table" then return nil end
        for _, shader in ipairs(value) do
            if type(shader) ~= "string" or shader:find("[%z\r\n]") then return nil end
        end
        return clone(value)
    end
    if name == "contrast" or name == "brightness" or name == "gamma" or name == "saturation" or name == "hue" then
        return finite(value) and value >= -100 and value <= 100 and value or nil
    end
    if type(value) == "number" and not finite(value) then return nil end
    if type(value) == "string" and value:find("[%z\r\n]") then return nil end
    if type(value) == "boolean" or type(value) == "number" or type(value) == "string" then return value end
    return nil
end

local function read_json(filename)
    local file = io.open(filename, "rb")
    if not file then return nil end
    local content = file:read(1024 * 1024 + 1)
    file:close()
    if not content or #content > 1024 * 1024 then return nil end
    local result = utils.parse_json(content)
    if type(result) ~= "table" then return nil end
    return result
end
local function read_saved()
    return read_json(path) or read_json(backup_path) or {}
end
local function publish(status)
    mp.set_property_native(STATUS_PROPERTY, status)
end
local function write_saved(value)
    local encoded_ok, encoded = pcall(utils.format_json, value)
    if not encoded_ok or type(encoded) ~= "string" then return false end
    local file = io.open(temp_path, "wb")
    if not file then return false end
    local write_ok, written = pcall(file.write, file, encoded)
    local close_ok, closed = pcall(file.close, file)
    if not write_ok or not written or not close_ok or not closed then os.remove(temp_path); return false end
    -- rename replaces directly on POSIX. Windows needs the existing target
    -- moved aside first; retain a recoverable previous copy and roll back if
    -- promoting the completed temporary file fails.
    if os.rename(temp_path, path) then return true end
    local existing = io.open(path, "rb")
    if not existing then os.remove(temp_path); return false end
    existing:close()
    os.remove(backup_path)
    if not os.rename(path, backup_path) then os.remove(temp_path); return false end
    if os.rename(temp_path, path) then return true end
    os.rename(backup_path, path)
    os.remove(temp_path)
    return false
end
local function flush()
    if save_timer then save_timer:kill(); save_timer = nil end
    if next(dirty) == nil then return true end
    -- Merge only changed keys into the latest file. A second player must not
    -- undo choices made by another player just by exiting with old defaults.
    local merged = read_saved()
    for name, value in pairs(dirty) do merged[name] = clone(value) end
    if not write_saved(merged) then
        publish("save-error")
        msg.error("设置自动保存失败：" .. path)
        mp.osd_message("设置自动保存失败，请检查配置目录写入权限", 3)
        return false
    end
    dirty = {}
    publish("saved")
    return true
end
local function schedule_save()
    if save_timer then save_timer:kill() end
    local delay = tonumber(opt.save_delay) or 0.2
    save_timer = mp.add_timeout(math.max(0, math.min(delay, 2)), flush)
    publish("pending")
end
local function remember(names)
    local changed = false
    local latest = read_saved()
    for _, name in ipairs(split(names)) do
        if menu_set[name] or tracked_set[name] then
            local value = preference(name, mp.get_property_native(name))
            if value ~= nil then
                -- Explicitly choosing the current default must also persist:
                -- the choice should survive different automatic profiles.
                if saved[name] == nil or not equal(saved[name], value) or not equal(latest[name], value) then
                    saved[name], dirty[name] = clone(value), clone(value)
                    changed = true
                end
            end
        end
    end
    if changed then schedule_save() end
end
local function restore_one(name, value)
    value = preference(name, value)
    if value == nil then return end
    local current = mp.get_property_native(name)
    if equal(preference(name, current), value) then return end
    if name == "vf" then
        for _, filter in ipairs(type(current) == "table" and current or {}) do
            if temporary_filter(filter) then value[#value + 1] = clone(filter) end
        end
    end
    if tracked_set[name] then observed[name] = clone(value) end
    local ok, result = pcall(mp.set_property_native, name, value)
    if not ok or result ~= true then msg.warn("无法恢复已保存设置：" .. name) end
end

saved = read_saved()
for _, name in ipairs(tracked) do
    if saved[name] ~= nil then restore_one(name, saved[name]) end
    observed[name] = clone(mp.get_property_native(name))
end
for _, name in ipairs(menu_props) do
    if saved[name] ~= nil then restore_one(name, saved[name]) end
end
publish("loaded")

for _, name in ipairs(tracked) do
    mp.observe_property(name, "native", function(_, value)
        value = preference(name, value)
        if value ~= nil and not equal(observed[name], value) then
            observed[name] = clone(value)
            saved[name], dirty[name] = clone(value), clone(value)
            if opt.save_mode == 2 then schedule_save() end
        end
    end)
end
mp.register_script_message("remember", remember)
mp.register_event("file-loaded", function()
    if restore_timer then restore_timer:kill() end
    -- Let file setup and automatic profiles settle before restoring an
    -- explicit preference. Properties never chosen by the user stay automatic.
    restore_timer = mp.add_timeout(0.1, function()
        restore_timer = nil
        for _, name in ipairs(menu_props) do
            if saved[name] ~= nil then restore_one(name, saved[name]) end
        end
    end)
end)
mp.register_event("shutdown", flush)
mp.register_script_message("clean_data", function()
    if save_timer then save_timer:kill(); save_timer = nil end
    if restore_timer then restore_timer:kill(); restore_timer = nil end
    if not write_saved({}) then
        publish("clear-error")
        mp.osd_message("设置记录清除失败，请检查配置目录写入权限", 3)
        return
    end
    saved, dirty = {}, {}
    -- Do not let the recovery copy bring cleared preferences back.
    os.remove(backup_path)
    publish("cleared")
    mp.osd_message("已清除保存的设置；下次启动使用配置默认值", 3)
end)
