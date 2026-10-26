"""Compare real MPV seek audio timing for external DASH audio and a paired EDL.

Uses a generated long-GOP H.264 video and an independent non-silent AAC tone.
Only isolated local fixtures are played; result.json contains no media URLs.
The old external-audio case is a required negative control, not a passed case.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools/telegram-web-mpv-bridge"))
from bilibili_danmaku import MpvPipe


def require(condition, message):
    if not condition:
        raise AssertionError(message)


async def get(ipc, property_name, default=None):
    try:
        return await ipc.command(["get_property", property_name])
    except (OSError, RuntimeError, TimeoutError):
        return default


async def wait_for(ipc, property_name, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await get(ipc, property_name)
        if predicate(value):
            return value
        await asyncio.sleep(.025)
    raise AssertionError("Timed out waiting for " + property_name)


def run_ffmpeg(ffmpeg, arguments):
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-nostdin", "-y", *arguments],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    require(result.returncode == 0, "Fixture encoding failed: " + result.stderr.decode(errors="replace")[-2000:])


def make_fixtures(ffmpeg, directory):
    directory.mkdir(parents=True)
    video, audio = directory / "long-gop.mp4", directory / "tone.m4a"
    run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25", "-t", "35",
                        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-g", "250",
                        "-keyint_min", "250", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(video)])
    run_ffmpeg(ffmpeg, ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "35",
                        "-vn", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(audio)])
    return video, audio


def edl_source(video, audio):
    def part(path):
        value = str(path).replace("\\", "/")
        return "%" + str(len(value.encode("utf-8"))) + "%" + value
    return "edl://!no_clip;!no_chapters;" + part(video) + ";!new_stream;!no_clip;!no_chapters;" + part(audio)


async def measure_case(mpv, video, audio, directory, paired, memory=False):
    directory.mkdir()
    pipe_name = "mpv-audio-seek-" + uuid.uuid4().hex
    ipc = MpvPipe(pipe_name)
    log = directory / "mpv.log"
    command = [str(mpv), "--no-config", "--load-scripts=no", "--no-ytdl", "--idle=yes",
               "--pause=yes", "--vo=null", "--ao=null", "--terminal=no", "--keep-open=yes",
               "--hr-seek-framedrop=no", "--input-ipc-server=" + ipc.path,
               "--msg-level=all=v", "--log-file=" + str(log), "--start=18.4"]
    if paired:
        source = edl_source(video, audio)
        if memory:
            source = "memory://# mpv EDL v0\n" + source.removeprefix("edl://")
        command.append(source)
    else:
        command.extend(["--audio-file=" + str(audio), str(video)])
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        await wait_for(ipc, "time-pos", lambda value: isinstance(value, (int, float)) and 18.3 < value < 18.5)
        tracks = await get(ipc, "track-list", [])
        require(any(track.get("type") == "audio" and track.get("selected") for track in tracks),
                "Fixture audio was not selected")
        require(any(track.get("type") == "video" and track.get("selected") for track in tracks),
                "Fixture video was not selected")
        # The request is 13.4, midway through the GOP that starts at 10.0.
        # Relative keyframe seeking reproduces normal arrow-key/scroll seeking.
        log_offset = log.stat().st_size
        await ipc.command(["seek", -5, "relative+keyframes"])
        await wait_for(ipc, "seeking", lambda value: value is False)
        started = time.monotonic()
        await ipc.command(["set_property", "pause", False])
        samples = []
        while time.monotonic() - started < 4.8:
            sample = await ipc.command(["expand-text", "${=time-pos}|${=audio-pts}"])
            try:
                raw_video, raw_audio = sample.split("|", 1)
                vpts = float(raw_video)
                apts = float(raw_audio) if raw_audio else None
                samples.append({"wall": round(time.monotonic() - started, 4),
                                "video": round(vpts, 5), "audio": round(apts, 5) if apts is not None else None})
            except (ValueError, AttributeError):
                pass
            await asyncio.sleep(.02)
        await ipc.command(["set_property", "pause", True])
        require(len(samples) > 50, "Insufficient playback timing samples")
        seek_log = log.read_bytes()[log_offset:].decode(errors="replace")
        delays = [float(value) for value in re.findall(r"delaying audio start .*?diff=([0-9.]+)", seek_log)]
        audio_samples = [sample for sample in samples if sample["audio"] is not None]
        require(audio_samples, "Audio playback clock never became available")
        initial_audio = audio_samples[0]["audio"]
        first_audio = next((sample["wall"] for sample in audio_samples if sample["audio"] > initial_audio + .05), None)
        maximum_lead = max(sample["audio"] - sample["video"] for sample in audio_samples[:10])
        result = {"source": "paired-memory-edl" if memory else "paired-edl" if paired else "external-audio-file",
                  "samples": len(samples), "firstVideoPts": samples[0]["video"],
                  "firstAudioPts": initial_audio, "maximumReportedAudioLeadSeconds": round(maximum_lead, 4),
                  "audioFirstAvailableSeconds": audio_samples[0]["wall"],
                  "audioFirstAdvanceSeconds": first_audio,
                  "maxLoggedAudioStartDelaySeconds": max(delays, default=0),
                  "finalAvDifferenceSeconds": round(samples[-1]["audio"] - samples[-1]["video"], 4)}
        (directory / "timing.json").write_text(json.dumps(samples, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        with contextlib.suppress(OSError, RuntimeError, TimeoutError):
            await ipc.command(["quit"])
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=8)


async def run(args):
    output = args.output_dir.resolve()
    require(output.is_relative_to(REPO) and "build" in output.relative_to(REPO).parts,
            "Use a repository build output directory")
    run_dir = output / ("run-" + uuid.uuid4().hex[:10])
    video, audio = make_fixtures(args.ffmpeg, run_dir / "fixtures")
    old = await measure_case(args.mpv, video, audio, run_dir / "external", False)
    paired = await measure_case(args.mpv, video, audio, run_dir / "edl", True)
    memory_paired = await measure_case(args.mpv, video, audio, run_dir / "memory-edl", True, memory=True)
    result = {"passed": False, "negativeControl": old, "pairedSource": paired,
              "memoryPairedSource": memory_paired,
              "scope": "Real MPV, local long-GOP video and independent non-silent AAC. No website or account access."}
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    require(old["maxLoggedAudioStartDelaySeconds"] > 2 and old["audioFirstAvailableSeconds"] > 2,
            "External audio negative control did not reproduce a multi-second start gap")
    require(old["audioFirstAdvanceSeconds"] is not None and old["audioFirstAdvanceSeconds"] > 2,
            "External audio negative control did not keep the audio clock stopped")
    for source in (paired, memory_paired):
        require(source["maximumReportedAudioLeadSeconds"] < .2 and source["maxLoggedAudioStartDelaySeconds"] < .2,
                source["source"] + " still has a seek audio gap")
        require(source["audioFirstAdvanceSeconds"] is not None and source["audioFirstAdvanceSeconds"] < .5,
                source["source"] + " audio did not resume promptly")
    result["passed"] = True
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mpv", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))
