
--[[
    * trakt-scrobble.lua
    *
    * AUTHORS: dyphire
    * License: MIT
    * link: https://github.com/dyphire/trakt-scrobble
]]


local msg = require "mp.msg"
local utils = require "mp.utils"
local options = require "mp.options"
input_available, input = pcall(require, "mp.input")
uosc_available = false

o = {
    enabled = true,
    -- Set to '~~/trakt_config.json' for sub path of mpv portable_config directory
    -- OR change to '/:dir%script%/trakt_config.json' for placing it in the same directory of script
    -- OR write any variable using '/:var',
    -- such as: '/:var%APPDATA%/mpv/trakt_config.json' or '/:var%HOME%/mpv/trakt_config.json'
    congfig_path = "~~/trakt_config.json",
    history_path = "~~/trakt_history.json",
    --slice long filenames, and how many chars to show
    max_title_length = 100,
    auto_match_policy = "balanced",
    min_auto_confidence = 85,
    min_prompt_confidence = 60,
    watched_threshold = 85,
    min_real_play_seconds = 120,
    osd_font_size = 32,
    osd_margin_x = 30,
    osd_margin_y = 120,
    osd_border_size = 2,
    mark_watched_on = "threshold",
    trust_legacy_history = false,
    debug_match = false,
    generic_title_blocklist = "current,video,web,default,play",
    -- https://trakt.tv/oauth/applications
    client_id = "MmU5ZDg5MDcyYmIxNThlZWZhZmNlNTA5ZjZiMWJmMzM3MzcwMTQwMDU4ODFlZGZiYjgzZWI3ZGRkMWMwNGY1YQ==",
    client_secret = "MDcxYTUzZTVkZDhkODA1NTM2MTU2YWNlZTJjYjU5Njc5YjUwOTAwZWU3YmZkYmVmMTZiNGZmODdkMzQ2NGFiNg==",
    redirect_uri = "https://zapp.com",
}

options.read_options(o, _, function() end)

o.min_auto_confidence = tonumber(o.min_auto_confidence) or 85
o.min_prompt_confidence = tonumber(o.min_prompt_confidence) or 60
o.watched_threshold = tonumber(o.watched_threshold) or 85
o.min_real_play_seconds = tonumber(o.min_real_play_seconds) or 120
o.osd_font_size = tonumber(o.osd_font_size) or 32
o.osd_margin_x = tonumber(o.osd_margin_x) or 30
o.osd_margin_y = tonumber(o.osd_margin_y) or 120
o.osd_border_size = tonumber(o.osd_border_size) or 2

state = {}
config = {}
enabled = o.enabled
local scrobble = false

base64 = require("modules/base64")
require('modules/guess')
require('modules/menu')

local function expand_path(path)
    if path:find('^/:dir%%script%%') then
        path = path:gsub('/:dir%%script%%', mp.get_script_directory())
    elseif path:find('/:var%%(.*)%%') then
        local os_variable = path:match('/:var%%(.*)%%')
        path = path:gsub('/:var%%(.*)%%', os.getenv(os_variable))
    else
        path = mp.command_native({ "expand-path", path }) -- Expands both ~ and ~~
    end
    return path
end

local config_file = expand_path(o.congfig_path)
local history_path = expand_path(o.history_path)

-- Check if the path is a protocol (e.g., http://)
local function is_protocol(path)
    return type(path) == "string" and (path:find("^%a[%w.+-]-://") ~= nil or path:find("^%a[%w.+-]-:%?") ~= nil)
end

-- URL decode function
local function hex_to_char(x)
    return string.char(tonumber(x, 16))
end

function url_encode(str)
    if str then
        str = str:gsub("([^%w%-%.%_%~])", function(c)
            return string.format("%%%02X", string.byte(c))
        end)
    end
    return str
end

function url_decode(str)
    if str ~= nil then
        str = str:gsub('^%a[%a%d-_]+://', '')
              :gsub('^%a[%a%d-_]+:\\?', '')
              :gsub('%%(%x%x)', hex_to_char)
        if str:find('://localhost:?') then
            str = str:gsub('^.*/', '')
        end
        str = str:gsub("%?.+", ""):gsub("%+", " ")
        local last_pos = str:match('.*[\\/:%?]()')
        if last_pos then
            str = str:sub(last_pos)
        end
    end
    return str
end

-- from http://lua-users.org/wiki/LuaUnicode
local UTF8_PATTERN = '[%z\1-\127\194-\244][\128-\191]*'

-- return a substring based on utf8 characters
-- like string.sub, but negative index is not supported
local function utf8_sub(s, i, j)
    if i > j then
        return s
    end

    local t = {}
    local idx = 1
    for char in s:gmatch(UTF8_PATTERN) do
        if i <= idx and idx <= j then
            local width = #char > 2 and 2 or 1
            idx = idx + width
            t[#t + 1] = char
        end
    end
    return table.concat(t)
end

function clip_title(title)
    if not title then
        return title
    end
    local title_clip = utf8_sub(title, 1, o.max_title_length)
    if title ~= title_clip then
        title = title_clip .. "..."
    end
    return title
end

local function normalize(path)
    if normalize_path ~= nil then
        if normalize_path then
            path = mp.command_native({"normalize-path", path})
        else
            local directory = mp.get_property_native("working-directory", "")
            path = utils.join_path(directory, path:gsub('^%.[\\/]',''))
            if platform == "windows" then path = path:gsub("\\", "/") end
        end
        return path
    end

    normalize_path = false

    local commands = mp.get_property_native("command-list", {})
    for _, command in ipairs(commands) do
        if command.name == "normalize-path" then
            normalize_path = true
            break
        end
    end
    return normalize(path)
end

local function get_parent_dir(path)
    local dir = nil
    if path and not is_protocol(path) then
        path = normalize(path)
        dir = utils.split_path(path)
    end
    return dir
end

local function split_by_numbers(filename)
    local parts = {}
    local pattern = "([^%d]*)(%d+)([^%d]*)"
    for pre, num, post in string.gmatch(filename, pattern) do
        table.insert(parts, {pre = pre, num = tonumber(num), post = post})
    end
    return parts
end

local function compare_filenames(fname1, fname2)
    local parts1 = split_by_numbers(fname1)
    local parts2 = split_by_numbers(fname2)

    local min_len = math.min(#parts1, #parts2)

    for i = 1, min_len do
        local part1 = parts1[i]
        local part2 = parts2[i]

        if part1.pre ~= part2.pre then
            return false
        end

        if part1.num ~= part2.num then
            return part1.num, part2.num
        end

        if part1.post ~= part2.post then
            return false
        end
    end

    return false
end

local function get_episode_number(fname, filename)
    local episode_num1, episode_num2 = compare_filenames(fname, filename)
    if episode_num1 and episode_num2 then
        return tonumber(episode_num1), tonumber(episode_num2)
    else
        return nil, nil
    end
end

local function trim(str)
    if not str then return str end
    return str:gsub("^%s+", ""):gsub("%s+$", "")
end

local function contains_non_ascii(str)
    return type(str) == "string" and str:find("[\128-\255]") ~= nil
end

local function parse_blocklist(value)
    local list = {}
    for item in tostring(value or ""):gmatch("([^,]+)") do
        item = trim(item):lower()
        if item ~= "" then
            list[item] = true
        end
    end
    return list
end

local generic_title_lookup = parse_blocklist(o.generic_title_blocklist)

local function normalize_match_text(str)
    str = tostring(str or ""):lower()
    str = str:gsub("&", " and ")
    str = str:gsub("[%[%]%(%){}]", " ")
    str = str:gsub("[%.%_%-%:%;%,%!%?%+%/\\|'\"]", " ")
    str = str:gsub("%s+", " ")
    return trim(str) or ""
end

local function compact_match_text(str)
    return normalize_match_text(str):gsub("%s+", "")
end

local function token_set(str)
    local set = {}
    for token in normalize_match_text(str):gmatch("%S+") do
        if #token > 1 and not token:match("^%d+[pPkKxXbBfF]$") then
            set[token] = true
        end
    end
    return set
end

local function title_similarity(a, b)
    local aa = compact_match_text(a)
    local bb = compact_match_text(b)
    if aa == "" or bb == "" then return 0 end
    if aa == bb then return 100 end
    if aa:find(bb, 1, true) or bb:find(aa, 1, true) then
        local shorter = math.min(#aa, #bb)
        local longer = math.max(#aa, #bb)
        return math.floor(75 + 25 * shorter / longer)
    end

    local set_a = token_set(a)
    local set_b = token_set(b)
    local common, total = 0, 0
    for token in pairs(set_a) do
        total = total + 1
        if set_b[token] then common = common + 1 end
    end
    for token in pairs(set_b) do
        if not set_a[token] then total = total + 1 end
    end
    if total == 0 then return 0 end
    return math.floor(common * 100 / total)
end

function clean_query_title(title)
    title = trim(title or "") or ""
    title = title:gsub("^%[.-%]%s*", "")
    title = title:gsub("^%(.-%)%s*", "")
    title = title:gsub("%s+", " ")
    title = title:gsub("[%s%-%_%.%+]+$", "")
    return trim(title) or ""
end

function parse_chinese_episode(title)
    if not contains_non_ascii(title) then return nil end
    title = clean_query_title(title)
    local show, season, episode = title:match("^%s*《(.-)》%s*第%s*(%d+)%s*季.-第%s*(%d+)%s*[话話集].*")
    if show and episode then
        return { search_title = clean_query_title(show), season = tonumber(season), episode = tonumber(episode), source = "chinese" }
    end
    show, episode = title:match("^%s*《(.-)》%s*第%s*(%d+)%s*[话話集].*")
    if show and episode then
        return { search_title = clean_query_title(show), season = 1, episode = tonumber(episode), source = "chinese" }
    end
    show, season, episode = title:match("^%s*(.-)%s*第%s*(%d+)%s*季.-第%s*(%d+)%s*[话話集].*")
    if show and episode and clean_query_title(show) ~= "" then
        return { search_title = clean_query_title(show), season = tonumber(season), episode = tonumber(episode), source = "chinese" }
    end
    show, episode = title:match("^%s*(.-)%s*第%s*(%d+)%s*[话話集].*")
    if show and episode and clean_query_title(show) ~= "" then
        return { search_title = clean_query_title(show), season = 1, episode = tonumber(episode), source = "chinese" }
    end
    show, episode = title:match("^%s*(.-)%s+[Ee][Pp]?%s*(%d+).*")
    if show and episode and clean_query_title(show) ~= "" then
        return { search_title = clean_query_title(show), season = 1, episode = tonumber(episode), source = "chinese" }
    end
    show, episode = title:match("^%s*(.-)%s+(%d%d?%d?)%s*$")
    if show and episode and clean_query_title(show) ~= "" then
        return { search_title = clean_query_title(show), season = 1, episode = tonumber(episode), source = "chinese" }
    end
    return nil
end

local function is_generic_title(title)
    local normalized = normalize_match_text(title)
    local compact = normalized:gsub("%s+", "")
    if compact == "" then return true end
    if generic_title_lookup[normalized] or generic_title_lookup[compact] then return true end
    if compact:match("^%d+$") then return true end
    if compact:match("^%d+[pPkKxXbBfF]$") then return true end
    if #compact <= 2 and not contains_non_ascii(compact) then return true end
    return false
end

local function api_ok(res)
    return res ~= nil and res.error == nil
end

function trakt_episode_exists(slug, season, episode)
    local ep_res = http_request("GET", string.format("https://api.trakt.tv/shows/%s/seasons/%s/episodes/%s",
        slug, season, episode), {
            ["trakt-api-key"] = base64.decode(o.client_id),
            ["trakt-api-version"] = "2"
    })
    return api_ok(ep_res)
end

local function now_iso()
    return os.date("!%Y-%m-%dT%H:%M:%SZ")
end

local function mark_uncertain(query, confidence, reason)
    state.match_confidence = confidence or 0
    state.match_reason = reason
    state.search_query = clean_query_title(query or state.search_query or state.filename or "")
    state.prompted = true
    local message = "无法可靠识别媒体标题，按 Alt+d 手动选择"
    if confidence and confidence >= o.min_prompt_confidence then
        message = "识别结果不确定，按 Alt+d 手动确认"
    end
    if state.search_query and state.search_query ~= "" then
        message = message .. "\n搜索词：" .. state.search_query
    end
    send_message(message, 6, "FF8800")
    msg.warn(string.format("Trakt match uncertain: query=%s confidence=%s reason=%s", state.search_query or "", tostring(confidence or 0), reason or ""))
end

function set_state_match(media_type, title, id, confidence, source, confirmed, slug, season, episode, query)
    if not state then return end
    state.type = media_type
    state.title = title
    state.id = id
    state.slug = slug
    state.season = season and tonumber(season) or nil
    state.episode = episode and tonumber(episode) or nil
    state.match_confidence = confidence or 100
    state.match_source = source or "auto"
    state.confirmed = confirmed == true
    state.search_query = clean_query_title(query or state.search_query or title or "")
end

local function parse_title(path)
    local dir = get_parent_dir(path)
    local filename = mp.get_property_native("filename/no-ext")
    local title = mp.get_property_native("media-title"):gsub("%.[^%.]+$", "")
    local thin_space = string.char(0xE2, 0x80, 0x89)
    local fname = filename

    if is_protocol(path) then
        title = url_decode(title)
        fname = title
    elseif #title < #filename then
        title = filename
    end

    title = title:gsub(thin_space, " ")
    title = format_filename(title)
    local media_title, season, episode = title:match("^(.-)%s*[sS](%d+)[eE](%d+)")
    if not season then
        local media_title, episode = title:match("^(.-)%s*[eE](%d+)")
        if episode and dir then
            local season = dir:match("[sS](%d+)") or dir:match("[sS]eason%s*(%d+)")
                or dir:match("(%d+)[nrdsth]+[_%.%s]%s*[sS]eason")
            if season then
                title = media_title .. " S" .. season .. "E" .. episode
            else
                title = media_title .. " S01" .. "E" .. episode
            end
        end
    end

    if not dir then
        if season then
            dir = title .. " S" .. season
        else
            dir = title
        end
    end

    return dir, fname, title
end

local function format_message(msg, color)
    return string.format("{\\1c&H%s&}%s", color, msg)
end

local function format_osd_style()
    return string.format("{\\an7\\pos(%d,%d)\\fs%d\\bord%d\\shad0}",
        o.osd_margin_x, o.osd_margin_y, o.osd_font_size, o.osd_border_size)
end

-- Send a message to the OSD
message_overlay = mp.create_osd_overlay('ass-events')
message_timer = mp.add_timeout(1, function ()
    message_overlay:remove()
end, true)

function send_message(msg, time, color)
    local text = color and format_message(msg, color) or msg
    text = text:gsub("\r\n", "\\N"):gsub("\n", "\\N")
    message_timer:kill()
    message_timer.timeout = time or 1
    message_overlay.data = format_osd_style() .. text
    message_overlay:update()
    message_timer:resume()
end

-- Read config file
local function read_config(file_path)
    local file = io.open(file_path, "r")
    if not file then
        return {}
    end
    local content = file:read("*a")
    file:close()
    return utils.parse_json(content)
end

config = read_config(config_file)

-- Write config file
local function write_config(file_path, data)
    local file = io.open(file_path, "w")
    if not file then
        return
    end
    file:write(utils.format_json(data))
    file:close()
end

-- Write history file
function write_history(dir, fname, confirmed)
    local history = read_config(history_path) or {}
    if not state or not state.id then return end
    history._schema = 2
    history[dir] = {}
    history[dir].fname = fname
    history[dir].type = state.type
    history[dir].title = state.title
    history[dir].id = state.id
    history[dir].slug = state.slug
    if state.season and state.episode then
        history[dir].season = state.season
        history[dir].episode = state.episode
    end
    history[dir].match = {
        source = state.match_source or (confirmed and "manual" or "auto"),
        confidence = state.match_confidence or (confirmed and 100 or 0),
        confirmed = confirmed == true or state.confirmed == true,
        query = state.search_query or state.filename or "",
        updated_at = now_iso()
    }
    write_config(history_path, history)
end

local function history_is_trusted(entry)
    if not entry then return false end
    if entry.match then
        return entry.match.confirmed == true or (entry.match.confidence and tonumber(entry.match.confidence) >= o.min_auto_confidence)
    end
    return o.trust_legacy_history == true
end

-- Send HTTP request using curl
function http_request(method, url, headers, body)
    local cmd = { "curl", "-sL", "-X", method, url }
    if headers then
        for k, v in pairs(headers) do
            table.insert(cmd, "-H")
            table.insert(cmd, string.format("%s: %s", k, v))
        end
    end
    if body then
        table.insert(cmd, "-d")
        table.insert(cmd, utils.format_json(body))
    end

    local res = mp.command_native({
        name = "subprocess",
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
        args = cmd
    })

    if res.status ~= 0 then
        msg.error("HTTP request failed: " .. res.stderr)
        return nil
    end

    return utils.parse_json(res.stdout)
end

-- Initialize and check config
local function init()
    config = read_config(config_file)
    if not o.client_id or not o.client_secret
    or #base64.decode(o.client_id) ~= 64 or #base64.decode(o.client_secret) ~= 64 then
        return 10
    end
    if not next(config) or not config.access_token
    or #base64.decode(config.access_token) ~= 64 then
        return 11
    end
    return 0
end

-- Generate device code
local function device_code()
    config = read_config(config_file)
    local res = http_request("POST", "https://api.trakt.tv/oauth/device/code", {
        ["Content-Type"] = "application/json"
    }, {
        client_id = base64.decode(o.client_id)
    })
    if not res then
        return -1
    end
    config.device_code = res.device_code
    write_config(config_file, config)
    return 0, res.user_code
end

-- Authenticate with device code
local function auth()
    config = read_config(config_file)
    if not next(config) or not config.device_code then
        return -1
    end
    local res = http_request("POST", "https://api.trakt.tv/oauth/device/token", {
        ["Content-Type"] = "application/json"
    }, {
        client_id = base64.decode(o.client_id),
        client_secret = base64.decode(o.client_secret),
        code = config.device_code
    })
    if not res or not res.access_token then
        return -1
    end
    config.access_token = base64.encode(res.access_token)
    config.refresh_token  = base64.encode(res.refresh_token)
    config.device_code = nil
    config.created_at = res.created_at
    config.expires_in = res.expires_in
    config.today = os.date("%Y-%m-%d")

    -- Get user info
    local user_res = http_request("GET", "https://api.trakt.tv/users/settings", {
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["Authorization"] = config.access_token and "Bearer " .. base64.decode(config.access_token),
        ["trakt-api-version"] = "2"
    })
    if not user_res or not user_res.user then
        return -1
    end
    config.user_slug = user_res.user.ids.slug
    write_config(config_file, config)
    return 0
end

-- Authentication activation
local function activation()
    send_message("正在向 Trakt 请求授权码...", 10, "FFFFFF")
    local status, output = device_code()

    if status == 0 then
        local message = string.format("https://trakt.tv/activate/%s", output)
        if mp.get_property('clipboard-backends') ~= nil or mp.get_property_bool('clipboard-enable') then
            mp.commandv('set', 'clipboard/text', message)
        end
        send_message("打开 " .. message .. "\n浏览器完成后回到菜单选择：工具 > Trakt > 完成授权", 60, "FF8800")
        msg.info("Open " .. message)
    else
        send_message("获取 Trakt 授权码失败，请查看控制台", 5, "0000FF")
        msg.error("Failed to generate device code")
    end
end

-- Refresh access_token with refresh_token
local function refresh_token(config)
    local res = http_request("POST", "https://api.trakt.tv/oauth/token", {
        ["Content-Type"] = "application/json"
    }, {
        client_id = base64.decode(o.client_id),
        client_secret = base64.decode(o.client_secret),
        refresh_token = base64.decode(config.refresh_token),
        redirect_uri = o.redirect_uri,
        grant_type = "refresh_token"
    })

    if not res or not res.access_token then
        msg.error("Failed to refresh access token.")
        return -1
    end

    config.access_token = base64.encode(res.access_token)
    config.refresh_token = base64.encode(res.refresh_token)
    config.created_at = res.created_at
    config.expires_in = res.expires_in
    config.today = os.date("%Y-%m-%d")

    write_config(config_file, config)

    msg.info("Successfully refreshed access token.")
    return 0
end

-- Check if access_token is expired
local function check_access_token(config)
    if not next(config) or not config.access_token or not config.refresh_token then
        return -1
    end

    if config.created_at and config.expires_in then
        local time = os.time()
        if time - config.created_at >= config.expires_in then
            msg.warn("Access token was expired, attempting to refresh.")
            return refresh_token(config)
        else
            return 0
        end
    end

    local res = http_request("GET", "https://api.trakt.tv/users/settings", {
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["Authorization"] = config.access_token and "Bearer " .. base64.decode(config.access_token),
        ["trakt-api-version"] = "2"
    })

    if not res then
        msg.warn("Access token might be expired, attempting to refresh.")
        return refresh_token(config)
    end

    return 0
end

function get_progress()
    if not state then return end
    local time_pos = state.pos or 0
    local duration = state.duration or 0

    if duration == 0 then
        return
    end

    local progress = (tonumber(time_pos) / tonumber(duration)) * 100
    return math.floor(progress)
end

function get_data(progress)
    if not state then return end
    local data = nil
    if state.season and state.episode then
        data = {
            progress = tonumber(progress),
            show = {
                ids = {
                    trakt = tonumber(state.id)
                }
            },
            episode = {
                season = tonumber(state.season),
                number = tonumber(state.episode)
            }
        }
    elseif state.id then
        data = {
            progress = tonumber(progress),
            movie = {
                ids = {
                    trakt = tonumber(state.id)
                }
            }
        }
    end
    return data
end

function start_scrobble(config, data, no_osd)
    msg.info("Starting scrobbling to Trakt.tv")
    local res = http_request("POST", "https://api.trakt.tv/scrobble/start", {
        ["Content-Type"] = "application/json",
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["Authorization"] = config.access_token and "Bearer " .. base64.decode(config.access_token),
        ["trakt-api-version"] = "2"
    }, data)
    if not res then
        send_message("无法同步到 Trakt，请查看控制台", 3, "0000FF")
        msg.error("Trakt start scrobble failed")
        return
    end
    local message = nil
    if state and state.title then
        if state.season and state.episode then
            message = "正在同步到 Trakt：" .. state.title .. " S" .. state.season .. "E" .. state.episode
        else
            message = "正在同步到 Trakt：" .. state.title
        end
        if (input_available or uosc_available) and not no_osd then
            local message1 = format_message(message, "00FF00")
            local message2 = format_message("匹配错误？按 Alt+d 重新选择", "FF8800")
            send_message(message1 .. "\n" .. message2, 10)
            msg.info(message)
        elseif not no_osd then
            send_message(message, 3, "00FF00")
            msg.info(message)
        else
            msg.info(message)
        end
        scrobble = true
    end
end

local function send_scrobble_action(action, config, data)
    local res = http_request("POST", "https://api.trakt.tv/scrobble/" .. action, {
        ["Content-Type"] = "application/json",
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["Authorization"] = config.access_token and "Bearer " .. base64.decode(config.access_token),
        ["trakt-api-version"] = "2"
    }, data)
    return res
end

function pause_scrobble(config, data)
    msg.info("Trakt 提交继续观看：正在调用 pause")
    local res = send_scrobble_action("pause", config, data)
    if not res then
        msg.error("Trakt pause scrobble failed")
        return
    end
    msg.info("Trakt 提交继续观看成功")
    scrobble = false
end

function stop_scrobble(config, data)
    msg.warn("Trakt 提交已看历史：正在调用 stop")
    local res = send_scrobble_action("stop", config, data)
    if not res then
        msg.error("Trakt stop scrobble failed")
        return
    end
    if state and state.title then
        msg.warn("Trakt 提交已看历史成功：" .. state.title)
    else
        msg.warn("Trakt 提交已看历史成功")
    end
    scrobble = false
end

-- Query show
local function query_search_show(name, season, episode)
    name = clean_query_title(name)
    if name == "" or is_generic_title(name) then
        mark_uncertain(name, 0, "generic show title")
        return
    end
    local title, year = name:match("^(.-)%s*%(?(%d%d%d%d)%)?$")
    if year then name = title end
    local url = string.format("https://api.trakt.tv/search/show?query=%s", url_encode(name))
    local res = http_request("GET", url, {
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["trakt-api-version"] = "2"
    })
    if not res or #res == 0 then
        msg.info("Trakt show search returned no results")
        return
    end

    local best, best_score = nil, 0
    if year then
        for _, item in ipairs(res) do
            if item.show.year == tonumber(year) or item.show.year == tonumber(year) + 1
            or item.show.year == tonumber(year) - 1 then
                local score = title_similarity(name, item.show.title) + 10
                if score > best_score then
                    best = item.show
                    best_score = math.min(score, 100)
                end
            end
        end
    else
        for _, item in ipairs(res) do
            local score = title_similarity(name, item.show.title)
            if score > best_score then
                best = item.show
                best_score = score
            end
        end
    end
    if not best then
        msg.info("No matching show found")
        return
    end

    if best_score < o.min_auto_confidence then
        mark_uncertain(name, best_score, "show confidence below threshold")
        return
    end

    local season_res = http_request("GET", string.format("https://api.trakt.tv/shows/%s/seasons/%s",
    best.ids.slug, season), {
            ["trakt-api-key"] = base64.decode(o.client_id),
            ["trakt-api-version"] = "2"
    })
    if not season_res then
        season = season - 1
    end

    local ep_res = http_request("GET", string.format("https://api.trakt.tv/shows/%s/seasons/%s/episodes/%s",
        best.ids.slug, season, episode), {
            ["trakt-api-key"] = base64.decode(o.client_id),
            ["trakt-api-version"] = "2"
    })
    if not api_ok(ep_res) then
        msg.error("Failed to fetch episode details on trakt.tv")
        return
    end

    set_state_match("show", best.title, best.ids.trakt, best_score, "auto", false, best.ids.slug, season, episode, name)
    send_message("已匹配 Trakt：" .. state.title .. " S" .. state.season .. "E" .. state.episode, 3)
    msg.info("Found on trakt.tv: " .. state.title .. " S" .. state.season .. "E" .. state.episode)
end

-- Query movie
local function query_movie(movie, year)
    movie = clean_query_title(movie)
    if movie == "" or is_generic_title(movie) then
        mark_uncertain(movie, 0, "generic movie title")
        return
    end
    local url = string.format("https://api.trakt.tv/search/movie?query=%s", url_encode(movie))
    local res = http_request("GET", url, {
        ["trakt-api-key"] = base64.decode(o.client_id),
        ["trakt-api-version"] = "2"
    })
    if not res or #res == 0 then
        msg.info("Trakt movie search returned no results")
        return
    end
    local best, best_score = nil, 0
    for _, item in ipairs(res) do
        if item.movie.year == tonumber(year) or item.movie.year == tonumber(year) + 1
            or item.movie.year == tonumber(year) - 1 then
            local score = title_similarity(movie, item.movie.title) + 10
            if score > best_score then
                best = item.movie
                best_score = math.min(score, 100)
            end
        end
    end
    if not best then
        msg.info("No matching movie found")
        return
    end
    if best_score < o.min_auto_confidence then
        mark_uncertain(movie, best_score, "movie confidence below threshold")
        return
    end
    set_state_match("movie", best.title, best.ids.trakt, best_score, "auto", false, nil, nil, nil, movie)
    send_message("已匹配 Trakt：" .. state.title, 3)
end

-- Query media
local function query_media(config, media)
    if is_generic_title(media) then
        mark_uncertain(media, 0, "generic media title")
        return
    end
    local chinese = parse_chinese_episode(media)
    if chinese then
        state.search_query = chinese.search_title
        state.parsed_episode = chinese
        query_search_show(chinese.search_title, chinese.season, chinese.episode)
        return
    end
    local infos = { string.match(media, "^(.-)%s*[sS](%d+).*[eE](%d+).*") }
    if #infos == 3 then
        local name, season, episode = infos[1], infos[2], infos[3]
        query_search_show(name, season, episode)
    else
        infos = { string.match(media, "^(.-)%s*%((%d%d%d%d)%)") }
        if #infos ~= 2 then
            infos = { string.match(media, "^(.-)%s+(%d%d%d%d)[^%dhHxXvVpPkKxXbBfF]") }
        end
        if #infos == 2 then
            query_movie(infos[1], infos[2])
        else
            mark_uncertain(media, 0, "no safe automatic parser")
        end
    end
end

-- Checkin function
local function checkin_file(path)
    config = read_config(config_file)
    if not next(config) then return end

    state.duration = mp.get_property_number("duration", 0)
    local progress = get_progress()
    if not progress then return end

    local dir, fname, title  = parse_title(path)
    state.dir = dir
    state.fname = fname
    state.filename = title
    state.search_query = title
    state.play_started_at = os.time()
    local parsed = parse_chinese_episode(title)
    if parsed then
        state.search_query = parsed.search_title
        state.parsed_episode = parsed
    end

    local history = read_config(history_path) or {}
    if history[dir] and history_is_trusted(history[dir]) then
        local old_fname = history[dir].fname
        local old_type = history[dir].type
        local old_title = history[dir].title
        local old_id = history[dir].id
        local old_slug = history[dir].slug
        local old_season = history[dir].season
        local old_episode = history[dir].episode
        local episode_num1, episode_num2 = get_episode_number(old_fname, fname)
        if fname == old_fname then
            episode_num1, episode_num2 = 0, 0
        end
        if state.parsed_episode then
            episode_num1 = old_episode
            episode_num2 = state.parsed_episode.episode
        end
        if episode_num1 and episode_num2 then
            if old_type == "show" and old_season and old_episode then
                local next_episode = old_episode + episode_num2 - episode_num1
                local next_season = state.parsed_episode and state.parsed_episode.season or old_season
                if old_slug and trakt_episode_exists(old_slug, next_season, next_episode) then
                    set_state_match(old_type, old_title, old_id, 100, "history", true, old_slug, next_season, next_episode, history[dir].match and history[dir].match.query or old_title)
                    send_message("已按历史匹配 Trakt：" .. state.title .. " S" .. state.season .. "E" .. state.episode, 3)
                    msg.info("Found from trusted history: " .. state.title .. " S" .. state.season .. "E" .. state.episode)
                else
                    msg.warn("Trusted history episode did not pass Trakt validation")
                end
            end
        end
    elseif history[dir] then
        msg.info("Ignoring untrusted legacy Trakt history for " .. dir)
    end

    if not state.id then
        query_media(config, title)
    end
    local data = get_data(progress)
    if data then
        mp.add_timeout(1, function()
            start_scrobble(config, data)
        end)
    elseif (input_available or uosc_available) then
        local message = format_message("无法可靠识别媒体标题。\n按 Alt+d 打开搜索匹配菜单", "FF8800")
        send_message(message, 5)
    end
    if state.id and (state.confirmed or (state.match_confidence and state.match_confidence >= o.min_auto_confidence)) then
        write_history(dir, fname, state.confirmed)
    end
end

-- Main function
local function trackt_scrobble(force)
    if not o.enabled and not force then
        return
    end

    local path = mp.get_property_native("path")
    if not path then
        return
    end

    local video = mp.get_property_native("current-tracks/video")
    local fps = mp.get_property_number("container-fps", 0)
    local duration = mp.get_property_number("duration", 0)
    if not video or video["image"] or video["albumart"] or fps < 23 or duration < 60 then
        return
    end

    local status = init()

    if status == 10 then
        send_message("[Trakt] client_id 或 client_secret 配置不正确", 5, "0000FF")
        msg.warn("Please make sure you have set client_id and client_secret correctly!")
    elseif status == 11 then
        send_message("[Trakt] 请通过菜单执行：工具 > Trakt > 授权登录", 10, "FF8800")
    elseif status == 0 then
        msg.info("Checking Trakt.tv authentication status.")
        if check_access_token(config) ~= 0 then
            send_message("Trakt 授权失败，请通过菜单重新授权", 5, "FF0000")
            return
        end
        checkin_file(path)
    end
end

function on_time_pos(_, value)
    if not state then return end
    state.pos = value
    if state and not state.first_pos_at and value and value > 0 then
        state.first_pos_at = os.time()
    end
end

local function enough_real_play_time()
    if o.min_real_play_seconds <= 0 then return true end
    if not state or not state.first_pos_at then return false end
    return os.time() - state.first_pos_at >= o.min_real_play_seconds
end

local function get_real_play_seconds()
    if not state or not state.first_pos_at then return 0 end
    return math.max(os.time() - state.first_pos_at, 0)
end

local function finish_scrobble()
    if not enabled then return end
    if not next(config) then return end
    local progress = get_progress()
    local data = get_data(progress)
    if data then
        local real_play_seconds = get_real_play_seconds()
        local action = "pause"
        local reason = "未达到已看进度"
        if progress and progress >= o.watched_threshold and enough_real_play_time() then
            action = "stop"
            reason = "达到已看条件"
        elseif progress and progress >= o.watched_threshold then
            reason = "实际播放时间不足"
        end
        local decision_message = string.format("Trakt 结束判定：progress=%s real_play=%ds threshold=%d min_real=%d action=%s reason=%s",
            tostring(progress or "nil"), real_play_seconds, o.watched_threshold, o.min_real_play_seconds, action, reason)
        if action == "stop" then
            msg.warn(decision_message)
            send_message(decision_message, 3, "00FF00")
            stop_scrobble(config, data)
        else
            msg.info(decision_message)
            pause_scrobble(config, data)
        end
    end
end

function on_pause_change(paused)
    if not enabled then return end
    if not next(config) then return end
    if paused then
        msg.info("Trakt 普通暂停：不提交进度，退出播放时统一判定")
    else
        msg.info("Trakt 恢复播放：不重复提交 start")
    end
end

function on_pause_callback(_, paused)
    on_pause_change(paused)
end

-- Register event
mp.register_script_message('uosc-version', function()
    uosc_available = true
end)

mp.register_event("file-loaded", function()
    state = {}
    mp.observe_property("pause", "bool", on_pause_callback)
    mp.observe_property("time-pos", "number", on_time_pos)
    trackt_scrobble(enabled)
end)

mp.register_event("end-file", function()
    mp.unobserve_property(on_time_pos)
    mp.unobserve_property(on_pause_callback)
    finish_scrobble()
    state = nil
end)

mp.register_script_message("auth-menu", function()
    activation()
end)

mp.register_script_message("complete-auth", function()
    local status = auth()
    if status == 0 then
        send_message("Trakt 授权完成", 3, "00FF00")
        msg.info("Trakt authentication completed")
    else
        send_message("授权未开始或验证码已过期，请先执行：工具 > Trakt > 授权登录", 5, "FF0000")
        msg.error("Trakt authentication failed")
    end
end)

mp.register_script_message("search-menu", function()
    if not state then state = {} end
    config = read_config(config_file)
    if not next(config) then
        send_message("无法读取 Trakt 授权配置，请先完成授权", 3)
        msg.error("Failed to read config file")
        return
    end

    local path = mp.get_property_native("path")
    if not state.dir or not state.fname or not state.filename then
        local dir, fname, title  = parse_title(path)
        state.dir = dir
        state.fname = fname
        state.filename = title
        state.search_query = title
        local parsed = parse_chinese_episode(title)
        if parsed then
            state.search_query = parsed.search_title
            state.parsed_episode = parsed
        end
    end
    local title = state.search_query or state.filename or ""
    open_input_menu(title)
end)

mp.register_script_message("toggle-scrobble", function()
    if scrobble then
        send_message("已关闭当前 Trakt 同步", 3)
        msg.info("Trakt scrobbling disabled")
        finish_scrobble()
        enabled = false
    else
        enabled = true
        trackt_scrobble(enabled)
    end
end)
