#!/usr/bin/env python3
"""Build and verify a cumulative overlay from committed Git blobs, not local state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

BASELINE = "547e3d7f7a1aa44538ab55fdba3a36e5bcb61984"
REPOSITORY = "HikigayaMan/mpv-lazy-2026-custom"
COUPLED = (
    "portable_config/scripts/uosc/", "portable_config/scripts/uosc_danmaku/",
    "portable_config/scripts/thumb_engine/", "portable_config/scripts/bilibiliAssert/",
    "tools/telegram-web-mpv-bridge/", "tools/release/",
)
BRIDGE_INPUTS = (
    "tools/telegram-web-mpv-bridge/bridge.py",
    "tools/telegram-web-mpv-bridge/bilibili_danmaku.py",
    "tools/telegram-web-mpv-bridge/requirements.txt",
    "tools/telegram-web-mpv-bridge/requirements-build.txt",
)
EXE_PATH = "tools/telegram-web-mpv-bridge/bilibili-live-bridge.exe"
MANIFEST = "UPDATE-MANIFEST.json"
CHECKSUMS = "UPDATE-FILES-SHA256.txt"
RESERVED = {MANIFEST, CHECKSUMS, "external_player.user.js", "BRIDGE-THIRD-PARTY-NOTICES.txt"}
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".lua", ".md", ".txt", ".json", ".conf", ".ps1", ".bat", ".cmd", ".yml", ".yaml", ".html", ".csv"}
SECRETS = re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_path(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ":" in name or any(p in {".", "..", ""} for p in name.split("/")):
        raise ValueError(f"Unsafe archive path: {name}")
    return name


def validate_payload(name: str, data: bytes) -> None:
    safe_path(name)
    lower = name.lower()
    parts = lower.split("/")
    if any(p in {".git", ".venv", "__pycache__", "_cache", "node_modules", "build"} for p in parts):
        raise ValueError(f"Runtime directory cannot be released: {name}")
    runtime = ("cookies", "saved-props", "uosc_danmaku_style", "trakt_config", "trakt_history", "rife_runtime.json", "rife_sr_runtime.json")
    if any(p.startswith(runtime) for p in parts) or parts[-1] in {"config.json", "settings.json", "viewed.json", "recent.json", "trakt_scrobble.conf"}:
        raise ValueError(f"Personal state cannot be released: {name}")
    if lower.endswith((".log", ".db", ".pyc", ".local.txt", ".zip", ".lnk")):
        raise ValueError(f"Runtime artifact cannot be released: {name}")
    if lower.endswith((".exe", ".dll", ".pyd", ".asi")) and name != EXE_PATH:
        raise ValueError(f"Unapproved binary: {name}")
    if PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES and SECRETS.search(data):
        raise ValueError(f"Credential pattern found in: {name}")


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def collect_source(repo: Path, baseline: str, ref: str) -> tuple[dict[str, bytes], dict]:
    baseline = git(repo, "rev-parse", f"{baseline}^{{commit}}").decode().strip()
    ref = git(repo, "rev-parse", f"{ref}^{{commit}}").decode().strip()
    subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", baseline, ref], check=True)
    fields = git(repo, "diff", "--name-status", "-z", "--no-renames", baseline, ref).decode().split("\0")
    changes = dict(zip(fields[1::2], fields[0::2]))
    if any(status not in {"A", "M"} for status in changes.values()):
        raise ValueError("Overlay has deletions or unsupported changes; provide an explicit migration before releasing.")
    entries = {}
    for record in git(repo, "ls-tree", "-rz", ref).split(b"\0"):
        if not record:
            continue
        metadata, name_raw = record.split(b"\t", 1)
        mode, kind, obj = metadata.decode().split()
        name = name_raw.decode()
        if name not in changes and name not in {"LICENSE.MD", "LICENSE.txt"} and not name.startswith(COUPLED):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError(f"Only regular Git blobs may be released: {name}")
        if name in RESERVED:
            raise ValueError(f"Reserved generated path: {name}")
        data = git(repo, "cat-file", "blob", obj)
        validate_payload(name, data)
        entries[name] = data
    if not changes.keys() <= entries.keys():
        raise ValueError("Not every cumulative change is included.")
    if "external_player.js" not in entries:
        raise ValueError("Missing browser userscript source.")
    entries["external_player.user.js"] = entries["external_player.js"]
    return entries, {"baselineCommit": baseline, "sourceCommit": ref, "cumulativeChanges": changes}


def build(repo: Path, baseline: str, ref: str, version: str, exe: Path, provenance: Path, notices: Path, output: Path) -> dict:
    if not re.fullmatch(r"v[0-9][A-Za-z0-9.-]*", version):
        raise ValueError("Invalid release version.")
    entries, manifest = collect_source(repo, baseline, ref)
    binary = exe.read_bytes()
    pe_offset = int.from_bytes(binary[60:64], "little")
    if not binary.startswith(b"MZ") or binary[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise ValueError("Bridge binary is not a Windows PE executable.")
    evidence = json.loads(provenance.read_text(encoding="utf-8"))
    if evidence.get("exeSha256") != sha256(binary) or not evidence.get("frozenHealthPassed") or not evidence.get("frozenWebSocketPassed") or not evidence.get("brotliBinaryIncluded"):
        raise ValueError("Bridge verification does not match this executable.")
    expected_inputs = {name: sha256(entries[name]) for name in BRIDGE_INPUTS}
    if evidence.get("sourceInputs") != expected_inputs:
        raise ValueError("Frozen bridge was built from different source inputs.")
    entries[EXE_PATH] = binary
    entries["BRIDGE-THIRD-PARTY-NOTICES.txt"] = notices.read_bytes()
    for name, data in entries.items():
        validate_payload(name, data)
    manifest.update({"schemaVersion": 1, "repository": REPOSITORY, "version": version,
                     "installation": "Merge archive contents into the directory containing mpv.exe; requires upstream v2026.06 runnable base.",
                     "coupledSourceDirectories": list(COUPLED), "removedPaths": [], "bridgeBuild": evidence,
                     "files": {name: {"sha256": sha256(data), "size": len(data)} for name, data in sorted(entries.items())}})
    entries[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    entries[CHECKSUMS] = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(entries.items())).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"mpv-lazy-2026-custom-cumulative-update-{version.removesuffix('-update')}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, data, compresslevel=9)
    verified = verify(archive)
    checksum = output / f"SHA256SUMS-{version}.txt"
    checksum.write_text(f"{sha256(archive.read_bytes())}  {archive.name}\n", encoding="utf-8")
    return {"archive": str(archive), "checksum": str(checksum), "sha256": sha256(archive.read_bytes()),
            "fileCount": verified["fileCount"], "changedFileCount": len(manifest["cumulativeChanges"]), "sourceCommit": manifest["sourceCommit"], "bytes": archive.stat().st_size}


def verify(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive paths.")
        if package.testzip():
            raise ValueError("Archive CRC verification failed.")
        payload = {name: package.read(name) for name in names}
    for name, data in payload.items():
        validate_payload(name, data)
    manifest = json.loads(payload[MANIFEST])
    expected = set(manifest["files"]) | {MANIFEST, CHECKSUMS}
    if set(payload) != expected:
        raise ValueError("Archive file list does not match the manifest.")
    for name, record in manifest["files"].items():
        if record != {"sha256": sha256(payload[name]), "size": len(payload[name])}:
            raise ValueError(f"Manifest checksum mismatch: {name}")
    required = set(manifest["cumulativeChanges"]) | set(BRIDGE_INPUTS) | {EXE_PATH, "external_player.js", "external_player.user.js", "README.MD"}
    if not required <= payload.keys():
        raise ValueError("Missing cumulative update files.")
    if payload["external_player.js"] != payload["external_player.user.js"]:
        raise ValueError("Userscript alias differs from source.")
    checksums = "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(payload.items()) if name != CHECKSUMS).encode()
    if checksums != payload[CHECKSUMS]:
        raise ValueError("Internal checksum list mismatch.")
    return {"fileCount": len(payload), "sourceCommit": manifest["sourceCommit"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--version", default="v2026.10.12-update")
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--notices", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        result = verify(args.verify)
    else:
        if not all((args.exe, args.provenance, args.notices, args.output)):
            parser.error("Build requires --exe, --provenance, --notices and --output.")
        result = build(args.repo, args.baseline, args.ref, args.version, args.exe, args.provenance, args.notices, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
