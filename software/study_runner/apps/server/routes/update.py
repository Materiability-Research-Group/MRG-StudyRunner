"""In-app updater endpoints (check, download, install, status).

An update ends everything first: after the operator's double confirmation the
running study run and any recording session are aborted with the reason
"Software update" (captured data stays, exactly as with an admin abort),
sensors stop, and the server exits so the helper can restart it. Finalization
and upload jobs are durable and simply continue after the restart.
"""
import threading

from flask import Blueprint, current_app, jsonify

from study_runner.runtime_core.settings.update_service import (
    UpdateError,
    build_update_status,
    check_for_update,
    download_and_stage_update,
    request_update_install,
)
from .helpers import _abort_study_run, _exit_process_soon, _stop_study_sensor_runtime, _study_run_state
from study_runner.runtime_core.studies.study_config_service import load_config

bp = Blueprint("update", __name__)

UPDATE_ABORT_REASON = "Software update"


@bp.route("/api/admin/update/status")
def admin_update_status():
    return jsonify({**build_update_status(current_app.config), "activity": _activity()})


@bp.route("/api/admin/update/check", methods=["POST"])
def admin_update_check():
    try:
        return jsonify(check_for_update(current_app.config))
    except UpdateError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@bp.route("/api/admin/update/download", methods=["POST"])
def admin_update_download():
    try:
        return jsonify(download_and_stage_update(current_app.config))
    except UpdateError as error:
        return jsonify({"ok": False, "error": str(error)}), 400


@bp.route("/api/admin/update/install", methods=["POST"])
def admin_update_install():
    try:
        result = request_update_install(current_app.config)
    except UpdateError as error:
        return jsonify({"ok": False, "error": str(error)}), 409
    stopped = _stop_everything_for_update()
    threading.Thread(target=_exit_process_soon, daemon=True, name="update-exit").start()
    return jsonify({"ok": True, **result, "stopped": stopped})


def _activity() -> dict:
    """What an update would end right now, for the confirmation dialog."""
    recording = current_app.config.get("RECORDING_RUNTIME_SERVICE")
    session = recording.current_status() if recording is not None else None
    try:
        run_state = _study_run_state(_active_study_id())
    except Exception:
        run_state = {}
    finalization = current_app.config.get("FINALIZATION_SERVICE")
    pending = 0
    if finalization is not None:
        try:
            pending = sum(
                1 for job in finalization.status(days=30).get("jobs", [])
                if job.get("status") in {"queued", "running"}
            )
        except Exception:
            pending = 0
    return {
        "study_run_status": str(run_state.get("status") or ""),
        "study_id": str(run_state.get("study_id") or ""),
        "active_session": bool(session),
        "participant_id": str((session or {}).get("participant_id") or ""),
        "pending_finalizations": pending,
    }


def _active_study_id() -> str:
    """The active study's id without validating the whole study (no plugin calls)."""
    try:
        return str((load_config(current_app.config["CONFIG_FILE"]) or {}).get("study_id") or "")
    except Exception:
        return ""


def _stop_everything_for_update() -> dict:
    """End the study run, abort a recording session, stop sensors. Never raises."""
    stopped: dict = {"session_aborted": False, "run_aborted": False, "sensors_stopped": False, "errors": []}
    recording = current_app.config.get("RECORDING_RUNTIME_SERVICE")
    try:
        current = recording.current_status() if recording is not None else None
        if current and current.get("status") == "frozen":
            current = None  # already ended and submitted; nothing live to abort
        session_id = str((current or {}).get("session_id") or "")
        paths = recording.find_paths(session_id) if session_id else None
        if paths is not None:
            outcome = current_app.config["WITHDRAWAL_SERVICE"].abort(
                session_id=session_id,
                session_root=paths.root,
                reason=UPDATE_ABORT_REASON,
                requested_by="update",
            )
            stopped["session_aborted"] = (outcome or {}).get("status") != "already_submitted"
    except Exception as error:  # an update must still proceed, e.g. after a WithdrawalError
        stopped["errors"].append(f"session: {error}")
    try:
        if _study_run_state(_active_study_id()).get("status") == "running":
            _abort_study_run(UPDATE_ABORT_REASON)
            stopped["run_aborted"] = True
    except Exception as error:
        stopped["errors"].append(f"study run: {error}")
    try:
        _stop_study_sensor_runtime()
        stopped["sensors_stopped"] = True
    except Exception as error:
        stopped["errors"].append(f"sensors: {error}")
    print(f"[UPDATE] Stopped for update: {stopped}")
    return stopped
