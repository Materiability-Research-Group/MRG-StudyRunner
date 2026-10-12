"""Relations, summaries and Notion views stay idempotent across retries."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from study_runner.plugins.destinations.notion_upload import linked_targets


def _mapping():
    return {"preset": "custom", "targets": [
        {"id": "people", "title": "People", "row_level": "participant", "database_id": "db-people",
         "columns": {"Name": {"type": "rich_text", "source": "session.participant_id"}},
         "aggregates": [
             {"name": "Score count", "target_id": "cards", "column": "Score", "function": "count"},
             {"name": "Mean score", "target_id": "cards", "column": "Score", "function": "mean"},
             {"name": "Minimum score", "target_id": "cards", "column": "Score", "function": "min"},
             {"name": "Maximum score", "target_id": "cards", "column": "Score", "function": "max"},
             {"name": "Latest score", "target_id": "cards", "column": "Score", "function": "last"},
         ]},
        {"id": "cards", "title": "Cards", "row_level": "card", "database_id": "db-cards",
         "columns": {"Score": {"type": "number", "source": "card.answer"}},
         "relations": [{"target_id": "people", "property_name": "Participant", "linked_view": True}],
         "comparison_views": True},
    ]}


def test_prepare_schema_creates_missing_properties_once():
    props = {"source-people": {}, "source-cards": {}}

    def update(data_source_id, properties):
        props[data_source_id].update({name: {"type": next(iter(spec)), **spec} for name, spec in properties.items()})

    client = SimpleNamespace(
        databases=SimpleNamespace(retrieve=lambda database_id: {"data_sources": [{"id": f"source-{database_id[3:]}"}]}),
        data_sources=SimpleNamespace(retrieve=lambda data_source_id: {"properties": props[data_source_id]}, update=Mock(side_effect=update)),
    )
    mapping = _mapping()
    assert {item["property"] for item in linked_targets.prepare_schema(client, mapping, apply=False)} >= {
        "Participant", "Mean score", "StudyRunner Card ID", "StudyRunner Card Revision",
    }
    linked_targets.prepare_schema(client, mapping, apply=True)
    assert linked_targets.prepare_schema(client, mapping, apply=False) == []
    assert client.data_sources.update.call_count == 2


def test_publish_links_recalculates_across_sessions_and_keeps_views_unique():
    mapping = _mapping()
    rows = [
        {"id": "card-old", "properties": {"Score": {"type": "number", "number": 2},
         linked_targets.SESSION_START_PROPERTY: {"type": "date", "date": {"start": "2026-01-01"}}}},
        {"id": "card-new", "properties": {"Score": {"type": "number", "number": 4},
         linked_targets.SESSION_START_PROPERTY: {"type": "date", "date": {"start": "2026-02-01"}}}},
        {"id": "card-empty", "properties": {"Score": {"type": "number", "number": None},
         linked_targets.SESSION_START_PROPERTY: {"type": "date", "date": {"start": "2026-03-01"}}}},
    ]
    databases = {
        "db-people": {"data_sources": [{"id": "source-people"}]},
        "db-cards": {"data_sources": [{"id": "source-cards"}]},
    }
    client = SimpleNamespace(
        databases=SimpleNamespace(retrieve=lambda database_id: databases[database_id]),
        data_sources=SimpleNamespace(query=Mock(return_value={"results": rows, "has_more": False})),
        pages=SimpleNamespace(update=Mock()),
    )
    views_by_id = {}

    def request(path, method, query=None, body=None):
        if path == "/views" and method == "GET":
            return {"results": [{"id": key} for key in views_by_id], "has_more": False}
        if path == "/views" and method == "POST":
            view_id = f"view-{len(views_by_id)}"
            parent = {"database_id": body.get("database_id", "linked-db")}
            views_by_id[view_id] = {**body, "id": view_id, "parent": parent}
            return views_by_id[view_id]
        return views_by_id[path.split("/")[-1]]

    views = SimpleNamespace(request=request)
    context = {"session": {"participant_id": "p1", "session_id": "s2"},
               "cards": [{"id": "stable-card", "index": 0, "prompt": "Rating"}]}
    ids = {("people", "p1"): "participant-page", ("cards", "s2:0"): "card-new"}
    value = lambda kind, item: {kind: item}
    # Linked participant views need the parent database's location.
    databases["linked-db"] = {"parent": {"page_id": "participant-page"}}
    linked_targets.publish_links(client, views, mapping, ids, context, value)
    linked_targets.publish_links(client, views, mapping, ids, context, value)
    assert len(views_by_id) == 2
    aggregate_updates = [call.kwargs["properties"] for call in client.pages.update.call_args_list
                         if "Mean score" in call.kwargs["properties"]]
    assert aggregate_updates[-1] == {
        "Score count": {"number": 2}, "Mean score": {"number": 3.0},
        "Minimum score": {"number": 2.0}, "Maximum score": {"number": 4.0},
        "Latest score": {"number": 4},
    }
