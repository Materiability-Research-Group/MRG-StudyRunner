"""Example upload-destination plugin -- Study Runner plugin SDK template.

Rename `example_destination` everywhere (this file's PLUGIN.key/config_key
and the manifest's plugin_key/config_key) before shipping;
`tools/plugin_sdk.py new destinations <your_key>` does this substitution
for you.

`publish_destination` is the one handler `upload_destination` requires. The
core invokes it only after all local session artifacts are complete. A
real destination uploads the artifacts named in `payload` to a remote
location and reports the outcome; this example uploads nothing and always
reports success, to prove the contract shape without any network code.
The child process checks this callback at startup and rejects undeclared
operations.

Publishing must be idempotent per session: the operator can "Upload again"
any finished upload (session detail view), and the same session then
arrives a second time, with the study's current settings. Update or
overwrite what this session published before; never add a duplicate.
A destination with discovered settings (a created folder or database id)
can list the hand-entered settings that name the target in
`upload_destination.target_settings`; only a change to those then makes
the discovered ones start over.
See `study_runner/plugins/destinations/nextcloud_upload/plugin.py` for a
complete, real implementation once you outgrow this template.
"""
from __future__ import annotations

from typing import Any

from study_runner.contracts.plugin_api import Plugin, PluginContext


def _initialize(context: PluginContext) -> None:
    del context  # unused in this minimal example


def _status(context: PluginContext) -> dict[str, Any]:
    del context
    return {"status": "ok"}


def _publish(context: PluginContext, payload: dict[str, Any]) -> dict[str, Any]:
    del context
    return {"ok": True, "detail": f"example destination received fields: {sorted(payload)}"}


PLUGIN = Plugin(
    key="example_destination",
    label="Example destination",
    category="storage",
    config_key="example_destination",
    initialize=_initialize,
    get_status=_status,
    publish_destination=_publish,
)
