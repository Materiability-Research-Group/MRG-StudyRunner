"""Crash-safe background upload jobs for completed study sessions.

The local result save is the commit point. Network destinations are represented
by small journaled jobs afterwards, so a slow or unavailable service never
keeps the participant waiting and a server restart never loses pending work.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
from typing import Any, Callable

from study_runner.shared.atomic_io import atomic_write_json
from .migrate import build_job_metadata, migrate_legacy_notion_queue


BACKOFF_SECONDS = (30, 120, 600, 1800)
MAX_RETRY_AGE_SECONDS = 48 * 60 * 60
DEFAULT_STATUS_DAYS = 7
MAX_STATUS_DAYS = 90

Executor = Callable[[dict[str, Any]], dict[str, Any] | None]
# A destination's own retry settings on this computer: {"auto_retry": bool,
# "retry_hours": number}. Every upload plugin declares both in its manifest.
RetryPolicy = Callable[[], dict[str, Any]]
# (public job dict, job payload or None) -> {"differs": bool, "fields": [...]}.
# Compares the destination settings a job's payload was frozen with against
# the study's current ones, so the operator can see a changed target before
# retrying. ``None`` payload (a done job's payload is deleted) means no
# comparison is possible.
RetryTargetDescriber = Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]]
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RETRY_TARGETS = ("snapshot", "current")


class UploadJobError(RuntimeError):
    """Raised for invalid job requests or unavailable payloads."""


class PermanentUploadError(UploadJobError):
    """The destination refused the upload for a reason retrying cannot fix.

    Examples: a rejected API key, a page the integration may not access, or a
    missing required setting. The job fails at once instead of retrying for
    48 hours, and the operator sees the real reason.
    """


def retry_delay_seconds(attempts: int) -> int:
    """Return the roadmap backoff: 30 s, 2 m, 10 m, 30 m, then hourly."""
    normalized = max(1, int(attempts))
    if normalized <= len(BACKOFF_SECONDS):
        return BACKOFF_SECONDS[normalized - 1]
    return 60 * 60


class UploadJobService:
    def __init__(
        self,
        data_dir: Path,
        *,
        executors: dict[str, Executor] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.payload_dir = self.data_dir / "upload_jobs"
        self.journal_path = self.data_dir / "upload_jobs.jsonl"
        self.legacy_notion_queue_path = self.data_dir / "notion_upload_queue.jsonl"
        self._executors = dict(executors or {})
        self._retry_policies: dict[str, RetryPolicy] = {}
        self._retry_target_describers: dict[str, RetryTargetDescriber] = {}
        self._clock = clock
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self._replay_journal()
        self._recover_interrupted_attempts()

    def register_executor(self, kind: str, executor: Executor, *, retry_policy: RetryPolicy | None = None) -> None:
        normalized = str(kind or "").strip()
        if not normalized or not SAFE_JOB_ID.fullmatch(normalized):
            raise ValueError("Upload executor kind must be a safe non-empty key.")
        if not callable(executor):
            raise ValueError("Upload executor must be callable.")
        self._executors[normalized] = executor
        if retry_policy is not None:
            self._retry_policies[normalized] = retry_policy

    def register_retry_target_describer(self, kind: str, describer: RetryTargetDescriber) -> None:
        normalized = str(kind or "").strip()
        if not normalized or not SAFE_JOB_ID.fullmatch(normalized):
            raise ValueError("Upload executor kind must be a safe non-empty key.")
        if not callable(describer):
            raise ValueError("Retry target describer must be callable.")
        self._retry_target_describers[normalized] = describer

    def _retry_settings(self, kind: str) -> tuple[bool, float]:
        """(auto_retry, max age in seconds) for one destination, read on use so
        a changed setting applies to the next failure."""
        policy = self._retry_policies.get(kind)
        settings = {}
        if policy is not None:
            try:
                settings = policy() or {}
            except Exception as error:  # a broken setting must not stop the queue
                print(f"[UPLOADS] Could not read retry settings for {kind!r}: {error}")
        auto_retry = settings.get("auto_retry", True) is not False
        try:
            hours = float(settings.get("retry_hours") or MAX_RETRY_AGE_SECONDS / 3600)
        except (TypeError, ValueError):
            hours = MAX_RETRY_AGE_SECONDS / 3600
        return auto_retry, min(168.0, max(1.0, hours)) * 3600

    def enqueue(
        self,
        *,
        kind: str,
        study_id: str,
        participant_id: str,
        session_id: str,
        label: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        job_id: str | None = None,
        created_epoch: float | None = None,
        retry_target: str = "snapshot",
    ) -> dict[str, Any]:
        """Queue one upload; an existing ``job_id`` is returned unchanged.

        ``retry_target="current"`` makes every attempt use the study's
        current destination settings instead of the frozen snapshot, which is
        what an operator's "upload again" asks for.
        """
        normalized_kind = str(kind or "").strip()
        if not normalized_kind or not SAFE_JOB_ID.fullmatch(normalized_kind):
            raise UploadJobError(f"Unsupported upload kind: {normalized_kind or '(empty)'}")
        normalized_job_id = str(job_id or uuid.uuid4()).strip()
        if not SAFE_JOB_ID.fullmatch(normalized_job_id):
            raise UploadJobError("Upload job ID is invalid.")
        if retry_target not in RETRY_TARGETS:
            raise UploadJobError(f"Unsupported retry target: {retry_target}")
        now = float(created_epoch if created_epoch is not None else self._clock())

        with self._lock:
            existing = self._jobs.get(normalized_job_id)
            if existing is not None:
                return _public_job(existing)

            payload_path = self.payload_dir / f"{normalized_job_id}.json"
            atomic_write_json(payload_path, payload)
            job = {
                "job_id": normalized_job_id,
                "kind": normalized_kind,
                "study_id": str(study_id or "").strip(),
                "participant_id": str(participant_id or "").strip(),
                "session_id": str(session_id or "").strip() or normalized_job_id,
                "label": str(label or normalized_kind.title()).strip(),
                "created_at": _iso_time(now),
                "created_epoch": now,
                "attempts": 0,
                "next_attempt_at": _iso_time(now),
                "next_attempt_epoch": now,
                "status": "queued",
                "last_error": "",
                "retry_target": retry_target,
                "steps": [
                    {"key": "local_save", "status": "done"},
                    {"key": normalized_kind, "status": "queued"},
                ],
                "metadata": dict(metadata or {}),
            }
            self._append_event({"event": "created", "job": job})
            self._jobs[normalized_job_id] = job
        self._wake.set()
        return _public_job(job)

    def migrate_legacy_notion_queue(self) -> dict[str, int | str]:
        """Idempotently move the old Notion JSONL queue into journaled jobs."""
        return migrate_legacy_notion_queue(
            self.legacy_notion_queue_path,
            enqueue=self.enqueue,
            clock=self._clock,
        )

    def process_due_jobs_once(self, *, limit: int | None = None) -> int:
        now = self._clock()
        with self._lock:
            due_ids = [
                job_id
                for job_id, job in sorted(
                    self._jobs.items(),
                    key=lambda item: (item[1].get("next_attempt_epoch", 0), item[1].get("created_epoch", 0)),
                )
                if job.get("status") == "queued"
                and float(job.get("next_attempt_epoch") or 0) <= now
            ]
        if limit is not None:
            due_ids = due_ids[: max(0, int(limit))]
        if due_ids:
            with ThreadPoolExecutor(max_workers=min(4, len(due_ids))) as pool:
                list(pool.map(self._run_job, due_ids))
        return len(due_ids)

    def cancel_session(self, session_id: str, *, reason: str = "withdrawn") -> dict[str, Any]:
        """Stop every unfinished upload for one session, permanently (5i).

        Consent withdrawal has to reach the queue, not just the disk: a
        pending job holds its own copy of the participant's data in
        ``payload_dir`` and would publish it minutes later. Cancelling is
        terminal here -- a cancelled job is never retried, rescheduled, or
        resurrected by a failure that was already in flight.

        Jobs already ``done`` are reported, never rewritten. Their data is
        on someone else's server, and this process cannot delete it or
        honestly claim it did; naming them is what lets an operator go and
        do it. That list is the point of the return value.
        """
        wanted = str(session_id or "").strip()
        if not wanted:
            raise UploadJobError("A session_id is required to cancel uploads.")
        cancelled: list[str] = []
        published: list[dict[str, Any]] = []
        with self._lock:
            for job in list(self._jobs.values()):
                if str(job.get("session_id") or "") != wanted:
                    continue
                if job.get("status") == "done":
                    published.append(
                        {
                            "job_id": job["job_id"],
                            "kind": job.get("kind"),
                            "label": job.get("label"),
                            "completed_at": job.get("completed_at"),
                        }
                    )
                    continue
                if job.get("status") == "cancelled":
                    continue
                event = {
                    "event": "cancelled",
                    "job_id": job["job_id"],
                    "cancelled_at": _iso_time(self._clock()),
                    "reason": str(reason or "withdrawn"),
                }
                self._append_event(event)
                self._apply_event(event)
                cancelled.append(job["job_id"])
        removed = 0
        for job_id in cancelled:
            # The queued payload is a second copy of the participant's data.
            # Deleting the session tree while leaving this behind would be a
            # withdrawal that did not withdraw.
            try:
                (self.payload_dir / f"{job_id}.json").unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return {
            "ok": True,
            "cancelled": cancelled,
            "payloads_removed": removed,
            "already_published": published,
        }

    def acknowledge(self, job_ids: list[str]) -> dict[str, Any]:
        """Record that an operator has seen these jobs in their current state.

        Journaled, so a notice that was looked at stays quiet across server
        restarts until something new happens to the job (a new attempt, a
        new status) - the same rule the finalization notice already follows.
        """
        acknowledged = []
        with self._lock:
            for raw_id in job_ids or []:
                job = self._jobs.get(str(raw_id or "").strip())
                if job is None:
                    continue
                signature = _notice_signature(job)
                if job.get("acknowledged_signature") == signature:
                    continue
                event = {"event": "acknowledged", "job_id": job["job_id"], "signature": signature}
                self._append_event(event)
                self._apply_event(event)
                acknowledged.append(job["job_id"])
        return {"ok": True, "acknowledged": acknowledged}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """One job by id, however old it is (``status`` only lists recent ones)."""
        with self._lock:
            job = self._jobs.get(str(job_id or "").strip())
            return _public_job(job) if job is not None else None

    def describe_retry_target(
        self,
        job_id: str,
        *,
        fallback_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compare a job's frozen destination settings with the study's current ones.

        Used before an operator-initiated retry so a target that moved since
        the session finished (a changed Notion page, for example) can be
        shown instead of silently replayed. A done job's payload file is
        deleted, so the caller may pass the frozen ``config_data`` instead.
        """
        with self._lock:
            job = self._jobs.get(str(job_id or "").strip())
            if job is None:
                raise UploadJobError("Upload job was not found.")
            public = _public_job(job)
            kind = str(job.get("kind") or "")
        describer = self._retry_target_describers.get(kind)
        if describer is None:
            return {"differs": False, "fields": []}
        payload_path = self.payload_dir / f"{job_id}.json"
        payload: dict[str, Any] | None = None
        if payload_path.is_file():
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
        return describer(public, payload if payload is not None else fallback_payload)

    def retry(
        self,
        *,
        job_id: str = "",
        all_failed: bool = False,
        kind: str = "",
        target: str = "snapshot",
    ) -> dict[str, Any]:
        normalized_target = str(target or "snapshot").strip()
        if normalized_target not in RETRY_TARGETS:
            raise UploadJobError(f"Unsupported retry target: {normalized_target}")
        with self._lock:
            if all_failed:
                targets = [
                    job
                    for job in self._jobs.values()
                    if job.get("status") == "failed" and (not kind or job.get("kind") == kind)
                ]
            else:
                job = self._jobs.get(str(job_id or "").strip())
                if job is None:
                    raise UploadJobError("Upload job was not found.")
                if kind and job.get("kind") != kind:
                    raise UploadJobError("Upload job does not match the requested destination.")
                if job.get("status") == "done":
                    raise UploadJobError("A completed upload cannot be retried.")
                if job.get("status") == "cancelled":
                    raise UploadJobError("A withdrawn upload cannot be retried.")
                targets = [job]

            now = self._clock()
            retried = []
            for job in targets:
                if job.get("status") == "running":
                    continue
                event = {
                    "event": "retry_requested",
                    "job_id": job["job_id"],
                    "next_attempt_epoch": now,
                    "next_attempt_at": _iso_time(now),
                    "target": normalized_target,
                }
                self._append_event(event)
                self._apply_event(event)
                retried.append(job["job_id"])
        self._wake.set()
        return {"ok": True, "retried": len(retried), "job_ids": retried}

    def status(self, *, days: int = DEFAULT_STATUS_DAYS) -> dict[str, Any]:
        bounded_days = min(MAX_STATUS_DAYS, max(1, int(days)))
        cutoff = self._clock() - bounded_days * 24 * 60 * 60
        with self._lock:
            jobs = [
                _public_job(job)
                for job in self._jobs.values()
                if float(job.get("created_epoch") or 0) >= cutoff
            ]
        jobs.sort(key=lambda job: (job["created_epoch"], job["job_id"]), reverse=True)

        sessions: dict[str, dict[str, Any]] = {}
        for job in jobs:
            session_key = job["session_id"] or job["job_id"]
            session = sessions.setdefault(
                session_key,
                {
                    "session_id": session_key,
                    "study_id": job["study_id"],
                    "participant_id": job["participant_id"],
                    "created_at": job["created_at"],
                    "metadata": job.get("metadata") or {},
                    "jobs": [],
                },
            )
            session["jobs"].append(job)
        return {"ok": True, "days": bounded_days, "sessions": list(sessions.values())}

    def counts(self, *, kind: str = "") -> dict[str, int]:
        with self._lock:
            matching = [
                job
                for job in self._jobs.values()
                if not kind or job.get("kind") == kind
            ]
        return {
            "queued": sum(job.get("status") == "queued" for job in matching),
            "running": sum(job.get("status") == "running" for job in matching),
            "done": sum(job.get("status") == "done" for job in matching),
            "failed": sum(job.get("status") == "failed" for job in matching),
            # 5i: withdrawn work is counted, not hidden. An operator checking
            # the queue should see that jobs stopped because of a withdrawal
            # rather than find them quietly absent.
            "cancelled": sum(job.get("status") == "cancelled" for job in matching),
        }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="study-runner-upload-jobs",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            processed = self.process_due_jobs_once()
            if processed:
                continue
            self._wake.wait(timeout=self._seconds_until_next_job())
            self._wake.clear()

    def _seconds_until_next_job(self) -> float:
        now = self._clock()
        with self._lock:
            due = [
                float(job.get("next_attempt_epoch") or now)
                for job in self._jobs.values()
                if job.get("status") == "queued"
            ]
        return max(0.05, min(5.0, min(due) - now)) if due else 5.0

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.get("status") != "queued":
                return
            attempts = int(job.get("attempts") or 0) + 1
            attempt_event = {"event": "attempt", "job_id": job_id, "attempts": attempts}
            self._append_event(attempt_event)
            self._apply_event(attempt_event)
            payload_path = self.payload_dir / f"{job_id}.json"
            kind = str(job.get("kind") or "")
            retry_target = str(job.get("retry_target") or "snapshot")

        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            # Not persisted to the payload file: an in-memory hint for this
            # one attempt, read and consumed by the destination's executor.
            payload["_retry_target"] = retry_target
            executor = self._executors.get(kind)
            if executor is None:
                raise UploadJobError(f"No executor is registered for {kind}.")
            result = executor(payload) or {"ok": True}
            if result.get("ok") is False:
                raise UploadJobError(str(result.get("error") or f"{kind} upload failed."))
        except Exception as error:
            self._record_failure(job_id, str(error), permanent=isinstance(error, PermanentUploadError))
            return

        with self._lock:
            if (self._jobs.get(job_id) or {}).get("status") == "cancelled":
                # Cancelled mid-attempt. The upload may well have reached the
                # destination, so this is recorded as a published destination
                # by cancel_session's caller rather than silently forgotten.
                return
            done_event = {
                "event": "done",
                "job_id": job_id,
                "completed_at": _iso_time(self._clock()),
                "result": _safe_result(result),
            }
            self._append_event(done_event)
            self._apply_event(done_event)
        try:
            payload_path.unlink(missing_ok=True)
        except OSError as error:
            print(f"[UPLOADS] Could not remove completed job payload {payload_path.name}: {error}")

    def _record_failure(self, job_id: str, error_message: str, *, permanent: bool = False) -> None:
        now = self._clock()
        with self._lock:
            job = self._jobs[job_id]
            if job.get("status") == "cancelled":
                # It was cancelled while this attempt was in flight. Its
                # failure must not schedule a retry of data the participant
                # has withdrawn.
                return
            attempts = int(job.get("attempts") or 1)
            created_epoch = job.get("created_epoch")
            if not isinstance(created_epoch, (int, float)):
                created_epoch = now
            auto_retry, max_age = self._retry_settings(str(job.get("kind") or ""))
            expired = now - float(created_epoch) >= max_age
            # With automatic retry off, a failed upload waits for the
            # operator's "Retry" in the session's progress rail.
            if expired or permanent or not auto_retry:
                event = {
                    "event": "failed",
                    "job_id": job_id,
                    "failed_at": _iso_time(now),
                    "last_error": error_message,
                }
            else:
                next_epoch = now + retry_delay_seconds(attempts)
                event = {
                    "event": "retry_scheduled",
                    "job_id": job_id,
                    "next_attempt_epoch": next_epoch,
                    "next_attempt_at": _iso_time(next_epoch),
                    "last_error": error_message,
                }
            self._append_event(event)
            self._apply_event(event)

    def _append_event(self, event: dict[str, Any]) -> None:
        persisted = {
            **event,
            "at": event.get("at") or _iso_time(self._clock()),
            "at_epoch": event.get("at_epoch") or self._clock(),
        }
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as file_handle:
            file_handle.write(json.dumps(persisted, ensure_ascii=False, separators=(",", ":")) + "\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())

    def _replay_journal(self) -> None:
        if not self.journal_path.exists():
            return
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            print(f"[UPLOADS] Could not read job journal: {error}")
            return
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                self._apply_event(event)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                print(f"[UPLOADS] Ignoring invalid journal event on line {line_number}: {error}")

    def _recover_interrupted_attempts(self) -> None:
        now = self._clock()
        interrupted = [
            job["job_id"]
            for job in self._jobs.values()
            if job.get("status") == "running"
        ]
        for job_id in interrupted:
            event = {
                "event": "retry_scheduled",
                "job_id": job_id,
                "next_attempt_epoch": now,
                "next_attempt_at": _iso_time(now),
                "last_error": "Server restarted during the previous upload attempt.",
            }
            self._append_event(event)
            self._apply_event(event)

    def _apply_event(self, event: dict[str, Any]) -> None:
        event_type = event["event"]
        if event_type == "created":
            job = dict(event["job"])
            if not SAFE_JOB_ID.fullmatch(str(job.get("job_id") or "")):
                raise ValueError("invalid job_id")
            self._jobs[job["job_id"]] = job
            return
        job = self._jobs.get(str(event.get("job_id") or ""))
        if job is None:
            return
        destination_step = next(
            (step for step in job.get("steps", []) if step.get("key") == job.get("kind")),
            None,
        )
        if event_type == "acknowledged":
            job["acknowledged_signature"] = str(event.get("signature") or "")
            return
        if event_type == "attempt":
            job.update(status="running", attempts=int(event["attempts"]), last_error="")
            if destination_step:
                destination_step["status"] = "running"
        elif event_type in {"retry_scheduled", "retry_requested"}:
            job.update(
                status="queued",
                next_attempt_epoch=float(event["next_attempt_epoch"]),
                next_attempt_at=event["next_attempt_at"],
                last_error=str(event.get("last_error") or ""),
            )
            if event_type == "retry_requested" and "target" in event:
                # Only an operator-initiated retry (not the automatic backoff
                # schedule) carries an explicit target choice.
                job["retry_target"] = str(event["target"])
            if destination_step:
                destination_step["status"] = "queued"
        elif event_type == "done":
            job.update(
                status="done",
                completed_at=event["completed_at"],
                last_error="",
                result=event.get("result") or {},
            )
            if destination_step:
                destination_step["status"] = "done"
        elif event_type == "cancelled":
            # Terminal by design (5i): a withdrawal that a later retry could
            # undo would not be a withdrawal.
            job.update(
                status="cancelled",
                cancelled_at=event.get("cancelled_at"),
                last_error="",
                result={},
            )
            if destination_step:
                destination_step["status"] = "cancelled"
        elif event_type == "failed":
            job.update(
                status="failed",
                failed_at=event["failed_at"],
                last_error=str(event.get("last_error") or ""),
            )
            if destination_step:
                destination_step["status"] = "failed"


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "job_id",
        "kind",
        "study_id",
        "participant_id",
        "session_id",
        "label",
        "created_at",
        "created_epoch",
        "attempts",
        "next_attempt_at",
        "status",
        "last_error",
        "retry_target",
        "acknowledged_signature",
        "steps",
        "metadata",
        "completed_at",
        "failed_at",
        "result",
    )
    public = {key: job[key] for key in allowed if key in job}
    # Compared with acknowledged_signature by the admin's upload notice.
    public["notice_signature"] = _notice_signature(job)
    return public


def _notice_signature(job: dict[str, Any]) -> str:
    """What makes a job's notice "new again": its status and attempt count."""
    return f"{job.get('status') or ''}:{int(job.get('attempts') or 0)}"


def _safe_result(result: dict[str, Any]) -> dict[str, Any]:
    # remote_sha256 is what finalization's purge step verifies local sources
    # against; dropping it here kept every source file forever.
    safe = {
        key: value
        for key, value in result.items()
        if key in {"ok", "uploaded", "endpoint", "message", "remote_path"}
    }
    remote_hashes = result.get("remote_sha256")
    if isinstance(remote_hashes, dict):
        safe["remote_sha256"] = {
            str(name): str(digest) for name, digest in remote_hashes.items() if isinstance(digest, str)
        }
    return safe


def _iso_time(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
