"""Package 5i: carrying out a consent withdrawal without overclaiming.

The cases that matter are the awkward ones: an interrupted run, a session
whose data was already published somewhere this process cannot reach, and
the question of what a withdrawn session looks like afterwards to someone
reading the session list.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.contracts.session_lifecycle import SEALED, WITHDRAWN, WITHDRAWN_MARKER
from study_runner.runtime_core.delivery.upload_jobs_service import (
    UploadJobError,
    UploadJobService,
)
from study_runner.runtime_core.delivery.withdrawal_service import (
    WithdrawalError,
    WithdrawalService,
)
from study_runner.runtime_core.studies.session_journal_service import SessionJournalStore
from study_runner.runtime_core.studies.sessions_index_service import list_sessions


SESSION_ID = "20260908T101500Z-abcdef"


def _populate_session(root: Path) -> None:
    """A session tree with data in all the places a withdrawal must reach."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "result.json").write_text(
        json.dumps({"session_id": SESSION_ID, "answers": {"q1": "yes"}}), encoding="utf-8"
    )
    (root / "COMPLETE.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    (root / "quality.jsonl").write_text('{"event":"summary"}\n', encoding="utf-8")
    raw = root / "raw" / "plugins" / "fixture"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "fixture.xdf").write_bytes(b"XDF")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "worker.jsonl").write_text("{}\n", encoding="utf-8")


class WithdrawalServiceTests(unittest.TestCase):
    def test_the_data_goes_and_the_tombstone_stays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)

            service = WithdrawalService(data_dir)
            state = service.withdraw(
                session_id=SESSION_ID,
                session_root=session,
                reason="participant request",
                requested_by="operator",
            )

            self.assertEqual(state["status"], "withdrawn")
            self.assertTrue(session.is_dir())
            self.assertEqual([path.name for path in session.iterdir()], [WITHDRAWN_MARKER])
            marker = json.loads((session / WITHDRAWN_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["status"], "withdrawn")
            self.assertEqual(marker["session_id"], SESSION_ID)

    def test_the_ledger_lives_outside_the_folder_it_empties(self) -> None:
        """Otherwise an interrupted run erases its own record of progress."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            service = WithdrawalService(data_dir)
            service.withdraw(session_id=SESSION_ID, session_root=session)

            ledger = service.load_state(SESSION_ID)
            self.assertIsNotNone(ledger)
            self.assertFalse(service.state_path(SESSION_ID).is_relative_to(session))
            self.assertEqual(ledger["steps"]["delete_session_contents"]["status"], "done")
            self.assertEqual(ledger["steps"]["delete_session_contents"]["details"]["removed"], 5)

    def test_an_interrupted_withdrawal_resumes_instead_of_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)

            attempts: list[str] = []

            def failing_stopper(session_id: str):
                attempts.append(session_id)
                raise RuntimeError("recorder is not responding")

            service = WithdrawalService(data_dir, recording_stopper=failing_stopper)
            with self.assertRaises(WithdrawalError):
                service.withdraw(session_id=SESSION_ID, session_root=session)

            # Nothing was deleted: the failure came before any deletion step.
            self.assertTrue((session / "result.json").is_file())
            self.assertEqual(service.load_state(SESSION_ID)["status"], "attention_required")

            resumed = WithdrawalService(data_dir, recording_stopper=lambda _id: {"stopped": True})
            state = resumed.withdraw(session_id=SESSION_ID, session_root=session)
            self.assertEqual(state["status"], "withdrawn")
            self.assertEqual(len(attempts), 1)

    def test_finishing_a_withdrawal_twice_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            service = WithdrawalService(data_dir)
            service.withdraw(session_id=SESSION_ID, session_root=session)
            first = (session / WITHDRAWN_MARKER).read_text(encoding="utf-8")
            service.withdraw(session_id=SESSION_ID, session_root=session)
            self.assertEqual((session / WITHDRAWN_MARKER).read_text(encoding="utf-8"), first)

    def test_journal_copies_outside_the_session_folder_are_removed_too(self) -> None:
        """The copies a plain folder deletion would silently leave behind."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            store = SessionJournalStore(data_dir)
            store.append("trial", SESSION_ID, "trial_completed", {"events": {"e1": {}}})
            journal = store.journal_path("trial", SESSION_ID)
            self.assertTrue(journal.is_file())

            WithdrawalService(data_dir, journal_store=store).withdraw(
                session_id=SESSION_ID, session_root=session
            )
            self.assertFalse(journal.is_file())

    def test_a_session_id_that_could_escape_its_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = WithdrawalService(Path(temp_dir))
            for bad in ("../elsewhere", "", "  ", "a/b"):
                with self.assertRaises(WithdrawalError):
                    service.withdraw(session_id=bad, session_root=Path(temp_dir) / "x")

    def test_a_consent_withdrawal_tombstone_says_so(self) -> None:
        """The default kind, written explicitly rather than left implicit."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            WithdrawalService(data_dir).withdraw(session_id=SESSION_ID, session_root=session)
            marker = json.loads((session / WITHDRAWN_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["kind"], "consent_withdrawal")

    def test_an_unknown_kind_is_refused_rather_than_guessed_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            with self.assertRaises(WithdrawalError):
                WithdrawalService(data_dir).withdraw(
                    session_id=SESSION_ID, session_root=session, kind="not_a_real_kind"
                )


class AdminAbortTests(unittest.TestCase):
    """kind="admin_abort" (via .abort()): same tombstone, nothing deleted."""

    def test_abort_stops_recording_and_keeps_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            before = sorted(path.name for path in session.iterdir())

            stopped_for: list[str] = []
            service = WithdrawalService(
                data_dir, recording_stopper=lambda session_id: stopped_for.append(session_id) or {"frozen": True}
            )
            state = service.abort(
                session_id=SESSION_ID, session_root=session, reason="tablet unresponsive", requested_by="operator",
            )

            self.assertEqual(state["status"], "withdrawn")
            self.assertEqual(stopped_for, [SESSION_ID])
            # Nothing removed -- every pre-existing file, plus the new tombstone.
            after = sorted(path.name for path in session.iterdir())
            self.assertEqual(after, sorted(before + [WITHDRAWN_MARKER]))
            marker = json.loads((session / WITHDRAWN_MARKER).read_text(encoding="utf-8"))
            self.assertEqual(marker["kind"], "admin_abort")
            self.assertEqual(marker["reason"], "tablet unresponsive")
            self.assertEqual(marker["requested_by"], "operator")

    def test_an_abort_without_a_reason_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            service = WithdrawalService(data_dir)
            for empty in ("", "   "):
                with self.assertRaises(WithdrawalError):
                    service.abort(session_id=SESSION_ID, session_root=session, reason=empty)
            # Refused before any step ran.
            self.assertIsNone(service.load_state(SESSION_ID))

    def test_a_session_that_was_already_submitted_is_not_marked_aborted(self) -> None:
        """An update after the session ended used to tombstone it anyway."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            (session / "meta").mkdir()
            (session / "meta" / ".submission-commit.json").write_text("{}", encoding="utf-8")
            stopped_for: list[str] = []
            service = WithdrawalService(data_dir, recording_stopper=stopped_for.append)

            state = service.abort(session_id=SESSION_ID, session_root=session, reason="Software update")

            self.assertEqual(state["status"], "already_submitted")
            self.assertFalse((session / WITHDRAWN_MARKER).exists())
            self.assertEqual(stopped_for, [])
            self.assertIsNone(service.load_state(SESSION_ID))

    def test_abort_never_touches_uploads_or_journals(self) -> None:
        """Only stop_recording and write_tombstone run -- nothing else."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            service = WithdrawalService(data_dir)
            state = service.abort(session_id=SESSION_ID, session_root=session, reason="stuck")
            self.assertEqual(set(state["steps"]), {"stop_recording", "write_tombstone"})


class WithdrawalAndUploadsTests(unittest.TestCase):
    def _service(self, data_dir: Path) -> UploadJobService:
        return UploadJobService(data_dir, clock=lambda: 1000.0)

    def test_pending_uploads_are_cancelled_and_their_payload_copies_deleted(self) -> None:
        """A queued job holds a second copy of the participant's data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            uploads = self._service(data_dir)
            uploads.enqueue(
                kind="notion",
                study_id="s",
                participant_id="p",
                session_id=SESSION_ID,
                label="Notion",
                payload={"answers": {"q1": "yes"}},
                job_id="job-1",
            )
            self.assertTrue((uploads.payload_dir / "job-1.json").is_file())

            result = uploads.cancel_session(SESSION_ID)
            self.assertEqual(result["cancelled"], ["job-1"])
            self.assertFalse((uploads.payload_dir / "job-1.json").is_file())
            self.assertEqual(uploads.counts()["cancelled"], 1)

    def test_a_cancelled_upload_is_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            uploads = self._service(data_dir)
            uploads.enqueue(
                kind="notion",
                study_id="s",
                participant_id="p",
                session_id=SESSION_ID,
                label="Notion",
                payload={"answers": {}},
                job_id="job-1",
            )
            uploads.cancel_session(SESSION_ID)

            self.assertEqual(uploads.process_due_jobs_once(), 0)
            with self.assertRaises(UploadJobError):
                uploads.retry(job_id="job-1")
            self.assertEqual(uploads.retry(all_failed=True)["retried"], 0)

    def test_a_cancellation_survives_a_restart(self) -> None:
        """The journal replay must not resurrect withdrawn work."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            uploads = self._service(data_dir)
            uploads.enqueue(
                kind="notion",
                study_id="s",
                participant_id="p",
                session_id=SESSION_ID,
                label="Notion",
                payload={"answers": {}},
                job_id="job-1",
            )
            uploads.cancel_session(SESSION_ID)

            restarted = self._service(data_dir)
            self.assertEqual(restarted.counts()["cancelled"], 1)
            self.assertEqual(restarted.process_due_jobs_once(), 0)

    def test_an_already_published_destination_is_named_not_claimed_deleted(self) -> None:
        """This process cannot reach someone else's server; it must not pretend."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            uploads = self._service(data_dir)
            uploads.register_executor("notion", lambda _payload: {"ok": True})
            uploads.enqueue(
                kind="notion",
                study_id="s",
                participant_id="p",
                session_id=SESSION_ID,
                label="Notion",
                payload={"answers": {}},
                job_id="job-1",
            )
            uploads.process_due_jobs_once()

            session = data_dir / "sessions" / SESSION_ID
            _populate_session(session)
            state = WithdrawalService(data_dir, upload_jobs=uploads).withdraw(
                session_id=SESSION_ID, session_root=session
            )

            published = state["already_published"]
            self.assertEqual([entry["job_id"] for entry in published], ["job-1"])
            marker = json.loads((session / WITHDRAWN_MARKER).read_text(encoding="utf-8"))
            self.assertEqual([entry["kind"] for entry in marker["already_published"]], ["notion"])


class WithdrawnSessionVisibilityTests(unittest.TestCase):
    """A withdrawn session must not simply vanish from the list."""

    def _tombstoned_tree(self, data_dir: Path) -> Path:
        session = data_dir / "study-a" / "p1" / SESSION_ID
        _populate_session(session)
        WithdrawalService(data_dir).withdraw(session_id=SESSION_ID, session_root=session)
        return session

    def test_a_withdrawn_session_is_still_listed_as_withdrawn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            self._tombstoned_tree(data_dir)
            sessions = list_sessions(data_dir)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["lifecycle"], WITHDRAWN)
            self.assertEqual(sessions[0]["session_id"], SESSION_ID)

    def test_the_listing_carries_no_answers_from_the_withdrawn_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            self._tombstoned_tree(data_dir)
            summary = list_sessions(data_dir)[0]
            self.assertEqual(summary["answers_count"], 0)
            self.assertEqual(summary["files"], [])

    def test_the_listing_names_the_withdrawal_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            self._tombstoned_tree(data_dir)
            self.assertEqual(list_sessions(data_dir)[0]["withdrawal_kind"], "consent_withdrawal")

    def test_a_tombstone_written_before_the_kind_field_existed_still_reads(self) -> None:
        """A rolling deploy must not crash on a marker an older build wrote."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = self._tombstoned_tree(data_dir)
            marker_path = session / WITHDRAWN_MARKER
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            del marker["kind"]
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

            summary = list_sessions(data_dir)[0]
            self.assertEqual(summary["lifecycle"], WITHDRAWN)
            self.assertEqual(summary["withdrawal_kind"], "consent_withdrawal")

    def test_an_aborted_session_is_listed_with_its_own_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "study-a" / "p1" / SESSION_ID
            _populate_session(session)
            WithdrawalService(data_dir).abort(session_id=SESSION_ID, session_root=session, reason="stuck")

            summary = list_sessions(data_dir)[0]
            self.assertEqual(summary["lifecycle"], WITHDRAWN)
            self.assertEqual(summary["withdrawal_kind"], "admin_abort")
            # The point of an abort: the recording's files are still there.
            self.assertTrue((session / "result.json").is_file())

    def _finalized_then_aborted(self, data_dir: Path, *, submitted: float, aborted: float) -> Path:
        session = data_dir / "study-a" / "p1" / SESSION_ID
        _populate_session(session)
        (session / "meta").mkdir()
        (session / "meta" / "finalization-state.json").write_text(
            json.dumps({"status": "completed", "created_epoch": submitted}), encoding="utf-8",
        )
        WithdrawalService(data_dir, clock=lambda: aborted).abort(
            session_id=SESSION_ID, session_root=session, reason="Software update",
        )
        return session

    def test_an_abort_written_after_the_submission_does_not_count(self) -> None:
        """Repairs the tombstones in-app updates wrote onto finished sessions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = self._finalized_then_aborted(data_dir, submitted=100.0, aborted=200.0)
            self.assertTrue((session / WITHDRAWN_MARKER).is_file())

            summary = list_sessions(data_dir)[0]
            self.assertEqual(summary["lifecycle"], SEALED)
            self.assertEqual(summary["withdrawal_kind"], "")
            self.assertEqual(summary["status"], "completed")

    def test_an_abort_before_the_submission_still_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            self._finalized_then_aborted(data_dir, submitted=300.0, aborted=200.0)

            summary = list_sessions(data_dir)[0]
            self.assertEqual(summary["lifecycle"], WITHDRAWN)
            self.assertEqual(summary["withdrawal_kind"], "admin_abort")

    def test_a_session_with_no_completion_marker_becomes_visible_once_aborted(self) -> None:
        """The actual gap this exists to close: a live session, never
        completed, is invisible to the session list until something -- here,
        the abort's own tombstone -- gives it a final marker.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            session = data_dir / "study-a" / "p1" / SESSION_ID
            session.mkdir(parents=True)
            raw = session / "raw" / "plugins" / "fixture"
            raw.mkdir(parents=True)
            (raw / "fixture.xdf").write_bytes(b"XDF")
            # No result.json, no COMPLETE.json: this session never finished.

            self.assertEqual(list_sessions(data_dir), [])

            WithdrawalService(data_dir).abort(
                session_id=SESSION_ID, session_root=session, reason="hung mid-recording",
            )

            sessions = list_sessions(data_dir)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["lifecycle"], WITHDRAWN)
            self.assertEqual(sessions[0]["withdrawal_kind"], "admin_abort")
            self.assertTrue((raw / "fixture.xdf").is_file())


if __name__ == "__main__":
    unittest.main()
