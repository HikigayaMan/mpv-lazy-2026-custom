local mp = require 'mp'
local utils = require 'mp.utils'

local script_name = mp.get_script_name()
local current_config_path = mp.command_native({ 'expand-path', '~~/script-opts/rife_sr_runtime.json' })
local default_config_path = mp.command_native({ 'expand-path', '~~/script-opts/rife_sr_runtime_default.json' })
local runtime_vpy = '~~/vs/MEMC_RIFE_SR_runtime.vpy'
local smooth_fps_suspend = 'user-data/uosc_danmaku/suspend-smooth-fps'
local first_apply = true

local default_config = { sr_preset = 'uai_rtx_default', rife_model = 46 }
local sr_presets = {
    { id = 'uai_rtx_default', label = 'UAI RTX 默认', hint = '1080P 到 1440P' },
    { id = 'animejanai_v31_balanced', label = 'AnimeJaNai V3.1 Balanced', hint = '1080P 到 4K' },
    { id = 'animejanai_v31_balanced_sharp', label = 'AnimeJaNai V3.1 Balanced Sharp', hint = '1080P 到 4K' },
    { id = 'animejanai_v31_performance', label = 'AnimeJaNai V3.1 Performance', hint = '1080P 到 4K' },
    { id = 'animejanai_v31_performance_sharp', label = 'AnimeJaNai V3.1 Performance Sharp', hint = '1080P 到 4K' },
    { id = 'realesrgan_x4v3_540', label = 'RealESRGAN x4v3 540P', hint = '540P 到 4K' },
    { id = 'realesrgan_x4v3_720', label = 'RealESRGAN x4v3 720P', hint = '720P 到 4K' },
    { id = 'realesrgan_x4v3_1080', label = 'RealESRGAN x4v3 1080P', hint = '1080P 到 4K' },
    { id = 'liveaction_span_540', label = 'LiveAction SPAN 540P', hint = '540P 到 1080P' },
    { id = 'liveaction_span_720', label = 'LiveAction SPAN 720P', hint = '720P 到 1440P' },
    { id = 'liveaction_span_1080', label = 'LiveAction SPAN 1080P', hint = '1080P 到 4K' },
    { id = 'starsample_1x', label = 'StarSample 1x', hint = '1080P 修复，不放大' },
    { id = 'starsample_2x_540', label = 'StarSample 2x 540P', hint = '540P 到 1080P' },
    { id = 'starsample_2x_720', label = 'StarSample 2x 720P', hint = '720P 到 1440P' },
    { id = 'starsample_2x_1080', label = 'StarSample 2x 1080P', hint = '1080P 到 4K' },
}
local rife_models = {
    { id = 46, label = '4.6', hint = '性能组合' },
    { id = 4151, label = '4.15 lite', hint = '2025 轻量' },
    { id = 422, label = '4.22', hint = '2025' },
    { id = 4221, label = '4.22 lite', hint = '2025 轻量' },
    { id = 4251, label = '4.25 lite', hint = '2025 轻量' },
    { id = 426, label = '4.26', hint = '2025' },
    { id = 4262, label = '4.26 heavy', hint = '2025 高负载' },
    { id = 47, label = '4.7', hint = '2026' },
    { id = 48, label = '4.8', hint = '2026' },
    { id = 49, label = '4.9', hint = '2026' },
}

local valid_sr, valid_models, sr_labels, model_labels = {}, {}, {}, {}
for _, preset in ipairs(sr_presets) do
    valid_sr[preset.id] = true
    sr_labels[preset.id] = preset.label
end
for _, model in ipairs(rife_models) do
    valid_models[model.id] = true
    model_labels[model.id] = model.label
end

local function copy_default()
    return { sr_preset = default_config.sr_preset, rife_model = default_config.rife_model }
end

local function normalize_config(value)
    if type(value) ~= 'table' then return copy_default() end
    local allowed = { sr_preset = true, rife_model = true }
    for key in pairs(value) do
        if not allowed[key] then return copy_default() end
    end
    if type(value.sr_preset) ~= 'string' or not valid_sr[value.sr_preset] then return copy_default() end
    if type(value.rife_model) ~= 'number' or value.rife_model ~= math.floor(value.rife_model) then return copy_default() end
    if not valid_models[value.rife_model] then return copy_default() end
    return { sr_preset = value.sr_preset, rife_model = value.rife_model }
end

local function read_config(path)
    local file = io.open(path, 'rb')
    if not file then return copy_default() end
    local content = file:read('*all')
    file:close()
    local ok, value = pcall(utils.parse_json, content)
    if not ok then return copy_default() end
    return normalize_config(value)
end

local function path_exists(path)
    local file = io.open(path, 'rb')
    if not file then return false end
    file:close()
    return true
end

local function remove_if_present(path)
    if not path_exists(path) then return true end
    return os.remove(path) ~= nil
end

local function write_config(path, value)
    local config = normalize_config(value)
    local temp_path = path .. '.tmp'
    local backup_path = path .. '.bak'

    -- Recover from an interrupted replacement before starting another one.
    if path_exists(backup_path) then
        if not path_exists(path) then
            if not os.rename(backup_path, path) then
                mp.osd_message('超分补帧组合：无法恢复旧配置', 4)
                return false
            end
        elseif not remove_if_present(backup_path) then
            mp.osd_message('超分补帧组合：无法清理旧备份', 4)
            return false
        end
    end
    if not remove_if_present(temp_path) then
        mp.osd_message('超分补帧组合：无法清理临时配置', 4)
        return false
    end

    local file = io.open(temp_path, 'wb')
    if not file then
        mp.osd_message('超分补帧组合：无法写入配置', 4)
        return false
    end
    file:write(utils.format_json(config))
    file:flush()
    file:close()

    local had_previous = path_exists(path)
    if had_previous and not os.rename(path, backup_path) then
        os.remove(temp_path)
        mp.osd_message('超分补帧组合：无法备份旧配置', 4)
        return false
    end

    if not os.rename(temp_path, path) then
        local restored = not had_previous or os.rename(backup_path, path)
        os.remove(temp_path)
        if not restored then
            mp.osd_message('超分补帧组合：写入失败且旧配置恢复失败', 4)
        else
            mp.osd_message('超分补帧组合：写入失败，已恢复旧配置', 4)
        end
        return false
    end

    if had_previous and not remove_if_present(backup_path) then
        -- Do not report success while a backup is stranded. Restore the old
        -- file when possible and remove every replacement artifact.
        local moved_new = os.rename(path, temp_path)
        local restored = os.rename(backup_path, path)
        if moved_new then os.remove(temp_path) end
        if not restored then
            mp.osd_message('超分补帧组合：备份清理失败', 4)
        else
            mp.osd_message('超分补帧组合：已恢复旧配置', 4)
        end
        return false
    end
    return true
end

local function describe(config)
    return string.format('%s + RIFE %s', sr_labels[config.sr_preset], model_labels[config.rife_model])
end

local function update_pending(field, value)
    local config = read_config(current_config_path)
    config[field] = value
    if write_config(current_config_path, config) then
        mp.osd_message('待应用：' .. describe(config), 3)
    end
end

local function has_conflicting_video_filter()
    local filters = mp.get_property_native('vf') or {}
    for _, filter in ipairs(filters) do
        local label = filter.label
        local name = filter.name
        local params = filter.params or {}
        if label ~= 'rife_sr_runtime' and label ~= 'danmaku_smooth' then
            if name == 'vapoursynth' then return true end
            if name == 'd3d11vpp' and params.scale ~= nil then
                local scale = tonumber(params.scale)
                if not scale or scale ~= 1 then return true end
            end
        end
    end
    return false
end

local function apply_current()
    if has_conflicting_video_filter() then
        mp.osd_message('请先关闭现有补帧/超分滤镜，不能叠加组合', 4)
        return
    end
    local config = read_config(current_config_path)
    if not write_config(current_config_path, config) then return end
    mp.set_property_bool(smooth_fps_suspend, true)
    mp.commandv('vf', 'remove', '@danmaku_smooth')
    mp.commandv('vf', 'remove', '@rife_sr_runtime')
    mp.commandv('vf', 'append', '@rife_sr_runtime:vapoursynth=' .. runtime_vpy)
    if first_apply then
        mp.osd_message('正在构建超分补帧组合：' .. describe(config), 4)
        first_apply = false
    else
        mp.osd_message('已应用：' .. describe(config), 3)
    end
    mp.add_timeout(0.2, function()
        mp.set_property_bool(smooth_fps_suspend, false)
    end)
end

local function save_default()
    local config = read_config(current_config_path)
    if write_config(default_config_path, config) then
        mp.osd_message('已设为默认：' .. describe(config), 3)
    end
end

local function restore_default()
    local config = read_config(default_config_path)
    if write_config(current_config_path, config) then
        mp.osd_message('默认已恢复到待应用：' .. describe(config), 3)
    end
end

local function disable_combo()
    mp.set_property_bool(smooth_fps_suspend, false)
    mp.commandv('vf', 'remove', '@rife_sr_runtime')
    mp.osd_message('已关闭超分补帧组合', 3)
end

local function open_menu()
    local current = read_config(current_config_path)
    local sr_items, rife_items = {}, {}
    for _, preset in ipairs(sr_presets) do
        sr_items[#sr_items + 1] = {
            title = preset.label, hint = preset.hint, active = current.sr_preset == preset.id,
            value = { 'script-message-to', script_name, 'select-sr', preset.id },
        }
    end
    for _, model in ipairs(rife_models) do
        rife_items[#rife_items + 1] = {
            title = 'RIFE ' .. model.label, hint = model.hint, active = current.rife_model == model.id,
            value = { 'script-message-to', script_name, 'select-rife', tostring(model.id) },
        }
    end
    local menu = {
        type = 'rife-sr-runtime-menu', title = '超分补帧：自定义组合',
        search_style = 'disabled', search_submenus = true,
        items = {
            { title = '应用当前组合', hint = describe(current), value = { 'script-message-to', script_name, 'apply-current' } },
            { title = '选择超分预设', hint = sr_labels[current.sr_preset], items = sr_items },
            { title = '选择 RIFE NV 模型', hint = 'RIFE ' .. model_labels[current.rife_model], items = rife_items },
            { title = '保存为默认', hint = '保存当前待应用组合', value = { 'script-message-to', script_name, 'save-default' } },
            { title = '恢复默认', hint = '恢复到待应用，不立即重建', value = { 'script-message-to', script_name, 'restore-default' } },
            { title = '关闭组合', hint = '移除本组合滤镜', value = { 'script-message-to', script_name, 'disable' } },
        },
    }
    mp.commandv('script-message-to', 'uosc', 'open-menu', utils.format_json(menu))
end

mp.register_script_message('open-menu', open_menu)
mp.register_script_message('apply-current', apply_current)
mp.register_script_message('select-sr', function(id)
    if valid_sr[id] then update_pending('sr_preset', id) end
end)
mp.register_script_message('select-rife', function(id)
    local model = tonumber(id)
    if model and valid_models[model] then update_pending('rife_model', model) end
end)
mp.register_script_message('save-default', save_default)
mp.register_script_message('restore-default', restore_default)
mp.register_script_message('disable', disable_combo)
mp.add_key_binding(nil, 'open-menu', open_menu)
