"""
Notion adapter - publishes anonymized study results to Notion.

What goes where is the study's layout (``layout.py``): a Participants table,
optionally each participant's own answers table, and optionally one table
per card that compares every participant's data of that card. This module
holds the Notion client, the connection test, the upload entry point and
the configurator's actions; ``structure.py`` and ``publisher.py`` do the
writing.

Network failures are returned to the central upload-job service, which owns the
persistent retry journal for every destination.

Requires: notion-client >= 3 (auto-install optional)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from study_runner.shared.dependency_utils import ensure_requirements

from .layout import LayoutError, effective_layout, included_cards, listed_cards, validate_layout
from .notion_api import NotionApi, compact_id
from .session_rows import (
    SESSION_SUMMARY_COLUMNS,
    PARTICIPANT_FIELD_COLUMNS,
    answers_cells,
    card_rows,
    comparison_cells,
    sensor_values,
    session_facts,
)
from .structure import ensure_card_databases, ensure_comparison_page, ensure_participants, parent_page


# Clients are cached by a hash of their API key, not by study id: two studies
# sharing one key share one client, renaming a study cannot orphan anything,
# and no second plaintext copy of the key sits around as a dict key.
_clients: dict[str, Any] = {}
_config: dict[str, Any] = {}


def _client_cache_key(api_key: str) -> str:
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _requirements_ready() -> bool:
    return ensure_requirements([("notion_client", "notion-client")], auto_install=True, label="NOTION")


def get_client(api_key: str) -> Any:
    """Return a cached Notion client for this key, building it on first use.

    Returns None when the integration is off machine-side, when no key was
    given, or when the client library is unavailable - callers treat that as
    "not ready".
    """
    if not _config.get("enabled") or not api_key:
        return None

    cache_key = _client_cache_key(api_key)
    if cache_key in _clients:
        return _clients[cache_key]

    if not _requirements_ready():
        return None

    try:
        from notion_client import Client

        client = Client(auth=api_key, timeout_ms=_config.get("timeout_seconds", 10) * 1000)
    except Exception as error:
        print(f"[NOTION] Client initialization failed: {error}")
        return None

    _clients[cache_key] = client
    return client


def initialize(
    *,
    enabled: bool,
    api_key: str,
    timeout_seconds: int,
    data_dir: Path,
) -> None:
    global _config

    # Re-initializing drops every cached client, which is what the
    # hardware-config save path relies on to pick up a changed key.
    _clients.clear()
    _config = {
        "enabled": bool(enabled),
        "api_key": api_key or "",
        "timeout_seconds": max(1, int(timeout_seconds)),
    }

    if not enabled:
        print("[NOTION] Disabled.")
        return

    if not api_key:
        print("[NOTION] No API key configured - upload disabled.")
        return

    if not _requirements_ready():
        return

    if get_client(api_key) is not None:
        print("[NOTION] Client ready.")


def upload_study_result(
    result_payload: dict[str, Any],
    saved_output: dict[str, Any],
    config_data: dict[str, Any] | None = None,
    api_key: str = "",
) -> dict[str, Any]:
    """Upload one completed study session into the study's layout.

    Retries are the persistent upload-job queue's responsibility
    (`upload_jobs_service.py`): it calls this again with the same payload,
    and every write here is an update-or-create, so that is always safe.

    This runs inside the plugin's own `driver.py` subprocess, which may not
    import `backend`. Ids of created databases are therefore reported in
    `study_config_updates`; the host (`upload_runtime.py`) saves them on the
    study after this call returns.
    """
    config_data = config_data or {}
    selection = ((config_data.get("study_settings") or {}).get("plugins") or {}).get("notion") or {}
    if not selection.get("enabled"):
        # Nothing to publish is not a failure: never retry or report it.
        return {"ok": True, "skipped": True, "reason": "Notion is disabled for this study."}
    settings = selection.get("settings") if isinstance(selection.get("settings"), dict) else {}

    summary_error = _card_summary_error(saved_output)
    if summary_error:
        return {"ok": False, "error": summary_error}
    try:
        validate_layout(settings.get("notion_layout") or {})
    except LayoutError as error:
        return {"ok": False, "permanent": True, "error": f"The Notion layout of this study is invalid: {error}"}
    layout = effective_layout(settings.get("notion_layout"), config_data)

    key = str(api_key or _config.get("api_key", "") or "").strip()
    if not key:
        return {
            "ok": False,
            "permanent": True,
            "error": "No Notion API key is stored for this study. Add it in the study's Notion settings.",
        }
    client = get_client(key)
    if client is None:
        if not _config.get("enabled"):
            return {
                "ok": False,
                "permanent": True,
                "error": "Notion upload is not ready: it is switched off on this computer. "
                "Turn it on under Settings > This computer > Notion.",
            }
        return {"ok": False, "error": "Notion upload is not ready: the Notion client could not be started."}

    from .publisher import publish_session

    updates: dict[str, str] = {}
    api = NotionApi(client)
    try:
        outcome = publish_session(
            api,
            settings=settings,
            layout=layout,
            config_data=config_data,
            result_payload=result_payload,
            card_summary=saved_output.get("card_summary") or {},
            updates=updates,
        )
    except Exception as error:
        print(f"[NOTION] Upload failed after {api.calls} calls: {error}")
        permanent, message = classify_notion_error(error)
        result: dict[str, Any] = {"ok": False, "error": message}
        if permanent:
            result["permanent"] = True
        if updates:
            # A database created before the failure is found again next time.
            result["study_config_updates"] = updates
        return result
    written = outcome["written"]
    print(
        f"[NOTION] Session uploaded: {written['answers']} answer rows, "
        f"{written['comparison']} comparison rows, {outcome['calls']} calls."
    )
    result = {"ok": True, "session_id": str(result_payload.get("session_id") or ""), "written": written}
    if updates:
        result["study_config_updates"] = updates
    return result


# Notion answers these when retrying cannot help: fix the key, share the page,
# or correct an ID. Anything else (rate limits, 5xx, network) is retried.
_PERMANENT_NOTION_CODES = {
    "unauthorized": "Notion rejected the API key. Check that it is the integration's secret and still valid.",
    # Notion sends restricted_resource (HTTP 403) both for a page that is not
    # shared with the integration and for an integration that lacks a
    # capability - e.g. it may insert rows but not update them. Name both
    # fixes and keep Notion's own text, which says which one it is.
    "restricted_resource": "Notion refused access ({detail}). Either the page is not shared with the "
    "integration (in Notion: open the page, ••• > Connections, add it), or the integration lacks a "
    "capability (Notion > Settings > Connections > your integration > Capabilities: allow Read, "
    "Update and Insert content).",
    "object_not_found": "Notion cannot find the page or database ({detail}). Check the ID and that the page "
    "is shared with your integration (••• > Connections).",
    "validation_error": "Notion rejected the request: {detail}",
    "invalid_request_url": "Notion rejected the request: {detail}",
}


def classify_notion_error(error: Exception) -> tuple[bool, str]:
    """Return (permanent, operator-facing message) for an upload failure."""
    code = str(getattr(error, "code", "") or "").lower()
    code = code.rsplit(".", 1)[-1]  # an APIErrorCode enum prints as APIErrorCode.Unauthorized
    for known, message in _PERMANENT_NOTION_CODES.items():
        if code == known or code == known.replace("_", ""):
            return True, message.format(detail=str(error))
    text = str(error)
    if "parent_page_id is required" in text:
        return True, (
            "No Notion page is set for this study. Paste the link of the Notion page the results "
            "should go to in the study's Notion settings."
        )
    return False, text


def test_connection(
    *,
    api_key: str,
    timeout_seconds: int = 10,
    parent_page_id: str = "",
    database_id: str = "",
) -> dict[str, Any]:
    """
    Test Notion connectivity with the given credentials (without saving anything).
    Returns a list of named checks so the UI can show granular status.
    """
    if not _requirements_ready():
        return {"ok": False, "checks": [{"name": "Package", "ok": False, "message": "The notion-client package could not be installed."}]}

    from notion_client import Client, APIErrorCode, APIResponseError

    checks: list[dict[str, Any]] = []

    try:
        client = Client(auth=api_key.strip(), timeout_ms=timeout_seconds * 1000)
        me = client.users.me()
        bot_name = me.get("name") or me.get("bot", {}).get("owner", {}).get("user", {}).get("name") or "Integration"
        checks.append({"name": "API Key", "ok": True, "message": f'Connected as "{bot_name}"'})
    except APIResponseError as error:
        msg = (
            "Notion rejected the API key. Check that it is the integration's secret and still valid."
            if error.code == APIErrorCode.Unauthorized
            else str(error)
        )
        checks.append({"name": "API Key", "ok": False, "message": msg})
        return {"ok": False, "checks": checks}
    except Exception as error:
        checks.append({"name": "API Key", "ok": False, "message": f"Connection failed: {error}"})
        return {"ok": False, "checks": checks}

    # The most common real problem: the page is not shared with the integration.
    for label, raw_id, retrieve in (
        ("Database", database_id, lambda value: client.databases.retrieve(database_id=value)),
        ("Parent page", parent_page_id, lambda value: client.pages.retrieve(page_id=value)),
    ):
        target = compact_id(str(raw_id or "").strip())
        if not target:
            continue
        try:
            retrieve(target)
            checks.append({"name": label, "ok": True, "message": f"{label} is reachable for the integration."})
        except Exception as error:
            _permanent, message = classify_notion_error(error)
            checks.append({"name": label, "ok": False, "message": message})
        break

    overall_ok = all(c["ok"] is not False for c in checks)
    return {"ok": overall_ok, "checks": checks}


# Same reason as in nextcloud_service: pytest would collect this as a test if a
# test module ever imported it by name.
test_connection.__test__ = False


def get_status() -> dict[str, Any]:
    return {
        "enabled": bool(_config.get("enabled")),
        "connected": bool(_clients),
        "queue_size": 0,
    }


# ── Configurator actions ──────────────────────────────────────────────────
# The layout configurator's own calls (plugin.py#_run_admin_action and
# ui/configurator.js). None of these change the study; the configurator
# saves the layout through the normal study-settings save.

def describe_layout_sources(*, data_dir: Any, config_data_json: str, session_path: str = "", study_id: str = "") -> dict[str, Any]:
    """The cards, participant columns and values this study can send."""
    try:
        config_data = _json_object(config_data_json, "config_data_json")
        card_summary: dict[str, Any] = {}
        if session_path:
            result_payload, card_summary = _read_session_summary(data_dir, session_path)
            if study_id and result_payload.get("study_id") != study_id:
                raise ValueError("This session does not belong to the current study.")
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    from .layout import stored_participant_fields

    stored = set(stored_participant_fields(config_data))
    participant_columns = [
        {"key": key, "label": name, "available": True} for key, (name, _type) in SESSION_SUMMARY_COLUMNS.items()
    ] + [
        {"key": key, "label": name, "available": key in stored} for key, (name, _type) in PARTICIPANT_FIELD_COLUMNS.items()
    ]
    cards = [
        {key: card[key] for key in ("card_id", "index", "type", "label", "default_included")}
        for card in listed_cards(config_data)
    ]
    values = [{"source": "answer", "label": "Answer"}, {"source": "duration_seconds", "label": "Duration (s)"}]
    values += _declared_sensor_values(config_data) + sensor_values(card_summary)
    unique = list({item["source"]: item for item in values}.values())
    return {"ok": True, "cards": cards, "participant_columns": participant_columns, "values": unique}


def check_structure(
    *,
    api_key: str,
    layout_json: str,
    config_data_json: str,
    settings_json: str,
    apply: bool,
) -> dict[str, Any]:
    """What the layout needs in Notion; with ``apply`` create it now.

    Answers tables are created per participant at upload, so they are not
    part of this check.
    """
    try:
        layout_value = _json_object(layout_json, "layout_json")
        validate_layout(layout_value)
        config_data = _json_object(config_data_json, "config_data_json")
        settings = _json_object(settings_json, "settings_json")
        if not (compact_id(settings.get("parent_page_id")) or compact_id(settings.get("database_id"))):
            raise ValueError("Paste the Notion page link first.")
        client = get_client(api_key)
        if client is None:
            raise ValueError("Notion is not ready: check the API key and that Notion is on for this computer.")
    except (ValueError, LayoutError) as error:
        return {"ok": False, "error": str(error)}
    layout = effective_layout(layout_value, config_data)
    api = NotionApi(client)
    updates: dict[str, str] = {}
    report: list[dict[str, Any]] = []
    try:
        parent_page_id = parent_page(api, settings)
        participants = ensure_participants(api, parent_page_id, settings, layout, updates, report, apply=apply)
        if layout["card_comparison"]:
            page_id = ensure_comparison_page(api, parent_page_id, settings, updates, report, apply=apply)
            ensure_card_databases(
                api, page_id, included_cards(layout, config_data), layout, participants, report, apply=apply,
            )
    except Exception as error:
        permanent, message = classify_notion_error(error)
        return {"ok": False, "error": message, "permanent": permanent, "settings_updates": updates}
    return {"ok": True, "applied": bool(apply), "tables": report, "settings_updates": updates}


def preview_layout(
    *,
    data_dir: Any,
    layout_json: str,
    config_data_json: str,
    session_path: str,
    study_id: str = "",
) -> dict[str, Any]:
    """What one finished session would write - read from disk, no Notion call."""
    try:
        layout_value = _json_object(layout_json, "layout_json")
        validate_layout(layout_value)
        config_data = _json_object(config_data_json, "config_data_json")
        result_payload, card_summary = _read_session_summary(data_dir, session_path)
        if study_id and result_payload.get("study_id") != study_id:
            raise ValueError("This session does not belong to the current study.")
        facts = session_facts(result_payload)
    except (ValueError, LayoutError) as error:
        return {"ok": False, "error": str(error)}
    layout = effective_layout(layout_value, config_data)
    rows = card_rows(layout, config_data, result_payload, card_summary)
    return {
        "ok": True,
        "participant_id": facts["participant_id"],
        "rows": [
            {
                "label": row["card"]["label"],
                "status": row["status"],
                "answers": answers_cells(layout, facts, row) if layout["answers_per_participant"] else None,
                "comparison": comparison_cells(layout, facts, row) if layout["card_comparison"] else None,
            }
            for row in rows
        ],
    }


def _declared_sensor_values(config_data: dict[str, Any]) -> list[dict[str, str]]:
    """Sensor values the study's enabled sensors declare, before any recording."""
    settings = config_data.get("study_settings") or {}
    if settings.get("sensors_enabled") is False:
        return []
    selected = settings.get("plugins") or {}
    values = []
    for manifest in config_data.get("plugin_manifests") or []:
        if not isinstance(manifest, dict):
            continue
        plugin_key = str(manifest.get("plugin_key") or "")
        if not (selected.get(plugin_key) or {}).get("enabled") or "interval_summary" not in (manifest.get("capabilities") or []):
            continue
        for stream in manifest.get("streams") or []:
            source_id = str((stream or {}).get("source_id") or "")
            if not source_id:
                continue
            key = f"{plugin_key}_{source_id.rsplit('.', 1)[-1]}"
            numeric = str(stream.get("channel_format") or "").lower() not in {"string", "str"}
            stats = ("mean", "min", "max", "stddev", "coverage", "max_gap_seconds") if numeric else ("mode",)
            for channel in stream.get("channels") or []:
                if isinstance(channel, str):
                    values += [
                        {"source": f"stream.{key}.channel.{channel}.{stat}", "label": f"{key} · {channel} · {stat}"}
                        for stat in stats
                    ]
    return values


def _json_object(text: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} must be valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _resolve_session_folder(data_dir: Any, session_path: str) -> Path:
    """A plugin-local copy of the host's own session-path safety check.

    Plugins run in their own process and must never import `runtime_core`
    (see tests/test_import_boundaries.py); this stays an exact match of
    `runtime_core.settings.folder_open_service.resolve_session_folder`'s
    traversal/layout rules rather than relaxing them.
    """
    normalized = str(session_path or "").strip().replace("\\", "/")
    relative = Path(normalized)
    parts = relative.parts
    if (
        not normalized
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) != 3
        or any(part.startswith("_") for part in parts)
        or parts[0] in {"runtime", "upload_jobs"}
    ):
        raise ValueError("A valid finalization session path is required.")
    root = Path(data_dir).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_dir() or not any(
        (target / marker).is_file()
        for marker in ("meta/session-identity.json", "COMPLETE.json", "ATTENTION_REQUIRED.json", "WITHDRAWN.json")
    ):
        raise ValueError("The session folder was not found on this computer.")
    return target


def _read_session_summary(data_dir: Any, session_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(result_payload, card_summary) for one finished session on disk."""
    root = _resolve_session_folder(data_dir, session_path)
    result_path = root / "answers" / "result.json"
    if not result_path.is_file():
        raise ValueError("This session has no finalized result.json yet.")
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    summary_path = root / "answers" / "card-summary.json"
    card_summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    return result_payload, card_summary


def _card_summary_error(saved_output: dict[str, Any]) -> str | None:
    """A finalized session always has its card summary; refuse a broken one."""
    if not (saved_output.get("card_summary_file") or saved_output.get("session_relative_path")):
        return None
    summary = saved_output.get("card_summary")
    if not isinstance(summary, dict):
        return "Notion upload requires the finalized card-summary.json."
    if summary and summary.get("schema") != "study-runner/card-summary/v1":
        return "Notion upload received an unsupported card-summary.json schema."
    if summary and not isinstance(summary.get("cards"), list):
        return "Notion upload requires a valid cards array from card-summary.json."
    return None
