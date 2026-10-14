-- quality-menu 4.2.0 - 2024-Oct-04
-- https://github.com/christoph-heinrich/mpv-quality-menu
--
-- Change the stream video and audio quality on the fly.
--
-- Usage:
-- add bindings to input.conf:
-- F     script-binding quality_menu/video_formats_toggle
-- Alt+f script-binding quality_menu/audio_formats_toggle

local mp = require 'mp'
local utils = require 'mp.utils'
local msg = require 'mp.msg'
local assdraw = require 'mp.assdraw'
local opt = require('mp.options')
local script_name = mp.get_script_name()

local opts = {
    --key bindings
    up_binding = 'UP WHEEL_UP',
    down_binding = 'DOWN WHEEL_DOWN',
    select_binding = 'ENTER MBTN_LEFT',
    close_menu_binding = 'ESC MBTN_RIGHT',

    --formatting / cursors
    selected_and_active     = '▶  - ',
    selected_and_inactive   = '●  - ',
    unselected_and_active   = '▷ - ',
    unselected_and_inactive = '○ - ',

    --font size scales by window, if false requires larger font and padding sizes
    scale_playlist_by_window = true,

    --playlist ass style overrides inside curly brackets, \keyvalue is one field, extra \ for escape in lua
    --example {\\fnUbuntu\\fs10\\b0\\bord1} equals: font=Ubuntu, size=10, bold=no, border=1
    --read http://docs.aegisub.org/3.2/ASS_Tags/ for reference of tags
    --undeclared tags will use default osd settings
    --these styles will be used for the whole playlist. More specific styling will need to be hacked in
    --
    --(a monospaced font is recommended but not required)
    style_ass_tags = '{\\fnmonospace\\fs25\\bord1}',

    -- Shift drawing coordinates. Required for mpv.net compatiblity
    shift_x = 0,
    shift_y = 0,

    --paddings from window edge
    text_padding_x = 5,
    text_padding_y = 10,

    --Screen dim when menu is open
    curtain_opacity = 0.7,

    --how many seconds until the quality menu times out
    --setting this to 0 deactivates the timeout
    menu_timeout = 6,

    --use youtube-dl to fetch a list of available formats (overrides quality_strings)
    fetch_formats = true,

    -- Stop waiting for a yt-dlp result that never arrives.
    fetch_timeout = 20,

    --list of ytdl-format strings to choose from
    quality_strings_video = [[
    [
    {"4320p" : "bestvideo[height<=?4320p]"},
    {"2160p" : "bestvideo[height<=?2160]"},
    {"1440p" : "bestvideo[height<=?1440]"},
    {"1080p" : "bestvideo[height<=?1080]"},
    {"720p" : "bestvideo[height<=?720]"},
    {"480p" : "bestvideo[height<=?480]"},
    {"360p" : "bestvideo[height<=?360]"},
    {"240p" : "bestvideo[height<=?240]"},
    {"144p" : "bestvideo[height<=?144]"}
    ]
    ]],
    quality_strings_audio = [[
    [
    {"default" : "bestaudio/best"}
    ]
    ]],

    --show the video format menu after opening an url
    start_with_menu = false,

    --percent-encoded JSON list of browser-discovered HLS child playlists
    hls_variants = '',

    -- Percent-encoded JSON of browser-resolved video qualities (FLV, DASH, HLS).
    stream_variants = '',

    --include unknown formats in the list
    --Unfortunately choosing which formats are video or audio is not always perfect.
    --Set to true to make sure you don't miss any formats, but then the list
    --might also include formats that aren't actually video or audio.
    --Formats that are known to not be video or audio are still filtered out.
    include_unknown = false,

    --hide columns that are identical for all formats
    hide_identical_columns = true,

    --which columns are shown in which order
    --comma separated list, prefix column with "-" to align left
    --
    --for the uosc integration it is possible to split the text up into a title and a hint
    --this is done by separating two columns with a "|" instead of a comma
    --column order in the hint is reversed
    --
    --columns that might be useful are:
    --resolution, width, height, fps, dynamic_range, tbr, vbr, abr, asr,
    --filesize, filesize_approx, vcodec, acodec, ext, video_ext, audio_ext,
    --language, format, format_note, quality
    --
    --columns that are derived from the above, but with special treatment:
    --size, frame_rate, bitrate_total, bitrate_video, bitrate_audio,
    --codec_video, codec_audio, audio_sample_rate
    --
    --If those still aren't enough or you're just curious, run:
    --yt-dlp -j <url>
    --This outputs unformatted JSON.
    --Format it and look under "formats" to see what's available.
    --
    --Not all videos have all columns available.
    --Be careful, misspelled columns simply won't be displayed, there is no error.
    columns_video = '-resolution,frame_rate,dynamic_range|language,bitrate_total,size,-codec_video,-codec_audio',
    columns_audio = 'audio_sample_rate,bitrate_total|size,language,-codec_audio',

    --columns used for sorting, see "columns_video" for available columns
    --comma separated list, prefix column with "-" to reverse sorting order
    --Leaving this empty keeps the order from yt-dlp/youtube-dl.
    --Be careful, misspelled columns won't result in an error,
    --but they might influence the result.
    sort_video = 'height,fps,tbr,size,format_id',
    sort_audio = 'asr,tbr,size,format_id',
}
opt.read_options(opts, 'quality-menu')

---@alias Format { properties: {[string]: string}, id: string, label?: string, title?: string, hint?: string }
-- *_active_id == nil means unknown, *_active_id == '' means disabled
---@alias Data { video_formats: Format[], audio_formats: Format[], video_active_id?: string, audio_active_id?: string, hls?: boolean, hls_mode?: string }
---@alias UIState { type: string, type_capitalized: string, name: string , to_other_type: UIState, to_fetching: UIState, to_menu: UIState, is_video: boolean }

do
    ---@param option_string string
    ---@param option_name string
    ---@return Format[]
    local function parse_predefined(option_string, option_name)
        ---@type {[string]: string}[]
        local json, error = utils.parse_json(option_string)
        if error then
            msg.error('Error while parsing JSON of option ' .. option_name .. ': ' .. error)
            return {}
        end
        ---@type Format[]
        local formats = {}
        for i, format in ipairs(json) do
            local label, format_string = next(format)
            formats[i] = {
                label = label,
                title = label,
                id = format_string,
            }
        end
        return formats
    end

    ---@type Data
    opts.predefined_data = {
        video_formats = parse_predefined(opts.quality_strings_video, 'quality_strings_video'),
        audio_formats = parse_predefined(opts.quality_strings_audio, 'quality_strings_audio'),
        video_active_id = nil,
        audio_active_id = nil,
    }
end

opts.font_size = tonumber(opts.style_ass_tags:match('\\fs(%d+%.?%d*)')) or mp.get_property_number('osd-font-size') or 25
opts.curtain_opacity = math.max(math.min(opts.curtain_opacity, 1), 0)

---@param input string
---@param separator string
---@return string[]
local function string_split(input, separator)
    if separator == nil then
        separator = '%s'
    end
    local t = {}
    for str in string.gmatch(input, '([^' .. separator .. ']+)') do
        table.insert(t, str)
    end
    return t
end

---@param strings string[]
---@return string[], boolean[]
local function strip_minus(strings)
    local stripped_list = {}
    local had_minus = {}
    for i, val in ipairs(strings) do
        if string.sub(val, 1, 1) == '-' then
            val = string.sub(val, 2)
            had_minus[val] = true
        end
        stripped_list[i] = val
    end
    return stripped_list, had_minus
end

do
    ---@param column_definition string
    ---@return { all: string[], all_align_left: boolean[], title: string[], title_align_left: boolean[], hint?: string[] }
    local function parse_columns(column_definition)
        local columns, columns_align_left = strip_minus(string_split(column_definition, '|,'))
        local title_hint = string_split(column_definition, '|')
        local title, title_align_left = strip_minus(string_split(title_hint[1], ','))

        local hint = nil
        if title_hint[2] then
            hint = strip_minus(string_split(title_hint[2], ','))
            -- reverse column order
            local n = #hint
            for i = 1, n / 2 do
                hint[i], hint[n - i + 1] = hint[n - i + 1], hint[i]
            end
        end
        return {
            all = columns, all_align_left = columns_align_left,
            title = title, title_align_left = title_align_left,
            hint = hint
        }
    end

    ---@type { all: string[], all_align_left: boolean[], title: string[], title_align_left: boolean[], hint?: string[] }
    ---@diagnostic disable-next-line: param-type-mismatch
    opts.columns_video = parse_columns(opts.columns_video)
    ---@type { all: string[], all_align_left: boolean[], title: string[], title_align_left: boolean[], hint?: string[] }
    ---@diagnostic disable-next-line: param-type-mismatch
    opts.columns_audio = parse_columns(opts.columns_audio)
end

local begin_stream_switch
local pending_browser_switch = nil
local reload_resume_cleanup = nil

-- Use one scoped resume callback for both native reloads and URL replacements.
-- A failed/abandoned reload must never seek a later, unrelated playlist item.
local function prepare_reload_resume(target_url, reload_duration, time_pos)
    if reload_resume_cleanup then reload_resume_cleanup() end
    if mp.get_property_bool('user-data/uosc_danmaku/live-active', false)
        or not reload_duration or reload_duration <= 0 or not time_pos then return end

    local seeker, failed, cleanup_timer
    local cleaned = false
    local function cleanup()
        if cleaned then return end
        cleaned = true
        mp.unregister_event(seeker)
        mp.unregister_event(failed)
        if cleanup_timer then cleanup_timer:kill() end
        if reload_resume_cleanup == cleanup then reload_resume_cleanup = nil end
    end
    seeker = function()
        local matches = mp.get_property('path') == target_url
        cleanup()
        if matches then mp.commandv('seek', time_pos, 'absolute+exact') end
    end
    failed = function(event)
        if event and event.reason == 'error' then cleanup() end
    end
    mp.register_event('file-loaded', seeker)
    mp.register_event('end-file', failed)
    cleanup_timer = mp.add_timeout(15, cleanup)
    reload_resume_cleanup = cleanup
end

local function stream_switch_in_progress()
    if pending_browser_switch then return true end
    local marker = mp.get_property_native('user-data/quality-menu/stream-switch')
    if not marker or type(marker.expiresAt) ~= 'number' or marker.expiresAt < os.time() then return false end
    if marker.stage == 'loading' then return true end
    if marker.stage == 'loaded' and marker.provider == 'bilibili'
        and mp.get_property_bool('user-data/uosc_danmaku/live-active', false)
        and marker.sessionId == mp.get_property_native('user-data/uosc_danmaku/live-session') then
        -- Playback can finish loading before the bridge's next poll. Preserve
        -- this generation until it has adopted the path, so a fast second
        -- click cannot replace the evidence authorizing the first change.
        local ack = mp.get_property_native('user-data/uosc_danmaku/live-switch-ack')
        return not (ack and ack.sessionId == marker.sessionId and ack.generation == marker.generation)
    end
    return false
end

-- special thanks to reload.lua (https://github.com/4e6/mpv-reload/)
local function reload_resume()
    if stream_switch_in_progress() then
        mp.commandv('show-text', '正在切换清晰度，请稍候', 3000)
        return
    end
    local reload_duration = mp.get_property_native('duration')
    local time_pos = mp.get_property('time-pos')

    -- Tries to determine live stream vs. pre-recorded VOD. VOD has non-zero
    -- duration property. When reloading VOD, to keep the current time position
    -- we should provide offset from the start. Stream doesn't have fixed start.
    -- Decent choice would be to reload stream from it's current 'live' position.
    -- That's the reason we don't pass the offset when reloading streams.
    prepare_reload_resume(mp.get_property('path'), reload_duration, time_pos)
    if begin_stream_switch then begin_stream_switch(mp.get_property('path'), time_pos) end
    mp.command('playlist-play-index current')
end

---@type { video_menu: UIState, audio_menu: UIState, video_fetching: UIState, audio_fetching: UIState }
local states = {
    video_menu = { type = 'video', type_capitalized = 'Video', name = 'video_menu', is_video = true },
    audio_menu = { type = 'audio', type_capitalized = 'Audio', name = 'audio_menu', is_video = false },
    video_fetching = { type = 'video', type_capitalized = 'Video', name = 'video_fetching', is_video = true },
    audio_fetching = { type = 'audio', type_capitalized = 'Audio', name = 'audio_fetching', is_video = false },
}
states.video_menu.to_fetching = states.video_fetching
states.video_menu.to_menu = states.video_menu
states.video_menu.to_other_type = states.audio_menu
states.audio_menu.to_fetching = states.audio_fetching
states.audio_menu.to_menu = states.audio_menu
states.audio_menu.to_other_type = states.video_menu
states.video_fetching.to_fetching = states.video_fetching
states.video_fetching.to_menu = states.video_menu
states.video_fetching.to_other_type = states.audio_fetching
states.audio_fetching.to_fetching = states.audio_fetching
states.audio_fetching.to_menu = states.audio_menu
states.audio_fetching.to_other_type = states.video_fetching

---@type UIState | nil
local open_menu_state = nil
---@type string | nil
local current_url = nil
local loaded_url = nil
---@type function | nil
local destructor = nil
local fetching_timer = nil

local menu_open
local menu_close
local video_formats_toggle
local audio_formats_toggle

local osd = mp.create_osd_overlay('ass-events')

local function hide_osd()
    -- workaround mpv bug, setting to hidden does not cause a redraw
    -- https://github.com/mpv-player/mpv/issues/10227
    osd.data = ''
    osd:update()
    osd.hidden = true
    osd:update()
end

local osd_timer = mp.add_timeout(1, function() menu_close() end)
osd_timer:kill()

---@param message string
---@param time number
local function osd_message(message, time)
    osd.res_x = 1280
    osd.res_y = 720
    osd.hidden = false
    osd.data = message
    osd:update()
    osd_timer.timeout = time
    osd_timer:kill()
    osd_timer:resume()
end

---@alias FormatRaw {format_id: string, vcodec?: string, acodec?: string, filesize: integer?, filesize_approx?: integer, fps?: number, tbr?: number, vbr?: number, abr?: number, asr?: number}

---@param json {formats: FormatRaw[], requested_formats: FormatRaw, requested_downloads: FormatRaw}
---@return Data
local function process_json(json)
    ---@param format FormatRaw
    ---@return boolean
    local function is_video(format)
        -- 'none' means it is not a video
        -- nil means it is unknown
        return (opts.include_unknown or format.vcodec) and format.vcodec ~= 'none' or false
    end

    ---@param format FormatRaw
    ---@return boolean
    local function is_audio(format)
        return (opts.include_unknown or format.acodec) and format.acodec ~= 'none' or false
    end

    local requested_video = nil
    local requested_audio = nil
    local requested_formats = json.requested_formats or json.requested_downloads or {}
    for _, format in ipairs(requested_formats) do
        if is_video(format) then
            requested_video = format.format_id
        elseif is_audio(format) then
            requested_audio = format.format_id
        end
    end

    local video_formats = {}
    local audio_formats = {}
    local all_formats = {}
    for i = #json.formats, 1, -1 do
        local format = json.formats[i]
        if is_video(format) then
            video_formats[#video_formats + 1] = format
            all_formats[#all_formats + 1] = format
        elseif is_audio(format) then
            audio_formats[#audio_formats + 1] = format
            all_formats[#all_formats + 1] = format
        end
    end

    ---@param format FormatRaw
    local function populate_special_fields(format)
        format.size = format.filesize or format.filesize_approx
        format.frame_rate = format.fps
        format.bitrate_total = format.tbr
        format.bitrate_video = format.vbr
        format.bitrate_audio = format.abr
        format.codec_video = format.vcodec
        format.codec_audio = format.acodec
        format.audio_sample_rate = format.asr
    end

    for _, format in ipairs(all_formats) do
        populate_special_fields(format)
    end

    local sort_video, reverse_video = strip_minus(string_split(opts.sort_video, ','))
    local sort_audio, reverse_audio = strip_minus(string_split(opts.sort_audio, ','))

    ---@param properties string[]
    ---@param reverse {[string]: boolean}
    ---@return fun(a: FormatRaw, b: FormatRaw): boolean
    local function comp(properties, reverse)
        return function(a, b)
            for _, prop in ipairs(properties) do
                local a_val = a[prop]
                local b_val = b[prop]
                if a_val and b_val and type(a_val) ~= 'table' and a_val ~= b_val then
                    if reverse[prop] then
                        return a_val < b_val
                    else
                        return a_val > b_val
                    end
                end
            end
            return false
        end
    end

    if #sort_video > 0 then
        table.sort(video_formats, comp(sort_video, reverse_video))
    end
    if #sort_audio > 0 then
        table.sort(audio_formats, comp(sort_audio, reverse_audio))
    end

    ---@param size integer
    ---@return string
    local function scale_filesize(size)
        if size == nil then
            return ''
        end

        local counter = 0
        while size > 1024 do
            size = size / 1024
            counter = counter + 1
        end

        if counter >= 3 then return string.format('%.1fGiB', size)
        elseif counter >= 2 then return string.format('%.1fMiB', size)
        elseif counter >= 1 then return string.format('%.1fKiB', size)
        else return string.format('%.1fB  ', size)
        end
    end

    ---@param bitrate integer
    ---@return string
    local function scale_bitrate(bitrate)
        if bitrate == nil then
            return ''
        end

        local counter = 0
        while bitrate > 1000 do
            bitrate = bitrate / 1000
            counter = counter + 1
        end

        if counter >= 2 then return string.format('%.1fGbps', bitrate)
        elseif counter >= 1 then return string.format('%.1fMbps', bitrate)
        else return string.format('%.1fKbps', bitrate)
        end
    end

    ---@param format FormatRaw
    local function format_special_fields(format)
        local size_prefix = not format.filesize and format.filesize_approx and '~' or ''
        ---@diagnostic disable-next-line: param-type-mismatch
        format.size = (size_prefix) .. scale_filesize(format.size)
        format.frame_rate = format.fps and format.fps .. 'fps' or ''
        format.bitrate_total = scale_bitrate(format.tbr)
        format.bitrate_video = scale_bitrate(format.vbr)
        format.bitrate_audio = scale_bitrate(format.abr)
        format.codec_video = format.vcodec == nil and 'unknown' or format.vcodec == 'none' and '' or format.vcodec
        format.codec_audio = format.acodec == nil and 'unknown' or format.acodec == 'none' and '' or format.acodec
        format.audio_sample_rate = format.asr and tostring(format.asr) .. 'Hz' or ''
    end

    for _, format in ipairs(all_formats) do
        format_special_fields(format)
    end

    ---@param raw_formats { [string]: any }
    ---@param properties string[]
    ---@return Format[]
    local function convert_to_format(raw_formats, properties)
        ---@type Format[]
        local formats = {}
        for i, format in ipairs(raw_formats) do
            local props = {}
            for _, prop in ipairs(properties) do
                props[prop] = tostring(format[prop] or '')
            end
            formats[i] = { properties = props, id = format.format_id }
        end
        return formats
    end

    return {
        video_formats = convert_to_format(video_formats, opts.columns_video.all),
        audio_formats = convert_to_format(audio_formats, opts.columns_audio.all),
        video_active_id = requested_video,
        audio_active_id = requested_audio,
    }
end

---@return string | nil
local function get_url()
    local path = mp.get_property('path')
    if not path then return nil end
    path = path:gsub('ytdl://', '') -- Strip possible ytdl:// prefix.

    ---@param str string
    ---@return boolean
    local function is_url(str)
        -- adapted the regex from
        -- https://stackoverflow.com/questions/3809401/what-is-a-good-regular-expression-to-match-a-url
        return nil ~=
            str:match(
                '^[%w]-://[-a-zA-Z0-9@:%._\\+~#=]+%.' ..
                '[a-zA-Z0-9()][a-zA-Z0-9()]?[a-zA-Z0-9()]?[a-zA-Z0-9()]?[a-zA-Z0-9()]?[a-zA-Z0-9()]?' ..
                '[-a-zA-Z0-9()@:%_\\+.~#?&/=]*')
    end

    return is_url(path) and path or nil
end

local uosc_available = false
---@type { [string]: Data }
local url_data = {}

local HLS_VARIANT_LIMIT = 12
local HLS_VARIANTS_OPTION_LENGTH_LIMIT = 32768
local HLS_VARIANTS_JSON_LENGTH_LIMIT = 24576
local HLS_VARIANT_URL_LENGTH_LIMIT = 4096
local HLS_DIMENSION_LIMIT = 16384
local HLS_BANDWIDTH_LIMIT = 1000 * 1000 * 1000
local OPTION_LENGTH_PREFIX_LIMIT = 32

---@param input string
---@return string | nil, string | nil
local function percent_decode(input, encoded_limit)
    encoded_limit = encoded_limit or HLS_VARIANTS_OPTION_LENGTH_LIMIT
    -- --script-opts consumes mpv's fixed-length syntax, while
    -- --script-opts-append can preserve it as part of the native option value.
    -- Accept one exact byte-length wrapper without treating its leading % as
    -- URL encoding. The payload retains the original encoded/decoded limits.
    local declared_length, payload = input:match('^%%(%d+)%%(.*)$')
    if declared_length then
        if tonumber(declared_length) ~= #payload then
            return nil, 'invalid fixed-length option prefix'
        end
        if payload:match('^%%%d+%%') then
            return nil, 'nested fixed-length option prefix'
        end
        input = payload
    end
    if #input > encoded_limit then return nil, 'encoded value is too large' end
    local output = {}
    local output_length = 0
    local index = 1

    while index <= #input do
        local char = input:sub(index, index)
        if char == '%' then
            local encoded = input:sub(index + 1, index + 2)
            if #encoded ~= 2 or not encoded:match('^%x%x$') then
                return nil, 'malformed percent encoding'
            end
            output[#output + 1] = string.char(tonumber(encoded, 16))
            index = index + 3
        else
            output[#output + 1] = char
            index = index + 1
        end

        output_length = output_length + 1
        if output_length > HLS_VARIANTS_JSON_LENGTH_LIMIT then
            return nil, 'decoded JSON is too large'
        end
    end

    return table.concat(output), nil
end

---@param value any
---@param maximum integer
---@return integer | nil, boolean
local function optional_positive_integer(value, maximum)
    if value == nil then return nil, true end
    if type(value) ~= 'number' or value ~= value or value < 1 or value > maximum or value % 1 ~= 0 then
        return nil, false
    end
    return value, true
end

---@param url any
---@return boolean
local function is_valid_hls_variant_url(url)
    if type(url) ~= 'string' or #url == 0 or #url > HLS_VARIANT_URL_LENGTH_LIMIT then return false end
    if url:find('[%c%s]') then return false end

    local scheme, rest = url:match('^(https?)://(.+)$')
    if not scheme or not rest then return false end

    local authority, path = rest:match('^([^/?#]+)(/[^?#]*)')
    if not authority or authority == '' or not path then return false end
    return path:lower():sub(-5) == '.m3u8'
end

---@param option_value string
---@return table[] | nil
local function parse_hls_variants(option_value)
    if option_value == '' then return nil end
    if type(option_value) ~= 'string' or #option_value > HLS_VARIANTS_OPTION_LENGTH_LIMIT + OPTION_LENGTH_PREFIX_LIMIT then
        msg.warn('ignoring hls_variants option: encoded value is too large')
        return nil
    end

    local decoded, decode_error = percent_decode(option_value)
    if not decoded then
        msg.warn('ignoring hls_variants option: ' .. decode_error)
        return nil
    end

    local parsed, parse_error = utils.parse_json(decoded)
    if type(parsed) ~= 'table' then
        msg.warn('ignoring hls_variants option: ' .. (parse_error or 'expected a JSON array'))
        return nil
    end

    local item_count = 0
    for key in pairs(parsed) do
        if type(key) ~= 'number' or key < 1 or key % 1 ~= 0 then
            msg.warn('ignoring hls_variants option: expected a JSON array')
            return nil
        end
        item_count = item_count + 1
    end
    if item_count == 0 then return nil end
    if item_count ~= #parsed or item_count > HLS_VARIANT_LIMIT then
        msg.warn('ignoring hls_variants option: invalid or oversized variant list')
        return nil
    end

    local variants = {}
    local seen_urls = {}
    for index, item in ipairs(parsed) do
        if type(item) ~= 'table' or not is_valid_hls_variant_url(item.url) then
            msg.warn('ignoring hls_variants option: invalid variant URL')
            return nil
        end

        local width, width_valid = optional_positive_integer(item.width, HLS_DIMENSION_LIMIT)
        local height, height_valid = optional_positive_integer(item.height, HLS_DIMENSION_LIMIT)
        local bandwidth, bandwidth_valid = optional_positive_integer(item.bandwidth, HLS_BANDWIDTH_LIMIT)
        if not width_valid or not height_valid or not bandwidth_valid then
            msg.warn('ignoring hls_variants option: invalid variant metadata')
            return nil
        end

        if not seen_urls[item.url] then
            seen_urls[item.url] = true
            variants[#variants + 1] = {
                url = item.url,
                width = width,
                height = height,
                bandwidth = bandwidth,
                source_index = index,
            }
        end
    end

    return #variants > 0 and variants or nil
end

local hls_variants = parse_hls_variants(opts.hls_variants)

local function is_valid_stream_url(url)
    return type(url) == 'string' and #url > 0 and #url <= HLS_VARIANT_URL_LENGTH_LIMIT
        and not url:find('[%c%s]') and url:match('^https?://[^/?#]+/.+') ~= nil
end

local function parse_stream_variants(value, raw_json)
    if value == '' then return nil end
    if type(value) ~= 'string' or #value > 24576 + OPTION_LENGTH_PREFIX_LIMIT then
        msg.warn('ignoring stream_variants: encoded list is too large')
        return nil
    end
    local decoded = raw_json and value or percent_decode(value, 24576)
    local parsed = decoded and utils.parse_json(decoded)
    if type(parsed) ~= 'table' or #parsed < 1 or #parsed > 16 then
        msg.warn('ignoring stream_variants: invalid variant list')
        return nil
    end
    local count = 0
    for key in pairs(parsed) do
        if type(key) ~= 'number' or key < 1 or key % 1 ~= 0 then return nil end
        count = count + 1
    end
    if count ~= #parsed then return nil end

    local variants, seen = {}, {}
    for index, item in ipairs(parsed) do
        if type(item) ~= 'table' or not is_valid_stream_url(item.url)
            or (item.audio_url ~= nil and not is_valid_stream_url(item.audio_url)) then
            msg.warn('ignoring stream_variants: invalid media URL')
            return nil
        end
        local width, width_valid = optional_positive_integer(item.width, HLS_DIMENSION_LIMIT)
        local height, height_valid = optional_positive_integer(item.height, HLS_DIMENSION_LIMIT)
        local bandwidth, bandwidth_valid = optional_positive_integer(item.bandwidth, HLS_BANDWIDTH_LIMIT)
        local fps_valid = item.fps == nil or (type(item.fps) == 'number' and item.fps > 0 and item.fps <= 240)
        local function valid_label(label, maximum)
            return label == nil or (type(label) == 'string' and #label <= maximum and not label:find('%c'))
        end
        if not width_valid or not height_valid or not bandwidth_valid or not fps_valid
            or not valid_label(item.title, 360) or not valid_label(item.codec, 64) then
            msg.warn('ignoring stream_variants: invalid metadata')
            return nil
        end
        if not seen[item.url] then
            seen[item.url] = true
            variants[#variants + 1] = {
                url = item.url, audio_url = item.audio_url, title = item.title,
                width = width, height = height, bandwidth = bandwidth,
                fps = item.fps, codec = item.codec, source_index = index,
            }
        end
    end
    return variants
end

local stream_variants = parse_stream_variants(opts.stream_variants)
local launch_stream_variants = stream_variants
local function playlist_media(url)
    local media = mp.get_property_native('user-data/mpv-lazy/web-playlist/current')
    if type(media) ~= 'table' or media.path ~= url or type(media.identity) ~= 'string'
        or not media.identity:match('^bilibili:%d+:%d+$') then return nil end
    return media
end

local function is_direct_hls_url(url)
    if not url then return false end
    local path = url:match('^https?://[^/]+/([^?#]+)')
    return path ~= nil and (path:lower():match('%.m3u8$') ~= nil or path:lower():match('%.m3u$') ~= nil)
end

local function should_skip_fetch_formats(url)
    if is_direct_hls_url(url) or mp.get_property_bool('ytdl', true) == false then return true end
    local path = url and url:match('^https?://[^/]+/([^?#]+)')
    return path ~= nil and (path:lower():match('%.flv$') ~= nil
        or path:lower():match('%.mp4$') ~= nil or path:lower():match('%.m4s$') ~= nil
        or path:lower():match('%.webm$') ~= nil or path:lower():match('%.mpd$') ~= nil)
end

---@param bitrate integer | number | nil
---@return string
local function hls_bitrate_id(bitrate)
    return bitrate and tostring(math.floor(bitrate + 0.5)) or ''
end

---@param bitrate integer | number | nil
---@return string
local function hls_bitrate_label(bitrate)
    if not bitrate then return '' end
    local mbps = bitrate / 1024 / 1024
    if mbps >= 1 then return string.format('%.2fMbps', mbps) end
    return string.format('%.0fKbps', bitrate / 1024)
end

---@param title string | nil
---@return number | nil
local function hls_title_bitrate(title)
    if not title then return nil end

    local mbps = title:match('([%d%.]+)%s*Mbps')
    if mbps then return tonumber(mbps) and tonumber(mbps) * 1024 * 1024 or nil end

    local kbps = title:match('([%d%.]+)%s*kbps') or title:match('([%d%.]+)%s*Kbps')
    if kbps then return tonumber(kbps) and tonumber(kbps) * 1024 or nil end

    return nil
end

---@param title string | nil
---@return string
local function hls_title_resolution_label(title)
    if not title then return '' end

    local width, height = title:match('(%d+)%s*x%s*(%d+)')
    if height then return height .. 'p' end

    local label = title:match('(%d+p)')
    return label or ''
end

---@param title string | nil
---@return string
local function hls_title_resolution_hint(title)
    if not title then return '' end

    local width, height = title:match('(%d+)%s*x%s*(%d+)')
    if width and height then return width .. 'x' .. height end
    return ''
end

---@param track table
---@return string
local function hls_resolution_label(track)
    local width = track['demux-w']
    local height = track['demux-h']
    if height then return tostring(height) .. 'p' end
    if width then return tostring(width) .. 'w' end
    return ''
end

---@param track table
---@return string
local function hls_resolution_hint(track)
    local width = track['demux-w']
    local height = track['demux-h']
    if width and height then return tostring(width) .. 'x' .. tostring(height) end
    return ''
end

---@param properties { [string]: string }
---@return { [string]: string }
local function hls_properties(properties)
    return setmetatable(properties, { __index = function() return '' end })
end

---@param url string | nil
---@return table | nil
local function find_hls_variant(url, variants)
    variants = variants or hls_variants
    if not variants or not url then return nil end
    for _, variant in ipairs(variants) do
        if variant.url == url then return variant end
    end
    return nil
end

---@param url string | nil
---@return Data | nil
local function create_hls_data_from_url_variants(url, variants, browser_stream)
    variants = variants or hls_variants
    if not find_hls_variant(url, variants) then return nil end

    local sorted_variants = {}
    local label_counts = {}
    for _, variant in ipairs(variants) do
        sorted_variants[#sorted_variants + 1] = variant

        local resolution = variant.height and tostring(variant.height) .. 'p'
            or variant.width and tostring(variant.width) .. 'w'
            or ''
        local bitrate_label = hls_bitrate_label(variant.bandwidth)
        local base_label = resolution
        if base_label ~= '' and bitrate_label ~= '' then
            base_label = base_label .. ' (' .. bitrate_label .. ')'
        elseif base_label == '' then
            base_label = bitrate_label
        end
        if variant.title and variant.title ~= '' then base_label = variant.title end
        if base_label == '' then base_label = browser_stream and 'Stream' or 'HLS Variant' end
        label_counts[base_label] = (label_counts[base_label] or 0) + 1
    end

    table.sort(sorted_variants, function(a, b)
        local a_height = a.height or 0
        local b_height = b.height or 0
        if a_height ~= b_height then return a_height > b_height end

        local a_width = a.width or 0
        local b_width = b.width or 0
        if a_width ~= b_width then return a_width > b_width end

        local a_bandwidth = a.bandwidth or 0
        local b_bandwidth = b.bandwidth or 0
        if a_bandwidth ~= b_bandwidth then return a_bandwidth > b_bandwidth end

        return a.source_index < b.source_index
    end)

    local video_formats = {}
    for _, variant in ipairs(sorted_variants) do
        local resolution = variant.height and tostring(variant.height) .. 'p'
            or variant.width and tostring(variant.width) .. 'w'
            or ''
        local resolution_hint = variant.width and variant.height
            and tostring(variant.width) .. 'x' .. tostring(variant.height)
            or ''
        local bitrate_label = hls_bitrate_label(variant.bandwidth)
        local label = resolution
        if label ~= '' and bitrate_label ~= '' then
            label = label .. ' (' .. bitrate_label .. ')'
        elseif label == '' then
            label = bitrate_label
        end
        if variant.title and variant.title ~= '' then label = variant.title end
        if label == '' then label = browser_stream and 'Stream' or 'HLS Variant' end
        if label_counts[label] > 1 then label = label .. ' #' .. tostring(variant.source_index) end

        local hint_parts = { browser_stream and 'Browser stream' or 'HLS URL' }
        if resolution_hint ~= '' then hint_parts[#hint_parts + 1] = resolution_hint end
        if bitrate_label ~= '' then hint_parts[#hint_parts + 1] = bitrate_label end
        if variant.fps then hint_parts[#hint_parts + 1] = tostring(variant.fps) .. 'fps' end
        if variant.codec then hint_parts[#hint_parts + 1] = variant.codec end

        video_formats[#video_formats + 1] = {
            id = variant.url,
            label = label,
            title = label,
            hint = table.concat(hint_parts, ', '),
            properties = hls_properties({
                resolution = resolution,
                width = variant.width and tostring(variant.width) or '',
                height = variant.height and tostring(variant.height) or '',
                fps = '',
                dynamic_range = '',
                tbr = bitrate_label,
                vbr = bitrate_label,
                abr = '',
                asr = '',
                filesize = '',
                filesize_approx = '',
                vcodec = '',
                acodec = '',
                ext = 'm3u8',
                video_ext = 'm3u8',
                audio_ext = '',
                language = '',
                format = 'HLS URL',
                format_note = 'Browser variant',
                quality = resolution ~= '' and resolution or label,
                size = '',
                frame_rate = '',
                bitrate_total = bitrate_label,
                bitrate_video = bitrate_label,
                bitrate_audio = '',
                codec_video = '',
                codec_audio = '',
                audio_sample_rate = '',
            }),
        }
    end

    return {
        hls = true,
        browser_stream = browser_stream,
        hls_mode = 'url',
        video_formats = video_formats,
        audio_formats = {},
        video_active_id = url,
        audio_active_id = '',
    }
end

---@return Data | nil
local function create_hls_data_from_track_list()
    local tracks = mp.get_property_native('track-list') or {}
    local seen = {}
    local video_formats = {}
    local active_id = nil

    for _, track in ipairs(tracks) do
        local bitrate = track['hls-bitrate']
        local id = hls_bitrate_id(bitrate)
        if track.type == 'video' and id ~= '' and not seen[id] then
            seen[id] = true
            local resolution = hls_resolution_label(track)
            local resolution_hint = hls_resolution_hint(track)
            local bitrate_label = hls_bitrate_label(bitrate)
            local height = track['demux-h'] or 0
            local label = resolution ~= '' and resolution or bitrate_label
            local hint_parts = { 'HLS' }
            if resolution_hint ~= '' and resolution_hint ~= label then hint_parts[#hint_parts + 1] = resolution_hint end
            if bitrate_label ~= '' and bitrate_label ~= label then hint_parts[#hint_parts + 1] = bitrate_label end
            video_formats[#video_formats + 1] = {
                id = id,
                label = label,
                title = label,
                hint = table.concat(hint_parts, ', '),
                properties = hls_properties({
                    resolution = label,
                    width = tostring(track['demux-w'] or ''),
                    height = height > 0 and tostring(height) or '',
                    fps = track['demux-fps'] and tostring(track['demux-fps']) or '',
                    dynamic_range = '',
                    tbr = bitrate_label,
                    vbr = bitrate_label,
                    abr = '',
                    asr = '',
                    filesize = '',
                    filesize_approx = '',
                    vcodec = track.codec or '',
                    acodec = '',
                    ext = 'm3u8',
                    video_ext = 'm3u8',
                    audio_ext = '',
                    language = track.lang or '',
                    format = 'HLS',
                    format_note = '',
                    quality = label,
                    size = '',
                    frame_rate = track['demux-fps'] and tostring(track['demux-fps']) .. 'fps' or '',
                    bitrate_total = bitrate_label,
                    bitrate_video = bitrate_label,
                    bitrate_audio = '',
                    codec_video = track.codec or '',
                    codec_audio = '',
                    audio_sample_rate = '',
                }),
            }
            if track.selected then active_id = id end
        end
    end

    if #video_formats == 0 then return nil end

    table.sort(video_formats, function(a, b)
        return tonumber(a.id) > tonumber(b.id)
    end)

    return {
        hls = true,
        hls_mode = 'bitrate',
        video_formats = video_formats,
        audio_formats = {},
        video_active_id = active_id,
        audio_active_id = '',
    }
end

---@return Data | nil
local function create_hls_data_from_edition_list()
    local editions = mp.get_property_native('edition-list') or {}
    local current_edition = mp.get_property_native('current-edition')
    if #editions <= 1 then return nil end

    local video_formats = {}
    for _, edition in ipairs(editions) do
        local id = tostring(edition.id)
        local title = edition.title or ''
        local resolution = hls_title_resolution_label(title)
        local resolution_hint = hls_title_resolution_hint(title)
        local bitrate = hls_title_bitrate(title)
        local bitrate_label = hls_bitrate_label(bitrate)
        local label = resolution ~= '' and resolution or ('Edition ' .. id)
        local hint_parts = { 'HLS' }
        if resolution_hint ~= '' and resolution_hint ~= label then hint_parts[#hint_parts + 1] = resolution_hint end
        if bitrate_label ~= '' then hint_parts[#hint_parts + 1] = bitrate_label end
        if #hint_parts == 1 and title ~= '' then hint_parts[#hint_parts + 1] = title end

        video_formats[#video_formats + 1] = {
            id = id,
            label = label,
            title = label,
            hint = table.concat(hint_parts, ', '),
            properties = hls_properties({
                resolution = label,
                width = resolution_hint:match('^(%d+)x') or '',
                height = resolution_hint:match('x(%d+)$') or '',
                fps = title:match('(%d+)%s*fps') or '',
                dynamic_range = '',
                tbr = bitrate_label,
                vbr = bitrate_label,
                abr = '',
                asr = '',
                filesize = '',
                filesize_approx = '',
                vcodec = title:match('%(([%w%d]+)%s*%[') or '',
                acodec = '',
                ext = 'm3u8',
                video_ext = 'm3u8',
                audio_ext = '',
                language = '',
                format = 'HLS',
                format_note = '',
                quality = label,
                size = '',
                frame_rate = title:match('(%d+)%s*fps') and title:match('(%d+)%s*fps') .. 'fps' or '',
                bitrate_total = bitrate_label,
                bitrate_video = bitrate_label,
                bitrate_audio = '',
                codec_video = title:match('%(([%w%d]+)%s*%[') or '',
                codec_audio = '',
                audio_sample_rate = '',
            }),
        }
    end

    table.sort(video_formats, function(a, b)
        local ah = tonumber(a.properties.height) or 0
        local bh = tonumber(b.properties.height) or 0
        if ah ~= bh then return ah > bh end
        return tonumber(a.id) > tonumber(b.id)
    end)

    return {
        hls = true,
        hls_mode = 'edition',
        video_formats = video_formats,
        audio_formats = {},
        video_active_id = current_edition ~= nil and tostring(current_edition) or nil,
        audio_active_id = '',
    }
end

---@param url string | nil
---@return Data | nil
local function refresh_hls_data(url)
    local media = playlist_media(url)
    if media then
        stream_variants = parse_stream_variants(utils.format_json(media.variants or {}), true)
        local data = stream_variants and create_hls_data_from_url_variants(media.video, stream_variants, true)
        if data then data.playlist_stream = true; url_data[url] = data; return data end
        return nil
    end
    stream_variants = launch_stream_variants
    local stream_data = stream_variants and create_hls_data_from_url_variants(url, stream_variants, true)
    if stream_data then
        stream_data.switch_failed = url_data[url] and url_data[url].switch_failed
        url_data[url] = stream_data
        return stream_data
    end
    if not is_direct_hls_url(url) then return nil end

    local url_variant_data = create_hls_data_from_url_variants(url)
    if url_variant_data then
        url_variant_data.switch_failed = url_data[url] and url_data[url].switch_failed
        url_data[url] = url_variant_data
        return url_variant_data
    end

    local previous = url_data[url]
    local data = create_hls_data_from_edition_list() or create_hls_data_from_track_list()
    if data then
        if previous and previous.hls and previous.video_active_id and not data.video_active_id then
            data.video_active_id = previous.video_active_id
        end
    elseif previous and previous.hls then
        data = previous
    else
        data = {
            hls = true,
            hls_mode = 'edition',
            video_formats = {},
            audio_formats = {},
            video_active_id = nil,
            audio_active_id = '',
        }
    end

    url_data[url] = data
    return data
end

local function unavailable_data(message)
    return { video_formats = {}, audio_formats = {}, unavailable = message }
end

local function current_stream_data(url)
    local parameters = mp.get_property_native('video-params') or {}
    local height, width = parameters.h, parameters.w
    local title = height and (tostring(height) .. 'p（当前源）') or '当前播放源'
    return {
        hls = true, hls_mode = 'url', browser_stream = true,
        direct_current = true,
        video_active_id = url, audio_active_id = '', audio_formats = {},
        video_formats = {{id = url, title = title, label = title,
            hint = '网站仅移交了当前源；请在网页选择清晰度后重新移交',
            properties = hls_properties({height = tostring(height or ''), width = tostring(width or '')})}},
    }
end

local function uosc_set_format_counts()
    local data = url_data[current_url]
    -- Safe diagnostics for integration tests and support: never expose signed
    -- media URLs, cookies, headers, or format IDs.
    local state = {
        status = data and (data.unavailable and 'unavailable' or 'ready') or 'loading',
        source = data and (data.direct_current and 'direct' or data.browser_stream and 'browser'
            or data.hls and 'hls' or 'ytdl') or 'unknown',
        video = {}, videoCount = data and #data.video_formats or 0,
        audioCount = data and #data.audio_formats or 0,
    }
    if not current_url then state.status = 'idle' end
    if pending_browser_switch then state.status = 'loading' end
    if data and data.switch_failed then state.status = 'unavailable' end
    if data then
        for _, format in ipairs(data.video_formats) do
            local properties = format.properties or {}
            local height, width = tonumber(properties.height), tonumber(properties.width)
            state.video[#state.video + 1] = {title = format.title or format.label or '', height = height, width = width}
            if not data.switch_failed and format.id == data.video_active_id
                and (data.hls_mode ~= 'url' or current_url == loaded_url) then
                state.activeHeight = height
            end
        end
    end
    mp.set_property_native('user-data/quality-menu/state', state)
    if not uosc_available then return end
    if data then
        mp.commandv('script-message-to', 'uosc', 'set', 'vformats', #data.video_formats)
        mp.commandv('script-message-to', 'uosc', 'set', 'aformats', #data.audio_formats)
    else
        mp.commandv('script-message-to', 'uosc', 'set', 'vformats', 0)
        mp.commandv('script-message-to', 'uosc', 'set', 'aformats', 0)
    end
end

---@param json string
---@return Data | nil
local function process_json_string(json)
    local json_table, err = utils.parse_json(json)

    if (json_table == nil) then
        if err == nil then err = 'unexpected error occurred' end
        msg.error('failed to parse JSON data: ' .. err)
        return
    end

    if type(json_table.formats) ~= 'table' then
        return
    end

    local success, data = pcall(process_json, json_table)
    if not success then
        msg.warn('invalid format metadata returned by yt-dlp')
        return nil
    end
    return data
end

---Unknown format falls back on highest ranked format if possible
---@param id string | nil
---@param formats Format[]
---@return string
local function sanitize_format_id(id, formats)
    return id or (formats[1] or {}).id or ''
end

---@param video_id string
---@param audio_id string
---@return string
local function format_string(video_id, audio_id)
    if #video_id > 0 and #audio_id > 0 then
        return video_id .. '+' .. audio_id
    elseif #video_id > 0 then
        return video_id
    elseif #audio_id > 0 then
        return audio_id
    else
        return ''
    end
end

local browser_switch_timer = nil
local stream_switch_timer = nil
local stream_switch_generation = 0
local STREAM_SWITCH_PROPERTY = 'user-data/quality-menu/stream-switch'

begin_stream_switch = function(target_url, resume_position)
    local progress = mp.get_property_native('user-data/mpv-lazy/playback-progress')
    if type(progress) == 'table' and progress.active == true and progress.ready == true then
        mp.set_property_native('user-data/mpv-lazy/progress-stream-switch', {
            sessionId = progress.sessionId, identity = progress.identity,
            from = mp.get_property('path'), to = target_url, expiresAt = mp.get_time() + 15,
            resumePosition = tonumber(resume_position) or mp.get_property_number('time-pos', progress.position),
        })
    end
    local session = mp.get_property_native('user-data/uosc_danmaku/live-session')
    if not mp.get_property_bool('user-data/uosc_danmaku/live-active', false)
        or type(session) ~= 'string' or session == '' then return end
    if stream_switch_timer then stream_switch_timer:kill() end
    stream_switch_generation = stream_switch_generation + 1
    local generation = stream_switch_generation
    mp.set_property_native(STREAM_SWITCH_PROPERTY, {
        generation = generation, provider = 'bilibili', sessionId = session,
        from = mp.get_property('path'), to = target_url,
        startedAt = mp.get_time(), expiresAt = os.time() + 15, stage = 'loading',
    })
    stream_switch_timer = mp.add_timeout(15, function()
        local marker = mp.get_property_native(STREAM_SWITCH_PROPERTY)
        if marker and marker.generation == generation then
            mp.set_property_native(STREAM_SWITCH_PROPERTY, nil)
        end
    end)
end

mp.register_event('file-loaded', function()
    loaded_url = mp.get_property('path')
    if url_data[loaded_url] then url_data[loaded_url].switch_failed = nil end
    if pending_browser_switch == mp.get_property('path') then
        pending_browser_switch = nil
        if browser_switch_timer then browser_switch_timer:kill() end
    end
    local marker = mp.get_property_native(STREAM_SWITCH_PROPERTY)
    if marker and marker.to == mp.get_property('path') then
        marker.stage = 'loaded'
        mp.set_property_native(STREAM_SWITCH_PROPERTY, marker)
    end
end)

mp.register_event('end-file', function(event)
    if event and event.reason == 'error' and pending_browser_switch then
        if url_data[pending_browser_switch] then url_data[pending_browser_switch].switch_failed = true end
        pending_browser_switch = nil
        if browser_switch_timer then browser_switch_timer:kill() end
        if stream_switch_timer then stream_switch_timer:kill() end
        mp.set_property_native(STREAM_SWITCH_PROPERTY, nil)
        uosc_set_format_counts()
        osd_message('切换清晰度失败，请在浏览器刷新播放地址后重试', 5)
    end
end)

---@param target_url string
local function load_hls_variant_resume(target_url)
    local reload_duration = mp.get_property_native('duration')
    local time_pos = mp.get_property('time-pos')

    prepare_reload_resume(target_url, reload_duration, time_pos)

    msg.verbose('switching browser-resolved stream')
    pending_browser_switch = target_url
    if url_data[target_url] then url_data[target_url].switch_failed = nil end
    if browser_switch_timer then browser_switch_timer:kill() end
    browser_switch_timer = mp.add_timeout(15, function()
        if pending_browser_switch ~= target_url then return end
        if url_data[target_url] then url_data[target_url].switch_failed = true end
        pending_browser_switch = nil
        uosc_set_format_counts()
        osd_message('切换清晰度超时，请在浏览器刷新播放地址后重试', 5)
    end)
    begin_stream_switch(target_url, time_pos)
    mp.commandv('loadfile', target_url, 'replace')
end

---@param url string
---@param video_format string
---@param audio_format string
local function set_format(url, video_format, audio_format)
    if stream_switch_in_progress() then
        osd_message('正在切换清晰度，请稍候', 3)
        return
    end
    local data = url_data[url]
    if not data then return end

    if data.playlist_stream then
        if data.video_active_id == video_format then return end
        local media = playlist_media(url)
        local variants = media and parse_stream_variants(utils.format_json(media.variants or {}), true)
        if not variants or not find_hls_variant(video_format, variants) then return end
        mp.commandv('script-message-to', 'web_playlist', 'web-playlist-quality', video_format)
        return
    end

    if data.hls and data.hls_mode == 'url' then
        if video_format == '' or (data.video_active_id == video_format and url == loaded_url) then return end

        local target_data = data.browser_stream
            and create_hls_data_from_url_variants(video_format, stream_variants, true)
            or create_hls_data_from_url_variants(video_format)
        if not target_data then
            msg.warn('refusing unknown HLS variant URL')
            return
        end

        url_data[video_format] = target_data
        if url == mp.get_property('path') then load_hls_variant_resume(video_format) end
        return
    end

    if (data.video_active_id ~= video_format or data.audio_active_id ~= audio_format) then
        data.video_active_id = video_format
        data.audio_active_id = audio_format
        if url == mp.get_property('path') then reload_resume() end
    end
end

---@param formats Format[]
---@param active_format string | nil
---@param menu_type UIState
local function text_menu_open(formats, active_format, menu_type)
    local menu_data = url_data[current_url] or {}
    local allow_disabled = not menu_data.hls
    local active = 0
    local selected = 1
    --set the cursor to the current format
    for i, format in ipairs(formats) do
        if format.id == active_format then
            active = i
            selected = active
            break
        end
    end
    if active_format == '' then
        active = #formats + 1
        selected = active
    end

    ---@param i integer
    ---@return string
    local function choose_prefix(i)
        if i == selected and i == active then return opts.selected_and_active
        elseif i == selected then return opts.selected_and_inactive end

        if i ~= selected and i == active then return opts.unselected_and_active
        elseif i ~= selected then return opts.unselected_and_inactive end
        return '> ' --shouldn't get here.
    end

    local width, height
    local margin_top, margin_bottom = 0, 0
    local num_options = #formats > 0 and #formats + (allow_disabled and 2 or 1) or 1

    ---@return integer
    local function get_scrolled_lines()
        local output_height = height - opts.text_padding_y * 2 - margin_top * height - margin_bottom * height
        local screen_lines = math.max(math.floor(output_height / opts.font_size), 1)
        local max_scroll = math.max(num_options - screen_lines, 0)
        return math.min(math.max(selected - math.ceil(screen_lines / 2), 0), max_scroll)
    end

    local function draw_menu()
        local ass = assdraw.ass_new()

        if opts.curtain_opacity > 0 then
            local alpha = 255 - math.ceil(255 * opts.curtain_opacity)
            ass.text = string.format('{\\pos(0,0)\\rDefault\\an7\\1c&H000000&\\alpha&H%X&}', alpha)
            ass:draw_start()
            ass:rect_cw(0, 0, width, height)
            ass:draw_stop()
            ass:new_event()
        end

        local scrolled_lines = get_scrolled_lines()
        local pos_y = opts.shift_y + margin_top * height + opts.text_padding_y - scrolled_lines * opts.font_size
        ass:pos(opts.shift_x + opts.text_padding_x, pos_y)
        local clip_top = math.floor(margin_top * height + 0.5)
        local clip_bottom = math.floor((1 - margin_bottom) * height + 0.5)
        local clipping_coordinates = '0,' .. clip_top .. ',' .. width .. ',' .. clip_bottom
        ass:append('{\\rDefault\\an7\\q2\\clip(' .. clipping_coordinates .. ')}' .. opts.style_ass_tags)

        if #formats > 0 then
            for i, format in ipairs(formats) do
                ass:append(choose_prefix(i) .. format.label .. '\\N')
            end
            if allow_disabled then ass:append(choose_prefix(#formats + 1) .. 'Disabled\\N') end
            ass:append(choose_prefix(num_options) .. menu_type.to_other_type.type_capitalized .. ' menu')
        else
            ass:append((menu_data.unavailable or '此播放源没有可切换的档位') .. '\\N')
            ass:append(opts.selected_and_inactive .. menu_type.to_other_type.type_capitalized .. ' menu')
        end

        osd.data = ass.text
        osd:update()
    end

    local function update_dimensions()
        local _, h, aspect = mp.get_osd_size()
        if opts.scale_playlist_by_window then h = 720 end
        height = h
        width = height * aspect
        osd.res_y = height
        osd.res_x = width
        draw_menu()
    end

    local update_margins = function(_, val)
        if not val then
            val = mp.get_property_native('user-data/osc/margins')
        end
        if val then
            margin_top = val.t
            margin_bottom = val.b
        else
            margin_top = 0
            margin_bottom = 0
        end
        draw_menu()
    end
    mp.observe_property('user-data/osc/margins', 'native', update_margins)

    update_dimensions()
    update_margins()
    mp.observe_property('osd-dimensions', 'native', update_dimensions)

    ---@param amount integer
    local function selected_move(amount)
        selected = selected + amount
        if selected < 1 then selected = num_options
        elseif selected > num_options then selected = 1 end
        if osd_timer then
            osd_timer:kill()
            osd_timer:resume()
        end
        draw_menu()
    end

    ---@param keys string | nil
    ---@param name string
    ---@param func function
    ---@param opts table | nil
    local function bind_keys(keys, name, func, opts)
        if not keys then
            mp.add_forced_key_binding(keys, name, func, opts)
            return
        end
        local i = 1
        for key in keys:gmatch('[^%s]+') do
            local prefix = i == 1 and '' or i
            mp.add_forced_key_binding(key, name .. prefix, func, opts)
            i = i + 1
        end
    end

    ---@param keys string | nil
    ---@param name string
    local function unbind_keys(keys, name)
        if not keys then
            mp.remove_key_binding(name)
            return
        end
        local i = 1
        for key in keys:gmatch('[^%s]+') do
            local prefix = i == 1 and '' or i
            mp.remove_key_binding(name .. prefix)
            i = i + 1
        end
    end

    -- make sure observers are cleaned up
    if open_menu_state and open_menu_state == open_menu_state.to_menu and destructor then destructor() end
    destructor = function()
        unbind_keys(opts.up_binding, 'move_up')
        unbind_keys(opts.down_binding, 'move_down')
        unbind_keys(opts.select_binding, 'select')
        unbind_keys(opts.close_menu_binding, 'close')
        mp.unobserve_property(update_dimensions)
        mp.unobserve_property(update_margins)
    end

    osd_timer:kill()
    if opts.menu_timeout > 0 then
        osd_timer.timeout = opts.menu_timeout
        osd_timer:resume()
    end

    bind_keys(opts.up_binding, 'move_up', function() selected_move( -1) end, { repeatable = true })
    bind_keys(opts.down_binding, 'move_down', function() selected_move(1) end, { repeatable = true })
    bind_keys(opts.close_menu_binding, 'close', menu_close)
    bind_keys(opts.select_binding, 'select', function()
        if selected == num_options then
            mp.unobserve_property(update_dimensions)
            mp.unobserve_property(update_margins)
            if menu_type.is_video then audio_formats_toggle()
            else video_formats_toggle() end
            return
        end
        menu_close()
        if selected == active then return end
        if current_url == nil then return end

        local video_id, audio_id
        local id = formats[selected] and formats[selected].id or ''
        local data = url_data[current_url]
        if menu_type.is_video then
            video_id = id
            audio_id = sanitize_format_id(data.audio_active_id, data.audio_formats)
        else
            video_id = sanitize_format_id(data.video_active_id, data.video_formats)
            audio_id = id
        end
        set_format(current_url, video_id, audio_id)
    end)

    osd.hidden = false
    draw_menu()
end

---@param menu table
---@param menu_type UIState
local function uosc_show_menu(menu, menu_type)
    local json = utils.format_json(menu)
    -- always using update wouldn't work, because it doesn't support the on_close command
    -- therefore opening a different kind requires `open-menu`
    -- while updating the same kind requires `update-menu`
    if open_menu_state == menu_type then mp.commandv('script-message-to', 'uosc', 'update-menu', json)
    else mp.commandv('script-message-to', 'uosc', 'open-menu', json) end
end

---@param formats Format[]
---@param active_format string | nil
---@param menu_type UIState
local function uosc_menu_open(formats, active_format, menu_type)
    local menu = {
        title = menu_type.type_capitalized .. ' Formats',
        items = {},
        type = 'quality-menu-' .. menu_type.name,
        keep_open = true,
        on_close = {
            'script-message-to',
            script_name,
            'uosc-menu-closed',
            menu_type.name,
        }
    }

    menu.items[#menu.items + 1] = {
        title = menu_type.to_other_type.type_capitalized,
        italic = true,
        bold = true,
        hint = 'open menu',
        value = {
            'script-message-to',
            script_name,
            menu_type.to_other_type.type .. '_formats_toggle',
        },
    }
    local data = url_data[current_url] or {}
    if #formats == 0 then
        menu.items[#menu.items + 1] = {
            title = data.unavailable or '此播放源没有可切换的档位',
            selectable = false, muted = true, value = 'ignore',
        }
    end
    if not data.hls then menu.items[#menu.items + 1] = {
        title = 'Disabled',
        italic = true,
        muted = true,
        hint = '—',
        active = active_format == '',
        value = {
            'script-message-to',
            script_name,
            menu_type.type .. '-format-set',
            current_url,
            '',
        }
    } end

    for _, format in ipairs(formats) do
        menu.items[#menu.items + 1] = {
            title = format.title,
            hint = format.hint,
            active = format.id == active_format,
            value = {
                'script-message-to',
                script_name,
                menu_type.type .. '-format-set',
                current_url,
                format.id,
            }
        }
    end

    uosc_show_menu(menu, menu_type)
    destructor = function()
        mp.commandv('script-message-to', 'uosc', 'close-menu', menu.type)
    end
end

---Check if property is same for all formats
---@param formats Format[]
---@param properties string[]
---@return { [string]: boolean }
local function identical_for_all(formats, properties)
    ---@param formats Format[]
    ---@param prop string
    ---@return boolean
    local function all_formats_same_value(formats, prop)
        local first_value = nil
        for _, format in ipairs(formats) do
            first_value = first_value or format.properties[prop]
            if format.properties[prop] ~= first_value then return false end
        end
        return true
    end

    local identical_props = {}
    for _, prop in ipairs(properties) do
        identical_props[prop] = all_formats_same_value(formats, prop)
    end
    return identical_props
end

---@param formats Format[]
---@param columns string[]
---@param column_align_left boolean[]
---@return string[]
local function format_table(formats, columns, column_align_left)
    local column_widths = {}
    for _, format in pairs(formats) do
        for col, prop in ipairs(columns) do
            local width = format.properties[prop]:len()
            if not column_widths[col] or column_widths[col] < width then
                column_widths[col] = width
            end
        end
    end

    local identical_columns = identical_for_all(formats, columns)

    local show_columns = {}
    for i, width in ipairs(column_widths) do
        local prop = columns[i]
        if width > 0 and not (opts.hide_identical_columns and identical_columns[prop]) then
            show_columns[#show_columns + 1] = {
                prop = prop,
                width = width,
                align_left = column_align_left[prop]
            }
        end
    end

    local spacing = 2
    ---@type string[]
    local rows = {}
    for i, format in ipairs(formats) do
        local row = {}
        for j, column in ipairs(show_columns) do
            -- lua errors out with width > 99 ("invalid conversion specification")
            local width = math.min(column.width * (column.align_left and -1 or 1), 99)
            row[j] = string.format('%' .. width .. 's', format.properties[column.prop] or '')
        end
        rows[i] = table.concat(row, string.format('%' .. spacing .. 's', '')):gsub('%s+$', '')
    end
    return rows
end

---@param formats Format[]
---@param columns string[]
---@return string[]
local function format_csv(formats, columns)
    local identical_props = identical_for_all(formats, columns)
    local hints = {}
    for i, format in ipairs(formats) do
        local row = {}
        for _, prop in ipairs(columns) do
            local val = format.properties[prop]
            if #val > 0 and not (opts.hide_identical_columns and identical_props[prop]) then
                row[#row + 1] = val
            end
        end
        hints[i] = table.concat(row, ', ')
    end
    return hints
end

---@param formats Format[]
---@param menu_type UIState
local function ensure_menu_data_filled(formats, menu_type)
    if uosc_available then
        if formats[1] and formats[1].title == nil then
            local columns = menu_type.is_video and opts.columns_video or opts.columns_audio
            local titles = format_table(formats, columns.title, columns.title_align_left)

            local hints = {}
            if columns.hint then
                hints = format_csv(formats, columns.hint)
            end

            for i, format in ipairs(formats) do
                format.title = titles[i]
                format.hint = hints[i]
            end
        end
    else
        if formats[1] and formats[1].label == nil then
            local columns = menu_type.is_video and opts.columns_video or opts.columns_audio
            local labels = format_table(formats, columns.all, columns.all_align_left)
            for i, format in ipairs(formats) do format.label = labels[i] end
        end
    end
end

---@param menu_type UIState
local function loading_message(menu_type)
    menu_type = menu_type.to_fetching
    if uosc_available then
        if open_menu_state and open_menu_state == menu_type then return end
        local menu = {
            title = menu_type.type_capitalized .. ' Formats',
            items = { { icon = 'spinner', selectable = false, value = 'ignore' } },
            type = 'quality-menu-' .. menu_type.name,
            keep_open = true,
            on_close = {
                'script-message-to',
                script_name,
                'uosc-menu-closed',
                menu_type.name
            }
        }
        uosc_show_menu(menu, menu_type)
        destructor = function()
            mp.commandv('script-message-to', 'uosc', 'close-menu', menu.type)
        end
    else
        osd_message('fetching available ' .. menu_type.type .. ' formats...', 60)
    end
    open_menu_state = menu_type
    uosc_set_format_counts()
    if fetching_timer then fetching_timer:kill() end
    local source_url = current_url
    fetching_timer = mp.add_timeout(math.max(1, tonumber(opts.fetch_timeout) or 20), function()
        if current_url ~= source_url or url_data[source_url] then return end
        url_data[source_url] = unavailable_data('获取清晰度超时；请刷新网页后重新移交播放')
        uosc_set_format_counts()
        if open_menu_state and open_menu_state == open_menu_state.to_fetching then
            menu_open(open_menu_state)
        end
    end)
end

---@param menu_type UIState
function menu_open(menu_type)
    if not current_url then return end
    menu_type = menu_type.to_menu

    local data = url_data[current_url]
    data = refresh_hls_data(current_url) or data
    if not data then
        if opts.fetch_formats and not should_skip_fetch_formats(current_url) then
            loading_message(menu_type)
            return
        end

        if should_skip_fetch_formats(current_url) then
            data = current_stream_data(current_url)
        else
            -- shallow clone so that each url has its own active format ids
            data = {}
            for k, v in pairs(opts.predefined_data) do data[k] = v end
        end
        url_data[current_url] = data
    end
    if fetching_timer then fetching_timer:kill() end
    uosc_set_format_counts()
    local formats = menu_type.is_video and data.video_formats or data.audio_formats
    local active_format
    if menu_type.is_video then active_format = data.video_active_id
    else active_format = data.audio_active_id end
    if data.switch_failed or (data.hls_mode == 'url' and current_url ~= loaded_url) then active_format = nil end

    msg.verbose('current ytdl-format: ' .. mp.get_property('ytdl-format', ''))

    ensure_menu_data_filled(formats, menu_type)
    if uosc_available then uosc_menu_open(formats, active_format, menu_type)
    else text_menu_open(formats, active_format, menu_type) end
    open_menu_state = menu_type
end

function menu_close()
    if destructor then
        destructor()
        destructor = nil
    end
    if not osd.hidden then hide_osd() end
    open_menu_state = nil
end

---@param menu_type UIState
local function toggle_menu(menu_type)
    if open_menu_state and open_menu_state.type == menu_type.type then
        menu_close()
        return
    end

    if current_url == nil then
        if uosc_available then
            if menu_type.is_video then
                mp.commandv('script-binding', 'uosc/video')
            else
                mp.commandv('script-binding', 'uosc/audio')
            end
        end
        return
    end

    menu_open(menu_type)
end

function video_formats_toggle() toggle_menu(states.video_menu) end
function audio_formats_toggle() toggle_menu(states.audio_menu) end

-- keybind to launch menu
mp.add_key_binding(nil, 'video_formats_toggle', video_formats_toggle)
mp.add_key_binding(nil, 'audio_formats_toggle', audio_formats_toggle)
mp.add_key_binding(nil, 'reload', reload_resume)

mp.register_event('start-file', function()
    local new_url = get_url()
    local url_changed = current_url ~= new_url
    current_url = new_url
    if url_changed and fetching_timer then fetching_timer:kill() end
    uosc_set_format_counts()

    -- new path isn't an url
    if not new_url then return menu_close() end

    -- open or update menu
    if opts.start_with_menu and url_changed or open_menu_state then
        menu_open(open_menu_state or states.video_menu)
    end
end)

-- run before ytdl_hook, which uses a priority of 10
mp.add_hook('on_load', 9, function()
    local path = mp.get_property('path')
    if playlist_media(path) then refresh_hls_data(path); return end
    local data = url_data[path]
    local browser_variant = stream_variants and find_hls_variant(path, stream_variants)
    if browser_variant then
        -- file-local options prevent a previous DASH audio track leaking into
        -- a muxed FLV/HLS quality. HTTP headers remain those supplied at launch.
        mp.set_property_native('file-local-options/audio-files',
            browser_variant.audio_url and {browser_variant.audio_url} or {})
        mp.set_property_bool('file-local-options/ytdl', false)
        return
    end

    if data and data.hls then
        if data.hls_mode == 'url' then
            return
        elseif data.hls_mode == 'edition' then
            if data.video_active_id and data.video_active_id ~= '' then
                msg.verbose('setting edition: ' .. data.video_active_id)
                mp.set_property('file-local-options/edition', data.video_active_id)
            elseif data.video_active_id == '' then
                msg.verbose('setting edition: auto')
                mp.set_property('file-local-options/edition', 'auto')
            end
        else
            if data.video_active_id and data.video_active_id ~= '' then
                msg.verbose('setting hls-bitrate: ' .. data.video_active_id)
                mp.set_property('file-local-options/hls-bitrate', data.video_active_id)
            elseif data.video_active_id == '' then
                msg.verbose('setting hls-bitrate: no')
                mp.set_property('file-local-options/hls-bitrate', 'no')
            end
        end
        return
    end

    if not (data and data.video_active_id and data.audio_active_id) then return end
    local format = format_string(data.video_active_id, data.audio_active_id)
    msg.verbose('setting ytdl-format: ' .. format)
    mp.set_property('file-local-options/ytdl-format', format)
end)

mp.register_event('file-loaded', function()
    local data = refresh_hls_data(current_url)
    if not data and current_url and should_skip_fetch_formats(current_url) then
        data = current_stream_data(current_url)
        url_data[current_url] = data
    end
    if not data then return end

    uosc_set_format_counts()
    if open_menu_state then menu_open(open_menu_state) end
end)

---@param url string
---@param format_id string
mp.register_script_message('video-format-set', function(url, format_id)
    menu_close()
    local data = url_data[url]
    if not data then return end
    set_format(url, format_id, sanitize_format_id(data.audio_active_id, data.audio_formats))
end)

---@param url string
---@param format_id string
mp.register_script_message('audio-format-set', function(url, format_id)
    menu_close()
    local data = url_data[url]
    if not data then return end
    set_format(url, sanitize_format_id(data.video_active_id, data.video_formats), format_id)
end)

--- check if uosc is running
---@param version string
mp.register_script_message('uosc-version', function(version)
    ---Like the comperator for table.sort, this returns v1 < v2
    ---Assumes two valid semver strings
    ---@param v1 string
    ---@param v2 string
    ---@return boolean
    local function semver_comp(v1, v2)
        local v1_iterator = v1:gmatch('%d+')
        local v2_iterator = v2:gmatch('%d+')
        for v2_num_str in v2_iterator do
            local v1_num_str = v1_iterator()
            if not v1_num_str then return true end
            local v1_num = tonumber(v1_num_str)
            local v2_num = tonumber(v2_num_str)
            if v1_num < v2_num then return true end
            if v1_num > v2_num then return false end
        end
        return false
    end

    local min_version = '4.6.0'
    uosc_available = not semver_comp(version, min_version)
    if not uosc_available then return end
    uosc_set_format_counts()
    mp.commandv(
        'script-message-to',
        'uosc',
        'overwrite-binding',
        'stream-quality',
        'script-binding ' .. script_name .. '/video_formats_toggle'
    )
    ---@param name string
    mp.register_script_message('uosc-menu-closed', function(name)
        -- got closed from the uosc side
        if open_menu_state and open_menu_state.name == name then
            destructor = nil
            menu_close()
        end
    end)
end)
mp.commandv('script-message-to', 'uosc', 'get-version', mp.get_script_name())

mp.observe_property('user-data/mpv/ytdl/json-subprocess-result', 'native', function(_, ytdl_result)
    if not ytdl_result then
        -- property gets deleted in on_after_end_file hook
        return
    end

    if not current_url then
        osd_message('current_url is nil', 2)
        msg.error('current_url is nil')
        return
    end

    local data = url_data[current_url]
    if data and data.hls then return end
    local json = ytdl_result.stdout
    if ytdl_result.status ~= 0 or type(json) ~= 'string' or json == '' then
        data = unavailable_data('无法获取清晰度；请刷新网页后重新移交播放')
    else
        -- A late valid result may replace a previous timeout/error state.
        data = process_json_string(json)
            or unavailable_data('此播放源未提供可用的清晰度列表')
    end
    url_data[current_url] = data
    if fetching_timer then fetching_timer:kill() end
    uosc_set_format_counts()
    if open_menu_state then
        menu_open(open_menu_state)
    end
end)
