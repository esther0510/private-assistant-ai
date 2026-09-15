import io
import re
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


class BootstrapScriptTests(unittest.TestCase):
    def test_start_reuses_existing_venv_unless_rebuild_needed(self) -> None:
        start = read_text(ROOT / "START.bat")
        self.assertIn('if "%NEED_REBUILD%"=="1" (', start)
        self.assertIn('"%UV%" venv --clear --managed-python --python "%PYTHON_SPEC%" "%VENV%"', start)
        self.assertIn("Reusing existing Python environment.", start)
        self.assertIn('resolved_python_version=$env:RESOLVED_PYTHON_VERSION', start)
        self.assertNotIn("CPython", start)

    def test_missing_fingerprint_does_not_force_clear_by_itself(self) -> None:
        start = read_text(ROOT / "START.bat")
        block = re.search(r"if not defined OLD_FINGERPRINT \((.*?)\)", start, re.S)
        self.assertIsNotNone(block)
        self.assertNotIn("NEED_REBUILD", block.group(1))

    def test_update_entry_does_not_force_rebuild(self) -> None:
        update = read_text(ROOT / "UPDATE_AND_START.bat")
        self.assertNotIn("PAI_FORCE_SYNC", update)
        self.assertIn('call "%~dp0START.bat"', update)

    def test_reset_env_is_only_force_rebuild_entry(self) -> None:
        self.assertNotIn("PAI_FORCE_SYNC", read_text(ROOT / "START.bat"))
        self.assertIn('rmdir /s /q "%~dp0.venv"', read_text(ROOT / "RESET_ENV.bat"))
        self.assertIn('rmdir /s /q "%~dp0.bootstrap"', read_text(ROOT / "RESET_ENV.bat"))

    def test_bootstrap_sources_do_not_feed_interactive_answers(self) -> None:
        forbidden = [
            "yes" + " |",
            "echo " + "yes",
            "> " + "yes",
            "\u203a " + "yes",
        ]
        files = [
            ROOT / "START.bat",
            ROOT / "UPDATE_AND_START.bat",
            ROOT / "RESET_ENV.bat",
            ROOT / "work" / "build_friend_bootstrap.ps1",
            ROOT / "work" / "smoke_friend_bootstrap.ps1",
        ]
        for path in files:
            with self.subTest(path=path.name):
                if path.parent.name == "work" and not path.exists():
                    continue  # Optional local packaging helpers are not in a source checkout.
                text = read_text(path)
                for pattern in forbidden:
                    self.assertNotIn(pattern, text)

    def test_release_zip_start_matches_root_start(self) -> None:
        zip_path = ROOT / "release" / "PrivateAssistantAI_v0.1.0-alpha_friend-bootstrap.zip"
        if not zip_path.exists():
            self.skipTest("friend bootstrap ZIP has not been built yet")

        root_start = read_text(ROOT / "START.bat").replace("\r\n", "\n")
        root_update = read_text(ROOT / "UPDATE_AND_START.bat").replace("\r\n", "\n")

        with zipfile.ZipFile(zip_path) as archive:
            names = {name.replace("\\", "/"): name for name in archive.namelist()}
            start_name = names["PrivateAssistantAI_friend_bootstrap/START.bat"]
            update_name = names["PrivateAssistantAI_friend_bootstrap/UPDATE_AND_START.bat"]
            zipped_start = io.TextIOWrapper(archive.open(start_name), encoding="utf-8", errors="ignore").read()
            zipped_update = io.TextIOWrapper(archive.open(update_name), encoding="utf-8", errors="ignore").read()

        self.assertEqual(zipped_start.replace("\r\n", "\n"), root_start)
        self.assertEqual(zipped_update.replace("\r\n", "\n"), root_update)

    def test_friend_package_excludes_wake_enrollment_data(self) -> None:
        zip_path = ROOT / "release" / "PrivateAssistantAI_v0.1.0-alpha_friend-bootstrap.zip"
        if not zip_path.exists():
            self.skipTest("friend bootstrap ZIP has not been built yet")

        with zipfile.ZipFile(zip_path) as archive:
            names = [name.replace("\\", "/").lower() for name in archive.namelist()]

        self.assertFalse(any("wake_templates" in name for name in names))
        self.assertFalse(any(name.endswith((".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus")) for name in names))

    def test_start_fingerprint_uses_major_minor_python_spec(self) -> None:
        start = read_text(ROOT / "START.bat")
        self.assertRegex(start, re.compile(r'set "PYTHON_SPEC=3\.12"'))
        self.assertIn("'python='+$env:PYTHON_SPEC", start)
        self.assertNotIn("resolved_python_version+[Environment]::NewLine", start)


if __name__ == "__main__":
    unittest.main()
