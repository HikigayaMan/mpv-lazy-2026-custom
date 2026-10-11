# 右键设置自动保存

右键菜单中的以下选择现在会自动保存，重新启动 MPV 后恢复：

- 视频滤镜：补帧、超分、RTX HDR，以及清空滤镜。
- 着色器：单个着色器开关、组合预设，以及清空着色器。
- 画面：解码模式、去色带、去隔行、自动校色、时间码解析、亮度、对比度、伽马、饱和度和色相。
- 弹幕：粗体、字号、描边、阴影、速度、不透明度、显示区域和弹幕帧率。

音量和静音也继续自动保存。全局画面设置在短暂合并写入后保存，弹幕样式在有效修改后立即保存；不需要先退出播放器或另点“保存当前样式”。清空滤镜或着色器也是一个会保存的选择。

普通视频和直播使用同一套弹幕样式。普通视频修改后会用已经加载的弹幕重建排布并刷新显示，暂停时也会生效；不需要重新获取弹幕或重新打开视频。显示区域、滚动速度及粗体选择会同步应用到生成的弹幕字幕，合并重复弹幕的数量标记不会强制打开粗体。菜单中的“弹幕帧率”也适用于普通视频；原有 `live_fps_mode` 保存键保持兼容。

更新后，关闭旧播放器并重新打开，以加载新的脚本和菜单。已经运行的旧播放器仍使用旧逻辑。保存文件是本机运行时数据，不包含在更新包中，更新不会覆盖已有偏好：

```text
portable_config/saved-props.json
portable_config/script-opts/uosc_danmaku_style.json
```

`工具 > 清除已记录的属性值` 清除全局画面及音量等记录；弹幕设置中的“清除已保存样式”清除弹幕样式记录。清除后本次播放画面保持现状，下次启动使用配置文件的默认值。之后新的菜单修改会重新保存。

保存失败时播放器显示提示，弹幕菜单会标记失败。请确认配置目录可写，再修改或重试保存。

## 保存边界

菜单和对应快捷键会明确通知保存层本次改了哪些属性。自动 SDR/HDR 或低码率去色带预设不会被当作用户选择写入；已明确保存的画面选择在文件加载后恢复。弹幕自动生成的 `@danmaku` 和 `@danmaku_smooth` 滤镜不写入全局视频滤镜记录，防止它们在下一次启动重复挂载。

播放位置、当前视频直链、流品质地址、暂停和轨道选择不属于这份全局画面偏好。播放位置和文件轨道继续由原有 watch-later 处理。RIFE 参数仍保存在各自的运行时配置中，视频滤镜记录负责恢复其启用状态。

## 开发验证

```powershell
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools/release -p "test_save_global_props_lua.py"
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe -m unittest discover -s tools/release -p "test_danmaku_style_lua.py"
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe tools/release/smoke_test_settings_persistence.py --mpv "<完整包路径>\mpv.exe" --output-dir tools/telegram-web-mpv-bridge/build/settings-smoke
.\tools\telegram-web-mpv-bridge\.venv\Scripts\python.exe tools/release/smoke_test_static_danmaku_settings.py --mpv "<完整包路径>\mpv.exe" --output-dir tools/telegram-web-mpv-bridge/build/static-settings-smoke
```

行为测试覆盖跨实例恢复、空列表和布尔值、多播放器写入合并、自动属性不误保存、清除、写入失败和 Windows 文件替换回滚。真实 MPV 冒烟使用隔离配置和测试自身创建的播放器，验证设置写入后非正常关闭仍可恢复，不改动日常偏好文件。
