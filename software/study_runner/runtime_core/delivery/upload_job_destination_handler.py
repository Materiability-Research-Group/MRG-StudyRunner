"""Bridge finalization destination steps to the persistent upload queue.

Every destination step of a finalization job becomes one upload job with a
deterministic id, so a restart or a repeated advance finds the same job again
instead of uploading twice. An operator's "upload again" (``republish``)
raises a per-destination counter, which gives the next attempt a new job id
and therefore a fresh upload with the study's current settings.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import time
from typing import Any

from .finalization_service import (
    DeferredStep,
    FinalizationContext,
    FinalizationError,
    StepResult,
    _read_json_object as read_json_object,
)


class UploadJobDestinationHandler:
    """Bridge finalization destinations to the existing persistent upload queue."""

    def __init__(self, service: Any, data_dir: Path) -> None:
        self.service = service
        self.data_dir = Path(data_dir).resolve()

    def publish(self, destination: str, context: FinalizationContext) -> StepResult:
        job_id = upload_job_id(context.state, destination)
        existing = self.service.get_job(job_id)
        if existing is None:
            existing = self.service.enqueue(
                kind=destination,
                study_id=context.state["study_id"],
                participant_id=context.state["participant_id"],
                session_id=context.state["session_id"],
                label=destination.title(),
                payload=self._payload(context),
                metadata={
                    "finalization_job_id": context.state["job_id"],
                    "session_folder": context.paths.root.name,
                },
                job_id=job_id,
                # Uploading again exists to apply what changed since: the
                # destination's current settings, not the frozen snapshot.
                retry_target="current" if republish_count(context.state, destination) else "snapshot",
            )
        status = str(existing.get("status") or "")
        if status == "done":
            upload_result = existing.get("result") or {}
            details = {"upload_job_id": job_id, "result": upload_result}
            if isinstance(upload_result.get("remote_sha256"), dict):
                details["remote_sha256"] = dict(upload_result["remote_sha256"])
            return StepResult("done", details)
        if status == "failed":
            raise FinalizationError(str(existing.get("last_error") or f"{destination} upload failed."))
        # Wait for the upload queue's own next attempt instead of polling every
        # second, and show why the previous attempt failed.
        last_error = str(existing.get("last_error") or "").strip()
        message = f"{destination} upload is {status or 'queued'}"
        if last_error:
            message += f"; last attempt: {last_error}"
        raise DeferredStep(message + ".", retry_after_seconds=seconds_until(existing.get("next_attempt_at")))

    def retry(self, destination: str, context: FinalizationContext, *, target: str = "snapshot") -> None:
        """Retry the deterministic upload job behind a finalization step.

        Resetting only the outer state-machine step would immediately find the
        same failed upload job again. The two journals therefore transition
        together while keeping the stable destination job id.
        """

        job_id = upload_job_id(context.state, destination)
        existing = self.service.get_job(job_id)
        if existing is None:
            return
        status = str(existing.get("status") or "")
        if status in {"done", "running"}:
            return
        try:
            self.service.retry(job_id=job_id, kind=destination, target=target)
        except Exception as error:
            raise FinalizationError(
                f"Could not retry the persistent {destination} upload: {error}"
            ) from error

    def describe_retry_target(self, destination: str, context: FinalizationContext) -> dict[str, Any]:
        job_id = upload_job_id(context.state, destination)
        if self.service.get_job(job_id) is None:
            return {"differs": False, "fields": []}
        return self.service.describe_retry_target(
            job_id,
            fallback_payload={"config_data": context.config_data},
        )

    def _payload(self, context: FinalizationContext) -> dict[str, Any]:
        result_path = context.paths.result_file
        result_payload = read_json_object(result_path) if result_path.is_file() else dict(context.submission)
        relative_root = context.paths.root.relative_to(self.data_dir).as_posix()
        saved_output = {
            "participant_dir": relative_root,
            "session_dir": relative_root,
            "session_relative_path": relative_root,
            "json_file": f"{relative_root}/answers/result.json",
            "card_summary_file": f"{relative_root}/answers/card-summary.json",
            "manifest_file": f"{relative_root}/meta/manifest.json",
            "xdf_file": (
                f"{relative_root}/derived/session.xdf"
                if context.paths.merged_xdf.is_file()
                else None
            ),
            "card_summary": (
                read_json_object(context.paths.card_summary_file)
                if context.paths.card_summary_file.is_file()
                else {}
            ),
        }
        return {
            "result_payload": result_payload,
            "hardware_config": context.hardware_config,
            "saved_output": saved_output,
            "config_data": context.config_data,
        }


def republish_count(state: dict[str, Any], destination: str) -> int:
    return max(0, int((state.get("republish_counts") or {}).get(destination) or 0))


def upload_job_id(state: dict[str, Any], destination: str) -> str:
    """``<finalization>-g<generation>-<destination>[-r<n>]``, stable per attempt."""
    generation = max(1, int(state.get("publication_generation") or 1))
    job_id = f"{state['job_id']}-g{generation}-{destination}"
    count = republish_count(state, destination)
    return f"{job_id}-r{count}" if count else job_id


def seconds_until(iso_value: Any, *, minimum: float = 1.0, maximum: float = 300.0) -> float:
    """Seconds until an ISO timestamp, clamped so a clock skew never stalls a step."""
    try:
        target = dt.datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return minimum
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    remaining = target.timestamp() - time.time()
    return max(minimum, min(maximum, remaining))
