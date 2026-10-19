"""Bind manifest-declared upload destinations to the persistent job queue."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from study_runner.contracts.card_ids import with_card_ids
from study_runner.contracts.plugin_api import Plugin
from study_runner.plugin_framework.registry import (
    build_context,
    get_plugin_manifest,
    get_plugins_with_capability,
)
from study_runner.shared.atomic_io import atomic_path_lock, atomic_write_json
from study_runner.shared.study_identifiers import normalize_study_id

from ..studies.study_config_service import (
    load_config,
    load_study,
    locked_study_change,
    patch_study_plugin_settings,
    study_busy_reason,
)
from ..studies.study_plugin_config import normalize_study_settings_plugins
from .upload_jobs_service import PermanentUploadError, UploadJobError, UploadJobService

# Study settings the retry comparison shows by value; any other type is only
# named as changed. Secrets are never compared.
_TEXT_SETTING_TYPES = frozenset({"string", "url"})
_SECRET_SETTING_TYPES = frozenset({"secret", "password"})


def configure_upload_jobs(app) -> UploadJobService:
    """Register every valid ``upload_destination`` plugin without a key list."""

    service = UploadJobService(Path(app.config["DATA_DIR"]))
    for plugin in get_plugins_with_capability("upload_destination"):
        capability = (
            (get_plugin_manifest(plugin.key).get("capability_config") or {})
            .get("upload_destination", {})
        )
        destination = str(capability.get("destination") or plugin.key).strip()
        service.register_executor(
            destination,
            _plugin_executor(app, plugin, destination),
            retry_policy=_machine_settings_reader(app, plugin),
        )
        service.register_retry_target_describer(destination, _retry_target_describer(app, plugin))

    migration = service.migrate_legacy_notion_queue()
    if migration.get("migrated"):
        print(f"[UPLOADS] Migrated {migration['migrated']} legacy Notion queue entries.")
    if migration.get("error"):
        print(f"[UPLOADS] Legacy Notion queue migration needs attention: {migration['error']}")
    app.config["UPLOAD_JOBS_SERVICE"] = service
    return service


def _machine_settings_reader(app: Any, plugin: Plugin) -> Callable[[], dict[str, Any]]:
    """The plugin's own section of this computer's settings, read fresh."""
    section = str(plugin.config_key or plugin.key)
    return lambda: dict((app.config.get("HARDWARE_CONFIG") or {}).get(section) or {})


def _current_plugin_selection(app: Any, study_id: str, plugin_key: str) -> dict[str, Any] | None:
    """A destination's current ``{enabled, required, settings}`` for ``study_id``.

    Reads the active study if it is the same one, otherwise the saved study
    library, exactly like ``patch_study_plugin_settings`` does. Returns
    ``None`` when the study (or its selection for this plugin) no longer
    exists, e.g. it was deleted since the session finished.
    """
    if not study_id:
        return None
    config_file = Path(app.config["CONFIG_FILE"])
    try:
        active = load_config(config_file) if config_file.is_file() else None
    except Exception:
        active = None
    same_active_study = (
        active is not None
        and normalize_study_id(str(active.get("study_id") or "")) == normalize_study_id(study_id)
    )
    try:
        latest = active if same_active_study else load_study(Path(app.config["SAVED_STUDIES_DIR"]), study_id)
    except FileNotFoundError:
        return None
    selection = ((latest.get("study_settings") or {}).get("plugins") or {}).get(plugin_key)
    if not isinstance(selection, dict) or not isinstance(selection.get("settings"), dict):
        return None
    return deepcopy(selection)


def _retry_target_describer(app: Any, plugin: Plugin) -> Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]]:
    """Build the comparison a retry confirmation shows for one destination.

    Compares every declared setting (not only the discovered ones): a
    hand-entered field such as the Notion parent page can drift from the
    frozen snapshot just as easily as an auto-discovered database id, and a
    structured one such as the Notion layout decides what an upload writes.
    Text settings are shown as they are; a structured setting is only named
    as changed (``kind: "structured"``), never dumped. A missing value counts
    as the field's default, so ``{}`` and an absent layout are the same.
    """
    manifest = get_plugin_manifest(plugin.key)
    schema = manifest.get("study_settings_schema") or {}
    fields_to_compare = {
        name: field for name, field in schema.items()
        if not field.get("secret") and field.get("type") not in _SECRET_SETTING_TYPES
    }

    def comparable(field: dict[str, Any], value: Any) -> str:
        if value is None:
            value = field.get("default")
        if field.get("type") in _TEXT_SETTING_TYPES:
            return str(value or "")
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    def describe(job: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
        if payload is None or not fields_to_compare:
            return {"differs": False, "fields": []}
        config_data = payload.get("config_data") or {}
        frozen_selection = ((config_data.get("study_settings") or {}).get("plugins") or {}).get(plugin.key) or {}
        frozen_settings = frozen_selection.get("settings")
        frozen_settings = frozen_settings if isinstance(frozen_settings, dict) else {}
        study_id = str(config_data.get("study_id") or "").strip()
        current_selection = _current_plugin_selection(app, study_id, plugin.key)
        if current_selection is None:
            return {"differs": False, "fields": [], "study_missing": True}
        current_settings = current_selection.get("settings") or {}
        fields = []
        for name, field in fields_to_compare.items():
            snapshot = comparable(field, frozen_settings.get(name))
            current = comparable(field, current_settings.get(name))
            if snapshot == current:
                continue
            entry = {"name": name, "label_key": field.get("label_key") or name}
            if field.get("type") in _TEXT_SETTING_TYPES:
                entry.update(snapshot=snapshot, current=current)
            else:
                entry.update(kind="structured", snapshot="", current="")
            fields.append(entry)
        return {"differs": bool(fields), "fields": fields}

    return describe


def _plugin_executor(
    app: Any,
    plugin: Plugin,
    destination: str,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    manifest = get_plugin_manifest(plugin.key)
    capability = (manifest.get("capability_config") or {}).get("upload_destination") or {}
    schema = manifest.get("study_settings_schema") or {}
    allowed_updates = set(capability.get("discovered_settings") or [])
    if any(schema.get(key, {}).get("type") != "string" for key in allowed_updates):
        raise ValueError("Discovered destination settings must be declared study string fields.")
    defaults = {key: field.get("default") for key, field in schema.items() if "default" in field}

    def execute(payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        retry_target = str(payload.pop("_retry_target", "snapshot") or "snapshot")
        if retry_target == "current":
            config_data = payload.get("config_data") or {}
            study_id = str(config_data.get("study_id") or "").strip()
            current_selection = _current_plugin_selection(app, study_id, plugin.key)
            if current_selection is not None:
                config_data = deepcopy(config_data)
                config_data.setdefault("study_settings", {}).setdefault("plugins", {})[
                    plugin.key
                ] = current_selection
                payload = {**payload, "config_data": config_data}
            # Else: the study was deleted or renamed since the session ended.
            # Falling back to the frozen snapshot is the only upload that is
            # still possible; the operator already saw this in the retry
            # comparison before choosing to retry.
        # A session recorded before card ids existed carries an id-less copy
        # of its study; give it the ids the saved study has, so its cards land
        # in the same place as every later session's.
        if isinstance(payload.get("config_data"), dict):
            payload["config_data"] = with_card_ids(payload["config_data"])

        handler = plugin.publish_destination
        if handler is None:
            raise UploadJobError(
                f"Upload plugin {plugin.key!r} has no destination handler."
            )
        context = build_context(
            base_dir=app.config["BASE_DIR"],
            data_dir=app.config["DATA_DIR"],
            hardware_config=app.config.get("HARDWARE_CONFIG", {}) or {},
            local_secrets=app.config.get("LOCAL_SECRETS", {}) or {},
            local_secrets_file=app.config["LOCAL_SECRETS_FILE"],
        )

        def publish(attempt: dict[str, Any]) -> dict[str, Any]:
            with app.app_context():
                result = handler(context, attempt) or {"ok": True}
            if not isinstance(result, dict):
                raise UploadJobError(f"Upload plugin {plugin.key!r} returned an invalid result.")
            return result

        if allowed_updates:
            config = payload.get("config_data") or {}
            study_id = str(config.get("study_id") or "").strip()
            if not study_id:
                raise UploadJobError("A study ID is required to remember discovered upload targets.")
            selection = normalize_study_settings_plugins(config.get("study_settings"))["plugins"][plugin.key]
            base_settings = {**defaults, **selection["settings"]}
            identity = json.dumps(
                [normalize_study_id(study_id), plugin.key, base_settings],
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
            state_path = Path(app.config["DATA_DIR"]) / "upload_targets" / f"{hashlib.sha256(identity).hexdigest()}.json"

            # Serialize attempts sharing one original target. The durable file
            # contains only permitted discoveries; scientific snapshots and
            # credentials are never copied into it.
            with atomic_path_lock(state_path):
                known: dict[str, str] = {}
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or state.get("schema") != "study-runner/upload-target/v1":
                        raise UploadJobError("Stored upload target has an unsupported schema.")
                    known = _validate_updates(state.get("settings"), allowed_updates, schema)
                attempt = deepcopy(payload)
                attempt_selection = deepcopy(selection)
                attempt_selection["settings"].update(known)
                attempt["config_data"].setdefault("study_settings", {}).setdefault("plugins", {})[plugin.key] = attempt_selection
                result = publish(attempt)
                discovered = _validate_updates(result.get("study_config_updates", {}), allowed_updates, schema)
                if discovered:
                    known.update(discovered)
                    # Checkpoint before evaluating ok: the target may have been
                    # created successfully even though the later upload failed.
                    atomic_write_json(state_path, {
                        "schema": "study-runner/upload-target/v1",
                        "settings": known,
                        "study_id": study_id,
                        "plugin_key": plugin.key,
                        "expected_settings": base_settings,
                        "defaults": defaults,
                    })
                if known:
                    _persist_study_config_updates(
                        app, study_id, plugin.key, base_settings, known, defaults,
                    )
        else:
            result = publish(deepcopy(payload))
            _validate_updates(result.get("study_config_updates", {}), allowed_updates, schema)

        if result.get("ok") is False:
            detail = str(result.get("error") or "unknown error")
            print(f"[UPLOADS] {destination} attempt failed: {detail}")
            if result.get("permanent") is True:
                # Wrong credentials, an inaccessible target, or a missing
                # setting: retrying cannot help, so say exactly what to fix.
                raise PermanentUploadError(f"{plugin.label}: {detail}")
            raise UploadJobError(f"{plugin.label}: {detail} (Study Runner will retry automatically.)")
        return result

    return execute


def _validate_updates(value: Any, allowed: set[str], schema: dict[str, Any]) -> dict[str, str]:
    """A plugin can report only its explicitly permitted nonempty string fields."""
    if not isinstance(value, dict):
        raise UploadJobError("Discovered destination settings must be an object.")
    for key, setting in value.items():
        if (
            key not in allowed
            or not isinstance(setting, str)
            or not setting.strip()
            or len(setting) > min(4096, schema.get(key, {}).get("max_length", 4096))
        ):
            raise UploadJobError("Plugin reported an invalid or undeclared destination setting.")
    return dict(value)


def _persist_study_config_updates(
    app: Any,
    study_id: str,
    plugin_key: str,
    expected_settings: dict[str, Any],
    updates: dict[str, str],
    defaults: dict[str, Any],
) -> None:
    """Refresh a matching study after the durable retry state was committed."""
    try:
        patch_study_plugin_settings(
            app.config["CONFIG_FILE"], app.config["SAVED_STUDIES_DIR"],
            study_id=study_id, plugin_key=plugin_key, expected_settings=expected_settings,
            updates=updates, defaults=defaults,
            busy_reason=lambda: study_busy_reason(app.config),
        )
    except Exception as error:
        # The target checkpoint remains available to the next queued upload.
        print(f"[UPLOADS] Could not refresh study settings for {plugin_key!r}: {error}")


def apply_deferred_upload_targets(app: Any) -> int:
    """Replay durable destination discoveries once changing studies is safe."""

    target_dir = Path(app.config["DATA_DIR"]) / "upload_targets"
    if not target_dir.is_dir():
        return 0
    with locked_study_change():
        if study_busy_reason(app.config):
            return 0
        applied = 0
        for path in sorted(target_dir.glob("*.json")):
            try:
                with atomic_path_lock(path):
                    state = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(state, dict) or state.get("schema") != "study-runner/upload-target/v1":
                        continue
                    # Older checkpoints lack replay metadata. Their next
                    # upload attempt still applies the discovery as before.
                    if not all(key in state for key in (
                        "study_id", "plugin_key", "expected_settings", "defaults",
                    )):
                        continue
                    if not all(isinstance(state.get(key), dict) for key in (
                        "settings", "expected_settings", "defaults",
                    )):
                        continue
                    if patch_study_plugin_settings(
                        app.config["CONFIG_FILE"],
                        app.config["SAVED_STUDIES_DIR"],
                        study_id=str(state["study_id"]),
                        plugin_key=str(state["plugin_key"]),
                        expected_settings=state["expected_settings"],
                        updates=state["settings"],
                        defaults=state["defaults"],
                        busy_reason=lambda: study_busy_reason(app.config),
                    ):
                        applied += 1
            except Exception as error:
                # A successful publication must not be retried because a
                # later study-file refresh failed; the checkpoint remains.
                print(f"[UPLOADS] Deferred target refresh from {path.name} failed: {error}")
        return applied
