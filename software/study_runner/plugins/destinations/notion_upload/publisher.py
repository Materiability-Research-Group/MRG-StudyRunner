"""Write one finished session into the study's Notion structure.

Every write is an update-or-create keyed by the session, so uploading a
session again - after a failed attempt or the operator's "Upload again"
with a changed layout - overwrites what that session wrote before and never
adds a second copy. Rows of other sessions and anything the operator added
in Notion stay untouched.
"""
from __future__ import annotations

from typing import Any

from .layout import included_cards
from .notion_api import NotionApi, compact_id, plain_text
from .session_rows import (
    KEY_COLUMN,
    PARTICIPANT_RELATION,
    PARTICIPANT_TITLE,
    SESSION_LIST_COLUMN,
    answers_cells,
    card_rows,
    comparison_cells,
    decode_sessions,
    fill_missing,
    participant_cells,
    session_facts,
)
from .structure import (
    ensure_answers,
    ensure_card_databases,
    ensure_comparison_page,
    ensure_participants,
    parent_page,
    title_column,
)


def publish_session(
    api: NotionApi,
    *,
    settings: dict[str, Any],
    layout: dict[str, Any],
    config_data: dict[str, Any],
    result_payload: dict[str, Any],
    card_summary: dict[str, Any] | None,
    updates: dict[str, str],
) -> dict[str, Any]:
    """Publish one session; ``updates`` collects ids to remember on the study."""
    facts = session_facts(result_payload)
    parent_page_id = parent_page(api, settings)
    report: list[dict[str, Any]] = []

    participants = ensure_participants(api, parent_page_id, settings, layout, updates, report)
    participant_page = _write_participant(api, participants, layout, result_payload, facts)

    rows = card_rows(layout, config_data, result_payload, card_summary)
    written = {"participant": 1, "answers": 0, "comparison": 0}
    if layout["answers_per_participant"]:
        answers = ensure_answers(api, participant_page, layout)
        existing = _rows_by_key(api, answers, f"{facts['session_id']}:", prefix=True)
        for row in rows:
            cells = answers_cells(layout, facts, row)
            _write(api, answers, existing.get(row["key"]), cells, layout)
            written["answers"] += 1

    if layout["card_comparison"]:
        comparison_page = ensure_comparison_page(api, parent_page_id, settings, updates, report)
        cards = included_cards(layout, config_data)
        databases = ensure_card_databases(api, comparison_page, cards, layout, participants, report)
        for row in rows:
            database = databases[row["card"]["card_id"]]
            cells = comparison_cells(layout, facts, row)
            cells[PARTICIPANT_RELATION] = [participant_page]
            existing = _rows_by_key(api, database, facts["session_id"])
            _write(api, database, existing.get(facts["session_id"]), cells, layout)
            written["comparison"] += 1
    return {"written": written, "calls": api.calls}


def _write_participant(
    api: NotionApi,
    database: dict[str, Any],
    layout: dict[str, Any],
    result_payload: dict[str, Any],
    facts: dict[str, str],
) -> str:
    title = title_column(database) or PARTICIPANT_TITLE
    rows = api.find_rows(database, title, facts["participant_id"])
    row = rows[0] if rows else None
    known = decode_sessions(plain_text(((row or {}).get("properties") or {}).get(SESSION_LIST_COLUMN, {}).get("rich_text")))
    sessions = [item for item in known if item[0] != facts["session_id"]] + [(facts["session_id"], facts["start"])]
    cells = participant_cells(layout, result_payload, sessions)
    return _write(api, database, row["id"] if row else None, cells, layout)


def _rows_by_key(api: NotionApi, database: dict[str, Any], key: str, *, prefix: bool = False) -> dict[str, str]:
    """``{key: page id}`` of the rows this session wrote before."""
    found: dict[str, str] = {}
    for row in api.find_rows(database, KEY_COLUMN, key, prefix=prefix):
        row_key = plain_text(((row.get("properties") or {}).get(KEY_COLUMN) or {}).get("rich_text"))
        found.setdefault(row_key, compact_id(row["id"]))
    return found


def _write(
    api: NotionApi,
    database: dict[str, Any],
    page_id: str | None,
    cells: dict[str, Any],
    layout: dict[str, Any],
) -> str:
    """Fill missing codes by each column's real type, then write the row."""
    title = title_column(database)
    named = dict(cells)
    our_title = next((name for name in (PARTICIPANT_TITLE, "Card") if name in named and name not in database["columns"]), None)
    if title and our_title:
        named[title] = named.pop(our_title)  # the operator renamed the title column
    filled = {
        name: value if name in {PARTICIPANT_RELATION, SESSION_LIST_COLUMN, KEY_COLUMN}
        else fill_missing(database["columns"].get(name, "rich_text"), value, layout["missing"])
        for name, value in named.items()
    }
    return api.write_row(database, page_id, filled)
