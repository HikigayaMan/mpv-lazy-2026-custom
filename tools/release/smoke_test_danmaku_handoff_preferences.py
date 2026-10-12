#!/usr/bin/env python3
"""Check shared live/video danmaku preferences in isolated real Windows MPV.

No visibility history is prefilled. Production menu handlers make every user
choice. Local XML autoload is explicitly enabled only for this offline fixture;
this does not claim coverage of a website's CID lookup, USH, or instance reuse.
Fullscreen checks cover new-process CLI options only, including an appended
--fullscreen=yes overriding an earlier --fullscreen=no. Only owned players stop.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from smoke_test_bilibili_live import (
    REPO, BilibiliSession, MpvPipe, OfflineConnector, OfflineOwner, OfflineRemote,
    chat_frame, eventually, has_danmaku_track, require, validate_auth, wait_property,
)
from smoke_test_settings_persistence import SOURCE, disk_json, prepare_config, wait_disk
from smoke_test_static_danmaku_settings import (
    CHOSEN_STYLE, STATIC_STATE, append_diagnostics, check_style, has_smooth_fps,
    make_xml, mov_duration, parse_ass, summary,
)


def check_header(ass: dict, expected: dict) -> None:
    for name in ("R2L", "TOP", "BTM"):
        require(name in ass["styles"], "rendered ASS has no shared " + name + " style")
        style = ass["styles"][name]
        for key, field in (("fontsize", "Fontsize"), ("outline", "Outline"), ("shadow", "Shadow")):
            require(math.isclose(float(style[field]), expected[key]), "shared ASS field differs: " + field)
        alpha = math.floor((1 - expected["opacity"]) * 255 + 0.5)
        require(style["PrimaryColour"].upper() == f"&H{alpha:02X}FFFFFF" and
                style["SecondaryColour"].upper() == f"&H{alpha:02X}FFFFFF", "shared ASS opacity differs")
        require(int(style["Bold"]) == (-1 if expected["bold"] else 0), "shared ASS bold differs")
    require(ass["events"], "visible preferences produced an empty ASS file")


async def run(args) -> dict:
    require(os.name == "nt", "this smoke requires Windows MPV named pipes")
    mpv, fixture, output = args.mpv.resolve(), args.fixture.resolve(), args.output_dir.resolve()
    require(mpv.is_file(), "MPV executable is missing")
    require(output.is_relative_to(REPO) and "build" in output.relative_to(REPO).parts,
            "output must be inside this repository's build directory")
    require(fixture.is_file() and fixture.is_relative_to(REPO) and
            "build" in fixture.relative_to(REPO).parts, "fixture must be an existing repository build file")
    require(mov_duration(fixture) >= 60, "ordinary-video fixture is too short")
    output.mkdir(parents=True, exist_ok=True)
    run_dir = output / ("run-" + uuid.uuid4().hex[:10])
    run_dir.mkdir()
    config, wrapper, _controls = prepare_config(run_dir)
    append_diagnostics(wrapper)  # Read-only production option/source diagnostics.
    fixtures = run_dir / "fixtures"
    fixtures.mkdir()
    video = fixtures / "handoff-ordinary.mov"
    shutil.copyfile(fixture, video)
    make_xml(video.with_suffix(".xml"), "HANDOFF")
    history_path = config / "_cache/danmaku-history.json"
    style_path = config / "script-opts/uosc_danmaku_style.json"
    require(not history_path.exists() and not style_path.exists(),
            "fresh isolated config unexpectedly contains danmaku preferences")
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(run_dir)
    overrides = ["uosc_danmaku-auto_load=no", "uosc_danmaku-autoload_for_url=no",
                 "uosc_danmaku-autoload_local_danmaku=yes", "uosc_danmaku-vf_fps=yes",
                 "uosc_danmaku-render_mode=ass", "uosc_danmaku-merge_tolerance=-1",
                 "uosc_danmaku-max_screen_danmaku=0",
                 "uosc_danmaku-history_path=~~/_cache/danmaku-history.json",
                 "uosc_danmaku-style_path=~~/script-opts/uosc_danmaku_style.json"]
    owned = []
    samples = []
    expected_header = None

    async def visibility(expected: bool) -> None:
        await wait_disk(history_path, lambda value: value.get("show_danmaku") is expected,
                        "shared visibility preference")

    async def diagnostic(ipc: MpvPipe) -> dict:
        previous = await ipc.command(["get_property", STATIC_STATE])
        await ipc.command(["script-message-to", "uosc_danmaku", "settings-smoke-read-static"])
        return await wait_property(ipc, STATIC_STATE,
            lambda value: isinstance(value, dict) and value.get("sequence", 0) > previous.get("sequence", 0))

    async def ass_output(ipc: MpvPipe, predicate, detail: str) -> dict:
        deadline = asyncio.get_running_loop().time() + 10
        last, error = None, None
        selection_attempts = []
        while asyncio.get_running_loop().time() < deadline:
            for track in await ipc.command(["get_property", "track-list"]) or []:
                filename = track.get("external-filename", "")
                if track.get("type") != "sub" or not Path(filename).name.startswith("uosc-danmaku-"):
                    continue
                path = Path(filename).resolve()
                require(path.is_relative_to(run_dir), "ASS output escaped the isolated test directory")
                with contextlib.suppress(OSError, ValueError, KeyError):
                    last = parse_ass(path)
                    try:
                        predicate(last)
                    except AssertionError as reason:
                        error = str(reason)
                        continue
                    # Live refresh removes/adds an ASS track. The initial
                    # track-list and later secondary-sid reads can straddle
                    # that reload, so require a current, selected ASS track
                    # within this existing wait rather than a stale track ID.
                    secondary = await ipc.command(["get_property", "secondary-sid"])
                    current_tracks = await ipc.command(["get_property", "track-list"]) or []
                    selected_current = any(
                        current.get("type") == "sub" and str(current.get("id")) == str(secondary) and
                        Path(current.get("external-filename", "")).resolve() == path
                        for current in current_tracks)
                    secondary_after = await ipc.command(["get_property", "secondary-sid"])
                    visible = await ipc.command(["get_property", "secondary-sub-visibility"])
                    selection = {"candidateTrackId": track.get("id"), "secondarySid": secondary,
                                 "secondarySidAfterTrackRead": secondary_after, "visible": visible,
                                 "selectedCurrentASS": selected_current}
                    selection_attempts.append(selection)
                    selection_attempts = selection_attempts[-20:]
                    if str(secondary) != str(secondary_after) or not selected_current:
                        error = "current ASS track was not yet stably selected as secondary subtitles"
                        continue
                    if visible is not True:
                        error = "current ASS track remained hidden"
                        continue
                    last["selection"] = selection
                    last["selectionAttempts"] = selection_attempts
                    return last
            await asyncio.sleep(0.05)
        if last:
            samples.append({"phase": "failed-ass-" + detail, "ass": summary(last),
                            "selectionAttempts": selection_attempts})
        raise AssertionError("ASS did not reflect shared preferences: " + detail + (": " + error if error else ""))

    async def state(player, phase: str, *, expected_visible: bool, static: bool = False) -> dict:
        ipc = player["ipc"]
        await wait_property(ipc, "user-data/uosc_danmaku/danmaku-switch-on", expected_visible)
        await wait_property(ipc, "user-data/uosc_danmaku/has-danmaku", expected_visible)
        values = await diagnostic(ipc)
        require(values["enabled"] is expected_visible, "effective visibility differs from saved preference: " + phase)
        require(all(values["values"].get(key) == value for key, value in CHOSEN_STYLE.items()),
                "effective live/video style differs from shared saved preferences: " + phase)
        sample = {"phase": phase, "pid": player["process"].pid, "visible": expected_visible,
                  "effectiveStyle": values["values"], "fullscreen": await ipc.command(["get_property", "fullscreen"]),
                  "historyExists": history_path.exists(), "historyVisible": disk_json(history_path).get("show_danmaku")}
        if expected_visible:
            def verify(ass):
                check_header(ass, CHOSEN_STYLE)
                if static:
                    check_style(ass, CHOSEN_STYLE, marker="HANDOFF")
                if expected_header is not None:
                    require(ass["styles"] == expected_header, "live and ordinary-video ASS style headers differ")
            ass = await ass_output(ipc, verify, phase)
            sample["ass"] = summary(ass)
            sample["assSelection"] = ass["selection"]
            sample["assSelectionAttempts"] = ass["selectionAttempts"]
            await wait_property(ipc, "vf", lambda filters: has_smooth_fps(filters, 120))
        else:
            await asyncio.sleep(0.3)
            require(not has_danmaku_track(await ipc.command(["get_property", "track-list"])),
                    "hidden shared preference loaded a visible ASS track: " + phase)
            require(not has_smooth_fps(await ipc.command(["get_property", "vf"])),
                    "hidden shared preference retained its smoothing filter: " + phase)
            require(disk_json(history_path).get("show_danmaku") is False,
                    "hidden new process overwrote the saved visibility preference")
        samples.append(sample)
        return sample

    async def start(*, live: bool, fullscreen: bool):
        index = len(owned) + 1
        pipe_name = "mpv-lazy-bilibili-handoff-smoke-" + uuid.uuid4().hex
        ipc = MpvPipe(pipe_name)
        remote = OfflineRemote() if live else None
        session = None
        if live:
            auth = validate_auth({"uid": 0, "token": "offline-test", "buvid": "offline-test-client",
                "userAgent": "Mozilla/5.0 offline preference test",
                "hostList": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}]})
            session = BilibiliSession("545068", "bilibili-handoff-smoke-" + uuid.uuid4().hex,
                pipe_name, auth, OfflineOwner(), connector=OfflineConnector(remote), ipc=ipc)
            await session.start()
        command = [str(mpv), f"--config-dir={config}", "--load-scripts=no", "--use-filedir-conf=no",
                   "--no-ytdl", "--idle=yes", "--terminal=yes", "--ao=null", "--vo=gpu-next",
                   "--force-window=yes", "--autofit=960x540", "--fullscreen=no", "--glsl-shaders-clr",
                   "--input-ipc-server=" + ipc.path,
                   "--script=" + str(SOURCE / "scripts/save_global_props.lua"),
                   "--script=" + str(SOURCE / "scripts/uosc"), "--script=" + str(wrapper),
                   *["--script-opts-append=" + value for value in overrides],
                   f"--log-file={run_dir / ('mpv-' + str(index) + '.log')}", "--msg-level=all=warn"]
        if fullscreen:
            command.append("--fullscreen=yes")
        command.append("av://lavfi:color=c=0x24364A:size=320x180:rate=30" if live else str(video))
        try:
            with (run_dir / f"process-{index}.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=run_dir, env=env, stdout=log,
                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            if session:
                await session.stop()
            raise
        player = {"process": process, "ipc": ipc, "session": session, "remote": remote}
        owned.append(player)
        await wait_property(ipc, "width", 320, timeout=15)
        await wait_property(ipc, "fullscreen", fullscreen, timeout=15)
        require(Path(await ipc.command(["get_property", "user-data/uosc_danmaku/style-defaults-path"])) == style_path,
                "style preference path escaped the shared isolated config")
        if live:
            await eventually(lambda: session.player_attached, "owned live API attached")
            await wait_property(ipc, "user-data/uosc_danmaku/live-active", True)
        return player

    async def frame(player, label: str) -> None:
        before = player["session"].sent
        await player["remote"].queue.put(chat_frame(label))
        await eventually(lambda: player["session"].sent > before, "offline frame crossed owned IPC")

    async def close(player, *, expected_visible: bool) -> None:
        session, process, ipc = player["session"], player["process"], player["ipc"]
        if session:
            await session.stop()
            require(disk_json(history_path).get("show_danmaku") is expected_visible,
                    "stopping a live session changed the saved visibility choice")
        await ipc.command(["quit"])
        await asyncio.to_thread(process.wait, 5)

    try:
        first = await start(live=True, fullscreen=False)
        ipc = first["ipc"]
        await wait_property(ipc, "user-data/uosc_danmaku/has-danmaku", True)
        await frame(first, "LIVE_PREFERENCE_FIXTURE")
        await ass_output(ipc, lambda ass: require("LIVE_PREFERENCE_FIXTURE" in ass["text"],
                                                "initial live frame is missing"), "initial-live")
        await ipc.command(["set_property", "pause", True])
        for key, value in CHOSEN_STYLE.items():
            if key == "bold":
                await ipc.command(["script-message-to", "uosc_danmaku", "setup-danmaku-style",
                                   json.dumps({"type": "activate", "index": 1})])
            else:
                await ipc.command(["script-message-to", "uosc_danmaku", "setup-danmaku-style", key, str(value)])
        await wait_disk(style_path, lambda value: all(value.get(k) == v for k, v in CHOSEN_STYLE.items()),
                        "style selected in the live player")
        live_ass = await ass_output(ipc, lambda value: check_header(value, CHOSEN_STYLE), "chosen-live-style")
        expected_header = live_ass["styles"]
        # This real menu toggle, rather than a fixture JSON write, is the sole
        # source of the visibility preference inherited by following players.
        await ipc.command(["script-message-to", "uosc_danmaku", "set", "show_danmaku", "off"])
        await visibility(False)
        await state(first, "live-user-hid-danmaku", expected_visible=False)
        await ipc.command(["script-message-to", "uosc_danmaku", "set", "show_danmaku", "on"])
        await visibility(True)
        await ipc.command(["set_property", "pause", False])
        await frame(first, "LIVE_REENABLED_FIXTURE")
        await state(first, "live-user-enabled-danmaku", expected_visible=True)
        await close(first, expected_visible=True)

        visible_vod = await start(live=False, fullscreen=True)
        await state(visible_vod, "fresh-vod-auto-visible-without-toggle", expected_visible=True, static=True)
        await visible_vod["ipc"].command(["set_property", "pause", True])
        await visible_vod["ipc"].command(["script-message-to", "uosc_danmaku", "set", "show_danmaku", "off"])
        await visibility(False)
        await state(visible_vod, "vod-user-hid-danmaku", expected_visible=False)
        await close(visible_vod, expected_visible=False)

        hidden_vod = await start(live=False, fullscreen=True)
        await state(hidden_vod, "fresh-vod-kept-hidden-without-toggle", expected_visible=False)
        await close(hidden_vod, expected_visible=False)
        hidden_live = await start(live=True, fullscreen=True)
        await frame(hidden_live, "HIDDEN_LIVE_FIXTURE")
        await state(hidden_live, "fresh-live-kept-hidden-without-toggle", expected_visible=False)
        await close(hidden_live, expected_visible=False)

        enabled_vod = await start(live=False, fullscreen=True)
        await state(enabled_vod, "fresh-vod-before-user-enable", expected_visible=False)
        await enabled_vod["ipc"].command(["script-message-to", "uosc_danmaku", "set", "show_danmaku", "on"])
        await visibility(True)
        await state(enabled_vod, "vod-user-enabled-danmaku", expected_visible=True, static=True)
        await close(enabled_vod, expected_visible=True)
        enabled_live = await start(live=True, fullscreen=True)
        await frame(enabled_live, "LIVE_INHERITED_VOD_FIXTURE")
        await state(enabled_live, "fresh-live-inherited-vod-enable", expected_visible=True)
        await close(enabled_live, expected_visible=True)

        for log_path in run_dir.glob("mpv-*.log"):
            log = log_path.read_text(encoding="utf-8", errors="replace").lower()
            require(not any(pattern in log for pattern in ("lua error", "stack traceback", "script failed",
                                                          "error parsing option", "error converting value")),
                    "owned player reported a Lua/option error: " + log_path.name)
        report = {"passed": True, "network": "offline live packets and local MOV/XML only",
                  "freshHistoryNotPrefilled": True, "productionMenusWroteAllPreferences": True,
                  "liveToFreshVideoVisibleWithoutManualToggle": True,
                  "liveAndVideoASSStylesMatch": True, "hiddenPreferenceKeptAcrossNewLiveAndVideoPlayers": True,
                  "videoEnableInheritedByFreshLivePlayer": True, "liveStopDoesNotOverwritePreference": True,
                  "newProcessCLIAppendedFullscreenYesVerified": True,
                  "fullscreenCoverage": "CLI new-process only; USH and reuse are not tested",
                  "autoloadCoverage": "offline XML subcase explicitly enables autoload_local_danmaku; website CID not tested",
                  "ownedPlayerCount": len(owned), "samples": samples,
                  "styleDisk": disk_json(style_path), "historyVisible": disk_json(history_path).get("show_danmaku"),
                  "runDirectory": str(run_dir)}
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    except Exception as error:
        failure = {"passed": False, "error": str(error), "runDirectory": str(run_dir),
                   "samples": samples, "styleDisk": disk_json(style_path),
                   "historyVisible": disk_json(history_path).get("show_danmaku"),
                   "historyExists": history_path.exists()}
        if owned and owned[-1]["process"].poll() is None:
            with contextlib.suppress(Exception):
                failure["effectiveStateBeforeCleanup"] = await diagnostic(owned[-1]["ipc"])
        (run_dir / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    finally:
        for player in owned:
            if player["session"]:
                with contextlib.suppress(Exception):
                    await player["session"].stop()
            if player["process"].poll() is None:
                with contextlib.suppress(Exception):
                    await player["ipc"].command(["quit"])
                    await asyncio.to_thread(player["process"].wait, 3)
                if player["process"].poll() is None:
                    player["process"].terminate()
                    await asyncio.to_thread(player["process"].wait, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    report = asyncio.run(run(parser.parse_args()))
    print(json.dumps({key: value for key, value in report.items()
        if isinstance(value, bool) or key in ("network", "runDirectory", "ownedPlayerCount",
                                              "fullscreenCoverage", "autoloadCoverage")}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
