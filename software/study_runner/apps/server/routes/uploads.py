"""Background upload status, retries, and local result-folder actions."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from study_runner.runtime_core.settings.folder_open_service import FolderOpenError, open_session_folder
from study_runner.runtime_core.delivery.finalization_service import FinalizationNotFoundError
from study_runner.runtime_core.delivery.upload_jobs_service import (
    DEFAULT_STATUS_DAYS,
    MAX_STATUS_DAYS,
    UploadJobError,
)


bp = Blueprint("uploads", __name__)


def _service():
    return current_app.config["UPLOAD_JOBS_SERVICE"]


@bp.route("/api/uploads/status", methods=["GET"])
def upload_status():
    raw_days = request.args.get("days", str(DEFAULT_STATUS_DAYS))
    try:
        days = int(raw_days)
    except ValueError:
        return jsonify({"ok": False, "error": "days must be a whole number."}), 400
    if not 1 <= days <= MAX_STATUS_DAYS:
        return jsonify({"ok": False, "error": f"days must be between 1 and {MAX_STATUS_DAYS}."}), 400
    status = _service().status(days=days)
    for session in status.get("sessions", []):
        session["orphaned"] = _is_orphaned(session)
    return jsonify(status)


def _is_orphaned(session: dict) -> bool:
    """A queued upload whose session the finalization service no longer knows.

    Every upload made by a finalization carries its job id. When that job
    is gone - the session folder was deleted or withdrawn, or the data folder
    moved - the upload has nothing left to link to, and an admin notice
    pointing at it would only lead to "session not found".
    """
    finalization = current_app.config.get("FINALIZATION_SERVICE")
    if finalization is None:
        return False
    job_ids = {
        str((job.get("metadata") or {}).get("finalization_job_id") or "").strip()
        for job in session.get("jobs", [])
    } - {""}
    if not job_ids:
        return False
    for job_id in job_ids:
        try:
            finalization.get(job_id)
            return False
        except FinalizationNotFoundError:
            continue
    return True


@bp.route("/api/uploads/acknowledge", methods=["POST"])
def upload_acknowledge():
    """Quiet the admin's upload notice for these jobs until they change."""
    payload = request.get_json(silent=True) or {}
    job_ids = payload.get("job_ids")
    if not isinstance(job_ids, list) or not all(isinstance(item, str) for item in job_ids):
        return jsonify({"ok": False, "error": "Send job_ids as a list of strings."}), 400
    return jsonify(_service().acknowledge(job_ids[:500]))


@bp.route("/api/uploads/<job_id>/retry-target", methods=["GET"])
def upload_retry_target(job_id: str):
    """Compare a job's frozen destination settings with the study's current ones."""
    try:
        return jsonify({"ok": True, **_service().describe_retry_target(job_id)})
    except UploadJobError as error:
        return jsonify({"ok": False, "error": str(error)}), 404


@bp.route("/api/uploads/retry", methods=["POST"])
def upload_retry():
    payload = request.get_json(silent=True) or {}
    job_id = str(payload.get("job_id") or "").strip()
    all_failed = payload.get("all_failed") is True
    target = str(payload.get("target") or "snapshot")
    if bool(job_id) == bool(all_failed):
        return jsonify({"ok": False, "error": "Send either job_id or all_failed: true."}), 400
    try:
        return jsonify(_service().retry(job_id=job_id, all_failed=all_failed, target=target))
    except UploadJobError as error:
        return jsonify({"ok": False, "error": str(error)}), 404


@bp.route("/api/admin/system/open-results-folder", methods=["POST"])
def open_results_folder_route():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(
            open_session_folder(
                current_app.config["DATA_DIR"],
                str(payload.get("session_path") or ""),
            )
        )
    except FolderOpenError as error:
        return jsonify({"ok": False, "error": str(error)}), 400
