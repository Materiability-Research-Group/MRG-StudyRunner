"""Find or create the Notion databases a study's layout needs.

Each database is found again by something that survives a lost setting:

- **Participants**: the saved ``database_id``, else a database titled
  "Participants" (or the older "StudyRunner Participants") on the parent page.
- **Card comparison**: a page of that name on the parent page holding one
  database per card. Each card database names its card id in its
  description, so it is found again even after the card's text changed.
- **Answers**: a database inside the participant's own page, marked the
  same way.

Nothing is ever deleted or moved. A column the layout no longer lists stays
in Notion; a column it newly lists is added. With ``apply=False`` the
functions only report what they would create (the configurator's check).
"""
from __future__ import annotations

from typing import Any

from .notion_api import NotionApi, compact_id
from .session_rows import (
    PARTICIPANT_RELATION,
    answers_columns,
    comparison_columns,
    participants_columns,
)

PARTICIPANTS_TITLE = "Participants"
LEGACY_PARTICIPANTS_TITLE = "StudyRunner Participants"
COMPARISON_PAGE_TITLE = "Card comparison"
ANSWERS_TITLE = "Answers"
PARTICIPANTS_MARKER = "Study Runner participants"
ANSWERS_MARKER = "Study Runner answers"
CARD_MARKER_PREFIX = "Study Runner card "
_NOT_FOUND_CODES = {"object_not_found", "objectnotfound"}


def card_marker(card_id: str) -> str:
    return f"{CARD_MARKER_PREFIX}{card_id}"


def parent_page(api: NotionApi, settings: dict[str, Any]) -> str:
    """The study's Notion page: the pasted one, else the one its Participants table is on.

    A study set up with only a database id (plugin 1.x allowed that) keeps
    working; its new tables are created next to that database.
    """
    page_id = compact_id(settings.get("parent_page_id"))
    if page_id:
        return page_id
    database = _usable_database(api, compact_id(settings.get("database_id")))
    if database and database["parent_page_id"]:
        return database["parent_page_id"]
    raise ValueError("parent_page_id is required")


def ensure_participants(
    api: NotionApi,
    parent_page_id: str,
    settings: dict[str, Any],
    layout: dict[str, Any],
    updates: dict[str, str],
    report: list[dict[str, Any]],
    *,
    apply: bool = True,
) -> dict[str, Any] | None:
    columns = participants_columns(layout)
    database = _usable_database(api, compact_id(settings.get("database_id")))
    if database is None:
        found = next(
            (db_id for db_id, title in api.child_databases(parent_page_id)
             if title in {PARTICIPANTS_TITLE, LEGACY_PARTICIPANTS_TITLE}),
            "",
        )
        database = _usable_database(api, found)
    if database is None:
        report.append({"table": PARTICIPANTS_TITLE, "status": "create", "columns": sorted(columns)})
        if not apply:
            return None
        database = api.create_database(parent_page_id, PARTICIPANTS_TITLE, PARTICIPANTS_MARKER, columns)
    else:
        _report_columns(report, PARTICIPANTS_TITLE, database, columns, apply, api)
    if database["id"] != compact_id(settings.get("database_id")):
        updates["database_id"] = database["id"]
    if database["data_source_id"] != str(settings.get("data_source_id") or ""):
        updates["data_source_id"] = database["data_source_id"]
    return database


def ensure_comparison_page(
    api: NotionApi,
    parent_page_id: str,
    settings: dict[str, Any],
    updates: dict[str, str],
    report: list[dict[str, Any]],
    *,
    apply: bool = True,
) -> str:
    page_id = compact_id(settings.get("comparison_page_id"))
    if page_id and not _page_usable(api, page_id):
        page_id = ""
    if not page_id:
        page_id = next((pid for pid, title in api.child_pages(parent_page_id) if title == COMPARISON_PAGE_TITLE), "")
    if not page_id:
        report.append({"table": COMPARISON_PAGE_TITLE, "status": "create", "columns": []})
        if not apply:
            return ""
        page_id = api.create_page(parent_page_id, COMPARISON_PAGE_TITLE)
    if page_id != compact_id(settings.get("comparison_page_id")):
        updates["comparison_page_id"] = page_id
    return page_id


def ensure_card_databases(
    api: NotionApi,
    comparison_page_id: str,
    cards: list[dict[str, Any]],
    layout: dict[str, Any],
    participants: dict[str, Any] | None,
    report: list[dict[str, Any]],
    *,
    apply: bool = True,
) -> dict[str, dict[str, Any]]:
    """``{card id: database}`` for every card, created where missing."""
    existing: dict[str, dict[str, Any]] = {}
    for db_id, _title in (api.child_databases(comparison_page_id) if comparison_page_id else []):
        database = _usable_database(api, db_id)
        if database and database["description"].startswith(CARD_MARKER_PREFIX):
            existing.setdefault(database["description"][len(CARD_MARKER_PREFIX):].strip(), database)
    databases: dict[str, dict[str, Any]] = {}
    for card in cards:
        columns: dict[str, Any] = dict(comparison_columns(layout, card))
        if participants is not None:
            columns[PARTICIPANT_RELATION] = {"relation": {
                "data_source_id": participants["data_source_id"],
                "type": "single_property",
                "single_property": {},
            }}
        database = existing.get(card["card_id"])
        if database is None:
            report.append({"table": card["label"], "status": "create", "columns": sorted(columns)})
            if not apply:
                continue
            database = api.create_database(comparison_page_id, card["label"], card_marker(card["card_id"]), columns)
        else:
            if database["title"] != card["label"]:
                report.append({"table": card["label"], "status": "rename", "columns": []})
                if apply:
                    api.rename_database(database["id"], card["label"])
            _report_columns(report, card["label"], database, columns, apply, api)
        databases[card["card_id"]] = database
    return databases


def ensure_answers(api: NotionApi, participant_page_id: str, layout: dict[str, Any]) -> dict[str, Any]:
    """The answers database inside one participant's page."""
    columns = answers_columns(layout)
    for db_id, _title in api.child_databases(participant_page_id):
        database = _usable_database(api, db_id)
        if database and database["description"] == ANSWERS_MARKER:
            api.add_columns(database, columns)
            return database
    return api.create_database(participant_page_id, ANSWERS_TITLE, ANSWERS_MARKER, columns)


def title_column(database: dict[str, Any]) -> str:
    """A database's one title column, whatever the operator renamed it to."""
    return next((name for name, kind in database["columns"].items() if kind == "title"), "")


def _report_columns(
    report: list[dict[str, Any]],
    table: str,
    database: dict[str, Any],
    columns: dict[str, Any],
    apply: bool,
    api: NotionApi,
) -> None:
    if apply:
        added = api.add_columns(database, columns)
    else:
        has_title = "title" in database["columns"].values()
        added = sorted(
            name for name, spec in columns.items()
            if name not in database["columns"] and not (has_title and spec == "title")
        )
    report.append({"table": table, "status": "add_columns" if added else "ok", "columns": added})


def _usable_database(api: NotionApi, database_id: str) -> dict[str, Any] | None:
    """The database, or None when it is gone or in Notion's trash."""
    if not database_id:
        return None
    try:
        database = api.database(database_id)
    except Exception as error:
        if _is_not_found(error):
            return None
        raise
    return None if database["in_trash"] else database


def _page_usable(api: NotionApi, page_id: str) -> bool:
    try:
        page = api.page(page_id)
    except Exception as error:
        if _is_not_found(error):
            return False
        raise
    return not (page.get("in_trash") or page.get("archived"))


def _is_not_found(error: Exception) -> bool:
    return str(getattr(error, "code", "") or "").lower().rsplit(".", 1)[-1] in _NOT_FOUND_CODES
