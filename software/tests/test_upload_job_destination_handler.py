"""Finalization destination steps and the persistent upload queue together."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.runtime_core.delivery.destination_plugin_service import DestinationPluginDefinition
from study_runner.runtime_core.delivery.finalization_service import FinalizationService
from study_runner.runtime_core.delivery.upload_job_destination_handler import (
    UploadJobDestinationHandler,
    upload_job_id,
)
from study_runner.runtime_core.delivery.upload_jobs_service import UploadJobService
from test_finalization_service import SUBMISSION, MutableClock


def _notion_definition() -> DestinationPluginDefinition:
    return DestinationPluginDefinition(
        plugin_key="notion",
        destination="notion",
        label="Notion",
        legacy_enabled_field="notion_enabled",
    )


class UploadJobDestinationHandlerTests(unittest.TestCase):
    def test_job_id_counts_generation_and_republish(self) -> None:
        state = {"job_id": "finalization-1", "publication_generation": 2}
        self.assertEqual(upload_job_id(state, "notion"), "finalization-1-g2-notion")
        state["republish_counts"] = {"notion": 3}
        self.assertEqual(upload_job_id(state, "notion"), "finalization-1-g2-notion-r3")
        self.assertEqual(upload_job_id(state, "nextcloud"), "finalization-1-g2-nextcloud")

    def test_upload_again_queues_a_fresh_job_with_current_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clock = MutableClock()
            targets = []
            uploads = UploadJobService(
                root,
                executors={"notion": lambda payload: targets.append(payload["_retry_target"]) or {"ok": True}},
                clock=clock,
            )
            service = FinalizationService(
                root,
                destination_handler=UploadJobDestinationHandler(uploads, root),
                destination_definitions=(_notion_definition(),),
                clock=clock,
            )
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True}},
            )

            def run_until_done() -> dict:
                for _ in range(5):
                    service.process_due_jobs_once()
                    uploads.process_due_jobs_once()
                    clock.value += 400
                    service.process_due_jobs_once()
                    current = service.get(job["job_id"])
                    if next(s for s in current["steps"] if s["key"] == "publish_notion")["status"] == "done":
                        return current
                self.fail("the Notion step never finished")

            first = run_until_done()
            self.assertEqual(targets, ["snapshot"])
            first_upload = next(s for s in first["steps"] if s["key"] == "publish_notion")["details"]["upload_job_id"]

            service.republish(job["job_id"], step_key="publish_notion")
            again = run_until_done()
            second_upload = next(s for s in again["steps"] if s["key"] == "publish_notion")["details"]["upload_job_id"]

            self.assertEqual(targets, ["snapshot", "current"])
            self.assertEqual(second_upload, f"{first_upload}-r1")
            self.assertEqual(uploads.get_job(first_upload)["status"], "done", "the first upload stays on record")


if __name__ == "__main__":
    unittest.main()
