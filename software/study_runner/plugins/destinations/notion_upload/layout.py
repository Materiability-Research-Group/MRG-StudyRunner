"""What a study sends to Notion: the ``notion_layout`` study setting.

The layout mirrors the structure it creates in Notion, one level at a time:

1. **Participants** (always): one row per participant. ``participants.columns``
   picks the session summary and the stored intake fields to show.
2. **Answers per participant** (opt-in): a table inside each participant's
   page with one row per session and card.
3. **Card comparison** (opt-in): one table per card with one row per
   session, so every participant's data of that card sits side by side.

``values`` is the one list of values every card provides (its answer, how
long it was shown, sensor statistics), each with its own choice of the
answers table and the card comparison. ``cards`` only switches single cards
off or on. ``missing`` is written where a card was skipped or not shown.

Standard library only: study settings are validated with this before any
Notion client exists.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

from study_runner.contracts.participant_fields import PARTICIPANT_FIELD_ORDER

from .answer_columns import ANSWERLESS_TYPES

LAYOUT_VERSION = 1
SESSION_COLUMNS = ("session_count", "first_session", "last_session")
# Shown in the configurator, but switched off unless the operator wants them.
CARD_TYPES_OFF_BY_DEFAULT = frozenset({"info"})
# Never a card of their own in Notion: nothing happens on them.
CARD_TYPES_NEVER_LISTED = frozenset({"finish", "participant-id"})
STREAM_STATS = ("mean", "min", "max", "stddev", "mode", "coverage", "max_gap_seconds")
_VALUE_SOURCE = re.compile(
    r"^(?:answer|duration_seconds|stream\.[^.\s]+\.channel\.[^.\s]+\.(?:" + "|".join(STREAM_STATS) + r"))$"
)
_TOP_LEVEL_KEYS = {"version", "participants", "answers_per_participant", "values", "cards", "card_comparison", "missing"}
MAX_VALUES = 60
MAX_CARD_SWITCHES = 500
DEFAULT_MISSING = {"number": 9999, "text": "n.a."}
DEFAULT_VALUES = (
    {"source": "answer", "answers": True, "comparison": False},
    {"source": "duration_seconds", "answers": True, "comparison": False},
)


class LayoutError(ValueError):
    """A ``notion_layout`` value the upload could not safely follow."""


def validate_layout(value: Any) -> None:
    """Raise LayoutError for anything ``effective_layout`` would have to guess."""
    if not isinstance(value, dict):
        raise LayoutError("notion_layout must be a JSON object.")
    if not value:
        return  # not configured yet: the defaults apply
    unknown = set(value) - _TOP_LEVEL_KEYS
    if unknown:
        raise LayoutError(f"notion_layout has unsupported fields: {', '.join(sorted(unknown))}.")
    if value.get("version") != LAYOUT_VERSION:
        raise LayoutError(f"notion_layout.version must be {LAYOUT_VERSION}.")
    participants = value.get("participants", {})
    if not isinstance(participants, dict) or set(participants) - {"columns"}:
        raise LayoutError("notion_layout.participants must be an object with a columns list.")
    columns = participants.get("columns", [])
    allowed = set(SESSION_COLUMNS) | set(PARTICIPANT_FIELD_ORDER)
    if not isinstance(columns, list) or any(column not in allowed for column in columns):
        raise LayoutError(f"notion_layout.participants.columns may only list: {', '.join(sorted(allowed))}.")
    for flag in ("answers_per_participant", "card_comparison"):
        if flag in value and not isinstance(value[flag], bool):
            raise LayoutError(f"notion_layout.{flag} must be true or false.")
    _validate_values(value.get("values", []))
    cards = value.get("cards", {})
    if not isinstance(cards, dict) or len(cards) > MAX_CARD_SWITCHES or any(
        not isinstance(key, str) or not key or not isinstance(on, bool) for key, on in cards.items()
    ):
        raise LayoutError("notion_layout.cards must map card IDs to true or false.")
    _validate_missing(value.get("missing", {}))


def _validate_values(values: Any) -> None:
    if not isinstance(values, list) or len(values) > MAX_VALUES:
        raise LayoutError(f"notion_layout.values must be a list of at most {MAX_VALUES} values.")
    seen: set[str] = set()
    for index, entry in enumerate(values):
        path = f"notion_layout.values[{index}]"
        if not isinstance(entry, dict) or set(entry) - {"source", "answers", "comparison"}:
            raise LayoutError(f"{path} must be an object with source, answers and comparison.")
        source = entry.get("source")
        if not isinstance(source, str) or not _VALUE_SOURCE.fullmatch(source):
            raise LayoutError(f"{path}.source {source!r} is not a value a card provides.")
        if source in seen:
            raise LayoutError(f"{path}.source {source!r} is listed twice.")
        seen.add(source)
        for flag in ("answers", "comparison"):
            if flag in entry and not isinstance(entry[flag], bool):
                raise LayoutError(f"{path}.{flag} must be true or false.")


def _validate_missing(missing: Any) -> None:
    if not isinstance(missing, dict) or set(missing) - {"number", "text"}:
        raise LayoutError("notion_layout.missing must be an object with number and text.")
    number = missing.get("number", DEFAULT_MISSING["number"])
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
        raise LayoutError("notion_layout.missing.number must be a number.")
    text = missing.get("text", DEFAULT_MISSING["text"])
    if not isinstance(text, str) or not text.strip() or len(text) > 100 or "," in text:
        raise LayoutError("notion_layout.missing.text must be a short text without commas.")


def effective_layout(value: Any, config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """The layout with every default filled in; ``value`` must be valid."""
    raw = value if isinstance(value, dict) and value else {}
    participants = raw.get("participants")
    if isinstance(participants, dict) and "columns" in participants:
        columns = list(participants["columns"])
    else:
        columns = [*SESSION_COLUMNS, *stored_participant_fields(config_data or {})]
    values = raw.get("values") if "values" in raw else deepcopy(list(DEFAULT_VALUES))
    return {
        "version": LAYOUT_VERSION,
        "participants": {"columns": columns},
        "answers_per_participant": bool(raw.get("answers_per_participant", False)),
        "values": [
            {"source": item["source"], "answers": item.get("answers", True), "comparison": item.get("comparison", False)}
            for item in values
        ],
        "cards": dict(raw.get("cards") or {}),
        "card_comparison": bool(raw.get("card_comparison", False)),
        "missing": {**DEFAULT_MISSING, **(raw.get("missing") or {})},
    }


def stored_participant_fields(config_data: dict[str, Any]) -> list[str]:
    """Intake fields the participant-id card stores (the others never leave the computer)."""
    question = next(
        (item for item in config_data.get("questions") or [] if isinstance(item, dict) and item.get("type") == "participant-id"),
        None,
    )
    fields = (question or {}).get("fields") or {}
    return [
        key for key in PARTICIPANT_FIELD_ORDER
        if (fields.get(key) or {}).get("enabled") and (fields.get(key) or {}).get("store")
    ]


def listed_cards(config_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every card of the study that can appear in Notion, in study order."""
    cards = []
    for index, question in enumerate(config_data.get("questions") or []):
        if not isinstance(question, dict) or question.get("type") in CARD_TYPES_NEVER_LISTED:
            continue
        card_id = str(question.get("card_id") or "").strip()
        if not card_id:
            continue  # a card the study editor has not saved yet
        cards.append({
            "card_id": card_id,
            "index": index,
            "type": str(question.get("type") or ""),
            "label": card_label(index, question),
            "question": question,
            "default_included": question.get("type") not in CARD_TYPES_OFF_BY_DEFAULT,
        })
    return cards


def included_cards(layout: dict[str, Any], config_data: dict[str, Any]) -> list[dict[str, Any]]:
    switches = layout.get("cards") or {}
    return [card for card in listed_cards(config_data) if switches.get(card["card_id"], card["default_included"])]


def card_label(index: int, question: dict[str, Any]) -> str:
    """``03 · How relaxed do you feel?``: position and text, on one line."""
    text = " ".join(str(question.get("prompt") or question.get("title") or question.get("type") or "Card").split())
    if len(text) > 80:
        text = text[:79].rstrip() + "…"
    return f"{index + 1:02d} · {text}"


def value_columns(layout: dict[str, Any], *, place: str) -> list[dict[str, str]]:
    """The non-answer values for ``place`` ("answers" or "comparison") as columns."""
    return [
        {"source": item["source"], "name": value_column_name(item["source"]), "type": value_column_type(item["source"])}
        for item in layout["values"]
        if item.get(place) and item["source"] != "answer"
    ]


def wants_answer(layout: dict[str, Any], *, place: str) -> bool:
    return any(item["source"] == "answer" and item.get(place) for item in layout["values"])


def value_column_name(source: str) -> str:
    if source == "duration_seconds":
        return "Duration (s)"
    _stream, key, _channel, channel, stat = source.split(".", 4)
    return f"{key} · {channel} · {stat}"


def value_column_type(source: str) -> str:
    return "rich_text" if source.endswith(".mode") else "number"


def card_collects_answer(card: dict[str, Any]) -> bool:
    return card["type"] not in ANSWERLESS_TYPES
