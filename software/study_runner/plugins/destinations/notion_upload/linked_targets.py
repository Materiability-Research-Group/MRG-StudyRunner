"""Relations, participant aggregates, and views for mapped Notion targets.

The legacy two-database publisher stays in adapter.py. This module only runs
when a configurable mapping requests links or comparison views.
"""
from __future__ import annotations

from typing import Any

CARD_ID_PROPERTY = "StudyRunner Card ID"
CARD_REVISION_PROPERTY = "StudyRunner Card Revision"
SESSION_START_PROPERTY = "StudyRunner Session Start"


def relation_name(relation: dict[str, Any], target: dict[str, Any]) -> str:
    return str(relation.get("property_name") or target["title"]).strip()


def required_schema(mapping: dict[str, Any], target: dict[str, Any], source_id_of) -> dict[str, Any]:
    """Properties this plugin needs in addition to operator-mapped columns."""
    by_id = {item["id"]: item for item in mapping.get("targets") or []}
    schema: dict[str, Any] = {}
    if target["row_level"] == "card" and (target.get("comparison_views") or target.get("relations")):
        schema.update({
            CARD_ID_PROPERTY: {"rich_text": {}},
            CARD_REVISION_PROPERTY: {"rich_text": {}},
            SESSION_START_PROPERTY: {"date": {}},
        })
    elif target["row_level"] == "session" and target.get("relations"):
        schema[SESSION_START_PROPERTY] = {"date": {}}
    for relation in target.get("relations") or []:
        other = by_id[relation["target_id"]]
        dual_property = {}
        if relation.get("reverse_property_name"):
            dual_property["synced_property_name"] = relation["reverse_property_name"]
        schema[relation_name(relation, other)] = {"relation": {
            "data_source_id": source_id_of(other["database_id"]),
            "type": "dual_property",
            "dual_property": dual_property,
        }}
    for aggregate in target.get("aggregates") or []:
        source = by_id[aggregate["target_id"]]["columns"][aggregate["column"]]
        kind = source["type"] if aggregate["function"] == "last" else "number"
        schema[aggregate["name"]] = {kind: {}}
    return schema


def prepare_schema(client: Any, mapping: dict[str, Any], *, apply: bool) -> list[dict[str, str]]:
    """Describe or apply only missing properties; refuse incompatible ones."""
    if not any(item.get("relations") or item.get("aggregates") or item.get("comparison_views") for item in mapping.get("targets") or []):
        return []
    if not hasattr(client, "data_sources"):
        raise ValueError("Relations and views require a Notion client with data-source support.")
    cache: dict[str, str] = {}

    def source_id_of(database_id: str) -> str:
        if database_id not in cache:
            sources = client.databases.retrieve(database_id=database_id).get("data_sources") or []
            if not sources:
                raise ValueError(f"Database {database_id} has no accessible data source.")
            cache[database_id] = str(sources[0]["id"])
        return cache[database_id]

    changes: list[dict[str, str]] = []
    for target in mapping.get("targets") or []:
        source_id = source_id_of(target["database_id"])
        existing = client.data_sources.retrieve(data_source_id=source_id).get("properties") or {}
        required = required_schema(mapping, target, source_id_of)
        missing: dict[str, Any] = {}
        for name, spec in required.items():
            kind = next(iter(spec))
            if name in existing:
                if existing[name].get("type") != kind:
                    raise ValueError(f"Notion column {name!r} on {target['title']!r} is not {kind}.")
                if kind == "relation":
                    expected = spec["relation"]["data_source_id"]
                    actual = (existing[name].get("relation") or {}).get("data_source_id")
                    if actual != expected:
                        raise ValueError(f"Notion relation {name!r} points to another database.")
            else:
                missing[name] = spec
                changes.append({"target_id": target["id"], "target": target["title"], "property": name, "type": kind})
        if apply and missing:
            client.data_sources.update(data_source_id=source_id, properties=missing)
    return changes


def view_client(api_key: str, timeout_seconds: int = 10) -> Any:
    """Keep the newer Views API version separate from ordinary page uploads."""
    from notion_client import Client

    return Client(auth=api_key, timeout_ms=timeout_seconds * 1000, notion_version="2026-03-11")


def _request_views(client: Any, method: str, path: str, *, query=None, body=None) -> dict[str, Any]:
    if hasattr(client, "views"):
        method_name = {"GET": "list", "POST": "create"}.get(method)
        if method_name:
            return getattr(client.views, method_name)(**(query if method == "GET" else body))
    return client.request(path, method, query=query, body=body)


def _find_view(client: Any, source_id: str, predicate) -> dict[str, Any] | None:
    cursor = None
    while True:
        query = {"data_source_id": source_id}
        if cursor:
            query["start_cursor"] = cursor
        listing = _request_views(client, "GET", "/views", query=query)
        for reference in listing.get("results") or []:
            view = client.request(f"/views/{reference['id']}", "GET")
            if predicate(view):
                return view
        cursor = listing.get("next_cursor") if listing.get("has_more") else None
        if not cursor:
            return None


def _source_id(client: Any, database_id: str) -> str:
    sources = client.databases.retrieve(database_id=database_id).get("data_sources") or []
    if not sources:
        raise ValueError(f"Database {database_id} has no accessible data source.")
    return str(sources[0]["id"])


def ensure_comparison_views(client: Any, views: Any, target: dict[str, Any], cards: list[dict[str, Any]]) -> None:
    if not target.get("comparison_views"):
        return
    source_id = _source_id(client, target["database_id"])
    for card in cards:
        card_id = str(card.get("id") or "")
        if not card_id:
            continue
        expected_filter = {"property": CARD_ID_PROPERTY, "rich_text": {"equals": card_id}}
        if _find_view(views, source_id, lambda view: view.get("parent", {}).get("database_id") == target["database_id"] and view.get("filter") == expected_filter):
            continue
        name = f"Card {int(card.get('index') or 0) + 1}: {str(card.get('prompt') or card_id)[:55]}"
        _request_views(views, "POST", "/views", body={
            "database_id": target["database_id"], "data_source_id": source_id,
            "name": name, "type": "table", "filter": expected_filter,
        })


def ensure_participant_view(client: Any, views: Any, card_target: dict[str, Any], relation_property: str, participant_page_id: str) -> None:
    source_id = _source_id(client, card_target["database_id"])
    expected_filter = {"property": relation_property, "relation": {"contains": participant_page_id}}

    def belongs_to_participant(view: dict[str, Any]) -> bool:
        if view.get("filter") != expected_filter:
            return False
        container_id = (view.get("parent") or {}).get("database_id")
        if not container_id:
            return False
        container = client.databases.retrieve(database_id=container_id)
        return (container.get("parent") or {}).get("page_id") == participant_page_id

    if _find_view(views, source_id, belongs_to_participant):
        return
    _request_views(views, "POST", "/views", body={
        "create_database": {"parent": {"type": "page_id", "page_id": participant_page_id}},
        "data_source_id": source_id, "name": "StudyRunner Cards", "type": "table",
        "filter": expected_filter,
    })


def _property_value(property_data: dict[str, Any]) -> Any:
    kind = property_data.get("type")
    if kind == "number":
        return property_data.get("number")
    if kind == "date":
        return (property_data.get("date") or {}).get("start")
    if kind in {"rich_text", "title"}:
        return "".join(item.get("plain_text") or (item.get("text") or {}).get("content") or "" for item in property_data.get(kind) or [])
    if kind == "select":
        return (property_data.get("select") or {}).get("name")
    if kind == "multi_select":
        return ", ".join(item.get("name") or "" for item in property_data.get("multi_select") or [])
    return None


def _related_rows(client: Any, source_id: str, property_name: str, page_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cursor = None
    while True:
        args = {"data_source_id": source_id, "filter": {"property": property_name, "relation": {"contains": page_id}}}
        if cursor:
            args["start_cursor"] = cursor
        response = client.data_sources.query(**args)
        rows.extend(response.get("results") or [])
        cursor = response.get("next_cursor") if response.get("has_more") else None
        if not cursor:
            return rows


def update_aggregates(client: Any, mapping: dict[str, Any], page_ids: dict[tuple[str, str], str], context: dict[str, Any], property_value_for) -> None:
    by_id = {item["id"]: item for item in mapping.get("targets") or []}
    participant_key = context["session"]["participant_id"]
    for participant in mapping.get("targets") or []:
        aggregates = participant.get("aggregates") or []
        if not aggregates:
            continue
        page_id = page_ids.get((participant["id"], participant_key))
        if not page_id:
            continue
        properties: dict[str, Any] = {}
        for aggregate in aggregates:
            source = by_id[aggregate["target_id"]]
            relation = next(item for item in source["relations"] if item["target_id"] == participant["id"])
            rows = _related_rows(client, _source_id(client, source["database_id"]), relation_name(relation, participant), page_id)
            valued = []
            for row in rows:
                props = row.get("properties") or {}
                value = _property_value(props.get(aggregate["column"]) or {})
                if value not in (None, ""):
                    started = _property_value(props.get(SESSION_START_PROPERTY) or {}) or ""
                    valued.append((started, str(row.get("id") or ""), value))
            function = aggregate["function"]
            numeric = [float(value) for _, _, value in valued if isinstance(value, (int, float)) and not isinstance(value, bool)]
            if function == "count":
                result = len(valued)
            elif function == "mean":
                result = sum(numeric) / len(numeric) if numeric else None
            elif function == "min":
                result = min(numeric) if numeric else None
            elif function == "max":
                result = max(numeric) if numeric else None
            else:
                result = max(valued, default=("", "", None))[2]
            kind = source["columns"][aggregate["column"]]["type"] if function == "last" else "number"
            properties[aggregate["name"]] = property_value_for(kind, result)
        client.pages.update(page_id=page_id, properties=properties)


def publish_links(client: Any, views: Any, mapping: dict[str, Any], page_ids: dict[tuple[str, str], str], context: dict[str, Any], property_value_for) -> None:
    """Finish an upsert after all target rows have stable Notion page IDs."""
    by_id = {item["id"]: item for item in mapping.get("targets") or []}
    session = context["session"]
    for target in mapping.get("targets") or []:
        for relation in target.get("relations") or []:
            other = by_id[relation["target_id"]]
            parent_key = session["participant_id"] if other["row_level"] == "participant" else session["session_id"]
            parent_page_id = page_ids.get((other["id"], parent_key))
            if not parent_page_id:
                raise ValueError(f"Relation target {other['title']!r} has no row for this session.")
            for (target_id, _key), page_id in page_ids.items():
                if target_id != target["id"]:
                    continue
                property_name = relation_name(relation, other)
                client.pages.update(page_id=page_id, properties={property_name: {"relation": [{"id": parent_page_id}]}})
            if relation.get("linked_view"):
                ensure_participant_view(client, views, target, relation_name(relation, other), parent_page_id)
    update_aggregates(client, mapping, page_ids, context, property_value_for)
    for target in mapping.get("targets") or []:
        if target.get("comparison_views"):
            ensure_comparison_views(client, views, target, context["cards"])
