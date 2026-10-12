"""Pure plugin manifest and command-payload contracts.

Normalization accepts JSON values and depends only on the standard library.
Filesystem discovery, runtime drivers and plugin objects remain in the framework.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import PurePosixPath
import re
from typing import Any

from study_runner.contracts.sensor_contract import (
    SensorContractError,
    normalize_backup_projection,
    normalize_live_view,
    normalize_stream_timing,
    validate_sensor_contract,
)


PLUGIN_API_VERSION = 5
# The in-process v3 import path was removed in Phase 3.1. Every shipped manifest
# was already api_version 4 (process-host) before this narrowed. v4 -> v5 in Phase 3.4:
# `runtime_control` retired (traced every call site; it had zero effect --
# process_host's only use was a fallback dead in practice because every
# declaring plugin already sets runtime.actions explicitly), `readiness`
# renamed to `runtime_modes` (it collided in name with the unrelated
# `readiness_requirements` capability), `health` now actually gates
# per-plugin status polling instead of being declared with no effect.
SUPPORTED_PLUGIN_API_VERSIONS = (5,)

# Capabilities that existed under API v4 and are rejected under v5+, each with
# the reason -- so a stale third-party manifest gets a clear message instead
# of the generic "invalid capability name" a silent drop would produce.
RETIRED_CAPABILITIES = {
    "runtime_control": (
        "removed in api_version 5: it had no effect (process_host's fallback "
        "was dead code; every plugin that declared it already sets "
        "runtime.actions explicitly). Remove the capability declaration."
    ),
    "readiness": (
        "renamed to runtime_modes in api_version 5, to stop colliding with "
        "the unrelated readiness_requirements capability. Rename the key; "
        "the schema (mode_setting/default_mode/platform_modes) is unchanged."
    ),
}


SIDECAR_FIELDS = ("sensor", "filename_suffix", "output_key")
DEFAULT_POLL_INTERVAL_MS = 2_000
DEFAULT_REQUEST_TIMEOUT_MS = 1_000
MAX_PUBLISH_TIMEOUT_MS = 30 * 60 * 1000
UI_VISIBILITY_AREAS = (
    "dashboard",
    "settings_hub",
    "study_settings",
    "destination_settings",
)
UI_EXTENSION_SURFACES = ("dashboard", "participant", "card", "study_settings")
_UI_ASSET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*\.(?:js|css)$")
_UI_EXTENSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*\.js$")
_TIMELINE_CHANNEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PLATFORM_TARGET_PATTERN = re.compile(r"^(?:default|[a-z][a-z0-9]*-[a-z0-9_]+)$")
ACQUISITION_TRANSPORTS = {
    "internal",
    "lan",
    "wlan",
    "ble",
    "serial",
    "browser_https",
    "local_hardware",
    "network_adapter",
}
ACQUISITION_DELIVERIES = {"native_lsl", "host_lsl_bridge"}
_NATIVE_LSL_TRANSPORTS = {"lan", "wlan"}
_HOST_LSL_BRIDGE_TRANSPORTS = ACQUISITION_TRANSPORTS - _NATIVE_LSL_TRANSPORTS
_BROWSER_TRANSPORT_REQUIREMENTS = (
    "heartbeat_required",
    "sequence_required",
    "source_timestamp_required",
)
_BROWSER_SOURCE_TIMESTAMP_FIELDS = {"source_epoch_ms", "source_timestamp", "source_monotonic_ms"}
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
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# A plugin's version is a semantic version: it is cited in methods sections and
# compared by tools/plugin_versions.py, so free text is not allowed.
_SEMVER_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_LSL_CHANNEL_FORMATS = {
    "int8",
    "int16",
    "int32",
    "int64",
    "float32",
    "double64",
    "string",
}
# Historic capability names are accepted while reading a manifest, but the
# public API-v5 catalog always exposes one current vocabulary.
CAPABILITY_ALIASES = {
    "status_poll": "health",
    "lsl_stream": "lsl_stream_provider",
    "recording": "recording_source",
    "xdf_recording": "recording_worker",
}


class PluginManifestError(ValueError):
    """Raised when one built-in extension manifest violates the contract."""


def validate_and_normalize_manifest(payload: Any, *, directory_name: str) -> dict[str, Any]:
    """Validate a raw ``manifest.json`` and return its stable public shape."""

    if not isinstance(payload, dict):
        raise PluginManifestError("manifest root must be a JSON object")
    api_version = payload.get("api_version")
    if api_version not in SUPPORTED_PLUGIN_API_VERSIONS:
        supported = ", ".join(str(value) for value in SUPPORTED_PLUGIN_API_VERSIONS)
        raise PluginManifestError(f"api_version must be one of: {supported}")

    plugin_key = _required_key(payload, "plugin_key")
    config_key = _required_key(payload, "config_key")
    version = _required_text(payload, "version")
    if not _SEMVER_PATTERN.fullmatch(version):
        raise PluginManifestError("version must be MAJOR.MINOR.PATCH, for example 1.2.0")
    category = _required_text(payload, "category")
    ui = payload.get("ui")
    if not isinstance(ui, dict):
        raise PluginManifestError("ui must be a JSON object")
    label = _required_text(ui, "label", prefix="ui.")
    description = _optional_text(ui.get("description"))
    order = _non_negative_int(ui.get("order", 1_000), "ui.order")
    visibility = _normalize_ui_visibility(ui.get("visibility"))
    extensions = _normalize_ui_extensions(ui.get("extensions"))
    assets = _normalize_ui_assets(ui.get("assets"))
    timeline = _normalize_timeline_metadata(ui.get("timeline"))
    icon = _optional_text(ui.get("icon"))
    # Translation key of the plugin's "(?)" help text (title + introduction);
    # the per-field help lives on each setting as help_key.
    help_key = _optional_text(ui.get("help_key"))
    unexpected_ui = sorted(
        set(ui) - {
            "label", "description", "order", "visibility", "extensions",
            "assets", "timeline", "icon", "help_key",
        }
    )
    if unexpected_ui:
        raise PluginManifestError(
            "ui contains unsupported fields: " + ", ".join(unexpected_ui)
        )

    capability_config = _normalize_capabilities(payload.get("capabilities"))
    if "card_contract" in capability_config:
        if category != "card" or set(capability_config) != {"card_contract"}:
            raise PluginManifestError("A card extension must declare category card and only card_contract")
        if "card" not in extensions:
            raise PluginManifestError("A card extension must declare ui.extensions.card")
    settings = _normalize_settings(payload.get("settings"))
    streams = _normalize_streams(payload.get("streams"))
    _validate_capability_contracts(capability_config, streams, settings)
    lifecycle = _normalize_lifecycle(payload.get("lifecycle"))
    runtime = _normalize_process_runtime(payload.get("runtime"), api_version=int(api_version))
    # The host builds a sensor's sidecar export from runtime.sidecar alone, so
    # declaring the capability without it would silently export nothing.
    if "sidecar_export" in capability_config and set(runtime.get("sidecar") or {}) != set(SIDECAR_FIELDS):
        raise PluginManifestError(
            "sidecar_export requires runtime.sidecar with " + ", ".join(SIDECAR_FIELDS)
        )

    poll_interval_ms = _positive_int(
        payload.get("poll_interval_ms", DEFAULT_POLL_INTERVAL_MS),
        "poll_interval_ms",
    )
    request_timeout_ms = _positive_int(
        payload.get("request_timeout_ms", DEFAULT_REQUEST_TIMEOUT_MS),
        "request_timeout_ms",
    )
    backpressure = _normalize_backpressure(payload.get("backpressure"))
    clock_domain = _optional_text(payload.get("clock_domain")) or _default_clock_domain(streams)
    expected_data_rate = payload.get("expected_data_rate", {})
    if not isinstance(expected_data_rate, dict):
        raise PluginManifestError("expected_data_rate must be a JSON object")

    return {
        "api_version": int(api_version),
        "plugin_key": plugin_key,
        "config_key": config_key,
        "version": version,
        "category": category,
        "directory": directory_name,
        "ui": {
            "label": label,
            "description": description,
            "order": order,
            "visibility": visibility,
            "extensions": extensions,
            "assets": assets,
            "timeline": timeline,
            "icon": icon,
            "help_key": help_key,
        },
        "capabilities": list(capability_config),
        "capability_config": capability_config,
        "streams": streams,
        "settings": settings,
        # Existing settings/status services consume these compatibility names.
        "runtime_settings": deepcopy(settings["machine"]),
        "study_settings_schema": deepcopy(settings["study"]),
        "card_actions_schema": deepcopy(settings["card_actions"]),
        "poll_interval_ms": poll_interval_ms,
        "request_timeout_ms": request_timeout_ms,
        "clock_domain": clock_domain,
        "expected_data_rate": deepcopy(expected_data_rate),
        "backpressure": backpressure,
        "lifecycle": lifecycle,
        "runtime": runtime,
    }


def validate_admin_action_payload(
    action: dict[str, Any],
    payload: Any,
) -> dict[str, Any]:
    """Validate one request body against its closed manifest declaration."""

    if not isinstance(payload, dict):
        raise ValueError("plugin action payload must be a JSON object")
    schema = action.get("payload_schema") or {}
    if not isinstance(schema, dict):
        raise ValueError("plugin action payload schema is invalid")
    unexpected = sorted(set(payload) - set(schema))
    if unexpected:
        raise ValueError(
            "plugin action payload contains undeclared fields: " + ", ".join(unexpected)
        )

    normalized: dict[str, Any] = {}
    for name, raw_field in schema.items():
        field = raw_field if isinstance(raw_field, dict) else {}
        if name not in payload:
            if field.get("required"):
                raise ValueError(f"plugin action payload is missing required field: {name}")
            continue
        value = payload[name]
        field_type = field.get("type")
        if field_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"plugin action payload field {name} must be a string")
            if len(value) > int(field.get("max_length", 512)):
                raise ValueError(f"plugin action payload field {name} is too long")
        elif field_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"plugin action payload field {name} must be an integer")
        elif field_type == "number":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"plugin action payload field {name} must be a finite number")
        elif field_type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"plugin action payload field {name} must be boolean")
        else:
            raise ValueError(f"plugin action payload field {name} has an invalid schema")
        if "minimum" in field and value < field["minimum"]:
            raise ValueError(f"plugin action payload field {name} is below its minimum")
        if "maximum" in field and value > field["maximum"]:
            raise ValueError(f"plugin action payload field {name} exceeds its maximum")
        if "enum" in field and value not in field["enum"]:
            raise ValueError(f"plugin action payload field {name} is not an allowed value")
        normalized[name] = deepcopy(value)

    any_of = action.get("any_of_required") or []
    if any_of and not any(
        name in normalized and normalized[name] not in (None, "")
        for name in any_of
    ):
        raise ValueError(
            "plugin action payload requires at least one of: " + ", ".join(any_of)
        )
    return normalized


def _normalize_process_runtime(value: Any, *, api_version: int) -> dict[str, Any]:
    """Validate the deliberately small API-v5 subprocess contract."""

    if api_version < 4:
        if value not in (None, {}):
            raise PluginManifestError("runtime is only supported by plugin API v5")
        return {}
    if not isinstance(value, dict):
        raise PluginManifestError("runtime must be a JSON object for plugin API v5")
    unexpected = sorted(
        set(value)
        - {
            "entrypoint",
            "protocol",
            "interactive_stdin",
            "modes",
            "sample_delivery",
            "stream_contract",
            "operation_timeouts_ms",
            "actions",
            "trial_events",
            "can_toggle",
            "sidecar",
        }
    )
    if unexpected:
        raise PluginManifestError("runtime contains unsupported fields: " + ", ".join(unexpected))
    entrypoint = _required_text(value, "entrypoint", prefix="runtime.")
    relative = PurePosixPath(entrypoint)
    if relative.as_posix() != "driver.py":
        raise PluginManifestError("runtime.entrypoint must be the fixed driver.py")
    protocol = _required_text(value, "protocol", prefix="runtime.")
    if protocol != "study-runner-stdio/v1":
        raise PluginManifestError("runtime.protocol must be study-runner-stdio/v1")
    interactive_stdin = value.get("interactive_stdin", False)
    if not isinstance(interactive_stdin, bool):
        raise PluginManifestError("runtime.interactive_stdin must be boolean")
    raw_modes = value.get("modes", ["runtime", "diagnostics"])
    if not isinstance(raw_modes, list) or not raw_modes:
        raise PluginManifestError("runtime.modes must be a non-empty list")
    modes = []
    for item in raw_modes:
        mode = str(item or "").strip()
        if mode not in {"runtime", "diagnostics"} or mode in modes:
            raise PluginManifestError("runtime.modes supports unique runtime/diagnostics values")
        modes.append(mode)
    sample_delivery = str(value.get("sample_delivery") or "none").strip()
    if sample_delivery not in {"none", "native_lsl", "declared_transport"}:
        raise PluginManifestError(
            "runtime.sample_delivery must be none, native_lsl, or declared_transport"
        )
    stream_contract = str(value.get("stream_contract") or "manifest").strip()
    if stream_contract not in {"manifest", "status_actual_streams"}:
        raise PluginManifestError(
            "runtime.stream_contract must be manifest or status_actual_streams"
        )
    if stream_contract == "status_actual_streams" and sample_delivery == "none":
        raise PluginManifestError(
            "runtime.stream_contract=status_actual_streams requires sample delivery"
        )
    raw_operation_timeouts = value.get("operation_timeouts_ms", {})
    if not isinstance(raw_operation_timeouts, dict):
        raise PluginManifestError("runtime.operation_timeouts_ms must be a JSON object")
    operation_timeouts: dict[str, int] = {}
    supported_timeout_operations = {
        "initialize",
        "status",
        "start",
        "stop",
        "restart",
        "admin_action",
        "participant_action",
        "participant_ingest",
        "trial_start",
        "trial_stop",
        "trial_marker",
        "session_end",
        "interval_summary",
        "interval_export",
        "publish",
        "shutdown",
        "validate_study_setting",
        # Package 5g.B5: the executable card contract.
        "card_defaults",
        "card_normalize",
        "card_validate_answer",
    }
    for operation, raw_timeout in raw_operation_timeouts.items():
        if operation not in supported_timeout_operations:
            raise PluginManifestError(
                f"runtime.operation_timeouts_ms contains unsupported operation {operation!r}"
            )
        if isinstance(raw_timeout, bool):
            raise PluginManifestError(
                f"runtime.operation_timeouts_ms.{operation} must be an integer"
            )
        try:
            timeout_value = int(raw_timeout)
        except (TypeError, ValueError) as error:
            raise PluginManifestError(
                f"runtime.operation_timeouts_ms.{operation} must be an integer"
            ) from error
        # An upload of a whole session may legitimately take many minutes;
        # every other operation must answer quickly.
        max_timeout = MAX_PUBLISH_TIMEOUT_MS if operation == "publish" else 120_000
        if timeout_value < 50 or timeout_value > max_timeout:
            raise PluginManifestError(
                f"runtime.operation_timeouts_ms.{operation} must be 50..{max_timeout}"
            )
        operation_timeouts[operation] = timeout_value
    raw_actions = value.get("actions", [])
    if not isinstance(raw_actions, list):
        raise PluginManifestError("runtime.actions must be a list")
    actions: list[str] = []
    for item in raw_actions:
        action = str(item or "").strip()
        if action not in {"start", "stop", "restart"} or action in actions:
            raise PluginManifestError("runtime.actions supports unique start/stop/restart values")
        actions.append(action)
    raw_trial_events = value.get("trial_events", [])
    if not isinstance(raw_trial_events, list):
        raise PluginManifestError("runtime.trial_events must be a list")
    trial_events: list[str] = []
    for item in raw_trial_events:
        event = str(item or "").strip()
        if event not in {"start", "stop", "marker", "session_end"} or event in trial_events:
            raise PluginManifestError(
                "runtime.trial_events supports unique start/stop/marker/session_end values"
            )
        trial_events.append(event)
    can_toggle = value.get("can_toggle", True)
    if not isinstance(can_toggle, bool):
        raise PluginManifestError("runtime.can_toggle must be boolean")
    sidecar = value.get("sidecar", {})
    if not isinstance(sidecar, dict):
        raise PluginManifestError("runtime.sidecar must be a JSON object")
    unexpected_sidecar = sorted(set(sidecar) - set(SIDECAR_FIELDS))
    if unexpected_sidecar:
        raise PluginManifestError(
            "runtime.sidecar contains unsupported fields: " + ", ".join(unexpected_sidecar)
        )
    normalized_sidecar = {
        name: _optional_text(sidecar.get(name))
        for name in SIDECAR_FIELDS
        if _optional_text(sidecar.get(name))
    }
    return {
        "entrypoint": relative.as_posix(),
        "protocol": protocol,
        "interactive_stdin": interactive_stdin,
        "modes": modes,
        "sample_delivery": sample_delivery,
        "stream_contract": stream_contract,
        "operation_timeouts_ms": operation_timeouts,
        "actions": actions,
        "trial_events": trial_events,
        "can_toggle": can_toggle,
        "sidecar": normalized_sidecar,
    }


def _normalize_capabilities(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, list):
        raw_items = [(item, {}) for item in value]
    elif isinstance(value, dict):
        raw_items = list(value.items())
    else:
        raise PluginManifestError("capabilities must be an object or a list")

    normalized: dict[str, dict[str, Any]] = {}
    for raw_name, raw_config in raw_items:
        name = str(raw_name or "").strip()
        if not _KEY_PATTERN.fullmatch(name):
            raise PluginManifestError(f"invalid capability name: {name!r}")
        canonical_name = CAPABILITY_ALIASES.get(name, name)
        if canonical_name in RETIRED_CAPABILITIES:
            raise PluginManifestError(
                f"capability {canonical_name!r} is retired: {RETIRED_CAPABILITIES[canonical_name]}"
            )
        if raw_config is True or raw_config is None:
            config: dict[str, Any] = {}
        elif raw_config is False:
            continue
        elif isinstance(raw_config, dict):
            config = deepcopy(raw_config)
        else:
            raise PluginManifestError(f"capability {name!r} must be an object or boolean")
        if canonical_name in normalized:
            raise PluginManifestError(f"duplicate capability: {canonical_name}")
        normalized[canonical_name] = _normalize_capability_config(canonical_name, config)
    if not normalized:
        raise PluginManifestError("capabilities must declare at least one capability")
    return normalized


def _normalize_capability_config(name: str, config: dict[str, Any]) -> dict[str, Any]:
    if name == "acquisition_transport":
        return _normalize_acquisition_transport(config)
    if name == "admin_actions":
        return _normalize_admin_actions(config)
    if name == "participant_actions":
        return _normalize_participant_operation_keys(
            config,
            capability="participant_actions",
            field="actions",
        )
    if name == "participant_ingest":
        return _normalize_participant_operation_keys(
            config,
            capability="participant_ingest",
            field="inputs",
        )
    if name == "runtime_modes":
        return _normalize_runtime_modes(config)
    if name == "readiness_requirements":
        return _normalize_readiness_requirements(config)
    if name == "upload_destination":
        return _normalize_upload_destination(config)
    if name == "credentials":
        return _normalize_credentials(config)
    if name == "card_contract":
        return _normalize_card_contract(config)
    if name == "connection":
        return _normalize_connection_capability(config)
    return config


# Roles an admin action can play in the shared sensor connection panel. The
# core decides which role is the next step (the call to action) from the
# plugin's reported connection state, so every sensor guides the same way.
ADMIN_ACTION_ROLES = ("select", "scan", "measure_signal", "initialize", "auto_reconnect")
# Where an action may appear: "panel" (the default) lets the generic study
# settings and dashboard panels offer it as a button; "configurator" keeps it
# for the plugin's own configurator, which calls it with values it built.
ADMIN_ACTION_SURFACES = ("panel", "configurator")


def _normalize_connection_capability(config: dict[str, Any]) -> dict[str, Any]:
    """How a sensor names its device and signal in the shared connection panel."""

    allowed = {"device_noun_key", "signal_label_key", "setup_label_key", "setup_per_participant"}
    unexpected = sorted(set(config) - allowed)
    if unexpected:
        raise PluginManifestError(
            "connection contains unsupported fields: " + ", ".join(unexpected)
        )
    normalized: dict[str, Any] = {}
    for key in ("device_noun_key", "signal_label_key", "setup_label_key"):
        value = _optional_text(config.get(key))
        if value:
            normalized[key] = value
    per_participant = config.get("setup_per_participant", False)
    if not isinstance(per_participant, bool):
        raise PluginManifestError("connection.setup_per_participant must be boolean")
    normalized["setup_per_participant"] = per_participant
    return normalized


def _normalize_credentials(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the one secret field this plugin needs.

    Declaring it here replaces three separate hardcoded maps that used to list
    the same fact -- which config field a secret lives under, which
    environment variable can override it, and whether a study may carry its
    own -- once each, in three different files, guaranteed to agree only by
    someone remembering to keep them in step.
    """

    presentation = ("label_key", "placeholder_key", "help_key")
    allowed = {"config_field", "env_var", "per_study", *presentation}
    unexpected = sorted(set(config) - allowed)
    if unexpected:
        raise PluginManifestError(
            "credentials contains unsupported fields: " + ", ".join(unexpected)
        )
    config_field = _required_key(config, "config_field", prefix="credentials.")
    env_var = _optional_text(config.get("env_var")) or ""
    per_study = bool(config.get("per_study", False))
    normalized = {"config_field": config_field, "env_var": env_var, "per_study": per_study}
    # Translation keys only: how the secret's input is labelled and explained.
    for key in presentation:
        value = _optional_text(config.get(key))
        if value:
            normalized[key] = value
    return normalized


_QUESTION_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
# What a card may ask the host to supply. ``plugin_actions`` are the card
# actions every plugin declares; ``actuator_plugins`` is the card's selection
# among the plugins whose manifest declares trial start and stop.
CARD_HOST_DATA = frozenset({"plugin_actions", "actuator_plugins"})


def _normalize_card_contract(config: dict[str, Any]) -> dict[str, Any]:
    allowed = {"version", "question_types", "answerless_types", "host_data"}
    unexpected = sorted(set(config) - allowed)
    if unexpected:
        raise PluginManifestError("card_contract contains unsupported fields: " + ", ".join(unexpected))
    if type(config.get("version")) is not int or config["version"] != 1:
        raise PluginManifestError("card_contract.version must be 1")
    types = config.get("question_types")
    if not isinstance(types, list) or not types or any(
        not isinstance(t, str) or not _QUESTION_TYPE_PATTERN.fullmatch(t) for t in types
    ) or len(set(types)) != len(types):
        raise PluginManifestError("card_contract.question_types must contain unique question type names")
    answerless = config.get("answerless_types", [])
    if not isinstance(answerless, list) or any(not isinstance(t, str) or t not in types for t in answerless) or len(set(answerless)) != len(answerless):
        raise PluginManifestError("card_contract.answerless_types must be a unique subset of question_types")
    host_data = config.get("host_data", [])
    if not isinstance(host_data, list) or any(t not in CARD_HOST_DATA for t in host_data) or len(set(host_data)) != len(host_data):
        raise PluginManifestError(
            "card_contract.host_data supports only: " + ", ".join(sorted(CARD_HOST_DATA))
        )
    return {"version": 1, "question_types": list(types), "answerless_types": list(answerless), "host_data": list(host_data)}


def _normalize_readiness_requirements(config: dict[str, Any]) -> dict[str, Any]:
    """What a plugin needs before a study using it can actually deliver results.

    Distinct from `runtime_modes` (runtime-mode platform support, below): this is
    "is the operator's configuration complete", checked before anything starts
    - see `study_readiness_service.py`. A plugin declares what it needs once;
    it does not have to also teach core what "not ready" looks like for it.
    """

    if not config:
        return {}
    allowed = {"requires_secret", "requires_settings", "requires_machine_enabled"}
    unexpected = sorted(set(config) - allowed)
    if unexpected:
        raise PluginManifestError(
            "readiness_requirements contains unsupported fields: " + ", ".join(unexpected)
        )
    raw_settings = config.get("requires_settings")
    if raw_settings is None:
        requires_settings: list[str] = []
    elif isinstance(raw_settings, list) and raw_settings and all(
        isinstance(item, str) and _KEY_PATTERN.fullmatch(item) for item in raw_settings
    ):
        requires_settings = [str(item) for item in raw_settings]
    else:
        raise PluginManifestError(
            "readiness_requirements.requires_settings must be a non-empty list of field names"
        )
    return {
        "requires_secret": bool(config.get("requires_secret", False)),
        "requires_settings": requires_settings,
        "requires_machine_enabled": bool(config.get("requires_machine_enabled", False)),
    }


def _normalize_runtime_modes(config: dict[str, Any]) -> dict[str, Any]:
    """Validate optional, manifest-driven runtime-mode platform support.

    Renamed from `readiness` in api_version 5 (Phase 3.4) -- that name
    collided with the unrelated `readiness_requirements` capability ("is the
    operator's config complete", a different question). Schema unchanged.
    """

    if not config:
        return {}
    allowed = {"mode_setting", "default_mode", "platform_modes"}
    unexpected = sorted(set(config) - allowed)
    if unexpected:
        raise PluginManifestError(
            "runtime_modes contains unsupported fields: " + ", ".join(unexpected)
        )

    mode_setting = _required_key(config, "mode_setting", prefix="runtime_modes.")
    default_mode = _required_key(config, "default_mode", prefix="runtime_modes.")
    raw_platform_modes = config.get("platform_modes")
    if not isinstance(raw_platform_modes, dict) or not raw_platform_modes:
        raise PluginManifestError("runtime_modes.platform_modes must be a non-empty object")
    if "default" not in raw_platform_modes:
        raise PluginManifestError("runtime_modes.platform_modes must declare a default target")

    platform_modes: dict[str, list[str]] = {}
    for raw_target, raw_modes in raw_platform_modes.items():
        target = str(raw_target or "").strip().lower()
        if not _PLATFORM_TARGET_PATTERN.fullmatch(target):
            raise PluginManifestError(
                f"invalid runtime_modes platform target: {raw_target!r}"
            )
        if not isinstance(raw_modes, list) or not raw_modes:
            raise PluginManifestError(
                f"runtime_modes.platform_modes.{target} must be a non-empty list"
            )
        modes: list[str] = []
        for raw_mode in raw_modes:
            mode = str(raw_mode or "").strip()
            if not _KEY_PATTERN.fullmatch(mode):
                raise PluginManifestError(
                    f"runtime_modes.platform_modes.{target} contains an invalid mode"
                )
            if mode in modes:
                raise PluginManifestError(
                    f"runtime_modes.platform_modes.{target} contains duplicate mode {mode!r}"
                )
            modes.append(mode)
        platform_modes[target] = modes

    if default_mode not in platform_modes["default"]:
        raise PluginManifestError(
            "runtime_modes.default_mode must be allowed by runtime_modes.platform_modes.default"
        )
    return {
        "mode_setting": mode_setting,
        "default_mode": default_mode,
        "platform_modes": platform_modes,
    }


def _normalize_upload_destination(config: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "destination",
        "default_enabled",
        "requires_valid_result",
        "publish_on_attention",
        "republish_on_degraded",
        "purge_verified_sources",
        "discovered_settings",
        "legacy",
    }
    unexpected = sorted(set(config) - allowed)
    if unexpected:
        raise PluginManifestError(
            "upload_destination contains unsupported fields: " + ", ".join(unexpected)
        )
    destination = _required_key(
        config,
        "destination",
        prefix="upload_destination.",
    )
    normalized: dict[str, Any] = {"destination": destination}
    defaults = {
        "default_enabled": False,
        "requires_valid_result": True,
        "publish_on_attention": False,
        "republish_on_degraded": False,
        "purge_verified_sources": False,
    }
    for name, default in defaults.items():
        value = config.get(name, default)
        if not isinstance(value, bool):
            raise PluginManifestError(f"upload_destination.{name} must be boolean")
        normalized[name] = value

    discovered = config.get("discovered_settings", [])
    if (
        not isinstance(discovered, list)
        or any(not isinstance(name, str) or not _KEY_PATTERN.fullmatch(name) for name in discovered)
        or len(set(discovered)) != len(discovered)
    ):
        raise PluginManifestError(
            "upload_destination.discovered_settings must be a list of unique snake_case keys"
        )
    normalized["discovered_settings"] = list(discovered)

    legacy = config.get("legacy", {})
    if not isinstance(legacy, dict):
        raise PluginManifestError("upload_destination.legacy must be a JSON object")
    legacy_unexpected = sorted(set(legacy) - {"enabled_field", "settings"})
    if legacy_unexpected:
        raise PluginManifestError(
            "upload_destination.legacy contains unsupported fields: "
            + ", ".join(legacy_unexpected)
        )
    enabled_field = _optional_text(legacy.get("enabled_field"))
    if enabled_field and not _KEY_PATTERN.fullmatch(enabled_field):
        raise PluginManifestError(
            "upload_destination.legacy.enabled_field must be a snake_case key"
        )
    raw_settings = legacy.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise PluginManifestError(
            "upload_destination.legacy.settings must be a JSON object"
        )
    legacy_settings: dict[str, str] = {}
    for raw_name, raw_field in raw_settings.items():
        name = str(raw_name or "").strip()
        field = _optional_text(raw_field)
        if not _KEY_PATTERN.fullmatch(name) or not field or not _KEY_PATTERN.fullmatch(field):
            raise PluginManifestError(
                "upload_destination.legacy.settings must map snake_case keys to snake_case fields"
            )
        legacy_settings[name] = field
    normalized["legacy"] = {
        "enabled_field": enabled_field,
        "settings": legacy_settings,
    }
    return normalized


def _normalize_participant_operation_keys(
    config: dict[str, Any],
    *,
    capability: str,
    field: str,
) -> dict[str, list[str]]:
    """Normalize the closed allow-list used by participant-facing routes."""

    unexpected = sorted(set(config) - {field})
    if unexpected:
        raise PluginManifestError(
            f"{capability} contains unsupported fields: " + ", ".join(unexpected)
        )
    raw_keys = config.get(field)
    if not isinstance(raw_keys, list) or not raw_keys:
        raise PluginManifestError(f"{capability}.{field} must be a non-empty list")

    keys: list[str] = []
    for index, raw_key in enumerate(raw_keys, start=1):
        key = str(raw_key or "").strip()
        if not _KEY_PATTERN.fullmatch(key):
            raise PluginManifestError(
                f"{capability}.{field}[{index}] must be a snake_case key"
            )
        if key in keys:
            raise PluginManifestError(f"duplicate {capability} key: {key}")
        keys.append(key)
    return {field: keys}


def _normalize_acquisition_transport(config: dict[str, Any]) -> dict[str, Any]:
    transport = _required_text(config, "transport", prefix="acquisition_transport.")
    delivery = _required_text(config, "delivery", prefix="acquisition_transport.")
    if transport not in ACQUISITION_TRANSPORTS:
        raise PluginManifestError(
            "acquisition_transport.transport must be one of: "
            + ", ".join(sorted(ACQUISITION_TRANSPORTS))
        )
    if delivery not in ACQUISITION_DELIVERIES:
        raise PluginManifestError(
            "acquisition_transport.delivery must be native_lsl or host_lsl_bridge"
        )

    expected_delivery = (
        "native_lsl" if transport in _NATIVE_LSL_TRANSPORTS else "host_lsl_bridge"
    )
    if delivery != expected_delivery:
        raise PluginManifestError(
            f"acquisition transport {transport!r} requires delivery {expected_delivery!r}"
        )

    allowed_keys = {"transport", "delivery"}
    normalized: dict[str, Any] = {"transport": transport, "delivery": delivery}
    if transport == "browser_https":
        allowed_keys.update(_BROWSER_TRANSPORT_REQUIREMENTS)
        allowed_keys.add("source_timestamp_fields")
        for requirement in _BROWSER_TRANSPORT_REQUIREMENTS:
            if config.get(requirement) is not True:
                raise PluginManifestError(
                    f"acquisition_transport.{requirement} must be true for browser_https"
                )
            normalized[requirement] = True
        raw_timestamp_fields = config.get(
            "source_timestamp_fields",
            sorted(_BROWSER_SOURCE_TIMESTAMP_FIELDS),
        )
        if not isinstance(raw_timestamp_fields, list) or not raw_timestamp_fields:
            raise PluginManifestError(
                "acquisition_transport.source_timestamp_fields must be a non-empty list"
            )
        timestamp_fields: list[str] = []
        for raw_field in raw_timestamp_fields:
            field = str(raw_field or "").strip()
            if field not in _BROWSER_SOURCE_TIMESTAMP_FIELDS:
                raise PluginManifestError(
                    "acquisition_transport.source_timestamp_fields may contain only: "
                    + ", ".join(sorted(_BROWSER_SOURCE_TIMESTAMP_FIELDS))
                )
            if field in timestamp_fields:
                raise PluginManifestError(
                    f"duplicate acquisition source timestamp field: {field}"
                )
            timestamp_fields.append(field)
        normalized["source_timestamp_fields"] = timestamp_fields
    unexpected = sorted(set(config) - allowed_keys)
    if unexpected:
        raise PluginManifestError(
            "acquisition_transport contains unsupported fields: " + ", ".join(unexpected)
        )
    return normalized


def _normalize_admin_actions(config: dict[str, Any]) -> dict[str, Any]:
    raw_actions = config.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise PluginManifestError("admin_actions.actions must be a non-empty list")
    unexpected = sorted(set(config) - {"actions", "rendered_by_dashboard"})
    if unexpected:
        raise PluginManifestError(
            "admin_actions contains unsupported fields: " + ", ".join(unexpected)
        )
    rendered_by_dashboard = config.get("rendered_by_dashboard", False)
    if not isinstance(rendered_by_dashboard, bool):
        raise PluginManifestError("admin_actions.rendered_by_dashboard must be boolean")

    actions: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_roles: set[str] = set()
    allowed_fields = {
        "key",
        "label",
        "description",
        "confirm",
        "danger",
        "payload_schema",
        "any_of_required",
        "instances",
        "role",
        "surface",
    }
    for index, raw_action in enumerate(raw_actions, start=1):
        if not isinstance(raw_action, dict):
            raise PluginManifestError(f"admin_actions.actions[{index}] must be a JSON object")
        extra = sorted(set(raw_action) - allowed_fields)
        if extra:
            raise PluginManifestError(
                f"admin_actions.actions[{index}] contains unsupported fields: "
                + ", ".join(extra)
            )
        key = _required_key(
            raw_action,
            "key",
            prefix=f"admin_actions.actions[{index}].",
        )
        if key in seen_keys:
            raise PluginManifestError(f"duplicate admin action key: {key}")
        seen_keys.add(key)
        action: dict[str, Any] = {
            "key": key,
            "label": _required_text(
                raw_action,
                "label",
                prefix=f"admin_actions.actions[{index}].",
            ),
        }
        description = _optional_text(raw_action.get("description"))
        if description:
            action["description"] = description
        for flag in ("confirm", "danger"):
            if flag in raw_action:
                if not isinstance(raw_action[flag], bool):
                    raise PluginManifestError(
                        f"admin_actions.actions[{index}].{flag} must be boolean"
                    )
                action[flag] = raw_action[flag]
        payload_schema = _normalize_admin_action_payload_schema(
            raw_action.get("payload_schema"),
            index=index,
        )
        action["payload_schema"] = payload_schema
        action["any_of_required"] = _normalize_admin_action_any_of(
            raw_action.get("any_of_required"),
            payload_schema,
            index=index,
        )
        instances = _normalize_admin_action_instances(
            raw_action.get("instances"),
            payload_schema,
            index=index,
        )
        if instances:
            action["instances"] = instances
        if "role" in raw_action:
            role = str(raw_action.get("role") or "").strip()
            if role not in ADMIN_ACTION_ROLES:
                raise PluginManifestError(
                    f"admin_actions.actions[{index}].role must be one of "
                    + ", ".join(ADMIN_ACTION_ROLES)
                )
            if role in seen_roles:
                raise PluginManifestError(f"duplicate admin action role: {role}")
            if role == "select" and (instances or {}).get("presentation") != "select":
                raise PluginManifestError(
                    f"admin_actions.actions[{index}] with role select needs instances.presentation select"
                )
            seen_roles.add(role)
            action["role"] = role
        if "surface" in raw_action:
            surface = raw_action.get("surface")
            if surface not in ADMIN_ACTION_SURFACES:
                raise PluginManifestError(
                    f"admin_actions.actions[{index}].surface must be one of "
                    + ", ".join(ADMIN_ACTION_SURFACES)
                )
            action["surface"] = surface
        actions.append(action)
    result: dict[str, Any] = {"actions": actions}
    if rendered_by_dashboard:
        result["rendered_by_dashboard"] = True
    return result


def _normalize_admin_action_payload_schema(
    value: Any,
    *,
    index: int,
) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginManifestError(
            f"admin_actions.actions[{index}].payload_schema must be a JSON object"
        )
    schema: dict[str, dict[str, Any]] = {}
    allowed = {"type", "required", "minimum", "maximum", "max_length", "enum"}
    for raw_name, raw_field in value.items():
        name = str(raw_name or "").strip()
        if not _KEY_PATTERN.fullmatch(name):
            raise PluginManifestError(
                f"admin_actions.actions[{index}].payload_schema has an invalid field name"
            )
        if not isinstance(raw_field, dict):
            raise PluginManifestError(
                f"admin_actions.actions[{index}].payload_schema.{name} must be an object"
            )
        extra = sorted(set(raw_field) - allowed)
        if extra:
            raise PluginManifestError(
                f"admin_actions.actions[{index}].payload_schema.{name} contains unsupported fields: "
                + ", ".join(extra)
            )
        field_type = _optional_text(raw_field.get("type"))
        if field_type not in {"string", "integer", "number", "boolean"}:
            raise PluginManifestError(
                f"admin_actions.actions[{index}].payload_schema.{name}.type is unsupported"
            )
        field: dict[str, Any] = {
            "type": field_type,
            "required": bool(raw_field.get("required", False)),
        }
        if "required" in raw_field and not isinstance(raw_field["required"], bool):
            raise PluginManifestError(
                f"admin_actions.actions[{index}].payload_schema.{name}.required must be boolean"
            )
        if field_type == "string":
            field["max_length"] = _positive_int(
                raw_field.get("max_length", 512),
                f"admin_actions.actions[{index}].payload_schema.{name}.max_length",
            )
        elif "max_length" in raw_field:
            raise PluginManifestError(
                f"admin_actions.actions[{index}].payload_schema.{name}.max_length requires string"
            )
        for limit in ("minimum", "maximum"):
            if limit not in raw_field:
                continue
            raw_limit = raw_field[limit]
            if (
                field_type not in {"integer", "number"}
                or isinstance(raw_limit, bool)
                or not isinstance(raw_limit, (int, float))
                or not math.isfinite(float(raw_limit))
            ):
                raise PluginManifestError(
                    f"admin_actions.actions[{index}].payload_schema.{name}.{limit} must be numeric"
                )
            field[limit] = raw_limit
        if "enum" in raw_field:
            enum = raw_field["enum"]
            if not isinstance(enum, list) or not enum:
                raise PluginManifestError(
                    f"admin_actions.actions[{index}].payload_schema.{name}.enum must be a non-empty list"
                )
            field["enum"] = deepcopy(enum)
        schema[name] = field
    return schema


def _normalize_admin_action_any_of(
    value: Any,
    schema: dict[str, dict[str, Any]],
    *,
    index: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise PluginManifestError(
            f"admin_actions.actions[{index}].any_of_required must be a non-empty list"
        )
    fields: list[str] = []
    for raw_field in value:
        field = str(raw_field or "").strip()
        if field not in schema:
            raise PluginManifestError(
                f"admin_actions.actions[{index}].any_of_required names an undeclared field"
            )
        if field not in fields:
            fields.append(field)
    return fields


def _normalize_admin_action_instances(
    value: Any,
    schema: dict[str, dict[str, Any]],
    *,
    index: int,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not schema:
        raise PluginManifestError(
            f"admin_actions.actions[{index}].instances requires payload_schema"
        )
    if not isinstance(value, dict):
        raise PluginManifestError(
            f"admin_actions.actions[{index}].instances must be a JSON object"
        )
    extra = sorted(set(value) - {"status_paths", "payload_map", "label_fields", "presentation"})
    if extra:
        raise PluginManifestError(
            f"admin_actions.actions[{index}].instances contains unsupported fields: "
            + ", ".join(extra)
        )
    status_paths = _normalize_status_paths(
        value.get("status_paths"),
        f"admin_actions.actions[{index}].instances.status_paths",
    )
    raw_map = value.get("payload_map")
    if not isinstance(raw_map, dict) or not raw_map:
        raise PluginManifestError(
            f"admin_actions.actions[{index}].instances.payload_map must be a non-empty object"
        )
    payload_map: dict[str, str] = {}
    for raw_target, raw_source in raw_map.items():
        target = str(raw_target or "").strip()
        if target not in schema:
            raise PluginManifestError(
                f"admin_actions.actions[{index}].instances.payload_map targets an undeclared field"
            )
        source = _normalize_status_path(
            raw_source,
            f"admin_actions.actions[{index}].instances.payload_map.{target}",
        )
        payload_map[target] = source
    label_fields = _normalize_status_paths(
        value.get("label_fields", []),
        f"admin_actions.actions[{index}].instances.label_fields",
        allow_empty=True,
    )
    presentation = value.get("presentation", "buttons")
    if presentation not in {"buttons", "select"}:
        raise PluginManifestError("admin action instances.presentation must be buttons or select")
    return {
        **({"presentation": presentation} if "presentation" in value else {}),
        "status_paths": status_paths,
        "payload_map": payload_map,
        "label_fields": label_fields,
    }


def _normalize_status_paths(value: Any, name: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        suffix = "a list" if allow_empty else "a non-empty list"
        raise PluginManifestError(f"{name} must be {suffix}")
    paths: list[str] = []
    for raw_path in value:
        path = _normalize_status_path(raw_path, name)
        if path not in paths:
            paths.append(path)
    return paths


def _normalize_status_path(value: Any, name: str) -> str:
    path = _optional_text(value)
    if not path or not _TIMELINE_CHANNEL_PATTERN.fullmatch(path):
        raise PluginManifestError(f"{name} contains an invalid status path")
    return path


def _normalize_settings(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginManifestError("settings must be a JSON object")
    extra = sorted(set(value) - {"machine", "study", "card_actions"})
    if extra:
        raise PluginManifestError("settings contains unsupported sections: " + ", ".join(extra))
    result: dict[str, dict[str, Any]] = {}
    for section_name in ("machine", "study", "card_actions"):
        section = value.get(section_name, {})
        if not isinstance(section, dict):
            raise PluginManifestError(f"settings.{section_name} must be a JSON object")
        result[section_name] = deepcopy(section)
    return result


def _normalize_lifecycle(value: Any) -> dict[str, bool]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginManifestError("lifecycle must be a JSON object")
    unexpected = sorted(set(value) - {"reinitialize_on_disable"})
    if unexpected:
        raise PluginManifestError(
            "lifecycle contains unsupported fields: " + ", ".join(unexpected)
        )
    reinitialize = value.get("reinitialize_on_disable", False)
    if not isinstance(reinitialize, bool):
        raise PluginManifestError("lifecycle.reinitialize_on_disable must be boolean")
    return {"reinitialize_on_disable": reinitialize}


def _normalize_ui_visibility(value: Any) -> dict[str, bool]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginManifestError("ui.visibility must be a JSON object")
    unexpected = sorted(set(value) - set(UI_VISIBILITY_AREAS))
    if unexpected:
        raise PluginManifestError(
            "ui.visibility contains unsupported fields: " + ", ".join(unexpected)
        )
    visibility: dict[str, bool] = {}
    for area in UI_VISIBILITY_AREAS:
        flag = value.get(area, True)
        if not isinstance(flag, bool):
            raise PluginManifestError(f"ui.visibility.{area} must be boolean")
        visibility[area] = flag
    return visibility


def _normalize_ui_extensions(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginManifestError("ui.extensions must be a JSON object")
    unexpected = sorted(set(value) - set(UI_EXTENSION_SURFACES))
    if unexpected:
        raise PluginManifestError(
            "ui.extensions contains unsupported fields: " + ", ".join(unexpected)
        )
    extensions: dict[str, str] = {}
    for surface in UI_EXTENSION_SURFACES:
        if surface not in value:
            continue
        extensions[surface] = _normalize_ui_asset_path(
            value[surface],
            f"ui.extensions.{surface}",
            javascript_only=True,
        )
    return extensions


def _normalize_ui_assets(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PluginManifestError("ui.assets must be a list")
    assets: list[str] = []
    for index, raw_path in enumerate(value, start=1):
        path = _normalize_ui_asset_path(raw_path, f"ui.assets[{index}]")
        if path in assets:
            raise PluginManifestError("ui.assets contains duplicates")
        assets.append(path)
    return assets


def _normalize_ui_asset_path(
    value: Any,
    name: str,
    *,
    javascript_only: bool = False,
) -> str:
    path = _optional_text(value)
    pattern = _UI_EXTENSION_PATTERN if javascript_only else _UI_ASSET_PATTERN
    if not path or not pattern.fullmatch(path):
        suffixes = ".js" if javascript_only else ".js or .css"
        raise PluginManifestError(f"{name} must be a relative POSIX {suffixes} path")
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or any(part in {"", ".", ".."} for part in pure_path.parts):
        raise PluginManifestError(f"{name} must remain inside the plugin directory")
    return pure_path.as_posix()


def _normalize_timeline_metadata(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {"lane_aliases": [], "preferred_channels": []}
    if not isinstance(value, dict):
        raise PluginManifestError("ui.timeline must be a JSON object")
    unexpected = sorted(set(value) - {"lane_aliases", "preferred_channels"})
    if unexpected:
        raise PluginManifestError(
            "ui.timeline contains unsupported fields: " + ", ".join(unexpected)
        )

    result: dict[str, list[str]] = {}
    for field in ("lane_aliases", "preferred_channels"):
        raw_items = value.get(field, [])
        if not isinstance(raw_items, list):
            raise PluginManifestError(f"ui.timeline.{field} must be a list")
        items: list[str] = []
        for index, raw_item in enumerate(raw_items, start=1):
            item = _optional_text(raw_item)
            if not item or not _TIMELINE_CHANNEL_PATTERN.fullmatch(item):
                raise PluginManifestError(
                    f"ui.timeline.{field}[{index}] contains an invalid identifier"
                )
            if item in items:
                raise PluginManifestError(f"ui.timeline.{field} contains duplicates")
            items.append(item)
        result[field] = items
    return result


def _normalize_streams(value: Any) -> list[dict[str, Any]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise PluginManifestError("streams must be a list")
    streams: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_source_ids: set[str] = set()
    for index, raw_stream in enumerate(value, start=1):
        if not isinstance(raw_stream, dict):
            raise PluginManifestError(f"stream {index} must be a JSON object")
        key = _required_key(raw_stream, "key", prefix=f"streams[{index}].")
        source_id = _required_text(raw_stream, "source_id", prefix=f"streams[{index}].")
        if key in seen_keys:
            raise PluginManifestError(f"duplicate stream key: {key}")
        if source_id in seen_source_ids:
            raise PluginManifestError(f"duplicate stream source_id: {source_id}")
        seen_keys.add(key)
        seen_source_ids.add(source_id)
        rate = raw_stream.get("nominal_rate_hz", 0)
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not math.isfinite(float(rate))
            or rate < 0
        ):
            raise PluginManifestError(f"streams[{index}].nominal_rate_hz must be zero or positive")
        if not isinstance(raw_stream.get("may_be_empty", False), bool):
            raise PluginManifestError(f"streams[{index}].may_be_empty must be true or false")
        estimated_rate = raw_stream.get("estimated_bytes_per_second")
        if estimated_rate is not None and (
            isinstance(estimated_rate, bool)
            or not isinstance(estimated_rate, (int, float))
            or not math.isfinite(float(estimated_rate))
            or estimated_rate <= 0
        ):
            raise PluginManifestError(f"streams[{index}].estimated_bytes_per_second must be positive and finite")
        channels = raw_stream.get("channels", [])
        if not isinstance(channels, list) or not channels:
            raise PluginManifestError(f"streams[{index}].channels must be a non-empty list")
        if any(not isinstance(channel, str) or not channel.strip() for channel in channels):
            raise PluginManifestError(f"streams[{index}].channels must contain non-empty strings")
        if len({channel.strip() for channel in channels}) != len(channels):
            raise PluginManifestError(f"streams[{index}].channels must be unique")
        channel_format = _optional_text(raw_stream.get("channel_format"))
        if channel_format not in _LSL_CHANNEL_FORMATS:
            raise PluginManifestError(
                f"streams[{index}].channel_format must be one of: "
                + ", ".join(sorted(_LSL_CHANNEL_FORMATS))
            )
        sequence_channel = _optional_text(raw_stream.get("sequence_channel"))
        if sequence_channel and sequence_channel not in channels:
            raise PluginManifestError(
                f"streams[{index}].sequence_channel must name a declared channel"
            )
        channel_units = raw_stream.get("channel_units")
        if not isinstance(channel_units, list) or len(channel_units) != len(channels):
            raise PluginManifestError(
                f"streams[{index}].channel_units must match the channel list"
            )
        if any(not isinstance(unit, str) or not unit.strip() for unit in channel_units):
            raise PluginManifestError(f"streams[{index}].channel_units must contain strings")
        try:
            timing = normalize_stream_timing(
                raw_stream.get("timing"), index=index, channels=channels, channel_units=channel_units,
            )
        except SensorContractError as error:
            raise PluginManifestError(str(error)) from error
        streams.append(
            {
                **deepcopy(raw_stream),
                "key": key,
                "source_id": source_id,
                "nominal_rate_hz": rate,
                "clock_domain": _optional_text(raw_stream.get("clock_domain")) or "server",
                "channel_format": channel_format,
                "sequence_channel": sequence_channel,
                "channels": deepcopy(channels),
                "channel_units": deepcopy(channel_units),
                "timing": timing,
            }
        )
    return streams


def _validate_capability_contracts(
    capabilities: dict[str, dict[str, Any]],
    streams: list[dict[str, Any]],
    settings: dict[str, dict[str, Any]],
) -> None:
    for name in capabilities.get("upload_destination", {}).get("discovered_settings", []):
        field = settings["study"].get(name)
        if not isinstance(field, dict) or field.get("type") != "string":
            raise PluginManifestError(
                "upload_destination.discovered_settings must name declared study string fields"
            )
    if "lsl_stream_provider" in capabilities and not streams:
        raise PluginManifestError("lsl_stream_provider requires at least one stream")
    if "recording_source" in capabilities and not streams:
        raise PluginManifestError("recording_source requires at least one stream")
    if "recording_source" in capabilities and "lsl_stream_provider" not in capabilities:
        raise PluginManifestError("recording_source requires lsl_stream_provider")
    if "recording_source" in capabilities:
        _reject_canonical_recording_disable_settings(settings)
        recording_source = capabilities["recording_source"]
        sample_policy = recording_source.get("start_sample_policy", "all_regular")
        if sample_policy not in {"all_regular", "primary_only", "headers_only"}:
            raise PluginManifestError(
                "recording_source.start_sample_policy must be all_regular, primary_only, or headers_only"
            )
        recording_source["start_sample_policy"] = sample_policy
        primary_stream = _optional_text(recording_source.get("primary_stream"))
        if primary_stream and primary_stream not in {stream["key"] for stream in streams}:
            raise PluginManifestError(
                "recording_source.primary_stream must name a declared stream"
            )
        if "study_sensor" in capabilities and not primary_stream:
            raise PluginManifestError(
                "study_sensor recording sources require recording_source.primary_stream"
            )
        for stream in streams:
            stream["primary"] = bool(primary_stream and stream["key"] == primary_stream)
    if "study_sensor" in capabilities and "recording_source" in capabilities:
        missing = [
            capability
            for capability in ("lsl_stream_provider", "backup_projection")
            if capability not in capabilities
        ]
        if missing:
            raise PluginManifestError(
                "study_sensor recording sources require capabilities: "
                + ", ".join(missing)
            )
    try:
        if "backup_projection" in capabilities:
            capabilities["backup_projection"] = normalize_backup_projection(
                capabilities["backup_projection"], streams,
            )
        if "live_view" in capabilities:
            capabilities["live_view"] = normalize_live_view(capabilities["live_view"], streams)
        validate_sensor_contract(capabilities, streams)
    except SensorContractError as error:
        raise PluginManifestError(str(error)) from error
    study_sensor = capabilities.get("study_sensor")
    if study_sensor is not None:
        for key in ("default_enabled", "default_required"):
            if key in study_sensor and not isinstance(study_sensor[key], bool):
                raise PluginManifestError(f"study_sensor.{key} must be boolean")
        start_condition = study_sensor.get("start_condition")
        if start_condition is not None:
            if not isinstance(start_condition, dict) or set(start_condition) != {"path", "equals"}:
                raise PluginManifestError(
                    "study_sensor.start_condition must contain path and equals"
                )
            path = start_condition["path"]
            expected = start_condition["equals"]
            if not isinstance(path, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*", path):
                raise PluginManifestError("study_sensor.start_condition.path must be a dotted status field")
            if not isinstance(expected, bool) and not (
                isinstance(expected, str) and expected.strip()
            ):
                raise PluginManifestError("study_sensor.start_condition.equals must be a boolean or non-empty string")


def _reject_canonical_recording_disable_settings(
    settings: dict[str, dict[str, Any]],
) -> None:
    for section_name, section in settings.items():
        for field_name, raw_field in section.items():
            field = raw_field if isinstance(raw_field, dict) else {}
            candidates = (str(field_name), str(field.get("path") or field_name))
            if any(_setting_token(candidate) in _CANONICAL_RECORDING_DISABLE_TOKENS for candidate in candidates):
                raise PluginManifestError(
                    "recording_source cannot expose a setting that disables canonical "
                    f"stream recording: settings.{section_name}.{field_name}"
                )


def _setting_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _normalize_backpressure(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PluginManifestError("backpressure must be a JSON object")
    return {
        "max_in_flight": _positive_int(value.get("max_in_flight", 1), "backpressure.max_in_flight"),
        "drop_policy": _optional_text(value.get("drop_policy")) or "latest_status_wins",
    }


def _default_clock_domain(streams: list[dict[str, Any]]) -> str:
    domains = {str(stream.get("clock_domain") or "server") for stream in streams}
    return domains.pop() if len(domains) == 1 else "mixed" if domains else "server"


def _required_key(payload: dict[str, Any], name: str, *, prefix: str = "") -> str:
    value = _required_text(payload, name, prefix=prefix)
    if not _KEY_PATTERN.fullmatch(value):
        raise PluginManifestError(f"{prefix}{name} must use lowercase snake_case")
    return value


def _required_text(payload: dict[str, Any], name: str, *, prefix: str = "") -> str:
    value = _optional_text(payload.get(name))
    if not value:
        raise PluginManifestError(f"{prefix}{name} must be a non-empty string")
    return value


def _optional_text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise PluginManifestError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise PluginManifestError(f"{name} must be a positive integer") from error
    if result <= 0:
        raise PluginManifestError(f"{name} must be a positive integer")
    return result


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise PluginManifestError(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise PluginManifestError(f"{name} must be a non-negative integer") from error
    if result < 0:
        raise PluginManifestError(f"{name} must be a non-negative integer")
    return result
