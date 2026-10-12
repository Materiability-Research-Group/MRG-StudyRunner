"""study_runner/updates/installer.py: the detached restart helper.

Git checkouts are updated before the restart; the helper only waits for the old
server and starts it again in a visible window. Archive installs are updated by
the helper itself: swap program files, add new content, install, restart -- and
roll everything back if the install of the new version fails. The packaged
branch stays covered by test_update_service.py.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.updates import archive_update, installer


def _release_tree(root: Path, version: str, marker: str) -> Path:
    (root / "software" / "study_runner").mkdir(parents=True)
    (root / "software" / "server.py").write_text(f"# {marker}\n", encoding="utf-8")
    (root / "software" / "study_runner" / "code.py").write_text(f"VERSION = '{version}'\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "install-macos.sh").write_text(f"# {marker}\n", encoding="utf-8")
    (root / "README.md").write_text(f"{marker}\n", encoding="utf-8")
    (root / archive_update.RELEASE_INFO_NAME).write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "software" / "study_content" / "studies").mkdir(parents=True)
    (root / "software" / "study_content" / "studies" / "Example.study-runner").write_text(marker, encoding="utf-8")
    return root


def _write_state(state_file: Path, install_root: Path, staged: dict) -> None:
    state_file.write_text(json.dumps({
        "staged": staged,
        "source_restart": {"base_dir": str(install_root / "software"), "install_root": str(install_root), "port": "0"},
    }), encoding="utf-8")


class InstallerRestartTests(unittest.TestCase):
    def test_git_checkout_restarts_in_a_visible_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_root = Path(temp) / "install"
            (install_root / "software").mkdir(parents=True)
            state_file = Path(temp) / "update-state.json"
            _write_state(state_file, install_root, {"mode": "source", "version": "1.1.0"})

            with patch.object(installer, "_start_visible") as start, patch.object(installer.time, "sleep"):
                exit_code = installer.main([str(state_file)])

            self.assertEqual(exit_code, 0)
            start.assert_called_once()
            self.assertEqual(Path(start.call_args[0][0]), install_root.resolve())
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["state"], "applied")


class InstallScriptFailureReasonTests(unittest.TestCase):
    def test_the_macos_script_names_its_reason_after_a_fixed_prefix(self) -> None:
        lines = ["Reusing uv 0.5.0.", "Study Runner setup failed: the uv download has the wrong checksum"]
        self.assertEqual(installer._failure_reason(lines), "the uv download has the wrong checksum")

    def test_a_powershell_error_is_read_from_the_line_before_its_location(self) -> None:
        lines = [
            "Downloading uv 0.5.0...",
            "The uv download has the wrong checksum (expected a, found b).",
            "At C:\\Study Runner\\tools\\install-windows.ps1:135 char:13",
            "+             throw \"The uv download has the wrong checksum\"",
            "    + CategoryInfo          : OperationStopped",
        ]
        self.assertEqual(installer._failure_reason(lines), "The uv download has the wrong checksum (expected a, found b).")

    def test_otherwise_the_last_line_is_the_reason(self) -> None:
        self.assertEqual(installer._failure_reason(["one", "two"]), "two")
        self.assertEqual(installer._failure_reason([]), "")


class InstallerVisibleStartTests(unittest.TestCase):
    def test_an_unanswered_terminal_prompt_does_not_fail_a_finished_install(self) -> None:
        # macOS asks before a program may control Terminal; a prompt nobody
        # answers must leave "start it by hand", not a failed update.
        with tempfile.TemporaryDirectory() as temp:
            log_file = Path(temp) / "update-helper.log"
            timeout = installer.subprocess.TimeoutExpired(cmd="osascript", timeout=60)
            with patch.object(installer.os, "name", "posix"), \
                    patch.object(installer.sys, "platform", "darwin"), \
                    patch.object(installer.subprocess, "run", side_effect=timeout):
                installer._start_visible(Path(temp), log_file)

            self.assertIn("start Study Runner by hand", log_file.read_text(encoding="utf-8"))


class InstallerArchiveUpdateTests(unittest.TestCase):
    def _prepare(self, temp: str):
        base = Path(temp)
        install_root = _release_tree(base / "install", "1.0.0", "old")
        (install_root / "software" / "saved_results" / "Study").mkdir(parents=True)
        (install_root / "software" / "saved_results" / "Study" / "result.json").write_text("data", encoding="utf-8")
        (install_root / "software" / "study_content" / "studies" / "Example.study-runner").write_text("mine", encoding="utf-8")
        (install_root / "software" / "study_content" / "settings").mkdir(parents=True)
        (install_root / "software" / "study_content" / "settings" / "local_secrets.json").write_text("secret", encoding="utf-8")
        (install_root / ".venv").mkdir()
        release_root = _release_tree(base / "staging" / "1.1.0" / "MRG-StudyRunner-1.1.0", "1.1.0", "new")
        (release_root / "software" / "study_content" / "studies" / "New Example.study-runner").write_text("new", encoding="utf-8")
        state_file = base / "update-state.json"
        _write_state(state_file, install_root, {"mode": "archive", "version": "1.1.0", "release_root": str(release_root)})
        return install_root, state_file

    def test_archive_update_replaces_program_files_and_keeps_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_root, state_file = self._prepare(temp)
            with patch.object(installer, "_start_visible") as start, \
                    patch.object(installer, "_run_install_script") as install, \
                    patch.object(installer.time, "sleep"):
                exit_code = installer.main([str(state_file)])

            self.assertEqual(exit_code, 0)
            install.assert_called_once()
            start.assert_called_once()
            software = install_root / "software"
            self.assertEqual((software / "server.py").read_text(encoding="utf-8"), "# new\n")
            self.assertIn("1.1.0", (software / "study_runner" / "code.py").read_text(encoding="utf-8"))
            self.assertEqual(archive_update.read_installed_version(install_root), "1.1.0")
            # user data untouched, new example added, existing study not overwritten
            self.assertEqual((software / "saved_results" / "Study" / "result.json").read_text(encoding="utf-8"), "data")
            self.assertEqual((software / "study_content" / "settings" / "local_secrets.json").read_text(encoding="utf-8"), "secret")
            self.assertEqual((software / "study_content" / "studies" / "Example.study-runner").read_text(encoding="utf-8"), "mine")
            self.assertTrue((software / "study_content" / "studies" / "New Example.study-runner").is_file())
            self.assertTrue((install_root / ".venv").is_dir())
            backups = list((install_root / ".tools" / "update-backup").iterdir())
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "software" / "server.py").read_text(encoding="utf-8"), "# old\n")
            # Installed: the panel must not offer "Restart now" or "version available" again.
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "applied")
            self.assertNotIn("staged", saved)
            self.assertNotIn("update", saved)

    def test_failed_install_rolls_back_and_restarts_the_old_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_root, state_file = self._prepare(temp)
            with patch.object(installer, "_start_visible") as start, \
                    patch.object(installer, "_run_install_script", side_effect=RuntimeError("uv failed")), \
                    patch.object(installer.time, "sleep"):
                exit_code = installer.main([str(state_file)])

            self.assertEqual(exit_code, 1)
            start.assert_called_once()
            self.assertEqual((install_root / "software" / "server.py").read_text(encoding="utf-8"), "# old\n")
            self.assertEqual(archive_update.read_installed_version(install_root), "1.0.0")
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(saved["state"], "install_failed")
            self.assertIn("undone", saved["error"])
            self.assertNotIn("staged", saved)

    def test_a_failed_install_names_its_reason_and_keeps_this_attempts_log_lines(self) -> None:
        def failing_install_script(command, **kwargs):
            kwargs["stdout"].write("Installing Study Runner Python dependencies...\n")
            kwargs["stdout"].write(
                "Study Runner setup failed: could not install software/requirements.txt. "
                "Check the internet connection and retry\n"
            )
            kwargs["stdout"].flush()
            return installer.subprocess.CompletedProcess(command, 1)

        with tempfile.TemporaryDirectory() as temp:
            install_root, state_file = self._prepare(temp)
            log_file = state_file.parent / "update-helper.log"
            log_file.write_text("2026-01-01T00:00:00Z an earlier attempt that failed differently\n", encoding="utf-8")
            with patch.object(installer, "_start_visible"), \
                    patch.object(installer.subprocess, "run", side_effect=failing_install_script), \
                    patch.object(installer.time, "sleep"):
                exit_code = installer.main([str(state_file)])

            self.assertEqual(exit_code, 1)
            saved = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn("could not install software/requirements.txt", saved["error"])
            self.assertTrue(any("setup failed" in line for line in saved["log_tail"]))
            self.assertFalse(any("earlier attempt" in line for line in saved["log_tail"]))
            self.assertEqual(Path(saved["log_file"]), log_file.resolve())

    def test_a_locked_file_during_the_swap_restarts_the_old_version(self) -> None:
        real_replace = archive_update.os.replace
        calls = {"n": 0}

        def replace_failing_on_third_move(source, target):
            calls["n"] += 1
            if calls["n"] == 3:
                raise PermissionError("file in use")
            return real_replace(source, target)

        with tempfile.TemporaryDirectory() as temp:
            install_root, state_file = self._prepare(temp)
            with patch.object(installer, "_start_visible") as start, \
                    patch.object(installer, "_run_install_script") as install, \
                    patch.object(archive_update.os, "replace", side_effect=replace_failing_on_third_move), \
                    patch.object(installer.time, "sleep"):
                exit_code = installer.main([str(state_file)])

            self.assertEqual(exit_code, 1)
            install.assert_not_called()
            start.assert_called_once()  # the operator gets a server back
            self.assertEqual((install_root / "README.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual((install_root / "software" / "server.py").read_text(encoding="utf-8"), "# old\n")
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["state"], "install_failed")


class RollbackTests(unittest.TestCase):
    def test_one_failing_step_does_not_stop_the_others_and_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            install_root = base / "install"
            backup_root = base / "backup"
            for name in ("a", "b"):
                (backup_root / name).mkdir(parents=True)
                (backup_root / name / "old.txt").write_text("old", encoding="utf-8")
            install_root.mkdir()
            journal = [{"action": "backed_up", "path": "a"}, {"action": "backed_up", "path": "b"}]
            real_replace = archive_update.os.replace

            def fail_for_b(source, target):
                if Path(source).name == "b":
                    raise PermissionError("locked")
                return real_replace(source, target)

            with patch.object(archive_update.os, "replace", side_effect=fail_for_b):
                with self.assertRaisesRegex(archive_update.ArchiveUpdateError, "could not be fully restored: b"):
                    archive_update.rollback(install_root, backup_root, journal)
            self.assertTrue((install_root / "a" / "old.txt").is_file())


class LongPathTests(unittest.TestCase):
    def test_fs_path_uses_the_extended_length_form_on_windows_only(self) -> None:
        with patch.object(archive_update.os, "name", "nt"), \
                patch.object(archive_update.os.path, "abspath", side_effect=lambda text: text):
            self.assertEqual(str(archive_update.fs_path(r"C:\x\y")), r"\\?\C:\x\y")
            self.assertEqual(str(archive_update.fs_path(r"\\srv\share\y")), r"\\?\UNC\srv\share\y")
            self.assertEqual(str(archive_update.fs_path(r"\\?\C:\z")), r"\\?\C:\z")

    @unittest.skipUnless(sys.platform == "win32", "extended-length paths are a Windows feature")
    def test_staging_works_beyond_260_characters(self) -> None:
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            deep_name = "d" * 120
            tree = _release_tree(base / "src" / "MRG-StudyRunner-1.1.0", "1.1.0", "new")
            archive = base / "study-runner-source.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                for path in tree.rglob("*"):
                    if path.is_file():
                        handle.write(path, path.relative_to(base / "src").as_posix())
                handle.writestr(f"MRG-StudyRunner-1.1.0/software/saved_results/{deep_name}/{deep_name}/r.json", "{}")
            staging = base / ("s" * 40) / "update-staging"
            root = archive_update.stage_archive(archive, archive_update.sha256_file(archive), staging, "1.1.0")
            deep = archive_update.fs_path(root / "software" / "saved_results" / deep_name / deep_name / "r.json")
            self.assertGreater(len(str(deep)), 300)
            self.assertTrue(deep.is_file())


class ArchiveStagingTests(unittest.TestCase):
    def test_wrong_checksum_and_unsafe_paths_are_rejected(self) -> None:
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            archive = base / "study-runner-source.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("MRG-StudyRunner-1.1.0/../evil.txt", "x")
            with self.assertRaisesRegex(archive_update.ArchiveUpdateError, "damaged"):
                archive_update.stage_archive(archive, "0" * 64, base / "staging", "1.1.0")
            with self.assertRaisesRegex(archive_update.ArchiveUpdateError, "unsafe path"):
                archive_update.stage_archive(archive, archive_update.sha256_file(archive), base / "staging", "1.1.0")

    def test_valid_archive_is_staged_and_version_checked(self) -> None:
        import zipfile
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            tree = _release_tree(base / "src" / "MRG-StudyRunner-1.1.0", "1.1.0", "new")
            archive = base / "study-runner-source.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                for path in tree.rglob("*"):
                    if path.is_file():
                        handle.write(path, path.relative_to(base / "src").as_posix())
            digest = archive_update.sha256_file(archive)
            root = archive_update.stage_archive(archive, digest, base / "staging", "1.1.0")
            self.assertTrue((root / "software" / "server.py").is_file())
            with self.assertRaisesRegex(archive_update.ArchiveUpdateError, "expected 1.2.0"):
                archive_update.stage_archive(archive, digest, base / "staging2", "1.2.0")

    def test_install_kind_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(archive_update.install_kind(root), "unknown")
            (root / archive_update.RELEASE_INFO_NAME).write_text("{}", encoding="utf-8")
            self.assertEqual(archive_update.install_kind(root), "archive")
            (root / ".git").mkdir()
            self.assertEqual(archive_update.install_kind(root), "git")

    def test_a_study_runner_tree_without_marker_is_an_archive_install(self) -> None:
        # GitHub's automatic "Source code" archive has no release marker.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "software" / "study_runner").mkdir(parents=True)
            (root / "software" / "server.py").write_text("", encoding="utf-8")
            (root / "tools").mkdir()
            (root / "tools" / "install-macos.sh").write_text("", encoding="utf-8")
            self.assertEqual(archive_update.install_kind(root), "unknown")
            (root / "software" / "study_runner" / "version.py").write_text(
                '"""Version."""\n\n__version__ = "1.3.2"\n', encoding="utf-8"
            )
            self.assertEqual(archive_update.install_kind(root), "archive")
            # Without a marker the installed version comes from the program itself.
            self.assertEqual(archive_update.read_installed_version(root), "1.3.2")
            (root / archive_update.RELEASE_INFO_NAME).write_text('{"version": "1.4.0"}', encoding="utf-8")
            self.assertEqual(archive_update.read_installed_version(root), "1.4.0")


if __name__ == "__main__":
    unittest.main()
