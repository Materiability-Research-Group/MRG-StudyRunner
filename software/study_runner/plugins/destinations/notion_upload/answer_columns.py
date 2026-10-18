"""Typed Notion columns for one card's answer, used by the card comparison.

A card comparison database holds one card only, so its answer can have real
Notion types: a slider answer is a number, a single choice a select, and an
answer made of parts (a semantic differential, a mood meter position) gets
one column per part. The participant's own answers table mixes every card in
one column and therefore always writes the answer as text
(``format_answer``).

The columns come from the card's settings, not from one session's answer, so
every session of a study writes into the same columns. A card type that is
not listed here still works: its answer is written as text.
``test_notion_answer_columns.py`` keeps every registered card type listed.

Standard library only; no Notion client is needed to decide a column.
"""
from __future__ import annotations

from typing import Any, Callable

# Card types that show something but collect no answer.
ANSWERLESS_TYPES = frozenset({"info", "stimulus", "finish", "participant-id"})
POSITION_VARIANTS = frozenset({"field", "orbit"})
# Every card type with its own columns below. Anything else is written as text.
TYPED_TYPES = frozenset({
    "slider", "likert", "single", "choice", "word-cloud", "text", "ranking",
    "semantic", "multi-slider", "mood-meter", "affect-map",
})

Reader = Callable[[Any], Any]


def answer_columns(question: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(column name, Notion type), ...]`` for one card's answer."""
    return [(name, column_type) for name, column_type, _read in _columns(question)]


def answer_cells(question: dict[str, Any], answer: Any) -> dict[str, Any]:
    """The answer split into those columns; a part that is not there is None."""
    cells: dict[str, Any] = {}
    for name, _column_type, read in _columns(question):
        try:
            cells[name] = read(answer)
        except (TypeError, ValueError, KeyError, AttributeError):
            cells[name] = None
    return cells


def format_answer(value: Any) -> str:
    """One answer as readable text, e.g. ``words: calm, curious · energy: 0.4``."""
    if isinstance(value, dict):
        return " · ".join(
            f"{key}: {', '.join(str(item) for item in part) if isinstance(part, list) else part}"
            for key, part in value.items()
        )
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return "" if value is None else str(value)


def _columns(question: dict[str, Any]) -> list[tuple[str, str, Reader]]:
    kind = str(question.get("type") or "")
    if kind in ANSWERLESS_TYPES:
        return []
    if kind in {"slider", "likert"}:
        return [("Answer", "number", _number)]
    if kind == "single":
        return [("Answer", "select", _text)]
    if kind in {"choice", "word-cloud"}:
        return [("Answer", "multi_select", _words)]
    if kind == "text":
        return [("Answer", "rich_text", _text)]
    if kind == "ranking":
        options = [str(option) for option in question.get("options") or []]
        return [("Answer", "rich_text", lambda answer: " > ".join(str(item) for item in answer))] + [
            (f"Rank · {option}", "number", _rank_of(option)) for option in options
        ]
    if kind == "semantic":
        return [
            (f"Answer · {left}–{right}", "number", _part(f"{left}_{right}", _number))
            for left, right in (pair for pair in question.get("pairs") or [] if len(pair) == 2)
        ]
    if kind == "multi-slider":
        labels = [str(item.get("label")) for item in question.get("dimensions") or [] if isinstance(item, dict)]
        return [(f"Answer · {label}", "number", _part(label, _number)) for label in labels]
    if kind in {"mood-meter", "affect-map"}:
        default_variant = "classic" if kind == "mood-meter" else "field"
        variant = str(question.get("variant") or default_variant)
        if variant not in POSITION_VARIANTS:
            return [("Answer", "multi_select", _words)]
        columns = [
            ("Answer · words", "multi_select", _part("words", _words)),
            ("Answer · pleasantness", "number", _part("pleasantness", _number)),
            ("Answer · energy", "number", _part("energy", _number)),
        ]
        if variant == "orbit":
            columns.append(("Answer · intensity", "number", _part("intensity", _number)))
        return columns
    return [("Answer", "rich_text", lambda answer: format_answer(answer) or None)]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _words(value: Any) -> list[str] | None:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    return [str(item) for item in value if item not in (None, "")]


def _part(key: str, read: Reader) -> Reader:
    return lambda answer: read(answer.get(key)) if isinstance(answer, dict) else None


def _rank_of(option: str) -> Reader:
    def read(answer: Any) -> float | None:
        ranked = [str(item) for item in answer] if isinstance(answer, list) else []
        return float(ranked.index(option) + 1) if option in ranked else None
    return read
