local msg = require "mp.msg"
local utils = require "mp.utils"

local function media_type_label(value)
    if value == "movie" or value == "mv" then return "电影" end
    if value == "show" then return "剧集" end
    return value
end

local function search_episodes(slug, season, type, id, title)
    local message = "正在获取剧集信息..."
    local menu_type = "trakt_episodes"
    local menu_title = "Trakt 剧集选择"
    local menu_footnote = "可使用 / 过滤"
    if uosc_available then
        update_menu_uosc(menu_type, menu_title, message, menu_footnote)
    else
        send_message(message)
    end

    local url = string.format("https://api.trakt.tv/shows/%s/seasons/%s/episodes", slug, season)
    local res = http_request("GET", url, {
            ["trakt-api-key"] = base64.decode(o.client_id),
            ["trakt-api-version"] = "2"
    })
    if not res or #res == 0 then
        local message = "没有找到结果"
        if uosc_available then
            update_menu_uosc(menu_type, menu_title, message, menu_footnote)
        else
            send_message(message, 3)
            msg.info(message)
        end
        return
    end
    local items = {}
    for _, item in ipairs(res) do
        table.insert(items, {
            title = string.format("第 %d 集：%s", item.number, clip_title(item.title) or "无标题"),
            value = {
                "script-message-to",
                mp.get_script_name(),
                "trakt-scrobble-event",
                slug, season, tostring(item.number), type, id, title,
            },
        })
    end

    if uosc_available then
        update_menu_uosc(menu_type, menu_title, items, menu_footnote)
    elseif input_available then
        message_timer:kill()
        message_overlay:remove()
        mp.add_timeout(0.1, function()
            open_menu_select(items)
        end)
    end
end

local function search_season(slug, type, id, title)
    local message = "正在获取季信息..."
    local menu_type = "trakt_season"
    local menu_title = "Trakt 季选择"
    local menu_footnote = "可使用 / 过滤"
    if uosc_available then
        update_menu_uosc(menu_type, menu_title, message, menu_footnote)
    else
        send_message(message)
    end

    local url = string.format("https://api.trakt.tv/shows/%s/seasons", slug)
    local res = http_request("GET", url, {
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["trakt-api-version"] = "2"
    })
    if not res or #res == 0 then
        local message = "没有找到结果"
        if uosc_available then
            update_menu_uosc(menu_type, menu_title, message, menu_footnote)
        else
            send_message(message, 3)
            msg.info(message)
        end
        return
    end
    if #res == 1 then
        search_episodes(slug, res[1].number, type, id, title)
    else
        local items = {}
        for _, item in ipairs(res) do
            table.insert(items, {
                title = item.number == 0 and "特别篇" or "第 " .. item.number .. " 季",
                value = {
                    "script-message-to",
                    mp.get_script_name(),
                    "trakt-episodes-event",
                    slug, tostring(item.number), type, id, title,
                },
            })
        end
    
        if uosc_available then
            update_menu_uosc(menu_type, menu_title, items, menu_footnote)
        elseif input_available then
            message_timer:kill()
            message_overlay:remove()
            mp.add_timeout(0.1, function()
                open_menu_select(items)
            end)
        end
    end
end

local function search_trakt(name, class, page)
    local message = "正在搜索..."
    local menu_type = "menu_trakt"
    local menu_title = "Trakt 搜索结果"
    local menu_footnote = "按 Enter 或 Ctrl+Enter 搜索"
    local menu_cmd = { "script-message-to", mp.get_script_name(), "search-event", name }
    if uosc_available then
        update_menu_uosc(menu_type, menu_title, message, menu_footnote, menu_cmd, name)
    else
        send_message(message)
    end
    local limit = 20
    local page = page or 1
    local url = string.format("https://api.trakt.tv/search/%s?query=%s&page=%d&limit=%s",
        class, url_encode(name), page, limit)
    local res = http_request("GET", url, {
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["trakt-api-version"] = "2"
    })
    if not res or #res == 0 then
        local message = "没有找到结果"
        if uosc_available then
            update_menu_uosc(menu_type, menu_title, message, menu_footnote)
        else
            send_message(message, 3)
            msg.info(message)
        end
        return
    end
    local items = {}
    for _, item in ipairs(res) do
        table.insert(items, {
            title = clip_title(item[class].title),
            hint = item[class].year and media_type_label(item.type) .. " " .. item[class].year or media_type_label(item.type),
            value = {
                "script-message-to",
                mp.get_script_name(),
                "trakt-season-event",
                class, item[class].ids.slug, item.type, tostring(item[class].ids.trakt), item[class].title,
            },
        })
    end
    if #items == limit then
        page = page + 1
        table.insert(items, {
            title = "加载下一页",
            value = {
                "script-message-to",
                mp.get_script_name(),
                "trakt-search-event",
                name, class, page,
            },
            italic = true,
            bold = true,
            align = "center",
        })
    end

    if uosc_available then
        update_menu_uosc(menu_type, menu_title, items, menu_footnote)
    elseif input_available then
        message_timer:kill()
        message_overlay:remove()
        mp.add_timeout(0.1, function()
            open_menu_select(items)
        end)
    end
end

local function search(name)
    local title, class = name:match("^(.-)%s*|%s*(.-)$")
    if title then name = title end
    local chinese = parse_chinese_episode(name)
    if chinese then
        name = chinese.search_title
    end
    local name = name:gsub("%(.-%)", "")
          :gsub("[sS]%d+[%.%-%s:]?[eE]%d+$", "")
          :gsub("[eE]%d+$", "")
          :gsub("^%s*(.-)%s*$", "%1")
    if class == "mv" then
        search_trakt(name, "movie")
    else
        search_trakt(name, "show")
    end
end

----- input menu -----
function open_menu_select(menu_items)
    local item_titles, item_values = {}, {}
    for i, v in ipairs(menu_items) do
        item_titles[i] = v.hint and v.title .. " (" .. v.hint .. ")" or v.title
        item_values[i] = v.value
    end
    mp.commandv('script-message-to', 'console', 'disable')
    input.select({
        prompt = '过滤：',
        items = item_titles,
        submit = function(id)
            mp.commandv(unpack(item_values[id]))
        end,
    })
end

function open_input_menu_get(name)
    mp.commandv('script-message-to', 'console', 'disable')
    input.get({
        prompt = '输入片名：',
        default_text = name,
        cursor_position = name and #name + 1,
        submit = function(text)
            input.terminate()
            search(text)
        end
    })
end
----- input menu END-----

----- uosc menu -----
function open_input_menu_uosc(name)
    local items = {}
    items[#items + 1] = {
        hint = "  搜电影请在搜索词后追加 '|mv'",
        keep_open = true,
        selectable = false,
    }
    local menu_props = {
        type = "menu_trakt",
        title = "输入 Trakt 搜索词",
        search_style = "palette",
        search_debounce = "submit",
        search_suggestion = name,
        on_search = {
            "script-message-to",
            mp.get_script_name(),
            "search-event",
        },
        footnote = "按 Enter 或 Ctrl+Enter 搜索",
        items = items,
    }
    local json_props = utils.format_json(menu_props)
    mp.commandv("script-message-to", "uosc", "open-menu", json_props)
end

function update_menu_uosc(menu_type, menu_title, menu_item, menu_footnote, menu_cmd, query)
    local items = {}
    if type(menu_item) == "string" then
        table.insert(items, {
            title = menu_item,
            value = "",
            italic = true,
            keep_open = true,
            selectable = false,
            align = "center",
        })
    else
        items = menu_item
    end

    local menu_props = {
        type = menu_type,
        title = menu_title,
        search_style = menu_cmd and "palette" or "on_demand",
        search_debounce = menu_cmd and "submit" or 0,
        on_search = menu_cmd,
        footnote = menu_footnote,
        search_suggestion = query,
        items = items,
    }
    local json_props = utils.format_json(menu_props)
    mp.commandv("script-message-to", "uosc", "open-menu", json_props)
end
----- uosc menu END-----

function open_input_menu(name)
    if uosc_available then
        open_input_menu_uosc(name)
    elseif input_available then
        open_input_menu_get(name)
    else
        send_message("uosc 或 mp.input 不可用", 3)
        msg.error("uosc or mp.input not available")
    end
end

mp.register_script_message("search-event", function(name)
    if uosc_available then
        mp.commandv("script-message-to", "uosc", "open-menu", "menu_trakt")
    end
    search(name)
end)
mp.register_script_message("trakt-search-event", function(name, class, page)
    if uosc_available then
        mp.commandv("script-message-to", "uosc", "open-menu", "menu_trakt")
    end
    search_trakt(name, class, page)
end)

mp.register_script_message("trakt-season-event", function(class, slug, type, id, title)
    if uosc_available then
        mp.commandv("script-message-to", "uosc", "open-menu", "trakt_season")
    end
    if class == "movie" or class == "mv" then
        set_state_match(type, title, id, 100, "manual", true, slug, nil, nil, state.search_query or title)
        local progress = get_progress()
        local data = get_data(progress)
        if data then
            start_scrobble(config, data)
        end
        write_history(state.dir, state.fname, true)
    else
        state.pending_slug = slug
        search_season(slug, type, id, title)
    end
end)

mp.register_script_message("trakt-episodes-event", function(slug, number, type, id, title)
    if uosc_available then
        mp.commandv("script-message-to", "uosc", "open-menu", "trakt_episodes")
    end
    search_episodes(slug, number, type, id, title)
end)

mp.register_script_message("trakt-scrobble-event", function(slug, season, number, type, id, title)
    if uosc_available then
        mp.commandv("script-message-to", "uosc", "close-menu", "trakt_episodes")
    end
    set_state_match(type, title, id, 100, "manual", true, slug or state.pending_slug, season, tonumber(number), state.search_query or title)
    local progress = get_progress()
    local data = get_data(progress)
    if data then
        enabled = true
        start_scrobble(config, data)
    end
    write_history(state.dir, state.fname, true)
end)
