"""Thin operator API for persistent session finalization jobs."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from flask import Blueprint, current_app, jsonify, request

from study_runner.runtime_core.settings.folder_open_service import FolderOpenError, open_session_folder, resolve_session_folder
from study_runner.runtime_core.delivery.finalization_service import (
    FinalizationError,
    FinalizationNotFoundError,
    InvalidTransitionError,
)


bp = Blueprint("finalization", __name__)


def _service():
    return current_app.config["FINALIZATION_SERVICE"]


@bp.route("/api/finalization/status", methods=["GET"])
def finalization_status():
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        return jsonify({"ok": False, "error": "days must be a whole number."}), 400
    if not 1 <= days <= 365:
        return jsonify({"ok": False, "error": "days must be between 1 and 365."}), 400
    return jsonify(_service().status(days=days))


@bp.route("/api/finalization/<job_id>", methods=["GET"])
def finalization_job(job_id: str):
    try:
        return jsonify({"ok": True, "job": _with_artifacts(_service().get(job_id))})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404


@bp.route("/api/finalization/<job_id>/retry-target", methods=["GET"])
def finalization_retry_target(job_id: str):
    """Compare a failed step's frozen destination with its current one.

    The operator sees this before confirming a retry, so a Notion page (or
    any other destination setting) that changed since the session ended is
    a choice, not a silent replay of the old target.
    """
    try:
        return jsonify({"ok": True, **_service().describe_retry_target(
            job_id, step_key=str(request.args.get("step") or ""),
        )})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except InvalidTransitionError as error:
        return jsonify({"ok": False, "error": str(error)}), 409


@bp.route("/api/finalization/<job_id>/retry", methods=["POST"])
def retry_finalization(job_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        job = _service().retry(
            job_id,
            step_key=str(payload.get("step") or ""),
            retry_target=str(payload.get("target") or "snapshot"),
        )
        return jsonify({"ok": True, "job": job})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except (InvalidTransitionError, FinalizationError) as error:
        return jsonify({"ok": False, "error": str(error)}), 409


@bp.route("/api/finalization/<job_id>/republish", methods=["POST"])
def republish_finalization(job_id: str):
    """Upload a finished session to one destination again, with current settings."""
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "job": _service().republish(job_id, step_key=str(payload.get("step") or ""))})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except (InvalidTransitionError, FinalizationError) as error:
        return jsonify({"ok": False, "error": str(error)}), 409


@bp.route("/api/finalization/<job_id>/acknowledge", methods=["POST"])
def acknowledge_finalization(job_id: str):
    try:
        return jsonify({"ok": True, "job": _service().acknowledge_attention(job_id)})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except (InvalidTransitionError, FinalizationError) as error:
        return jsonify({"ok": False, "error": str(error)}), 409


@bp.route("/api/finalization/<job_id>/confirm-degraded", methods=["POST"])
def confirm_degraded_finalization(job_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        job = _service().confirm_degraded(
            job_id,
            reason=str(payload.get("reason") or ""),
            confirmed_by=str(payload.get("confirmed_by") or "admin"),
        )
        return jsonify({"ok": True, "job": job})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except (InvalidTransitionError, FinalizationError) as error:
        return jsonify({"ok": False, "error": str(error)}), 409


@bp.route("/api/finalization/<job_id>/continue", methods=["POST"])
def continue_finalization(job_id: str):
    """Finish a session that an earlier version stopped after "confirm degraded"."""
    payload = request.get_json(silent=True) or {}
    try:
        job = _service().continue_processing(
            job_id,
            confirmed_by=str(payload.get("confirmed_by") or "admin"),
        )
        return jsonify({"ok": True, "job": job})
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except (InvalidTransitionError, FinalizationError) as error:
        return jsonify({"ok": False, "error": str(error)}), 409


@bp.route("/api/finalization/<job_id>/open-folder", methods=["POST"])
def open_finalization_folder(job_id: str):
    """Open only the exact session directory referenced by a durable job."""

    try:
        job = _service().get(job_id)
        return jsonify(
            open_session_folder(
                current_app.config["DATA_DIR"],
                str(job.get("session_path") or ""),
            )
        )
    except FinalizationNotFoundError as error:
        return jsonify({"ok": False, "error": str(error)}), 404
    except FolderOpenError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


def _with_artifacts(job: dict) -> dict:
    """Add a small, read-only artifact inventory to the public job shape.

    The canonical manifest already owns checksums and provenance.  Polling the
    admin monitor must not re-hash large XDFs, so this route only reads that
    manifest and adds the mutable completion/control files that exist now.
    """

    enriched = dict(job)
    try:
        root = resolve_session_folder(
            current_app.config["DATA_DIR"],
            str(enriched.get("session_path") or ""),
        )
    except FolderOpenError:
        enriched["artifacts"] = []
        return enriched

    artifacts: list[dict] = []
    manifest_path = root / "meta" / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("Artifact manifest must be an object.")
            for item in manifest.get("artifacts", []):
                safe = _public_artifact(item)
                if safe is not None:
                    artifacts.append(safe)
        except (OSError, ValueError, TypeError):
            pass

    known_paths = {item["path"] for item in artifacts}
    for name, role in (
        ("answers/submission.json", "submission"),
        ("answers/result.json", "result"),
        ("answers/card-summary.json", "card_summary"),
        ("meta/session-identity.json", "session_identity"),
        ("meta/manifest.json", "manifest"),
        ("meta/checksums.sha256", "checksums"),
        ("COMPLETE.json", "completion_marker"),
        ("ATTENTION_REQUIRED.json", "attention_marker"),
    ):
        path = root / name
        if name in known_paths or not path.is_file():
            continue
        try:
            size_bytes = path.stat().st_size
        except OSError:
            continue
        artifacts.append({"path": name, "role": role, "size_bytes": size_bytes, "local_present": True})

    enriched["artifacts"] = sorted(artifacts, key=lambda item: item["path"])
    return enriched


def _public_artifact(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    normalized = str(item.get("path") or "").strip().replace("\\", "/")
    relative = PurePosixPath(normalized)
    if not normalized or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    public = {
        "path": relative.as_posix(),
        "role": str(item.get("role") or "session_artifact"),
        "local_present": item.get("local_present") is not False,
    }
    for key in ("size_bytes", "sha256", "remote_verified", "remote_sha256"):
        if key in item:
            public[key] = item[key]
    return public
