"""Persistent upload jobs: replay, backoff, migration, and route contracts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.apps.server import create_app
from study_runner.runtime_core.settings.folder_open_service import (
    FolderOpenError,
    open_session_folder,
    resolve_session_folder,
)
from study_runner.runtime_core.delivery.upload_jobs_service import (
    MAX_RETRY_AGE_SECONDS,
    PermanentUploadError,
    UploadJobError,
    UploadJobService,
    retry_delay_seconds,
)


class ManualClock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def job_arguments() -> dict:
    return {
        "kind": "notion",
        "study_id": "study",
        "participant_id": "p01",
        "session_id": "session-1",
        "label": "Notion",
        "payload": {
            "result_payload": {"study_id": "study", "participant_id": "p01"},
            "saved_output": {"json_file": "saved_results/study/p01/p01.json"},
            "config_data": {"study_settings": {"notion_enabled": True}},
        },
        "metadata": {"answer_count": 2, "recorded_files": ["p01.json"]},
    }


class UploadJobServiceTests(unittest.TestCase):
    def test_independent_destinations_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UploadJobService(Path(temp_dir))
            barrier = threading.Barrier(2)

            def publish(_payload):
                barrier.wait(timeout=5)
                return {"ok": True}

            for kind in ("notion", "fixture_export"):
                service.register_executor(kind, publish)
                service.enqueue(**{**job_arguments(), "kind": kind, "label": kind})

            self.assertEqual(service.process_due_jobs_once(), 2)
            self.assertEqual(
                {job["status"] for job in service.status()["sessions"][0]["jobs"]},
                {"done"},
            )

    def test_backoff_schedule_matches_roadmap(self) -> None:
        self.assertEqual(
            [retry_delay_seconds(attempt) for attempt in range(1, 7)],
            [30, 120, 600, 1800, 3600, 3600],
        )

    def test_registered_fixture_destination_needs_no_service_key_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            received = []
            service = UploadJobService(Path(temp_dir))
            service.register_executor(
                "fixture_export",
                lambda payload: received.append(payload) or {"ok": True},
            )
            arguments = {
                **job_arguments(),
                "kind": "fixture_export",
                "label": "Fixture export",
            }
            service.enqueue(**arguments)

            self.assertEqual(service.process_due_jobs_once(), 1)
            self.assertEqual(len(received), 1)
            self.assertEqual(
                service.status()["sessions"][0]["jobs"][0]["status"],
                "done",
            )

    def test_failed_attempt_replays_and_succeeds_when_due(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()

            def unavailable(_payload):
                raise RuntimeError("network unavailable")

            first = UploadJobService(Path(temp_dir), executors={"notion": unavailable}, clock=clock)
            job = first.enqueue(**job_arguments())
            self.assertEqual(first.process_due_jobs_once(), 1)
            first_state = first.status()["sessions"][0]["jobs"][0]
            self.assertEqual(first_state["status"], "queued")
            self.assertEqual(first_state["attempts"], 1)
            self.assertEqual(first_state["next_attempt_at"], "1970-01-12T13:47:10Z")
            self.assertIn("network unavailable", first_state["last_error"])

            completed_payloads = []
            replayed = UploadJobService(
                Path(temp_dir),
                executors={"notion": lambda payload: completed_payloads.append(payload) or {"ok": True}},
                clock=clock,
            )
            self.assertEqual(replayed.process_due_jobs_once(), 0)
            clock.value += 30
            self.assertEqual(replayed.process_due_jobs_once(), 1)
            final = replayed.status()["sessions"][0]["jobs"][0]
            self.assertEqual(final["job_id"], job["job_id"])
            self.assertEqual(final["status"], "done")
            self.assertEqual(final["attempts"], 2)
            self.assertEqual(len(completed_payloads), 1)
            self.assertFalse((Path(temp_dir) / "upload_jobs" / f"{job['job_id']}.json").exists())

    def test_running_job_is_requeued_during_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()
            service = UploadJobService(Path(temp_dir), clock=clock)
            job = service.enqueue(**job_arguments())
            event = {"event": "attempt", "job_id": job["job_id"], "attempts": 1}
            service._append_event(event)
            service._apply_event(event)

            replayed = UploadJobService(Path(temp_dir), clock=clock)
            restored = replayed.status()["sessions"][0]["jobs"][0]
            self.assertEqual(restored["status"], "queued")
            self.assertIn("Server restarted", restored["last_error"])
            self.assertEqual(restored["next_attempt_at"], "1970-01-12T13:46:40Z")

    def test_automatic_retries_stop_after_48_hours(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()
            service = UploadJobService(
                Path(temp_dir),
                executors={"notion": lambda _payload: (_ for _ in ()).throw(RuntimeError("still offline"))},
                clock=clock,
            )
            service.enqueue(**job_arguments())
            clock.value += MAX_RETRY_AGE_SECONDS
            service.process_due_jobs_once()

            job = service.status()["sessions"][0]["jobs"][0]
            self.assertEqual(job["status"], "failed")
            self.assertIn("still offline", job["last_error"])

    def test_permanent_failure_fails_at_once_with_the_real_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            def rejected(_payload):
                raise PermanentUploadError("Notion upload: Notion rejected the API key.")

            service = UploadJobService(Path(temp_dir), executors={"notion": rejected}, clock=ManualClock())
            service.enqueue(**job_arguments())
            service.process_due_jobs_once()

            job = service.status()["sessions"][0]["jobs"][0]
            self.assertEqual(job["status"], "failed", "retrying cannot fix a rejected key")
            self.assertIn("rejected the API key", job["last_error"])

    def test_legacy_queue_migration_is_idempotent_and_preserves_bad_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = root / "notion_upload_queue.jsonl"
            entry = {
                **job_arguments()["payload"],
                "hardware_config": {
                    "notion": {"api_key": "legacy-secret"},
                    "nextcloud": {"password": "legacy-share-secret"},
                },
                # Relative to today: a fixed date fell out of the 90-day
                # status window below once enough time had passed.
                "queued_at": (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            legacy.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            service = UploadJobService(root)

            first = service.migrate_legacy_notion_queue()
            self.assertEqual(first, {"found": 1, "migrated": 1})
            self.assertFalse(legacy.exists())
            first_job_id = service.status(days=90)["sessions"][0]["jobs"][0]["job_id"]
            migrated_payload = (root / "upload_jobs" / f"{first_job_id}.json").read_text(encoding="utf-8")
            self.assertNotIn("legacy-secret", migrated_payload)
            self.assertNotIn("legacy-share-secret", migrated_payload)

            legacy.write_text(json.dumps(entry) + "\n", encoding="utf-8")
            second = service.migrate_legacy_notion_queue()
            self.assertEqual(second, {"found": 1, "migrated": 1})
            jobs = service.status(days=90)["sessions"][0]["jobs"]
            self.assertEqual([job["job_id"] for job in jobs], [first_job_id])

            legacy.write_text("[]\n", encoding="utf-8")
            invalid = service.migrate_legacy_notion_queue()
            self.assertIn("invalid entry", invalid["error"])
            self.assertTrue(legacy.exists())

    def test_describe_retry_target_without_describer_reports_no_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UploadJobService(Path(temp_dir), executors={"notion": lambda _payload: {"ok": True}})
            job = service.enqueue(**job_arguments())

            self.assertEqual(
                service.describe_retry_target(job["job_id"]),
                {"differs": False, "fields": []},
            )
            with self.assertRaises(UploadJobError):
                service.describe_retry_target("missing")

    def test_retry_target_choice_reaches_the_executor(self) -> None:
        """An operator's 'use current target' choice must survive a restart
        (it is only a journal event, the payload file stays frozen) and be
        visible to the destination's executor on the next attempt."""
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()
            seen_targets = []

            def unavailable(_payload):
                raise RuntimeError("network unavailable")

            first = UploadJobService(Path(temp_dir), executors={"notion": unavailable}, clock=clock)
            first.register_retry_target_describer(
                "notion", lambda _job, _payload: {"differs": True, "fields": [{"name": "parent_page_id"}]},
            )
            job = first.enqueue(**job_arguments())
            first.process_due_jobs_once()  # fails once, so it becomes retryable

            comparison = first.describe_retry_target(job["job_id"])
            self.assertTrue(comparison["differs"])

            with self.assertRaises(UploadJobError):
                first.retry(job_id=job["job_id"], target="not-a-real-target")

            first.retry(job_id=job["job_id"], target="current")
            self.assertEqual(first.status()["sessions"][0]["jobs"][0]["retry_target"], "current")

            replayed = UploadJobService(
                Path(temp_dir),
                executors={"notion": lambda payload: seen_targets.append(payload.get("_retry_target")) or {"ok": True}},
                clock=clock,
            )
            replayed.process_due_jobs_once()
            self.assertEqual(seen_targets, ["current"])
            self.assertEqual(replayed.status()["sessions"][0]["jobs"][0]["status"], "done")

    def test_automatic_backoff_does_not_reset_an_operator_chosen_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()
            attempts = []

            def still_failing(payload):
                attempts.append(payload.get("_retry_target"))
                raise RuntimeError("still offline")

            service = UploadJobService(Path(temp_dir), executors={"notion": still_failing}, clock=clock)
            job = service.enqueue(**job_arguments())
            service.process_due_jobs_once()
            service.retry(job_id=job["job_id"], target="current")
            clock.value += 10_000  # past the automatic backoff delay
            service.process_due_jobs_once()

            # The automatic retry after the operator's explicit choice keeps
            # using "current", it does not quietly fall back to "snapshot".
            self.assertEqual(attempts, ["snapshot", "current"])

    def test_an_acknowledged_notice_stays_quiet_after_a_restart_until_the_job_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()

            def offline(_payload):
                raise RuntimeError("offline")

            first = UploadJobService(Path(temp_dir), executors={"notion": offline}, clock=clock)
            job = first.enqueue(**job_arguments())
            first.process_due_jobs_once()
            first.acknowledge([job["job_id"], "unknown-job"])

            replayed = UploadJobService(Path(temp_dir), executors={"notion": offline}, clock=clock)
            seen = replayed.status()["sessions"][0]["jobs"][0]
            self.assertEqual(seen["acknowledged_signature"], seen["notice_signature"])

            clock.value += 10_000
            replayed.process_due_jobs_once()  # a new attempt: the notice is new again
            changed = replayed.status()["sessions"][0]["jobs"][0]
            self.assertNotEqual(changed["acknowledged_signature"], changed["notice_signature"])

    def test_public_status_never_contains_private_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UploadJobService(Path(temp_dir))
            service.enqueue(
                **{
                    **job_arguments(),
                    "payload": {
                        **job_arguments()["payload"],
                        "hardware_config": {"notion": {"api_key": "must-not-leak"}},
                    },
                }
            )

            response = json.dumps(service.status())
            self.assertNotIn("must-not-leak", response)
            self.assertNotIn("hardware_config", response)


    def test_get_job_finds_jobs_older_than_the_status_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = ManualClock()
            service = UploadJobService(Path(temp_dir), clock=clock)
            service.enqueue(**job_arguments(), job_id="old-job")
            clock.value += 200 * 24 * 60 * 60
            self.assertEqual(service.status(days=90)["sessions"], [])
            self.assertEqual(service.get_job("old-job")["job_id"], "old-job")
            self.assertIsNone(service.get_job("missing"))

    def test_enqueue_with_current_target_reaches_the_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            seen = []
            service = UploadJobService(Path(temp_dir), executors={"fixture": lambda payload: seen.append(payload.get("_retry_target")) or {"ok": True}})
            arguments = {**job_arguments(), "kind": "fixture"}
            service.enqueue(**arguments, job_id="again-r1", retry_target="current")
            service.process_due_jobs_once()
            self.assertEqual(seen, ["current"])
            with self.assertRaises(UploadJobError):
                service.enqueue(**arguments, job_id="bad-target", retry_target="elsewhere")

    def test_describe_retry_target_uses_the_fallback_after_the_payload_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            seen = []
            service = UploadJobService(Path(temp_dir), executors={"fixture": lambda _payload: {"ok": True}})
            service.register_retry_target_describer("fixture", lambda _job, payload: seen.append(payload) or {"differs": False, "fields": []})
            service.enqueue(**{**job_arguments(), "kind": "fixture"}, job_id="done-job")
            service.process_due_jobs_once()
            service.describe_retry_target("done-job", fallback_payload={"config_data": {"study_id": "s"}})
            self.assertEqual(seen, [{"config_data": {"study_id": "s"}}])


class FolderOpenServiceTests(unittest.TestCase):
    def test_session_folder_resolution_requires_exact_canonical_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relative = Path("study") / "p01" / "20260731T100000Z__session-1"
            expected = root / relative
            expected.mkdir(parents=True)
            (expected / "COMPLETE.json").write_text("{}", encoding="utf-8")

            self.assertEqual(resolve_session_folder(root, relative.as_posix()), expected.resolve())
            for unsafe in ("../study", "study/p01", "study/p01/../../escape"):
                with self.subTest(path=unsafe), self.assertRaises(FolderOpenError):
                    resolve_session_folder(root, unsafe)


class UploadRoutesTests(unittest.TestCase):
    def _app(self, data_dir: str):
        with patch.dict(
            os.environ,
            {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            },
            clear=False,
        ):
            return create_app()

    def test_status_and_manual_retry_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(temp_dir)
            service = app.config["UPLOAD_JOBS_SERVICE"]
            job = service.enqueue(**job_arguments())
            client = app.test_client()

            status = client.get("/api/uploads/status?days=7")
            retry = client.post("/api/uploads/retry", json={"job_id": job["job_id"]})
            invalid = client.post("/api/uploads/retry", json={})

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["sessions"][0]["jobs"][0]["job_id"], job["job_id"])
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.get_json()["retried"], 1)
        self.assertEqual(invalid.status_code, 400)

    def test_retry_target_route_reports_no_describer_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(temp_dir)
            service = app.config["UPLOAD_JOBS_SERVICE"]
            job = service.enqueue(**job_arguments())
            client = app.test_client()

            found = client.get(f"/api/uploads/{job['job_id']}/retry-target")
            missing = client.get("/api/uploads/does-not-exist/retry-target")

        self.assertEqual(found.status_code, 200)
        found_body = found.get_json()
        self.assertTrue(found_body["ok"])
        self.assertFalse(found_body["differs"])
        self.assertEqual(found_body["fields"], [])
        self.assertEqual(missing.status_code, 404)

    def test_status_marks_uploads_of_a_vanished_finalization_as_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(temp_dir)
            service = app.config["UPLOAD_JOBS_SERVICE"]
            orphan = service.enqueue(**{
                **job_arguments(), "session_id": "gone",
                "metadata": {"finalization_job_id": "finalization-that-no-longer-exists"},
            })
            plain = service.enqueue(**{**job_arguments(), "session_id": "plain"})
            client = app.test_client()
            sessions = {s["session_id"]: s for s in client.get("/api/uploads/status").get_json()["sessions"]}
            acknowledged = client.post("/api/uploads/acknowledge", json={"job_ids": [plain["job_id"]]})
            invalid = client.post("/api/uploads/acknowledge", json={"job_ids": "not-a-list"})

        self.assertTrue(sessions["gone"]["orphaned"])
        self.assertFalse(sessions["plain"]["orphaned"])
        self.assertEqual(acknowledged.get_json()["acknowledged"], [plain["job_id"]])
        self.assertEqual(invalid.status_code, 400)
        self.assertTrue(orphan["job_id"])

    def test_open_folder_route_uses_cross_platform_service(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(temp_dir)
            with patch(
                "study_runner.apps.server.routes.uploads.open_session_folder",
                return_value={"ok": True, "path": "/results/study/p01/session-1"},
            ) as opener:
                response = app.test_client().post(
                    "/api/admin/system/open-results-folder",
                    json={"session_path": "study/p01/session-1"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        opener.assert_called_once_with(app.config["DATA_DIR"], "study/p01/session-1")

    def test_open_folder_route_resolves_a_real_v3_session_on_disk(self) -> None:
        """The regression this guards: the route used to target the flat
        study_id/participant_id layout the app stopped producing in 2026-08
        (commit a65ceaf), so it failed for every real completed session. This
        exercises the actual resolver against an on-disk v3 fixture -- the
        route, not a mock, decides whether the folder was found -- and only
        stubs the final OS-level "open a window" call.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(temp_dir)
            data_dir = Path(app.config["DATA_DIR"])
            session_relative = "study/p01/20260811T163356Z__session-1"
            (data_dir / session_relative).mkdir(parents=True)
            (data_dir / session_relative / "COMPLETE.json").write_text("{}", encoding="utf-8")

            with patch("study_runner.runtime_core.settings.folder_open_service.os.startfile", create=True),                  patch("study_runner.runtime_core.settings.folder_open_service.subprocess.Popen"):
                response = app.test_client().post(
                    "/api/admin/system/open-results-folder",
                    json={"session_path": session_relative},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(Path(payload["path"]), (data_dir / session_relative).resolve())

    def test_open_folder_route_rejects_a_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = self._app(temp_dir)
            response = app.test_client().post(
                "/api/admin/system/open-results-folder",
                json={"session_path": "study/p01/never-recorded"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
