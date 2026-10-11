# uosc_danmaku 使用说明

## 1. 介绍

`uosc_danmaku` 是 mpv 弹幕插件，基于 uosc UI 框架和弹弹play API，可在 mpv 内搜索、加载、开关和调整弹幕。当前自定义版还提供版本化的实时弹幕 API，斗鱼直播弹幕可以直接复用同一套样式、菜单和渲染器。

当前整合包中它用于补充/替代部分 B 站弹幕体验，并支持网络视频自动加载弹幕。配置已按当前整合包样式做过调整，例如字体、透明度、显示区域、滚动速度和 `vf_fps`。

依赖/相关仓库：

- uosc_danmaku 上游：https://github.com/Tony15246/uosc_danmaku
- uosc 上游：https://github.com/tomasklaen/uosc
- url-scheme-handler 本地维护：https://github.com/luoxue03/url-scheme-handler?tab=readme-ov-file
- external-player 本地维护：https://github.com/luoxue03/external-player

## 2. 文件介绍

| 文件/目录 | 作用 |
|---|---|
| `portable_config/scripts/uosc_danmaku/main.lua` | 插件入口，注册弹幕搜索、开关、保存、延迟、样式等消息。 |
| `portable_config/scripts/uosc_danmaku/apis/` | 弹幕 API 适配，例如弹弹play与扩展服务。 |
| `portable_config/scripts/uosc_danmaku/sites/` | 站点适配，例如 Bilibili、巴哈姆特、爱奇艺、芒果、腾讯、优酷。 |
| `portable_config/scripts/uosc_danmaku/modules/` | 菜单、渲染、解析、哈希、压缩、工具函数等内部模块。 |
| `portable_config/scripts/uosc_danmaku/modules/live.lua` | 接收有界实时消息批次，并生成动态 ASS 弹幕。 |
| `portable_config/scripts/uosc_danmaku/modules/style.lua` | 保存/加载跨进程样式和视频、直播共用的弹幕帧率偏好。 |
| `portable_config/scripts/douyu_danmaku.lua` | 斗鱼弹幕兼容路由器；优先接入 uosc，必要时才使用轻量 ASS 兜底。 |
| `portable_config/scripts/uosc_danmaku/dicts/` | 简繁转换字典。 |
| `portable_config/scripts/uosc_danmaku/README.md` | 插件上游说明。 |
| `portable_config/script-opts/uosc_danmaku.conf` | 当前整合包的弹幕插件配置。 |
| `portable_config/input_uosc.conf` | 菜单入口定义：`工具 > 弹幕 > ...`。 |

## 3. 配置

当前配置文件：`portable_config/script-opts/uosc_danmaku.conf`。

当前关键配置：

```ini
api_server=https://api.dandanplay.net
auto_load=no
autoload_for_url=yes
vf_fps=yes
opacity=0.55
fontname=SimHei
fontsize=16
bold=yes
outline=1
shadow=0
displayarea=0.05
scrolltime=10
fixtime=5
history_path=~~/_cache/danmaku-history.json
```

说明：

- `autoload_for_url=yes`：网络视频可尝试自动加载弹幕。
- `auto_load=no`：本地番剧记忆型自动加载默认关闭，避免误匹配。
- `vf_fps=yes`：启用弹幕帧率相关处理，用于补帧后弹幕速度/帧率适配。
- `displayarea=0.05`：弹幕显示区域较小，避免覆盖画面过多。
- `opacity=0.55`：弹幕透明度。
- `history_path=~~/_cache/danmaku-history.json`：弹幕历史缓存路径，属于运行态。
- `live_fps_mode=auto`：直播弹幕平滑模式，内置默认值；也可在菜单中选 `60 / 80 / 120 / display`。

如要恢复更密集的弹幕区域，可以提高 `displayarea`；如觉得弹幕过快/过慢，调整 `scrolltime`。

## 4. 使用

当前菜单入口位于 `工具 > 弹幕`：

| 菜单项 | 对应命令 | 作用 |
|---|---|---|
| `BilibiliAssert 开关 (b)` | `script-binding bilibiliAssert/toggle` | 开关旧 B 站弹幕脚本。 |
| `搜索弹幕` | `script-message open_search_danmaku_menu` | 打开搜索弹幕菜单。 |
| `开关弹幕 开关 (j)` | `script-message show_danmaku_keyboard` | 显示/隐藏弹幕。 |
| `弹幕设置` | `script-message open_add_total_menu` | 打开弹幕设置总菜单。 |

基础流程：

1. 打开视频。
2. 打开 `工具 > 弹幕 > 搜索弹幕`。
3. 输入番剧名或选择匹配结果。
4. 选择剧集。
5. 弹幕加载后可用 `开关弹幕` 显示/隐藏。
6. 如弹幕时间不准，进入弹幕设置调整延迟。

网络视频流程：

1. 用 `external_player.js` 从网页拉起 mpv。
2. 对支持 URL 自动识别的站点，插件会尝试自动加载弹幕。
3. 如果自动加载失败，手动进入 `搜索弹幕`。

### Bilibili 直播弹幕

PC 浏览器的 External Player 可将 B 站直播的实时文字弹幕交给本机中继，再通过每次播放独立的 IPC pipe 接入本插件。与普通视频使用同一套字体、透明度、显示区域、速度和弹幕帧率设置，`j` 可反复隐藏／显示，暂停时不积压消息。

本功能需要同步更新本仓库用户脚本和 Bilibili 专用中继（`bilibili-live-bridge.exe`，或本机源码环境）。完整步骤见 [Bilibili 直播弹幕安装与交接说明](./bilibili-live-danmaku.md)。

### 斗鱼直播弹幕

斗鱼直播由 External Player 与 Browser Media Bridge 建立每次播放专用的 MPV IPC pipe。`douyu_danmaku.lua` 会先等待 `uosc_danmaku` 实时 API：

1. API 已就绪：直接进入 uosc 路径。
2. API 稍晚加载：短暂缓存首批消息，再升级到 uosc。
3. API 始终不可用：使用不改动 VF 的轻量 ASS 兜底。

进入 `工具 > 弹幕 > 弹幕设置` 后，可以实时调整：

- 字号、粗体、描边和阴影
- 不透明度
- 弹幕速度
- 显示区域
- 弹幕帧率：`auto / 60 / 80 / 120 / display`，普通视频和直播共用

普通视频修改任意样式后，会从已加载的弹幕重新生成排布并立即刷新字幕，暂停时也会更新。粗体关闭同样作用于合并重复弹幕，显示区域和滚动速度的更改不会继续沿用旧排布。字体名称可通过配置文件设置。

合法样式修改现在会自动保存，新拉起的 mpv 也会读取同一套设置。“立即保存当前样式”可用于手动重试。保存文件为：

```text
portable_config/script-opts/uosc_danmaku_style.json
```

它是本机运行时偏好，不进入 Git 和配置发布包。

### 弹幕帧率与 RIFE

弹幕看起来是否流畅，不只取决于视频本身是不是 60 fps，还取决于 ASS 位置更新和 mpv 渲染节奏。普通视频和直播的 ASS 模式会在没有冲突时使用独立的 `@danmaku_smooth` 帧率滤镜。保留原有 `live_fps_mode` 配置键，以兼容已有偏好文件。

- `auto`：按显示器/视频状态选择安全值，推荐日常使用。
- `60 / 80 / 120`：手动指定弹幕平滑目标。
- `display`：优先跟随当前显示器刷新率。
- 已启用 RIFE、VapourSynth 或其他不应叠加的 VF 时，弹幕脚本不会再添加平滑滤镜。
- 启用“超分补帧 > 自定义组合”时，组合菜单会主动移除 `@danmaku_smooth`，避免和 RIFE 重复补帧。

## 5. 常见错误

### 搜索弹幕没有结果

- 弹弹play API 变更或网络不可达。
- 标题识别不准确。
- 当前视频不是动画/番剧类资源，弹幕库没有匹配项。

处理：手动改关键词搜索；确认网络能访问 `api.dandanplay.net`。

### 补帧后弹幕速度不对

- 确认 `vf_fps=yes`。
- 若仍异常，先清空当前 VF，确认是补帧组合还是弹幕插件自身问题。
- 某些超分+补帧组合负载很高，可能导致画面与弹幕感知不同步。

### 斗鱼弹幕仍然不够流畅

1. 先在弹幕设置中选择 `auto` 或 `display`。
2. 查看统计信息里是否存在 `@danmaku_smooth`，或当前是否已经由 RIFE 输出高帧率。
3. 若 GPU/渲染队列已满，提高弹幕帧率只会增加压力，应降低到 `60`。
4. 样式修改后确认没有自动保存失败提示，再重新从网页拉起一次直播。

### 斗鱼只有视频、没有弹幕

- Browser Media Bridge 或 Node helper 未启动。
- Node.js 未安装，或工具目录尚未执行 `npm install`。
- mpv 启动后 IPC pipe 未能建立。
- 弹幕在 uosc 中被关闭。

视频与弹幕链路故意解耦，因此弹幕失败不会阻止直播画面。

### 弹幕区域太大或太小

调整 `displayarea`。值越大，可显示弹幕的区域越高；当前整合包偏向低遮挡。

### 网络视频自动加载失败

- 站点不在插件支持范围。
- `external_player.js` 没有传入足够的 URL / 标题 / cid 信息。
- URL 是中转链接或临时流，插件无法识别原站。

处理：手动搜索弹幕；B 站场景确认 external_player args 中是否包含 `cid`。

