"""Focused tests for incremental release checksum handling."""

from __future__ import annotations

import importlib.util
import sys
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
