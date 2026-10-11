# Bilibili 直播弹幕交接到 MPV

本仓库新增的链路适用于 PC 浏览器中的 `live.bilibili.com/<房间号>`：点击 External Player 的 MPV 按钮后，视频仍使用解析出的直播直链，实时弹幕经本机 Browser Media Bridge 送入 `uosc_danmaku`。短房间号会先解析为真实房间号。

直播页即使把播放器放在 iframe 中，或者使用自定义解析规则、资源嗅探来取得视频地址，也会在 MPV 启动前单独交接当前直播房间的弹幕。弹幕交接不依赖视频解析器的类型。

## 安装本次修改

已有 Release 和在线 External Player 不包含本次功能，需要同时更新浏览器脚本、Bilibili 专用中继和播放器配置。专用中继使用独立端口，已有 Telegram／斗鱼中继可继续运行。

1. 用 Tampermonkey 或 Violentmonkey 安装本仓库根目录的 `external_player.js`（打开脚本的本地内容，粘贴到新建用户脚本并保存），禁用旧版重复脚本，然后完整刷新直播页面。
2. 将 `portable_config/scripts/uosc_danmaku/` 的修改覆盖到完整 mpv 整合包的同名目录。
3. 将本仓库 `tools/telegram-web-mpv-bridge/` 放到整合包根目录下的同名目录。
4. 在该工具目录安装源码依赖，或使用从本次源码构建的 `bilibili-live-bridge.exe`。该程序与已有 `browser-media-bridge.exe` 并存。

本地更新压缩包中的 `external_player.user.js` 与 `external_player.js` 内容相同，可以直接导入脚本管理器。安装本地定制版后关闭该脚本的自动更新，避免被原上游版本覆盖。

更新已经运行的安装时，还需要重新导入新版用户脚本、完整刷新直播页，并重启 Bilibili 中继；只覆盖磁盘上的 EXE 不会更新已启动的服务。2026-10-11 修复版脚本版本为 `1.2.14.11`。

源码方式（Python 3.10+）：

```powershell
cd tools\telegram-web-mpv-bridge
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start_browser_relay.ps1 -Bilibili -Source
```

构建免 Python 的 Windows 程序：

```powershell
.\build_windows.ps1 -InstallDependencies
```

生成的 `bilibili-live-bridge.exe` 位于同一工具目录；Bilibili 客户端已内置，不需要额外 Node helper。复制时不要复制其他电脑的 `.venv`。`config` 打包工具已包含本次源码、启动脚本和专用 EXE。

自动启动沿用现有 URL Scheme 设置。在 `url-scheme-handler.exe` 中注册 `MPV` 指向整合包的 `mpv.exe`，并注册 `BrowserRelay` 指向 `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`。浏览器脚本调用：

将 `url-scheme-handler.exe` 放在整合包根目录：它以自身所在目录启动程序，因此下面的相对脚本路径可以找到工具目录。

```powershell
-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ".\tools\telegram-web-mpv-bridge\start_browser_relay.ps1" -Bilibili
```

未注册自动启动时，可先运行 `start_browser_relay.ps1 -Bilibili`（优先使用专用 EXE，缺失时使用本机 `.venv`），再点击网页的 MPV 按钮。手动运行 EXE 时需要指定专用端口：

```powershell
.\bilibili-live-bridge.exe --host 127.0.0.1 --http-port 9002 --ws-port 9001
```

## 使用

1. 在浏览器打开并正常播放 Bilibili 直播。
2. 点击 External Player 的 MPV 按钮。
3. 脚本取得当前房间弹幕认证信息，为这次播放器创建独立的 Windows IPC pipe。
4. MPV 中的 `uosc_danmaku` 自动显示收到的文字弹幕，可通过 `j` 或 `工具 > 弹幕 > 开关弹幕` 隐藏和恢复。

在 `工具 > 弹幕 > 弹幕设置` 中调整字体、字号、透明度、速度、显示区域和直播帧率。保存跨进程默认样式后，新启动的播放器也会应用设置。礼物、系统广播和进房提示不作为文字弹幕显示。

表情弹幕以文本显示：保留消息中的文字和 `[dog]` 等表情标签，纯图片表情使用消息携带的名称；缺少名称时显示 `[表情]`。不会下载或绘制表情图片，也不会把图片地址当作弹幕。

建议保留直播标签页，断线重连时可从当前浏览器会话刷新短期认证。关闭标签页后已建立的弹幕连接会继续运行，但认证过期后可能无法恢复。暂停时不会积压弹幕；关闭播放器或切换到其他文件后会释放该会话。

## 验证和排错

弹幕失败会降级为正常视频播放。若只有视频：

- 检查是否同时更新了用户脚本和中继 EXE，页面是否完整刷新。
- 检查 `http://127.0.0.1:9002/status` 和工具目录的 `bilibili-bridge.err.log`。
- `/health` 中的 `bridgeVersion` 应为 `2026.10.11`。`/status` 的 `diagnostics.startRequests` 为 0 表示浏览器尚未发起弹幕会话；`diagnostics.recentFailures` 保留会话创建失败的记录，`danmaku` 列表中的 `phase` 和 `error` 可定位 MPV 附加或运行阶段的失败。`diagnostics.browserConnected` 表示浏览器中继连接，旧字段 `connected` 仅用于 Telegram 视频链路。
- 检查直播网页本身能否正常接收弹幕，浏览器是否有有效的 `buvid3`；认证被站点拒绝时，可刷新页面或在网页正常登录后重新交接。
- 新版浮层会显示弹幕失败的具体阶段和原因。即使弹幕连接失败，视频仍可启动；“已交给播放器”只表示视频已启动，不代表弹幕连接成功。
- 确认 `uosc_danmaku` 已加载，且弹幕没有被 `j` 隐藏。
- Bilibili 中继保持本机 HTTP `127.0.0.1:9002`／WebSocket `127.0.0.1:9001`，原中继使用 `8999/9000`；每次启动的 pipe 不应手工改为共用名称。

浏览器仅向本机中继传递短期弹幕 token、用户 ID、buvid、用于匹配认证的 User-Agent 和官方弹幕服务器地址，不导出完整 Cookie、登录凭证或持久化认证信息。弹幕不写入视频弹幕历史。

开发验证：

```powershell
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools\telegram-web-mpv-bridge -p "test_*.py"
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools\release -p "test_*.py"
node --test tools/telegram-web-mpv-bridge/test_bilibili_live_handoff.mjs
```

Lua 行为测试额外需要 `lupa`。真实播放器的离线接入验证可运行：

```powershell
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe tools\release\smoke_test_bilibili_live.py --mpv "<完整包路径>\mpv.exe"
```

该验证使用生成的视频和模拟服务器消息，覆盖真实 Windows IPC 与完整 Lua 脚本，不连接实际直播房间。

协议参考：[Bilibili API collect 的直播消息流](https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/live/message_stream.md)、[WBI 签名](https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/misc/sign/wbi.md) 和 [blivedm WebSocket 实现](https://github.com/xfgryujk/blivedm/blob/master/blivedm/clients/ws_base.py)。
