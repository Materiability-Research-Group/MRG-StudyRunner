"""One finished session as the rows the layout sends to Notion.

Nothing here calls Notion: given the layout, the study as it was run and the
session's ``result.json`` and ``card-summary.json``, it decides every column
(name and Notion type) and every value. ``publisher.py`` writes them.

Every included card gets a row in every session, also when the participant
skipped it or never saw it: its ``Status`` says which, and its values are the
layout's missing codes (9999 / "n.a." by default), so a table of all
participants has no silent gaps.
"""
from __future__ import annotations

from typing import Any

from .answer_columns import answer_cells, answer_columns, format_answer
from .layout import (
    card_collects_answer,
    included_cards,
    value_columns,
    wants_answer,
)

KEY_COLUMN = "StudyRunner Key"
STATUS_COLUMN = "Status"
SESSION_START_COLUMN = "Session start"
SESSION_ID_COLUMN = "Session ID"
PARTICIPANT_TITLE = "Participant ID"
CARD_TITLE = "Card"
PARTICIPANT_RELATION = "Participant"
SESSION_LIST_COLUMN = "StudyRunner Sessions"
STATUSES = ("answered", "skipped", "not shown", "shown")

# Intake fields as they appear in the Participants table.
PARTICIPANT_FIELD_COLUMNS = {
    "first_name": ("First Name", "rich_text"),
    "last_name": ("Last Name", "rich_text"),
    "age_group": ("Age Group", "select"),
    "gender": ("Gender", "select"),
    "childhood_area": ("Childhood Area", "select"),
    "childhood_nearest_city": ("Childhood Nearest City", "rich_text"),
    "birth_place": ("Birth Place", "rich_text"),
    "birth_date": ("Birth Date", "date"),
}
SESSION_SUMMARY_COLUMNS = {
    "session_count": ("Sessions", "number"),
    "first_session": ("First Session", "date"),
    "last_session": ("Last Session", "date"),
}


# ── Columns (the same for every session of a study) ────────────────────────

def participants_columns(layout: dict[str, Any]) -> dict[str, str]:
    columns = {PARTICIPANT_TITLE: "title", SESSION_LIST_COLUMN: "rich_text"}
    for key in layout["participants"]["columns"]:
        name, column_type = SESSION_SUMMARY_COLUMNS.get(key) or PARTICIPANT_FIELD_COLUMNS.get(key) or (None, None)
        if name:
            columns[name] = column_type
    return columns


def answers_columns(layout: dict[str, Any]) -> dict[str, str]:
    columns = _row_columns(CARD_TITLE)
    if wants_answer(layout, place="answers"):
        columns["Answer"] = "rich_text"
    columns.update({item["name"]: item["type"] for item in value_columns(layout, place="answers")})
    return columns


def comparison_columns(layout: dict[str, Any], card: dict[str, Any]) -> dict[str, str]:
    """Without the relation to the Participants table, which needs its id."""
    columns = _row_columns(PARTICIPANT_TITLE)
    if wants_answer(layout, place="comparison"):
        columns.update(dict(answer_columns(card["question"])))
    columns.update({item["name"]: item["type"] for item in value_columns(layout, place="comparison")})
    return columns


def _row_columns(title: str) -> dict[str, str]:
    return {
        title: "title",
        KEY_COLUMN: "rich_text",
        SESSION_ID_COLUMN: "rich_text",
        SESSION_START_COLUMN: "date",
        STATUS_COLUMN: "select",
    }


# ── Rows (one session) ────────────────────────────────────────────────────

def session_facts(result_payload: dict[str, Any]) -> dict[str, str]:
    session_id = str(result_payload.get("session_id") or "").strip()
    participant_id = str(result_payload.get("participant_id") or "").strip()
    if not session_id or not participant_id:
        raise ValueError("The finalized result.json has no session_id or participant_id.")
    return {
        "session_id": session_id,
        "participant_id": participant_id,
        "start": str(result_payload.get("timestamp_start") or ""),
    }


def participant_cells(
    layout: dict[str, Any],
    result_payload: dict[str, Any],
    sessions: list[tuple[str, str]],
) -> dict[str, Any]:
    """The participant's row; ``sessions`` is every (session id, start) known so far."""
    metadata = result_payload.get("participant_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    starts = sorted(start for _session, start in sessions if start)
    cells: dict[str, Any] = {
        PARTICIPANT_TITLE: session_facts(result_payload)["participant_id"],
        SESSION_LIST_COLUMN: encode_sessions(sessions),
    }
    for key in layout["participants"]["columns"]:
        if key == "session_count":
            cells["Sessions"] = len(sessions)
        elif key == "first_session":
            cells["First Session"] = starts[0] if starts else None
        elif key == "last_session":
            cells["Last Session"] = starts[-1] if starts else None
        elif key in PARTICIPANT_FIELD_COLUMNS:
            value = str(metadata.get(key) or "").strip()
            cells[PARTICIPANT_FIELD_COLUMNS[key][0]] = value or None
    return cells


def card_rows(
    layout: dict[str, Any],
    config_data: dict[str, Any],
    result_payload: dict[str, Any],
    card_summary: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """One entry per included card: its status, answer and values in this session."""
    facts = session_facts(result_payload)
    details = _details_by_card(config_data, result_payload)
    streams = _streams_by_index(card_summary)
    rows = []
    for card in included_cards(layout, config_data):
        detail = details.get(card["card_id"])
        if detail is None:
            status = "not shown"
        elif detail.get("skipped"):
            status = "skipped"
        else:
            status = "answered" if card_collects_answer(card) else "shown"
        usable = status in {"answered", "shown"}
        answer = detail.get("answer") if usable and card_collects_answer(card) else None
        values = {
            source: _value(source, detail, streams.get(card["index"]) or {}) if usable else None
            for source in (item["source"] for item in layout["values"] if item["source"] != "answer")
        }
        rows.append({
            "card": card,
            "status": status,
            "answer": answer,
            "values": values,
            # The answers table holds every card of every session.
            "key": f"{facts['session_id']}:{card['card_id']}",
        })
    return rows


def answers_cells(layout: dict[str, Any], facts: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    cells = _row_cells(CARD_TITLE, row["card"]["label"], row["key"], facts, row)
    if wants_answer(layout, place="answers"):
        cells["Answer"] = format_answer(row["answer"]) or None
    for item in value_columns(layout, place="answers"):
        cells[item["name"]] = row["values"].get(item["source"])
    return cells


def comparison_cells(layout: dict[str, Any], facts: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    cells = _row_cells(PARTICIPANT_TITLE, facts["participant_id"], facts["session_id"], facts, row)
    if wants_answer(layout, place="comparison"):
        cells.update(answer_cells(row["card"]["question"], row["answer"]))
    for item in value_columns(layout, place="comparison"):
        cells[item["name"]] = row["values"].get(item["source"])
    return cells


def _row_cells(title_column: str, title: str, key: str, facts: dict[str, str], row: dict[str, Any]) -> dict[str, Any]:
    return {
        title_column: title,
        KEY_COLUMN: key,
        SESSION_ID_COLUMN: facts["session_id"],
        SESSION_START_COLUMN: facts["start"] or None,
        STATUS_COLUMN: row["status"],
    }


def fill_missing(column_type: str, value: Any, missing: dict[str, Any]) -> Any:
    """The layout's missing code for an empty cell, by column type."""
    if value not in (None, "", []):
        return value
    if column_type == "number":
        return missing["number"]
    if column_type in {"rich_text", "select", "title"}:
        return missing["text"]
    if column_type == "multi_select":
        return [missing["text"]]
    return None


# ── The session list kept on the participant's row ─────────────────────────

def encode_sessions(sessions: list[tuple[str, str]]) -> str:
    """``session id|start`` lines; re-uploading a session never counts it twice."""
    return "\n".join(f"{session}|{start}" for session, start in sorted(set(sessions), key=lambda item: (item[1], item[0])))


def decode_sessions(text: str) -> list[tuple[str, str]]:
    sessions = []
    for line in str(text or "").splitlines():
        session, _sep, start = line.partition("|")
        if session.strip():
            sessions.append((session.strip(), start.strip()))
    return sessions


# ── Reading one session ───────────────────────────────────────────────────

def _details_by_card(config_data: dict[str, Any], result_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``answer_details`` keyed by card id, also for sessions recorded before card ids existed."""
    questions = config_data.get("questions") or []
    by_card: dict[str, dict[str, Any]] = {}
    for detail in result_payload.get("answer_details") or []:
        if not isinstance(detail, dict):
            continue
        card_id = str(detail.get("card_id") or "")
        index = detail.get("question_index")
        if not card_id and isinstance(index, int) and 0 <= index < len(questions):
            question = questions[index] if isinstance(questions[index], dict) else {}
            expected = str(question.get("prompt") or question.get("title") or question.get("subtitle") or "")
            if expected == str(detail.get("question_prompt") or ""):
                card_id = str(question.get("card_id") or "")
        if card_id:
            by_card[card_id] = detail
    return by_card


def _streams_by_index(card_summary: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    by_index: dict[int, dict[str, Any]] = {}
    for card in (card_summary or {}).get("cards") or []:
        if isinstance(card, dict) and isinstance(card.get("question_index"), int):
            streams = card.get("streams") or {}
            by_index[card["question_index"]] = {
                stable_stream_key(stream): stream for stream in streams.values() if isinstance(stream, dict)
            }
    return by_index


def _value(source: str, detail: dict[str, Any], streams: dict[str, Any]) -> Any:
    if source == "duration_seconds":
        return detail.get("interval_seconds", detail.get("seconds_since_previous_answer"))
    _stream, key, _channel, channel, stat = source.split(".", 4)
    channels = (streams.get(key) or {}).get("channels") or {}
    return (channels.get(channel) or {}).get(stat)


def stable_stream_key(stream: dict[str, Any]) -> str:
    """A stream name that stays the same across sessions.

    `card-summary.json` keys a stream by its LSL stream id, a small number
    that changes when a sensor connects in another order. The manifest's
    ``source_id`` (e.g. ``study_runner.brainbit.eeg``) does not change, so a
    value picked in one session reads the same stream in every other one.
    """
    plugin_key = str(stream.get("plugin_key") or "").strip()
    source_id = str(stream.get("source_id") or "").strip()
    if plugin_key and source_id:
        return f"{plugin_key}_{source_id.rsplit('.', 1)[-1]}"
    return str(stream.get("stream_key") or "unknown")


def sensor_values(card_summary: dict[str, Any] | None) -> list[dict[str, str]]:
    """Every sensor value one real session offers, for the configurator."""
    seen: dict[str, dict[str, str]] = {}
    for streams in _streams_by_index(card_summary).values():
        for key, stream in streams.items():
            for channel_name, channel in (stream.get("channels") or {}).items():
                if not isinstance(channel, dict):
                    continue
                stats = ("mode",) if channel.get("kind") == "categorical" else (
                    "mean", "min", "max", "stddev", "coverage", "max_gap_seconds",
                )
                for stat in stats:
                    if channel.get(stat) is not None:
                        source = f"stream.{key}.channel.{channel_name}.{stat}"
                        seen.setdefault(source, {"source": source, "label": f"{key} · {channel_name} · {stat}"})
    return sorted(seen.values(), key=lambda item: item["label"])
