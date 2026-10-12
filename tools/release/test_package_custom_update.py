"""Release integrity checks using a disposable Git repository and PE fixture."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

import package_custom_update as pack


class PackageCustomUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Release fixture")
        for name in ("README.MD", "external_player.js", "LICENSE.MD", "LICENSE.txt", *pack.BRIDGE_INPUTS, "portable_config/scripts/uosc/unchanged.lua"):
            self.write(name, b"original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").strip().decode()
        self.write("README.MD", b"release documentation\n")
        self.write("external_player.js", b"// browser 1.2.14.15\n")
        self.write(pack.BRIDGE_INPUTS[0], b"# new bridge\n")
        self.write("docs/new-feature.md", b"feature\n")
        self.git("add", ".")
        self.git("commit", "-qm", "custom update")
        self.exe = self.root / "fixture.exe"
        binary = bytearray(128)
        binary[:2] = b"MZ"
        binary[60:64] = (64).to_bytes(4, "little")
        binary[64:68] = b"PE\0\0"
        self.exe.write_bytes(binary)
        self.provenance = self.root / "provenance.json"
        self.evidence = {"exeSha256": pack.sha256(binary), "frozenHealthPassed": True, "frozenWebSocketPassed": True,
                         "frozenPlaybackHttpPassed": True,
                         "brotliBinaryIncluded": True, "sourceInputs": {name: pack.sha256((self.repo / name).read_bytes()) for name in pack.BRIDGE_INPUTS}}
        self.provenance.write_text(json.dumps(self.evidence))
        self.notices = self.root / "notices.txt"
        self.notices.write_text("public licenses\n")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL)

    def write(self, name, data):
        file = self.repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)

    def build(self):
        return pack.build(self.repo, self.baseline, "HEAD", "v2026.10.12-update", self.exe, self.provenance, self.notices, self.root / "output")

    def test_complete_diff_and_unchanged_coupled_files(self):
        result = self.build()
        with zipfile.ZipFile(result["archive"]) as archive:
            manifest = json.loads(archive.read(pack.MANIFEST))
            self.assertEqual(result["changedFileCount"], 4)
            self.assertTrue(set(manifest["cumulativeChanges"]) <= set(archive.namelist()))
            self.assertIn("portable_config/scripts/uosc/unchanged.lua", archive.namelist())
            self.assertIn(pack.EXE_PATH, archive.namelist())
            self.assertEqual(archive.read("external_player.js"), archive.read("external_player.user.js"))
        self.assertEqual(pack.verify(Path(result["archive"]))["sourceCommit"], self.git("rev-parse", "HEAD").decode().strip())

    def test_working_state_and_personal_files_are_not_copied(self):
        self.write("external_player.js", b"uncommitted accidental change")
        self.write("portable_config/saved-props.json", b'{"volume":7}')
        self.write("tools/telegram-web-mpv-bridge/build/old.zip", b"old")
        with zipfile.ZipFile(self.build()["archive"]) as archive:
            self.assertNotIn("portable_config/saved-props.json", archive.namelist())
            self.assertNotIn("tools/telegram-web-mpv-bridge/build/old.zip", archive.namelist())
            self.assertEqual(archive.read("external_player.js"), b"// browser 1.2.14.15\n")

    def test_frozen_source_mismatch_is_rejected(self):
        self.evidence["sourceInputs"][pack.BRIDGE_INPUTS[0]] = "0" * 64
        self.provenance.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(ValueError, "different source"):
            self.build()

    def test_unverified_binary_is_rejected(self):
        self.evidence["frozenWebSocketPassed"] = False
        self.provenance.write_text(json.dumps(self.evidence))
        with self.assertRaisesRegex(ValueError, "verification"):
            self.build()

    def test_missing_or_failed_http_progress_verification_is_rejected(self):
        for value in (None, False, "true", 1):
            with self.subTest(value=value):
                if value is None:
                    self.evidence.pop("frozenPlaybackHttpPassed", None)
                else:
                    self.evidence["frozenPlaybackHttpPassed"] = value
                self.provenance.write_text(json.dumps(self.evidence))
                with self.assertRaisesRegex(ValueError, "verification"):
                    self.build()

    def test_non_pe_binary_is_rejected(self):
        self.exe.write_bytes(b"not an EXE")
        with self.assertRaisesRegex(ValueError, "Windows PE"):
            self.build()

    def test_deletion_requires_migration(self):
        self.git("rm", "README.MD")
        self.git("commit", "-qm", "delete")
        with self.assertRaisesRegex(ValueError, "deletions"):
            self.build()

    def test_traversal_runtime_and_credentials_are_rejected(self):
        for name in ("../outside", "C:/file", "a\\b", "/absolute", "a/./b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                pack.validate_payload(name, b"safe")
        for name in ("portable_config/saved-props.json", "portable_config/script-opts/uosc_danmaku_style.json", "tools/.venv/file.py", "tools/cookies.txt", "tools/output.log", "tools/telegram-web-mpv-bridge/playback-progress.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                pack.validate_payload(name, b"personal")
        with self.assertRaisesRegex(ValueError, "Credential"):
            pack.validate_payload("tools/code.py", b"ghp_" + b"x" * 36)

    def test_archive_tampering_is_detected(self):
        original = Path(self.build()["archive"])
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(tampered, "w") as dst:
            for name in src.namelist():
                dst.writestr(name, b"tampered\n" if name == "README.MD" else src.read(name))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            pack.verify(tampered)

    def test_duplicate_members_are_rejected(self):
        original = Path(self.build()["archive"])
        with zipfile.ZipFile(original, "a") as dst:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                dst.writestr("README.MD", b"duplicate")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            pack.verify(original)


if __name__ == "__main__":
    unittest.main()
