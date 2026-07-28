"""Focused tests for incremental release checksum handling."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("package_release.py")
SPEC = importlib.util.spec_from_file_location("package_release", MODULE_PATH)
assert SPEC and SPEC.loader
PACKAGE_RELEASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PACKAGE_RELEASE
SPEC.loader.exec_module(PACKAGE_RELEASE)


class ReleaseAssetDigestTests(unittest.TestCase):
    def test_parses_github_sha256_digests(self) -> None:
        output = "\n".join(
            [
                "base.zip\tsha256:" + "a" * 64,
                "config.zip\t" + "B" * 64,
                "missing.zip\tnull",
                "malformed",
            ]
        )

        self.assertEqual(
            PACKAGE_RELEASE.parse_release_asset_digests(output),
            {
                "base.zip": "a" * 64,
                "config.zip": "b" * 64,
            },
        )

    def test_rejects_non_sha256_values(self) -> None:
        output = "\n".join(
            [
                "short.zip\tsha256:1234",
                "invalid.zip\tsha256:" + "z" * 64,
                "\tsha256:" + "a" * 64,
            ]
        )

        self.assertEqual(PACKAGE_RELEASE.parse_release_asset_digests(output), {})

    def test_selected_build_prefers_remote_digest_for_unbuilt_local_asset(self) -> None:
        version = "vtest"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            paths = PACKAGE_RELEASE.Paths(
                repo=root,
                source=root,
                work=root / "work",
                artifacts=artifacts,
                staging=root / "staging",
                package_inputs=root / "packages",
                verify=root / "verify",
                seven_zip=root / "7z.exe",
            )
            base = artifacts / f"mpv-lazy-2026-custom-base-{version}.zip"
            config = artifacts / f"mpv-lazy-2026-custom-config-{version}.zip"
            base.write_bytes(b"stale local base")
            config.write_bytes(b"new config")
            remote_base_digest = "a" * 64

            PACKAGE_RELEASE.write_sha256(
                paths,
                version,
                ["config"],
                {base.name: remote_base_digest},
            )

            checksums = PACKAGE_RELEASE.read_sha256_file(
                artifacts / f"SHA256SUMS-{version}.txt"
            )
            self.assertEqual(checksums[base.name], remote_base_digest)
            self.assertEqual(
                checksums[config.name],
                PACKAGE_RELEASE.sha256_file(config),
            )


class ReleaseOverlayTests(unittest.TestCase):
    def test_excludes_timestamped_development_backups(self) -> None:
        rel = "portable_config/scripts/douyu_danmaku.lua.bak-20260726-032711"
        filename = "douyu_danmaku.lua.bak-20260726-032711"

        self.assertTrue(PACKAGE_RELEASE.matches_exclude_glob(rel, filename))
        self.assertFalse(
            PACKAGE_RELEASE.should_overlay(
                rel,
                filename,
            )
        )


if __name__ == "__main__":
    unittest.main()
