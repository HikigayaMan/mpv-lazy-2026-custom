# 网页与 MPV 双向播放进度

累计更新包内的 External Player **1.2.14.19** 与中继 **2026.10.12** 新增双向进度。`v2026.10.12-progress-update` 已将此前 .15 累计包与进度功能整合，直接覆盖新的累计包并导入 .19 即可。此前的 `v2026.10.12-update` 本身不包含进度功能。

## 使用行为

- 在网页观看普通视频后点击 MPV／MPVNET，从当前观看位置继续，而不是从零开始。
- 在播放器继续观看或拖动播放条，会保存本机位置并同步到同一网页；关闭播放器后再在网页播放，可从该位置继续。
- B站登录用户同时写入账号观看历史，其他设备可使用 B站自己的“继续播放”。没有登录或账号接口失败时，本机续播仍可使用。
- 按视频 `aid + cid` 独立记录 B站分 P；切到另一视频或另一分 P，不会套用旧内容的位置。
- 采用最近一次实际观看／主动跳转的位置，允许倒退和回到零秒，不取进度最大值。
- MPV 切换普通视频品质时保留相同内容身份和位置；加载中的临时零秒不覆盖已保存位置。
- 原网页在移交后暂停时不会覆盖 MPV。用户主动在网页恢复播放或拖动进度后，续播记录交回网页；此时不再自动把网页拉回 MPV 的旧位置。
- 看完时向 B站标记完成；手动退出播放器或换文件只保存实际位置，不误标成看完。

此功能默认开启，可在 External Player 快速设置或完整设置中关闭“网页与播放器双向进度”。它独立于旧版播放器预设的单向“同步时间”选项；旧预设默认关闭不会挡住新功能。

## 安装

1. 更新 `external_player.js`，将对应 .19 `.user.js` 导入 Tampermonkey／Violentmonkey，禁用重复旧版并完整刷新网页。
2. 更新 `portable_config/scripts/web_progress.lua` 和 `quality-menu.lua`。
3. 更新 `tools/telegram-web-mpv-bridge/` 的中继 EXE、源码和 `start_browser_relay.ps1`，重启本安装的中继与播放器。
4. 保留原来的 `MPV` 与 `BrowserRelay` 注册。进度服务与 B站弹幕共享 HTTP `9002`／WebSocket `9001`。脚本 .18 起在 WebSocket 不可连接时自动使用匿名扩展 HTTP 请求和轮询。只开放 `/health` 的无凭据 CORS，进度接口不向普通网页开放 CORS。随机 HTTP 客户端标识只保留在内存，刷新后可以重新订阅同一视频。

源码方式手动启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\telegram-web-mpv-bridge\start_browser_relay.ps1 -PlaybackProgress
```

脚本会检测 `/health` 的 `playbackProgressApiVersion: 1`。旧 EXE 仅支持弹幕，需要更新后重启；脚本不会擅自终止其他安装的服务。中继不可用时仍可手动播放，网页中的当前位置和已保存本机位置提供续播兜底。

已导入 .18 的用户：本次 `web_progress.lua` 同时兼容旧版合并参数，不需要为了这次修复再导入一次；更新包的 .19 已将两个参数分别发送。

## 保存位置和边界

本机中继保存到其所在目录的 `playback-progress.json`；用户脚本另外在脚本管理器中保存本机续播记录。这些文件与浏览器数据不会打包或提交。中继原子替换保存文件，暂停且位置不变时不持续更新时间戳，避免旧暂停窗口被当成最新观看。

B站账号历史由原网页浏览器使用现有登录会话更新，约 15 秒一次，暂停、结束及显著倒退时及时提交。Cookie、CSRF、账号认证和 WBI 签名密钥只在浏览器内使用，不发送给本机中继，也不写入播放器配置。

关闭原网页后，正在运行的中继继续保存 MPV 本机位置；浏览器关闭期间不能使用该登录会话提交账号历史。重新打开同一视频页后可读取本机记录并补同步。网页和中继同时关闭时，保留最后一次已经落盘的记录。

直播不套用点播续播，B站番剧／影视和互动分支目前尚未接入；B站普通 BV／av 投稿视频及分 P 支持。没有有限时长、无法读取视频元素、跨域播放器或站点隐藏媒体状态的页面不保证支持。普通网页视频支持本机同步，站点账号历史写入当前专门接入 Bilibili。

## 开发验证

```powershell
python -m unittest discover -s tools/telegram-web-mpv-bridge -p 'test*progress.py'
python -m unittest discover -s tools/release -p 'test_web_progress_lua.py'
node --test tools/telegram-web-mpv-bridge/test*progress.mjs
python tools/release/smoke_test_web_progress.py --mpv '<完整安装>/mpv.exe' --fixture '<已有本地MOV素材>' --output-dir tools/telegram-web-mpv-bridge/build/web-progress-smoke
```

真实 MPV 冒烟使用隔离偏好目录、本地 HTTP 视频与实际 WebSocket 或 HTTP 进度通道／Windows IPC，验证 12.5 秒启动、倒退与零秒、23.9 秒切画质保持、网页断开后保存 31.875 秒、重新启动播放器续播、不同分 P 与换文件隔离。使用 `--transport http` 可单独复测兜底通道。它不调用真实 B站账号历史接口，联机验证应另行记录。

网页历史接口行为参考 B站实际播放器与历史页的官方前端代码：[播放器核心](https://s1.hdslb.com/bfs/static/player/main/core.ba67b466.js)、[历史页](https://s1.hdslb.com/bfs/static/history-record/app_d0026f29.js)。这些接口可能随站点更新变化，失败时保留本机记录并给出提示。

## 本机真实联机验证

2026-10-12 的真实 B站普通视频测试已确认：网页当前位置移交后 MPV 全屏以 1920×1080 播放；MPV 暂停并跳转到 187.5 秒，原网页同步到相同位置；B站账号历史页显示测试视频 `03:07 / 06:20`。退出 MPV 后本机保存 187.5 秒且没有误标看完，刷新网页可继续播放。截图和具体验证边界见 [验证报告](../reports/2026-10-12-playback-progress.md)。
