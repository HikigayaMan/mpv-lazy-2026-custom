local mp = require 'mp'
local utils = require 'mp.utils'
local tree = require 'lib/web_playlist_tree'
local M = {}
local MANIFEST = 'user-data/mpv-lazy/web-playlist-manifest'
local STATUS = 'user-data/uosc/web-playlist-menu'
local METADATA_STATE = 'user-data/mpv-lazy/web-playlist/metadata-state'
local menu, model, manifest, playlist_id, current_value
local expanded = {}

local function metadata_status()
    local state = mp.get_property_native(METADATA_STATE)
    if type(state) == 'table' and state.playlistId == playlist_id
        and type(state.revision) == 'number' and state.revision >= (manifest.revision or 0)
        and (state.status == 'pending' or state.status == 'failed') then return state.status end
    return manifest.metadataPending == true and 'pending' or 'ready'
end

local function available()
    local value = mp.get_property_native(MANIFEST)
    if type(value) ~= 'table' or type(value.items) ~= 'table' or #value.items == 0 then return end
    for _, entry in ipairs(mp.get_property_native('playlist') or {}) do
        if tree.item_from_path(entry.filename, value.playlistId) then return value end
    end
end

local function diagnostics()
    if not model then return end
    local rows = {}
    -- Bounded support data, never the media URLs or the entire large manifest.
    for index = 1, math.min(50, #model.rows) do
        local row = model.rows[index]
        rows[index] = {value = row.value, title = row.title, depth = row.depth, hint = row.hint,
            active = row.active, kind = row.kind, expanded = model.groups[row.value] and expanded[row.value] == true}
    end
    mp.set_property_native(STATUS, {playlistId = playlist_id, visibleCount = #model.rows,
        videoCount = model.videoCount, partCount = model.partCount, currentValue = model.currentValue,
        metadataStatus = metadata_status(), rows = rows})
end

local function data_for()
    local title = model.collectionTitle ~= '' and model.collectionTitle or ulang._playlist_submenu_title
    local status = metadata_status()
    local footnote = '点击多 P 视频展开／收起；点击视频或分 P 播放'
    if status == 'pending' then
        title = title .. '（合集加载中）'
        footnote = '完整合集正在后台加载；当前视频可正常播放'
    elseif status == 'failed' then
        title = title .. '（合集加载失败）'
        footnote = '完整合集加载失败；当前视频可继续播放，请在原网页重试'
    elseif model.collectionTitle ~= '' then
        title = title .. ' (' .. tostring(model.currentVideoIndex or 0) .. '/' .. model.videoCount .. ')'
    end
    return {type = 'playlist', title = title, items = model.rows, item_actions = {}, keep_open = true,
        footnote = footnote, search_style = 'on_demand'}
end

local function visible_focus(value, previous)
    local visible = {}
    for _, row in ipairs(model.rows) do visible[row.value] = true end
    if visible[value] then return value end
    local parents = model.parentKeys[value] or (previous and previous.parentKeys[value]) or {}
    for index = #parents, 1, -1 do if visible[parents[index]] then return parents[index] end end
    if value ~= model.currentValue and model.currentValue then return visible_focus(model.currentValue) end
    return model.rows[1] and model.rows[1].value
end

local function rebuild(focus)
    local value = available()
    if not value then
        if menu then menu:close() end
        return
    end
    manifest = value
    if playlist_id ~= manifest.playlistId then
        playlist_id, expanded, current_value = manifest.playlistId, {}, nil
    end
    local native = mp.get_property_native('playlist') or {}
    local previous = model
    model = tree.build(manifest, native, expanded)
    if model.currentValue ~= current_value then
        current_value = model.currentValue
        for _, key in ipairs(model.parentKeys[current_value] or {}) do expanded[key] = true end
        model = tree.build(manifest, native, expanded)
    end
    if menu then
        if not focus and menu.current and menu.current.selected_index then
            local selected = menu.current.items[menu.current.selected_index]
            focus = selected and selected.value
        end
        menu:update(data_for())
        menu:select_value(visible_focus(focus or model.currentValue, previous))
    end
    diagnostics()
end

function M.activate(value)
    if not menu or not model or type(value) ~= 'string' then return end
    if model.groups[value] then
        expanded[value] = not expanded[value]
        rebuild(value)
    else
        local binding = model.bindings[value]
        if not binding then return end
        local index = tree.resolve_index(binding, mp.get_property_native('playlist') or {})
        if index ~= nil then mp.commandv('set', 'playlist-pos', tostring(index)) end
    end
end

local function update()
    if menu and not menu.destroyed and not menu.is_closing then rebuild() end
end
local function close()
    mp.unobserve_property(update)
    menu, model = nil, nil
    mp.set_property_native(STATUS, nil)
end

local function open(value)
    manifest = value
    rebuild()
    if not model then return false end
    menu = Menu:open(data_for(), function(event)
        if event.type == 'activate' and not event.action then M.activate(event.value)
        elseif event.type == 'close' then close() end
    end)
    menu:select_value(visible_focus(model.currentValue))
    menu:scroll_to_index(menu.current.selected_index, nil, true)
    mp.observe_property('playlist', 'native', update)
    mp.observe_property(MANIFEST, 'native', update)
    mp.observe_property(METADATA_STATE, 'native', update)
    return true
end

function M.open()
    local value = available()
    if not value then return false end
    if Menu:is_open('playlist') then Menu:close(); return true end
    return open(value)
end

-- A fast user can open the flat list before the asynchronous manifest commits.
-- Upgrade that already-open list once metadata and native entries are ready.
local function upgrade()
    if not menu and Menu:is_open('playlist') then
        local value = available()
        if value then open(value) end
    end
end
mp.observe_property(MANIFEST, 'native', upgrade)
mp.observe_property('playlist', 'native', upgrade)
mp.register_script_message('web-playlist-menu-action', function(raw)
    local event = utils.parse_json(raw or '')
    if type(event) == 'table' and event.type == 'activate' then M.activate(event.value) end
end)

return M
