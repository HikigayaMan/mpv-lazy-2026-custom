# 网页视频列表移交 MPV

源码版 External Player **1.2.14.21** 与配套播放器 Lua／中继支持 B站完整合集及分 P 的层级播放列表。先前发布的 `.19` 累计 Release 不包含本功能；需要同步更新本说明列出的文件。

## 使用

在普通 BV／av 视频页点击 MPV 或 MPVNET：投稿属于合集时，将完整合集及其中各视频的全部 P 交给播放器；没有合集时交接当前视频的全部 P。相关推荐不进入列表，播放器从网页当前选中的具体 P 开始。

播放列表顶部显示合集名称与当前视频序号，下面按“分节（有多个分节时）→视频→分 P”排列。多 P 视频可在同一面板展开／收起，子视频缩进显示名称及时长；单 P 视频直接点击播放，不增加只有一项的子层。当前播放项的祖先在首次打开或切换播放项时自动展开，其余折叠选择保留。

使用播放器原有的播放列表入口选择条目，或点击上一条／下一条。展开／收起只改变显示，不删除实际视频。顺序播放依次走完整合集与各 P；乱序以可播放的具体 P 为单位；列表循环遍历整个合集，单曲循环重复当前 P。续播点只应用于首次加载，单曲循环重播完整视频。点选按实时原生条目 ID 查找，原生乱序后不会因界面行号变化而打开错误的视频。

每次实际播放条目时，通过原浏览器账号重新解析源，保持账号可用品质、独立音轨和字幕。切换画质时只重载当前条目，不清空整个列表。弹幕 CID、样式和开关按当前视频生效；旧条目的迟到弹幕请求不会覆盖新条目。

## 进度与浏览器

每个 `aid + cid` 分别保存位置。切换回未看完条目可续播，已完成条目从零开始；加载中的临时零秒不覆盖续播记录。已启用 B站账号历史时，也按实际播放的条目写入账号记录；只有正在播放的 CID 与原网页相同时才更新该网页的进度条。

请保留原网页，后续条目需要它进行账号鉴权解析。关闭网页后当前已经打开的媒体可以继续播放，但后续新条目的解析可能失败；重新打开相同视频页可重新接管列表。失败与超时会释放加载，不让播放器无限等待。关闭进度同步不会关闭列表解析。

## 更新文件

- `external_player.js`／对应 `.21.user.js`：导入用户脚本管理器，禁用重复旧版并完整刷新页面。
- `portable_config/scripts/web_playlist.lua`、`web_progress.lua`、`quality-menu.lua`。
- `portable_config/scripts/uosc_danmaku/main.lua`、`sites/bilibili.lua`。
- `portable_config/scripts/uosc/main.lua`、`elements/Menu.lua`、`lib/web_playlist_tree.lua`、`lib/web_playlist_menu.lua`。
- `tools/telegram-web-mpv-bridge/` 内的 EXE、`bridge.py`、`bilibili_danmaku.py`、`playback_progress.py`、`web_playlist.py` 和启动／构建脚本。

继续使用原有 MPV 与 BrowserRelay 注册。列表服务与 B站弹幕／进度共享 `127.0.0.1:9002/9001`；可手动运行 `start_browser_relay.ps1 -WebPlaylist`，`/health` 应包含 `webPlaylistApiVersion: 1`、`webPlaylistHierarchyApiVersion: 1` 与 `webPlaylistChunkApiVersion: 1`。WebSocket 不可用时走匿名扩展 HTTP 轮询。

列表不按视频或分 P 的数量截断，通过分块注册、分页恢复及分块 IPC 传递完整元数据。缺失分 P 详情会以最多三个并发请求补齐，已知单 P 不重复请求；明确发现合集或分 P 不完整时会提示。清单只保存公开内容信息和稳定本机条目地址；签名媒体地址只在解析和播放器内存中使用，Cookie、CSRF 和账号密钥留在浏览器。单视频及其他播放器保留原有交接方式；番剧影视、互动分支和 DRM 媒体没有接入。

传输保留资源保护：每次上传约 48 KiB、IPC 每条不超过 32 KiB，公开元数据总预算 32 MiB、最多八个会话。超过预算明确报错，不静默截断成部分合集；这些预算不是视频数量上限。

## 验证

`test_web_playlist.mjs`、`test_web_playlist.py` 与播放列表 Lua 测试覆盖完整层级、分块重试、归属、迟到请求、各项媒体参数和进度隔离。`smoke_test_web_playlist_hierarchy.py` 使用真实 MPV、1001 条目、3 P／4 P 视频，验证单 P 没有子层、当前祖先自动展开、折叠不改变原生列表，以及乱序后点选仍对应正确条目。既有真实 MPV 冒烟继续验证循环和品质切换，测试均使用长模拟签名地址。真实页面范围见对应验证报告。
