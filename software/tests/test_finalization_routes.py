from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.apps.server.routes.finalization import bp


class FakeFinalizationService:
    def __init__(self, job):
        self.job = job

    def status(self, *, days=30):
        return {"ok": True, "days": days, "counts": {"completed": 1}, "jobs": [dict(self.job)]}

    def get(self, job_id):
        assert job_id == self.job["job_id"]
        return dict(self.job)

    def continue_processing(self, job_id, *, confirmed_by="admin"):
        from study_runner.runtime_core.delivery.finalization_service import InvalidTransitionError

        assert job_id == self.job["job_id"]
        if self.job.get("status") != "completed_degraded":
            raise InvalidTransitionError("Only a degraded finalization can be processed further.")
        return {**self.job, "status": "queued", "continued_by": confirmed_by}


class FinalizationRoutesTests(unittest.TestCase):
    def _app(self, root: Path, job: dict) -> Flask:
        app = Flask(__name__)
        app.config.update(DATA_DIR=root, FINALIZATION_SERVICE=FakeFinalizationService(job))
        app.register_blueprint(bp)
        return app

    def test_job_details_expose_manifest_artifacts_without_rehashing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = "study/p01/20260731T100000Z__session-1"
            session_root = root / session_path
            session_root.mkdir(parents=True)
            (session_root / "meta").mkdir(parents=True, exist_ok=True)
            (session_root / "COMPLETE.json").write_text("{}", encoding="utf-8")
            (session_root / "meta" / "manifest.json").write_text(
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "path": "derived/session.xdf",
                                "role": "merged_xdf",
                                "size_bytes": 42,
                                "sha256": "a" * 64,
                                "local_present": True,
                            },
                            {"path": "../secret", "role": "unsafe"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            job = {
                "job_id": "finalization-1",
                "session_path": session_path,
                "status": "completed",
                "steps": [],
            }
            response = self._app(root, job).test_client().get("/api/finalization/finalization-1")

        self.assertEqual(response.status_code, 200)
        artifacts = response.get_json()["job"]["artifacts"]
        self.assertEqual([item["path"] for item in artifacts], ["COMPLETE.json", "derived/session.xdf", "meta/manifest.json"])
        self.assertEqual(artifacts[1]["sha256"], "a" * 64)

    def test_open_folder_uses_job_session_path_not_client_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_path = "study/p01/20260731T100000Z__session-1"
            (root / session_path).mkdir(parents=True)
            job = {"job_id": "finalization-1", "session_path": session_path}
            app = self._app(root, job)
            with patch(
                "study_runner.apps.server.routes.finalization.open_session_folder",
                return_value={"ok": True, "path": str(root / session_path)},
            ) as opener:
                response = app.test_client().post(
                    "/api/finalization/finalization-1/open-folder",
                    json={"session_path": "../ignored"},
                )

        self.assertEqual(response.status_code, 200)
        opener.assert_called_once_with(root, session_path)

    def test_continue_route_resumes_a_degraded_job_and_refuses_others(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            degraded = {"job_id": "finalization-1", "status": "completed_degraded"}
            response = self._app(root, degraded).test_client().post(
                "/api/finalization/finalization-1/continue",
                json={"confirmed_by": "operator-1"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["job"]["status"], "queued")
            self.assertEqual(response.get_json()["job"]["continued_by"], "operator-1")

            completed = {"job_id": "finalization-1", "status": "completed"}
            refused = self._app(root, completed).test_client().post(
                "/api/finalization/finalization-1/continue",
                json={},
            )
            self.assertEqual(refused.status_code, 409)


    def test_republish_route_reports_refusals(self) -> None:
        from study_runner.runtime_core.delivery.finalization_service import InvalidTransitionError

        calls = []

        def republish(job_id, *, step_key):
            calls.append((job_id, step_key))
            if step_key != "publish_notion":
                raise InvalidTransitionError("Only a finished upload of a completed session can be uploaded again.")
            return {"job_id": job_id, "status": "completed"}

        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(Path(temp_dir), {"job_id": "finalization-1", "status": "completed", "steps": []})
            app.config["FINALIZATION_SERVICE"].republish = republish
            client = app.test_client()
            ok = client.post("/api/finalization/finalization-1/republish", json={"step": "publish_notion"})
            refused = client.post("/api/finalization/finalization-1/republish", json={"step": "export_csv"})

        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["job"]["status"], "completed")
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(calls, [("finalization-1", "publish_notion"), ("finalization-1", "export_csv")])


if __name__ == "__main__":
    unittest.main()
