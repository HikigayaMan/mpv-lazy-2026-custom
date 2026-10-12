# 播放条缓存缩略图

鼠标停留在播放条约 0.2 秒后显示附近画面的缩略图。本地视频沿用完整预览；网页移交的网络视频仅在 MPV 实际缓存范围内预览，未缓存的位置显示时间与“未缓存”，并清除上一张缩略图。

网络预览从主播放器的 `demuxer-cache-state.seekable-ranges` 判断范围，再异步导出该范围内的小窗口到临时本地文件，仅使用本地文件生成画面。缩略图子进程不打开原视频 URL，不重新请求网页视频，也不改变主播放器的播放位置或暂停状态。切换视频、移走鼠标及缓存驱逐时，会取消过期任务并清理临时文件。

默认使用 `thumb_engine`，已启用 `cache_only_network=yes`，短视频也可预览。`Ctrl+t` 开关预览，`Ctrl+Alt+t` 重启引擎；工具栏中的缩略图按钮也控制同一引擎。

画面由请求位置附近的关键帧生成，并非每次都精确对应悬停的那一帧。请求窗口最多四秒，并按实际缓存边界截断；封装可能按 GOP 边界对齐。缓存刚开始或接近缓存末尾时，如果缓存缺少足够的完整视频包，暂不显示缩略图，后续缓存到位后可再次预览。不会为了补齐缩略图下载未缓存内容。[MPV 缓存导出说明](https://mpv.io/manual/stable/#command-interface-dump-cache)

本机缩减版 FFmpeg 优先使用 MOV 导出，失败后尝试 Matroska；如果当前格式无法封装或解码，则清除预览，正常播放不受影响。

回归测试：`tools/release/test_cached_thumbnails_lua.py` 覆盖缓存边界、异步任务隔离、请求合并、驱逐清图和清理。真实播放验证：

```powershell
python tools/release/smoke_test_cached_thumbnails.py --mpv "C:/path/to/mpv.exe" --output-dir "C:/path/to/verification"
```

验证使用隔离配置和本地 HTTP 视频，在真实 uosc 播放条上悬停缓存内外的位置，并检查缩略图不增加网络请求、播放位置和暂停状态保持。
