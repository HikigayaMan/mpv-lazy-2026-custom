#!/usr/bin/env python3
"""Check automatic settings persistence using an isolated real Windows MPV.

Reads production menu commands and Lua scripts, but writes only to a unique run
directory inside the repository build directory. No accounts or downloads are
used, and only MPV processes created by this test are stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from smoke_test_bilibili_live import REPO, MpvPipe, require, wait_property


SOURCE = REPO / "portable_config"
STYLE_STATE = "user-data/settings-smoke/style"
SHADER_DIAGNOSTIC = "user-data/settings-smoke/shader-diagnostic"


def menu_entry(text: str, title: str) -> tuple[str, str]:
    for line in text.splitlines():
        if "#!" not in line:
            continue
        binding, label = line.split("#!", 1)
        if title not in label:
            continue
        match = re.match(r"\s*(\S+)\s+(.*?)\s*$", binding)
        if match:
            return match.group(1), match.group(2)
    raise AssertionError("missing production menu entry: " + title)


def disk_json(path: Path) -> dict:
    with contextlib.suppress(OSError, ValueError):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    return {}


async def wait_disk(path: Path, predicate, detail: str, timeout: float = 8) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = disk_json(path)
        if predicate(value):
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("settings were not saved while MPV was running: " + detail)


def is_hflip(filters) -> bool:
    # Some builds expose FFmpeg filter aliases as lavfi with a graph parameter.
    return any(item.get("name") == "hflip" or
               (item.get("name") == "lavfi" and "hflip" in
                str(item.get("params", {}).get("graph", "")))
               for item in filters or [])


def prepare_config(run_dir: Path) -> tuple[Path, Path, dict]:
    config = run_dir / "config"
    config.mkdir()
    for filename in ("mpv.conf", "profiles.conf", "script-opts.conf",
                     "input_uosc.conf", "menu.conf"):
        shutil.copyfile(SOURCE / filename, config / filename)
    (config / "script-opts").mkdir()
    shutil.copyfile(SOURCE / "script-opts/uosc_danmaku.conf",
                    config / "script-opts/uosc_danmaku.conf")
    for name in ("shader", "watch_later", "icc"):
        (config / "_cache" / name).mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE / "fonts", config / "fonts")
    shader = config / "shaders/QCOM/QCOM_SGEDS_ms_RT.glsl"
    shader.parent.mkdir(parents=True)
    shader.write_text("//!HOOK MAIN\n//!BIND HOOKED\n"
                      "vec4 hook() { return HOOKED_tex(HOOKED_pos); }\n", encoding="utf-8")

    text = (config / "input_uosc.conf").read_text(encoding="utf-8")
    controls = {
        "shader": menu_entry(text, "QCOM 主缩放锐化"),
        "brightness": menu_entry(text, "视频 > 均衡器 > 亮度/明度 +"),
        "contrast": menu_entry(text, "视频 > 均衡器 > 对比度 +"),
        "deband": menu_entry(text, "视频 > 切换 去色带状态"),
        "clear_vf": menu_entry(text, "VF 滤镜 > 清空"),
        "clear_shaders": menu_entry(text, "着色器 > 清空"),
    }
    for name, (_key, command) in controls.items():
        require("script-message-to save_global_props remember " in command,
                "production menu is missing persistence notification: " + name)
    # Exercise the production native-vf save/restore path with a low-cost filter.
    # Heavy VapourSynth presets are intentionally not needed by this fixture.
    with (config / "input_uosc.conf").open("a", encoding="utf-8") as file:
        file.write("\nF8 vf set hflip ; script-message-to save_global_props remember vf\n")

    wrapper = run_dir / "uosc_danmaku"
    wrapper.mkdir()
    source_dir = SOURCE / "scripts/uosc_danmaku"
    # Same script name and production modules; callbacks expose effective style
    # options and dispatch the original menu command through MPV's Lua API.
    wrapper.joinpath("main.lua").write_text(
        "package.path = " + json.dumps(source_dir.as_posix() + "/?.lua;") +
        " .. package.path\n" +
        "dofile(" + json.dumps((source_dir / "main.lua").as_posix()) + ")\n" +
        "local function publish_style()\n"
        "    mp.set_property_native('" + STYLE_STATE + "', {\n"
        "        fontsize = tonumber(options.fontsize), opacity = tonumber(options.opacity),\n"
        "        live_fps_mode = options.live_fps_mode\n"
        "    })\nend\n"
        "mp.register_script_message('settings-smoke-read-style', publish_style)\n"
        "mp.register_script_message('settings-smoke-run-command', function(command)\n"
        "    mp.command(command)\nend)\n"
        "local diagnostic_sequence = 0\n"
        "local function publish_shader_diagnostic()\n"
        "    diagnostic_sequence = diagnostic_sequence + 1\n"
        "    local value = mp.get_property_native('glsl-shaders')\n"
        "    local entries = {}\n"
        "    if type(value) == 'table' then\n"
        "        for key, item in pairs(value) do\n"
        "            entries[#entries + 1] = {key = tostring(key), keyType = type(key), value = item}\n"
        "        end\n"
        "    end\n"
        "    mp.set_property_native('" + SHADER_DIAGNOSTIC + "', {\n"
        "        sequence = diagnostic_sequence, nativeType = type(value), value = value, entries = entries,\n"
        "        length = type(value) == 'table' and #value or nil,\n"
        "        rawString = mp.get_property('glsl-shaders'),\n"
        "        optionValue = mp.get_property_native('options/glsl-shaders')\n"
        "    })\nend\n"
        "mp.register_script_message('settings-smoke-read-shaders', publish_shader_diagnostic)\n"
        "publish_style()\npublish_shader_diagnostic()\n", encoding="utf-8")
    return config, wrapper, controls


async def run(args) -> dict:
    require(os.name == "nt", "this smoke requires Windows MPV named pipes")
    mpv, output = args.mpv.resolve(), args.output_dir.resolve()
    require(mpv.is_file(), "MPV executable is missing")
    require(output.is_relative_to(REPO) and "build" in output.relative_to(REPO).parts,
            "output must be inside a build directory within this repository")
    fixture = (args.fixture or REPO / "tools/telegram-web-mpv-bridge/build/"
               "quality-smoke-20261011/fixtures/360.mov").resolve()
    require(fixture.is_file(), "local fixture is missing; pass --fixture or run the quality smoke first")
    require(fixture.is_relative_to(REPO), "fixture must be inside this repository")
    output.mkdir(parents=True, exist_ok=True)
    run_dir = output / ("run-" + uuid.uuid4().hex[:10])
    run_dir.mkdir()
    config, wrapper, controls = prepare_config(run_dir)
    saved_path = config / "saved-props.json"
    style_path = config / "script-opts/uosc_danmaku_style.json"
    owned: list[tuple[subprocess.Popen, MpvPipe]] = []
    samples = []
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(run_dir)
    overrides = ["uosc_danmaku-auto_load=no", "uosc_danmaku-autoload_for_url=no",
                 "uosc_danmaku-autoload_local_danmaku=no", "uosc_danmaku-vf_fps=no",
                 "uosc_danmaku-history_path=~~/_cache/danmaku-history.json",
                 "uosc_danmaku-style_path=~~/script-opts/uosc_danmaku_style.json"]

    async def start(index: int) -> tuple[subprocess.Popen, MpvPipe]:
        ipc = MpvPipe("mpv-settings-smoke-" + uuid.uuid4().hex)
        command = [str(mpv), f"--config-dir={config}", "--load-scripts=no",
                   "--use-filedir-conf=no", "--no-ytdl", "--idle=yes", "--pause=yes",
                   "--terminal=yes", "--ao=null", "--vo=gpu-next", "--force-window=yes",
                   "--autofit=960x540", "--glsl-shaders-clr", "--brightness=0", "--contrast=0",
                   "--input-ipc-server=" + ipc.path,
                   "--script=" + str(SOURCE / "scripts/save_global_props.lua"),
                   "--script=" + str(SOURCE / "scripts/uosc"), "--script=" + str(wrapper),
                   *["--script-opts-append=" + value for value in overrides],
                   f"--log-file={run_dir / ('mpv-' + str(index) + '.log')}",
                   "--msg-level=all=warn", str(fixture)]
        with (run_dir / f"process-{index}.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=run_dir, env=env, stdout=log,
                                       stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        owned.append((process, ipc))
        await wait_property(ipc, "height", 360, timeout=15)
        await wait_property(ipc, STYLE_STATE, lambda value: isinstance(value, dict), timeout=15)
        require(Path(await ipc.command(["get_property", "user-data/save_global_props/path"])) == saved_path,
                "global settings path escaped the isolated config")
        require(Path(await ipc.command(["get_property", "user-data/uosc_danmaku/style-defaults-path"])) == style_path,
                "danmaku style path escaped the isolated config")
        await asyncio.sleep(0.4)  # Allow source metadata profiles and delayed preference restoration.
        return process, ipc

    async def snapshot(ipc: MpvPipe) -> dict:
        values = {name: await ipc.command(["get_property", name])
                  for name in ("brightness", "contrast", "deband", "glsl-shaders", "vf", STYLE_STATE)}
        previous = await ipc.command(["get_property", SHADER_DIAGNOSTIC])
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-read-shaders"])
        values["luaShaderDiagnostic"] = await wait_property(ipc, SHADER_DIAGNOSTIC,
                lambda value: value and value.get("sequence", 0) > previous.get("sequence", 0))
        return values

    async def check_restored(ipc: MpvPipe) -> dict:
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-read-style"])
        values = await snapshot(ipc)
        require(values["brightness"] == 1 and values["contrast"] == 1,
                "picture adjustments did not survive restart")
        require(values["deband"] is False, "automatic bitrate profile overwrote explicit deband=no")
        require(len(values["glsl-shaders"]) == 1 and
                "QCOM_SGEDS_ms_RT.glsl" in values["glsl-shaders"][0], "shader did not survive restart")
        require(is_hflip(values["vf"]), "native video filter did not survive restart")
        require(values[STYLE_STATE] == {"fontsize": 32, "opacity": 0.77, "live_fps_mode": "80"},
                "effective danmaku options were not restored")
        return values

    try:
        first, ipc = await start(1)
        samples.append({"phase": "before-menu-controls", "properties": await snapshot(ipc)})
        duration = await ipc.command(["get_property", "duration"])
        size = await ipc.command(["get_property", "file-size"])
        require(duration and size * 8 / 1024 / duration <= 3000,
                "fixture does not exercise production low-bitrate deband profile")
        require(await ipc.command(["get_property", "deband"]) is True,
                "production automatic deband profile was not active")
        for name in ("shader", "brightness", "contrast"):
            key, _command = controls[name]
            require(key != "#", "smoke requires a production binding: " + name)
            await ipc.command(["keypress", key])
            samples.append({"phase": "after-" + name, "properties": await snapshot(ipc)})
        # This menu-only item has no physical binding. Route its complete
        # command through mp.command, preserving the actual menu semantics.
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-run-command",
                           controls["deband"][1]])
        await ipc.command(["keypress", "F8"])
        for key, value in (("fontsize", "32"), ("opacity", "0.77"), ("live_fps_mode", "80")):
            await ipc.command(["script-message-to", "uosc_danmaku", "setup-danmaku-style", key, value])
        samples.append({"phase": "after-all-menu-controls", "properties": await snapshot(ipc)})
        saved = await wait_disk(saved_path,
                                lambda value: value.get("brightness") == 1 and value.get("contrast") == 1 and
                                value.get("deband") is False and len(value.get("glsl-shaders", [])) == 1 and
                                is_hflip(value.get("vf")), "shader/filter/picture settings")
        await wait_disk(style_path, lambda value: value.get("fontsize") == 32 and
                        value.get("opacity") == 0.77 and value.get("live_fps_mode") == "80", "danmaku style")
        require(first.poll() is None, "first player exited before automatic save")
        samples.append({"phase": "saved-before-exit", "properties": saved})
        # Terminate only the exact Popen child: no shutdown handler can save it.
        first.terminate()
        await asyncio.to_thread(first.wait, 5)

        second, ipc = await start(2)
        require(await ipc.command(["get_property", "user-data/uosc_danmaku/style-defaults-status"]) == "loaded",
                "new player did not load persisted danmaku style")
        values = await check_restored(ipc)
        samples.append({"phase": "restored-after-forced-termination", "properties": values})
        await ipc.command(["loadfile", str(fixture), "replace"])
        await wait_property(ipc, "height", 360)
        await asyncio.sleep(0.5)
        await check_restored(ipc)
        await ipc.command(["script-message-to", "uosc_danmaku", "open_danmaku_style_menu"])
        await wait_property(ipc, "user-data/uosc/menu/type", "menu_style")
        await ipc.command(["screenshot-to-file", str(run_dir / "restored-danmaku-style.png"), "window"])
        await ipc.command(["keypress", "ESC"])
        for name in ("clear_vf", "clear_shaders"):
            await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-run-command",
                               controls[name][1]])
        await wait_disk(saved_path, lambda value: value.get("vf") == [] and
                        value.get("glsl-shaders") == [], "explicitly cleared filter and shader lists")
        await ipc.command(["quit"])
        await asyncio.to_thread(second.wait, 5)

        third, ipc = await start(3)
        require(await ipc.command(["get_property", "vf"]) == [], "cleared filters returned after restart")
        require(await ipc.command(["get_property", "glsl-shaders"]) == [], "cleared shaders returned after restart")
        require(await ipc.command(["get_property", "deband"]) is False, "explicit deband choice was lost")
        await ipc.command(["screenshot-to-file", str(run_dir / "cleared-filters-restored.png"), "window"])
        await ipc.command(["quit"])
        await asyncio.to_thread(third.wait, 5)
        for log_path in run_dir.glob("mpv-*.log"):
            log = log_path.read_text(encoding="utf-8", errors="replace").lower()
            require(not any(error in log for error in ("lua error", "stack traceback", "script failed", "error parsing option", "error converting value")),
                    "real player reported a Lua/option failure: " + log_path.name)
        report = {"passed": True, "network": "none; existing local video fixture",
                  "automaticSaveBeforeExit": True, "restoredAfterForcedTermination": True,
                  "fileReloadKeepsExplicitDeband": True, "effectiveDanmakuStyleRestored": True,
                  "clearedFilterAndShaderListsRestored": True, "samples": samples,
                  "screenshots": [str(run_dir / "restored-danmaku-style.png"),
                                  str(run_dir / "cleared-filters-restored.png")],
                  "runDirectory": str(run_dir)}
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    except Exception as error:
        failure = {"passed": False, "error": str(error), "samples": samples,
                   "savedDisk": disk_json(saved_path), "styleDisk": disk_json(style_path)}
        if owned and owned[-1][0].poll() is None:
            with contextlib.suppress(Exception):
                failure["nativePropertiesBeforeCleanup"] = await snapshot(owned[-1][1])
        (run_dir / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        for process, ipc in owned:
            if process.poll() is not None:
                continue
            with contextlib.suppress(Exception):
                await ipc.command(["quit"])
                await asyncio.to_thread(process.wait, 3)
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
