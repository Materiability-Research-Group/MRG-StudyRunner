from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.data_core.host.artifacts import ArtifactStore, SessionIdentity, sha256_file
from study_runner.runtime_core.delivery.artifact_manifest_service import ArtifactManifestStore
from study_runner.runtime_core.studies.card_summary_service import CardSummaryBuilder
from study_runner.runtime_core.studies.sessions_index_service import list_sessions
from study_runner.runtime_core.delivery.destination_plugin_service import (
    DestinationPluginDefinition,
)
from study_runner.runtime_core.delivery.finalization_service import (
    DeferredStep,
    FinalizationError,
    FinalizationService,
    InvalidTransitionError,
    QualityAttentionError,
    StepResult,
    SubmissionConflictError,
    _step,
)


SUBMISSION = {
    "submission_id": "submission-1",
    "session_id": "session-1",
    "study_id": "Study A",
    "participant_id": "p01",
    "timestamp_start": "2026-07-31T10:00:00Z",
    "timestamp_end": "2026-07-31T10:01:00Z",
    "answers": {"q1": 4},
    "card_events": [
        {
            "event_id": "card-event-1",
            "shown_event_id": "card-1-shown",
            "answered_event_id": "card-1-answered",
            "question_index": 1,
            "question_type": "slider",
            "client_start_trigger_epoch_ms": 1000,
            "client_stop_trigger_epoch_ms": 2000,
        }
    ],
    "study_end_event": {"event_id": "study-end-session-1"},
}


class OneStreamReader:
    def read_streams(self, _path):
        return [
            {
                "stream_key": "sensor.one",
                "plugin_key": "fixture",
                "nominal_rate_hz": 2,
                "timestamps": [1.0, 1.5, 2.0],
                "samples": [{"value": 2.0}, {"value": 4.0}, {"value": 99.0}],
            },
            {
                "stream_key": "lsl.markers",
                "plugin_key": "lsl",
                "nominal_rate_hz": 0,
                "timestamps": [1.0, 2.0, 2.1],
                "samples": [
                    {"marker": "event_id=card-1-shown|phase=shown"},
                    {"marker": "event_id=card-1-answered|phase=answered"},
                    {"marker": "event_id=study-end-session-1|phase=study_end"},
                ],
            },
        ]


class SuccessfulRecordingAdapter:
    def freeze(self, _context):
        return {"closed_segments": 1}

    def validate_sources(self, context):
        source = context.paths.plugin_dir("fixture") / "part-0001.xdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source-xdf")
        return {"source_paths": [source.relative_to(context.paths.root).as_posix()]}

    def merge(self, context):
        context.paths.merged_xdf.parent.mkdir(parents=True, exist_ok=True)
        context.paths.merged_xdf.write_bytes(b"merged-xdf")
        return {"output": "derived/session.xdf"}

    def validate_merge(self, _context):
        return {"stream_parity": True, "native_rates_preserved": True}


class FailingRecordingAdapter(SuccessfulRecordingAdapter):
    def validate_sources(self, _context):
        raise FinalizationError("required source fixture is unreadable")


COVERAGE_ERROR = (
    "XDF source validation failed: insufficient_time_coverage: stream "
    "'study_runner.brainbit.bands' starts 52 ms after the session start marker, so "
    "it does not cover the marker-defined session window"
)


class QualityWarningRecordingAdapter(SuccessfulRecordingAdapter):
    """Readable data that starts 52 ms late: only a quality warning."""

    def __init__(self):
        self.validate_calls = 0

    def validate_sources(self, context):
        self.validate_calls += 1
        super().validate_sources(context)
        raise QualityAttentionError(
            COVERAGE_ERROR,
            issues=[
                {
                    "code": "insufficient_time_coverage",
                    "message": "stream 'study_runner.brainbit.bands' starts 52 ms after the session start marker",
                    "source_key": "brainbit",
                    "origin_id": None,
                }
            ],
            details={"checked_streams": 6},
        )


class UnconfirmedDrainRecordingAdapter(SuccessfulRecordingAdapter):
    def validate_sources(self, context):
        super().validate_sources(context)
        raise QualityAttentionError(
            "XDF source validation failed: source_stream_drain_unconfirmed",
            issues=[{
                "code": "source_stream_drain_unconfirmed",
                "message": "stream 'study_runner.brainbit.bands' was safely cut off after 1 recorded samples",
                "source_key": "brainbit",
                "origin_id": "brainbit:part-0001.xdf:0",
            }],
            details={"checked_streams": 1},
        )


class LegacyCoverageFailureAdapter(SuccessfulRecordingAdapter):
    """How earlier versions reported the same warning: as a plain failure."""

    def validate_sources(self, _context):
        raise FinalizationError(COVERAGE_ERROR)


class RecordingDestinationHandler:
    def __init__(self, *, defer_notion_once: bool = False):
        self.calls = []
        self.defer_notion_once = defer_notion_once

    def publish(self, destination, context):
        self.calls.append(destination)
        if destination == "notion" and self.defer_notion_once:
            self.defer_notion_once = False
            raise DeferredStep("Notion is queued", retry_after_seconds=1)
        return StepResult("done", {"destination": destination, "session_id": context.state["session_id"]})


class FailedPersistentDestinationHandler(RecordingDestinationHandler):
    def __init__(self):
        super().__init__()
        self.retry_calls = []
        self.fail_notion = True

    def publish(self, destination, context):
        self.calls.append(destination)
        if destination == "notion" and self.fail_notion:
            raise FinalizationError("persistent notion upload failed")
        return StepResult("done", {"destination": destination, "session_id": context.state["session_id"]})

    def retry(self, destination, _context, *, target="snapshot"):
        self.retry_calls.append(destination)
        self.retry_targets_used = getattr(self, "retry_targets_used", [])
        self.retry_targets_used.append(target)
        self.fail_notion = False


class DeferredNextcloudDestinationHandler(RecordingDestinationHandler):
    def publish(self, destination, context):
        self.calls.append(destination)
        if destination == "nextcloud":
            raise DeferredStep("Nextcloud attention backup is queued", retry_after_seconds=1)
        return StepResult("done", {"destination": destination, "session_id": context.state["session_id"]})


class MutableClock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


class FinalizationServiceTests(unittest.TestCase):
    def _service(self, root: Path, **kwargs):
        return FinalizationService(
            root,
            recording_adapter=kwargs.get("recording_adapter", SuccessfulRecordingAdapter()),
            destination_handler=kwargs.get("destination_handler"),
            destination_definitions=kwargs.get("destination_definitions"),
            card_summary_builder=CardSummaryBuilder(OneStreamReader()),
            plugin_manifests=kwargs.get("plugin_manifests"),
            clock=kwargs.get("clock", MutableClock()),
        )

    @staticmethod
    def _recording_plan(root: Path, plan: dict) -> None:
        """What the recording runtime writes at session start, before the submission."""
        identity = SessionIdentity(
            study_id="Study A",
            participant_id="p01",
            session_id="session-1",
            started_at=dt.datetime(2026, 7, 31, 10, tzinfo=dt.timezone.utc),
        )
        ArtifactStore(root).reserve(identity).recording_plan_file.write_text(json.dumps(plan), encoding="utf-8")

    def test_exact_source_retry_is_acknowledged_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            committed = service.commit_submission(
                SUBMISSION, recording_expected=True, source_submission_sha256="source-hash-1",
            )
            service = self._service(root)  # a retry can arrive after a server restart
            retry = service.acknowledged_source_submission(
                study_id="Study A", participant_id="p01", submission_id="submission-1",
                session_id="session-1", source_submission_sha256="source-hash-1",
            )
            self.assertEqual(retry["job_id"], committed["job_id"])
            self.assertFalse(retry["created"])
            with self.assertRaises(SubmissionConflictError):
                service.acknowledged_source_submission(
                    study_id="Study A", participant_id="p01", submission_id="submission-1",
                    session_id="session-1", source_submission_sha256="changed-content",
                )

    def test_commit_is_idempotent_and_processing_publishes_complete_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            created = service.commit_submission(SUBMISSION, config_data={"study_settings": {}}, recording_expected=True)
            repeated = service.commit_submission(SUBMISSION, config_data={"study_settings": {}}, recording_expected=True)

            self.assertTrue(created["created"])
            self.assertFalse(repeated["created"])
            self.assertEqual(created["job_id"], repeated["job_id"])
            session_root = root / created["session_path"]
            self.assertTrue((session_root / "answers" / "submission.json").is_file())
            self.assertTrue((session_root / "meta" / "finalization-state.json").is_file())
            self.assertEqual(service.get(created["job_id"])["status"], "queued")

            self.assertEqual(service.process_due_jobs_once(), 1)
            completed = service.get(created["job_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["quality_status"], "valid")
            self.assertTrue((session_root / "derived" / "session.xdf").is_file())
            self.assertTrue((session_root / "answers" / "card-summary.json").is_file())
            self.assertTrue((session_root / "answers" / "result.json").is_file())
            self.assertTrue((session_root / "meta" / "manifest.json").is_file())
            self.assertTrue((session_root / "meta" / "checksums.sha256").is_file())
            self.assertTrue((session_root / "COMPLETE.json").is_file())
            self.assertFalse((session_root / "ATTENTION_REQUIRED.json").exists())
            index = session_root.parent.parent / "sessions-index.csv"
            self.assertTrue(index.is_file())
            self.assertIn(created["session_path"], index.read_text(encoding="utf-8-sig"))

            restarted = self._service(root)
            self.assertEqual(restarted.get(created["job_id"])["status"], "completed")
            self.assertEqual(restarted.process_due_jobs_once(), 0)

    SOFTWARE_PLAN = {
        "study_runner_version": "1.6.1",
        "recording_contract": {
            "source_descriptors": {
                "fixture": {"plugin_version": "2.0.0"},
                # A core source: covered by the Study Runner version.
                "lsl": {"plugin_version": "1.0.0"},
            }
        },
    }

    def test_the_manifest_names_the_software_that_produced_the_session(self) -> None:
        manifests = {
            "fixture": {"version": "2.1.0"},
            "slider_card": {"version": "1.2.0", "capability_config": {"card_contract": {"question_types": ["slider"]}}},
            "unused_card": {"version": "4.0.0", "capability_config": {"card_contract": {"question_types": ["affect-map"]}}},
            "fixture_export": {"version": "1.0.3"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._recording_plan(root, self.SOFTWARE_PLAN)
            service = self._service(
                root,
                destination_handler=RecordingDestinationHandler(),
                destination_definitions=(
                    DestinationPluginDefinition(
                        plugin_key="fixture_export", destination="fixture_export", label="Fixture export", default_enabled=True,
                    ),
                ),
                plugin_manifests=lambda: manifests,
            )
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {}, "questions": [{"type": "slider"}]},
                recording_expected=True,
            )
            # Installed after the submission: does not change what produced it.
            manifests["slider_card"] = {**manifests["slider_card"], "version": "1.3.0"}
            service.process_due_jobs_once()

            manifest = json.loads((root / job["session_path"] / "meta" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["provenance"]["software"],
                {
                    "study_runner_version": "1.6.1",
                    "plugins": {
                        # The version that recorded, from the recording contract.
                        "fixture": {"version": "2.0.0", "role": "recording"},
                        "slider_card": {"version": "1.2.0", "role": "card"},
                        "fixture_export": {"version": "1.0.3", "role": "destination"},
                    },
                },
            )

    def test_a_job_from_before_version_tracking_reports_only_what_the_session_recorded(self) -> None:
        manifests = {"slider_card": {"version": "1.3.0", "capability_config": {"card_contract": {"question_types": ["slider"]}}}}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._recording_plan(root, self.SOFTWARE_PLAN)
            job = self._service(root, plugin_manifests=lambda: manifests).commit_submission(
                SUBMISSION,
                config_data={"study_settings": {}, "questions": [{"type": "slider"}]},
                recording_expected=True,
            )
            state_file = root / job["session_path"] / "meta" / "finalization-state.json"
            state = json.loads(state_file.read_text(encoding="utf-8"))
            del state["software"]  # as written before version tracking
            state_file.write_text(json.dumps(state), encoding="utf-8")

            self._service(root, plugin_manifests=lambda: manifests).process_due_jobs_once()

            manifest = json.loads((root / job["session_path"] / "meta" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["provenance"]["software"],
                {
                    "study_runner_version": "1.6.1",
                    "plugins": {"fixture": {"version": "2.0.0", "role": "recording"}},
                    "partial": True,
                },
            )

    def test_journal_xdf_event_id_mismatch_surfaces_as_a_warning_not_a_failure(self) -> None:
        """Package 5e end to end: the durable "trial" journal is read from
        disk (not a live TrialEventService) and compared against the XDF
        markers OneStreamReader fabricates. Adds one event id the journal
        has but the XDF fixture does not, which must become a warning on
        the completed job, not a finalization failure.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            service.session_journals.append(
                "trial",
                SUBMISSION["session_id"],
                "trial_state_updated",
                {
                    "version": 2,
                    "events": {
                        "card-1-shown": {"kind": "trial_marker"},
                        "card-1-answered": {"kind": "trial_marker"},
                        "study-end-session-1": {"kind": "study_end"},
                        "card-2-shown": {"kind": "trial_marker"},
                    },
                },
            )
            created = service.commit_submission(SUBMISSION, config_data={"study_settings": {}}, recording_expected=True)
            self.assertEqual(service.process_due_jobs_once(), 1)

            completed = service.get(created["job_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(
                any("journal_xdf_event_id_mismatch" in warning for warning in completed["warnings"]),
                completed["warnings"],
            )
            self.assertTrue(
                any("card-2-shown" in warning and "missing_from_xdf" in warning for warning in completed["warnings"]),
                completed["warnings"],
            )

            summary = json.loads((root / created["session_path"] / "answers" / "card-summary.json").read_text(encoding="utf-8"))
            mismatch = next(
                warning
                for warning in summary["quality_warnings"]
                if warning["event_id"] == "card-2-shown"
            )
            self.assertEqual(mismatch["direction"], "missing_from_xdf")

    def test_durable_commit_survives_projection_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            from study_runner.runtime_core.delivery import finalization_service as module

            real_atomic_write = module.atomic_write_json

            def fail_submission_projection(path, payload):
                if Path(path).name == "submission.json":
                    raise OSError("simulated projection failure")
                return real_atomic_write(path, payload)

            with mock.patch.object(module, "atomic_write_json", side_effect=fail_submission_projection):
                created = service.commit_submission(SUBMISSION, recording_expected=True)

            self.assertTrue(created["created"])
            session_root = root / created["session_path"]
            self.assertTrue((session_root / "meta" / ".submission-commit.json").is_file())
            self.assertFalse((session_root / "answers" / "submission.json").exists())
            self.assertEqual(service.get(created["job_id"])["status"], "queued")
            repeated = service.commit_submission(SUBMISSION, recording_expected=True)
            self.assertFalse(repeated["created"])

            restarted = self._service(root)
            self.assertTrue((session_root / "answers" / "submission.json").is_file())
            self.assertEqual(restarted.get(created["job_id"])["status"], "queued")

    def test_v3_destination_selection_overrides_legacy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            job = service.commit_submission(
                SUBMISSION,
                config_data={
                    "study_settings": {
                        "notion_enabled": False,
                        "nextcloud_enabled": True,
                        "plugins": {
                            "notion": {"enabled": True, "required": False, "settings": {}},
                            "nextcloud": {"enabled": False, "required": False, "settings": {}},
                        },
                    }
                },
                recording_expected=True,
            )

            steps = {step["key"]: step for step in service.get(job["job_id"])["steps"]}
            self.assertEqual(steps["publish_notion"]["status"], "pending")
            self.assertEqual(steps["publish_nextcloud"]["status"], "skipped")

    def test_fixture_destination_adds_and_executes_a_step_without_core_key_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = RecordingDestinationHandler()
            definition = DestinationPluginDefinition(
                plugin_key="fixture_export",
                destination="fixture_export",
                label="Fixture export",
            )
            service = self._service(
                Path(temp_dir),
                destination_handler=destination,
                destination_definitions=(definition,),
            )
            job = service.commit_submission(
                SUBMISSION,
                config_data={
                    "study_settings": {
                        "plugins": {
                            "fixture_export": {
                                "enabled": True,
                                "required": False,
                                "settings": {"bucket": "fixture"},
                            }
                        }
                    }
                },
                recording_expected=True,
            )

            step_keys = [step["key"] for step in job["steps"]]
            self.assertIn("publish_fixture_export", step_keys)
            self.assertNotIn("publish_notion", step_keys)
            self.assertNotIn("publish_nextcloud", step_keys)
            service.process_due_jobs_once()
            self.assertEqual(destination.calls, ["fixture_export"])
            self.assertEqual(service.get(job["job_id"])["status"], "completed")

    def test_even_legacy_attention_policy_cannot_publish_before_local_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = RecordingDestinationHandler()
            definition = DestinationPluginDefinition(
                plugin_key="fixture_export",
                destination="fixture_export",
                label="Fixture export",
                default_enabled=True,
                publish_on_attention=True,
                republish_on_degraded=True,
            )
            service = self._service(
                Path(temp_dir),
                recording_adapter=FailingRecordingAdapter(),
                destination_handler=destination,
                destination_definitions=(definition,),
            )
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            service.process_due_jobs_once()
            service.process_due_jobs_once()
            self.assertEqual(service.get(job["job_id"])["status"], "attention_required")
            self.assertEqual(destination.calls, [])
            service.confirm_degraded(job["job_id"], reason="Reviewed raw data")
            service.process_due_jobs_once()
            self.assertEqual(destination.calls, ["fixture_export"])

    def test_multiple_source_purge_destinations_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            definitions = tuple(
                DestinationPluginDefinition(
                    plugin_key=key,
                    destination=key,
                    label=key,
                    purge_verified_sources=True,
                )
                for key in ("first_archive", "second_archive")
            )
            with self.assertRaisesRegex(ValueError, "Only one upload destination"):
                self._service(
                    Path(temp_dir),
                    destination_definitions=definitions,
                )

    def test_the_source_release_step_is_hidden_when_no_destination_can_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir), destination_handler=RecordingDestinationHandler())
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            service.process_due_jobs_once()
            purge = next(s for s in service.get(job["job_id"])["steps"] if s["key"] == "purge_local_sources")
            self.assertTrue(purge["hidden"])
            # Only the public view hides it; the durable state is unchanged.
            self.assertNotIn("hidden", _step(service._jobs[job["job_id"]], "purge_local_sources"))

    def _archive_service(self, root: Path, handler):
        definition = DestinationPluginDefinition(
            plugin_key="archive", destination="archive", label="Archive",
            default_enabled=True, purge_verified_sources=True,
        )
        return self._service(root, destination_handler=handler, destination_definitions=(definition,))

    def test_a_destination_that_can_release_shows_the_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._archive_service(Path(temp_dir), RecordingDestinationHandler())
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            purge = next(s for s in job["steps"] if s["key"] == "purge_local_sources")
            self.assertNotIn("hidden", purge)

    def test_retrying_the_releasing_destination_reopens_a_release_that_waited_for_it(self) -> None:
        class FailsUntilRetried(RecordingDestinationHandler):
            failing = True

            def publish(self, destination, context):
                if self.failing:
                    raise FinalizationError("archive offline")
                return super().publish(destination, context)

            def retry(self, destination, _context, *, target="snapshot"):
                self.failing = False

        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._archive_service(Path(temp_dir), FailsUntilRetried())
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            service.process_due_jobs_once()
            state = service._jobs[job["job_id"]]
            self.assertEqual(_step(state, "publish_archive")["status"], "failed")
            self.assertEqual(_step(state, "purge_local_sources")["status"], "skipped")

            service.retry(job["job_id"], step_key="publish_archive")
            self.assertEqual(_step(state, "purge_local_sources")["status"], "pending")

    def test_conflicting_reuse_of_submission_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.commit_submission(SUBMISSION, recording_expected=True)
            with self.assertRaises(SubmissionConflictError):
                service.commit_submission({**SUBMISSION, "answers": {"q1": 5}}, recording_expected=True)

    def test_core_failure_requires_attention_and_admin_can_confirm_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = RecordingDestinationHandler()
            service = self._service(
                Path(temp_dir),
                recording_adapter=FailingRecordingAdapter(),
                destination_handler=destination,
            )
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True, "nextcloud_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()
            failed = service.get(job["job_id"])
            self.assertEqual(failed["status"], "attention_required")
            self.assertEqual(next(step for step in failed["steps"] if step["key"] == "validate_sources")["status"], "failed")
            session_root = Path(temp_dir) / failed["session_path"]
            self.assertTrue((session_root / "ATTENTION_REQUIRED.json").is_file())

            service.process_due_jobs_once()
            self.assertEqual(destination.calls, [], "Destinations wait for local completion")
            degraded = service.confirm_degraded(job["job_id"], reason="Sensor cable was removed", confirmed_by="operator-1")
            self.assertEqual(degraded["status"], "completed_degraded")
            self.assertEqual(degraded["degraded_confirmation"]["confirmed_by"], "operator-1")
            service.process_due_jobs_once()
            self.assertIn("notion", destination.calls)
            self.assertTrue((session_root / "COMPLETE.json").is_file())

    def test_accepted_quality_warning_continues_every_remaining_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = RecordingDestinationHandler()
            adapter = QualityWarningRecordingAdapter()
            service = self._service(
                Path(temp_dir),
                recording_adapter=adapter,
                destination_handler=destination,
            )
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True, "nextcloud_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()
            attention = service.get(job["job_id"])
            self.assertEqual(attention["status"], "attention_required")
            self.assertTrue(attention["can_continue_with_warnings"])
            self.assertEqual(
                attention["quality_acceptance"]["issues"][0]["code"],
                "insufficient_time_coverage",
            )
            session_root = Path(temp_dir) / attention["session_path"]
            # Both destinations must wait for completed local data.
            service.process_due_jobs_once()
            self.assertEqual(destination.calls, [])

            continued = service.confirm_degraded(
                job["job_id"],
                reason="Test study was very short",
                confirmed_by="operator-1",
            )
            self.assertEqual(continued["status"], "queued")
            self.assertTrue(continued["degraded_confirmation"]["continued_processing"])

            service.process_due_jobs_once()
            done = service.get(job["job_id"])
            self.assertEqual(done["status"], "completed_degraded")
            self.assertEqual(done["quality_status"], "degraded")
            steps = {step["key"]: step for step in done["steps"]}
            for key in ("merge_xdf", "validate_merge", "build_card_summary", "write_result_manifest"):
                self.assertEqual(steps[key]["status"], "done", key)
            # The CSV is a projection of the backup grid, which this fixture
            # does not write; it must be settled, never left pending.
            self.assertIn(steps["export_csv"]["status"], {"done", "skipped"})
            accepted = steps["validate_sources"]["details"]["accepted_with_warnings"]
            self.assertEqual(accepted["reason"], "Test study was very short")
            self.assertEqual(accepted["issues"][0]["code"], "insufficient_time_coverage")
            self.assertEqual(steps["purge_local_sources"]["status"], "skipped")
            self.assertEqual(adapter.validate_calls, 2)
            # The real derived artifacts exist, not the old empty stub.
            self.assertTrue((session_root / "derived" / "session.xdf").is_file())
            summary = json.loads((session_root / "answers" / "card-summary.json").read_text(encoding="utf-8"))
            self.assertGreater(summary["card_count"], 0)
            result = json.loads((session_root / "answers" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["server_finalization"]["quality_status"], "degraded")
            self.assertEqual(result["server_finalization"]["quality_warning"], "Test study was very short")
            marker = json.loads((session_root / "COMPLETE.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "completed_degraded")
            self.assertFalse((session_root / "ATTENTION_REQUIRED.json").exists())
            # Both destinations publish the finished session.
            self.assertIn("notion", destination.calls)
            self.assertEqual(destination.calls.count("nextcloud"), 1)
            self.assertFalse(done["can_continue_with_warnings"])
            self.assertFalse(done["can_continue_processing"])

    def test_unconfirmed_drain_acceptance_finishes_all_core_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root, recording_adapter=UnconfirmedDrainRecordingAdapter())
            job = service.commit_submission(SUBMISSION, config_data={"study_settings": {}}, recording_expected=True)
            session_root = root / job["session_path"]
            backup = session_root / "raw" / "backup" / "part-0001.xdf"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(b"synthetic backup fixture")

            def export_csv(_source, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("timestamp,value\n1.0,2.0\n", encoding="utf-8")
                return {"row_count": 1, "channel_count": 1}

            with mock.patch(
                "study_runner.runtime_core.delivery.finalization_service.write_backup_csv",
                side_effect=export_csv,
            ):
                service.process_due_jobs_once()
                attention = service.get(job["job_id"])
                self.assertTrue(attention["can_continue_with_warnings"])
                self.assertEqual(attention["quality_acceptance"]["issues"][0]["code"],
                                 "source_stream_drain_unconfirmed")
                service.confirm_degraded(job["job_id"], reason="Tail reviewed", confirmed_by="operator-1")
                service.process_due_jobs_once()

            done = service.get(job["job_id"])
            self.assertEqual(done["status"], "completed_degraded")
            self.assertEqual(done["quality_status"], "degraded")
            steps = {step["key"]: step for step in done["steps"]}
            for key in ("merge_xdf", "validate_merge", "build_card_summary", "export_csv", "write_result_manifest"):
                self.assertEqual(steps[key]["status"], "done", key)
            self.assertEqual(steps["purge_local_sources"]["status"], "skipped")
            self.assertEqual(steps["validate_sources"]["details"]["accepted_with_warnings"]["reason"],
                             "Tail reviewed")
            for path in ("derived/session.xdf", "answers/card-summary.json", "answers/result.json",
                         "session_1hz.csv", "meta/manifest.json"):
                self.assertTrue((session_root / path).is_file(), path)

    def test_blocking_failure_cannot_continue_with_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir), recording_adapter=FailingRecordingAdapter())
            job = service.commit_submission(SUBMISSION, config_data={}, recording_expected=True)
            service.process_due_jobs_once()
            failed = service.get(job["job_id"])
            self.assertFalse(failed["can_continue_with_warnings"])
            degraded = service.confirm_degraded(job["job_id"], reason="Cable removed")
            self.assertEqual(degraded["status"], "completed_degraded")
            self.assertFalse(degraded["can_continue_processing"])
            with self.assertRaises(InvalidTransitionError):
                service.continue_processing(job["job_id"])

    def test_continue_processing_finishes_a_session_stopped_by_an_earlier_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            legacy = self._service(root, recording_adapter=LegacyCoverageFailureAdapter())
            job = legacy.commit_submission(SUBMISSION, config_data={}, recording_expected=True)
            legacy.process_due_jobs_once()
            stopped = legacy.confirm_degraded(job["job_id"], reason="to short?")
            self.assertEqual(stopped["status"], "completed_degraded")
            steps = {step["key"]: step["status"] for step in stopped["steps"]}
            self.assertEqual(steps["merge_xdf"], "pending")
            self.assertTrue(stopped["can_continue_processing"])

            upgraded = self._service(root, recording_adapter=QualityWarningRecordingAdapter())
            continued = upgraded.continue_processing(job["job_id"])
            self.assertEqual(continued["status"], "queued")
            upgraded.process_due_jobs_once()
            done = upgraded.get(job["job_id"])
            self.assertEqual(done["status"], "completed_degraded")
            self.assertEqual(done["degraded_confirmation"]["reason"], "to short?")
            session_root = root / done["session_path"]
            self.assertTrue((session_root / "derived" / "session.xdf").is_file())
            remaining = {step["key"]: step["status"] for step in done["steps"]}
            for key in ("merge_xdf", "validate_merge", "build_card_summary", "export_csv", "write_result_manifest"):
                self.assertIn(remaining[key], {"done", "skipped"}, key)

    def test_acknowledging_attention_quiets_the_job_but_keeps_its_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir), recording_adapter=FailingRecordingAdapter())
            job = service.commit_submission(SUBMISSION, config_data={}, recording_expected=True)
            # Seeing a job that is still working only quiets its notice.
            seen = service.acknowledge_attention(job["job_id"])
            self.assertNotIn("attention_acknowledged_at", seen)
            self.assertEqual(seen["acknowledged_signature"], seen["notice_signature"])
            service.process_due_jobs_once()
            failed = service.get(job["job_id"])
            self.assertNotEqual(failed["acknowledged_signature"], failed["notice_signature"])

            acknowledged = service.acknowledge_attention(job["job_id"])
            self.assertEqual(acknowledged["status"], "attention_required")
            self.assertTrue(acknowledged["attention_acknowledged_at"])
            session_root = Path(temp_dir) / acknowledged["session_path"]
            self.assertTrue((session_root / "ATTENTION_REQUIRED.json").is_file())

            reloaded = self._service(Path(temp_dir), recording_adapter=FailingRecordingAdapter())
            self.assertTrue(reloaded.get(job["job_id"])["attention_acknowledged_at"])

            retried = reloaded.retry(job["job_id"])
            self.assertNotIn("attention_acknowledged_at", retried)

    def test_degraded_confirmation_does_not_wait_for_remote_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = DeferredNextcloudDestinationHandler()
            service = self._service(
                Path(temp_dir),
                recording_adapter=FailingRecordingAdapter(),
                destination_handler=destination,
            )
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"nextcloud_enabled": True}},
                recording_expected=True,
            )

            service.process_due_jobs_once()
            self.assertEqual(destination.calls, [])
            confirmed = service.confirm_degraded(job["job_id"], reason="Known sensor loss")
            self.assertEqual(confirmed["status"], "completed_degraded")
            service.process_due_jobs_once()
            self.assertEqual(destination.calls, ["nextcloud"])

    def test_destinations_progress_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            destination = RecordingDestinationHandler(defer_notion_once=True)
            service = self._service(Path(temp_dir), destination_handler=destination, clock=clock)
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True, "nextcloud_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()
            pending = service.get(job["job_id"])
            notion = next(step for step in pending["steps"] if step["key"] == "publish_notion")
            nextcloud = next(step for step in pending["steps"] if step["key"] == "publish_nextcloud")
            self.assertEqual(notion["status"], "retrying")
            self.assertEqual(nextcloud["status"], "done")
            self.assertEqual(
                service.process_due_jobs_once(),
                0,
                "a future retry must not hot-loop because purge is still pending",
            )

            retried = service.retry(job["job_id"], step_key="publish_notion")
            retried_nextcloud = next(step for step in retried["steps"] if step["key"] == "publish_nextcloud")
            self.assertEqual(retried_nextcloud["status"], "done")
            service.process_due_jobs_once()
            self.assertEqual(service.get(job["job_id"])["status"], "completed")
            self.assertEqual(destination.calls.count("nextcloud"), 1)

    def test_destination_retry_requeues_persistent_job_and_preserves_merge_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = FailedPersistentDestinationHandler()
            service = self._service(Path(temp_dir), destination_handler=destination)
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()
            state = service._jobs[job["job_id"]]
            self.assertEqual(next(step for step in state["steps"] if step["key"] == "publish_notion")["status"], "failed")
            self.assertTrue(state["runtime"]["merge_parity"])

            service.retry(job["job_id"], step_key="publish_notion")

            self.assertEqual(destination.retry_calls, ["notion"])
            self.assertTrue(state["runtime"]["merge_parity"])
            service.process_due_jobs_once()
            self.assertEqual(service.get(job["job_id"])["status"], "completed")

    def test_failed_upload_completes_the_session_and_stays_retryable(self) -> None:
        """A failed upload must not leave a valid session 'finalizing' forever."""
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = FailedPersistentDestinationHandler()
            service = self._service(Path(temp_dir), destination_handler=destination)
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True, "nextcloud_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()

            failed = service.get(job["job_id"])
            self.assertEqual(failed["status"], "completed")
            self.assertEqual(failed["quality_status"], "valid")
            self.assertEqual(failed["upload_failures"], ["publish_notion"])
            self.assertTrue(any("persistent notion upload failed" in warning for warning in failed["warnings"]))
            steps = {step["key"]: step["status"] for step in failed["steps"]}
            self.assertEqual(steps["publish_nextcloud"], "done")
            self.assertIn(steps["archive_session_journals"], {"done", "skipped"})
            self.assertFalse(service._job_has_due_work(service._jobs[job["job_id"]], 10_000.0))
            listed = [item for item in list_sessions(Path(temp_dir)) if item["session_id"] == failed["session_id"]]
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["finalization_job_id"], job["job_id"])
            self.assertEqual(listed[0]["finalization_status"], "completed")
            self.assertEqual(listed[0]["upload_failures"], ["publish_notion"])

            retried = service.retry(job["job_id"], step_key="publish_notion")
            self.assertEqual(retried["status"], "completed", "retrying an upload never reopens the sealed session")
            service.process_due_jobs_once()
            done = service.get(job["job_id"])
            self.assertEqual(done["status"], "completed")
            self.assertNotIn("upload_failures", done)
            self.assertEqual(next(s for s in done["steps"] if s["key"] == "publish_notion")["status"], "done")

    def test_local_source_purge_requires_matching_remote_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = self._service(root)
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            state = service._jobs[job["job_id"]]
            context = service._context(state)
            source = context.paths.plugin_dir("fixture") / "part-0001.xdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"native")
            context.paths.merged_xdf.parent.mkdir(parents=True, exist_ok=True)
            context.paths.merged_xdf.write_bytes(b"merged")
            manifest = ArtifactManifestStore()
            manifest.write(
                context.paths,
                identity=context.paths.identity,
                quality_status="valid",
                merge_parity=True,
            )
            relative = source.relative_to(context.paths.root).as_posix()
            result = manifest.purge_plugin_xdfs(
                context.paths,
                remote_sha256={relative: sha256_file(source)},
                session_status="completed",
                merge_parity=True,
            )
            self.assertEqual(result["removed"], [relative])
            self.assertFalse(source.exists())
            self.assertTrue(context.paths.merged_xdf.exists())
            persisted = json.loads((context.paths.manifest_file).read_text(encoding="utf-8"))
            source_entry = next(item for item in persisted["artifacts"] if item["path"] == relative)
            self.assertFalse(source_entry["local_present"])
            self.assertTrue(source_entry["remote_verified"])

    def test_completed_job_retries_due_source_purge(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = MutableClock()
            service = self._service(Path(temp_dir), clock=clock)
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            state = service._jobs[job["job_id"]]
            state["status"] = "completed"
            purge = next(step for step in state["steps"] if step["key"] == "purge_local_sources")
            purge.update(status="retrying", next_attempt_epoch=clock.value + 5)

            self.assertFalse(service._job_has_due_work(state, clock.value))
            self.assertTrue(service._job_has_due_work(state, clock.value + 5))

    def test_source_purge_reconciles_crash_after_unlink_before_progress_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            job = service.commit_submission(SUBMISSION, recording_expected=True)
            context = service._context(service._jobs[job["job_id"]])
            source = context.paths.plugin_dir("fixture") / "part-0001.xdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"native")
            context.paths.merged_xdf.parent.mkdir(parents=True, exist_ok=True)
            context.paths.merged_xdf.write_bytes(b"merged")
            store = ArtifactManifestStore()
            manifest = store.write(
                context.paths,
                identity=context.paths.identity,
                quality_status="valid",
                merge_parity=True,
            )
            relative = source.relative_to(context.paths.root).as_posix()
            digest = sha256_file(source)
            manifest["source_purge"] = {
                "status": "prepared",
                "planned": [{"path": relative, "sha256": digest}],
                "removed": [],
                "remote": "nextcloud",
            }
            (context.paths.manifest_file).write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            source.unlink()  # exact crash window being replayed

            result = store.purge_plugin_xdfs(
                context.paths,
                remote_sha256={relative: digest},
                session_status="completed",
                merge_parity=True,
            )

            self.assertEqual(result["removed"], [relative])
            reconciled = json.loads(
                (context.paths.manifest_file).read_text(encoding="utf-8")
            )
            self.assertEqual(reconciled["source_purge"]["status"], "completed")
            entry = next(item for item in reconciled["artifacts"] if item["path"] == relative)
            self.assertFalse(entry["local_present"])


    def test_republish_uploads_one_finished_destination_again(self) -> None:
        """'Upload again' re-runs only that destination of a completed session."""
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = RecordingDestinationHandler()
            service = self._service(Path(temp_dir), destination_handler=destination)
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True, "nextcloud_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()
            done = service.get(job["job_id"])
            self.assertEqual(done["status"], "completed")
            self.assertEqual(sorted(done["republishable_steps"]), ["publish_nextcloud", "publish_notion"])
            purge_before = next(step for step in done["steps"] if step["key"] == "purge_local_sources")

            again = service.republish(job["job_id"], step_key="publish_notion")
            self.assertEqual(again["status"], "completed", "uploading again never reopens the session")
            self.assertEqual(again["quality_status"], "valid")
            steps = {step["key"]: step for step in again["steps"]}
            self.assertEqual(steps["publish_notion"]["status"], "pending")
            self.assertEqual(steps["publish_nextcloud"]["status"], "done")
            self.assertEqual(service._jobs[job["job_id"]]["republish_counts"], {"notion": 1})

            service.process_due_jobs_once()
            finished = service.get(job["job_id"])
            self.assertEqual(destination.calls.count("notion"), 2)
            self.assertEqual(destination.calls.count("nextcloud"), 1)
            self.assertEqual(next(s for s in finished["steps"] if s["key"] == "publish_notion")["status"], "done")
            self.assertEqual(next(s for s in finished["steps"] if s["key"] == "purge_local_sources"), purge_before)

            service.republish(job["job_id"], step_key="publish_notion")
            self.assertEqual(service._jobs[job["job_id"]]["republish_counts"], {"notion": 2})

    def test_republish_refuses_anything_but_a_finished_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = FailedPersistentDestinationHandler()
            service = self._service(Path(temp_dir), destination_handler=destination)
            job = service.commit_submission(
                SUBMISSION,
                config_data={"study_settings": {"notion_enabled": True, "nextcloud_enabled": True}},
                recording_expected=True,
            )
            service.process_due_jobs_once()
            self.assertEqual(service.get(job["job_id"])["republishable_steps"], ["publish_nextcloud"])
            with self.assertRaises(InvalidTransitionError):
                service.republish(job["job_id"], step_key="publish_notion")  # failed: use retry
            with self.assertRaises(InvalidTransitionError):
                service.republish(job["job_id"], step_key="export_csv")
            with self.assertRaises(InvalidTransitionError):
                service.republish(job["job_id"], step_key="")

            state = service._jobs[job["job_id"]]
            state["status"] = "attention_required"
            self.assertEqual(service.public_job(state)["republishable_steps"], [])
            with self.assertRaises(InvalidTransitionError):
                service.republish(job["job_id"], step_key="publish_nextcloud")
            state["status"] = "completed"

            # A withdrawal deletes the session tree the upload is built from.
            (Path(temp_dir) / state["session_path"] / "meta" / ".submission-commit.json").unlink()
            with self.assertRaises(InvalidTransitionError):
                service.republish(job["job_id"], step_key="publish_nextcloud")


if __name__ == "__main__":
    unittest.main()
