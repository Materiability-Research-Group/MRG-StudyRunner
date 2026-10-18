"""Validation and normalization of study configs, results, and trial options.

Table of contents (in file order):
1. PUBLIC ENTRY POINTS   validate_and_normalize_config / _results / _trial_options
2. RESULT PARTS          answers, participant metadata, answer/card events
3. ANSWER VALUES         card-extension dispatch and result assembly
4. QUESTIONS & SETTINGS  card-extension normalization and study settings
5. SHARED CONTRACTS      participant vocabulary and validation primitives

Everything raises ValidationError with an operator-readable message.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import NAMESPACE_URL, UUID, uuid5
from typing import Any

from ..studies.study_plugin_config import (
    PluginConfigError,
    normalize_card_plugin_actions,
    normalize_study_settings_plugins,
)
from .migrate import migrated_info_top
# STUDY_SENSOR_KEYS remains imported as a compatibility/patch seam for fixture
# tests and external validators. Unknown legacy keys are migrated instead of
# being rejected against this tuple.
from study_runner.data_core.host.study_sensor_runtime import STUDY_SENSOR_KEYS, normalize_study_sensors
from study_runner.contracts.participant_fields import (
    CHILDHOOD_AREA_OPTIONS,
    CONFIGURABLE_OPTION_DEFAULTS,
    PARTICIPANT_FIELD_ORDER,
)
from study_runner.contracts.card_validation_primitives import (
    CardValidationError as ValidationError,
    normalize_boolean,
    normalize_float,
    normalize_integer,
    normalize_optional_integer,
    normalize_text,
    normalize_text_list,
    require_text,
)
from study_runner.plugin_framework.card_catalog import QuestionTypes
from study_runner.contracts.media_content import MediaContentError, normalize_media_content
from .card_extension_bridge import normalize_card_config, validate_card_answer

ALLOWED_QUESTION_TYPES = QuestionTypes()
NON_ANSWER_QUESTION_TYPES = QuestionTypes(answerless=True)


# Participant identity vocabulary is a dependency-free contract shared by
# RuntimeCore, the participant-id card, and destination adapters. It is
# imported above and re-exported here for existing callers.


# Fields whose allowed answers can be configured per study.


# ============================================================
#  1. PUBLIC ENTRY POINTS
# ============================================================


def validate_and_normalize_config(config_data: Any) -> dict[str, Any]:
    if not isinstance(config_data, dict):
        raise ValidationError("The study configuration must be a JSON object.")

    study_id = require_text(config_data.get("study_id"), "Study ID")
    questions = config_data.get("questions", [])
    if not isinstance(questions, list):
        raise ValidationError("Questions must be a list.")

    normalized_questions = [
        _validate_question(question_data, question_index, study_id)
        for question_index, question_data in enumerate(questions)
    ]
    card_ids = [question["card_id"] for question in normalized_questions]
    if len(card_ids) != len(set(card_ids)):
        raise ValidationError("Every card must have a unique card_id.")
    return {
        "study_id": study_id,
        "questions": normalized_questions,
        "study_settings": _validate_study_settings(config_data.get("study_settings")),
    }


def validate_and_normalize_study_settings(value: Any) -> dict[str, Any]:
    """``study_settings`` alone, with no question/card validation.

    Hardware/sensor-runtime callers only ever need this sibling key -- which
    plugins a study enables and how -- never the question list. Routing them
    through the full :func:`validate_and_normalize_config` would make every
    hardware save depend on every installed card extension staying available
    (Package 5g.B5: a study using ``participant-id``/``finish`` used to
    validate for free because those types were hardcoded; both are now
    ordinary card extensions like any other, so an uninstalled or failing
    card must not block an unrelated hardware-config save).
    """
    return _validate_study_settings(value)


def validate_and_normalize_results(
    result_payload: Any,
    study_config: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(result_payload, dict):
        raise ValidationError("The result payload must be a JSON object.")

    participant_id = require_text(result_payload.get("participant_id"), "Participant-ID")
    timestamp_start = _require_iso_timestamp(result_payload.get("timestamp_start"), "Start timestamp")
    timestamp_end = _require_iso_timestamp(result_payload.get("timestamp_end"), "End timestamp")
    submitted_study_id = normalize_text(result_payload.get("study_id"))
    if submitted_study_id and submitted_study_id != study_config["study_id"]:
        raise ValidationError("Submitted study_id does not match the active study configuration.")

    if _parse_iso_timestamp(timestamp_end) < _parse_iso_timestamp(timestamp_start):
        raise ValidationError("End timestamp must be later than or equal to the start timestamp.")

    answers = result_payload.get("answers")
    if not isinstance(answers, dict):
        raise ValidationError("Answers must be a JSON object.")

    answer_events = _validate_answer_events(
        result_payload.get("answer_events"),
        study_config.get("questions", []),
    )
    card_events = _validate_card_events(
        result_payload.get("card_events"),
        study_config.get("questions", []),
    )
    participant_metadata = _validate_participant_metadata(
        result_payload.get("participant_metadata"),
        study_config.get("questions", []),
    )

    normalized_answers = _validate_answers(answers, study_config.get("questions", []))
    skipped_questions = _skipped_optional_questions(
        answers,
        study_config.get("questions", []),
        answer_events=answer_events,
        card_events=card_events,
    )

    submission_id = normalize_text(result_payload.get("submission_id"))
    if len(submission_id) > 200:
        raise ValidationError("submission_id must not exceed 200 characters.")
    study_end_event = _validate_study_end_event(result_payload.get("study_end_event"))

    return {
        "participant_id": participant_id,
        "study_id": study_config["study_id"],
        "submission_id": submission_id,
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        # Tablet-vs-server clock offset (server = client + offset), used to
        # align client timestamps with server-clock sensor samples.
        "client_clock_offset_ms": normalize_float(
            result_payload.get("client_clock_offset_ms"),
            field_name="client_clock_offset_ms",
            minimum=-10_000_000_000.0,
            maximum=10_000_000_000.0,
            allow_none=True,
        ),
        "clock_sync_samples": _validate_clock_sync_samples(result_payload.get("clock_sync_samples")),
        "answers": normalized_answers,
        "skipped_questions": skipped_questions,
        "participant_metadata": participant_metadata,
        "answer_events": answer_events,
        "card_events": card_events,
        "study_end_event": study_end_event,
    }


def _validate_study_end_event(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValidationError("study_end_event must be a JSON object.")
    event_id = normalize_text(value.get("event_id"))
    if not event_id or len(event_id) > 200:
        raise ValidationError("study_end_event.event_id is required and must not exceed 200 characters.")
    sequence = value.get("sequence_number")
    return {
        "event_id": event_id,
        "source_epoch_ms": normalize_float(
            value.get("source_epoch_ms"),
            field_name="study_end_event.source_epoch_ms",
            minimum=0.0,
            maximum=100_000_000_000_000.0,
            allow_none=True,
        ),
        "source_monotonic_ms": normalize_float(
            value.get("source_monotonic_ms"),
            field_name="study_end_event.source_monotonic_ms",
            minimum=0.0,
            maximum=100_000_000_000_000.0,
            allow_none=True,
        ),
        "time_source": normalize_text(value.get("time_source")),
        "clock_sync_id": normalize_text(value.get("clock_sync_id")),
        "clock_sync_age_ms": normalize_float(
            value.get("clock_sync_age_ms"),
            field_name="study_end_event.clock_sync_age_ms",
            minimum=0.0,
            maximum=10_000_000_000.0,
            allow_none=True,
        ),
        "clock_sync_rtt_ms": normalize_float(
            value.get("clock_sync_rtt_ms"),
            field_name="study_end_event.clock_sync_rtt_ms",
            minimum=0.0,
            maximum=10_000_000_000.0,
            allow_none=True,
        ),
        "sequence_number": normalize_integer(
            sequence,
            field_name="study_end_event.sequence_number",
            minimum=0,
            maximum=9_007_199_254_740_991,
        ) if sequence is not None else None,
    }


def _validate_clock_sync_samples(value: Any) -> list[dict[str, Any]]:
    """Keep each selected four-timestamp exchange and its paired RTT together."""
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 1440:
        raise ValidationError("clock_sync_samples must be a list of at most 1440 exchanges.")
    normalized = []
    for index, sample in enumerate(value):
        if not isinstance(sample, dict):
            raise ValidationError(f"clock_sync_samples[{index}] must be an object.")
        item = {"id": normalize_text(sample.get("id"))}
        if not item["id"] or len(item["id"]) > 200:
            raise ValidationError(f"clock_sync_samples[{index}].id is invalid.")
        for key in ("client_send_ms", "client_receive_ms", "server_receive_ms", "server_send_ms",
                    "network_delay_ms", "offset_ms"):
            item[key] = normalize_float(
                sample.get(key), field_name=f"clock_sync_samples[{index}].{key}",
                minimum=-10_000_000_000_000.0, maximum=10_000_000_000_000.0,
            )
        if item["client_receive_ms"] < item["client_send_ms"] or item["server_send_ms"] < item["server_receive_ms"]:
            raise ValidationError(f"clock_sync_samples[{index}] has reversed timestamps.")
        if item["network_delay_ms"] < 0:
            raise ValidationError(f"clock_sync_samples[{index}] has negative network delay.")
        calculated_delay = (item["client_receive_ms"] - item["client_send_ms"]) - (
            item["server_send_ms"] - item["server_receive_ms"]
        )
        calculated_offset = ((item["server_receive_ms"] - item["client_send_ms"]) + (
            item["server_send_ms"] - item["client_receive_ms"]
        )) / 2.0
        if abs(item["network_delay_ms"] - calculated_delay) > 0.01 or abs(item["offset_ms"] - calculated_offset) > 0.01:
            raise ValidationError(f"clock_sync_samples[{index}] does not match its four timestamps.")
        normalized.append(item)
    return normalized


def validate_and_normalize_trial_options(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValidationError("The trial control payload must be a JSON object.")

    try:
        # Pass the complete request so one-release legacy fields can be
        # translated once. Downstream handlers receive only plugin_actions.
        plugin_actions = normalize_card_plugin_actions(payload)
    except PluginConfigError as error:
        raise ValidationError(str(error)) from error

    return {
        "client_trigger_ms": normalize_float(
            payload.get("client_trigger_ms"),
            field_name="client_trigger_ms",
            minimum=0.0,
            maximum=10_000_000_000.0,
            allow_none=True,
        ),
        "time_source": normalize_text(payload.get("time_source")),
        "clock_sync_id": normalize_text(payload.get("clock_sync_id")),
        "clock_sync_age_ms": normalize_float(
            payload.get("clock_sync_age_ms"),
            field_name="clock_sync_age_ms",
            minimum=0.0,
            maximum=10_000_000_000.0,
            allow_none=True,
        ),
        "clock_sync_rtt_ms": normalize_float(
            payload.get("clock_sync_rtt_ms"),
            field_name="clock_sync_rtt_ms",
            minimum=0.0,
            maximum=10_000_000_000.0,
            allow_none=True,
        ),
        "clock_offset_ms": normalize_float(
            payload.get("clock_offset_ms"),
            field_name="clock_offset_ms",
            minimum=-3_600_000.0,
            maximum=3_600_000.0,
            allow_none=True,
        ),
        "client_trigger_epoch_ms": normalize_float(
            payload.get("client_trigger_epoch_ms"),
            field_name="client_trigger_epoch_ms",
            minimum=0.0,
            maximum=10_000_000_000_000.0,
            allow_none=True,
        ),
        "visual_onset_epoch_ms": normalize_float(
            payload.get("visual_onset_epoch_ms"),
            field_name="visual_onset_epoch_ms",
            minimum=0.0,
            maximum=10_000_000_000_000.0,
            allow_none=True,
        ),
        "onset_uncertainty_ms": normalize_float(
            payload.get("onset_uncertainty_ms"),
            field_name="onset_uncertainty_ms",
            minimum=0.0,
            maximum=86_400_000.0,
            allow_none=True,
        ),
        "planned_start_epoch_ms": normalize_float(
            payload.get("planned_start_epoch_ms"),
            field_name="planned_start_epoch_ms",
            minimum=0.0,
            maximum=10_000_000_000_000.0,
            allow_none=True,
        ),
        "planned_deadline_epoch_ms": normalize_float(
            payload.get("planned_deadline_epoch_ms"),
            field_name="planned_deadline_epoch_ms",
            minimum=0.0,
            maximum=10_000_000_000_000.0,
            allow_none=True,
        ),
        "study_id": normalize_text(payload.get("study_id")),
        "participant_id": normalize_text(payload.get("participant_id")),
        "session_id": normalize_text(payload.get("session_id")),
        "client_id": normalize_text(payload.get("client_id")),
        "question_index": normalize_optional_integer(
            payload.get("question_index"),
            field_name="question_index",
            minimum=0,
            maximum=10_000,
        ),
        "question_type": _normalize_question_type(payload.get("question_type")),
        "phase": normalize_text(payload.get("phase")),
        "marker_event": normalize_text(payload.get("marker_event") or payload.get("event")),
        "event_id": normalize_text(payload.get("event_id")),
        "stop_event_id": normalize_text(payload.get("stop_event_id")),
        "stimulus_id": normalize_text(payload.get("stimulus_id")),
        "automatic_deadline": normalize_boolean(payload.get("automatic_deadline", False)),
        "plugin_actions": plugin_actions,
        "actuator_plugins": _normalize_actuator_plugins(payload.get("actuator_plugins")),
        "time_up_event_id": normalize_text(payload.get("time_up_event_id")),
        "actuator_stop_at": _normalize_actuator_stop_at(payload.get("actuator_stop_at")),
        "stop_deadline_epoch_ms": normalize_float(
            payload.get("stop_deadline_epoch_ms"),
            field_name="stop_deadline_epoch_ms",
            minimum=0.0,
            maximum=10_000_000_000_000.0,
            allow_none=True,
        ),
    }


def _normalize_actuator_plugins(value: Any) -> list[str] | None:
    """The card's actuator selection; ``None`` keeps the pre-selection default."""

    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError("actuator_plugins must be a list of plugin keys.")
    selected: list[str] = []
    for item in value:
        plugin_key = normalize_text(item)
        if not plugin_key or len(plugin_key) > 200:
            raise ValidationError("actuator_plugins contains an invalid plugin key.")
        if plugin_key not in selected:
            selected.append(plugin_key)
    return selected


def _normalize_actuator_stop_at(value: Any) -> str:
    """Which event stops the actuators: leaving the card or the time-up."""

    stop_at = normalize_text(value, default="stop") or "stop"
    if stop_at not in {"stop", "time_up"}:
        raise ValidationError("actuator_stop_at must be stop or time_up.")
    return stop_at


def skipped_optional_questions_for_result(
    answers: dict[str, Any],
    questions: list[dict[str, Any]],
    *,
    answer_events: list[dict[str, Any]] | None = None,
    card_events: list[dict[str, Any]] | None = None,
) -> list[str]:
    return _skipped_optional_questions(
        answers,
        questions,
        answer_events=answer_events,
        card_events=card_events,
    )


# ============================================================
#  2. RESULT PARTS - answers, metadata, events
# ============================================================
def _validate_answers(
    answers: dict[str, Any],
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_answers: dict[str, Any] = {}
    expected_keys = set()

    for question_index, question in enumerate(questions):
        if question.get("type") in NON_ANSWER_QUESTION_TYPES:
            continue

        answer_key = f"q{question_index}"
        expected_keys.add(answer_key)
        if answer_key not in answers or answers.get(answer_key) is None:
            if not _question_is_required(question):
                continue
            raise ValidationError(f"Missing answer for question {question_index + 1}.")

        normalized_answers[answer_key] = _validate_answer_value(
            answer_key=answer_key,
            question=question,
            answer=answers.get(answer_key),
            question_number=question_index + 1,
        )

    extra_keys = sorted(set(answers.keys()) - expected_keys)
    if extra_keys:
        raise ValidationError(f"Unexpected answer keys: {', '.join(extra_keys)}.")

    return normalized_answers


def _skipped_optional_questions(
    answers: dict[str, Any],
    questions: list[dict[str, Any]],
    *,
    answer_events: list[dict[str, Any]] | None = None,
    card_events: list[dict[str, Any]] | None = None,
) -> list[str]:
    skipped: list[str] = []
    seen_indexes = {
        int(event["question_index"])
        for event in [*(answer_events or []), *(card_events or [])]
        if isinstance(event.get("question_index"), int)
    }
    for question_index, question in enumerate(questions):
        if question.get("type") in NON_ANSWER_QUESTION_TYPES or _question_is_required(question):
            continue
        if seen_indexes and question_index not in seen_indexes:
            continue
        answer_key = f"q{question_index}"
        if answer_key not in answers or answers.get(answer_key) is None:
            skipped.append(answer_key)
    return skipped


def _question_is_required(question: dict[str, Any]) -> bool:
    return normalize_boolean(question.get("required", True))


def _validate_participant_metadata(
    value: Any,
    questions: list[dict[str, Any]],
) -> dict[str, str]:
    stored_fields = _stored_participant_fields(questions)

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValidationError("participant_metadata must be a JSON object.")

    normalized: dict[str, str] = {}
    for field_key in stored_fields:
        if field_key not in value:
            if _participant_field_is_required(questions, field_key):
                raise ValidationError(f"participant_metadata is missing {field_key}.")
            continue
        normalized[field_key] = _validate_participant_metadata_value(
            field_key,
            value.get(field_key),
            _participant_field_options(questions, field_key) if field_key in CONFIGURABLE_OPTION_DEFAULTS else None,
        )

    extra_keys = sorted(set(value.keys()) - set(stored_fields))
    if extra_keys:
        raise ValidationError(
            f"participant_metadata contains unexpected fields: {', '.join(extra_keys)}."
        )

    return normalized


def _participant_field_is_required(questions: list[dict[str, Any]], field_key: str) -> bool:
    participant_question = next(
        (question for question in questions if question.get("type") == "participant-id"),
        None,
    )
    field_config = ((participant_question or {}).get("fields") or {}).get(field_key) or {}
    return normalize_boolean(field_config.get("required", True))


def _stored_participant_fields(questions: list[dict[str, Any]]) -> list[str]:
    participant_question = next(
        (question for question in questions if question.get("type") == "participant-id"),
        None,
    )
    if not participant_question:
        return []

    fields = participant_question.get("fields") or {}
    stored: list[str] = []
    for field_key in PARTICIPANT_FIELD_ORDER:
        field_config = fields.get(field_key) or {}
        if field_config.get("enabled") and field_config.get("store"):
            stored.append(field_key)
    return stored


def _validate_participant_metadata_value(
    field_key: str,
    value: Any,
    configured_options: list[str] | None = None,
) -> str:
    normalized = require_text(value, f"participant_metadata {field_key}")

    if field_key in CONFIGURABLE_OPTION_DEFAULTS:
        allowed = configured_options or CONFIGURABLE_OPTION_DEFAULTS[field_key]
        if normalized not in allowed:
            raise ValidationError(
                f"participant_metadata {field_key} must be one of: "
                + ", ".join(allowed)
                + "."
            )
        return normalized

    if field_key == "childhood_area":
        normalized = normalized.lower()
        if normalized not in CHILDHOOD_AREA_OPTIONS:
            raise ValidationError(
                "participant_metadata childhood_area must be urban or rural."
            )
        return normalized

    if field_key == "birth_date":
        try:
            _parse_iso_timestamp(normalized)
        except ValueError as exc:
            raise ValidationError(
                "participant_metadata birth_date must be a valid ISO date (YYYY-MM-DD)."
            ) from exc
        return normalized

    return normalized


def _validate_answer_events(
    value: Any,
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("answer_events must be a list.")

    normalized_events: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()

    for raw_event in value:
        if not isinstance(raw_event, dict):
            raise ValidationError("Each answer_event must be an object.")

        question_index = normalize_integer(
            raw_event.get("question_index"),
            field_name="answer_event question_index",
            minimum=0,
            maximum=max(0, len(questions) - 1),
        )
        if question_index in seen_indexes:
            raise ValidationError(f"Duplicate answer_event for question index {question_index}.")
        seen_indexes.add(question_index)

        question = questions[question_index] if question_index < len(questions) else {}
        answer_key = raw_event.get("answer_key")
        normalized_answer_key = normalize_text(answer_key) if answer_key is not None else ""
        expected_answer_key = "" if question.get("type") == "participant-id" else f"q{question_index}"
        if normalized_answer_key != expected_answer_key:
            raise ValidationError(
                f"answer_event for question index {question_index} has an unexpected answer_key."
            )

        normalized_events.append(
            {
                "question_index": question_index,
                "question_type": normalize_text(raw_event.get("question_type")),
                "answer_key": normalized_answer_key,
                "shown_at": _require_iso_timestamp(raw_event.get("shown_at"), "answer_event shown_at"),
                "answered_at": _require_iso_timestamp(raw_event.get("answered_at"), "answer_event answered_at"),
                "answer_revision_times": [
                    _require_iso_timestamp(stamp, "answer_event answer_revision_times")
                    for stamp in (raw_event.get("answer_revision_times") or [])
                ],
            }
        )

    return normalized_events


def _validate_card_events(
    value: Any,
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("card_events must be a list.")

    normalized_events: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()

    for raw_event in value:
        if not isinstance(raw_event, dict):
            raise ValidationError("Each card_event must be an object.")

        question_index = normalize_integer(
            raw_event.get("question_index"),
            field_name="card_event question_index",
            minimum=0,
            maximum=max(0, len(questions) - 1),
        )
        if question_index in seen_indexes:
            raise ValidationError(f"Duplicate card_event for question index {question_index}.")
        seen_indexes.add(question_index)

        question = questions[question_index] if question_index < len(questions) else {}
        expected_type = normalize_text(question.get("type"))
        question_type = normalize_text(raw_event.get("question_type"), default=expected_type)
        if expected_type and question_type and question_type != expected_type:
            raise ValidationError(
                f"card_event for question index {question_index} has an unexpected question_type."
            )

        normalized_events.append(
            {
                "question_index": question_index,
                "question_type": question_type,
                "shown_at": _require_iso_timestamp(raw_event.get("shown_at"), "card_event shown_at"),
                "shown_at_server_epoch_ms": normalize_float(
                    raw_event.get("shown_at_server_epoch_ms"),
                    field_name="card_event shown_at_server_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "answered_at": _optional_iso_timestamp(raw_event.get("answered_at"), "card_event answered_at"),
                "answered_at_server_epoch_ms": normalize_float(
                    raw_event.get("answered_at_server_epoch_ms"),
                    field_name="card_event answered_at_server_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "completed_at": _optional_iso_timestamp(raw_event.get("completed_at"), "card_event completed_at"),
                "active_started_at": _optional_iso_timestamp(raw_event.get("active_started_at"), "card_event active_started_at"),
                "active_ended_at": _optional_iso_timestamp(raw_event.get("active_ended_at"), "card_event active_ended_at"),
                "server_start_received_at": _optional_iso_timestamp(raw_event.get("server_start_received_at"), "card_event server_start_received_at"),
                "server_stop_received_at": _optional_iso_timestamp(raw_event.get("server_stop_received_at"), "card_event server_stop_received_at"),
                "server_start_received_epoch_ms": normalize_float(
                    raw_event.get("server_start_received_epoch_ms"),
                    field_name="card_event server_start_received_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "server_stop_received_epoch_ms": normalize_float(
                    raw_event.get("server_stop_received_epoch_ms"),
                    field_name="card_event server_stop_received_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "client_start_trigger_epoch_ms": normalize_float(
                    raw_event.get("client_start_trigger_epoch_ms"),
                    field_name="card_event client_start_trigger_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "visual_onset_epoch_ms": normalize_float(
                    raw_event.get("visual_onset_epoch_ms"),
                    field_name="card_event visual_onset_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "onset_uncertainty_ms": normalize_float(
                    raw_event.get("onset_uncertainty_ms"),
                    field_name="card_event onset_uncertainty_ms",
                    minimum=0.0,
                    maximum=86_400_000.0,
                    allow_none=True,
                ),
                "client_stop_trigger_epoch_ms": normalize_float(
                    raw_event.get("client_stop_trigger_epoch_ms"),
                    field_name="card_event client_stop_trigger_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "planned_start_epoch_ms": normalize_float(
                    raw_event.get("planned_start_epoch_ms"),
                    field_name="card_event planned_start_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "planned_deadline_epoch_ms": normalize_float(
                    raw_event.get("planned_deadline_epoch_ms"),
                    field_name="card_event planned_deadline_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "stimulus_id": normalize_text(raw_event.get("stimulus_id")),
                "start_event_id": normalize_text(raw_event.get("start_event_id")),
                "stop_event_id": normalize_text(raw_event.get("stop_event_id")),
                "attempt_history": _validate_attempt_history(raw_event.get("attempt_history")),
                "shown_event_id": normalize_text(raw_event.get("shown_event_id")),
                "answered_event_id": normalize_text(raw_event.get("answered_event_id")),
                "prepare_failed": normalize_boolean(raw_event.get("prepare_failed", False)),
                "visibility_interrupted": normalize_boolean(
                    raw_event.get("visibility_interrupted", False)
                ),
                "visibility_interruption_count": normalize_integer(
                    raw_event.get("visibility_interruption_count", 0),
                    field_name="card_event visibility_interruption_count",
                    minimum=0,
                    maximum=10_000,
                ),
                "visibility_hidden_duration_ms": normalize_float(
                    raw_event.get("visibility_hidden_duration_ms", 0),
                    field_name="card_event visibility_hidden_duration_ms",
                    minimum=0.0,
                    maximum=86_400_000.0,
                ),
                "warmup_callback_delay_ms": normalize_float(
                    raw_event.get("warmup_callback_delay_ms", 0),
                    field_name="card_event warmup_callback_delay_ms",
                    minimum=0.0,
                    maximum=86_400_000.0,
                ),
                "onset_callback_delay_ms": normalize_float(
                    raw_event.get("onset_callback_delay_ms", 0),
                    field_name="card_event onset_callback_delay_ms",
                    minimum=0.0,
                    maximum=86_400_000.0,
                ),
                "deadline_callback_delay_ms": normalize_float(
                    raw_event.get("deadline_callback_delay_ms", 0),
                    field_name="card_event deadline_callback_delay_ms",
                    minimum=0.0,
                    maximum=86_400_000.0,
                ),
                "start_marker": normalize_text(raw_event.get("start_marker")),
                "stop_marker": normalize_text(raw_event.get("stop_marker")),
                # Overtime: the participant stayed after the duration was reached.
                "time_up_at": _optional_iso_timestamp(raw_event.get("time_up_at"), "card_event time_up_at"),
                "time_up_epoch_ms": normalize_float(
                    raw_event.get("time_up_epoch_ms"),
                    field_name="card_event time_up_epoch_ms",
                    minimum=0.0,
                    maximum=10_000_000_000_000.0,
                    allow_none=True,
                ),
                "time_up_event_id": normalize_text(raw_event.get("time_up_event_id")),
                "overtime_ms": normalize_float(
                    raw_event.get("overtime_ms"),
                    field_name="card_event overtime_ms",
                    minimum=0.0,
                    maximum=86_400_000.0,
                    allow_none=True,
                ),
                "overtime_capped": normalize_boolean(raw_event.get("overtime_capped", False)),
            }
        )

    return normalized_events


def _validate_attempt_history(value: Any) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 10:
        raise ValidationError("card_event attempt_history must contain at most 10 attempts.")
    normalized = []
    for attempt in value:
        if not isinstance(attempt, dict):
            raise ValidationError("Each card_event attempt must be an object.")
        stimulus_id = normalize_text(attempt.get("stimulus_id"))
        if not stimulus_id:
            raise ValidationError("card_event attempt stimulus_id is required.")
        normalized.append({
            "stimulus_id": stimulus_id,
            "start_event_id": normalize_text(attempt.get("start_event_id")),
            "stop_event_id": normalize_text(attempt.get("stop_event_id")),
            "active_started_at": _optional_iso_timestamp(attempt.get("active_started_at"), "attempt active_started_at"),
            "active_ended_at": _optional_iso_timestamp(attempt.get("active_ended_at"), "attempt active_ended_at"),
            "server_start_received_epoch_ms": normalize_float(
                attempt.get("server_start_received_epoch_ms"), field_name="attempt server_start_received_epoch_ms",
                minimum=0.0, maximum=10_000_000_000_000.0, allow_none=True,
            ),
            "server_stop_received_epoch_ms": normalize_float(
                attempt.get("server_stop_received_epoch_ms"), field_name="attempt server_stop_received_epoch_ms",
                minimum=0.0, maximum=10_000_000_000_000.0, allow_none=True,
            ),
            "interrupted_by_reload": normalize_boolean(attempt.get("interrupted_by_reload", False)),
            "inferred_from_checkpoint": normalize_boolean(attempt.get("inferred_from_checkpoint", False)),
            "reconciliation_outcome": normalize_text(attempt.get("reconciliation_outcome")),
        })
    return normalized


# ============================================================
#  3. ANSWER VALUES - one branch per card type
# ============================================================


def _validate_answer_value(*, answer_key: str, question: dict[str, Any], answer: Any, question_number: int) -> Any:
    return validate_card_answer(question.get("type"), question, answer, question_number)


# ============================================================
#  4. QUESTIONS & SETTINGS - config validation
# ============================================================
def _validate_question(question_data: Any, question_index: int, study_id: str = "") -> dict[str, Any]:
    normalized = _validate_question_by_type(question_data, question_index + 1)
    raw_card_id = question_data.get("card_id")
    if raw_card_id is None or raw_card_id == "":
        # Legacy studies get deterministic IDs. Once saved, the ID follows
        # the card when the researcher moves it in the editor.
        normalized["card_id"] = str(uuid5(NAMESPACE_URL, f"study-runner:{study_id}:{question_index}"))
    else:
        try:
            normalized["card_id"] = str(UUID(str(raw_card_id)))
        except (ValueError, AttributeError, TypeError) as error:
            raise ValidationError(f"Question {question_index + 1} card_id must be a UUID.") from error
    if normalized.get("type") not in NON_ANSWER_QUESTION_TYPES:
        normalized["required"] = normalize_boolean(question_data.get("required", True))

    # Optional per-question info text, shared by every card type. Only kept when set.
    info_top = normalize_text(question_data.get("info_top"))
    info_top = normalize_text(migrated_info_top(question_data, info_top))
    info_bottom = normalize_text(question_data.get("info_bottom"))
    if info_top:
        normalized["info_top"] = info_top
    if info_bottom:
        normalized["info_bottom"] = info_bottom

    return normalized


def _validate_question_by_type(question_data: Any, question_index: int) -> dict[str, Any]:
    if not isinstance(question_data, dict):
        raise ValidationError(f"Question {question_index} must be a JSON object.")
    question_type = require_text(question_data.get("type"), f"Question {question_index} type")
    if question_type not in ALLOWED_QUESTION_TYPES:
        raise ValidationError(f"Question {question_index} uses an unknown type: {question_type!r}.")
    return normalize_card_config(question_type, question_data, question_index)


def _validate_study_settings(value: Any) -> dict[str, Any]:
    if value is None:
        value = {"sensors_enabled": False}
    if not isinstance(value, dict):
        raise ValidationError("study_settings must be a JSON object.")

    raw_sensors = value.get("sensors")
    if raw_sensors is not None and not isinstance(raw_sensors, dict):
        raise ValidationError("study_settings.sensors must be a JSON object.")

    try:
        migrated = normalize_study_settings_plugins(value)
    except PluginConfigError as error:
        raise ValidationError(str(error)) from error

    raw_sensors = migrated.get("sensors")
    plugins = _validate_plugin_study_settings(migrated["plugins"])

    sensors_enabled = normalize_boolean(migrated.get("sensors_enabled", True))
    participant_language = migrated.get("participant_language", "en")
    if not isinstance(participant_language, str) or participant_language not in {"en", "de"}:
        raise ValidationError("study_settings.participant_language must be 'en' or 'de'.")
    return {
        "sensors_enabled": sensors_enabled,
        "sensors": normalize_study_sensors(
            {
                "sensors_enabled": sensors_enabled,
                "sensors": raw_sensors,
            }
        ),
        "plugins": plugins,
        "progress_bar_enabled": normalize_boolean(migrated.get("progress_bar_enabled", False)),
        "card_frame_enabled": normalize_boolean(migrated.get("card_frame_enabled", True)),
        "participant_language": participant_language,
        "cover_page": _validate_cover_page(migrated.get("cover_page")),
        "planned_session_duration_minutes": _optional_positive_minutes(
            migrated.get("planned_session_duration_minutes"),
            "study_settings.planned_session_duration_minutes",
        ),
    }


def _validate_cover_page(value: Any) -> dict[str, Any]:
    """Optional page shown after the admin releases the study, before the Participant ID card.

    It is not a card: no session, recording, or marker exists yet, so it never
    enters per-card timing or the recording quality window.
    """
    raw = value if isinstance(value, dict) else {}
    if value is not None and not isinstance(value, dict):
        raise ValidationError("study_settings.cover_page must be a JSON object.")
    try:
        content = normalize_media_content(raw, label="Cover page")
    except MediaContentError as error:
        raise ValidationError(str(error)) from error
    button_label = normalize_text(raw.get("button_label"))
    if len(button_label) > 60:
        raise ValidationError("Cover page button label may be at most 60 characters.")
    return {"enabled": normalize_boolean(raw.get("enabled", False)), **content, "button_label": button_label}


def _validate_plugin_study_settings(
    plugins: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate installed plugin settings from manifest schemas, key-agnostically."""

    from study_runner.plugin_framework.registry import get_plugin_manifests

    manifests = get_plugin_manifests()
    normalized = deepcopy(plugins)
    for plugin_key, selection in normalized.items():
        manifest = manifests.get(plugin_key)
        if not isinstance(manifest, dict) or not isinstance(selection, dict):
            continue
        settings = selection.get("settings")
        if not isinstance(settings, dict):
            continue
        schema = manifest.get("study_settings_schema") or {}
        for name, raw_value in list(settings.items()):
            field = schema.get(name)
            if not isinstance(field, dict):
                # Preserve settings from a newer manifest during downgrade.
                continue
            field_type = str(field.get("type") or "string")
            path = f"study_settings.plugins.{plugin_key}.settings.{name}"
            if field_type in {"string", "url", "choice"}:
                value = normalize_text(raw_value)
                if field_type == "choice" and field.get("options") and value not in field["options"]:
                    raise ValidationError(
                        f"{path} must be one of: {', '.join(map(str, field['options']))}."
                    )
                if field_type == "url" and value:
                    _validate_manifest_url(value, str(field.get("format") or ""), path, plugin_key, name)
                elif field_type == "string" and value and field.get("format"):
                    # A plain text field can declare a plugin-owned shape too,
                    # e.g. "a page link or its ID".
                    _validate_plugin_format(value, str(field["format"]), path, plugin_key, name)
                settings[name] = value
            elif field_type == "boolean":
                settings[name] = normalize_boolean(raw_value)
            elif field_type == "number":
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as error:
                    raise ValidationError(f"{path} must be a number.") from error
                minimum, maximum = field.get("minimum"), field.get("maximum")
                if minimum is not None and value < float(minimum):
                    raise ValidationError(f"{path} must be at least {minimum}.")
                if maximum is not None and value > float(maximum):
                    raise ValidationError(f"{path} must be at most {maximum}.")
                settings[name] = int(value) if value.is_integer() else value
            elif field_type == "object":
                # An opaque, plugin-owned JSON document (e.g. Notion's table
                # layout): too shaped for a generic field, so this validates
                # only size and that the plugin itself accepts the shape -
                # see Plugin.validate_study_setting, the same hook a "url"
                # field with a declared format uses for its own shape.
                value = raw_value if isinstance(raw_value, dict) else {}
                if raw_value not in (None, {}) and not isinstance(raw_value, dict):
                    raise ValidationError(f"{path} must be a JSON object.")
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
                max_length = int(field.get("max_length") or 200_000)
                if len(encoded) > max_length:
                    raise ValidationError(f"{path} is too large (at most {max_length} characters).")
                from study_runner.plugin_framework.registry import get_plugin

                plugin = get_plugin(plugin_key)
                validator = plugin.validate_study_setting if plugin else None
                if validator is not None:
                    try:
                        validator(name, encoded)
                    except ValueError as error:
                        raise ValidationError(f"{path}: {error}") from error
                settings[name] = value
    return normalized


def _validate_manifest_url(
    value: str,
    format_name: str,
    path: str,
    plugin_key: str,
    field_name: str,
) -> None:
    """A plain HTTP(S) check, or a plugin-owned shape when the field names one.

    `format_name` is documentation for the operator, not a name core matches
    on - the plugin that declared the field validates its own shape through
    `Plugin.validate_study_setting`, the same way it owns everything else
    about that field.
    """
    if format_name:
        _validate_plugin_format(value, format_name, path, plugin_key, field_name)
        return
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(f"{path} must be an HTTP(S) URL.")


def _validate_plugin_format(
    value: str,
    format_name: str,
    path: str,
    plugin_key: str,
    field_name: str,
) -> None:
    """Let the plugin that declared a field's format check the value's shape."""
    from study_runner.plugin_framework.registry import get_plugin

    plugin = get_plugin(plugin_key)
    validator = plugin.validate_study_setting if plugin else None
    if validator is None:
        raise ValidationError(
            f"{path}: plugin {plugin_key!r} declares format {format_name!r} "
            "but has no validator."
        )
    try:
        validator(field_name, value)
    except ValueError as error:
        raise ValidationError(f"{path}: {error}") from error


def _participant_field_options(questions: list[dict[str, Any]], field_key: str) -> list[str]:
    participant_question = next(
        (question for question in questions if question.get("type") == "participant-id"),
        None,
    )
    fields = (participant_question or {}).get("fields") or {}
    field_config = fields.get(field_key) or {}
    options = field_config.get("options")
    if isinstance(options, list) and options:
        return [opt for opt in options if isinstance(opt, str) and opt]
    return list(CONFIGURABLE_OPTION_DEFAULTS.get(field_key, []))


# ============================================================
#  5. PRIMITIVES - low-level normalize/require helpers
# ============================================================


def _optional_positive_minutes(value: Any, field_name: str) -> float | None:
    """None means unset -- distinct from zero, which would be a promise."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (value > 0) or value == float("inf"):
        raise ValidationError(f"{field_name} must be a positive number of minutes.")
    return float(value)


def _normalize_question_type(value: Any) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    if normalized not in ALLOWED_QUESTION_TYPES:
        raise ValidationError(f"question_type must be one of: {', '.join(sorted(ALLOWED_QUESTION_TYPES))}.")
    return normalized


def _require_iso_timestamp(value: Any, field_name: str) -> str:
    timestamp = require_text(value, field_name)
    try:
        _parse_iso_timestamp(timestamp)
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be a valid ISO timestamp.") from exc
    return timestamp


def _optional_iso_timestamp(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    return _require_iso_timestamp(value, field_name)


def _parse_iso_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
