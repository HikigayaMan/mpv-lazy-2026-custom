# mpv-lazy 2026 自定义增强版

这是面向 Windows 10/11 的 mpv 整合包定制版本，基于 [luoxue03/mpv-lazy-2026-custom](https://github.com/luoxue03/mpv-lazy-2026-custom)，原项目基于 [hooke007/mpv_PlayKit](https://github.com/hooke007/mpv_PlayKit)。本仓库新增并修复 Bilibili 直播弹幕、网页流品质切换、设置自动保存、普通视频与直播的弹幕一致性、网页启动全屏、缓存缩略图、自动移交播放器，以及网页／MPV 双向续播和 B站账号历史同步。

**下载本仓库的 [2026-10-12 累计更新 Release（含双向进度）](https://github.com/HikigayaMan/mpv-lazy-2026-custom/releases/tag/v2026.10.12-progress-update)。** 已有完整安装只需覆盖这一个累计更新包，再重新导入浏览器脚本，无需依次安装此前的小补丁。

## 下载与版本关系

| 文件或版本 | 用途 |
|---|---|
| [累计更新 ZIP](https://github.com/HikigayaMan/mpv-lazy-2026-custom/releases/download/v2026.10.12-progress-update/mpv-lazy-2026-custom-cumulative-update-v2026.10.12-progress.zip) | 包含相对原仓库的全部累计修改、配套脚本、文档及新构建的 B站中继 EXE。 |
| [SHA256SUMS](https://github.com/HikigayaMan/mpv-lazy-2026-custom/releases/download/v2026.10.12-progress-update/SHA256SUMS-v2026.10.12-progress-update.txt) | 校验下载的 ZIP 是否完整。 |
| [原项目可运行包](https://github.com/luoxue03/mpv-lazy-2026-custom/releases/tag/v2026.06) | 首次安装所需的 `base`、原版 `config`；使用 AI 功能时另需 `ai` 分卷。 |
| `external_player.user.js` / `external_player.js` | 本次包内两文件内容相同，脚本版本均为 **1.2.14.19**。前者可直接导入用户脚本管理器。 |
| `bilibili-live-bridge.exe` | 本次累计包内置的 Windows x64 B站专用中继，服务版本 **2026.10.12**，普通用户无需安装 Python 或 Node。 |

累计比较的原仓库基线为 `547e3d7f7a1aa44538ab55fdba3a36e5bcb61984`，即本分支与原仓库的共同起点。本次整合此前全部功能及最新双向进度修复，并加入发布工具、校验和文档。完整差异见 [基线到本次版本的比较](https://github.com/HikigayaMan/mpv-lazy-2026-custom/compare/547e3d7f7a1aa44538ab55fdba3a36e5bcb61984...v2026.10.12-progress-update)。`v2026.06` 是运行基础包版本，并非累计源码比较的起点。

本仓库保存源码、配置和文档；累计更新包需要覆盖到已有可运行整合包，单独克隆仓库或单独解压累计包不能提供 `mpv.exe`、播放器依赖或 AI 模型。

**后续源码／本地更新：** External Player **1.2.14.24** 支持 B站完整合集及分 P 的层级播放列表，并改为先播放当前视频、在后台补齐完整合集。此功能尚未包含在上方 `.19` 累计 Release 中，需要同步更新用户脚本、播放器 Lua／uosc 和中继；使用与升级说明见 [网页视频列表](./docs/scripts/web-playlist.md)，本次清理和修复范围见 [快启与清理报告](./docs/reports/2026-10-14-cleanup-and-fast-start.md)。

## 安装和升级

### 已有完整安装

1. 备份当前配置，关闭该安装目录的 MPV 和 Bilibili 中继。已经启动的旧程序仍使用内存中的旧脚本或 EXE，需要在更新后重启。
2. 下载累计 ZIP 和 SHA256SUMS，用以下命令核对 ZIP 哈希：

   ```powershell
   Get-FileHash .\mpv-lazy-2026-custom-cumulative-update-v2026.10.12-progress.zip -Algorithm SHA256
   ```

3. 将 ZIP 内的内容直接合并到 **`mpv.exe` 所在目录**，确认覆盖同名文件。正确结构是 `mpv.exe` 与 `portable_config/`、`tools/`、`docs/` 并列；不要在播放器目录中再套一层更新包目录。
4. 在 Tampermonkey 或 Violentmonkey 导入包内的 `external_player.user.js`，禁用重复的旧脚本，并关闭这份定制脚本的上游自动更新，避免定制功能被覆盖。
5. 完整刷新已打开的视频或直播网页，重新启动播放器。需要 B站直播弹幕或双向进度时，按下文检查 `MPV` 与 `BrowserRelay` 注册。

更新包不包含 `saved-props.json`、`uosc_danmaku_style.json`、RIFE 运行时偏好、`playback-progress.json`、Cookie、Trakt 凭证、播放历史、缓存或日志，因此不会用打包者的偏好覆盖用户的已保存设置。同名菜单和脚本配置会更新；直接手改过这些配置的用户，可从备份中合并自己的修改。

### 首次安装

1. 从原项目下载 `mpv-lazy-2026-custom-base-v2026.06.zip` 并解压。
2. 需要 RIFE、VapourSynth 或 AI 超分时，下载全部 AI 分卷，并用 7-Zip 从 `.001` 解压到同一目录。
3. 覆盖原项目该版本的最新 `config` 包，获得原有工具和依赖。
4. 最后按“已有完整安装”步骤覆盖本仓库的累计更新包。

本次不会重复打包未修改的 AI 模型、780+ 着色器、字体或整套运行时。已有这些依赖时无需重复下载。

### 网页移交、B站直播和进度中继

运行整合包中的 `url-scheme-handler.exe` 注册 `ush://`，添加应用 `MPV` 指向当前目录的 `mpv.exe`。应用名称必须与用户脚本设置一致；移动安装目录后需要更新路径。MPVNET 用户对应注册自己的程序。

B站直播弹幕和双向进度都需要将 `BrowserRelay` 注册到：

```text
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe
```

把 `url-scheme-handler.exe` 保留在整合包根目录。用户脚本会交给它以下 B站启动参数，启动脚本会自动定位同一安装内的中继和播放器：

```powershell
-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ".\tools\telegram-web-mpv-bridge\start_browser_relay.ps1" -Bilibili
```

未配置自动启动时，可先在整合包目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\telegram-web-mpv-bridge\start_browser_relay.ps1 -Bilibili
```

需要双向进度时也可手动运行同一启动脚本，加上 `-PlaybackProgress`。脚本会检查当前中继支持的进度 API，旧版本需要更新后重启。

B站专用中继使用 HTTP `127.0.0.1:9002` 和 WebSocket `127.0.0.1:9001`，与原 Telegram／斗鱼中继的 `8999/9000` 并存。本次包含 `bilibili-live-bridge.exe`；原有 `browser-media-bridge.exe`、斗鱼 helper 和 `url-scheme-handler.exe` 继续由原项目运行包提供。

## 相比原仓库的全部功能更新

**本次新增双向进度：** External Player .19 + 中继 2026.10.12 支持网页与 MPV 双向续播，并可同步 B站账号观看历史；WebSocket 受限时自动使用 HTTP 进度通道。本次累计包已整合此前 .15 Release 和后续进度功能，覆盖这一个包即可。详见 [进度同步](./docs/scripts/playback-progress.md)。

### 1. Bilibili 直播实时弹幕

在 PC 浏览器的 B站直播页点击 MPV，视频通过当前解析出的直链播放，弹幕通过本机中继接入 `uosc_danmaku`。短房间号会解析为真实房间号；网页播放器位于 iframe、使用自定义解析或资源嗅探时，也会单独交接当前房间的弹幕。

每次启动创建独立 Windows IPC pipe，避免多个播放器混入其他房间的消息。中继支持会话创建、浏览器心跳、断线重连、短期认证刷新和退出清理。暂停或切流加载时不积压弹幕，恢复后显示新收到的消息；隐藏弹幕保留连接。关闭播放器或打开其他文件后结束原会话。

保留直播网页可帮助刷新过期认证。关闭标签页后已有连接可继续运行，但认证过期时可能无法恢复。弹幕失败会给出阶段和原因，并允许视频继续播放；视频启动成功不等于弹幕连接成功。

### 2. 表情按文字显示

保留文字消息中的 `[dog]` 等标签，纯图片表情优先显示消息携带的表情名称；无名称时显示 `[表情]`。不会把表情图片链接作为弹幕，也不下载图片。礼物、系统广播和进房提示不作为文字弹幕显示。

### 3. 清晰度解析和播放器内品质切换

B站点播与直播在交接时传递当前账号实际可用的品质列表、编码和视频／音频信息。默认使用可用最高品质，用户明确选择较低档位时保留选择。播放器“流品质”菜单显示实际返回的档位，修复空菜单持续转圈；解析错误、失败或超时会结束等待并显示提示。

普通 DASH 视频切换时保留独立音轨、播放位置和暂停状态。同一直播间切换品质时保留弹幕会话和显示开关，清空旧时间轴，丢弃加载期间的消息，待新流就绪继续显示自然弹幕。直播切换失败或超过 15 秒会结束该会话，可刷新网页后重新交接。

菜单只提供账号和接口实际返回的源，不会解锁登录或付费权限。单条直链也不能恢复未交接的其他品质；地址过期或权限变化时需刷新网页。

### 4. B站同画质备用源

修复部分 B站 PCDN 主源采用特殊端口而本机无法连接的情况。DASH 视频和音频优先选择接口同时返回的、标准 HTTPS 端口的 `bilivideo.com` 备用源，保持画质、编码和原始签名。该修复已随 `1.2.14.15` 用户脚本打包。

### 5. 右键设置自动保存

以下明确的菜单选择会自动保存，并在重新启动或打开其他普通视频后恢复：

| 类别 | 保存内容 |
|---|---|
| 视频滤镜 | 补帧、超分、RTX HDR、启用状态和清空滤镜；RIFE 参数继续使用各自的运行时配置。 |
| 着色器 | 单个开关、组合预设和清空着色器。 |
| 画面设置 | 解码模式、去色带、去隔行、自动校色、时间码解析、亮度、对比度、伽马、饱和度、色相。 |
| 弹幕 | 显示开关、粗体、字号、描边、阴影、速度、不透明度、显示区域和帧率。 |
| 既有音频偏好 | 音量和静音继续自动保存。 |

全局画面偏好写入时会合并其他实例已保存的属性；弹幕样式通过偏好文件同步到其他实例。清空滤镜／着色器也会保存；自动 SDR/HDR、低码率预设和临时弹幕 FPS 滤镜不会被误记成用户选择。保存失败时会提示。播放位置、直链、暂停及轨道选择不属于全局画面偏好，原有 watch-later 仍负责文件恢复。

全局偏好位于 `portable_config/saved-props.json`，弹幕位于 `portable_config/script-opts/uosc_danmaku_style.json`。`工具 > 清除已记录的属性值` 和弹幕菜单“清除已保存样式”分别清除对应记录。

### 6. 普通视频与直播共用弹幕行为

普通视频和直播统一使用 `uosc_danmaku` 的开关、样式与帧率。首次无开关记录默认显示；明确关闭后下次保持关闭，退出或清理字幕不会意外保存成关闭。已经运行的实例会同步其他实例保存的样式。

普通视频修改样式后会根据已加载弹幕重建排布，暂停时也能刷新，不需重新下载弹幕。显示区域、速度、粗体和重复弹幕数量标记都遵循同一套设置。`b` 以及旧 `bilibiliAssert/toggle` 入口转交统一插件，避免旧插件生成固定样式字幕；B站分 P 的 `cid` 交接和短视频加载也已修复。

### 7. 网页启动自动全屏

通过用户脚本交给 **MPV 或 MPVNET** 的普通视频、直播默认以全屏启动。脚本移除冲突的全屏参数再加入 `--fullscreen=yes`，其他播放器按各自配置启动。

### 8. 播放条上的缓存缩略图

鼠标在播放条停留约 0.2 秒后显示附近画面。本地视频沿用完整预览；网络视频只对 MPV 实际缓存的 `seekable-ranges` 生成缩略图，缓存之外显示“未缓存”并清除上一张图。

预览从主播放器导出一小段已缓存内容，再用本地文件生成图片，子进程不重新请求原视频 URL，不改变主播放位置或暂停状态。切换文件、移走鼠标、缓存驱逐或任务过期时清理预览和临时文件。工具栏按钮、`Ctrl+t` 开关及 `Ctrl+Alt+t` 重启统一控制 `thumb_engine`，避免多个引擎冲突。

预览采用附近关键帧，最多请求四秒缓存窗口，并非精确逐帧定位。缓存刚开始、缓存边界缺少完整视频包，或格式无法封装／解码时，可能暂不显示；不会为补齐缩略图下载未缓存内容。直播没有可用缓存时间轴时也无法提供任意时刻预览。

### 9. 进入网页后自动移交默认播放器

用户脚本的快速设置与完整设置新增“**进入视频或直播页面后自动移交默认播放器**”，默认关闭。开启后，下一次进入、刷新或切换至可解析的新内容时，自动启动所选且已启用的默认播放器；开启开关或改变默认播放器不会立即重复启动当前内容。

B站不同分 P、不同直播间和 YouTube 新视频会重新识别，分享／时间参数变化、iframe 初始化和重复嗅探通知不会重复启动同一内容。旧播放器的单独自动播放配置仍兼容，启用全局设置时仅启动默认播放器。解析失败可手动点击重试；浏览器自身的外部协议确认仍由浏览器控制。

### 10. 配套诊断、构建与回归验证

B站中继内置会话阶段和最近失败记录，区分“浏览器已连接”与“弹幕会话已建立”。新增免依赖 EXE 构建脚本、网页脚本行为测试、Lua 保存／样式／品质／缩略图测试及真实 MPV 冒烟工具。本次累计打包工具从 Git 提交读取全部差异及配套模块，避免遗漏依赖或混入本地运行数据。

### 11. 网页／MPV 双向播放进度

- 网页观看后移交 MPV／MPVNET，从最近观看位置继续；播放器观看和拖动后的进度同步回原网页，并保存本机续播位置。
- B站普通 BV／av 投稿及分 P 按 `aid + cid` 分开记录，同时通过浏览器现有登录状态写入 B站账号历史。Cookie、CSRF 和签名密钥不会发送给中继或写入播放器。
- 支持倒退和回到零秒，按最近一次观看选择位置；看完才标记完成，手动退出不误标看完。品质切换中的临时零秒不会抹掉位置。
- 默认开启，网页设置可分别关闭双向进度和账号历史写入。网页主动恢复播放或拖动时接管记录；后台暂停的旧网页不会覆盖播放器。
- 关闭网页后中继仍保存本机位置；账号历史需要网页保持打开，重新进入相同视频可补同步。普通网页视频支持可读取的有限时长视频，账号历史当前专门支持 Bilibili；直播、番剧影视和互动分支不套用点播续播。
- .18 起具备 HTTP 兜底；.19 修正 MPV 参数传递。包内 Lua 兼容已导入 .18；已经手动安装这次进度补丁的用户可继续使用 .18，其余升级用户统一导入包内 .19。

真实测试已验证 MPV 的 187.5 秒回写原网页，B站历史显示 `03:07 / 06:20`，退出播放器后保留本机位置。安装和使用见 [进度同步](./docs/scripts/playback-progress.md)，验证细节见 [联机报告](./docs/reports/2026-10-12-playback-progress.md)。此功能已包含在本次累计 Release 中。

### 12. B站完整合集及分 P 层级播放列表（.24 源码／本地更新）

普通 BV／av 投稿属于合集时，将完整合集、其中每个视频以及全部分 P 交给 MPV／MPVNET；没有合集时交接当前视频的全部 P，从网页当前选中的具体 P 开始。播放列表顶部显示合集名和当前视频序号，有多个分节时增加分节层。多 P 视频在同一面板展开／收起，子项显示名称与时长；单 P 视频直接点击播放，不增加只有一项的子层。

展开／收起只改变菜单显示，原生播放列表始终保留全部可播放条目。顺序播放和列表循环遍历整个合集及各 P，乱序以具体 P 为单位，单曲循环重播当前 P 的完整视频。点击条目时重新查找当前原生条目 ID，避免乱序、移除或重排后误用菜单行号。首次打开和当前播放项变化时展开其祖先，其他折叠选择保留。

后续条目在实际打开时通过原网页重新解析，保持可用品质、独立音轨、字幕和对应 CID 的弹幕。切画质保留列表、位置和暂停状态；各条目的续播和 B站账号历史分别记录。请保留原网页供后续条目解析。详情与文件更新清单见 [网页视频列表](./docs/scripts/web-playlist.md)。

`.24` 不再等完整合集的全部分 P 详情解析完才启动播放器：先交接网页当前选中的 CID，后台以最多三个并发请求补齐完整清单，再扩展同一个原生列表。后台使用轻量分 P 接口，避免每个视频重新下载整份合集；核对连续页序、唯一 CID 和已知分 P。扩展保留当前条目的原生 ID、时间、暂停、品质、音轨和弹幕，不重新加载正在播放的视频。菜单加载期间显示“完整合集正在后台加载”，失败明确显示“合集加载失败”；完整清单提交后才显示真实视频序号和总数。

视频和分 P 的数量不设两条或 500 条硬限。完整公开元数据通过分块注册、分页恢复及分块 IPC 传递，每份清单预算为 **32 MiB**，最多 **8 个会话**；超过预算明确报错，不静默截断合集。1001 条目及 3 P／4 P 视频的真实 MPV 验证工具见 [发布与验证说明](./tools/release/README.md#完整合集层级与-1001-条目验证)，最新快启验证见 [快启与清理报告](./docs/reports/2026-10-14-cleanup-and-fast-start.md)。

此前 `.21` 已通过 **460 项回归**及真实 MPV 的 **1001 条目**测试；截图对应的合集已真实交接 **20 个视频、24 个分 P**，4 P 与 2 P 均完整显示。实际复测通过选集、折叠保持、乱序、片尾顺序播放、单曲／列表循环、画质切换和独立续播。该版本的菜单截图与历史验证范围保留在 [层级列表报告](./docs/reports/2026-10-13-web-playlist-hierarchy.md)。

此前 `.20` 的 401 项回归及真实 B站双视频复测已通过：`MISMATCH`／`Movie Star` 可选集、顺序与乱序播放、单曲及列表循环；切品质保留列表和独立进度。联机发现的长签名地址 IPC 部分写入问题已修复，历史范围保留在 [列表验证报告](./docs/reports/2026-10-12-web-playlist.md)。

### 13. 重复项清理与加载、品质、弹幕修复（.24 源码／本地更新）

- 复用首播已经取得的 B站 `view` 和 `playurl` 结果，避免为移交同一个当前 CID 再做一轮相同解析；完整合集补齐在后台进行，页面切换后不会混用旧页面结果。
- 清理 `input_uosc.conf` 中 **15 条重复快捷键绑定**，保留对应菜单、快捷键和最终生效命令。`SR_ARTCNN_NV_2025.vpy` 保留旧入口，转用 `SR_ARTCNN_NV_2K.vpy` 的共用参数，避免两份相同预设分别维护。
- 通用品质重载和浏览器直链品质切换共用有目标校验、失败及超时清理的续播回调；旧重载失败后不会把下个视频跳到上个视频的位置。合集视频初次加载或品质重载中不接受重复重载，避免临时零秒覆盖原位置。
- 修复普通视频自动弹幕的 CID 请求失败时缺少回调导致的 Lua 异常，失败时保留当前视频播放。
- IPC 的字节预算和实际发送使用同一份 JSON 序列化，按 UTF-8 及嵌套 JSON 转义后的真实大小分块，防止文字或长地址超过 Windows pipe 限制。
- 刷新网页或新页面接管未完成的合集时恢复后台补齐，撤销旧页面未提交的上传；HTTP 与 WebSocket 都转发合集就绪和失败通知，避免 HTTP 兜底模式一直显示加载中。
- 自动移交等待页面控件初始化完成；提示和加载组件也能按需创建，避免进入网页后触发空组件异常。后台详情遇到临时网络故障仅重试一次，接口拒绝或数据不完整仍明确报错。

这些清理不删除用户偏好、播放历史、AI 模型或原有功能入口。最新测试与实际网页耗时以 [快启与清理报告](./docs/reports/2026-10-14-cleanup-and-fast-start.md) 为准。

本次 `.24` 已通过 **529 项回归**及真实 MPV 的 **1001 条目**集成测试。目标 325 视频合集从搜索进入后自动移交成功：从自动移交流程开始计，约 **3.9 秒发出启动指令**、**4.8 秒提交当前源**，完整合集在后台约 **60 秒**完成，保持当前视频、暂停位置、1080p、音轨与弹幕。启动指令耗时不是精确首帧时间；详细阶段与截图见上述报告。

## 已发布 .19 累计更新的验证范围

此前功能验证见 [累计功能报告](./docs/reports/2026-10-12-cumulative-update.md)，本次进度实测与回归见 [双向续播报告](./docs/reports/2026-10-12-playback-progress.md)。

下表对应已发布的 `.19` 累计包；后续 `.24` 快启与清理的测试结果、实际网页耗时和限制见 [2026-10-14 报告](./docs/reports/2026-10-14-cleanup-and-fast-start.md)。

| 验证 | 结果与范围 |
|---|---|
| 自动测试 | 57 项中继 Python、87 项用户脚本、150 项发布／Lua／包完整性测试通过，共 294 项。 |
| 新构建 EXE | 隔离端口的 `/health`、WebSocket hello、匿名 HTTP 进度接口和会话诊断通过，原生 Brotli 解码依赖已包含。 |
| 真实 B站直播 | 2026-10-11 房间 1017：网页与 MPV 均为 1920×1080，1080p → 720p → 1080p 保持同一弹幕会话并收到新消息。 |
| 真实 B站点播品质 | BV1ju6TYHEKU P5：1920×960，四档菜单，切至 640×320 再切回，独立音轨和暂停的 20 秒位置保持。 |
| 新版自动移交点播 | `1.2.14.15` 在真实 B站网页自动启动一次，MPV 全屏、1920×1080、普通弹幕及缓存缩略图均验证通过。 |
| 双向进度及账号历史 | 真实 B站网页 → MPV 续播；MPV 187.5 秒回写原网页，官方账号历史显示 `03:07 / 06:20`；退出保存且不误标完成。 |
| 进度隔离与兼容 | 实际 MPV + WebSocket／HTTP 通道：倒退及零秒、品质切换、断开网页后本机保存、重开恢复、分 P／换文件隔离，.18 旧参数与 .19 分别传参均通过。 |
| 缓存边界 | 本地 HTTP + 真实 MPV/uosc：缓存内出图、未缓存清图、额外视频请求为 0、播放位置／暂停保持、临时文件清理。 |
| 跨实例偏好 | 六次真实播放器启动，使用离线素材验证直播／点播开关、样式与帧率继承。 |

新版自动移交的直播入口有脚本行为测试，本次未另做真实直播自动启动联机复测。上述测试不保证所有网页、编码、账号权限或网络线路都能正常工作。

## 原仓库已有功能与依赖

以下由原项目提供，继续保留，并非本次新增加的模型或功能：RIFE 模型及动态参数菜单、AI 超分、15 个超分与 10 个 RIFE 模型的组合、780+ GLSL 着色器、AI 字幕、Trakt、Telegram 浏览器媒体中继、斗鱼签名解析与弹幕，以及原版 External Player 悬浮 UI 和资源嗅探。

原仓库配置曾以 RTX 4080 SUPER 为主要调试环境；基础视频播放不要求该显卡，RIFE、TensorRT 和 AI 超分仍取决于对应硬件及运行依赖。首次生成 TensorRT engine 可能耗时较长。个人账号相关功能需自行配置。

## 文档入口

- [工具脚本安装、配置与使用](./docs/工具脚本安装配置使用说明.md)
- [External Player 与自动移交](./docs/scripts/external-player.md)
- [B站直播弹幕安装与排错](./docs/scripts/bilibili-live-danmaku.md)
- [流品质](./docs/scripts/stream-quality.md)
- [设置自动保存](./docs/scripts/settings-persistence.md)
- [缓存缩略图](./docs/scripts/cached-thumbnails.md)
- [网页／MPV 双向进度与 B站观看历史](./docs/scripts/playback-progress.md)
- [uosc_danmaku](./docs/scripts/uosc-danmaku.md)
- [Telegram／斗鱼中继](./docs/scripts/telegram-web-mpv-bridge.md)
- [AI 字幕](./docs/scripts/sub-fastwhisper.md)、[Trakt](./docs/scripts/trakt-scrobble.md)
- [仓库结构](./REPO_STRUCTURE.md)、[构建与发布工具](./tools/release/README.md)

## 更新包内容与维护

更新包为平铺覆盖结构：`portable_config/`、`tools/`、`docs/`、浏览器脚本、README 和许可说明。完整包含累计变更，还补齐 `uosc`、`uosc_danmaku`、`thumb_engine`、`bilibiliAssert` 及中继／发布工具的已跟踪配套源码。包中无须删除的旧文件。

`UPDATE-MANIFEST.json` 记录基线、准确的源提交、累计修改列表、各文件大小和 SHA-256，以及 EXE 构建输入和验证信息。`UPDATE-FILES-SHA256.txt` 校验包内文件，Release 的 SHA256SUMS 校验整个 ZIP。包内另有 EXE 的第三方许可说明。

使用 [`package_custom_update.py`](./tools/release/package_custom_update.py) 构建本仓库累计包，流程见发布工具 README。原 `package_release.py` 保留用于完整分类包构建，其默认目标为原作者仓库；使用它发布时需显式指定目标仓库，本次累计 Release 不使用该默认上传流程。

## 上游与致谢

- 原配置整合：[luoxue03/mpv-lazy-2026-custom](https://github.com/luoxue03/mpv-lazy-2026-custom)
- mpv-lazy 基础：[hooke007/mpv_PlayKit](https://github.com/hooke007/mpv_PlayKit)
- 播放器：[mpv-player/mpv](https://github.com/mpv-player/mpv)
- 原用户脚本：[LuckyPuppy514/external-player](https://github.com/LuckyPuppy514/external-player)，原作者定制：[luoxue03/external-player](https://github.com/luoxue03/external-player)
- URL Scheme：[luoxue03/url-scheme-handler](https://github.com/luoxue03/url-scheme-handler)
- 中继来源：[luoxue03/telegram-web-mpv-bridge](https://github.com/luoxue03/telegram-web-mpv-bridge)
- [VapourSynth](https://github.com/vapoursynth/vapoursynth) 及各插件、脚本和模型作者

各文件按自己的许可证与原作者声明使用，见 [LICENSE.MD](./LICENSE.MD)、文件头和包内第三方许可。用户脚本保留原作者及 MIT 声明。
