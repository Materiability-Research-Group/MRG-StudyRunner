"""Compatibility migration from legacy study fields to the manifest-driven
plugin settings shape.

Legacy fields are accepted as input, but the returned configuration uses the
manifest-driven ``study_settings.plugins`` and ``card.plugin_actions`` shapes.
That one-way migration prevents old sensor names from leaking back into the
generic runtime or newly saved study files.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from study_runner.data_core.host import clock_diagnostics as recording_clock_diagnostics
from study_runner.data_core.host import markers as recording_markers


# markers and clock_diagnostics record unconditionally on every session -- see
# recording/markers.py -- so no card can address an action at them. A saved
# study may still carry the field from before they stopped being plugins; it
# is dropped here rather than in the general "unknown plugin" fallback below,
# which deliberately preserves settings for plugins that are merely absent
# from *this* install so a config does not lose data across a machine swap.
_NOT_ADDRESSABLE_BY_CARDS = {recording_markers.SOURCE_KEY, recording_clock_diagnostics.SOURCE_KEY}

_CANONICAL_RECORDING_DISABLE_TOKENS = {
    "captureenabled",
    "canonicalrecording",
    "canonicalrecordingenabled",
    "canonicalstreamrecording",
    "canonicalstreamrecordingenabled",
    "lslenabled",
    "recordingenabled",
    "sendmarker",
    "tolsl",
}
LEGACY_CARD_FIELDS = {
    "send_signal",
    "brainbit_to_lsl",
    "brainbit_to_touchdesigner",
    "camera_capture_enabled",
    "camera_snapshot_interval_ms",
    "mini_radar_recording_enabled",
}
_LEGACY_PLUGIN_ALIASES = {
    "emotion_worker": "camera_emotion",
}


class PluginConfigError(ValueError):
    """Raised when a study's plugin selection has an invalid shape."""


def normalize_study_plugins(
    study_settings: dict[str, Any] | None,
    *,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return normalized ``{plugin_key: {enabled, required, settings}}`` data.

    References to currently missing plugins are preserved.  A later readiness
    check can then report a missing required plugin instead of silently losing
    the study's intent while loading or saving its file.
    """

    source = study_settings if isinstance(study_settings, dict) else {}
    available = manifests if manifests is not None else _plugin_manifests()
    raw_plugins = source.get("plugins")
    if raw_plugins is not None and not isinstance(raw_plugins, dict):
        raise PluginConfigError("study_settings.plugins must be a JSON object")
    if isinstance(raw_plugins, dict):
        raw_plugins = _migrate_legacy_plugin_aliases(raw_plugins)

    normalized: dict[str, dict[str, Any]] = {}
    for plugin_key, manifest in available.items():
        capabilities = set(manifest.get("capabilities") or [])
        capability_config = manifest.get("capability_config") or {}
        if "study_sensor" in capabilities:
            sensor_config = capability_config.get("study_sensor") or {}
            enabled = _legacy_sensor_enabled(source, plugin_key, sensor_config)
            normalized[plugin_key] = {
                "enabled": enabled,
                "required": bool(sensor_config.get("default_required", True)),
                "settings": {},
            }
        elif "upload_destination" in capabilities:
            destination_config = capability_config.get("upload_destination") or {}
            normalized[plugin_key] = {
                "enabled": _legacy_destination_enabled(source, destination_config),
                "required": False,
                "settings": _legacy_destination_settings(source, destination_config),
            }

    # A legacy sensor mirror may refer to a plugin that is not installed on
    # this machine.  Turn that opaque reference into the canonical shape before
    # dropping the mirror, so an empty catalog produces a readiness blocker
    # instead of either data loss or an "unsupported sensor" load error.
    raw_sensors = source.get("sensors")
    if isinstance(raw_sensors, dict):
        master_enabled = _as_bool(source.get("sensors_enabled", True))
        for raw_key, raw_enabled in raw_sensors.items():
            plugin_key = str(raw_key or "").strip()
            if not plugin_key:
                raise PluginConfigError("study_settings.sensors contains an empty plugin key")
            if plugin_key in normalized:
                continue
            enabled = master_enabled and _as_bool(raw_enabled)
            normalized[plugin_key] = {
                "enabled": enabled,
                "required": enabled,
                "settings": {},
            }

    if isinstance(raw_plugins, dict):
        for raw_key, raw_entry in raw_plugins.items():
            plugin_key = str(raw_key or "").strip()
            if not plugin_key:
                raise PluginConfigError("study_settings.plugins contains an empty plugin key")
            if not isinstance(raw_entry, dict):
                raise PluginConfigError(
                    f"study_settings.plugins.{plugin_key} must be a JSON object"
                )
            raw_plugin_settings = raw_entry.get("settings", {})
            if raw_plugin_settings is None:
                raw_plugin_settings = {}
            if not isinstance(raw_plugin_settings, dict):
                raise PluginConfigError(
                    f"study_settings.plugins.{plugin_key}.settings must be a JSON object"
                )

            defaults = normalized.get(
                plugin_key,
                {"enabled": False, "required": False, "settings": {}},
            )
            enabled = _as_bool(raw_entry.get("enabled", defaults["enabled"]))
            required_default = defaults["required"] if enabled else False
            normalized[plugin_key] = {
                "enabled": enabled,
                "required": _as_bool(raw_entry.get("required", required_default)),
                "settings": deepcopy(raw_plugin_settings),
            }

    return normalized


def _migrate_legacy_plugin_aliases(
    raw_plugins: dict[str, Any],
) -> dict[str, Any]:
    """Return canonical plugin keys without losing legacy study settings.

    An explicitly configured canonical plugin wins for selection fields.  Its
    settings also win key-by-key, while settings that exist only on the old
    plugin entry are retained.  This prevents a historic required
    ``emotion_worker`` entry from surviving as an unknown, start-blocking
    plugin after camera and emotion were consolidated.
    """

    migrated = deepcopy(raw_plugins)
    for legacy_key, canonical_key in _LEGACY_PLUGIN_ALIASES.items():
        if legacy_key not in migrated:
            continue
        legacy_entry = migrated.pop(legacy_key)
        if not isinstance(legacy_entry, dict):
            raise PluginConfigError(
                f"study_settings.plugins.{legacy_key} must be a JSON object"
            )
        legacy_settings = legacy_entry.get("settings", {})
        if legacy_settings is None:
            legacy_settings = {}
        if not isinstance(legacy_settings, dict):
            raise PluginConfigError(
                f"study_settings.plugins.{legacy_key}.settings must be a JSON object"
            )

        canonical_entry = migrated.get(canonical_key)
        if canonical_entry is None:
            canonical_entry = deepcopy(legacy_entry)
            canonical_entry["settings"] = deepcopy(legacy_settings)
            migrated[canonical_key] = canonical_entry
            continue
        if not isinstance(canonical_entry, dict):
            # Keep the canonical value so the normal validation below reports
            # the error against the current plugin key.
            continue
        canonical_settings = canonical_entry.get("settings", {})
        if canonical_settings is None:
            canonical_settings = {}
        if not isinstance(canonical_settings, dict):
            # As above, defer the canonical-key error to the regular validator.
            continue
        merged_settings = deepcopy(legacy_settings)
        merged_settings.update(deepcopy(canonical_settings))
        canonical_entry["settings"] = merged_settings

    return migrated


def normalize_study_settings_plugins(
    study_settings: dict[str, Any] | None,
    *,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return canonical v3 plugin data while accepting legacy fields as input."""

    source = deepcopy(study_settings if isinstance(study_settings, dict) else {})
    available = manifests if manifests is not None else _plugin_manifests()
    plugins = normalize_study_plugins(source, manifests=available)
    source["plugins"] = plugins

    sensors = {
        plugin_key: bool(entry.get("enabled"))
        for plugin_key, entry in plugins.items()
        if "study_sensor" in set((available.get(plugin_key) or {}).get("capabilities") or [])
    }
    # The mirror is compatibility output only and therefore contains installed
    # study sensors exclusively. Missing plugin intent remains in ``plugins``.
    source["sensors_enabled"] = any(sensors.values())
    source["sensors"] = sensors
    _remove_legacy_destination_fields(source, available)
    return source


def normalize_card_plugin_actions(
    card: dict[str, Any] | None,
    *,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build declarative per-plugin actions, with v3 values taking priority."""

    source = card if isinstance(card, dict) else {}
    available = manifests if manifests is not None else _plugin_manifests()
    raw_actions = source.get("plugin_actions")
    if raw_actions is not None and not isinstance(raw_actions, dict):
        raise PluginConfigError("plugin_actions must be a JSON object")

    actions: dict[str, dict[str, Any]] = {}
    for plugin_key, manifest in available.items():
        schema = manifest.get("card_actions_schema") or {}
        if not schema:
            continue
        actions[plugin_key] = {
            field_name: deepcopy(field.get("default"))
            for field_name, field in schema.items()
            if isinstance(field, dict) and "default" in field
        }

    _apply_legacy_card_actions(actions, source)
    if isinstance(raw_actions, dict):
        for raw_key, raw_value in raw_actions.items():
            plugin_key = str(raw_key or "").strip()
            if not plugin_key:
                raise PluginConfigError("plugin_actions contains an empty plugin key")
            if not isinstance(raw_value, dict):
                raise PluginConfigError(f"plugin_actions.{plugin_key} must be a JSON object")
            if plugin_key in _NOT_ADDRESSABLE_BY_CARDS:
                actions.pop(plugin_key, None)
                continue
            manifest = available.get(plugin_key) or {}
            schema = manifest.get("card_actions_schema") or {}
            normalized_values = _normalize_action_values(
                plugin_key,
                _without_recording_disable_actions(plugin_key, raw_value, manifest),
                schema,
            )
            if normalized_values:
                actions[plugin_key] = normalized_values
            else:
                actions.pop(plugin_key, None)
    return actions


def normalize_card_actuator_plugins(
    card: dict[str, Any] | None,
    *,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Return which actuator plugins a stimulus card starts and stops.

    Only the manifests decide what an actuator is (trial ``start`` and
    ``stop``). A card saved before this selection existed sent both to every
    such plugin unless it switched them off - all of them with the old
    ``send_signal`` field, or one of them through a boolean option of that
    plugin's own card actions. A missing field reproduces exactly that. Keys
    of plugins missing from this install are kept like any other plugin
    reference; installed plugins that are not actuators are dropped.
    """

    from study_runner.plugin_framework.registry import (
        is_stimulus_actuator,
        stimulus_actuator_keys,
    )

    source = card if isinstance(card, dict) else {}
    available = manifests if manifests is not None else _plugin_manifests()
    raw = source.get("actuator_plugins")
    if raw is None:
        if not _as_bool(source.get("send_signal", True)):
            return []
        legacy_actions = source.get("plugin_actions")
        legacy_actions = legacy_actions if isinstance(legacy_actions, dict) else {}
        return [
            plugin_key
            for plugin_key in stimulus_actuator_keys(available)
            if not _switched_off(legacy_actions.get(plugin_key))
        ]
    if not isinstance(raw, list):
        raise PluginConfigError("actuator_plugins must be a list of plugin keys")
    selected: list[str] = []
    for item in raw:
        plugin_key = str(item or "").strip()
        if not plugin_key:
            raise PluginConfigError("actuator_plugins contains an empty plugin key")
        if plugin_key in available and not is_stimulus_actuator(available[plugin_key]):
            continue
        if plugin_key not in selected:
            selected.append(plugin_key)
    return selected


def _switched_off(actions: Any) -> bool:
    return isinstance(actions, dict) and any(value is False for value in actions.values())


def reset_stale_upload_discoveries(
    previous_study_settings: dict[str, Any] | None,
    next_study_settings: dict[str, Any] | None,
    *,
    manifests: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Clear an upload destination's discovered settings its own save left stale.

    Each ``upload_destination`` plugin may auto-discover settings (e.g. a
    database id created under a parent page) and remember them on the study
    (``capability_config.upload_destination.discovered_settings``). If the
    operator then changes one of the *other*, hand-entered settings for that
    destination (e.g. the parent page) without also updating the discovered
    ones, the stale discovered values would otherwise keep pointing at the
    previous target until some retry happens to notice. Clearing them here,
    at the moment the operator's intent changes, makes the next upload
    rediscover a target that matches what was just saved.

    A discovered field is left alone when the same save explicitly set it to
    something new - that is the operator (or a replay) supplying the value
    on purpose, not a leftover from before. A destination that declares
    ``target_settings`` resets only when one of those changes; its other
    settings (e.g. which columns to fill) never move the target.
    """

    available = manifests if manifests is not None else _plugin_manifests()
    next_settings = deepcopy(next_study_settings if isinstance(next_study_settings, dict) else {})
    previous = previous_study_settings if isinstance(previous_study_settings, dict) else {}
    previous_plugins = previous.get("plugins") if isinstance(previous.get("plugins"), dict) else {}
    next_plugins = next_settings.get("plugins") if isinstance(next_settings.get("plugins"), dict) else {}

    for plugin_key, manifest in available.items():
        capability = (manifest.get("capability_config") or {}).get("upload_destination")
        if not isinstance(capability, dict):
            continue
        discovered_fields = set(capability.get("discovered_settings") or [])
        if not discovered_fields:
            continue
        previous_entry = previous_plugins.get(plugin_key)
        next_entry = next_plugins.get(plugin_key)
        if not isinstance(previous_entry, dict) or not isinstance(next_entry, dict):
            continue
        previous_values = previous_entry.get("settings")
        next_values = next_entry.get("settings")
        if not isinstance(previous_values, dict) or not isinstance(next_values, dict):
            continue

        schema = manifest.get("study_settings_schema") or {}
        hand_entered_fields = set(capability.get("target_settings") or schema.keys()) - discovered_fields
        changed_by_hand = any(
            previous_values.get(field) != next_values.get(field) for field in hand_entered_fields
        )
        if not changed_by_hand:
            continue
        for field in discovered_fields:
            # The save already changed this discovered field itself (an
            # explicit override, or a replayed discovery): keep it.
            if next_values.get(field) != previous_values.get(field):
                continue
            if next_values.get(field):
                next_values[field] = str(schema.get(field, {}).get("default") or "")
    return next_settings


def migrate_study_plugin_config(config_data: dict[str, Any]) -> dict[str, Any]:
    """Compatibility entry point for callers that migrate complete studies."""

    from .migrate import migrate_study_config

    return migrate_study_config(
        config_data,
        normalize_settings=normalize_study_settings_plugins,
        normalize_actions=normalize_card_plugin_actions,
        normalize_actuators=normalize_card_actuator_plugins,
        legacy_card_fields=LEGACY_CARD_FIELDS,
    )


def _plugin_manifests() -> dict[str, dict[str, Any]]:
    from study_runner.plugin_framework.registry import get_plugin_manifests

    return get_plugin_manifests()


def _legacy_sensor_enabled(
    settings: dict[str, Any],
    plugin_key: str,
    capability_config: dict[str, Any],
) -> bool:
    master_enabled = _as_bool(settings.get("sensors_enabled", True))
    sensors = settings.get("sensors")
    default_enabled = _as_bool(capability_config.get("default_enabled", False))
    selected = sensors.get(plugin_key, default_enabled) if isinstance(sensors, dict) else default_enabled
    return master_enabled and _as_bool(selected)


def _legacy_destination_enabled(
    settings: dict[str, Any],
    capability_config: dict[str, Any],
) -> bool:
    legacy = capability_config.get("legacy")
    legacy = legacy if isinstance(legacy, dict) else {}
    enabled_field = str(legacy.get("enabled_field") or "").strip()
    if enabled_field and enabled_field in settings:
        return _as_bool(settings.get(enabled_field))
    return _as_bool(capability_config.get("default_enabled", False))


def _legacy_destination_settings(
    settings: dict[str, Any],
    capability_config: dict[str, Any],
) -> dict[str, Any]:
    legacy = capability_config.get("legacy")
    legacy = legacy if isinstance(legacy, dict) else {}
    fields = legacy.get("settings")
    fields = fields if isinstance(fields, dict) else {}
    return {
        str(name): (
            str(settings.get(legacy_field) or "").strip()
            if isinstance(settings.get(legacy_field), str)
            else deepcopy(settings.get(legacy_field))
        )
        for name, legacy_field in fields.items()
        if legacy_field in settings
    }


def _remove_legacy_destination_fields(
    settings: dict[str, Any],
    manifests: dict[str, dict[str, Any]],
) -> None:
    for manifest in manifests.values():
        destination = (
            (manifest.get("capability_config") or {}).get("upload_destination")
            or {}
        )
        legacy = destination.get("legacy")
        legacy = legacy if isinstance(legacy, dict) else {}
        enabled_field = str(legacy.get("enabled_field") or "").strip()
        if enabled_field:
            settings.pop(enabled_field, None)
        legacy_settings = legacy.get("settings")
        if isinstance(legacy_settings, dict):
            for field in legacy_settings.values():
                settings.pop(str(field), None)


def _apply_legacy_card_actions(
    actions: dict[str, dict[str, Any]],
    card: dict[str, Any],
) -> None:
    # Which plugins a card starts and stops is no longer a per-plugin card
    # action; see normalize_card_actuator_plugins. Only this pre-v3 field
    # still maps onto a declared card action.
    if "camera_emotion" in actions:
        actions["camera_emotion"]["snapshot_interval_ms"] = card.get(
            "camera_snapshot_interval_ms", 1_000
        )


def _without_recording_disable_actions(
    plugin_key: str,
    values: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    capabilities = set(manifest.get("capabilities") or [])
    if "recording_source" not in capabilities:
        return values
    return {
        name: value
        for name, value in values.items()
        if _setting_token(str(name)) not in _CANONICAL_RECORDING_DISABLE_TOKENS
    }


def _setting_token(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _normalize_action_values(
    plugin_key: str,
    values: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, raw_value in values.items():
        field = schema.get(name)
        if not isinstance(field, dict):
            # Missing plugins and newer manifest fields must survive a save.
            normalized[str(name)] = deepcopy(raw_value)
            continue
        field_type = field.get("type")
        if field_type == "boolean":
            normalized[name] = _as_bool(raw_value)
            continue
        if field_type == "number":
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as error:
                raise PluginConfigError(f"plugin_actions.{plugin_key}.{name} must be a number") from error
            minimum = field.get("minimum")
            maximum = field.get("maximum")
            if minimum is not None and value < minimum:
                raise PluginConfigError(
                    f"plugin_actions.{plugin_key}.{name} must be at least {minimum}"
                )
            if maximum is not None and value > maximum:
                raise PluginConfigError(
                    f"plugin_actions.{plugin_key}.{name} must be at most {maximum}"
                )
            normalized[name] = int(value) if value.is_integer() else value
            continue
        normalized[name] = deepcopy(raw_value)
    return normalized


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"0", "false", "no", "off"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)
