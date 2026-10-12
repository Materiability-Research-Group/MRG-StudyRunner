from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urljoin, urlparse
import zipfile

import requests

from study_runner.updates import archive_update
from study_runner.updates import signatures as update_signatures
from study_runner.updates.signatures import UPDATER_SCHEMA_VERSION
from study_runner.version import __version__


DEFAULT_MANIFEST_URL = (
    "https://github.com/realfabianschmidt/MRG-StudyRunner/releases/latest/download/"
    "study-runner-python-latest.json"
)
# Source-mode updates (git checkout, no packaged build) use a different,
# already-published file instead of the signed Python-package manifest above
# -- see release_tools/build_source_release.py, which writes this file for
# every tagged release.
DEFAULT_SOURCE_RELEASE_URL = (
    "https://github.com/realfabianschmidt/MRG-StudyRunner/releases/latest/download/"
    "study-runner-source-release.json"
)
# Tagged releases only ever land on this branch (see release.ps1); pulling on
# any other branch would not be "updating to the latest release" at all.
SOURCE_RELEASE_BRANCH = "main"
STATE_FILE_NAME = "update-state.json"
MANIFEST_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 30
INSTALL_SCRIPT_TIMEOUT_SECONDS = 1800


class UpdateError(Exception):
    """Raised when update state, metadata, or downloaded assets are invalid."""


@dataclass(frozen=True)
class UpdatePaths:
    root: Path
    downloads: Path
    staged: Path
    state_file: Path


def build_update_status(app_config: dict[str, Any]) -> dict[str, Any]:
    paths = resolve_update_paths(app_config)
    key_status = get_public_key_status()
    state = _read_state(paths.state_file)
    status = _base_status(app_config, key_status)
    status.update(
        {
            "state": state.get("state", "idle"),
            "checked_at": state.get("checked_at", ""),
            "error": state.get("error", ""),
            "update": state.get("update"),
            "download": state.get("download"),
            "staged": _public_staged(state.get("staged")),
            # What the restart helper logged during a failed install, so the
            # operator sees the reason without looking for the file.
            "log_tail": [str(line) for line in state.get("log_tail") or []][-40:],
            "log_file": str(state.get("log_file") or ""),
            "install_supported": is_install_supported(app_config),
            "install_kind": (
                "packaged" if getattr(sys, "frozen", False)
                else archive_update.install_kind(_install_root(app_config))
            ),
        }
    )
    return status


def _install_root(app_config: dict[str, Any]) -> Path:
    """The folder the operator extracted or cloned (the parent of software/)."""
    return Path(app_config.get("BASE_DIR") or ".").resolve().parent


def check_for_update(app_config: dict[str, Any]) -> dict[str, Any]:
    if not getattr(sys, "frozen", False):
        return _check_for_source_update(app_config)
    _require_public_key()
    paths = resolve_update_paths(app_config)
    manifest_url = get_manifest_url()
    manifest = fetch_manifest(manifest_url)
    platform_key = detect_platform_key()
    asset = select_platform_asset(manifest, platform_key)
    latest_version = str(manifest["version"])
    available = compare_versions(latest_version, __version__) > 0

    state = {
        "state": "available" if available else "current",
        "checked_at": _utc_now(),
        "current_version": __version__,
        "platform": platform_key,
        "manifest_url": manifest_url,
        "update": {
            "available": available,
            "version": latest_version,
            "notes_url": str(manifest.get("notes_url") or ""),
            "asset": _public_asset(asset),
        },
        "asset": asset,
        "error": "",
    }
    _write_state(paths.state_file, state)
    return build_update_status(app_config)


def download_and_stage_update(app_config: dict[str, Any]) -> dict[str, Any]:
    if not getattr(sys, "frozen", False):
        kind = archive_update.install_kind(_install_root(app_config))
        if kind == "archive":
            return _stage_archive_update(app_config)
        if kind == "git":
            return _apply_source_update(app_config)
        raise UpdateError(
            "This folder is not a Study Runner installation that can update itself. Download "
            "study-runner-source.tar.gz (macOS) or study-runner-source.zip (Windows) from the "
            "release page and install it -- see docs/release-and-update.md."
        )
    _require_public_key()
    paths = resolve_update_paths(app_config)
    state = _read_state(paths.state_file)
    update_info = state.get("update") if isinstance(state.get("update"), dict) else {}
    if not update_info.get("available"):
        raise UpdateError("No checked update is available. Check for updates first.")

    asset = state.get("asset") if isinstance(state.get("asset"), dict) else None
    if not asset:
        raise UpdateError("The checked update has no downloadable asset.")

    version = str(update_info.get("version") or "")
    platform_key = str(state.get("platform") or detect_platform_key())
    file_name = _asset_file_name(asset)
    download_path = paths.downloads / file_name

    paths.downloads.mkdir(parents=True, exist_ok=True)
    paths.staged.mkdir(parents=True, exist_ok=True)

    _set_download_state(
        paths.state_file,
        state,
        {
            "state": "downloading",
            "file_name": file_name,
            "bytes_downloaded": 0,
            "total_bytes": int(asset.get("size") or 0),
        },
    )
    sha256 = _download_asset(asset, download_path, paths.state_file, state)

    expected_sha256 = str(asset.get("sha256") or "").strip().lower()
    if sha256 != expected_sha256:
        _set_error(paths.state_file, state, f"Downloaded file hash mismatch for {file_name}.")
        raise UpdateError("Downloaded update did not match the expected SHA-256 hash.")

    verify_asset_signature(version, platform_key, asset)
    _set_download_state(
        paths.state_file,
        state,
        {
            "state": "verifying",
            "file_name": file_name,
            "bytes_downloaded": download_path.stat().st_size,
            "total_bytes": download_path.stat().st_size,
            "sha256": sha256,
        },
    )

    staged_info = _stage_zip(download_path, paths.staged, version)
    state = _read_state(paths.state_file)
    state.update(
        {
            "state": "staged",
            "error": "",
            "download": {
                "state": "staged",
                "file_name": file_name,
                "bytes_downloaded": download_path.stat().st_size,
                "total_bytes": download_path.stat().st_size,
                "sha256": sha256,
            },
            "staged": staged_info,
        }
    )
    _write_state(paths.state_file, state)
    return build_update_status(app_config)


def request_update_install(app_config: dict[str, Any]) -> dict[str, Any]:
    if not getattr(sys, "frozen", False):
        return _request_source_restart(app_config)  # git and archive share the restart path
    if not is_install_supported(app_config):
        raise UpdateError("Install/restart is only available in Python packaged builds, not source mode or legacy desktop mode.")

    paths = resolve_update_paths(app_config)
    state = _read_state(paths.state_file)
    staged = state.get("staged") if isinstance(state.get("staged"), dict) else None
    if not staged:
        raise UpdateError("No staged update is ready to install.")
    executable = Path(str(staged.get("executable") or ""))
    if not executable.exists():
        raise UpdateError("The staged update executable is missing.")

    helper = {
        "storage_root": str(app_config.get("STORAGE_ROOT", "")),
        "host": str(app_config.get("SERVER_HOST", "")),
        "port": str(app_config.get("SERVER_PORT", "")),
        "https": os.getenv("STUDY_RUNNER_HTTPS", ""),
    }
    state["state"] = "installing"
    state["helper"] = helper
    state["install_requested_at"] = _utc_now()
    state["error"] = ""
    _write_state(paths.state_file, state)
    _spawn_installer(paths.state_file, app_config)
    return build_update_status(app_config)


# --------------------------------------------------------------------------
# Source mode: git checkout, no packaged build.
#
# A source checkout has no signed executable to download, so it cannot use
# the packaged flow above at all. Instead it verifies itself the same way an
# operator following docs/release-and-update.md would by hand: fetch the
# already-published release metadata, `git pull --ff-only`, then re-run the
# platform install script. The three functions below are called from
# check_for_update / download_and_stage_update / request_update_install --
# the same three HTTP endpoints and the same admin panel, just a different
# path once inside.
# --------------------------------------------------------------------------

def _check_for_source_update(app_config: dict[str, Any]) -> dict[str, Any]:
    """Compare this checkout's version against the latest tagged release.

    No signing key is involved: a source update is trusted through git and
    the GitHub release itself, not through the Python-package signature
    scheme the packaged flow above uses.
    """
    paths = resolve_update_paths(app_config)
    url = get_source_release_url()
    metadata = fetch_source_release_metadata(url)
    latest_version = str(metadata.get("version") or "").strip()
    if not _is_semver(latest_version):
        raise UpdateError("The latest release has no valid version number.")
    available = compare_versions(latest_version, __version__) > 0
    tag = str(metadata.get("tag") or f"app-v{latest_version}")

    state = {
        "state": "available" if available else "current",
        "checked_at": _utc_now(),
        "current_version": __version__,
        "update": {
            "available": available,
            "version": latest_version,
            "notes_url": f"https://github.com/realfabianschmidt/MRG-StudyRunner/releases/tag/{tag}",
            "asset": None,
        },
        "error": "",
    }
    _write_state(paths.state_file, state)
    return build_update_status(app_config)


def _apply_source_update(app_config: dict[str, Any]) -> dict[str, Any]:
    """Update this checkout in place: `git pull --ff-only`, then the install script.

    Fails closed at the first problem it finds -- an active study session, a
    checkout that is not a git clone, the wrong branch, or local changes to
    tracked files -- and changes nothing on disk before that point. Once the
    pull itself starts, `--ff-only` is what keeps it safe: git either
    fast-forwards cleanly or refuses and leaves the checkout exactly as it
    was, never a half-merged tree.
    """
    base_dir = Path(app_config.get("BASE_DIR") or ".").resolve()
    repo_root = base_dir.parent
    paths = resolve_update_paths(app_config)
    state = _read_state(paths.state_file)
    update_info = state.get("update") if isinstance(state.get("update"), dict) else {}
    if not update_info.get("available"):
        raise UpdateError("No checked update is available. Check for updates first.")

    if not (repo_root / ".git").is_dir():
        raise UpdateError(
            "This checkout is not a git clone, so it cannot update itself here. "
            "Download a fresh release archive instead -- see docs/release-and-update.md."
        )
    branch = _current_git_branch(repo_root)
    if branch != SOURCE_RELEASE_BRANCH:
        raise UpdateError(
            f"This checkout is on branch '{branch}', not '{SOURCE_RELEASE_BRANCH}'. "
            f"Only '{SOURCE_RELEASE_BRANCH}' receives tagged releases; switch "
            "branches and update manually if that is intended."
        )
    dirty = _git_tracked_changes(repo_root)
    if dirty:
        raise UpdateError(
            "This checkout has local changes to tracked files. Commit, stash, "
            "or discard them before updating."
        )

    state["state"] = "downloading"
    state["error"] = ""
    _write_state(paths.state_file, state)

    try:
        _run_checked(
            ["git", "pull", "--ff-only"], repo_root, "git pull --ff-only failed",
            timeout=GIT_TIMEOUT_SECONDS,
        )
        _run_checked(
            _install_script_command(repo_root), repo_root, "install script failed",
            timeout=INSTALL_SCRIPT_TIMEOUT_SECONDS,
        )
    except UpdateError as error:
        _set_error(paths.state_file, state, str(error))
        raise

    # No "executable" key here, unlike the packaged flow's staged info --
    # source mode restarts by re-running server.py in place, not by
    # launching a separate downloaded executable. installer.py tells the two
    # apart by this "mode" field.
    staged_info = {
        "mode": "source",
        "version": _read_checkout_version(base_dir),
        "staged_at": _utc_now(),
    }
    state = _read_state(paths.state_file)
    state.update({"state": "staged", "error": "", "staged": staged_info})
    _write_state(paths.state_file, state)
    return build_update_status(app_config)


def _stage_archive_update(app_config: dict[str, Any]) -> dict[str, Any]:
    """Download and verify the next release archive while the server keeps running.

    The archive's SHA-256 comes from the published release metadata. Nothing in
    the installation changes here; the swap happens in the restart helper once
    the server has stopped.
    """
    install_root = _install_root(app_config)
    paths = resolve_update_paths(app_config)
    state = _read_state(paths.state_file)
    update_info = state.get("update") if isinstance(state.get("update"), dict) else {}
    if not update_info.get("available"):
        raise UpdateError("No checked update is available. Check for updates first.")

    metadata = fetch_source_release_metadata(get_source_release_url())
    version = str(metadata.get("version") or "").strip()
    if not _is_semver(version) or compare_versions(version, __version__) <= 0:
        raise UpdateError("The published release is not newer than this installation.")
    asset = source_archive_asset(metadata, version)
    archive_name = asset["name"]
    expected_sha256 = asset["sha256"]

    staging_parent = install_root / ".tools" / "update-staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    download_path = staging_parent / archive_name
    _set_download_state(paths.state_file, state, {
        "state": "downloading", "file_name": archive_name,
        "bytes_downloaded": 0, "total_bytes": asset["size"],
    })
    _download_asset(asset, download_path, paths.state_file, state)
    _set_download_state(paths.state_file, state, {
        "state": "verifying", "file_name": archive_name,
        "bytes_downloaded": download_path.stat().st_size, "total_bytes": download_path.stat().st_size,
    })
    try:
        release_root = archive_update.stage_archive(download_path, expected_sha256, staging_parent, version)
    except archive_update.ArchiveUpdateError as error:
        _set_error(paths.state_file, state, str(error))
        raise UpdateError(str(error)) from error
    except OSError as error:
        message = f"The update could not be unpacked ({error}). Nothing was changed; try again."
        _set_error(paths.state_file, state, message)
        raise UpdateError(message) from error
    finally:
        download_path.unlink(missing_ok=True)

    state = _read_state(paths.state_file)
    state.update({
        "state": "staged",
        "error": "",
        "staged": {
            "mode": "archive",
            "version": version,
            "path": str(release_root),
            "release_root": str(release_root),
            "staged_at": _utc_now(),
        },
    })
    _write_state(paths.state_file, state)
    return build_update_status(app_config)


def _request_source_restart(app_config: dict[str, Any]) -> dict[str, Any]:
    """Restart into the just-updated checkout by re-running `python server.py`.

    Reuses the same detached helper process as the packaged flow
    (`study_runner.updates.installer`) and the same shutdown sequence in the
    HTTP route -- only `installer.py`'s own source-mode branch differs,
    since there is no downloaded executable to launch here.
    """
    base_dir = Path(app_config.get("BASE_DIR") or ".").resolve()
    paths = resolve_update_paths(app_config)
    state = _read_state(paths.state_file)
    staged = state.get("staged") if isinstance(state.get("staged"), dict) else None
    if not staged:
        raise UpdateError("No update is ready to restart into.")

    state["state"] = "installing"
    state["install_requested_at"] = _utc_now()
    state["error"] = ""
    state["source_restart"] = {
        "base_dir": str(base_dir),
        "install_root": str(base_dir.parent),
        "port": str(app_config.get("SERVER_PORT") or ""),
        "server_pid": os.getpid(),
    }
    _write_state(paths.state_file, state)
    _spawn_installer(paths.state_file, app_config)
    return build_update_status(app_config)


def source_archive_asset(metadata: dict[str, Any], version: str) -> dict[str, Any]:
    """Name, URL, SHA-256 and size of this platform's release archive.

    The archive of the published GitHub release, unless the release metadata
    URL was pointed elsewhere (a mirror, or a local server in the release
    acceptance test): then the archive sits next to that metadata file.
    """
    name = archive_update.archive_name_for_platform()
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    artifact = artifacts.get(name) if isinstance(artifacts.get(name), dict) else {}
    sha256 = str(artifact.get("sha256") or "").strip().lower()
    if len(sha256) != 64:
        raise UpdateError(f"The release metadata has no checksum for {name}.")
    release_url = get_source_release_url()
    if release_url != DEFAULT_SOURCE_RELEASE_URL:
        url = urljoin(release_url, name)
    else:
        repository = str(metadata.get("repository") or "realfabianschmidt/MRG-StudyRunner")
        tag = str(metadata.get("tag") or f"app-v{version}")
        url = f"https://github.com/{repository}/releases/download/{tag}/{name}"
    return {"name": name, "url": url, "sha256": sha256, "size": int(artifact.get("size") or 0)}


def recover_interrupted_update(app_config: dict[str, Any]) -> None:
    """At server start: a download or check the previous server process was
    still running cannot be running now, so it must not keep the Update panel
    busy forever. The installed update (``staged``) is not touched."""
    paths = resolve_update_paths(app_config)
    state = _read_state(paths.state_file)
    if state.get("state") in {"downloading", "verifying"}:
        state["state"] = "error"
        state["error"] = "The last update download was interrupted. Check for updates to try again."
        try:
            _write_state(paths.state_file, state)
        except OSError as error:
            print(f"[UPDATE] Could not reset the interrupted update state: {error}")


def get_source_release_url() -> str:
    return os.getenv("STUDY_RUNNER_SOURCE_RELEASE_URL", DEFAULT_SOURCE_RELEASE_URL).strip() or DEFAULT_SOURCE_RELEASE_URL


def fetch_source_release_metadata(url: str) -> dict[str, Any]:
    try:
        response = requests.get(url, timeout=MANIFEST_TIMEOUT_SECONDS, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise UpdateError(f"Could not fetch the latest release metadata: {error}") from error
    except ValueError as error:
        raise UpdateError("Release metadata is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise UpdateError("Release metadata must be a JSON object.")
    return payload


def _current_git_branch(repo_root: Path) -> str:
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    return result.strip()


def _git_tracked_changes(repo_root: Path) -> str:
    # --untracked-files=no: a stray local file (e.g. a scratch note) should
    # not block an update; only edits to files git already tracks should.
    return _run_git(["status", "--porcelain", "--untracked-files=no"], repo_root).strip()


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(f"git {' '.join(args)} failed: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise UpdateError(f"git {' '.join(args)} failed: {detail}" if detail else f"git {' '.join(args)} failed")
    return result.stdout


def _run_checked(cmd: list[str], cwd: Path, error_prefix: str, *, timeout: int) -> None:
    try:
        result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise UpdateError(f"{error_prefix}: timed out after {int(error.timeout)}s") from error
    except OSError as error:
        raise UpdateError(f"{error_prefix}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise UpdateError(f"{error_prefix}: {detail}" if detail else error_prefix)


def _install_script_command(repo_root: Path) -> list[str]:
    if os.name == "nt":
        return [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(repo_root / "tools" / "install-windows.ps1"),
        ]
    return ["bash", str(repo_root / "tools" / "install-macos.sh")]


def _read_checkout_version(base_dir: Path) -> str:
    # Re-read version.py from disk instead of trusting the version string the
    # network response claimed -- this reports what the checkout actually
    # became, which is what matters if the two ever disagreed.
    version_file = base_dir / "study_runner" / "version.py"
    try:
        text = version_file.read_text(encoding="utf-8")
    except OSError as error:
        raise UpdateError(f"Could not read the updated version: {error}") from error
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise UpdateError("Could not read the version from the updated checkout.")
    return match.group(1)


def resolve_update_paths(app_config: dict[str, Any]) -> UpdatePaths:
    storage_root = Path(app_config.get("STORAGE_ROOT") or app_config.get("DATA_DIR") or ".").expanduser().resolve()
    root = storage_root / "updates"
    return UpdatePaths(
        root=root,
        downloads=root / "downloads",
        staged=root / "staged",
        state_file=root / STATE_FILE_NAME,
    )


def get_manifest_url() -> str:
    return os.getenv("STUDY_RUNNER_UPDATE_MANIFEST_URL", DEFAULT_MANIFEST_URL).strip() or DEFAULT_MANIFEST_URL


def get_public_key_status() -> dict[str, Any]:
    try:
        keys = load_public_keys()
    except UpdateError as error:
        return {"configured": False, "error": str(error)}
    return {"configured": bool(keys), "error": ""}


def load_public_keys() -> list:
    try:
        return update_signatures.load_trusted_public_keys()
    except update_signatures.SignatureVerificationError as error:
        raise UpdateError(str(error)) from error


def fetch_manifest(manifest_url: str) -> dict[str, Any]:
    try:
        response = requests.get(manifest_url, timeout=MANIFEST_TIMEOUT_SECONDS, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as error:
        raise UpdateError(f"Could not fetch update manifest: {error}") from error
    except ValueError as error:
        raise UpdateError("Update manifest is not valid JSON.") from error
    return normalize_manifest(payload)


def normalize_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UpdateError("Update manifest must be a JSON object.")

    version = str(payload.get("version") or "").strip()
    if not _is_semver(version):
        raise UpdateError("Update manifest has no valid version.")

    minimum_updater_version = int(payload.get("minimum_updater_version") or 1)
    if minimum_updater_version > UPDATER_SCHEMA_VERSION:
        raise UpdateError("This update requires a newer Study Runner updater.")

    assets = payload.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise UpdateError("Update manifest has no platform assets.")

    normalized_assets: dict[str, dict[str, Any]] = {}
    for platform_key, asset in assets.items():
        if not isinstance(asset, dict):
            continue
        normalized_assets[str(platform_key)] = normalize_asset(asset)
    if not normalized_assets:
        raise UpdateError("Update manifest has no usable platform assets.")

    return {
        "version": version,
        "notes_url": str(payload.get("notes_url") or ""),
        "minimum_updater_version": minimum_updater_version,
        "assets": normalized_assets,
    }


def normalize_asset(asset: dict[str, Any]) -> dict[str, Any]:
    url = str(asset.get("url") or "").strip()
    sha256 = str(asset.get("sha256") or "").strip().lower()
    signature = str(asset.get("signature") or "").strip()
    if not url.startswith(("https://", "http://")):
        raise UpdateError("Update asset has no valid URL.")
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise UpdateError("Update asset has no valid SHA-256 hash.")
    if not signature:
        raise UpdateError("Update asset has no signature.")
    size = int(asset.get("size") or 0)
    return {
        "url": url,
        "sha256": sha256,
        "signature": signature,
        "size": size,
        "file_name": str(asset.get("file_name") or "").strip(),
    }


def select_platform_asset(manifest: dict[str, Any], platform_key: str | None = None) -> dict[str, Any]:
    key = platform_key or detect_platform_key()
    assets = manifest.get("assets") if isinstance(manifest.get("assets"), dict) else {}
    asset = assets.get(key)
    if not asset:
        raise UpdateError(f"No update asset is available for {key}.")
    return dict(asset)


def detect_platform_key() -> str:
    return update_signatures.detect_platform_key()


def compare_versions(left: str, right: str) -> int:
    left_parts = _version_tuple(left)
    right_parts = _version_tuple(right)
    if left_parts > right_parts:
        return 1
    if left_parts < right_parts:
        return -1
    return 0


def canonical_asset_payload(version: str, platform_key: str, asset: dict[str, Any]) -> bytes:
    return update_signatures.canonical_asset_payload(version, platform_key, asset)


def verify_asset_signature(version: str, platform_key: str, asset: dict[str, Any]) -> None:
    try:
        update_signatures.verify_asset_signature(version, platform_key, asset, public_keys=load_public_keys())
    except (update_signatures.SignatureVerificationError, ValueError) as error:
        raise UpdateError("Update asset signature could not be verified.") from error


def is_install_supported(app_config: dict[str, Any]) -> bool:
    if not getattr(sys, "frozen", False):
        # Source mode restarts by re-running `python server.py` from the
        # same checkout -- always possible, no packaged build required.
        return True
    app_mode = str(app_config.get("APP_MODE") or "").strip().lower()
    return app_mode != "desktop"


def _base_status(app_config: dict[str, Any], key_status: dict[str, Any]) -> dict[str, Any]:
    packaged = bool(getattr(sys, "frozen", False))
    source_mode = not packaged
    public_key_configured = bool(key_status.get("configured"))
    configuration_error = ""
    recommended_action = ""
    if source_mode:
        # Source mode verifies an update through git and the release tag
        # instead of a signing key, so it needs no key to be "configured".
        configured = True
    else:
        configured = public_key_configured
        if not configured:
            configuration_error = "This packaged release is missing the trusted Python updater public key."
            recommended_action = "Install a newer release ZIP or rebuild the release with PYTHON_UPDATER_PUBLIC_KEY configured."
    return {
        "ok": True,
        "current_version": __version__,
        "platform": detect_platform_key(),
        "manifest_url": get_manifest_url(),
        "app_mode": str(app_config.get("APP_MODE") or ""),
        "packaged": packaged,
        "source_mode": source_mode,
        "configured": configured,
        "public_key_configured": public_key_configured,
        "configuration_error": configuration_error,
        "recommended_action": recommended_action,
    }


def _require_public_key() -> None:
    # Only reached from the packaged branches below -- source mode returns
    # from its own functions before ever calling this (see check_for_update,
    # download_and_stage_update, request_update_install above).
    status = get_public_key_status()
    if not status.get("configured"):
        message = status.get("error") or "No Python updater public key is configured."
        raise UpdateError(str(message))


def _download_asset(asset: dict[str, Any], destination: Path, state_file: Path, state: dict[str, Any]) -> str:
    hasher = hashlib.sha256()
    bytes_downloaded = 0
    total_bytes = int(asset.get("size") or 0)
    last_state_update = 0.0

    try:
        response = requests.get(asset["url"], stream=True, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
        if not total_bytes:
            total_bytes = int(response.headers.get("content-length") or 0)

        with destination.open("wb") as file_handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                file_handle.write(chunk)
                hasher.update(chunk)
                bytes_downloaded += len(chunk)
                now = time.monotonic()
                if now - last_state_update > 0.5:
                    _set_download_state(
                        state_file,
                        state,
                        {
                            "state": "downloading",
                            "file_name": destination.name,
                            "bytes_downloaded": bytes_downloaded,
                            "total_bytes": total_bytes,
                        },
                    )
                    last_state_update = now
    except (requests.RequestException, OSError) as error:  # OSError: e.g. the disk is full
        _set_error(state_file, state, f"Could not download update: {error}")
        raise UpdateError(f"Could not download update: {error}") from error
    finally:
        close = getattr(locals().get("response", None), "close", None)
        if callable(close):
            close()

    return hasher.hexdigest()


def _stage_zip(zip_path: Path, staged_root: Path, version: str) -> dict[str, Any]:
    target = (staged_root / version).resolve()
    temp_target = (staged_root / f".{version}.tmp").resolve()
    if temp_target.exists():
        shutil.rmtree(temp_target)
    if target.exists():
        shutil.rmtree(target)
    temp_target.mkdir(parents=True, exist_ok=True)

    try:
        _safe_extract_zip(zip_path, temp_target)
        executable = _find_staged_executable(temp_target)
        if os.name != "nt":
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        temp_target.replace(target)
        executable = target / executable.relative_to(temp_target)
        return {
            "version": version,
            "path": str(target),
            "executable": str(executable),
            "staged_at": _utc_now(),
        }
    except Exception:
        if temp_target.exists():
            shutil.rmtree(temp_target)
        raise


def _safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if destination != target and destination not in target.parents:
                raise UpdateError("Update archive contains an unsafe path.")
        archive.extractall(destination)


def _find_staged_executable(stage_dir: Path) -> Path:
    executable_name = "study-runner-server.exe" if os.name == "nt" else "study-runner-server"
    candidates = sorted(
        (path for path in stage_dir.rglob(executable_name) if path.is_file()),
        key=lambda path: (len(path.parts), str(path)),
    )
    if not candidates:
        raise UpdateError(f"Staged update does not contain {executable_name}.")
    return candidates[0]


def _spawn_installer(state_file: Path, app_config: dict[str, Any]) -> None:
    base_dir = Path(app_config.get("BASE_DIR") or ".")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--apply-update", str(state_file)]
    else:
        cmd = [sys.executable, "-m", "study_runner.updates.installer", str(state_file)]

    # The helper outlives this server and its terminal window. Its own output
    # (a crash before it can write the state, say) goes to the helper log,
    # never to a terminal that is about to close.
    log_file = state_file.parent / "update-helper.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as log_handle:
        kwargs: dict[str, Any] = {
            "cwd": str(base_dir), "env": os.environ.copy(), "close_fds": True,
            "stdin": subprocess.DEVNULL, "stdout": log_handle, "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)


def _public_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(asset.get("url") or ""),
        "sha256": str(asset.get("sha256") or ""),
        "size": int(asset.get("size") or 0),
        "file_name": _asset_file_name(asset),
    }


def _public_staged(staged: Any) -> dict[str, Any] | None:
    if not isinstance(staged, dict):
        return None
    return {
        "version": str(staged.get("version") or ""),
        "path": str(staged.get("path") or ""),
        "staged_at": str(staged.get("staged_at") or ""),
    }


def _asset_file_name(asset: dict[str, Any]) -> str:
    explicit = str(asset.get("file_name") or "").strip()
    if explicit:
        return Path(explicit).name
    parsed = urlparse(str(asset.get("url") or ""))
    return Path(parsed.path).name or "study-runner-update.zip"


def _set_download_state(state_file: Path, state: dict[str, Any], download: dict[str, Any]) -> None:
    state["state"] = str(download.get("state") or "downloading")
    state["download"] = download
    state["error"] = ""
    _write_state(state_file, state)


def _set_error(state_file: Path, state: dict[str, Any], message: str) -> None:
    state["state"] = "error"
    state["error"] = message
    _write_state(state_file, state)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as file_handle:
            payload = json.load(file_handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, indent=2, sort_keys=True)
        file_handle.write("\n")


def _version_tuple(value: str) -> tuple[int, int, int, int, str]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?", str(value))
    if match is None:
        return (0, 0, 0, 0, "")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    prerelease = match.group(4)
    return major, minor, patch, int(prerelease is None), prerelease or ""


def _is_semver(value: str) -> bool:
    return re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", str(value)) is not None


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
