# trakt-scrobble 使用说明

## 1. 介绍

`trakt-scrobble` 是 mpv 的 Trakt.tv 同步脚本，用于把正在播放的电影/剧集同步到 Trakt。脚本会在视频播放时尝试识别片名、季集信息，开始 scrobble；暂停时保存播放进度，停止、切换或退出时按进度阈值决定是否标记已看。

依赖/相关仓库：

- trakt-scrobble 上游：https://github.com/dyphire/trakt-scrobble
- Trakt 官网：https://trakt.tv

当前整合包已接入菜单和快捷键：

```text
工具 > Trakt > 搜索匹配
工具 > Trakt > 开关同步
工具 > Trakt > 自动通知开关（默认关闭）
工具 > Trakt > 授权登录
工具 > Trakt > 完成授权
```

## 2. 文件介绍

| 文件/目录 | 作用 |
|---|---|
| `portable_config/scripts/trakt-scrobble/main.lua` | 脚本入口，负责授权、搜索匹配、开始/停止 scrobble。 |
| `portable_config/scripts/trakt-scrobble/modules/` | base64、标题猜测、菜单等内部模块。 |
| `portable_config/scripts/trakt-scrobble/imgs/` | 上游说明图片。 |
| `portable_config/scripts/trakt-scrobble/README.md` | 上游英文说明。 |
| `portable_config/trakt_config.json` | 授权 token 等运行态配置，自动生成，不应提交。 |
| `portable_config/trakt_history.json` | 手动匹配历史，自动生成，不应提交。 |
| `portable_config/input_uosc.conf` | Trakt 菜单与快捷键定义。 |

## 3. 配置

脚本内默认配置：

```lua
enabled = true
show_notifications = false
congfig_path = "~~/trakt_config.json"
history_path = "~~/trakt_history.json"
max_title_length = 100
auto_match_policy = "balanced"
min_auto_confidence = 85
min_prompt_confidence = 60
watched_threshold = 85
min_real_play_seconds = 120
mark_watched_on = "threshold"
trust_legacy_history = false
debug_match = false
use_directory_title_match = true
directory_min_auto_confidence = 95
directory_min_confidence_gap = 15
generic_title_blocklist = "current,video,web,default,play"
directory_title_blocklist = "movie,movies,film,films,video,videos,download,downloads,anime,bangumi,series,tv,season,seasons,合集,电影,剧集,动画,动漫,下载"
```

关键配置说明：

- `min_auto_confidence=85`：自动匹配置信度达到该值才会自动同步。
- `show_notifications=false`：自动匹配、开始/停止同步等后台通知默认不弹出；授权、手动操作和错误仍会显示必要反馈。
- `min_prompt_confidence=60`：低于自动阈值时只提示手动确认，不直接写入 Trakt。
- `watched_threshold=85`：停止、切换或退出时，播放进度达到 85% 后才允许标记已看。
- `min_real_play_seconds=120`：至少实际播放 120 秒才允许自动标记已看，避免短时间拖动进度条误记。
- `trust_legacy_history=false`：旧版 `trakt_history.json` 默认不作为可信递推来源，手动确认后会写入新版可信记录。
- `generic_title_blocklist`：泛标题拦截列表，命中后不会自动匹配，例如 `current`、`video`、`web`。
- `use_directory_title_match=true`：文件名和可信历史都无法匹配时，才尝试用父文件夹名作为兜底搜索词。
- `directory_min_auto_confidence=95`：目录名兜底需要更高置信度，宁愿失败也不强行同步。
- `directory_min_confidence_gap=15`：目录名兜底要求 Trakt 第一候选明显领先第二候选；同名或近似结果过多时不自动同步。
- `directory_title_blocklist`：目录名泛标题拦截列表，例如 `Movies`、`Season 2`、`下载`、`合集` 不会作为标题自动匹配。

当前使用脚本内置 Trakt API client。一般不需要自行配置。

如果你想使用自己的 Trakt API：

1. 打开 https://trakt.tv/oauth/applications 创建应用。
2. 获取 `client_id` 和 `client_secret`。
3. Base64 后替换脚本配置或放入脚本配置文件。

注意：`trakt_config.json` 和 `trakt_history.json` 是个人运行态文件，已被 `.gitignore` 忽略。

## 4. 使用

### 首次授权

1. 打开任意视频。
2. 进入 `工具 > Trakt > 授权登录`。
3. mpv 会显示类似 `https://trakt.tv/activate/XXXXXX` 的地址或验证码。
4. 在浏览器打开该地址，登录 Trakt 并输入验证码。
5. 回到 mpv，选择 `工具 > Trakt > 完成授权`。
6. 成功后会生成 `portable_config/trakt_config.json`。

如果验证码过期，需要重新执行 `授权登录` 获取新 code。

### 日常同步

- 脚本默认启用，视频加载后会尝试自动识别并 scrobble。
- 高置信匹配会自动同步；低置信匹配只提示按 `Alt+d` 手动选择，不会直接写入 Trakt。
- 普通暂停不会立刻提交 Trakt；停止、切换文件或退出时统一判定。
- 退出播放时，如果未达到 `watched_threshold`，会调用 Trakt pause 写入继续观看；达到进度阈值且实际播放时间达到 `min_real_play_seconds`，会调用 Trakt stop 标记已看。
- 中文剧集会尝试清洗副标题，例如 `《凡人修仙传》第181话 慕兰之战05` 会用 `凡人修仙传` 作为搜索词，并保留第 `181` 集。
- 手动确认过的剧集目录会写入可信历史；后续同目录剧集可按当前集数自动递推，并校验 Trakt 上该集存在。
- 如果文件名无法提供可靠标题，脚本会最后尝试使用父文件夹名兜底匹配；目录名兜底优先级低于可信历史和文件名解析，且必须高置信、非泛标题、非模糊结果才会自动同步。
- 两级剧集目录可自动识别，例如 `剑来\第一季\01.mkv` 会解析为 `剑来 S01E01`；`第一季`、`第二季`、`第十季` 只作为季号，不会被当作电影标题搜索。
- 中文剧名在 Trakt 有多个候选时不会自动选第一项；脚本会提示按 `Alt+d` 手动选择，确认一次后后续同目录可走可信历史递推。
- 如果识别错误，可按 `Alt+d` 或进入 `工具 > Trakt > 搜索匹配` 手动选择正确条目。

### 菜单与快捷键

| 入口 | 命令 | 作用 |
|---|---|---|
| `Alt+d` | `script-message-to trakt_scrobble search-menu` | 打开搜索匹配菜单。 |
| `Alt+D` | `script-message-to trakt_scrobble toggle-scrobble` | 开关当前同步。 |
| `工具 > Trakt > 自动通知开关（默认关闭）` | `script-message-to trakt_scrobble toggle-notifications` | 开关并持久化后台自动通知。 |
| `工具 > Trakt > 授权登录` | `script-message-to trakt_scrobble auth-menu` | 获取 Trakt 设备授权 code。 |
| `工具 > Trakt > 完成授权` | `script-message-to trakt_scrobble complete-auth` | 浏览器完成授权后回到 mpv 执行。 |

## 5. 常见错误

### 第二次授权提示 code expired

如果你已经成功授权，第二次再用旧 code 登录提示过期是正常现象。真正要看的是 `完成授权` 后是否生成/更新 `trakt_config.json`，以及播放时是否能 scrobble。

### 选择“完成授权”提示验证码过期

- 当前没有先执行 `工具 > Trakt > 授权登录`。
- 旧验证码已经过期。

处理：重新执行 `工具 > Trakt > 授权登录` 获取新验证码，在浏览器完成授权后再执行 `工具 > Trakt > 完成授权`。

### 同步到了错误的电影/剧集

- 文件名无法准确解析，或标题过短/过泛。
- 旧版 `trakt_history.json` 中已有错误记录。
- Trakt 搜索结果误匹配。

处理：使用 `Alt+d` 或 `工具 > Trakt > 搜索匹配`，选择正确条目；手动选择结果会写入新版可信 `trakt_history.json`。如果 Trakt 网站上已经出现错误已看记录，需要在 Trakt 网页中手动删除对应历史。

### 播放了但没有标记已看

- 当前进度低于 `watched_threshold`。
- 实际播放时长低于 `min_real_play_seconds`。
- 当前匹配置信度不足，脚本只提示手动确认，没有自动同步。

处理：确认播放进度和配置阈值；如果标题识别不确定，使用 `Alt+d` 手动选择。

### 完全不同步

- 未完成授权。
- 网络无法访问 Trakt API。
- `trakt_config.json` 过期或损坏。

处理：删除本地 `portable_config/trakt_config.json` 后重新授权。
