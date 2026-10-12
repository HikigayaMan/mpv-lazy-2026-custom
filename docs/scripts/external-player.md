# External Player 使用说明

## 这个脚本是做什么的

External Player 是浏览器用户脚本。它从网页取得视频地址、标题、清晰度、字幕和必要的请求信息，再通过 `ush://` 协议把内容交给本地播放器。

当前 2026 自定义版的主要链路：

```text
网页
  -> External Player 1.2.14.19
  -> ush://MPV
  -> url-scheme-handler.exe
  -> mpv.exe
```

遇到只能由浏览器读取的媒体时，会启用本机中继：

```text
网页
  -> External Player
  -> 127.0.0.1:9000 WebSocket
  -> Browser Media Bridge
  -> 127.0.0.1:8999 HTTP Range/HLS
  -> mpv
```

相关仓库：

- 本仓库定制版：[HikigayaMan/mpv-lazy-2026-custom](https://github.com/HikigayaMan/mpv-lazy-2026-custom)
- 原作者定制来源：[luoxue03/external-player](https://github.com/luoxue03/external-player)
- 上游项目：[LuckyPuppy514/external-player](https://github.com/LuckyPuppy514/external-player)
- URL Scheme 处理器：[luoxue03/url-scheme-handler](https://github.com/luoxue03/url-scheme-handler)
- 浏览器媒体中继：[luoxue03/telegram-web-mpv-bridge](https://github.com/luoxue03/telegram-web-mpv-bridge)

本地维护版继续保留上游作者、MIT License、原有播放器配置和站点解析能力；新增功能只用于 mpv-lazy 2026 的本地播放链路。

## 本版新增内容

- 新的悬浮交接面板，使用 Shadow DOM 隔离网页 CSS。
- 主按钮直接启动默认播放器，其他播放器收进下拉菜单。
- 面板内可快速设置默认播放器、首选画质、浏览器中继和贴边隐藏。
- 完整设置页不再自动弹出，可从面板的设置按钮打开。
- 贴边隐藏状态会持久化，初次状态提示结束后约 1.2 秒收起。
- 新增通用资源嗅探兜底，但专用解析器始终优先。
- MissAV 支持 HLS 主清单、变体清晰度和浏览器授权请求信息。
- Telegram Web K/Z 支持自动启动本机中继并交给 mpv。
- 斗鱼直播使用新鲜签名的完整直播流，支持网页当前可用画质。
- 斗鱼弹幕可通过 Browser Media Bridge 接入 mpv 的 `uosc_danmaku`。
- 浏览器中继弹出通知默认关闭，失败时仍保留必要的面板状态。
- 网页交给 MPV/MPVNET 后自动以全屏播放，普通视频与直播均适用。
- 可选“进入视频或直播页面后自动移交默认播放器”，默认关闭。
- B站 DASH 视频与音频优先使用 API 同时返回的 HTTPS 标准端口 bilivideo.com 备用源，避免部分 PCDN 主源端口无法连接；保持原画质、编码和完整签名。
- 网页与 MPV 双向续播并保存本机位置，支持 B站普通视频及分 P；登录时可同时写入 B站账号观看历史。
- WebSocket 无法连接时，进度通道自动使用匿名扩展 HTTP 请求；.19 修复 MPV 参数传递，包内 Lua 兼容已导入 .18。

## 安装与更新

### 浏览器脚本

1. 安装 Tampermonkey 或 Violentmonkey。
2. 从 [本仓库累计更新 Release](https://github.com/HikigayaMan/mpv-lazy-2026-custom/releases/tag/v2026.10.12-progress-update) 解压并导入 `external_player.user.js`，禁用重复旧脚本。
3. 关闭这份定制脚本的上游自动更新，更新后完整刷新已经打开的视频网页。

本次累计包内的 `external_player.js` 与 `external_player.user.js` 内容相同，版本为 `1.2.14.19`。外仓库脚本不保证已同步本仓库修改；浏览器中只需要安装一份定制脚本。

### 注册 MPV

1. 运行 `url-scheme-handler.exe`。
2. 点击 `Add to Registry` 注册 `ush://`。
3. 新增应用 `MPV`，路径选择整合包根目录下的 `mpv.exe`。
4. 如果配置了其他播放器，名称必须与脚本设置中的名称完全一致。

移动整合包目录后，需要重新选择播放器路径并注册。

### 注册自动中继

Telegram 和斗鱼弹幕需要 Browser Media Bridge；B站直播弹幕和双向进度使用同一注册入口启动独立的 B站中继。若希望点击网页按钮时自动启动中继，在 `url-scheme-handler` 中再增加：

| 名称 | 程序 |
|---|---|
| `BrowserRelay` | `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` |

External Player 会把以下启动参数交给它：

```powershell
-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ".\tools\telegram-web-mpv-bridge\start_browser_relay.ps1"
```

中继安装方法见 [luoxue03/telegram-web-mpv-bridge](https://github.com/luoxue03/telegram-web-mpv-bridge#readme)。

B站直播使用 `-Bilibili`，双向进度使用 `-PlaybackProgress`，由脚本按需要附加。B站中继为 HTTP `9002`／WebSocket `9001`，原 Telegram／斗鱼中继为 `8999/9000`。本次累计包已包含 B站 EXE；详见 [进度同步](./playback-progress.md)。

## 悬浮面板与设置

### 面板操作

- 点击主按钮：使用默认播放器播放。
- 点击主按钮右侧箭头：选择其他已启用播放器。
- 点击齿轮：展开快速设置。
- 点击“完整设置”：打开原有播放器配置页。
- 拖动面板：改变停靠位置。
- 开启贴边隐藏后：鼠标移开会收起，只保留窄边；悬停后展开。

面板不会因为一次解析失败自动打开完整配置页。

### 主要设置

| 设置 | 作用 | 默认值 |
|---|---|---|
| 默认播放器 | 主按钮点击后使用的播放器 | `MPV` |
| 自动移交默认播放器 | 进入可解析的视频或直播页面后自动启动默认播放器 | 关闭 |
| 网页与播放器双向进度 | 网页与 MPV／MPVNET 相互续播并保存本机位置 | 开启 |
| B站账号观看历史 | 使用原网页登录状态把 MPV 位置写入 B站账号历史 | 开启 |
| 首选画质 | yt-dlp、B 站或斗鱼的首选档位 | 最高可用 |
| 浏览器中继 | `自动`、`始终中继`、`关闭` | 自动 |
| 中继弹出通知 | 是否显示中继启动/回退通知 | 关闭 |
| 贴边隐藏 | 面板空闲时是否收至屏幕边缘 | 关闭 |
| 资源嗅探兜底 | 未命中专用解析器时观察网页媒体资源 | 开启 |

`自动`只在 Telegram、`blob:` 或明确需要浏览器授权的来源上启用中继；普通直链不会多走一层代理。`始终中继`适合临时诊断浏览器可播、mpv 直连失败的 HTTP/HLS 来源。

进度同步独立于浏览器媒体代理和旧播放器预设的“同步时间”。关闭网页后中继仍保存本机位置；账号历史需登录网页保持打开，重新进入同一视频可补同步。普通 BV／av 与分 P 支持，直播、番剧影视和互动分支不套用点播续播。详见 [双向进度说明](./playback-progress.md)。

在浮层快速设置或完整设置中勾选“进入视频或直播页面后自动移交默认播放器”，之后打开视频、直播或在网页内切换到新的内容时，会使用上方选定且已启用的默认播放器。开启开关或修改默认播放器时，当前内容不会立即再次启动；打开下一条内容或刷新页面后生效。

每次进入同一内容只自动尝试一次。B站分 P、不同直播间及 YouTube 新视频会重新移交，分享和时间参数变化不会重复启动。iframe 初始化和资源嗅探通知也不会导致重复启动。自动解析失败后可以点击播放按钮手动重试，或关闭自动移交继续使用原来的网页交接方式。Telegram 需要当前有可见的视频和可读取的媒体源；浏览器的外部协议确认提示仍由浏览器控制。

旧版每个播放器的“自动播放”预设仍可使用；开启新的全局自动移交时，只启动默认播放器，避免两种设置同时启动多个播放器。

## 当前站点行为

| 站点/类型 | 处理方式 | 重要说明 |
|---|---|---|
| Bilibili | 专用 DASH/直播解析 | 可传视频、音频、字幕、标题和 `cid`。 |
| MissAV | 专用 HLS 解析 | 提取主清单与可用变体，传入 Origin、Referer、UA；不把网页 Cookie 发给 CDN。 |
| Telegram Web K/Z | 浏览器媒体中继 | 点击时自动尝试启动中继；播放期间 Telegram 标签页必须保持打开。 |
| 斗鱼直播 | 专用签名直播解析 | 每次点击重新取完整直播流和实时画质，不缓存过期签名。 |
| Pornhub | yt-dlp 页面解析 | 可配合本机 `cookies.txt` 与 impersonate。 |
| SpankBang | yt-dlp 页面解析 | 概率性 403 由整合包 `ytdl-retry.lua` 做有限重试。 |
| YouTube 等 yt-dlp 站点 | 交给 mpv/yt-dlp | 使用全局首选画质和代理设置。 |
| 未适配网页 | 资源嗅探兜底 | 只在发现可信媒体后显示；DRM、私有 MSE 和未知 `blob:` 不保证可用。 |

### Bilibili 直播弹幕

本仓库用户脚本为 `live.bilibili.com` 的 MPV 播放增加实时弹幕交接。视频仍使用直链；中继只负责弹幕，失败时仍启动视频。每次播放器使用独立 IPC pipe，避免混入其他房间的弹幕。

需要安装本仓库修改后的用户脚本，并同步更新中继 EXE 和 uosc 配置；本次累计 Release 已包含这些文件，原项目旧 Release 可能尚未包含此功能。详见 [安装与交接说明](./bilibili-live-danmaku.md)。

### MissAV

脚本会优先取得 HLS 播放地址，并尽量预读变体清单，把清晰度信息传给 `quality-menu.lua`。正常情况下保持浏览器中继为“自动”即可。

如果网页可以播放但 mpv 报 403：

1. 刷新页面，让脚本取得当前有效地址。
2. 确认浏览器和 mpv 使用相同网络出口。
3. 临时把“浏览器中继”改为“始终中继”进行对比。
4. 查看 mpv 控制台是否收到正确的 Origin、Referer 和 User-Agent。

### Telegram Web

External Player 已内置 Telegram 捕获逻辑，不需要另装旧版 Telegram userscript，也不需要 Telegram API ID/Hash。

点击 MPV 后：

1. 脚本捕获 Telegram Web 当前媒体。
2. 若本机中继未运行，通过 `BrowserRelay` 自动启动。
3. 中继生成临时本机地址。
4. `url-scheme-handler` 拉起 mpv。

浏览器仍负责读取 Telegram 登录态媒体，因此播放期间不能关闭对应标签页。

### 斗鱼直播与弹幕

斗鱼不会使用嗅探到的 7 秒低画质片段。专用解析器会：

1. 识别真实房间号。
2. 生成当前请求签名。
3. 获取完整直播流与当前可用画质。
4. 按首选画质启动 mpv。

视频地址直连，不经过浏览器中继。只有弹幕使用本机中继和 Node helper；弹幕启动失败不会阻止视频播放。

斗鱼协议实现参考了 [qianjiachun](https://github.com/qianjiachun) 的公开实现，当前仓库保留自己的边界检查、测试和 mpv 接入逻辑。

## 清晰度

- B 站、yt-dlp 和斗鱼分别使用各自解析器的实时画质列表。
- MissAV/HLS 会把可识别的变体交给 mpv 的 `quality-menu.lua`。
- 斗鱼签名 URL 会在点击时重新生成，切换画质应回到网页面板重新选择并播放。
- 如果一个来源本身只有一个档位，mpv 菜单只显示该档位是正常现象。

## 本地文件与隐私

以下内容不要提交到 Git：

- `cookies.txt`
- `config.json`
- 浏览器 Cookie、Token、签名直播地址
- 中继日志、`.venv/`、`node_modules/`
- mpv 播放历史、最近文件和运行时状态

斗鱼签名 URL 属于短期敏感地址；脚本只在当前启动中使用，不写入仓库或长期缓存。

## 常见问题

### 页面没有面板

- 确认脚本版本为 `1.2.14.19`，更新后完整刷新当前网页。
- 更新后完整刷新页面。
- 查看控制台是否出现页面脚本异常。
- 未适配网页只有在嗅探到可信媒体后才显示面板。

### 点击后没有 mpv 窗口

- 检查 `ush://` 是否注册。
- 检查 `MPV` 应用名和 `mpv.exe` 路径。
- 浏览器首次调用外部协议时允许打开。
- 移动整合包后重新注册路径。

### Telegram 黑屏

- 保持 Telegram 标签页打开。
- 确认 `127.0.0.1:8999/status` 可访问。
- 确认 `BrowserRelay` 指向 PowerShell。
- 更新 External Player 后完整刷新 Telegram 页面。

### 斗鱼有画面但没有弹幕

- 视频直连成功不代表弹幕 helper 已启动。
- 确认 Browser Media Bridge 正在运行。
- 正式 config 包包含免 Node 的 `douyu-danmaku-client.exe`；只有源码开发模式需要 Node.js 和 `npm install`。
- 在 mpv 中打开 `工具 > 弹幕 > 弹幕设置`，确认弹幕未关闭。

### MissAV 仍然 403

先重新刷新页面并点击，不要复用旧的 HLS URL。若“始终中继”可播而“自动”不可播，说明当前 CDN 需要浏览器授权链路；保留“始终中继”作为该次播放的兼容方案。

## 开发验证

独立仓库中的离线测试：

```powershell
node --check external-player.user.js
node --test tests\*.test.js
```

测试覆盖配置迁移、面板贴边持久化、斗鱼房间/签名/画质解析和斗鱼弹幕启动参数。

## License

本项目遵循上游 MIT License。原作者与上游链接保留在用户脚本头部、仓库 README 和 `LICENSE` 中。
