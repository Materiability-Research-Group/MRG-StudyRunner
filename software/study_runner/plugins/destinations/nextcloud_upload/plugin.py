"""Nextcloud public-share upload destination.

Owns its own WebDAV client (`webdav_client.py`) end to end: publishing a
finished session, testing a connection, and validating the share-link shape a
study author types into the settings form.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from study_runner.contracts.plugin_api import PluginContext, Plugin


def _status(context: PluginContext) -> dict[str, Any]:
    section = context.hardware_config.get("nextcloud")
    configured = section if isinstance(section, dict) else {}
    enabled = configured.get("enabled", True) is not False
    return {
        "status": "available" if enabled else "disabled",
        "runtime_enabled": enabled,
        "configured_enabled": enabled,
        "device_label": "Nextcloud upload",
        "last_message": (
            "Nextcloud is available as a per-study upload destination."
            if enabled
            else "Nextcloud upload is switched off on this computer."
        ),
    }


def _publish(context: PluginContext, payload: dict[str, Any]) -> dict[str, Any]:
    from .webdav_client import NextcloudError, NextcloudPublicShareClient

    machine = context.hardware_config.get("nextcloud")
    if isinstance(machine, dict) and machine.get("enabled") is False:
        return {
            "ok": False,
            "permanent": True,
            "error": "Nextcloud upload is switched off on this computer. "
            "Turn it on under Settings > This computer > Nextcloud.",
        }

    config_data = payload.get("config_data") or {}
    study_settings = config_data.get("study_settings") or {}
    plugins = study_settings.get("plugins")
    selection = plugins.get("nextcloud") if isinstance(plugins, dict) else None
    if isinstance(selection, dict):
        enabled = bool(selection.get("enabled"))
        plugin_settings = selection.get("settings")
        plugin_settings = plugin_settings if isinstance(plugin_settings, dict) else {}
        share_link = str(plugin_settings.get("share_link") or "").strip()
    else:
        # Jobs committed before API v3 remain replayable.
        enabled = bool(study_settings.get("nextcloud_enabled"))
        share_link = str(study_settings.get("nextcloud_share_link") or "").strip()
    if not enabled or not share_link:
        return {
            "ok": False,
            "skipped": True,
            "error": "Nextcloud is no longer configured for this study.",
        }

    study_id = str(
        config_data.get("study_id")
        or (payload.get("result_payload") or {}).get("study_id")
        or ""
    )
    nextcloud_config = context.hardware_config.get("nextcloud") or {}
    password = context.secret("nextcloud", study_id)
    timeout_seconds = int(nextcloud_config.get("timeout_seconds") or 30)

    saved_output = payload.get("saved_output") or {}
    relative_folder = str(
        saved_output.get("session_relative_path")
        or saved_output.get("session_dir")
        or saved_output.get("participant_dir")
        or ""
    )
    root = Path(context.data_dir).resolve()
    local_folder = (root / relative_folder).resolve()
    legacy_folder = (root.parent / relative_folder).resolve()
    if relative_folder and not local_folder.is_dir() and legacy_folder.is_relative_to(root):
        local_folder = legacy_folder
    if not relative_folder or not local_folder.is_relative_to(root):
        return {
            "ok": False,
            "error": "The saved session folder is outside the results directory.",
        }

    result_payload = payload.get("result_payload") or {}
    try:
        return NextcloudPublicShareClient(
            share_link,
            password=password,
            timeout_seconds=timeout_seconds,
        ).upload_session_folder(
            local_folder,
            study_id=str(result_payload.get("study_id") or ""),
            participant_id=str(result_payload.get("participant_id") or ""),
            session_relative_path=str(saved_output.get("session_relative_path") or ""),
        )
    except NextcloudError as error:
        if not error.permanent:
            raise
        # Retrying cannot fix a rejected password or a deleted share.
        return {"ok": False, "permanent": True, "error": str(error)}


def _run_admin_action(
    context: PluginContext,
    action_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if action_key != "test_connection":
        raise ValueError(f"Unknown Nextcloud admin action: {action_key}")

    from .webdav_client import test_connection

    share_link = str(payload.get("share_link") or "").strip()
    # An omitted password means "use whatever is already stored" - the
    # operator is testing before saving what they just typed.
    password = str(payload.get("password") or "") or context.secret(
        "nextcloud", str(payload.get("study_id") or "").strip()
    )
    # The study settings panel never asks for a timeout: it is a machine
    # setting, so an omitted value means "the one this computer uses".
    machine_timeout = (context.hardware_config.get("nextcloud") or {}).get("timeout_seconds") or 10
    timeout_seconds = int(payload.get("timeout_seconds") or machine_timeout)
    return test_connection(share_link, password=password, timeout_seconds=timeout_seconds)


def _validate_setting(field_name: str, value: str) -> None:
    """Reject a share link the WebDAV client could not use, before it is saved.

    Called by `validation.py` because this manifest's `share_link` field
    declares `"format": "nextcloud_public_share"` - see
    `Plugin.validate_study_setting`.
    """
    if field_name != "share_link":
        return
    from .webdav_client import parse_share_link

    parse_share_link(value)


PLUGIN = Plugin(
    key="nextcloud",
    label="Nextcloud upload",
    category="storage",
    config_key="nextcloud",
    can_toggle=False,
    get_status=_status,
    publish_destination=_publish,
    run_admin_action=_run_admin_action,
    validate_study_setting=_validate_setting,
)
