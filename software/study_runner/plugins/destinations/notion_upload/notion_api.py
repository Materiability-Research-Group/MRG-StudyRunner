"""The few Notion API calls the upload needs, paced and retried.

Notion allows about three requests per second per integration and answers
``rate_limited`` (HTTP 429) above that. ``NotionApi.call`` keeps that pace
and waits as long as Notion asks before it tries again, so one session with
many cards never fails just for being large.

Only the data-source API (Notion version 2025-09-03, notion-client 3) is
used: a database holds its columns in a data source, rows are pages in that
data source.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .notion_ids import notion_id_from_input

MIN_SECONDS_BETWEEN_CALLS = 0.34
MAX_RATE_LIMIT_RETRIES = 4
MAX_RETRY_AFTER_SECONDS = 30.0
TEXT_LIMIT = 2000
OPTION_LIMIT = 100
WRITABLE_TYPES = {"title", "rich_text", "number", "select", "multi_select", "date", "relation"}


class NotionApi:
    def __init__(
        self,
        client: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self._sleep = sleep
        self._clock = clock
        self._last_call = -MIN_SECONDS_BETWEEN_CALLS
        self.calls = 0

    def call(self, method: Callable[..., Any], **kwargs: Any) -> Any:
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            wait = self._last_call + MIN_SECONDS_BETWEEN_CALLS - self._clock()
            if wait > 0:
                self._sleep(wait)
            self._last_call = self._clock()
            self.calls += 1
            try:
                return method(**kwargs)
            except Exception as error:
                if _error_code(error) != "rate_limited" or attempt == MAX_RATE_LIMIT_RETRIES:
                    raise
                self._sleep(_retry_after(error, attempt))
        raise AssertionError("unreachable")

    # ── Pages and databases ──

    def children(self, block_id: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        cursor = None
        while True:
            kwargs: dict[str, Any] = {"block_id": block_id, "page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            response = self.call(self.client.blocks.children.list, **kwargs)
            blocks.extend(response.get("results") or [])
            cursor = response.get("next_cursor") if response.get("has_more") else None
            if not cursor:
                return blocks

    def child_databases(self, page_id: str) -> list[tuple[str, str]]:
        """``(database id, title)`` directly inside a page."""
        return [
            (compact_id(block.get("id")), str((block.get("child_database") or {}).get("title") or "").strip())
            for block in self.children(page_id)
            if block.get("type") == "child_database"
        ]

    def child_pages(self, page_id: str) -> list[tuple[str, str]]:
        return [
            (compact_id(block.get("id")), str((block.get("child_page") or {}).get("title") or "").strip())
            for block in self.children(page_id)
            if block.get("type") == "child_page"
        ]

    def page(self, page_id: str) -> dict[str, Any]:
        return self.call(self.client.pages.retrieve, page_id=page_id)

    def create_page(self, parent_page_id: str, title: str) -> str:
        page = self.call(
            self.client.pages.create,
            parent={"type": "page_id", "page_id": parent_page_id},
            properties={"title": {"title": _text(title)}},
        )
        return compact_id(page["id"])

    def database(self, database_id: str) -> dict[str, Any]:
        """Title, description, data source and columns of one database."""
        database = self.call(self.client.databases.retrieve, database_id=database_id)
        sources = database.get("data_sources") or []
        if not sources:
            raise RuntimeError(f"Notion database {database_id} has no data source.")
        source_id = str(sources[0]["id"])
        source = self.call(self.client.data_sources.retrieve, data_source_id=source_id)
        return {
            "id": compact_id(database.get("id") or database_id),
            "title": plain_text(database.get("title")),
            "description": plain_text(database.get("description")),
            "parent_page_id": compact_id((database.get("parent") or {}).get("page_id")),
            "in_trash": bool(database.get("in_trash") or database.get("archived")),
            "data_source_id": source_id,
            "columns": column_types(source.get("properties")),
        }

    def create_database(
        self,
        parent_page_id: str,
        title: str,
        description: str,
        columns: dict[str, Any],
    ) -> dict[str, Any]:
        database = self.call(
            self.client.databases.create,
            parent={"type": "page_id", "page_id": parent_page_id},
            title=_text(title),
            description=_text(description),
            initial_data_source={"properties": {name: column_schema(spec) for name, spec in columns.items()}},
        )
        sources = database.get("data_sources") or []
        if not sources:
            raise RuntimeError("Notion created a database without a data source.")
        return {
            "id": compact_id(database["id"]),
            "title": title,
            "description": description,
            "parent_page_id": compact_id(parent_page_id),
            "in_trash": False,
            "data_source_id": str(sources[0]["id"]),
            "columns": {name: column_type_of(spec) for name, spec in columns.items()},
        }

    def rename_database(self, database_id: str, title: str) -> None:
        self.call(self.client.databases.update, database_id=database_id, title=_text(title))

    def add_columns(self, database: dict[str, Any], wanted: dict[str, Any]) -> list[str]:
        """Add the wanted columns a database lacks; existing ones stay as they are."""
        missing = {name: spec for name, spec in wanted.items() if name not in database["columns"]}
        # A database has exactly one title column, whatever it is called.
        if "title" in database["columns"].values():
            missing = {name: spec for name, spec in missing.items() if column_type_of(spec) != "title"}
        if missing:
            self.call(
                self.client.data_sources.update,
                data_source_id=database["data_source_id"],
                properties={name: column_schema(spec) for name, spec in missing.items()},
            )
            database["columns"].update({name: column_type_of(spec) for name, spec in missing.items()})
        return sorted(missing)

    # ── Rows ──

    def find_rows(self, database: dict[str, Any], column: str, value: str, *, prefix: bool = False) -> list[dict[str, Any]]:
        column_type = database["columns"].get(column, "rich_text")
        condition = {"starts_with" if prefix else "equals": value}
        rows: list[dict[str, Any]] = []
        cursor = None
        while True:
            kwargs: dict[str, Any] = {
                "data_source_id": database["data_source_id"],
                "filter": {"property": column, column_type: condition},
                "page_size": 100,
            }
            if cursor:
                kwargs["start_cursor"] = cursor
            response = self.call(self.client.data_sources.query, **kwargs)
            rows.extend(response.get("results") or [])
            cursor = response.get("next_cursor") if response.get("has_more") else None
            if not cursor:
                return rows

    def write_row(self, database: dict[str, Any], page_id: str | None, cells: dict[str, Any]) -> str:
        """Create a row, or update the given one; only columns the database has are written."""
        properties = {
            name: property_value(database["columns"][name], value)
            for name, value in cells.items()
            if database["columns"].get(name) in WRITABLE_TYPES
        }
        if page_id:
            self.call(self.client.pages.update, page_id=page_id, properties=properties)
            return page_id
        page = self.call(
            self.client.pages.create,
            parent={"type": "data_source_id", "data_source_id": database["data_source_id"]},
            properties=properties,
        )
        return compact_id(page["id"])


# ── Values ──────────────────────────────────────────────────────────────────

def column_schema(spec: Any) -> dict[str, Any]:
    """A column type ("number") or a full schema ({"relation": ...}) as Notion expects it."""
    if isinstance(spec, dict):
        return spec
    if spec == "number":
        return {"number": {"format": "number"}}
    return {spec: {}}


def column_type_of(spec: Any) -> str:
    return next(iter(spec)) if isinstance(spec, dict) else str(spec)


def column_types(properties: Any) -> dict[str, str]:
    return {
        str(name): str((spec or {}).get("type") or next(iter(spec or {"rich_text": {}})))
        for name, spec in (properties or {}).items()
    }


def property_value(column_type: str, value: Any) -> dict[str, Any]:
    """A Python value as a Notion property of ``column_type``; None clears it."""
    if column_type in {"title", "rich_text"}:
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        return {column_type: _text(value) if value not in (None, "") else []}
    if column_type == "number":
        number = _number(value)
        return {"number": number}
    if column_type == "select":
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        return {"select": {"name": option_name(value)} if value not in (None, "") else None}
    if column_type == "multi_select":
        items = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        names = list(dict.fromkeys(option_name(item) for item in items if item not in (None, "")))
        return {"multi_select": [{"name": name} for name in names]}
    if column_type == "date":
        return {"date": {"start": str(value)} if value else None}
    if column_type == "relation":
        return {"relation": [{"id": str(item)} for item in (value or [])]}
    raise ValueError(f"Cannot write a Notion {column_type} column.")


def option_name(value: Any) -> str:
    """Notion select options may not contain commas and are at most 100 characters."""
    return " ".join(str(value).replace(",", " ").split())[:OPTION_LIMIT] or "-"


def plain_text(items: Any) -> str:
    return "".join(str((item or {}).get("plain_text") or ((item or {}).get("text") or {}).get("content") or "") for item in items or [])


def compact_id(value: Any) -> str:
    return notion_id_from_input(value) or str(value or "").replace("-", "").strip()


def _text(value: Any) -> list[dict[str, Any]]:
    text = str(value)
    chunks = [text[start:start + TEXT_LIMIT] for start in range(0, len(text), TEXT_LIMIT)] or [""]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks[:100]]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _error_code(error: Exception) -> str:
    return str(getattr(error, "code", "") or "").lower().rsplit(".", 1)[-1].replace("ratelimited", "rate_limited")


def _retry_after(error: Exception, attempt: int) -> float:
    headers = getattr(error, "headers", None) or {}
    try:
        seconds = float(headers.get("retry-after") or headers.get("Retry-After") or 0)
    except (TypeError, ValueError, AttributeError):
        seconds = 0.0
    return min(MAX_RETRY_AFTER_SECONDS, seconds or 2.0 ** attempt)
