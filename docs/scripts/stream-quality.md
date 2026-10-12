# 浏览器交接与 MPV 流品质

External Player `1.2.14.19` 与本仓库的 `quality-menu.lua` 配合使用。浏览器会把 Bilibili 视频和直播中当前账号实际可用的品质列表一并交给 MPV，播放器的“流品质 / Video Formats”菜单可据此切换。DASH 视频及独立音轨优先使用同一品质、编码下由 API 返回的 HTTPS 标准端口备用源，避免部分 PCDN 地址无法连接。

## 更新与使用

1. 更新完整 MPV 包中的 `portable_config/scripts/quality-menu.lua`、`portable_config/scripts/uosc_danmaku/modules/live.lua` 和 Bilibili 中继 EXE。
2. 在 Tampermonkey／Violentmonkey 中导入新版 `external_player.user.js`，禁用重复旧脚本，完整刷新网页。
3. 从网页重新点击 MPV 按钮启动播放器。已运行的播放器仍使用启动时载入的旧脚本。
4. 点击“流品质”，选择实际返回的品质档位。加载成功后菜单才更新当前档位；切换失败会显示原因，可刷新网页取得新地址后重新交接。

默认选择当前账号可用的最高品质；用户明确指定较低品质时保留该选择。直播沿用旧配置的“原画”偏好，使用新的直播播放接口取得实际可用源。接口可能在不同编码下返回不同品质，因此会比较服务端返回的实际档位，再选择编码和线路。

Bilibili 视频 DASH 的独立音轨会随品质切换保留。直播切换品质时保留弹幕会话和显示／隐藏选择，清空旧时间轴，丢弃加载期间的弹幕；中继确认新流后允许下一次切换。打开其他文件仍会结束直播弹幕会话。表情继续以消息中的文字名称显示。

## 只有一个档位或无法加载

菜单只显示当前账号实际取得的源；页面展示的付费、登录或不可用档位不会自动获得播放权限。单独一条视频直链也不能恢复未交接的其他品质。

对未提供品质列表的直链，菜单显示当前流。需要 yt-dlp 解析的页面遇到错误或超时，会显示提示，避免一直转圈。地址过期、网络错误或页面权限变化时，刷新网页后重新交接。

## 开发验证

```powershell
node --test tools/telegram-web-mpv-bridge/test_bilibili_live_handoff.mjs tools/telegram-web-mpv-bridge/test_bilibili_stream_quality.mjs
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools\release -p "test_quality_menu_lua.py"
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe tools\release\smoke_test_stream_quality.py --mpv "<完整包路径>\mpv.exe" --output-dir tools\telegram-web-mpv-bridge\build\quality-smoke
```

真实播放器冒烟使用本地 HTTP 视频、独立音轨和模拟直播消息，覆盖 360p → 720p → 1440p → 360p 的实际解码尺寸、菜单、请求头、音轨清理和弹幕连续性；它不代表实际网站联机复测。

2026-10-11 另在真实 Bilibili 直播房间 1017 完成新版浏览器交接：网页与 MPV 均为 1920×1080，菜单显示三档，1080p → 720p → 1080p 两次切换后均收到新的自然弹幕，并保持同一会话。

同日对 Bilibili 点播 BV1ju6TYHEKU 第 5 集复验网页交接参数，网页与 MPV 均为 1920×960，菜单显示四档；切到 640×320 再切回后，独立音轨被继续选用，暂停的 20.0 秒位置保持不变。
