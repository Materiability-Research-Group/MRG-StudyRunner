"""Review and enqueue historical Notion uploads through the ordinary job queue."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from study_runner.runtime_core.studies.sessions_index_service import list_sessions
from study_runner.runtime_core.studies.study_config_service import load_config, load_study


def _current_study(app: Any, study_id: str) -> dict[str, Any]:
    config_file = Path(app.config["CONFIG_FILE"])
    active = load_config(config_file) if config_file.is_file() else {}
    if active.get("study_id") == study_id:
        return active
    return load_study(Path(app.config["SAVED_STUDIES_DIR"]), study_id)


def _historical_revision(detail: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({
        "type": detail.get("question_type"), "prompt": detail.get("question_prompt"),
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _question_prompt(question: dict[str, Any]) -> str:
    return str(question.get("prompt") or question.get("title") or question.get("subtitle") or "").strip()


def review(app: Any, study_id: str) -> dict[str, Any]:
    """List sessions safe to replay and cards whose old identity is ambiguous."""
    config = _current_study(app, study_id)
    notion = (((config.get("study_settings") or {}).get("plugins") or {}).get("notion") or {})
    mapping = (notion.get("settings") or {}).get("export_mapping") or {}
    if not notion.get("enabled") or not mapping.get("targets"):
        raise ValueError("Enable Notion and save a configurable mapping before backfill.")
    needs_card_identity = any(target.get("row_level") == "card" for target in mapping["targets"])
    questions = config.get("questions") or []
    eligible: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for session in list_sessions(Path(app.config["DATA_DIR"])):
        if session.get("study_id") != study_id:
            continue
        root = Path(app.config["DATA_DIR"]) / session["session_path"]
        result_path = root / "answers" / "result.json"
        summary_path = root / "answers" / "card-summary.json"
        if not result_path.is_file() or not summary_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("schema") != "study-runner/card-summary/v1":
            continue
        details = result.get("answer_details") or []
        indices = [detail.get("question_index") for detail in details if isinstance(detail, dict)]
        ambiguous = len(indices) != len(set(indices))
        resolved = []
        for detail in details:
            if not isinstance(detail, dict):
                continue
            index = detail.get("question_index")
            current = questions[index] if isinstance(index, int) and 0 <= index < len(questions) else None
            if detail.get("card_id"):
                copy = deepcopy(detail)
                copy.setdefault("card_revision", _historical_revision(copy))
                resolved.append(copy)
                continue
            prompt = str(detail.get("question_prompt") or "")
            expected = _question_prompt(current or {})
            matches = [question for question in questions if question.get("type") == detail.get("question_type")
                       and _question_prompt(question) == prompt]
            if ambiguous or current is None or prompt != expected or detail.get("question_type") != current.get("type") or len(matches) != 1:
                if needs_card_identity:
                    unresolved.append({"session_path": session["session_path"], "question_index": index, "prompt": prompt})
                else:
                    resolved.append(detail)
                continue
            copy = deepcopy(detail)
            copy["card_id"] = str(current.get("card_id") or uuid5(NAMESPACE_URL, f"study-runner:{study_id}:{index}"))
            copy["card_revision"] = _historical_revision(copy)
            resolved.append(copy)
        if len(resolved) != len(details):
            continue
        eligible.append({"session_path": session["session_path"], "session_id": result.get("session_id"),
                         "participant_id": result.get("participant_id"), "matched_cards": len(resolved),
                         "_result": {**result, "answer_details": resolved}, "_summary": summary})
    public = [{key: value for key, value in item.items() if not key.startswith("_")} for item in eligible]
    return {"study_id": study_id, "mapping": mapping, "config": config,
            "eligible": eligible, "sessions": public, "unresolved_cards": unresolved}


def enqueue(app: Any, study_id: str) -> dict[str, Any]:
    report = review(app, study_id)
    service = app.config["UPLOAD_JOBS_SERVICE"]
    mapping_hash = hashlib.sha256(json.dumps(report["mapping"], sort_keys=True).encode("utf-8")).hexdigest()[:16]
    jobs = []
    for item in report["eligible"]:
        payload = {
            "config_data": report["config"], "result_payload": item["_result"],
            "hardware_config": {},
            "saved_output": {"card_summary": item["_summary"], "session_relative_path": item["session_path"]},
        }
        jobs.append(service.enqueue(
            kind="notion", study_id=study_id, participant_id=str(item["participant_id"] or ""),
            session_id=str(item["session_id"] or ""), label="Notion backfill", payload=payload,
            metadata={"backfill": True, "session_path": item["session_path"]},
            job_id=f"notion-backfill-{mapping_hash}-{hashlib.sha256(item['session_path'].encode('utf-8')).hexdigest()[:16]}",
        ))
    return {"ok": True, "jobs": jobs, "unresolved_cards": report["unresolved_cards"]}
