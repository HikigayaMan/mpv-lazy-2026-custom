local mp = require 'mp'
local msg = require 'mp.msg'
local utils = require 'mp.utils'
local options = require 'mp.options'

local o = {
    show_notifications = false,
}

options.read_options(o)

local root_dir = mp.command_native({ 'normalize-path', mp.command_native({ 'expand-path', '~~/..' }) })
local bridge_dir = root_dir .. '/tools/telegram-web-mpv-bridge'
local bridge_exe_path = bridge_dir .. '/browser-media-bridge.exe'
local python_path = bridge_dir .. '/.venv/Scripts/python.exe'
local bridge_path = bridge_dir .. '/bridge.py'
local mpv_path = root_dir .. '/mpv.exe'
local http_port = 8999
local ws_port = 9000
local notification_options_path = mp.command_native({
    'expand-path',
    '~~/script-opts/telegram_web_mpv_bridge.conf',
})

local function persist_notification_option()
    local lines = {}
    local input = io.open(notification_options_path, 'r')
    if input then
        for line in input:lines() do
            if not line:match('^%s*show_notifications%s*=') then
                lines[#lines + 1] = line
            end
        end
        input:close()
    end
    lines[#lines + 1] = 'show_notifications=' .. (o.show_notifications and 'yes' or 'no')

    local output, err = io.open(notification_options_path, 'w')
    if not output then
        msg.error('failed to persist browser media relay notification option: ' .. tostring(err))
        return false
    end
    output:write(table.concat(lines, '\n'), '\n')
    output:close()
    return true
end

local function ps_quote(value)
    value = tostring(value or ''):gsub("'", "''")
    return "'" .. value .. "'"
end

local function exists(path)
    return utils.file_info(path) ~= nil
end

local function run_ps(script, callback)
    mp.command_native_async({
        name = 'subprocess',
        playback_only = false,
        capture_stdout = true,
        capture_stderr = true,
        args = { 'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script },
    }, function(success, result)
        if callback then callback(success, result or {}) end
    end)
end

local function port_check_script(port)
    return table.concat({
        '$ok=$false',
        '$c=New-Object System.Net.Sockets.TcpClient',
        'try { $c.Connect(' .. ps_quote('127.0.0.1') .. ', ' .. tostring(port) .. '); $ok=$true } catch { $ok=$false } finally { $c.Close() }',
        'if ($ok) { Write-Output 1 } else { Write-Output 0 }',
    }, '; ')
end

local function is_running(callback)
    run_ps(port_check_script(http_port), function(success, result)
        local stdout = result.stdout or ''
        callback(success and stdout:match('1') ~= nil)
    end)
end

local function show_dependency_hint()
    mp.osd_message('浏览器媒体中继：未找到 browser-media-bridge.exe，源码回退也不可用（需要 .venv\\Scripts\\python.exe 和 bridge.py）', 7)
    msg.error('browser media relay executable and source fallback are unavailable: ' .. bridge_exe_path .. ' | ' .. python_path .. ' | ' .. bridge_path)
end

local function bridge_launcher()
    if exists(bridge_exe_path) then
        return bridge_exe_path, 'exe'
    end
    if exists(python_path) and exists(bridge_path) then
        return python_path, 'source'
    end
    return nil, nil
end

local function start_bridge()
    local launcher, mode = bridge_launcher()
    if not launcher then
        show_dependency_hint()
        return
    end

    local out_log = bridge_dir .. '/bridge.out.log'
    local err_log = bridge_dir .. '/bridge.err.log'
    local ps = table.concat({
        '$launcher=' .. ps_quote(launcher),
        '$bridge=' .. ps_quote(bridge_path),
        '$work=' .. ps_quote(bridge_dir),
        '$out=' .. ps_quote(out_log),
        '$err=' .. ps_quote(err_log),
        '$mpv=' .. ps_quote(mpv_path),
        '$args=@(' .. (mode == 'source' and '$bridge, ' or '') .. ps_quote('--http-port') .. ', ' .. ps_quote(tostring(http_port)) .. ', ' .. ps_quote('--ws-port') .. ', ' .. ps_quote(tostring(ws_port)) .. ', ' .. ps_quote('--mpv-path') .. ', $mpv)',
        'Start-Process -WindowStyle Hidden -FilePath $launcher -ArgumentList $args -WorkingDirectory $work -RedirectStandardOutput $out -RedirectStandardError $err',
        'Write-Output started',
    }, '; ')

    run_ps(ps, function(success, result)
        if success then
            mp.osd_message('浏览器媒体中继：已启动' .. (mode == 'source' and '（源码回退）' or ''), 3)
            if mode == 'source' then
                msg.warn('browser-media-bridge.exe not found; using Python source fallback')
            end
            mp.set_property_bool('user-data/telegram-web-mpv-bridge/running', true)
        else
            mp.osd_message('浏览器媒体中继：启动失败', 5)
            msg.error(result.stderr or result.stdout or 'unknown start failure')
        end
    end)
end

local function stop_bridge()
    local ps = table.concat({
        '$exe=' .. ps_quote(bridge_exe_path),
        '$bridge=' .. ps_quote(bridge_path),
        '$procs = Get-CimInstance Win32_Process | Where-Object { (($_.Name -ieq ' .. ps_quote('browser-media-bridge.exe') .. ') -and ($_.ExecutablePath -ieq $exe)) -or ((($_.Name -ieq ' .. ps_quote('python.exe') .. ') -or ($_.Name -ieq ' .. ps_quote('pythonw.exe') .. ')) -and $_.CommandLine -and ($_.CommandLine.IndexOf($bridge, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)) }',
        '$count = @($procs).Count',
        '$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }',
        'Write-Output $count',
    }, '; ')

    run_ps(ps, function(success, result)
        if success then
            local count = tonumber((result.stdout or ''):match('%d+')) or 0
            mp.osd_message(count > 0 and '浏览器媒体中继：已停止' or '浏览器媒体中继：未在运行', 3)
            mp.set_property_bool('user-data/telegram-web-mpv-bridge/running', false)
        else
            mp.osd_message('浏览器媒体中继：停止失败', 5)
            msg.error(result.stderr or result.stdout or 'unknown stop failure')
        end
    end)
end

local function status_bridge()
    is_running(function(running)
        mp.set_property_bool('user-data/telegram-web-mpv-bridge/running', running)
        mp.osd_message(running and '浏览器媒体中继：运行中' or '浏览器媒体中继：未运行', 3)
    end)
end

local function toggle_bridge()
    is_running(function(running)
        if running then stop_bridge() else start_bridge() end
    end)
end

mp.register_script_message('start', start_bridge)
mp.register_script_message('stop', stop_bridge)
mp.register_script_message('status', status_bridge)
mp.register_script_message('toggle', toggle_bridge)
mp.register_script_message('toggle-notifications', function()
    o.show_notifications = not o.show_notifications
    local persisted = persist_notification_option()
    local status = o.show_notifications and '开启' or '关闭'
    mp.osd_message('浏览器媒体中继启动通知：' .. status .. (persisted and '' or '（仅本次运行）'), 3)
end)

mp.add_key_binding(nil, 'toggle', toggle_bridge)
mp.add_key_binding(nil, 'status', status_bridge)

if o.show_notifications then
    mp.add_timeout(1, status_bridge)
end
