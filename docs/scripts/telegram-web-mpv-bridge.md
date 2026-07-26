# Browser Media Bridge 使用说明

仓库名称仍为 `telegram-web-mpv-bridge`，但当前版本已经从 Telegram 专用工具扩展为 mpv-lazy 的本机浏览器媒体中继，并同时承载斗鱼直播弹幕 helper。

仓库：[luoxue03/telegram-web-mpv-bridge](https://github.com/luoxue03/telegram-web-mpv-bridge)

当前发布版本：`v0.2.0`。

## 这个工具是做什么的

它解决两类浏览器到 mpv 的交接问题：

1. Telegram Web 的视频依赖浏览器登录态，mpv 无法直接请求。
2. 斗鱼弹幕需要一个独立于网页标签页的长连接，并通过 MPV JSON IPC 送入 `uosc_danmaku`。

Telegram 媒体链路：

```text
Telegram Web
  -> External Player 捕获媒体
  -> WebSocket 9000
  -> bridge.py
  -> HTTP Range/HLS 8999
  -> mpv
```

斗鱼弹幕链路：

```text
External Player
  -> bridge.py 启动 Node helper
  -> 斗鱼弹幕 WebSocket
  -> 每次启动专用 MPV IPC pipe
  -> douyu_danmaku.lua
  -> uosc_danmaku live API
```

斗鱼视频本身是签名直链，不经过中继。中继或弹幕失败时，视频仍应正常启动。

## 文件

| 文件 | 作用 |
|---|---|
| `bridge.py` | 本机 HTTP + WebSocket 服务，负责媒体 Range/HLS 转发和斗鱼弹幕进程生命周期。 |
| `start_browser_relay.ps1` | 隐藏窗口启动 bridge，并等待 `8999` 端口就绪。 |
| `douyu_danmaku_client.js` | 斗鱼弹幕协议客户端，输出有界 JSON 消息。 |
| `douyu_danmaku.lua` | mpv 兼容路由器；优先转给 `uosc_danmaku`，不可用时使用轻量 ASS 兜底。 |
| `telegram-web-mpv-bridge.user.js` | 旧的 Telegram 独立 userscript；使用最新版 External Player 时通常不需要。 |
| `build_windows.ps1` | 可复现构建两个 Windows x64 单文件 EXE。 |
| `requirements.txt` / `requirements-build.txt` | 固定 Python 运行与 PyInstaller 构建依赖。 |
| `package.json` / `package-lock.json` | 固定 Node `ws` 与 `@yao-pkg/pkg` 构建依赖。 |
| `tests/` | Python、Node 和 Lua 路由测试。 |

整合包中的对应位置：

```text
tools\telegram-web-mpv-bridge\
portable_config\scripts\telegram_web_mpv_bridge.lua
portable_config\scripts\douyu_danmaku.lua
portable_config\scripts\uosc_danmaku\
```

## 普通用户安装

mpv-lazy 的正式 `config` 包已经包含：

```text
tools\telegram-web-mpv-bridge\browser-media-bridge.exe
tools\telegram-web-mpv-bridge\douyu-danmaku-client.exe
tools\telegram-web-mpv-bridge\start_browser_relay.ps1
```

覆盖 `config` 包后即可使用，不需要安装 Python、Node、`.venv` 或 `node_modules`。启动脚本和 MPV 菜单都会优先运行 `browser-media-bridge.exe`。

单独使用本仓库时，也可以从 [GitHub Releases](https://github.com/luoxue03/telegram-web-mpv-bridge/releases/latest) 下载两个 EXE，并与 `start_browser_relay.ps1` 放在同一目录：

```text
<mpv-lazy>\tools\telegram-web-mpv-bridge\
```

不要从其他电脑复制 `.venv/`、`node_modules/`、日志或缓存。

## 源码开发与构建

源码模式是开发回退，不是普通用户的安装要求。

### 安装开发依赖

```powershell
cd tools\telegram-web-mpv-bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm ci
```

源码启动时：

```powershell
.\.venv\Scripts\python.exe bridge.py `
  --host 127.0.0.1 `
  --http-port 8999 `
  --ws-port 9000 `
  --mpv-path ..\..\mpv.exe
```

### 构建 Windows EXE

构建机需要 Python 3.10+ 与 Node.js，最终生成物不需要：

```powershell
.\build_windows.ps1
```

输出：

```text
dist\browser-media-bridge.exe
dist\douyu-danmaku-client.exe
```

脚本使用锁定版本的 PyInstaller 和 `@yao-pkg/pkg`。首次构建会下载对应的 Node 基础运行时；如果构建机通过本机代理访问 GitHub，可以仅对本次构建传入代理，不修改系统配置：

```powershell
.\build_windows.ps1 -Proxy http://127.0.0.1:7897
```

### URL Scheme 自动启动

在 `url-scheme-handler.exe` 中新增：

| 名称 | 路径 |
|---|---|
| `BrowserRelay` | `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` |

最新版 External Player 点击 Telegram 或启动斗鱼弹幕时，会自动调用：

```powershell
-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ".\tools\telegram-web-mpv-bridge\start_browser_relay.ps1"
```

因此日常使用不需要先在 mpv 菜单手动开启 bridge。

## 使用

### Telegram Web

1. 安装最新版 [External Player](https://github.com/luoxue03/external-player#readme)。
2. 登录 [Telegram Web K](https://web.telegram.org/k/) 或 Web Z。
3. 更新脚本后完整刷新页面。
4. 打开目标视频并点击 External Player 的 MPV 按钮。
5. 脚本会自动启动中继、注册当前媒体并拉起 mpv。

不需要 Telegram API ID/Hash，也不需要下载完整文件。拖动进度条通过 HTTP Range 在线读取。

播放期间必须保持对应 Telegram 标签页打开，因为真正带登录态的媒体请求仍由浏览器执行。

### 斗鱼直播弹幕

1. 在斗鱼房间点击 External Player 的 MPV 按钮。
2. External Player 直接把新鲜签名直播流交给 mpv。
3. 同时请求 bridge 启动该房间的弹幕 helper。
4. helper 只绑定这次 mpv 启动生成的 IPC pipe。
5. mpv 关闭或 pipe 消失后，bridge 自动结束 helper。

`uosc_danmaku` 已就绪时，弹幕直接使用它的字体、字号、透明度、显示区域、速度和实时帧率设置；若启动时 API 稍晚注册，路由器会短暂缓冲并完成延迟升级。

### MPV 菜单

手动控制入口：

```text
工具 > 浏览器媒体中继 开关
工具 > 浏览器媒体中继 状态
工具 > 浏览器媒体中继 启动通知开关（默认关闭）
```

这些入口主要用于诊断。External Player 的自动启动与菜单开关使用同一套 `8999/9000` 服务。

### 手动启动

正式包：

```powershell
cd tools\telegram-web-mpv-bridge
.\browser-media-bridge.exe --host 127.0.0.1 --http-port 8999 --ws-port 9000 --mpv-path ..\..\mpv.exe
```

未构建 EXE 时，`start_browser_relay.ps1` 会回退到 `.venv\Scripts\python.exe + bridge.py`。两种模式都默认只监听本机回环地址。

## 状态与接口

默认端口：

- HTTP：`127.0.0.1:8999`
- WebSocket：`127.0.0.1:9000`

查看状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8999/status
```

主要本机接口：

| 路径 | 作用 |
|---|---|
| `/status` | 服务、浏览器连接与当前来源状态。 |
| `/stream/current` | 兼容旧版的当前媒体入口。 |
| `/s/<token>/stream` | 单次注册媒体的稳定 HTTP Range 入口。 |
| `/s/<token>/master.m3u8` | 中继重写后的 HLS 主清单入口。 |
| `/play/current` | 让 bridge 直接启动当前媒体。 |

这些接口只供本机使用，不应通过端口转发暴露到局域网或公网。

## 设置与运行状态

- `bridge.out.log`、`bridge.err.log`：自动生成日志。
- `.venv/`：本机 Python 环境。
- `node_modules/`：本机 Node 依赖。
- `__pycache__/`：Python 缓存。

以上均被 `.gitignore` 排除，不应上传 GitHub。

斗鱼弹幕样式默认保存在：

```text
portable_config\script-opts\uosc_danmaku_style.json
```

它是用户运行时偏好，不随仓库和配置压缩包发布。

## 常见问题

### 点击 Telegram 后没有 mpv

1. 检查 `ush://MPV` 与 `ush://BrowserRelay` 是否都已注册。
2. 检查 `browser-media-bridge.exe` 是否与 `start_browser_relay.ps1` 位于同一目录。
3. 访问 `http://127.0.0.1:8999/status`。
4. 查看 `bridge.err.log`。
5. 更新 External Player 后完整刷新 Telegram 页面。

### Telegram 打开 mpv 但黑屏

- 不要关闭 Telegram 标签页。
- 确认页面视频本身可以播放。
- 确认 External Player 控制台参数是本机 `/s/<token>/stream` 或 `/master.m3u8`。
- 旧页面捕获状态可能失效，完整刷新后重新点击。

### 斗鱼有视频但没有弹幕

- 这是允许的降级行为，视频链路与弹幕链路互不阻塞。
- 确认 `douyu-danmaku-client.exe` 与 bridge EXE 位于同一目录。
- 源码开发模式下，才需要确认 Node.js 和 `npm ci`。
- 检查 `bridge.err.log` 是否提示 `Douyu danmaku helper is unavailable`。
- 在 mpv 中确认 `工具 > 弹幕 > 开关弹幕` 没有关闭。
- 通过弹幕设置保存一次跨进程默认样式，随后重新拉起直播。

### 端口被占用

```powershell
Get-NetTCPConnection -LocalPort 8999,9000 -State Listen
```

结束冲突程序，或同时修改 bridge 启动参数、External Player 与 mpv Lua 控制脚本。三处端口必须一致。

### CPU 占用异常

- 检查是否重复启动多个 bridge。
- 检查是否有旧版 Telegram userscript 和 External Player 同时注册来源。
- 关闭不再使用的 mpv 进程后，斗鱼 helper 应随 IPC pipe 消失而停止。

## 开发验证

Python：

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Node：

```powershell
npm test
```

Lua 路由集成测试需要整合包中的 mpv：

```powershell
mpv.exe --no-config --idle=yes --script=tests\douyu_danmaku_router_test.lua
```

测试重点包括 Range/HLS 转发、来源隔离、斗鱼 helper 命令选择、进程停止、MPV pipe 消失、消息限长/限量和 `uosc_danmaku` 延迟注册升级。

## 安全边界

- 服务仅绑定 `127.0.0.1`。
- 不保存 Telegram API 凭据。
- 不把浏览器 Cookie 写入仓库。
- 不缓存斗鱼签名直播地址。
- WebSocket 消息、文件名、会话 ID 和 MPV pipe 名都经过边界检查。
- 日志、会话、缓存和本机依赖不得提交。
