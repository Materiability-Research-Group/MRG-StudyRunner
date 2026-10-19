"""Carry out a consent withdrawal, replayably and without overclaiming.

Package 5i (docs/archive/architecture-1.0-umbau.md), target doc §10. Package 5a
already defined the destination -- ``WITHDRAWN`` is reachable from every
other lifecycle state, including from ``SEALED`` months later, and nothing
is reachable from it -- so this module only has to get there safely.

A second, unrelated reason to land in the same place: an operator stopping a
session that is stuck or misbehaving is not withdrawing anyone's consent and
must not delete their data on the way out. ``kind="admin_abort"`` (see
``abort()``) reuses the exact same ledger, tombstone file and terminal
lifecycle state -- stopping any live recording, keeping everything already
captured -- and skips only the steps that would remove or cancel anything.
The tombstone's ``kind`` field is the one place the two are told apart.

Three things shape the design, and each of them is a constraint the
obvious implementation gets wrong:

**The ledger has to outlive what it deletes.** A withdrawal that recorded
its own progress inside the session folder would erase that record halfway
through, and an interrupted run could never tell "already deleted" from
"never started". So the ledger lives in ``runtime/withdrawals/`` and the
session tree is only ever a target.

**Deleting everything would hide the withdrawal.** A session folder that
simply vanishes is indistinguishable from data loss -- exactly the silence
this project spends its effort removing. The contents go; the folder stays,
holding one ``WITHDRAWN.json`` and nothing else. A tombstone that carries
no participant data but proves the withdrawal happened.

**Published data cannot be un-published by wishing.** Uploads that already
completed put a copy on someone else's server. This process can stop the
queue, and does; it cannot delete the remote copy, and never says it did.
Those destinations are listed by name in the tombstone so an operator knows
exactly where to go. Claiming external deletion without evidence would be
the one failure here that no later check could catch.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Mapping

from study_runner.contracts.session_lifecycle import WITHDRAWN_MARKER
from study_runner.shared.atomic_io import atomic_write_json

WITHDRAWAL_SCHEMA = "study-runner/withdrawal/v1"
WITHDRAWAL_STATE_SCHEMA = "study-runner/withdrawal-state/v1"
# Written when the participant submits (ArtifactPaths.submission_commit_file).
SUBMISSION_COMMIT_NAME = ".submission-commit.json"

# Ordered, and the order is a safety property: the writers are stopped and
# the queue is drained *before* anything is deleted, so nothing can publish
# or re-create a file behind the deletion's back.
WITHDRAWAL_STEPS = (
    "stop_recording",
    "cancel_uploads",
    "delete_session_journals",
    "delete_session_contents",
    "write_tombstone",
)

# An abort stops the same way but deletes nothing: no cancel/delete steps,
# just freeze whatever is recording and leave a tombstone next to the data
# it is not touching.
ABORT_STEPS = ("stop_recording", "write_tombstone")

_KNOWN_KINDS = {"consent_withdrawal": WITHDRAWAL_STEPS, "admin_abort": ABORT_STEPS}


class WithdrawalError(RuntimeError):
    """A withdrawal could not be carried out safely."""


class WithdrawalService:
    """Run and resume the withdrawal of one session's data."""

    def __init__(
        self,
        data_dir: Path,
        *,
        upload_jobs: Any | None = None,
        journal_store: Any | None = None,
        recording_stopper: Callable[[str], Mapping[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "runtime" / "withdrawals"
        self._upload_jobs = upload_jobs
        self._journal_store = journal_store
        # Injected rather than imported: stopping a recording is the host
        # runtime's job, and reaching into it from here would tie the
        # withdrawal path to the recording stack it must be able to run
        # without (a sealed session has no recorder left to stop).
        self._recording_stopper = recording_stopper
        self._clock = clock

    def state_path(self, session_id: str) -> Path:
        return self.root / f"{_safe_session_id(session_id)}.json"

    def load_state(self, session_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.state_path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def withdraw(
        self,
        *,
        session_id: str,
        session_root: Path,
        reason: str = "",
        requested_by: str = "",
        kind: str = "consent_withdrawal",
    ) -> dict[str, Any]:
        """Withdraw (or abort) one session, resuming an interrupted run if any.

        Safe to call again after any interruption: every step records its
        own completion in the ledger and is a no-op once done, so a crash
        between two steps costs a repeat of at most one of them.

        ``kind`` selects which steps run -- the full, destructive
        ``"consent_withdrawal"`` (default, unchanged) or the data-preserving
        ``"admin_abort"`` (see ``ABORT_STEPS``/``abort()``). Fails closed on
        an unrecognized kind rather than silently running one or the other.
        """
        try:
            steps = _KNOWN_KINDS[kind]
        except KeyError:
            raise WithdrawalError(f"unknown withdrawal kind: {kind!r}") from None
        safe_id = _safe_session_id(session_id)
        root = Path(session_root)
        state = self.load_state(safe_id) or {
            "schema": WITHDRAWAL_STATE_SCHEMA,
            "session_id": safe_id,
            "session_path": str(root),
            "study_id": _study_id_for_root(root, self.data_dir),
            "requested_at_epoch": float(self._clock()),
            "requested_by": str(requested_by or ""),
            "reason": str(reason or ""),
            "kind": kind,
            "status": "running",
            "steps": {key: {"status": "pending"} for key in steps},
            "already_published": [],
        }
        state.setdefault("steps", {})
        state.setdefault("kind", kind)
        self._persist(state)

        for step in steps:
            entry = state["steps"].setdefault(step, {"status": "pending"})
            if entry.get("status") == "done":
                continue
            try:
                entry["details"] = getattr(self, f"_step_{step}")(state, root) or {}
            except Exception as error:
                entry.update(status="failed", error=f"{type(error).__name__}: {error}")
                state["status"] = "attention_required"
                self._persist(state)
                raise WithdrawalError(
                    f"withdrawal step {step!r} failed: {error}"
                ) from error
            entry["status"] = "done"
            entry["completed_at_epoch"] = float(self._clock())
            self._persist(state)

        state["status"] = "withdrawn"
        state["completed_at_epoch"] = float(self._clock())
        self._persist(state)
        try:
            from study_runner.runtime_core.studies.session_index_csv import rebuild_study_index
            study_id = str(state.get("study_id") or "")
            if study_id:
                rebuild_study_index(self.data_dir, study_id)
        except Exception as error:
            print(f"[WITHDRAWAL] Could not refresh session index: {error}")
        return state

    def abort(
        self,
        *,
        session_id: str,
        session_root: Path,
        reason: str,
        requested_by: str = "",
    ) -> dict[str, Any]:
        """Stop a live session and mark it aborted, keeping everything captured.

        A thin, purpose-named entry point onto ``withdraw(kind="admin_abort")``
        so a caller stopping a stuck or misbehaving session never has to name
        the destructive default kind to avoid it. ``reason`` is required here
        (unlike ``withdraw``'s optional one) -- an abort with no stated reason
        is exactly the silent "something happened" this project's tombstones
        exist to rule out.
        """
        if not str(reason or "").strip():
            raise WithdrawalError("an abort needs a non-empty reason")
        if (Path(session_root) / "meta" / SUBMISSION_COMMIT_NAME).is_file():
            # The participant already submitted: there is nothing live left
            # to stop, and a tombstone would mark a finished session aborted.
            return {
                "schema": WITHDRAWAL_STATE_SCHEMA,
                "session_id": _safe_session_id(session_id),
                "kind": "admin_abort",
                "status": "already_submitted",
            }
        return self.withdraw(
            session_id=session_id,
            session_root=session_root,
            reason=reason,
            requested_by=requested_by,
            kind="admin_abort",
        )

    # -- steps ---------------------------------------------------------

    def _step_stop_recording(self, state: dict[str, Any], root: Path) -> dict[str, Any]:
        """Stop any writer still holding this session open.

        A session can be withdrawn mid-recording, so this runs first and
        unconditionally. No stopper configured means there is nothing that
        could be recording -- said out loud rather than assumed.
        """
        if self._recording_stopper is None:
            return {"skipped": "no recording stopper configured"}
        return {"stopped": dict(self._recording_stopper(state["session_id"]) or {})}

    def _step_cancel_uploads(self, state: dict[str, Any], root: Path) -> dict[str, Any]:
        """Drain the queue before deleting, so nothing publishes afterwards."""
        if self._upload_jobs is None:
            return {"skipped": "no upload queue configured"}
        result = self._upload_jobs.cancel_session(state["session_id"], reason="withdrawn")
        published = list(result.get("already_published") or [])
        # Kept in the ledger *and* the tombstone: this is the one part of a
        # withdrawal that the operator, not the software, has to finish.
        state["already_published"] = published
        return {
            "cancelled": list(result.get("cancelled") or []),
            "payloads_removed": int(result.get("payloads_removed") or 0),
            "already_published": published,
        }

    def _step_delete_session_journals(self, state: dict[str, Any], root: Path) -> dict[str, Any]:
        """Remove the journal copies that live outside the session folder.

        These are the copies a deletion of the session tree would miss, and
        missing them would leave the participant's trial-by-trial record in
        place after a withdrawal that reported success.
        """
        if self._journal_store is None:
            return {"skipped": "no journal store configured"}
        removed: list[str] = []
        for stream in ("session", "trial"):
            path = Path(self._journal_store.journal_path(stream, state["session_id"]))
            if path.is_file():
                path.unlink()
                removed.append(path.name)
            parent = path.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        return {"removed": removed}

    def _step_delete_session_contents(self, state: dict[str, Any], root: Path) -> dict[str, Any]:
        """Empty the session folder, keeping the folder itself.

        Everything goes: raw and derived recordings, manifests, results,
        the quality/timing/checkpoint journals, logs. What is counted here
        is what was removed, because a withdrawal that reports "0 files"
        on a session that had data is a bug worth seeing in the ledger.
        """
        if not root.is_dir():
            return {"skipped": "session folder does not exist", "removed": 0}
        removed = 0
        for entry in sorted(root.iterdir()):
            if entry.name == WITHDRAWN_MARKER:
                continue
            if entry.is_dir() and not entry.is_symlink():
                removed += sum(1 for path in entry.rglob("*") if path.is_file())
                shutil.rmtree(entry)
            else:
                entry.unlink()
                removed += 1
        _fsync_directory(root)
        return {"removed": removed}

    def _step_write_tombstone(self, state: dict[str, Any], root: Path) -> dict[str, Any]:
        """Leave the one file that proves this was a withdrawal, not a loss."""
        root.mkdir(parents=True, exist_ok=True)
        marker = {
            "schema": WITHDRAWAL_SCHEMA,
            "status": "withdrawn",
            # Absent on tombstones written before this field existed; every
            # reader must treat a missing kind as "consent_withdrawal", the
            # only kind there was.
            "kind": state.get("kind") or "consent_withdrawal",
            "session_id": state["session_id"],
            "withdrawn_at_epoch": float(self._clock()),
            "requested_by": state.get("requested_by") or "",
            # The operator-supplied reason, not the participant's: nothing
            # about why a person withdrew belongs in a file that survives
            # the deletion of everything else they contributed.
            "reason": state.get("reason") or "",
            "already_published": list(state.get("already_published") or []),
        }
        atomic_write_json(root / WITHDRAWN_MARKER, marker)
        _fsync_directory(root)
        return {"marker": WITHDRAWN_MARKER}

    # -- ledger --------------------------------------------------------

    def _persist(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.state_path(state["session_id"]), state)


def _safe_session_id(session_id: Any) -> str:
    value = str(session_id or "").strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise WithdrawalError("a withdrawal needs a concrete, single-segment session_id")
    return value


def _study_id_for_root(root: Path, data_dir: Path) -> str:
    try:
        identity = json.loads((root / "meta" / "session-identity.json").read_text(encoding="utf-8"))
        if isinstance(identity, dict) and identity.get("study_id"):
            return str(identity["study_id"])
    except (OSError, ValueError):
        pass
    return root.parent.parent.name if root.parent.parent.parent.resolve() == data_dir.resolve() else ""


def _fsync_directory(directory: Path) -> None:
    """Make a deletion or creation of directory entries durable.

    Without this the tombstone can be in the page cache while the deletions
    are not, and a power loss could leave a folder that says "withdrawn"
    next to files that are still there.
    """
    try:
        handle = os.open(directory, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except (OSError, AttributeError):
        return  # Windows has no directory fsync; the atomic writes carry it.
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)
