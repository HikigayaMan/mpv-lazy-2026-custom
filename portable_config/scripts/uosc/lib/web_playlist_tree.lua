-- Presentation tree only. Playback order and entry IDs remain owned by MPV.
local M = {}

local function duration_hint(value)
    local seconds = math.max(0, math.floor(tonumber(value) or 0))
    if seconds == 0 then return '' end
    if seconds >= 3600 then
        return string.format('%d:%02d:%02d', math.floor(seconds / 3600), math.floor(seconds / 60) % 60, seconds % 60)
    end
    return string.format('%02d:%02d', math.floor(seconds / 60), seconds % 60)
end

function M.item_from_path(path, playlist_id)
    if type(path) ~= 'string' or type(playlist_id) ~= 'string' then return nil end
    local port, id, item = path:match('^http://127%.0%.0%.1:(%d+)/web%-playlist/(playlist%-%x+)/item/(%d+:%d+)$')
    if id == playlist_id and tonumber(port) >= 1 and tonumber(port) <= 65535 then return item end
end

function M.build(manifest, playlist, expanded)
    expanded = expanded or {}
    local result = {rows = {}, bindings = {}, groups = {}, parentKeys = {},
        videoCount = 0, partCount = 0, collectionTitle = manifest.collectionTitle or ''}
    local by_item, unknown, occurrences = {}, {}, {}
    for index, entry in ipairs(playlist or {}) do
        local item_id = M.item_from_path(entry.filename, manifest.playlistId)
        occurrences[entry.filename or ''] = (occurrences[entry.filename or ''] or 0) + 1
        local value = entry.id ~= nil and ('native:' .. tostring(entry.id)) or
            (item_id and ('item:' .. item_id .. ':' .. occurrences[entry.filename]) or ('external:' .. index))
        local binding = {nativeId = entry.id, filename = entry.filename, itemId = item_id,
            occurrence = occurrences[entry.filename or ''], current = entry.current == true, nativeIndex = index}
        result.bindings[value], result.parentKeys[value] = binding, {}
        binding.value, binding.nativeTitle = value, entry.title
        if binding.current then result.currentValue = value end
        if item_id then
            by_item[item_id] = by_item[item_id] or {}
            by_item[item_id][#by_item[item_id] + 1] = binding
        else
            unknown[#unknown + 1] = binding
        end
    end

    local sections, section_map, consumed, ordered = {}, {}, {}, {}
    for index, item in ipairs(manifest.items or {}) do
        local order = tonumber(item.order)
        if not order or order ~= order or order < 0 or order == math.huge then order = index end
        ordered[#ordered + 1] = {item = item, order = order, index = index}
    end
    table.sort(ordered, function(a, b) return a.order == b.order and a.index < b.index or a.order < b.order end)
    for _, record in ipairs(ordered) do
        local item = record.item
        local native = by_item[item.itemId]
        if native and not consumed[item.itemId] then
            consumed[item.itemId] = true
            local section_id = tostring(item.sectionId or 'default')
            local section = section_map[section_id]
            if not section then
                section = {id = section_id, title = item.sectionTitle or '', videos = {}, video_map = {}}
                sections[#sections + 1], section_map[section_id] = section, section
            end
            local video_id = tostring(item.videoId or item.itemId:match('^(%d+):'))
            local video = section.video_map[video_id]
            if not video then
                video = {id = video_id, title = item.videoTitle or item.title or '', leaves = {}, partCount = 1}
                section.videos[#section.videos + 1], section.video_map[video_id] = video, video
                result.videoCount = result.videoCount + 1
                video.ordinal = result.videoCount
            end
            video.partCount = math.max(video.partCount, tonumber(item.partCount) or 1, tonumber(item.p) or 1)
            for _, binding in ipairs(native) do
                binding.item = item
                video.leaves[#video.leaves + 1] = binding
                result.partCount = result.partCount + 1
                if binding.current then
                    video.active, section.active = true, true
                    result.currentVideoIndex = video.ordinal
                end
            end
        end
    end
    -- Metadata can arrive after the native list, or users can append media.
    -- Keep these entries selectable rather than losing them from the menu.
    for item_id, native in pairs(by_item) do
        if not consumed[item_id] then
            for _, binding in ipairs(native) do unknown[#unknown + 1] = binding end
        end
    end
    table.sort(unknown, function(a, b) return a.nativeIndex < b.nativeIndex end)

    local function group(value, title, depth, parents, active, count)
        result.groups[value] = {value = value, title = title, depth = depth, active = active,
            expanded = expanded[value] == true, count = count}
        result.parentKeys[value] = parents
        return {value = value, title = title, depth = depth, active = active,
            hint = tostring(count) .. (value:match('^video:') and 'P' or ' 个视频'),
            icon = expanded[value] and 'expand_more' or 'chevron_right',
            keep_open = true, actions = {}, kind = 'group', reserve_hint = true}
    end
    local function leaf(binding, title, depth, parents)
        result.parentKeys[binding.value] = parents
        return {value = binding.value, title = title, depth = depth, active = binding.current,
            hint = duration_hint(binding.item and binding.item.duration), keep_open = true, actions = {}, kind = 'leaf', reserve_hint = true}
    end
    local section_level = #sections > 1
    for _, section in ipairs(sections) do
        local section_key = 'section:' .. section.id
        local section_row = section_level and group(section_key,
            section.title ~= '' and section.title or '分组', 0, {}, section.active, #section.videos)
        if section_row then result.rows[#result.rows + 1] = section_row end
        local section_open = not section_level or expanded[section_key] == true
        local depth = section_level and 1 or 0
        for _, video in ipairs(section.videos) do
            local video_key = 'video:' .. section.id .. ':' .. video.id
            local parents = section_level and {section_key} or {}
            local multi = video.partCount > 1 or #video.leaves > 1
            local video_row = multi and group(video_key, video.title, depth, parents, video.active, #video.leaves)
            if section_open and video_row then result.rows[#result.rows + 1] = video_row end
            for _, binding in ipairs(video.leaves) do
                local ancestors = {}
                for _, key in ipairs(parents) do ancestors[#ancestors + 1] = key end
                if multi then ancestors[#ancestors + 1] = video_key end
                local title = multi and (binding.item.partTitle or binding.item.title or ('P' .. tostring(binding.item.p))) or video.title
                local row = leaf(binding, title, depth + (multi and 1 or 0), ancestors)
                if section_open and (not multi or expanded[video_key] == true) then
                    result.rows[#result.rows + 1] = row
                end
            end
        end
    end
    for _, binding in ipairs(unknown) do
        result.rows[#result.rows + 1] = leaf(binding,
            binding.nativeTitle or (binding.itemId and ('视频 ' .. binding.itemId)) or
                tostring(binding.filename or ''):match('[^/\\]+$') or '视频', 0, {})
    end
    return result
end

-- Re-scan at activation time: visible row numbers are not native indices.
function M.resolve_index(binding, playlist)
    local occurrence = 0
    for index, entry in ipairs(playlist or {}) do
        if binding.nativeId ~= nil then
            if entry.id == binding.nativeId then return index - 1 end
        elseif entry.filename == binding.filename then
            occurrence = occurrence + 1
            if occurrence == binding.occurrence then return index - 1 end
        end
    end
end

return M
