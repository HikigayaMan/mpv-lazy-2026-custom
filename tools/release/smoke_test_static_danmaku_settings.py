#!/usr/bin/env python3
"""Check saved settings against ordinary-video ASS output in real Windows MPV.

Two generated local MOV files (320x180 and 512x288) and same-name XML sources exercise the complete
uosc_danmaku loader, parser, menu handler and ASS subtitle renderer. The second
MOV has trailing padding so the production file-size bitrate profile changes
condition without a high-cost video encoder. All files and preferences remain
inside a unique repository build directory; only owned child players are closed.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid

from smoke_test_bilibili_live import REPO, MpvPipe, require, wait_property
from smoke_test_settings_persistence import SOURCE, disk_json, prepare_config, wait_disk


STATIC_STATE = "user-data/settings-smoke/static-danmaku"
PROFILE_TRACE = "user-data/settings-smoke/profile-trace"
STYLE_KEYS = ("fontsize", "outline", "shadow", "opacity", "bold", "scrolltime", "displayarea")
CHOSEN_STYLE = {"fontsize": 36, "outline": 3.5, "shadow": 2,
                "opacity": 0.31, "bold": False, "scrolltime": 7, "displayarea": 0.25,
                "live_fps_mode": "120"}


def mov_duration(target: Path) -> float:
    """Read the encoded MOV movie-header duration without another player."""
    with target.open("rb") as file:
        def scan(end: int):
            while file.tell() + 8 <= end:
                start = file.tell()
                header = file.read(8)
                size, kind = struct.unpack(">I4s", header)
                header_size = 8
                if size == 1:
                    large_size = file.read(8)
                    require(len(large_size) == 8, "truncated generated MOV large atom")
                    size = struct.unpack(">Q", large_size)[0]
                    header_size = 16
                if size == 0:
                    size = end - start
                require(size >= header_size and start + size <= end,
                        "invalid generated MOV atom size")
                if kind == b"moov":
                    duration = scan(start + size)
                    if duration is not None:
                        return duration
                elif kind == b"mvhd":
                    content = file.read(min(size - header_size, 32))
                    require(len(content) >= 20, "truncated generated MOV movie header")
                    if content[0] == 0:
                        timescale, duration = struct.unpack(">II", content[12:20])
                    else:
                        require(content[0] == 1 and len(content) >= 32,
                                "unsupported generated MOV movie header")
                        timescale = struct.unpack(">I", content[20:24])[0]
                        duration = struct.unpack(">Q", content[24:32])[0]
                    require(timescale > 0, "generated MOV has no valid timescale")
                    return duration / timescale
                file.seek(start + size)
            return None
        duration = scan(target.stat().st_size)
    require(duration is not None and duration >= 60, "generated MOV is too short for ordinary danmaku loading")
    return duration


def video_metadata(target: Path, *, padded: bool) -> dict:
    duration = mov_duration(target)
    size = target.stat().st_size
    profile_kbps = size * 8 / 1024 / duration
    require(profile_kbps > 3000 if padded else profile_kbps <= 3000,
            "generated fixture has the wrong production bitrate-profile condition: " +
            f"{target.name}; bytes={size}, duration={duration:.3f}, profileKbps={profile_kbps:.2f}")
    return {"path": str(target), "bytes": size, "duration": duration,
            "profileKbps": profile_kbps, "lowBitrateProfileExpected": not padded,
            "pngCompressionLevel": 9, "trailingPaddingBytes": 40 * 1024 * 1024 if padded else 0}


def make_video(mpv: Path, target: Path, width: int, height: int, color: str,
               *, padded: bool = False) -> dict:
    result = subprocess.run([
        str(mpv), "--no-config", "--terminal=yes", "--force-window=no",
        "--audio=no", "--untimed", "--ovc=png", "--ovcopts=compression_level=9,pred=mixed",
        "--of=mov", "--frames=2160",
        f"--o={target}", f"av://lavfi:color=c={color}:size={width}x{height}:rate=24",
    ], capture_output=True, encoding="utf-8", errors="replace", timeout=120,
       creationflags=subprocess.CREATE_NO_WINDOW)
    require(result.returncode == 0 and target.is_file(),
            "local MOV encoder failed: " + result.stdout[-1200:] + result.stderr[-1200:])
    if padded:
        # Existing production profile uses actual file-size/duration. Trailing
        # MOV padding raises that condition while decoded frames stay simple.
        with target.open("ab") as file:
            block = bytes(1024 * 1024)
            for _ in range(40):
                file.write(block)
    return video_metadata(target, padded=padded)


def copy_fixtures(source_dir: Path, low: Path, high: Path) -> list[dict]:
    source_dir = source_dir.resolve()
    require(source_dir.is_dir() and source_dir.is_relative_to(REPO) and
            "build" in source_dir.relative_to(REPO).parts,
            "reused fixtures must be inside a build directory within this repository")
    metadata = []
    for target, padded in ((low, False), (high, True)):
        source = (source_dir / target.name).resolve()
        require(source.is_file() and source.is_relative_to(REPO) and
                "build" in source.relative_to(REPO).parts,
                "reused fixture is missing or escaped the repository build directory: " + target.name)
        # Validate the original before copying; each run still gets its own
        # fixtures and newly written XML sources, with no shared preferences.
        video_metadata(source, padded=padded)
        shutil.copyfile(source, target)
        values = video_metadata(target, padded=padded)
        values["reusedFrom"] = str(source)
        metadata.append(values)
    return metadata


def make_xml(target: Path, marker: str) -> None:
    records = []
    for index in range(120):
        records.append(f'<d p="4,1,25,1193046,0,0,fixture,{index}">'
                       f'STATIC_ROLL_{marker}_{index:03d}</d>')
    records.extend([
        f'<d p="4,5,25,16777215,0,0,fixture,201">STATIC_TOP_{marker}</d>',
        f'<d p="4,4,25,16777215,0,0,fixture,202">STATIC_BOTTOM_{marker}</d>',
    ])
    target.write_text('<?xml version="1.0" encoding="UTF-8"?><i>\n' +
                      "\n".join(records) + "\n</i>\n", encoding="utf-8")


def ass_seconds(value: str) -> float:
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_ass(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    section, styles, events, formats = "", {}, [], {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif line.startswith("Format:"):
            formats[section] = [field.strip() for field in line.split(":", 1)[1].split(",")]
        elif line.startswith("Style:"):
            fields = [field.strip() for field in line.split(":", 1)[1].split(",")]
            style = dict(zip(formats.get(section, []), fields))
            styles[style.get("Name", "")] = style
        elif line.startswith("Dialogue:"):
            fields = line.split(":", 1)[1].strip().split(",", 9)
            event = dict(zip(formats.get(section, []), fields))
            if "Start" in event and "End" in event:
                event["seconds"] = ass_seconds(event["End"]) - ass_seconds(event["Start"])
                move = re.search(r"\\move\(([^)]+)\)", event.get("Text", ""))
                pos = re.search(r"\\pos\(([^)]+)\)", event.get("Text", ""))
                if move:
                    event["move"] = [float(part.strip()) for part in move.group(1).split(",")]
                    event["y"] = event["move"][1]
                elif pos:
                    event["pos"] = [float(part.strip()) for part in pos.group(1).split(",")]
                    event["y"] = event["pos"][1]
            events.append(event)
    return {"path": str(path), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "styles": styles, "events": events, "text": text}


def check_style(ass: dict, expected: dict, *, marker: str | None = None) -> None:
    require(all(name in ass["styles"] for name in ("R2L", "TOP", "BTM")),
            "generated ASS is missing ordinary-video styles")
    for name in ("R2L", "TOP", "BTM"):
        style = ass["styles"][name]
        for key, field in (("fontsize", "Fontsize"), ("outline", "Outline"), ("shadow", "Shadow")):
            if key in expected:
                require(math.isclose(float(style[field]), expected[key]),
                        f"ASS {name}.{field} does not reflect changed {key}")
        if "bold" in expected:
            require(int(style["Bold"]) == (-1 if expected["bold"] else 0),
                    f"ASS {name}.Bold does not reflect changed bold")
        if "opacity" in expected:
            alpha = math.floor((1 - expected["opacity"]) * 255 + 0.5)
            require(style["PrimaryColour"].upper() == f"&H{alpha:02X}FFFFFF" and
                    style["SecondaryColour"].upper() == f"&H{alpha:02X}FFFFFF",
                    f"ASS {name} color alpha does not reflect changed opacity")
    roll = [event for event in ass["events"] if event.get("Style") == "R2L"]
    require(roll, "generated ASS has no rolling local XML comments")
    require(all("\\c&H563412&" in event.get("Text", "") for event in roll),
            "local XML RGB colors were not preserved as ASS BGR colors")
    if "scrolltime" in expected:
        require(all(math.isclose(event["seconds"], expected["scrolltime"]) for event in roll),
                "ASS dialogue duration does not reflect changed scrolltime")
        require(all(len(event.get("move", [])) == 6 and
                    event["move"][5] == expected["scrolltime"] * 1000 for event in roll),
                "ASS movement duration does not reflect changed scrolltime")
    if "displayarea" in expected:
        limit = 1080 * expected["displayarea"]
        require(all("y" in event and 0 <= event["y"] <= limit for event in ass["events"]),
                "ASS event positions exceed changed displayarea")
    if marker:
        require(any(f"STATIC_ROLL_{marker}_" in event.get("Text", "") for event in roll),
                "ASS contains an old video's XML source instead of the new source")
        require(any(f"STATIC_BOTTOM_{marker}" in event.get("Text", "") for event in ass["events"]),
                "bottom fixed comment disappeared from local XML fixture")


def summary(ass: dict) -> dict:
    return {"path": ass["path"], "sha256": ass["sha256"], "styles": ass["styles"],
            "eventCount": len(ass["events"]),
            "rollingCount": sum(event.get("Style") == "R2L" for event in ass["events"]),
            "maxY": max((event.get("y", 0) for event in ass["events"]), default=None),
            "sampleEvents": ass["events"][:2] + ass["events"][-2:]}


def has_smooth_fps(filters, target: int | None = None) -> bool:
    for filter_ in filters or []:
        if filter_.get("label") != "danmaku_smooth":
            continue
        if target is None:
            return True
        with contextlib.suppress(ValueError, TypeError, ZeroDivisionError):
            if Fraction(str(filter_.get("params", {}).get("fps"))) == target:
                return True
    return False


def append_diagnostics(wrapper: Path) -> None:
    # Read-only visibility into the loaded full script and timed profile state.
    # Nothing here converts comments, renders ASS, or applies preferences.
    with (wrapper / "main.lua").open("a", encoding="utf-8") as file:
        file.write(
            "local static_sequence, profile_sequence = 0, 0\n"
            "local profile_trace = {}\n"
            "local function publish_static()\n"
            "    static_sequence = static_sequence + 1\n"
            "    local values, sources = {}, 0\n"
            "    for _, key in ipairs({'fontsize','outline','shadow','opacity','bold','scrolltime','displayarea','live_fps_mode'}) do\n"
            "        values[key] = options[key]\n"
            "    end\n"
            "    for _ in pairs(DANMAKU.sources or {}) do sources = sources + 1 end\n"
            f"    mp.set_property_native('{STATIC_STATE}', {{sequence = static_sequence, values = values,\n"
            "        enabled = ENABLED, commentCount = COMMENTS and #COMMENTS or 0, sourceCount = sources})\n"
            "end\n"
            "mp.register_script_message('settings-smoke-read-static', publish_static)\n"
            "publish_static()\n"
            "mp.register_event('file-loaded', function()\n"
            "    profile_sequence = profile_sequence + 1\n"
            "    local generation = profile_sequence\n"
            "    local path = mp.get_property('path')\n"
            "    for _, delay in ipairs({0.1, 0.4, 1.0}) do\n"
            "        local elapsed = delay\n"
            "        mp.add_timeout(delay, function()\n"
            "            profile_trace[#profile_trace + 1] = {generation = generation, elapsed = elapsed,\n"
            "                path = path, currentPath = mp.get_property('path'),\n"
            "                deband = mp.get_property_native('deband'),\n"
            "                brightness = mp.get_property_native('brightness'),\n"
            "                contrast = mp.get_property_native('contrast'),\n"
            "                width = mp.get_property_number('width'), height = mp.get_property_number('height'),\n"
            "                fileSize = mp.get_property_number('file-size'), duration = mp.get_property_number('duration')}\n"
            f"            mp.set_property_native('{PROFILE_TRACE}', profile_trace)\n"
            "        end)\n"
            "    end\n"
            "end)\n")


async def run(args) -> dict:
    require(os.name == "nt", "this smoke requires Windows MPV named pipes")
    mpv, output = args.mpv.resolve(), args.output_dir.resolve()
    require(mpv.is_file(), "MPV executable is missing")
    require(output.is_relative_to(REPO) and "build" in output.relative_to(REPO).parts,
            "output must be inside a build directory within this repository")
    output.mkdir(parents=True, exist_ok=True)
    run_dir = output / ("run-" + uuid.uuid4().hex[:10])
    run_dir.mkdir()
    config, wrapper, controls = prepare_config(run_dir)
    append_diagnostics(wrapper)
    fixtures = run_dir / "fixtures"
    fixtures.mkdir()
    low, high = fixtures / "ordinary-low.mov", fixtures / "ordinary-high.mov"
    if args.fixture_dir:
        fixture_metadata = await asyncio.to_thread(copy_fixtures, args.fixture_dir, low, high)
    else:
        fixture_metadata = [await asyncio.to_thread(make_video, mpv, low, 320, 180, "0x24364A"),
                            await asyncio.to_thread(make_video, mpv, high, 512, 288, "0x314723", padded=True)]
    (run_dir / "fixture-metadata.json").write_text(
        json.dumps(fixture_metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    make_xml(low.with_suffix(".xml"), "A")
    make_xml(high.with_suffix(".xml"), "B")
    primary = fixtures / "primary-smoke.srt"
    primary.write_text("1\n00:00:00,000 --> 00:01:30,000\nPRIMARY_SUBTITLE_SMOKE\n", encoding="utf-8")
    saved_path = config / "saved-props.json"
    style_path = config / "script-opts/uosc_danmaku_style.json"
    history_path = config / "_cache/danmaku-history.json"
    history_path.write_text('{"show_danmaku":true}\n', encoding="utf-8")
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(run_dir)
    overrides = ["uosc_danmaku-auto_load=no", "uosc_danmaku-autoload_for_url=no",
                 "uosc_danmaku-autoload_local_danmaku=yes", "uosc_danmaku-vf_fps=yes",
                 "uosc_danmaku-render_mode=ass", "uosc_danmaku-displayarea=0.85",
                 "uosc_danmaku-merge_tolerance=-1", "uosc_danmaku-max_screen_danmaku=0",
                 "uosc_danmaku-history_path=~~/_cache/danmaku-history.json",
                 "uosc_danmaku-style_path=~~/script-opts/uosc_danmaku_style.json"]
    owned: list[tuple[subprocess.Popen, MpvPipe]] = []
    samples, profile_traces = [], []

    async def read_static(ipc: MpvPipe) -> dict:
        previous = await ipc.command(["get_property", STATIC_STATE]) or {}
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-read-static"])
        return await wait_property(ipc, STATIC_STATE,
            lambda value: isinstance(value, dict) and value.get("sequence", 0) > previous.get("sequence", 0))

    async def close_style_menu(ipc: MpvPipe) -> None:
        await ipc.command(["script-message-to", "uosc", "close-menu", "menu_style"])
        deadline = asyncio.get_running_loop().time() + 8
        while asyncio.get_running_loop().time() < deadline:
            try:
                value = await ipc.command(["get_property", "user-data/uosc/menu/type"])
            except RuntimeError as error:
                # MPV versions expose a cleared native user-data node as either
                # JSON null or an unavailable property. Both mean no menu.
                if "property unavailable" in str(error) or "property not found" in str(error):
                    return
                raise
            if value is None or value == "":
                return
            await asyncio.sleep(0.05)
        raise AssertionError("style menu stayed open and would intercept picture-control keys")

    async def wait_ass(ipc: MpvPipe, predicate, detail: str, *, timeout: float = 8) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        last, last_error = None, None
        while asyncio.get_running_loop().time() < deadline:
            tracks = await ipc.command(["get_property", "track-list"]) or []
            for track in tracks:
                filename = track.get("external-filename", "")
                if track.get("type") != "sub" or not Path(filename).name.startswith("uosc-danmaku-"):
                    continue
                path = Path(filename).resolve()
                require(path.is_relative_to(run_dir), "ASS output escaped the isolated run")
                with contextlib.suppress(OSError, ValueError, KeyError):
                    last = parse_ass(path)
                    try:
                        predicate(last)
                    except AssertionError as error:
                        last_error = str(error)
                        continue
                    secondary = await ipc.command(["get_property", "secondary-sid"])
                    require(str(secondary) == str(track["id"]), "generated ASS is not selected as secondary subtitles")
                    require(await ipc.command(["get_property", "secondary-sub-visibility"]) is True,
                            "generated ASS subtitles are hidden")
                    return last
            await asyncio.sleep(0.05)
        if last is not None:
            samples.append({"phase": "failed-ass-" + detail, "ass": summary(last)})
        raise AssertionError("ASS output did not update: " + detail + (": " + last_error if last_error else ""))

    async def profile_samples(ipc: MpvPipe, phase: str, *, expected_deband=None) -> list:
        trace = await wait_property(ipc, PROFILE_TRACE,
            lambda value: isinstance(value, list) and value and value[-1].get("elapsed") == 1.0)
        generation = trace[-1]["generation"]
        current = [item for item in trace if item["generation"] == generation]
        require([item["elapsed"] for item in current] == [0.1, 0.4, 1.0],
                "file-loaded trace did not capture all three profile settling samples")
        profile_traces.append({"phase": phase, "samples": current})
        if expected_deband is not None:
            require(current[-1]["deband"] is expected_deband,
                    "ordinary-video profile overwrote the saved deband choice: " + phase)
        return current

    async def start(index: int, video: Path, width: int, height: int) -> tuple[subprocess.Popen, MpvPipe]:
        ipc = MpvPipe("mpv-static-settings-smoke-" + uuid.uuid4().hex)
        command = [str(mpv), f"--config-dir={config}", "--load-scripts=no",
                   "--use-filedir-conf=no", "--no-ytdl", "--idle=yes", "--pause=yes",
                   "--terminal=yes", "--ao=null", "--vo=gpu-next", "--force-window=yes",
                   "--autofit=960x540", "--glsl-shaders-clr", "--brightness=0", "--contrast=0",
                   "--input-ipc-server=" + ipc.path,
                   "--script=" + str(SOURCE / "scripts/save_global_props.lua"),
                   "--script=" + str(SOURCE / "scripts/uosc"), "--script=" + str(wrapper),
                   *["--script-opts-append=" + value for value in overrides],
                   f"--log-file={run_dir / ('mpv-' + str(index) + '.log')}",
                   "--msg-level=all=warn", str(video)]
        with (run_dir / f"process-{index}.log").open("wb") as log:
            process = subprocess.Popen(command, cwd=run_dir, env=env, stdout=log,
                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        owned.append((process, ipc))
        await wait_property(ipc, "width", width, timeout=15)
        await wait_property(ipc, "height", height, timeout=15)
        await wait_property(ipc, "user-data/uosc_danmaku/has-danmaku", True, timeout=15)
        require(Path(await ipc.command(["get_property", "user-data/save_global_props/path"])) == saved_path,
                "global preferences path escaped the isolated config")
        require(Path(await ipc.command(["get_property", "user-data/uosc_danmaku/style-defaults-path"])) == style_path,
                "style preferences path escaped the isolated config")
        return process, ipc

    async def check_picture(ipc: MpvPipe, phase: str) -> None:
        trace = await profile_samples(ipc, phase, expected_deband=False)
        require(trace[-1]["brightness"] == 1 and trace[-1]["contrast"] == 1,
                "saved picture controls did not survive ordinary-video profile switching: " + phase)
        await wait_property(ipc, "vf", lambda filters: has_smooth_fps(filters, 120))

    async def load_video(ipc: MpvPipe, video: Path, width: int, height: int, marker: str, phase: str) -> dict:
        previous = await ipc.command(["get_property", PROFILE_TRACE]) or []
        generation = previous[-1]["generation"] if previous else 0
        await ipc.command(["loadfile", str(video), "replace"])
        await wait_property(ipc, "width", width)
        await wait_property(ipc, "height", height)
        await wait_property(ipc, PROFILE_TRACE,
            lambda value: isinstance(value, list) and value and value[-1].get("generation", 0) > generation and
                          value[-1].get("elapsed") == 1.0)
        ass = await wait_ass(ipc, lambda value: check_style(value, CHOSEN_STYLE, marker=marker), phase)
        samples.append({"phase": phase, "ass": summary(ass), "effectiveStyle": await read_static(ipc)})
        await check_picture(ipc, phase)
        return ass

    try:
        first, ipc = await start(1, low, 320, 180)
        initial_profile = await profile_samples(ipc, "initial-low-bitrate", expected_deband=True)
        require(initial_profile[-1]["fileSize"] * 8 / 1024 / initial_profile[-1]["duration"] <= 3000,
                "first ordinary-video fixture did not enter production deband_bitrate profile")
        baseline = await wait_ass(ipc, lambda value: check_style(value, {}, marker="A"), "initial-local-xml")
        samples.append({"phase": "initial-local-xml", "ass": summary(baseline),
                        "effectiveStyle": await read_static(ipc)})
        await ipc.command(["sub-add", str(primary), "select", "primary-smoke"])
        primary_sid = await ipc.command(["get_property", "sid"])
        expected, before_region = {}, None
        for key in STYLE_KEYS:
            before = await wait_ass(ipc, lambda value: check_style(value, expected, marker="A"), "before-" + key)
            if key == "displayarea":
                before_region = summary(before)
            if key == "bold":
                await ipc.command(["script-message-to", "uosc_danmaku", "setup-danmaku-style",
                                   json.dumps({"type": "activate", "index": 1})])
            else:
                await ipc.command(["script-message-to", "uosc_danmaku", "setup-danmaku-style",
                                   key, str(CHOSEN_STYLE[key])])
            expected[key] = CHOSEN_STYLE[key]
            ass = await wait_ass(ipc, lambda value: check_style(value, expected, marker="A"), "after-" + key)
            require(ass["sha256"] != before["sha256"], "menu change did not rewrite ASS: " + key)
            require(await ipc.command(["get_property", "sid"]) == primary_sid,
                    "ordinary-video ASS refresh displaced the user's primary subtitle: " + key)
            require(await ipc.command(["get_property", "pause"]) is True,
                    "style change unexpectedly resumed a paused ordinary video")
            samples.append({"phase": "after-" + key, "ass": summary(ass),
                            "effectiveStyle": await read_static(ipc)})
        require(before_region and summary(ass)["rollingCount"] < before_region["rollingCount"] and
                summary(ass)["maxY"] < before_region["maxY"],
                "displayarea changed options but did not rebuild static comment lane allocation")
        for fps in (80, 120):
            await ipc.command(["script-message-to", "uosc_danmaku", "setup-danmaku-style",
                               "live_fps_mode", str(fps)])
            filters = await wait_property(ipc, "vf", lambda value: has_smooth_fps(value, fps))
            require(await ipc.command(["get_property", "pause"]) is True,
                    "ordinary-video frame-rate change resumed a paused player")
            samples.append({"phase": "ordinary-video-fps-" + str(fps), "vf": filters,
                            "effectiveStyle": await read_static(ipc)})
        # A user VF must suppress the smoothing filter, and clearing that VF
        # must bring back the selected frame rate without saving temporary VF.
        await ipc.command(["keypress", "F8"])
        await wait_property(ipc, "vf", lambda filters: any(item.get("name") == "hflip" for item in filters)
                            and not has_smooth_fps(filters))
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-run-command", controls["clear_vf"][1]])
        await wait_property(ipc, "vf", lambda filters: has_smooth_fps(filters, 120))
        await wait_disk(saved_path, lambda value: value.get("vf") == [],
                        "user VF cleared without persisting temporary danmaku fps")
        await wait_disk(style_path, lambda value: all(value.get(k) == v for k, v in CHOSEN_STYLE.items()),
                        "ordinary-video style values")
        await close_style_menu(ipc)
        for name in ("brightness", "contrast"):
            await ipc.command(["keypress", controls[name][0]])
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-run-command", controls["deband"][1]])
        await wait_disk(saved_path, lambda value: value.get("brightness") == 1 and
                        value.get("contrast") == 1 and value.get("deband") is False,
                        "ordinary-video picture controls")
        await ipc.command(["seek", "4.5", "absolute+exact"])
        await ipc.command(["keypress", "ESC"])
        await ipc.command(["screenshot-to-file", str(run_dir / "changed-static-style.png"), "window"])
        await load_video(ipc, high, 512, 288, "B", "changed-to-high-bitrate-video")
        high_profile = profile_traces[-1]["samples"][-1]
        require(high_profile["fileSize"] * 8 / 1024 / high_profile["duration"] > 3000,
                "second ordinary-video fixture did not leave production deband_bitrate profile")
        await load_video(ipc, low, 320, 180, "A", "returned-to-low-bitrate-video")
        require(first.poll() is None, "player exited before saved ordinary-video settings were verified")
        first.terminate()
        await asyncio.to_thread(first.wait, 5)

        second, ipc = await start(2, high, 512, 288)
        restored = await wait_ass(ipc, lambda value: check_style(value, CHOSEN_STYLE, marker="B"),
                                  "restarted-on-another-ordinary-video")
        await check_picture(ipc, "restarted-high-bitrate-video")
        samples.append({"phase": "restored-after-forced-termination", "ass": summary(restored),
                        "effectiveStyle": await read_static(ipc)})
        await load_video(ipc, low, 320, 180, "A", "restarted-player-low-bitrate-video")
        await ipc.command(["seek", "4.5", "absolute+exact"])
        await ipc.command(["screenshot-to-file", str(run_dir / "restored-static-style.png"), "window"])
        await ipc.command(["quit"])
        await asyncio.to_thread(second.wait, 5)
        for log_path in run_dir.glob("mpv-*.log"):
            log = log_path.read_text(encoding="utf-8", errors="replace").lower()
            require(not any(error in log for error in ("lua error", "stack traceback", "script failed",
                        "error parsing option", "error converting value")),
                    "real ordinary-video player reported a Lua/option failure: " + log_path.name)
        report = {"passed": True, "network": "none; two generated local videos and XML sources",
                  "fixtureMetadata": fixture_metadata,
                  "ordinaryVideoXmlLoaded": True, "allSevenStyleChangesRewriteASS": True,
                  "staticLayoutRebuiltForDisplayArea": True, "primarySubtitlePreserved": True,
                  "ordinaryVideoFrameRate80And120Verified": True,
                  "frameRateRestoredAcrossVideosAndRestart": True, "userFilterSuppressesSmoothing": True,
                  "pausedUpdatesVerified": True, "styleRestoredAcrossDifferentVideosAndRestart": True,
                  "pictureRestoredAcrossRealBitrateProfileSwitches": True,
                  "profileSamplesSeconds": [0.1, 0.4, 1.0], "profileTraces": profile_traces,
                  "samples": samples, "styleDisk": disk_json(style_path), "savedDisk": disk_json(saved_path),
                  "runDirectory": str(run_dir),
                  "screenshots": [str(run_dir / "changed-static-style.png"),
                                  str(run_dir / "restored-static-style.png")]}
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    except Exception as error:
        failure = {"passed": False, "error": str(error), "runDirectory": str(run_dir),
                   "fixtureMetadata": fixture_metadata,
                   "samples": samples, "profileTraces": profile_traces,
                   "savedDisk": disk_json(saved_path), "styleDisk": disk_json(style_path)}
        if owned and owned[-1][0].poll() is None:
            with contextlib.suppress(Exception):
                failure["latestProfileTrace"] = await owned[-1][1].command(["get_property", PROFILE_TRACE])
                failure["effectiveStyleBeforeCleanup"] = await read_static(owned[-1][1])
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
    parser.add_argument("--fixture-dir", type=Path,
                        help="Reuse ordinary-low.mov and ordinary-high.mov from a repository build directory")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    print(json.dumps({key: value for key, value in report.items()
                      if isinstance(value, bool) or key in ('network', 'runDirectory', 'screenshots')},
                     ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
