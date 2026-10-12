"""Historical cards must have an unambiguous identity before queuing."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

from study_runner.runtime_core.delivery import notion_backfill_service as backfill


def test_backfill_matches_original_positions_and_reports_changed_cards(tmp_path, monkeypatch):
    study = {"study_id": "study-a", "questions": [
        {"type": "text", "prompt": "Old question", "card_id": "stable-0"},
        {"type": "text", "prompt": "Changed question", "card_id": "stable-1"},
    ], "study_settings": {"plugins": {"notion": {"enabled": True, "settings": {
        "export_mapping": {"preset": "analysis", "targets": [{"id": "cards", "row_level": "card"}]},
    }}}}}
    monkeypatch.setattr(backfill, "_current_study", lambda _app, _id: study)
    monkeypatch.setattr(backfill, "list_sessions", lambda _dir: [
        {"study_id": "study-a", "session_path": "study-a/p1/session-1"},
        {"study_id": "study-a", "session_path": "study-a/p1/session-2"},
    ])
    for number, prompt in [(1, "Old question"), (2, "Earlier text")]:
        root = tmp_path / "study-a" / "p1" / f"session-{number}" / "answers"
        root.mkdir(parents=True)
        (root / "result.json").write_text(json.dumps({
            "study_id": "study-a", "session_id": f"s{number}", "participant_id": "p1",
            "answer_details": [{"question_index": number - 1, "question_type": "text", "question_prompt": prompt}],
        }), encoding="utf-8")
        (root / "card-summary.json").write_text(json.dumps({
            "schema": "study-runner/card-summary/v1", "cards": [],
        }), encoding="utf-8")
    service = SimpleNamespace(enqueue=Mock(return_value={"status": "queued"}))
    app = SimpleNamespace(config={"DATA_DIR": tmp_path, "UPLOAD_JOBS_SERVICE": service})
    dry_run = backfill.review(app, "study-a")
    assert [item["session_id"] for item in dry_run["sessions"]] == ["s1"]
    assert dry_run["unresolved_cards"][0]["question_index"] == 1
    queued = backfill.enqueue(app, "study-a")
    assert len(queued["jobs"]) == 1
    payload = service.enqueue.call_args.kwargs["payload"]
    assert payload["result_payload"]["answer_details"][0]["card_id"] == "stable-0"
    assert payload["config_data"]["study_settings"]["plugins"]["notion"]["settings"]["export_mapping"] == study["study_settings"]["plugins"]["notion"]["settings"]["export_mapping"]
