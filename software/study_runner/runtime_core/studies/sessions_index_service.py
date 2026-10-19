"""Read-only index and timeline access for canonical finalized sessions.

Legacy flat result directories are intentionally never scanned. They remain
untouched on disk, while this browser only exposes the immutable v3 layout:
``<study>/<participant>/<UTC>__<session-id>``.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
from pathlib import Path, PurePosixPath
import threading
from typing import Any

from study_runner.clock_core.assessment import display_epoch_offset
from study_runner.contracts.session_lifecycle import (
    WITHDRAWN_MARKER,
    derive_session_lifecycle,
)

from study_runner.runtime_core.delivery.software_provenance import legacy_software_provenance

from .card_summary_service import CardSummaryError, PyXdfSampleReader
from .session_quality_summary import summarize_session_quality


DEFAULT_MAX_POINTS = 2000
MAX_MAX_POINTS = 10_000
_INDEX_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], list[dict[str, Any]]]] = {}
_STREAM_CACHE_LOCK = threading.RLock()
_STREAM_CACHE: tuple[str, int, int, list[dict[str, Any]]] | None = None
_OFFSET_CACHE: tuple[list[dict[str, Any]], float] | None = None
_PUBLIC_CONTROL_FILES = {
    "answers/submission.json",
    "answers/result.json",
    "answers/card-summary.json",
    "meta/manifest.json",
    "meta/checksums.sha256",
    "meta/session-identity.json",
    "COMPLETE.json",
    "ATTENTION_REQUIRED.json",
}


class SessionNotFoundError(LookupError):
    pass


def list_sessions(data_dir: Path) -> list[dict[str, Any]]:
    data_root = Path(data_dir).resolve()
    signature = _directory_signature(data_root)
    cache_key = str(data_root)
    cached = _INDEX_CACHE.get(cache_key)
    if cached is not None and cached[0] == signature:
        return copy.deepcopy(cached[1])

    sessions = [
        _session_summary(data_root, session_root, result_file, payload)
        for session_root, result_file, payload in _canonical_records(data_root)
    ]
    indexed = sorted(
        sessions,
        key=lambda item: (str(item.get("saved_at") or ""), str(item["session_path"])),
        reverse=True,
    )
    _INDEX_CACHE[cache_key] = (signature, indexed)
    return copy.deepcopy(indexed)


def load_session(
    data_dir: Path,
    study_id: str,
    participant_id: str,
    *,
    session_id: str | None = None,
    session_folder: str | None = None,
) -> dict[str, Any]:
    data_root = Path(data_dir).resolve()
    session_root, result_file, payload = _select_session(
        data_root,
        study_id,
        participant_id,
        session_id=session_id,
        session_folder=session_folder,
    )
    streams = _read_merged_streams(session_root)
    offset = _display_epoch_offset(streams)
    stream_metadata = [_stream_metadata(session_root, stream, offset) for stream in streams]
    return {
        **_session_summary(data_root, session_root, result_file, payload),
        "result": payload,
        "streams": stream_metadata,
        # Kept as an alias while the existing timeline component moves from
        # JSON-sidecar terminology to generic XDF streams.
        "sidecars": stream_metadata,
        # Package A1: quality.jsonl (5c) and its unconfirmed-tail entries
        # (5h) existed with no reader on the UI side. Only the detail view
        # reads it -- the list view stays cheap, matching the existing split
        # where _read_merged_streams/_stream_metadata are also detail-only.
        "quality_summary": summarize_session_quality(session_root),
        "software": _software(session_root),
    }


def load_signal_samples(
    data_dir: Path,
    study_id: str,
    participant_id: str,
    sensor: str,
    *,
    session_id: str | None = None,
    session_folder: str | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
    start: float | None = None,
    end: float | None = None,
) -> dict[str, Any]:
    # Validate before opening a potentially large XDF.
    if max_points < 1 or max_points > MAX_MAX_POINTS:
        raise ValueError(f"max_points must be between 1 and {MAX_MAX_POINTS}.")
    data_root = Path(data_dir).resolve()
    session_root, _result_file, payload = _select_session(
        data_root,
        study_id,
        participant_id,
        session_id=session_id,
        session_folder=session_folder,
    )
    stream_key = str(sensor or "").strip()
    streams = _read_merged_streams(session_root)
    matching = [stream for stream in streams if str(stream.get("stream_key") or "") == stream_key]
    if not matching:
        raise SessionNotFoundError(f"No {stream_key} stream was recorded for this session.")

    window = _validate_window(start, end)
    # The viewer's axis and card markers are Unix time; samples carry the
    # recorder's LSL clock. Shift for display only (clock_core.assessment).
    offset = _display_epoch_offset(streams)

    stream = matching[0]
    timestamps = list(stream.get("timestamps") or [])
    rows = list(stream.get("samples") or [])
    samples = []
    for timestamp, row in zip(timestamps, rows):
        # Clip before downsampling, so a zoomed-in request spends its whole
        # point budget on the span the operator is actually looking at.
        epoch = float(timestamp) + offset
        if window is not None and not (window[0] <= epoch <= window[1]):
            continue
        sample = _json_safe(dict(row) if isinstance(row, dict) else {"value": row})
        sample["_epoch"] = _json_safe(epoch)
        samples.append(sample)
    downsampled = min_max_envelope(samples, max_points)
    return {
        **_stream_descriptor(stream),
        "window_start": _json_safe(window[0]) if window else None,
        "window_end": _json_safe(window[1]) if window else None,
        "study_id": str(payload.get("study_id") or study_id),
        "participant_id": str(payload.get("participant_id") or participant_id),
        "session_id": str(payload.get("session_id") or ""),
        "session_folder": session_root.name,
        "stream_key": stream_key,
        "sensor": stream_key,
        "plugin_key": str(stream.get("plugin_key") or ""),
        "sample_count": len(samples),
        **downsampled,
    }


def _validate_window(start: float | None, end: float | None) -> tuple[float, float] | None:
    """Both bounds or neither; an inverted window is a caller bug, not empty data."""
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError("start and end must be given together.")
    if not (math.isfinite(start) and math.isfinite(end)):
        raise ValueError("start and end must be finite epoch seconds.")
    if start >= end:
        raise ValueError("start must be earlier than end.")
    return (start, end)


def min_max_envelope(samples: list[dict[str, Any]], max_points: int) -> dict[str, Any]:
    if max_points < 1 or max_points > MAX_MAX_POINTS:
        raise ValueError(f"max_points must be between 1 and {MAX_MAX_POINTS}.")
    if len(samples) <= max_points:
        return {"mode": "raw", "points": samples}

    bucket_size = len(samples) / max_points
    points: list[dict[str, Any]] = []
    for bucket_index in range(max_points):
        start = math.floor(bucket_index * bucket_size)
        end = math.floor((bucket_index + 1) * bucket_size)
        bucket = samples[start : max(start + 1, end)]
        numeric_values: dict[str, list[float]] = {}
        for sample in bucket:
            for key, value in _flatten_numeric(sample).items():
                numeric_values.setdefault(key, []).append(value)
        points.append(
            {
                "start_epoch": _sample_epoch(bucket[0]),
                "end_epoch": _sample_epoch(bucket[-1]),
                "sample_count": len(bucket),
                "min": {key: min(values) for key, values in numeric_values.items()},
                "max": {key: max(values) for key, values in numeric_values.items()},
            }
        )
    return {"mode": "min_max_envelope", "points": points}


def _canonical_records(data_root: Path) -> list[tuple[Path, Path, dict[str, Any]]]:
    records = []
    for session_root in _canonical_session_roots(data_root):
        selected = _select_result_payload(session_root)
        if selected is not None:
            result_file, payload = selected
            records.append((session_root, result_file, payload))
            continue
        # Package 5i: a withdrawn session has no result payload left -- that
        # was the point. It is still listed, from its tombstone alone, so the
        # operator sees a withdrawal rather than a gap in the list.
        marker = session_root / WITHDRAWN_MARKER
        if marker.is_file():
            records.append((session_root, marker, _withdrawn_payload(session_root, marker)))
    return records


def _withdrawn_payload(session_root: Path, marker: Path) -> dict[str, Any]:
    """The little that can honestly be said about a withdrawn session.

    Study, participant and session identifiers come from the folder path,
    because the tombstone deliberately carries no participant data of its
    own. Those path segments are the one identifying trace a tombstone
    cannot shed: removing them would mean removing the folder, and then the
    withdrawal would be invisible again.
    """
    payload = _optional_json(marker)
    return {
        "study_id": session_root.parent.parent.name,
        "participant_id": session_root.parent.name,
        "session_id": str(payload.get("session_id") or session_root.name),
        "answers": {},
        "withdrawn": True,
        # Absent on tombstones written before this field existed; those were
        # all consent withdrawals, the only kind there was then.
        "kind": str(payload.get("kind") or "consent_withdrawal"),
    }


def _canonical_session_roots(data_root: Path) -> list[Path]:
    roots = []
    if not data_root.is_dir():
        return roots
    for study_dir in data_root.iterdir():
        if not study_dir.is_dir() or study_dir.name.startswith("_") or study_dir.name in {"runtime", "upload_jobs"}:
            continue
        for participant_dir in study_dir.iterdir():
            if not participant_dir.is_dir() or participant_dir.name.startswith("_"):
                continue
            for session_root in participant_dir.iterdir():
                if not session_root.is_dir() or session_root.name.startswith(".") or not _has_final_marker(session_root):
                    continue
                resolved = session_root.resolve()
                if resolved.is_relative_to(data_root.resolve()):
                    roots.append(resolved)
    return roots


def resolve_session_root(
    data_dir: Path,
    study_id: str,
    participant_id: str,
    *,
    session_id: str | None = None,
    session_folder: str | None = None,
) -> tuple[Path, str]:
    """Find one session's folder and id, without ``load_session()``'s detail cost.

    Package A3 (withdrawal route): the caller needs only "where does this
    session live", not merged-stream metadata or a quality summary -- both
    of which would also be wasted work, or fail outright, against a session
    an earlier withdrawal attempt already partly emptied. Shares
    ``_select_session``'s matching rules with ``load_session`` so a given
    URL never resolves to two different sessions depending on which
    function answered it.

    Works for an already-withdrawn (tombstoned) session too: 5i's marker
    fallback in ``_canonical_records`` keeps it selectable so a repeated or
    resumed withdrawal request still finds the same folder.
    """
    data_root = Path(data_dir).resolve()
    session_root, _result_file, payload = _select_session(
        data_root,
        study_id,
        participant_id,
        session_id=session_id,
        session_folder=session_folder,
    )
    resolved_session_id = str(payload.get("session_id") or _identity(session_root).get("session_id") or "")
    return session_root, resolved_session_id


def _select_session(
    data_root: Path,
    study_id: str,
    participant_id: str,
    *,
    session_id: str | None,
    session_folder: str | None,
) -> tuple[Path, Path, dict[str, Any]]:
    study = _required_selector(study_id, "study_id")
    participant = _required_selector(participant_id, "participant_id")
    wanted_session = str(session_id or "").strip()
    wanted_folder = str(session_folder or "").strip()
    if wanted_folder and (Path(wanted_folder).name != wanted_folder or wanted_folder in {".", ".."}):
        raise ValueError("Invalid session_folder.")

    matches = []
    for session_root, result_file, payload in _canonical_records(data_root):
        identity = _identity(session_root)
        actual_study = str(payload.get("study_id") or identity.get("study_id") or "")
        actual_participant = str(payload.get("participant_id") or identity.get("participant_id") or "")
        actual_session = str(payload.get("session_id") or identity.get("session_id") or "")
        if actual_study != study or actual_participant != participant:
            continue
        if wanted_session and actual_session != wanted_session:
            continue
        if wanted_folder and session_root.name != wanted_folder:
            continue
        matches.append((session_root, result_file, payload))
    if not matches:
        raise SessionNotFoundError("Completed canonical session not found.")
    return max(matches, key=lambda item: (_saved_at(item[0], item[1], item[2]), item[0].name))


def _session_summary(
    data_root: Path,
    session_root: Path,
    result_file: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    identity = _identity(session_root)
    manifest = _optional_json(session_root / "meta" / "manifest.json")
    marker = _marker_payload(session_root)
    finalization_state = _optional_json(session_root / "meta" / "finalization-state.json")
    answers = payload.get("answers")
    answer_details = payload.get("answer_details")
    return {
        "study_id": str(payload.get("study_id") or identity.get("study_id") or session_root.parent.parent.name),
        "participant_id": str(payload.get("participant_id") or identity.get("participant_id") or session_root.parent.name),
        "session_id": str(payload.get("session_id") or identity.get("session_id") or session_root.name),
        "session_folder": session_root.name,
        "session_path": session_root.relative_to(data_root).as_posix(),
        "result_file": result_file.name,
        "saved_at": _saved_at(session_root, result_file, payload),
        "status": str(marker.get("status") or ("attention_required" if (session_root / "ATTENTION_REQUIRED.json").is_file() else "completed")),
        "quality_status": str(manifest.get("quality_status") or ""),
        # Package 5a: one name for "where is this session overall", derived
        # from the per-machine documents rather than stored a second time.
        "lifecycle": derive_session_lifecycle(
            recording_plan=_optional_json(session_root / "meta" / "recording-plan.json"),
            finalization_state=finalization_state,
            terminal_marker=marker,
            withdrawn=_applicable_withdrawal(session_root) is not None,
        ),
        "answers_count": (
            len(answers)
            if isinstance(answers, dict)
            else len(answer_details)
            if isinstance(answer_details, list)
            else 0
        ),
        "files": [_file_metadata(session_root, path) for path in _related_files(session_root, manifest)],
        "recovered": bool(payload.get("recovered")),
        # Read from the WITHDRAWN.json marker directly, not from ``payload``:
        # an abort deliberately leaves result.json in place, so ``payload``
        # here is the ordinary result, not the tombstone, even though the
        # session is withdrawn.
        "withdrawal_kind": _withdrawal_kind(session_root),
        # Lets the session view load the finalization steps and retry an
        # upload long after the live finalization notice is gone.
        "finalization_job_id": str(finalization_state.get("job_id") or marker.get("job_id") or ""),
        "finalization_status": str(finalization_state.get("status") or ""),
        "upload_failures": [
            str(step) for step in finalization_state.get("upload_failures") or [] if isinstance(step, str)
        ],
    }


def _related_files(session_root: Path, manifest: dict[str, Any]) -> list[Path]:
    files: set[Path] = set()
    for name in _PUBLIC_CONTROL_FILES:
        candidate = session_root / name
        if candidate.is_file():
            files.add(candidate)
    for item in manifest.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        relative = _safe_artifact_path(item.get("path"))
        if relative is None:
            continue
        candidate = (session_root / Path(*relative.parts)).resolve()
        if candidate.is_relative_to(session_root.resolve()) and candidate.is_file():
            files.add(candidate)
    for container in (session_root / "raw", session_root / "derived"):
        if container.is_dir():
            files.update(path for path in container.rglob("*.xdf") if path.is_file())
    return sorted(files, key=lambda path: path.relative_to(session_root).as_posix())


def _read_merged_streams(session_root: Path) -> list[dict[str, Any]]:
    global _STREAM_CACHE
    merged = session_root / "derived" / "session.xdf"
    if not merged.is_file() or not merged.resolve().is_relative_to(session_root.resolve()):
        return []
    try:
        stat = merged.stat()
    except OSError:
        return []
    cache_key = str(merged.resolve())
    with _STREAM_CACHE_LOCK:
        if _STREAM_CACHE is not None and _STREAM_CACHE[:3] == (cache_key, stat.st_mtime_ns, stat.st_size):
            return _STREAM_CACHE[3]
        try:
            streams = list(PyXdfSampleReader().read_streams(merged))
        except (CardSummaryError, OSError, TypeError, ValueError) as error:
            print(f"[SESSIONS] Could not read merged XDF {merged}: {error}")
            return []
        # One session is enough: detail loads it once, then parallel lane
        # requests reuse the same parsed XDF without retaining the whole study.
        _STREAM_CACHE = (cache_key, stat.st_mtime_ns, stat.st_size, streams)
        return streams


def _display_epoch_offset(streams: list[dict[str, Any]]) -> float:
    """Recorder-clock to Unix-time shift from the study markers, cached per parsed file."""
    global _OFFSET_CACHE
    if _OFFSET_CACHE is not None and _OFFSET_CACHE[0] is streams:
        return _OFFSET_CACHE[1]
    lsl_times: list[float] = []
    server_ms: list[float] = []
    for stream in streams:
        samples = list(stream.get("samples") or [])
        first = samples[0] if samples else None
        if not isinstance(first, dict) or not any(isinstance(value, str) for value in first.values()):
            continue
        for timestamp, sample in zip(stream.get("timestamps") or [], samples):
            for value in (sample.values() if isinstance(sample, dict) else ()):
                if isinstance(value, str) and "server_ms=" in value:
                    fields = dict(item.partition("=")[::2] for item in value.split("|") if "=" in item)
                    lsl_times.append(timestamp)
                    server_ms.append(fields.get("server_ms"))
    offset = display_epoch_offset(lsl_times, server_ms) or 0.0
    _OFFSET_CACHE = (streams, offset)
    return offset


def _stream_metadata(session_root: Path, stream: dict[str, Any], offset: float = 0.0) -> dict[str, Any]:
    timestamps = list(stream.get("timestamps") or [])
    merged = session_root / "derived" / "session.xdf"
    metadata = _file_metadata(session_root, merged)
    return {
        **metadata,
        "sensor": str(stream.get("stream_key") or ""),
        "stream_key": str(stream.get("stream_key") or ""),
        "stream_name": str(stream.get("name") or ""),
        "plugin_key": str(stream.get("plugin_key") or ""),
        "sample_count": min(len(timestamps), len(list(stream.get("samples") or []))),
        "timestamp_start": _json_safe(float(timestamps[0]) + offset) if timestamps else None,
        "timestamp_end": _json_safe(float(timestamps[-1]) + offset) if timestamps else None,
        **_stream_descriptor(stream),
    }


def _stream_descriptor(stream: dict[str, Any]) -> dict[str, Any]:
    """The parts of the LSL header the viewer renders from.

    Everything here comes out of the recording itself, so a sensor added later
    through a plugin gets a labelled, correctly drawn track without the viewer
    or any manifest knowing it exists.
    """
    return {
        "stream_name": str(stream.get("name") or ""),
        "nominal_rate_hz": _json_safe(stream.get("nominal_rate_hz")),
        "channels": [str(channel) for channel in (stream.get("channels") or [])],
        "channel_types": {str(k): str(v) for k, v in (stream.get("channel_types") or {}).items()},
        "channel_units": {str(k): str(v) for k, v in (stream.get("channel_units") or {}).items()},
    }


def _select_result_payload(session_root: Path) -> tuple[Path, dict[str, Any]] | None:
    for name in ("result.json", "submission.json"):
        path = session_root / "answers" / name
        if not path.is_file() or not path.resolve().is_relative_to(session_root.resolve()):
            continue
        try:
            payload = _read_json_object(path)
        except (OSError, ValueError):
            continue
        if _is_result_payload(payload):
            return path, payload
    return None


def _directory_signature(data_root: Path) -> tuple[tuple[str, int, int], ...]:
    entries = []
    if not data_root.is_dir():
        return ()
    for session_root in _canonical_session_roots(data_root):
        for path in session_root.rglob("*"):
            relative = path.relative_to(session_root)
            if not path.is_file() or "logs" in relative.parts or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path.relative_to(data_root).as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def _has_final_marker(session_root: Path) -> bool:
    # Package 5i: a withdrawn session is a tombstone -- its COMPLETE.json and
    # its result file were deleted along with everything else. Without this
    # third marker it would drop out of the index entirely, which is exactly
    # the silence that makes a withdrawal indistinguishable from data loss.
    return (
        (session_root / "COMPLETE.json").is_file()
        or (session_root / "ATTENTION_REQUIRED.json").is_file()
        or (session_root / WITHDRAWN_MARKER).is_file()
    )


def _marker_payload(session_root: Path) -> dict[str, Any]:
    withdrawal = _applicable_withdrawal(session_root)
    if withdrawal is not None:
        return withdrawal
    attention = session_root / "ATTENTION_REQUIRED.json"
    return _optional_json(attention if attention.is_file() else session_root / "COMPLETE.json")


def _applicable_withdrawal(session_root: Path) -> dict[str, Any] | None:
    """The session's tombstone, unless it is an abort that came too late.

    Up to 1.7.23 an in-app update aborted the last recorded session even
    when it had already ended: the participant had submitted, finalization
    had run, and only then was an ``admin_abort`` tombstone written. Such an
    abort stopped nothing, so it does not make the session aborted. A
    genuine abort comes before the submission (or there is none at all).
    """
    marker_path = session_root / WITHDRAWN_MARKER
    if not marker_path.is_file():
        return None
    marker = _optional_json(marker_path)
    if marker.get("kind") == "admin_abort":
        submitted = _optional_json(session_root / "meta" / "finalization-state.json").get("created_epoch")
        aborted = marker.get("withdrawn_at_epoch")
        if all(isinstance(value, (int, float)) for value in (submitted, aborted)) and aborted > submitted:
            return None
    return marker


def _withdrawal_kind(session_root: Path) -> str:
    """Empty for a session that was never withdrawn or aborted.

    A withdrawal always writes a tombstone, so the marker existing but
    lacking a ``kind`` means only one thing: it predates the field, back
    when consent withdrawal was the only kind there was.
    """
    marker = _applicable_withdrawal(session_root)
    if marker is None:
        return ""
    return str(marker.get("kind") or "consent_withdrawal")


def _saved_at(session_root: Path, result_file: Path, payload: dict[str, Any]) -> str:
    marker = _marker_payload(session_root)
    return str(
        payload.get("timestamp_end")
        or payload.get("server_received_at")
        or marker.get("published_at")
        or dt.datetime.fromtimestamp(result_file.stat().st_mtime, tz=dt.timezone.utc).isoformat()
    )


def _identity(session_root: Path) -> dict[str, Any]:
    return _optional_json(session_root / "meta" / "session-identity.json")


def _software(session_root: Path) -> dict[str, Any] | None:
    """Which Study Runner and plugin versions produced the session.

    The finalized manifest is authoritative and a job still running already
    holds it; an older session shows what it recorded itself.
    """
    meta = session_root / "meta"
    for software in (
        (_optional_json(meta / "manifest.json").get("provenance") or {}).get("software"),
        _optional_json(meta / "finalization-state.json").get("software"),
    ):
        if isinstance(software, dict):
            return software
    return legacy_software_provenance(_optional_json(meta / "recording-plan.json"))


def _file_metadata(session_root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.relative_to(session_root).as_posix(),
        "type": path.suffix.lower().lstrip("."),
        "size": stat.st_size,
        "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat(),
    }


def _safe_artifact_path(value: Any) -> PurePosixPath | None:
    normalized = str(value or "").strip().replace("\\", "/")
    relative = PurePosixPath(normalized)
    if not normalized or relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return relative


def _required_selector(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 256:
        raise ValueError(f"Invalid {label}.")
    return normalized


def _is_result_payload(payload: dict[str, Any]) -> bool:
    return not payload.get("sensor") and (
        isinstance(payload.get("answers"), dict) or isinstance(payload.get("answer_details"), list)
    )


def _optional_json(path: Path) -> dict[str, Any]:
    try:
        return _read_json_object(path)
    except (OSError, ValueError):
        return {}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"Invalid JSON file: {path.name}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return payload


def _flatten_numeric(value: Any, prefix: str = "") -> dict[str, float]:
    flattened: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_numeric(child, child_prefix))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        flattened[prefix] = float(value)
    return flattened


def _sample_epoch(sample: dict[str, Any]) -> float | None:
    for key in ("_epoch", "server_received_epoch", "processed_epoch"):
        value = sample.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
