from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.runtime_core.settings import update_service


class FakeDownloadResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index : index + chunk_size]

    def close(self) -> None:
        return None


class UpdateServiceTests(unittest.TestCase):
    def test_compare_versions(self) -> None:
        self.assertEqual(update_service.compare_versions("0.3.0", "0.2.9"), 1)
        self.assertEqual(update_service.compare_versions("0.2.2", "0.2.2"), 0)
        self.assertEqual(update_service.compare_versions("0.2.1", "0.2.2"), -1)
        self.assertEqual(update_service.compare_versions("1.0.0", "1.0.0-dev"), 1)
        self.assertEqual(update_service.compare_versions("1.0.0-dev", "0.7.0"), 1)

    def test_asset_signature_verification_accepts_generated_key(self) -> None:
        private_key, public_key = _make_keypair()
        asset = _signed_asset(private_key, "0.9.0", "windows-x86_64")

        with patch.dict(os.environ, {"STUDY_RUNNER_UPDATE_PUBLIC_KEY": public_key}, clear=False):
            update_service.verify_asset_signature("0.9.0", "windows-x86_64", asset)

    def test_asset_signature_verification_rejects_tampering(self) -> None:
        private_key, public_key = _make_keypair()
        asset = _signed_asset(private_key, "0.9.0", "windows-x86_64")
        asset["sha256"] = "b" * 64

        with patch.dict(os.environ, {"STUDY_RUNNER_UPDATE_PUBLIC_KEY": public_key}, clear=False):
            with self.assertRaises(update_service.UpdateError):
                update_service.verify_asset_signature("0.9.0", "windows-x86_64", asset)

    def test_manifest_normalization_and_platform_selection(self) -> None:
        private_key, _public_key = _make_keypair()
        asset = _signed_asset(private_key, "0.9.0", "linux-x86_64")
        manifest = update_service.normalize_manifest(
            {
                "version": "0.9.0",
                "minimum_updater_version": 1,
                "assets": {"linux-x86_64": asset},
            }
        )

        selected = update_service.select_platform_asset(manifest, "linux-x86_64")
        self.assertEqual(selected["sha256"], asset["sha256"])

    def test_download_and_stage_update_zip(self) -> None:
        private_key, public_key = _make_keypair()
        zip_bytes = _make_update_zip()
        sha256 = update_service.hashlib.sha256(zip_bytes).hexdigest()
        platform_key = update_service.detect_platform_key()
        asset = _signed_asset(
            private_key,
            "9.9.9",
            platform_key,
            sha256=sha256,
            size=len(zip_bytes),
            url="https://example.com/study-runner-server-test.zip",
        )
        manifest = {"version": "9.9.9", "minimum_updater_version": 1, "assets": {platform_key: asset}}

        with tempfile.TemporaryDirectory() as temp_dir:
            app_config = {"STORAGE_ROOT": temp_dir, "APP_MODE": "python", "BASE_DIR": PROJECT_ROOT}
            # The signed-manifest/zip flow below is the packaged-build path
            # (check_for_update/download_and_stage_update both dispatch to
            # the git-based source-mode path instead when sys.frozen is not
            # set); simulate a packaged build the same way
            # test_packaged_update_status_reports_release_key_problem does.
            with patch.object(sys, "frozen", True, create=True):
                with patch.dict(os.environ, {"STUDY_RUNNER_UPDATE_PUBLIC_KEY": public_key}, clear=False):
                    with patch.object(update_service, "fetch_manifest", return_value=update_service.normalize_manifest(manifest)):
                        check_status = update_service.check_for_update(app_config)
                    self.assertTrue(check_status["update"]["available"])

                    with patch.object(update_service.requests, "get", return_value=FakeDownloadResponse(zip_bytes)):
                        staged_status = update_service.download_and_stage_update(app_config)

                    self.assertEqual(staged_status["state"], "staged")
                    self.assertEqual(staged_status["staged"]["version"], "9.9.9")
                    self.assertTrue(Path(staged_status["staged"]["path"]).exists())


class SourceUpdateTests(unittest.TestCase):
    """`download_and_stage_update`/`request_update_install` in a git checkout.

    These drive real `git` subprocesses against a real repository built for
    the test, not mocks -- the whole point of `_apply_source_update` is
    that `git pull --ff-only` itself is what makes it safe, so a mock of
    git would not prove that.
    """

    def setUp(self) -> None:
        self._frozen_patch = patch.object(sys, "frozen", False, create=True)
        self._frozen_patch.start()
        self.addCleanup(self._frozen_patch.stop)

    def _write_checkout_files(self, repo_root: Path, version: str) -> None:
        study_runner = repo_root / "software" / "study_runner"
        study_runner.mkdir(parents=True, exist_ok=True)
        (study_runner / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (repo_root / "software" / "server.py").write_text("# test stub\n", encoding="utf-8")
        tools_dir = repo_root / "tools"
        tools_dir.mkdir(exist_ok=True)
        # A harmless install script: real subprocess, no real install.
        (tools_dir / "install-windows.ps1").write_text("exit 0\n", encoding="utf-8")
        script = tools_dir / "install-macos.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if os.name != "nt":
            script.chmod(0o755)

    def _git(self, repo_root: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(repo_root), *args], check=True, capture_output=True, text=True)

    def _init_repo(self, repo_root: Path, version: str) -> None:
        repo_root.mkdir(parents=True, exist_ok=True)
        self._write_checkout_files(repo_root, version)
        subprocess.run(["git", "init", "--quiet", str(repo_root)], check=True, capture_output=True)
        # Fixed branch name regardless of the machine's init.defaultBranch.
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", "refs/heads/main"], check=True, cwd=str(repo_root), capture_output=True,
        )
        self._git(repo_root, "config", "user.email", "test@example.com")
        self._git(repo_root, "config", "user.name", "Test")
        self._git(repo_root, "add", "-A")
        self._git(repo_root, "commit", "--quiet", "-m", version)

    def _app_config(self, base_dir: Path, storage_root: Path, *, active: bool = False) -> dict:
        config: dict = {"BASE_DIR": base_dir, "STORAGE_ROOT": str(storage_root)}
        if active:
            config["ACTIVE_STUDY_HARDWARE_CONFIG"] = {"brainbit": {"enabled": True}}
        return config

    def _mark_update_available(self, app_config: dict, version: str) -> None:
        paths = update_service.resolve_update_paths(app_config)
        update_service._write_state(
            paths.state_file,
            {"state": "available", "update": {"available": True, "version": version, "notes_url": "", "asset": None}},
        )

    def test_refuses_when_no_update_was_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._init_repo(root / "repo", "1.0.0")
            app_config = self._app_config(root / "repo" / "software", root / "storage")
            with self.assertRaisesRegex(update_service.UpdateError, "Check for updates first"):
                update_service.download_and_stage_update(app_config)

    def test_an_active_study_session_no_longer_blocks_the_update(self) -> None:
        # The install route ends the run and aborts the session itself (after
        # the operator's confirmation), so the checkout step does not refuse.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._init_repo(root / "repo", "1.0.0")
            app_config = self._app_config(root / "repo" / "software", root / "storage", active=True)
            self._mark_update_available(app_config, "1.1.0")
            with self.assertRaises(update_service.UpdateError) as raised:
                update_service.download_and_stage_update(app_config)  # fails later: no remote to pull
            self.assertNotIn("session is active", str(raised.exception))

    def test_a_study_runner_folder_without_git_or_marker_updates_as_an_archive(self) -> None:
        # GitHub's automatic "Source code" archive and pre-marker installs.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_checkout_files(root / "repo", "1.0.0")  # no git init, no marker
            app_config = self._app_config(root / "repo" / "software", root / "storage")
            self._mark_update_available(app_config, "1.1.0")
            with patch.object(update_service, "_stage_archive_update", return_value={"state": "ready"}) as staged:
                result = update_service.download_and_stage_update(app_config)
            staged.assert_called_once_with(app_config)
            self.assertEqual(result, {"state": "ready"})

    def test_refuses_a_folder_that_is_not_a_study_runner_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "repo" / "software").mkdir(parents=True)
            app_config = self._app_config(root / "repo" / "software", root / "storage")
            self._mark_update_available(app_config, "1.1.0")
            with self.assertRaisesRegex(update_service.UpdateError, "not a Study Runner installation"):
                update_service.download_and_stage_update(app_config)

    def test_refuses_a_branch_other_than_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._init_repo(root / "repo", "1.0.0")
            self._git(root / "repo", "checkout", "-b", "feature/other")
            app_config = self._app_config(root / "repo" / "software", root / "storage")
            self._mark_update_available(app_config, "1.1.0")
            with self.assertRaisesRegex(update_service.UpdateError, "not 'main'"):
                update_service.download_and_stage_update(app_config)

    def test_refuses_local_changes_to_tracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._init_repo(root / "repo", "1.0.0")
            (root / "repo" / "software" / "server.py").write_text("# edited locally\n", encoding="utf-8")
            app_config = self._app_config(root / "repo" / "software", root / "storage")
            self._mark_update_available(app_config, "1.1.0")
            with self.assertRaisesRegex(update_service.UpdateError, "local changes"):
                update_service.download_and_stage_update(app_config)

    def test_pulls_a_real_fast_forward_and_reruns_the_install_script(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True, capture_output=True)

            seed = root / "seed"
            self._init_repo(seed, "1.0.0")
            self._git(seed, "remote", "add", "origin", str(remote))
            self._git(seed, "push", "-u", "origin", "main")

            subprocess.run(
                # --branch pins the checkout explicitly: a fresh bare repo's
                # own HEAD symref may still point at whatever
                # init.defaultBranch was on this machine (often "master",
                # which was never pushed), and clone would otherwise leave
                # an empty, branch-less working tree.
                ["git", "clone", "--quiet", "--branch", "main", str(remote), str(root / "local")],
                check=True, capture_output=True,
            )
            local_repo = root / "local"
            self._git(local_repo, "config", "user.email", "test@example.com")
            self._git(local_repo, "config", "user.name", "Test")

            # A second commit lands on the shared remote, as a real release
            # would -- local is now one fast-forwardable commit behind it.
            self._write_checkout_files(seed, "1.1.0")
            self._git(seed, "add", "-A")
            self._git(seed, "commit", "--quiet", "-m", "1.1.0")
            self._git(seed, "push", "origin", "main")

            base_dir = local_repo / "software"
            app_config = self._app_config(base_dir, root / "storage")
            self._mark_update_available(app_config, "1.1.0")

            status = update_service.download_and_stage_update(app_config)

            self.assertEqual(status["state"], "staged")
            self.assertEqual(status["staged"]["version"], "1.1.0")
            self.assertEqual(
                (base_dir / "study_runner" / "version.py").read_text(encoding="utf-8"),
                '__version__ = "1.1.0"\n',
            )

    def test_restart_is_refused_without_a_staged_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._init_repo(root / "repo", "1.0.0")
            app_config = self._app_config(root / "repo" / "software", root / "storage")
            with self.assertRaisesRegex(update_service.UpdateError, "No update is ready"):
                update_service.request_update_install(app_config)


class ArchiveUpdateServiceTests(unittest.TestCase):
    """The release-archive path of the in-app updater (no git, no packaged build)."""

    METADATA = {
        "version": "9.9.9", "repository": "owner/repo", "tag": "app-v9.9.9",
        "artifacts": {
            "study-runner-source.zip": {"sha256": "a" * 64, "size": 10},
            "study-runner-source.tar.gz": {"sha256": "b" * 64, "size": 20},
        },
    }

    def test_the_archive_comes_from_the_github_release_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STUDY_RUNNER_SOURCE_RELEASE_URL", None)
            asset = update_service.source_archive_asset(self.METADATA, "9.9.9")
        self.assertEqual(asset["url"], f"https://github.com/owner/repo/releases/download/app-v9.9.9/{asset['name']}")
        self.assertEqual(len(asset["sha256"]), 64)

    def test_an_overridden_release_url_serves_the_archive_next_to_its_metadata(self) -> None:
        with patch.dict(os.environ, {"STUDY_RUNNER_SOURCE_RELEASE_URL": "http://127.0.0.1:8765/release.json"}):
            asset = update_service.source_archive_asset(self.METADATA, "9.9.9")
        self.assertEqual(asset["url"], f"http://127.0.0.1:8765/{asset['name']}")

    def test_metadata_without_a_checksum_is_refused(self) -> None:
        with self.assertRaisesRegex(update_service.UpdateError, "no checksum"):
            update_service.source_archive_asset({"version": "9.9.9", "artifacts": {}}, "9.9.9")

    def test_an_interrupted_download_does_not_keep_the_panel_busy(self) -> None:
        for interrupted in ("downloading", "verifying"):
            with tempfile.TemporaryDirectory() as temp:
                app_config = {"STORAGE_ROOT": temp}
                state_file = update_service.resolve_update_paths(app_config).state_file
                update_service._write_state(state_file, {"state": interrupted})
                update_service.recover_interrupted_update(app_config)
                state = update_service._read_state(state_file)
                self.assertEqual(state["state"], "error")
                self.assertIn("interrupted", state["error"])

    def test_a_failed_install_shows_its_log_lines_in_the_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app_config = {"STORAGE_ROOT": temp, "BASE_DIR": str(Path(temp) / "software")}
            state_file = update_service.resolve_update_paths(app_config).state_file
            update_service._write_state(state_file, {
                "state": "install_failed",
                "error": "The update could not be installed and was undone: install script exited with code 1",
                "log_tail": ["Study Runner setup failed: could not download uv"],
                "log_file": str(state_file.parent / "update-helper.log"),
            })
            status = update_service.build_update_status(app_config)
            self.assertEqual(status["log_tail"], ["Study Runner setup failed: could not download uv"])
            self.assertTrue(status["log_file"].endswith("update-helper.log"))

    def test_the_helper_writes_its_own_output_to_the_helper_log(self) -> None:
        # Never to the terminal of the server that is about to exit.
        with tempfile.TemporaryDirectory() as temp:
            state_file = Path(temp) / "updates" / "update-state.json"
            with patch.object(update_service.subprocess, "Popen") as popen:
                update_service._spawn_installer(state_file, {"BASE_DIR": temp})
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            self.assertTrue(kwargs["stdout"].name.endswith("update-helper.log"))
            self.assertTrue((state_file.parent / "update-helper.log").exists())

    def test_other_states_are_left_alone_at_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            app_config = {"STORAGE_ROOT": temp}
            state_file = update_service.resolve_update_paths(app_config).state_file
            staged = {"state": "staged", "staged": {"mode": "archive", "version": "9.9.9"}}
            update_service._write_state(state_file, staged)
            update_service.recover_interrupted_update(app_config)
            self.assertEqual(update_service._read_state(state_file), staged)

    def test_a_file_system_error_while_unpacking_becomes_a_clear_error_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "install" / "software").mkdir(parents=True)
            (root / "install" / "study-runner-release.json").write_text('{"version": "1.0.0"}', encoding="utf-8")
            app_config = {"BASE_DIR": root / "install" / "software", "STORAGE_ROOT": str(root / "storage")}
            state_file = update_service.resolve_update_paths(app_config).state_file
            update_service._write_state(state_file, {"state": "available", "update": {"available": True}})

            def fake_download(asset, destination, *_args):
                destination.write_bytes(b"zip")
                return ""

            with patch.object(sys, "frozen", False, create=True), \
                    patch.object(update_service, "fetch_source_release_metadata", return_value=self.METADATA), \
                    patch.object(update_service, "_download_asset", side_effect=fake_download), \
                    patch.object(update_service.archive_update, "stage_archive",
                                 side_effect=OSError("path too long")):
                with self.assertRaisesRegex(update_service.UpdateError, "could not be unpacked"):
                    update_service.download_and_stage_update(app_config)
            state = update_service._read_state(state_file)
            self.assertEqual(state["state"], "error")
            self.assertIn("path too long", state["error"])


def _make_keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(public_bytes).decode("ascii")


def _signed_asset(
    private_key: Ed25519PrivateKey,
    version: str,
    platform_key: str,
    *,
    sha256: str = "a" * 64,
    size: int = 123,
    url: str = "https://example.com/study-runner-server.zip",
) -> dict:
    asset = {
        "url": url,
        "sha256": sha256,
        "size": size,
        "file_name": Path(url).name,
    }
    signature = private_key.sign(update_service.canonical_asset_payload(version, platform_key, asset))
    asset["signature"] = base64.b64encode(signature).decode("ascii")
    return asset


def _make_update_zip() -> bytes:
    executable_name = "study-runner-server.exe" if os.name == "nt" else "study-runner-server"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"study-runner-server/{executable_name}", "placeholder executable")
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
