"""An in-memory stand-in for the notion-client 3 calls the Notion plugin makes.

Pages, databases with one data source each, rows and child blocks, enough
to run a whole upload twice and look at what Notion would then contain.
Ids are plain counters; filters support ``equals`` and ``starts_with`` on
title and rich-text columns, which is all the plugin queries with.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import count
from typing import Any


class FakeNotionError(Exception):
    def __init__(self, code: str, message: str = "", headers: dict | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.headers = headers or {}


def plain(value: dict[str, Any]) -> str:
    items = value.get("title") if "title" in value else value.get("rich_text")
    return "".join(item["text"]["content"] for item in items or [])


class _Endpoint:
    def __init__(self, **methods: Any) -> None:
        self.__dict__.update(methods)


class FakeNotion:
    """Use as the Notion client: ``client.pages.create(...)`` and so on."""

    def __init__(self) -> None:
        self._ids = count(1)
        self.page_store: dict[str, dict[str, Any]] = {}
        self.database_store: dict[str, dict[str, Any]] = {}
        self.source_store: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []
        self.fail_next: list[Exception] = []
        self.blocks = _Endpoint(children=_Endpoint(list=self._list_children))
        self.pages = _Endpoint(create=self._create_page, retrieve=self._retrieve_page, update=self._update_page)
        self.databases = _Endpoint(
            create=self._create_database, retrieve=self._retrieve_database, update=self._update_database,
        )
        self.data_sources = _Endpoint(
            retrieve=self._retrieve_source, update=self._update_source, query=self._query_source,
        )

    def new_page(self, title: str = "Study results") -> str:
        page_id = self._id()
        self.page_store[page_id] = {"id": page_id, "title": title, "properties": {}, "parent": None, "in_trash": False}
        self.children[page_id] = []
        return page_id

    # ── helpers for assertions ──
    def rows(self, database_id: str) -> list[dict[str, Any]]:
        source_id = self.database_store[database_id]["source"]
        return [page for page in self.page_store.values() if page.get("parent") == source_id and not page["in_trash"]]

    def database_titled(self, title: str, *, inside: str | None = None) -> str:
        matches = [
            db_id for db_id, database in self.database_store.items()
            if database["title"] == title and (inside is None or database["parent"] == inside)
        ]
        assert len(matches) == 1, f"{len(matches)} databases titled {title!r}"
        return matches[0]

    def databases_inside(self, page_id: str) -> list[str]:
        return [db_id for db_id, database in self.database_store.items() if database["parent"] == page_id]

    def columns(self, database_id: str) -> dict[str, str]:
        properties = self.source_store[self.database_store[database_id]["source"]]["properties"]
        return {name: spec["type"] for name, spec in properties.items()}

    def add_database(self, parent_page_id: str, title: str, columns: dict[str, str], description: str = "") -> str:
        """A database the operator made by hand, e.g. one an older plugin created."""
        return self._create_database(
            parent={"type": "page_id", "page_id": parent_page_id},
            title=[{"text": {"content": title}}],
            description=[{"text": {"content": description}}],
            initial_data_source={"properties": {name: {kind: {}} for name, kind in columns.items()}},
        )["id"]

    # ── endpoints ──
    def _id(self) -> str:
        return f"{next(self._ids):032x}"

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _list_children(self, block_id: str, page_size: int = 100, start_cursor: str | None = None) -> dict[str, Any]:
        self._record("blocks.children.list")
        if block_id not in self.children:
            raise FakeNotionError("object_not_found")
        return {"results": deepcopy(self.children[block_id]), "has_more": False, "next_cursor": None}

    def _create_page(self, parent: dict[str, Any], properties: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._record("pages.create")
        page_id = self._id()
        if parent.get("type") == "page_id":
            title = plain(properties["title"])
            self.page_store[page_id] = {"id": page_id, "title": title, "properties": {}, "parent": parent["page_id"], "in_trash": False}
            self.children[parent["page_id"]].append({"id": page_id, "type": "child_page", "child_page": {"title": title}})
        else:
            self._check_columns(self.source_store[parent["data_source_id"]], properties)
            self.page_store[page_id] = {
                "id": page_id, "properties": deepcopy(properties), "parent": parent["data_source_id"], "in_trash": False,
            }
        self.children[page_id] = []
        return {"id": page_id}

    def _retrieve_page(self, page_id: str, **_: Any) -> dict[str, Any]:
        self._record("pages.retrieve")
        if page_id not in self.page_store:
            raise FakeNotionError("object_not_found")
        return deepcopy(self.page_store[page_id])

    def _update_page(self, page_id: str, properties: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._record("pages.update")
        page = self.page_store[page_id]
        self._check_columns(self.source_store[page["parent"]], properties)
        page["properties"].update(deepcopy(properties))
        return {"id": page_id}

    def _create_database(self, parent: dict[str, Any], title: list, initial_data_source: dict,
                         description: list | None = None, **_: Any) -> dict[str, Any]:
        self._record("databases.create")
        db_id, source_id = self._id(), self._id()
        name = "".join(item["text"]["content"] for item in title)
        properties = {key: {"type": next(iter(spec)), **spec} for key, spec in initial_data_source["properties"].items()}
        if sum(spec["type"] == "title" for spec in properties.values()) != 1:
            raise FakeNotionError("validation_error", "a database needs exactly one title column")
        self.source_store[source_id] = {"id": source_id, "properties": properties}
        self.database_store[db_id] = {
            "title": name,
            "description": "".join(item["text"]["content"] for item in description or []),
            "source": source_id,
            "parent": parent["page_id"],
            "in_trash": False,
        }
        self.children[parent["page_id"]].append({"id": db_id, "type": "child_database", "child_database": {"title": name}})
        return {"id": db_id, "data_sources": [{"id": source_id}]}

    def _retrieve_database(self, database_id: str) -> dict[str, Any]:
        self._record("databases.retrieve")
        if database_id not in self.database_store:
            raise FakeNotionError("object_not_found")
        database = self.database_store[database_id]
        return {
            "id": database_id,
            "title": [{"plain_text": database["title"]}],
            "description": [{"plain_text": database["description"]}],
            "data_sources": [{"id": database["source"]}],
            "parent": {"type": "page_id", "page_id": database["parent"]},
            "in_trash": database["in_trash"],
        }

    def _update_database(self, database_id: str, title: list, **_: Any) -> dict[str, Any]:
        self._record("databases.update")
        database = self.database_store[database_id]
        database["title"] = "".join(item["text"]["content"] for item in title)
        for block in self.children[database["parent"]]:
            if block["id"] == database_id:
                block["child_database"]["title"] = database["title"]
        return {"id": database_id}

    def _retrieve_source(self, data_source_id: str) -> dict[str, Any]:
        self._record("data_sources.retrieve")
        return deepcopy(self.source_store[data_source_id])

    def _update_source(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        self._record("data_sources.update")
        for key, spec in properties.items():
            self.source_store[data_source_id]["properties"][key] = {"type": next(iter(spec)), **spec}
        return {"id": data_source_id}

    def _query_source(self, data_source_id: str, filter: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._record("data_sources.query")
        column = filter["property"]
        condition = filter.get("title") or filter.get("rich_text") or {}
        results = []
        for page in self.page_store.values():
            if page.get("parent") != data_source_id or page["in_trash"]:
                continue
            text = plain(page["properties"].get(column) or {})
            if ("equals" in condition and text == condition["equals"]) or (
                "starts_with" in condition and text.startswith(condition["starts_with"])
            ):
                results.append(deepcopy(page))
        return {"results": results, "has_more": False, "next_cursor": None}

    @staticmethod
    def _check_columns(source: dict[str, Any], properties: dict[str, Any]) -> None:
        unknown = set(properties) - set(source["properties"])
        if unknown:
            raise FakeNotionError("validation_error", f"unknown columns: {sorted(unknown)}")
