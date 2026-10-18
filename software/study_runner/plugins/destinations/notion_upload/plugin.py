from __future__ import annotations

from typing import Any

from study_runner.plugin_framework.adapter_utils import config_section
from study_runner.contracts.plugin_api import PluginContext, Plugin


def _initialize(context: PluginContext) -> None:
    config = config_section(context, "notion")
    from . import adapter

    adapter.initialize(
        enabled=config.get("enabled", True) is not False,
        api_key=context.secret("notion"),
        timeout_seconds=config.get("timeout_seconds", 10),
        data_dir=context.data_dir,
    )


def _status(context: PluginContext) -> dict[str, Any]:
    config = config_section(context, "notion")
    from . import adapter

    status = adapter.get_status()
    has_key = bool(context.secret("notion"))
    enabled = config.get("enabled", True) is not False
    if not enabled:
        status_value = "disabled"
    elif status.get("connected"):
        status_value = "connected"
    elif has_key:
        status_value = "waiting"
    else:
        status_value = "missing_key"

    return {
        **status,
        "status": status_value,
        "runtime_enabled": bool(status.get("connected")),
        "api_key_configured": has_key,
        "device_label": "Notion upload",
        "last_message": _message(enabled, has_key, bool(status.get("connected"))),
    }


def _message(enabled: bool, has_key: bool, connected: bool) -> str:
    if not enabled:
        return "Notion upload is disabled."
    if not has_key:
        return "Notion upload is enabled but no backend-local API key is stored."
    if not connected:
        return "Notion API key is configured; client is not connected yet."
    return "Notion client is ready."


def _publish(context: PluginContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one queued publication through the plugin boundary."""
    from . import adapter

    config_data = payload.get("config_data") or {}
    # The key is usually stored per study (Study settings > Notion); the
    # machine-level key is only the fallback.
    study_id = str(config_data.get("study_id") or "").strip()
    return adapter.upload_study_result(
        result_payload=payload.get("result_payload") or {},
        saved_output=payload.get("saved_output") or {},
        config_data=config_data,
        api_key=context.secret("notion", study_id),
    )


def _run_admin_action(
    context: PluginContext,
    action_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    from . import adapter

    # An empty/omitted key means "use whatever is already stored" - the
    # operator is testing or browsing before saving what they just typed.
    study_id = str(payload.get("study_id") or "").strip()
    api_key = str(payload.get("api_key") or "").strip() or context.secret("notion", study_id)

    if action_key == "test_connection":
        # The study settings panel never asks for a timeout: it is a machine
        # setting, so an omitted value means "the one this computer uses".
        machine_timeout = config_section(context, "notion").get("timeout_seconds") or 10
        return adapter.test_connection(
            api_key=api_key,
            timeout_seconds=int(payload.get("timeout_seconds") or machine_timeout),
            parent_page_id=str(payload.get("parent_page_id") or ""),
            database_id=str(payload.get("database_id") or ""),
        )
    if action_key == "describe_layout_sources":
        return adapter.describe_layout_sources(
            data_dir=context.data_dir,
            config_data_json=str(payload.get("config_data_json") or "{}"),
            session_path=str(payload.get("session_path") or ""),
            study_id=study_id,
        )
    if action_key == "check_structure":
        return adapter.check_structure(
            api_key=api_key,
            layout_json=str(payload.get("layout_json") or "{}"),
            config_data_json=str(payload.get("config_data_json") or "{}"),
            settings_json=str(payload.get("settings_json") or "{}"),
            apply=payload.get("apply") is True,
        )
    if action_key == "preview_layout":
        return adapter.preview_layout(
            data_dir=context.data_dir,
            layout_json=str(payload.get("layout_json") or "{}"),
            config_data_json=str(payload.get("config_data_json") or "{}"),
            session_path=str(payload.get("session_path") or ""),
            study_id=study_id,
        )
    raise ValueError(f"Unknown Notion admin action: {action_key}")


def _validate_setting(field_name: str, encoded_value: str) -> None:
    """`Plugin.validate_study_setting`: the shape of this plugin's own fields.

    Called by `_validate_plugin_study_settings` for a field that declares a
    format (the parent page, as typed) and for the "object" field (the
    layout, JSON-encoded) - see runtime_core/studies/validation.py.
    """
    if field_name == "parent_page_id":
        from .notion_ids import notion_id_from_input

        if encoded_value.strip() and not notion_id_from_input(encoded_value):
            raise ValueError(
                "is not a Notion page link or ID. In Notion, open the page, choose "
                "••• > Copy link, and paste the link here."
            )
        return
    if field_name != "notion_layout":
        return
    import json

    from .layout import LayoutError, validate_layout

    try:
        value = json.loads(encoded_value)
    except json.JSONDecodeError as error:
        raise ValueError(f"must be valid JSON: {error}") from error
    try:
        validate_layout(value)
    except LayoutError as error:
        raise ValueError(str(error)) from error


PLUGIN = Plugin(
    key="notion",
    label="Notion upload",
    category="storage",
    config_key="notion",
    initialize=_initialize,
    get_status=_status,
    publish_destination=_publish,
    run_admin_action=_run_admin_action,
    validate_study_setting=_validate_setting,
)
