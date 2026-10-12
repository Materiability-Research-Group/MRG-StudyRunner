from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import ANY, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from support.fixture_plugin import FixturePluginRootMixin, write_driver_py

from study_runner.plugin_framework.plugin_catalog import (
    PluginManifestError,
    discover_plugin_catalog,
    validate_and_normalize_manifest,
)
from study_runner.contracts.plugin_api import PluginContext
from study_runner.plugin_framework.process_host import get_process_runtime
from study_runner.plugin_framework.registry import run_admin_action
from study_runner.plugin_framework.registry import get_plugin_catalog_payload
from study_runner.apps.server.routes.helpers import _plugin_context
from study_runner.apps.server.routes.plugins import bp as plugins_blueprint


def _manifest(plugin_key: str, *, source_id: str | None = None) -> dict:
    streams = []
    capabilities: dict[str, dict] = {"health": {}}
    if source_id:
        capabilities["lsl_stream_provider"] = {}
        streams = [
            {
                "key": "values",
                "source_id": source_id,
                "nominal_rate_hz": 10,
                "clock_domain": "lsl",
                "channel_format": "float32",
                "channels": ["value"],
                "channel_units": ["arbitrary_unit"],
            }
        ]
    return {
        "api_version": 5,
        "plugin_key": plugin_key,
        "version": "1.0.0",
        "category": "test",
        "config_key": plugin_key,
        "runtime": {"entrypoint": "driver.py", "protocol": "study-runner-stdio/v1"},
        "ui": {"label": plugin_key, "order": 1},
        "capabilities": capabilities,
        "streams": streams,
        "settings": {"machine": {}, "study": {}, "card_actions": {}},
    }


def _card_manifest(plugin_key: str, question_type: str) -> dict:
    payload = _manifest(plugin_key)
    payload["category"] = "card"
    payload["capabilities"] = {
        "card_contract": {
            "version": 1,
            "question_types": [question_type],
            "answerless_types": [],
            "host_data": [],
        }
    }
    payload["ui"]["extensions"] = {"card": "card.js"}
    payload["ui"]["assets"] = ["card.js"]
    return payload


class PluginManifestTests(unittest.TestCase):
    def test_sensor_start_rules_are_validated_and_manifest_driven(self) -> None:
        path = PROJECT_ROOT / "study_runner" / "plugins" / "sensors" / "brainbit" / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        normalized = validate_and_normalize_manifest(payload, directory_name="brainbit")
        self.assertEqual(
            normalized["capability_config"]["study_sensor"]["start_condition"],
            {"path": "connection.ready", "equals": True},
        )
        self.assertEqual(
            normalized["capability_config"]["recording_source"]["start_sample_policy"],
            "all_regular",
        )
        payload["capabilities"]["study_sensor"]["start_condition"]["path"] = "connection.missing-field!"
        with self.assertRaisesRegex(PluginManifestError, "start_condition.path"):
            validate_and_normalize_manifest(payload, directory_name="brainbit")

    def test_every_recording_sensor_declares_a_primary_stream_for_its_mode(self) -> None:
        sensor_root = PROJECT_ROOT / "study_runner" / "plugins" / "sensors"
        manifests = list(sensor_root.glob("*/manifest.json"))
        manifests.append(PROJECT_ROOT.parent / "tools" / "plugin_templates" / "sensors" / "manifest.json")
        for path in manifests:
            with self.subTest(plugin=path.parent.name):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                capabilities = manifest["capabilities"]
                if "recording_source" not in capabilities:
                    continue
                self.assertIn("study_sensor", capabilities)
                self.assertIn("lsl_stream_provider", capabilities)
                primary = capabilities["recording_source"]["primary_stream"]
                streams = [stream for stream in manifest["streams"] if stream["key"] == primary]
                self.assertEqual(len(streams), 1)
                self.assertGreaterEqual(float(streams[0]["nominal_rate_hz"]), 0)

    def test_every_destination_waits_for_completed_local_data(self) -> None:
        destination_root = PROJECT_ROOT / "study_runner" / "plugins" / "destinations"
        manifests = list(destination_root.glob("*/manifest.json"))
        manifests.append(PROJECT_ROOT.parent / "tools" / "plugin_templates" / "destinations" / "manifest.json")
        for path in manifests:
            with self.subTest(plugin=path.parent.name):
                capability = json.loads(path.read_text(encoding="utf-8"))["capabilities"]["upload_destination"]
                self.assertTrue(capability["requires_valid_result"])
                self.assertFalse(capability["publish_on_attention"])
                self.assertFalse(capability["republish_on_degraded"])

    def test_card_contract_is_closed_and_normalized(self) -> None:
        payload = _card_manifest("fixture_card", "fixture-card")
        payload["capabilities"]["card_contract"].update(
            {"answerless_types": ["fixture-card"], "host_data": ["plugin_actions"]}
        )
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture_card")
        self.assertEqual(
            manifest["capability_config"]["card_contract"],
            {
                "version": 1,
                "question_types": ["fixture-card"],
                "answerless_types": ["fixture-card"],
                "host_data": ["plugin_actions"],
            },
        )

        for field, value, message in (
            ("version", 2, "version must be 1"),
            ("answerless_types", ["other"], "unique subset"),
            ("host_data", ["secrets"], "supports only: actuator_plugins, plugin_actions"),
        ):
            invalid = _card_manifest("fixture_card", "fixture-card")
            invalid["capabilities"]["card_contract"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(PluginManifestError, message):
                validate_and_normalize_manifest(invalid, directory_name="fixture_card")

    def test_ui_visibility_defaults_every_supported_surface_to_visible(self) -> None:
        manifest = validate_and_normalize_manifest(
            _manifest("fixture"),
            directory_name="fixture",
        )
        self.assertEqual(
            manifest["ui"]["visibility"],
            {
                "dashboard": True,
                "settings_hub": True,
                "study_settings": True,
                "destination_settings": True,
            },
        )

    def test_legacy_entry_point_is_ignored(self) -> None:
        payload = _manifest("fixture")
        payload["entry_point"] = "legacy_module:PLUGIN"

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertNotIn("entry_point", manifest)
        self.assertEqual(manifest["runtime"]["entrypoint"], "driver.py")

    def test_upload_destination_policy_and_legacy_aliases_are_normalized(self) -> None:
        payload = _manifest("fixture_export")
        payload["capabilities"]["upload_destination"] = {
            "destination": "fixture_export",
            "publish_on_attention": True,
            "legacy": {
                "enabled_field": "fixture_export_enabled",
                "settings": {"bucket": "fixture_export_bucket"},
            },
        }

        manifest = validate_and_normalize_manifest(
            payload,
            directory_name="fixture_export",
        )
        capability = manifest["capability_config"]["upload_destination"]
        self.assertTrue(capability["publish_on_attention"])
        self.assertTrue(capability["requires_valid_result"])
        self.assertEqual(
            capability["legacy"]["settings"],
            {"bucket": "fixture_export_bucket"},
        )

    def test_ui_visibility_is_strict_and_normalized(self) -> None:
        payload = _manifest("fixture")
        payload["ui"]["visibility"] = {
            "dashboard": False,
            "study_settings": True,
        }

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertFalse(manifest["ui"]["visibility"]["dashboard"])
        self.assertTrue(manifest["ui"]["visibility"]["settings_hub"])

        payload["ui"]["visibility"]["admin"] = True
        with self.assertRaisesRegex(PluginManifestError, "unsupported fields: admin"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_ui_extension_and_timeline_metadata_are_strict(self) -> None:
        payload = _manifest("fixture")
        payload["ui"]["extensions"] = {"dashboard": "../outside.js"}
        with self.assertRaisesRegex(PluginManifestError, "relative POSIX"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload = _manifest("fixture")
        payload["ui"]["assets"] = ["card.js", "card.css"]
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        self.assertEqual(manifest["ui"]["assets"], ["card.js", "card.css"])

        payload["ui"]["extensions"] = {"card": "card.css"}
        with self.assertRaisesRegex(PluginManifestError, r"\.js path"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload = _manifest("fixture")
        payload["ui"]["timeline"] = {
            "lane_aliases": ["fixture_sidecar"],
            "preferred_channels": ["payload.value"],
        }
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        self.assertEqual(
            manifest["ui"]["timeline"]["preferred_channels"],
            ["payload.value"],
        )

        payload["ui"]["timeline"]["preferred_channels"] = ["value<script>"]
        with self.assertRaisesRegex(PluginManifestError, "invalid identifier"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_v2_capability_names_are_normalized_to_v3(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["capabilities"] = {"status_poll": {}, "lsl_stream": {}}

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertEqual(manifest["capabilities"], ["health", "lsl_stream_provider"])

    def test_backup_projection_requires_a_positive_rate(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["capabilities"]["backup_projection"] = {
            "rate_hz": 0,
            "channels": [{"output": "value", "stream": "values", "channel": "value"}],
        }

        with self.assertRaises(PluginManifestError):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_sidecar_export_requires_a_complete_runtime_sidecar(self) -> None:
        # The host exports a sidecar from runtime.sidecar alone (am_hub lost
        # its block once and silently dropped out of crash recovery).
        payload = _manifest("fixture")
        payload["capabilities"]["sidecar_export"] = {}
        with self.assertRaisesRegex(PluginManifestError, "sidecar_export requires runtime.sidecar"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload["runtime"]["sidecar"] = {"sensor": "fixture", "filename_suffix": "fixture_signals"}
        with self.assertRaisesRegex(PluginManifestError, "sidecar_export requires runtime.sidecar"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload["runtime"]["sidecar"]["output_key"] = "fixture_file"
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        self.assertEqual(
            manifest["runtime"]["sidecar"],
            {"sensor": "fixture", "filename_suffix": "fixture_signals", "output_key": "fixture_file"},
        )

    def test_backup_projection_runs_on_the_fixed_one_hertz_grid(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["capabilities"]["backup_projection"] = {
            "channels": [{"output": "value", "stream": "values", "channel": "value"}],
        }
        projection = validate_and_normalize_manifest(payload, directory_name="fixture")["capability_config"][
            "backup_projection"
        ]
        self.assertEqual((projection["rate_hz"], projection["stale_after_ms"]), (1.0, 2500))

        payload["capabilities"]["backup_projection"]["rate_hz"] = 4
        with self.assertRaisesRegex(PluginManifestError, "fixed at 1 Hz"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_live_view_names_declared_numeric_channels(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        series = {"key": "trend", "stream": "values", "channels": ["value"], "axis": "signed", "min_span": 5}
        payload["capabilities"]["live_view"] = {"series": [series], "channel_key_prefix": "fixture.channel"}
        live_view = validate_and_normalize_manifest(payload, directory_name="fixture")["capability_config"]["live_view"]
        self.assertEqual(
            live_view,
            {
                "rate_hz": 2,
                "window_s": 60,
                "channel_key_prefix": "fixture.channel",
                "series": [{
                    "key": "trend", "stream": "values", "channels": ["value"], "axis": "signed",
                    "min_span": 5.0, "scale": 1.0, "unit_label": "",
                }],
            },
        )
        for bad, message in (
            ({"series": [{**series, "stream": "missing"}]}, "declared stream"),
            ({"series": [{**series, "channels": ["nope"]}]}, "declared channels"),
            ({"series": [{**series, "key": "Trend"}]}, "snake_case"),
            ({"series": [{**series, "axis": "log"}]}, "axis"),
            ({"series": [{**series, "key": f"s{index}"} for index in range(5)]}, "at most 4"),
            ({"series": []}, "non-empty"),
            ({"series": [series], "rate_hz": 10}, "unsupported fields"),
        ):
            payload["capabilities"]["live_view"] = bad
            with self.subTest(message=message), self.assertRaisesRegex(PluginManifestError, message):
                validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_stream_timing_declares_where_timestamps_come_from(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        stream = payload["streams"][0]
        stream["timing"] = {"timestamp_source": "host_arrival"}
        timing = validate_and_normalize_manifest(payload, directory_name="fixture")["streams"][0]["timing"]
        self.assertEqual(timing["timestamp_source"], "host_arrival")

        stream["timing"] = {"timestamp_source": "device_clock"}
        with self.assertRaisesRegex(PluginManifestError, "timestamp_source must be one of"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_a_corrected_timestamp_must_stay_reversible(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        stream = payload["streams"][0]
        stream["timing"] = {"timestamp_source": "host_arrival_corrected"}
        with self.assertRaisesRegex(PluginManifestError, "correction_channel is required"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        stream["channels"] = ["value", "correction_ms"]
        stream["channel_units"] = ["arbitrary_unit", "second"]
        stream["timing"]["correction_channel"] = "correction_ms"
        with self.assertRaisesRegex(PluginManifestError, "unit millisecond"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        stream["channel_units"] = ["arbitrary_unit", "millisecond"]
        timing = validate_and_normalize_manifest(payload, directory_name="fixture")["streams"][0]["timing"]
        self.assertEqual(timing["correction_channel"], "correction_ms")

        stream["timing"]["timestamp_source"] = "host_arrival"
        with self.assertRaisesRegex(PluginManifestError, "belongs to host_arrival_corrected"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_lsl_stream_channels_and_format_are_strict(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["channel_format"] = "complex128"
        with self.assertRaisesRegex(PluginManifestError, "channel_format"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["channels"] = ["value", "value"]
        payload["streams"][0]["channel_units"] = ["unit", "unit"]
        with self.assertRaisesRegex(PluginManifestError, "must be unique"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["sequence_channel"] = "missing"
        with self.assertRaisesRegex(PluginManifestError, "sequence_channel"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_stream_timing_defaults_to_an_honest_unknown_capture_delay(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        self.assertEqual(
            manifest["streams"][0]["timing"],
            {"capture_delay_ns": {"source": "unknown", "min_ns": None, "max_ns": None, "reference": None}},
        )

    def test_stream_timing_accepts_a_measured_capture_delay_with_provenance(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["timing"] = {
            "capture_delay_ns": {
                "source": "measured",
                "min_ns": 12_000_000,
                "max_ns": 18_000_000,
                "reference": "loopback-2026-08-14, tools/measure_capture_delay.py",
            }
        }
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        self.assertEqual(manifest["streams"][0]["timing"]["capture_delay_ns"]["source"], "measured")
        self.assertEqual(manifest["streams"][0]["timing"]["capture_delay_ns"]["min_ns"], 12_000_000)

    def test_stream_timing_requires_a_recognized_source(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["timing"] = {"capture_delay_ns": {"source": "guessed"}}
        with self.assertRaisesRegex(PluginManifestError, "source must be one of"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_stream_timing_requires_bounds_and_reference_unless_unknown(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["timing"] = {"capture_delay_ns": {"source": "estimated"}}
        with self.assertRaises(PluginManifestError):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_stream_timing_rejects_an_inverted_range(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["streams"][0]["timing"] = {
            "capture_delay_ns": {
                "source": "datasheet",
                "min_ns": 20,
                "max_ns": 10,
                "reference": "vendor datasheet rev 3",
            }
        }
        with self.assertRaisesRegex(PluginManifestError, "max_ns must be >= min_ns"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_acquisition_transport_enforces_delivery_pairs(self) -> None:
        payload = _manifest("fixture")
        payload["capabilities"]["acquisition_transport"] = {
            "transport": "ble",
            "delivery": "host_lsl_bridge",
        }

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertEqual(
            manifest["capability_config"]["acquisition_transport"],
            {"transport": "ble", "delivery": "host_lsl_bridge"},
        )

        payload["capabilities"]["acquisition_transport"]["delivery"] = "native_lsl"
        with self.assertRaisesRegex(PluginManifestError, "requires delivery"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_lan_and_wlan_acquisition_require_native_lsl(self) -> None:
        for transport in ("lan", "wlan"):
            with self.subTest(transport=transport):
                payload = _manifest("fixture")
                payload["capabilities"]["acquisition_transport"] = {
                    "transport": transport,
                    "delivery": "native_lsl",
                }
                manifest = validate_and_normalize_manifest(
                    payload,
                    directory_name="fixture",
                )
                self.assertEqual(
                    manifest["capability_config"]["acquisition_transport"]["delivery"],
                    "native_lsl",
                )

    def test_browser_https_acquisition_requires_source_quality_fields(self) -> None:
        payload = _manifest("fixture")
        payload["capabilities"]["acquisition_transport"] = {
            "transport": "browser_https",
            "delivery": "host_lsl_bridge",
            "heartbeat_required": True,
            "sequence_required": True,
            "source_timestamp_required": True,
        }

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertTrue(
            manifest["capability_config"]["acquisition_transport"]["heartbeat_required"]
        )

        payload["capabilities"]["acquisition_transport"]["sequence_required"] = False
        with self.assertRaisesRegex(PluginManifestError, "sequence_required must be true"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_runtime_modes_platform_modes_are_closed_and_normalized(self) -> None:
        # Renamed from `readiness` in api_version 5 (Phase 3.4) -- it collided
        # in name with the unrelated `readiness_requirements` capability.
        payload = _manifest("fixture")
        payload["capabilities"]["runtime_modes"] = {
            "mode_setting": "worker_mode",
            "default_mode": "local_worker",
            "platform_modes": {
                "default": ["local_worker", "remote_worker"],
                "macos-x64": ["remote_worker"],
            },
        }

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        runtime_modes = manifest["capability_config"]["runtime_modes"]
        self.assertEqual(runtime_modes["platform_modes"]["macos-x64"], ["remote_worker"])

        payload["capabilities"]["runtime_modes"]["platform_modes"].pop("default")
        with self.assertRaisesRegex(PluginManifestError, "default target"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_retired_v4_capabilities_are_rejected_with_a_clear_message(self) -> None:
        # runtime_control and the old readiness name both existed under
        # api_version 4; a manifest still declaring either must fail loudly,
        # not silently accept an unrecognized capability.
        payload = _manifest("fixture")
        payload["capabilities"]["runtime_control"] = {}
        with self.assertRaisesRegex(PluginManifestError, "runtime_control.*retired"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload = _manifest("fixture")
        payload["capabilities"]["readiness"] = {}
        with self.assertRaisesRegex(PluginManifestError, "readiness.*retired"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_recording_source_requires_lsl_and_cannot_offer_disable_controls(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["capabilities"]["recording_source"] = {"artifact": "xdf"}

        validate_and_normalize_manifest(payload, directory_name="fixture")

        payload["capabilities"].pop("lsl_stream_provider")
        with self.assertRaisesRegex(PluginManifestError, "requires lsl_stream_provider"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload = _manifest("fixture", source_id="fixture.stream")
        payload["capabilities"]["recording_source"] = {"artifact": "xdf"}
        payload["settings"]["machine"]["lsl.enabled"] = {
            "type": "boolean",
            "path": "lsl.enabled",
            "default": True,
        }
        with self.assertRaisesRegex(PluginManifestError, "disables canonical stream"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_study_sensor_declares_an_existing_primary_recording_stream(self) -> None:
        payload = _manifest("fixture", source_id="fixture.stream")
        payload["capabilities"].update(
            {
                "study_sensor": {},
                "recording_source": {"artifact": "xdf"},
                "backup_projection": {
                    "rate_hz": 1,
                    "channels": [
                        {"output": "value", "stream": "values", "channel": "value"}
                    ],
                },
                "live_view": {"series": [{"key": "value", "stream": "values", "channels": ["value"]}]},
            }
        )
        payload["streams"][0]["timing"] = {"timestamp_source": "host_arrival"}
        with self.assertRaisesRegex(PluginManifestError, "primary_stream"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

        payload["capabilities"]["recording_source"]["primary_stream"] = "values"
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        self.assertTrue(manifest["streams"][0]["primary"])

        payload["capabilities"]["recording_source"]["primary_stream"] = "missing"
        with self.assertRaisesRegex(PluginManifestError, "declared stream"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_admin_actions_have_unique_snake_case_keys(self) -> None:
        payload = _manifest("fixture")
        payload["capabilities"]["admin_actions"] = {
            "actions": [
                {
                    "key": "repair_runtime",
                    "label": "Repair runtime",
                    "confirm": True,
                }
            ]
        }

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertEqual(
            manifest["capability_config"]["admin_actions"]["actions"][0]["key"],
            "repair_runtime",
        )

        payload["capabilities"]["admin_actions"]["actions"].append(
            {"key": "repair_runtime", "label": "Again"}
        )
        with self.assertRaisesRegex(PluginManifestError, "duplicate admin action key"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_admin_action_surface_is_panel_or_configurator(self) -> None:
        payload = _manifest("fixture")
        payload["capabilities"]["admin_actions"] = {
            "actions": [
                {"key": "test_connection", "label": "Test"},
                {"key": "browse", "label": "Browse", "surface": "configurator"},
            ]
        }

        actions = validate_and_normalize_manifest(payload, directory_name="fixture")[
            "capability_config"
        ]["admin_actions"]["actions"]

        # Only a declared surface is kept, so other plugins' normalized
        # manifests do not change; the panels treat a missing one as "panel".
        self.assertNotIn("surface", actions[0])
        self.assertEqual(actions[1]["surface"], "configurator")

        payload["capabilities"]["admin_actions"]["actions"][1]["surface"] = "dashboard"
        with self.assertRaisesRegex(PluginManifestError, "surface must be one of panel, configurator"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_admin_action_payload_contract_is_closed(self) -> None:
        payload = _manifest("fixture")
        payload["capabilities"]["admin_actions"] = {
            "actions": [
                {
                    "key": "select_item",
                    "label": "Select",
                    "payload_schema": {
                        "index": {"type": "integer", "minimum": 0},
                    },
                    "any_of_required": ["index"],
                    "instances": {
                        "status_paths": ["candidates"],
                        "payload_map": {"index": "index"},
                        "label_fields": ["name"],
                    },
                }
            ]
        }
        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")
        action = manifest["capability_config"]["admin_actions"]["actions"][0]
        self.assertEqual(action["instances"]["payload_map"], {"index": "index"})

        payload["capabilities"]["admin_actions"]["actions"][0]["instances"]["payload_map"] = {
            "undeclared": "index"
        }
        with self.assertRaisesRegex(PluginManifestError, "undeclared field"):
            validate_and_normalize_manifest(payload, directory_name="fixture")

    def test_participant_operations_are_closed_manifest_allow_lists(self) -> None:
        payload = _manifest("fixture")
        payload["capabilities"].update(
            {
                "participant_actions": {
                    "actions": ["start_monitor", "stop_monitor"],
                },
                "participant_ingest": {"inputs": ["frame"]},
            }
        )

        manifest = validate_and_normalize_manifest(payload, directory_name="fixture")

        self.assertEqual(
            manifest["capability_config"]["participant_actions"]["actions"],
            ["start_monitor", "stop_monitor"],
        )
        self.assertEqual(
            manifest["capability_config"]["participant_ingest"]["inputs"],
            ["frame"],
        )

        payload["capabilities"]["participant_ingest"]["inputs"].append("frame")
        with self.assertRaisesRegex(PluginManifestError, "duplicate participant_ingest key"):
            validate_and_normalize_manifest(payload, directory_name="fixture")


class PluginDiscoveryIsolationTests(FixturePluginRootMixin, unittest.TestCase):
    fixture_group_name = "plugin-catalog-tests"

    def _plugin_folder(self, folder: str, manifest: dict, source: str | None = None) -> Path:
        plugin_dir = self.package_dir / folder
        plugin_dir.mkdir()
        (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
        (plugin_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        plugin_source = source or (
            "from study_runner.contracts.plugin_api import Plugin\n"
            f"PLUGIN = Plugin(key={manifest['plugin_key']!r}, label={manifest['ui']['label']!r}, "
            f"category='test', config_key={manifest['config_key']!r}, get_status=lambda context: {{}})\n"
        )
        (plugin_dir / "plugin.py").write_text(plugin_source, encoding="utf-8")
        write_driver_py(plugin_dir, manifest["plugin_key"])
        return plugin_dir

    def _discover(self):
        return discover_plugin_catalog(self.package_dir, package_name=self.package_name)

    def test_new_folder_is_discovered_without_a_registry_change(self) -> None:
        self._plugin_folder("fixture_sensor", _manifest("fixture_sensor"))

        catalog = self._discover()

        self.assertEqual([plugin.key for plugin in catalog.plugins], ["fixture_sensor"])
        self.assertFalse(catalog.invalid_entries)

    def test_missing_manifest_is_reported_as_invalid(self) -> None:
        helper = self.package_dir / "internal_worker"
        helper.mkdir()
        (helper / "__init__.py").write_text("", encoding="utf-8")
        (helper / "plugin.py").write_text(
            "raise RuntimeError('internal helper was imported')\n",
            encoding="utf-8",
        )

        catalog = self._discover()

        self.assertFalse(catalog.plugins)
        self.assertEqual(len(catalog.invalid_entries), 1)
        self.assertIn("missing manifest.json", catalog.invalid_entries[0].errors[0])

    def test_explicitly_ignored_helper_package_is_not_a_catalog_candidate(self) -> None:
        helper = self.package_dir / "internal_worker"
        helper.mkdir()
        (helper / "__init__.py").write_text("", encoding="utf-8")
        (helper / "plugin.py").write_text(
            "raise RuntimeError('internal helper was imported')\n",
            encoding="utf-8",
        )
        (helper / ".pluginignore").write_text(
            "Internal implementation package, not a public plugin.\n",
            encoding="utf-8",
        )

        catalog = self._discover()

        self.assertFalse(catalog.entries)

    def test_invalid_manifest_is_never_imported(self) -> None:
        manifest = _manifest("broken")
        manifest["api_version"] = 2
        marker = self.root / "imported.txt"
        self._plugin_folder(
            "broken",
            manifest,
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        )

        catalog = self._discover()

        self.assertEqual(len(catalog.invalid_entries), 1)
        self.assertFalse(marker.exists())

    def test_duplicate_keys_are_isolated_before_import(self) -> None:
        source = "raise RuntimeError('duplicate plugin was imported')\n"
        self._plugin_folder("first", _manifest("duplicate"), source)
        self._plugin_folder("second", _manifest("duplicate"), source)

        catalog = self._discover()

        self.assertFalse(catalog.plugins)
        self.assertEqual(len(catalog.invalid_entries), 2)
        self.assertTrue(all("duplicate plugin_key" in entry.errors[0] for entry in catalog.invalid_entries))

    def test_duplicate_card_question_types_isolate_every_provider(self) -> None:
        first = self._plugin_folder("first_card", _card_manifest("first_card", "shared-card"))
        second = self._plugin_folder("second_card", _card_manifest("second_card", "shared-card"))
        for directory in (first, second):
            (directory / "card.js").write_text("export function configureCard() {}\n", encoding="utf-8")

        catalog = self._discover()

        self.assertFalse(catalog.plugins)
        self.assertEqual(len(catalog.invalid_entries), 2)
        self.assertTrue(
            all("duplicate card question type: shared-card" in entry.errors for entry in catalog.invalid_entries)
        )

    def test_duplicate_stream_ids_isolate_both_plugins(self) -> None:
        self._plugin_folder("first", _manifest("first", source_id="same.source"))
        self._plugin_folder("second", _manifest("second", source_id="same.source"))

        catalog = self._discover()

        self.assertFalse(catalog.plugins)
        self.assertEqual(len(catalog.invalid_entries), 2)

    def test_conflicts_are_checked_across_extension_category_roots(self) -> None:
        first_root = self.package_dir / "sensors"
        second_root = self.package_dir / "outputs"
        first_root.mkdir()
        second_root.mkdir()
        for root, folder, manifest in (
            (first_root, "first", _manifest("duplicate", source_id="same.source")),
            (second_root, "second", _manifest("duplicate", source_id="same.source")),
        ):
            plugin = root / folder
            plugin.mkdir()
            (plugin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (plugin / "plugin.py").write_text("raise AssertionError('conflict imported')\n", encoding="utf-8")
            write_driver_py(plugin, "duplicate")
        roots = ((first_root, self.package_name), (second_root, self.package_name))

        with patch("study_runner.plugin_framework.plugin_catalog.trusted_roots", return_value=roots):
            catalog = discover_plugin_catalog()

        self.assertFalse(catalog.plugins)
        self.assertEqual(len(catalog.invalid_entries), 2)
        self.assertTrue(all("duplicate plugin_key" in entry.errors[0] for entry in catalog.invalid_entries))

    def test_upload_destination_is_discovered_with_its_generic_handler(self) -> None:
        manifest = _manifest("fixture_export")
        manifest["capabilities"]["upload_destination"] = {
            "destination": "fixture_export"
        }
        manifest["settings"]["study"]["bucket"] = {
            "type": "string",
            "default": "",
        }
        source = (
            "from study_runner.contracts.plugin_api import Plugin\n"
            "PLUGIN = Plugin(key='fixture_export', label='fixture_export', "
            "category='test', config_key='fixture_export', get_status=lambda context: {}, "
            "publish_destination=lambda context, payload: {'ok': True})\n"
        )
        self._plugin_folder("fixture_export", manifest, source)

        catalog = self._discover()

        self.assertEqual([plugin.key for plugin in catalog.plugins], ["fixture_export"])
        self.assertTrue(callable(catalog.plugins[0].publish_destination))
        self.assertIn(
            "bucket",
            catalog.manifests["fixture_export"]["study_settings_schema"],
        )

    def test_missing_declared_ui_asset_isolated_before_plugin_import(self) -> None:
        manifest = _manifest("missing_asset")
        manifest["ui"]["extensions"] = {"dashboard": "ui/missing.js"}
        self._plugin_folder("missing_asset", manifest)

        catalog = self._discover()

        self.assertFalse(catalog.plugins)
        self.assertIn("declared UI asset does not exist", catalog.invalid_entries[0].errors[0])


class PublicCatalogTests(unittest.TestCase):
    def test_public_catalog_has_only_v5_valid_plugins_and_is_keyed_for_ui_use(self) -> None:
        payload = get_plugin_catalog_payload()

        self.assertEqual(payload["api_version"], 5)
        self.assertEqual(payload["invalid_plugins"], [])
        self.assertEqual(
            set(payload["plugins_by_key"]),
            {plugin["plugin_key"] for plugin in payload["plugins"]},
        )
        self.assertIn("study_sensor", payload["plugins_by_key"]["brainbit"]["capabilities"])

    def test_catalog_endpoint_returns_the_public_contract(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)

        response = app.test_client().get("/api/plugins/catalog")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["api_version"], 5)

    def test_admin_action_endpoint_enforces_the_manifest_allow_list(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)
        client = app.test_client()
        with patch(
            "study_runner.apps.server.routes.plugins._plugin_context",
            return_value=object(),
        ):
            rejected = client.post(
                "/api/admin/plugins/camera_emotion/actions/format_disk"
            )

        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(rejected.get_json()["ok"])

    def test_participant_routes_dispatch_through_generic_plugin_boundaries(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)
        client = app.test_client()
        with (
            patch(
                "study_runner.apps.server.routes.plugins.get_plugin",
                return_value=object(),
            ),
            patch(
                "study_runner.apps.server.routes.plugins._plugin_context",
                return_value=object(),
            ),
            patch(
                "study_runner.apps.server.routes.plugins._require_secure_participant_ingest",
            ),
            patch(
                "study_runner.apps.server.routes.plugins.run_participant_action",
                return_value={"ok": True, "result": {"monitor_active": True}},
            ) as action,
            patch(
                "study_runner.apps.server.routes.plugins.ingest_participant_payload",
                return_value={"ok": True, "result": {"accepted": True}},
            ) as ingest,
        ):
            action_response = client.post(
                "/api/plugins/fixture_sensor/participant/actions/start_monitor",
                json={"study_id": "study-a"},
            )
            ingest_response = client.post(
                "/api/plugins/fixture_sensor/participant/ingest/frame",
                json={"sequence_number": 3},
            )

        self.assertEqual(action_response.status_code, 200)
        self.assertEqual(ingest_response.status_code, 200)
        action.assert_called_once_with(
            "fixture_sensor",
            "start_monitor",
            ANY,
            {"study_id": "study-a"},
        )
        ingest.assert_called_once_with(
            "fixture_sensor",
            "frame",
            ANY,
            {"sequence_number": 3},
        )

    def test_browser_participant_ingest_requires_sequence_and_source_time(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)
        client = app.test_client()
        runtime = get_process_runtime("camera_emotion")
        self.assertIsNotNone(runtime)
        with (
            patch.object(runtime, "_context", object()),
            patch(
                "study_runner.apps.server.routes.plugins._plugin_context",
                return_value=object(),
            ),
            patch.object(
                runtime,
                "request",
                side_effect=lambda operation, payload: {
                    "accepted": True,
                    "sequence_number": payload["payload"]["sequence_number"],
                },
            ) as process_rpc,
        ):
            missing_sequence = client.post(
                "/api/plugins/camera_emotion/participant/ingest/frame",
                json={"source_epoch_ms": 1000.0},
                base_url="https://study-runner.test",
            )
            missing_source_time = client.post(
                "/api/plugins/camera_emotion/participant/ingest/frame",
                json={"sequence_number": 0},
                base_url="https://study-runner.test",
            )
            insecure_remote = client.post(
                "/api/plugins/camera_emotion/participant/ingest/frame",
                json={"sequence_number": 0, "source_epoch_ms": 1000.0},
                base_url="http://study-runner.test",
                headers={"X-Forwarded-Proto": "https"},
                environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
            )
            valid = client.post(
                "/api/plugins/camera_emotion/participant/ingest/frame",
                json={"sequence_number": 0, "source_epoch_ms": 1000.0},
                base_url="https://study-runner.test",
                environ_overrides={"REMOTE_ADDR": "192.0.2.10"},
            )

        self.assertEqual(missing_sequence.status_code, 400)
        self.assertIn("sequence_number", missing_sequence.get_json()["error"])
        self.assertEqual(missing_source_time.status_code, 400)
        self.assertIn("source timestamp", missing_source_time.get_json()["error"])
        self.assertEqual(insecure_remote.status_code, 403)
        self.assertIn("HTTPS", insecure_remote.get_json()["error"])
        self.assertEqual(valid.status_code, 200)
        self.assertTrue(valid.get_json()["result"]["accepted"])
        process_rpc.assert_called_once_with(
            "participant_ingest",
            {
                "ingest": "frame",
                "payload": {"sequence_number": 0, "source_epoch_ms": 1000.0},
            },
        )

    def test_plugin_asset_endpoint_serves_declared_javascript_and_css(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)
        client = app.test_client()

        declared = client.get("/api/plugins/brainbit/assets/ui/dashboard.js")
        stylesheet = client.get("/api/plugins/choice/assets/card.css")
        undeclared = client.get("/api/plugins/brainbit/assets/manifest.json")

        self.assertEqual(declared.status_code, 200)
        self.assertEqual(declared.mimetype, "text/javascript")
        self.assertEqual(declared.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertEqual(stylesheet.mimetype, "text/css")
        self.assertEqual(stylesheet.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(undeclared.status_code, 404)
        declared.close()
        stylesheet.close()
        undeclared.close()

    def test_plugin_asset_endpoint_serves_the_modules_a_declared_module_imports(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)
        client = app.test_client()

        for asset in ("ui/configurator.js", "ui/configurator-node-view.js", "ui/node-canvas.js"):
            response = client.get(f"/api/plugins/notion/assets/{asset}")
            self.assertEqual(response.status_code, 200, asset)
            self.assertEqual(response.mimetype, "text/javascript", asset)
            response.close()

    def test_admin_action_endpoint_rejects_wrong_and_unknown_payload_fields(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.register_blueprint(plugins_blueprint)
        client = app.test_client()
        with patch(
            "study_runner.apps.server.routes.plugins._plugin_context",
            return_value=object(),
        ):
            wrong_type = client.post(
                "/api/admin/plugins/brainbit/actions/select_device",
                json={"index": "0"},
            )
            unknown = client.post(
                "/api/admin/plugins/brainbit/actions/select_device",
                json={"index": 0, "command": "format_disk"},
            )
            malformed = client.post(
                "/api/admin/plugins/brainbit/actions/select_device",
                data="{",
                content_type="application/json",
            )
            wrong_media_type = client.post(
                "/api/admin/plugins/brainbit/actions/select_device",
                data='{"index": 0}',
                content_type="text/plain",
            )

        self.assertEqual(wrong_type.status_code, 400)
        self.assertIn("must be an integer", wrong_type.get_json()["error"])
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("undeclared fields", unknown.get_json()["error"])
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(wrong_media_type.status_code, 415)

    def test_brainbit_selection_uses_generic_route_and_machine_context(self) -> None:
        """BrainBit is an API-v5 plugin: its admin actions run inside the
        driver.py child process, not the server process. Patching
        study_runner.plugins.sensors.brainbit.plugin._restart here would be a no-op --
        that module only runs inside the isolated driver. This test instead
        observes the process RPC boundary (like the equivalent Nextcloud
        test), so the HTTP-route -> validated-payload -> process-request
        wiring is exercised without starting a real child. The actual
        persist_hardware_config business logic run inside the driver is
        already covered directly by BrainBitManifestActionTests below."""
        from flask import Flask

        app = Flask(__name__)
        app.config.update(
            BASE_DIR=PROJECT_ROOT,
            DATA_DIR=PROJECT_ROOT / ".tmp",
            HARDWARE_CONFIG={"brainbit": {"enabled": False, "keep": "value"}},
            HARDWARE_CONFIG_FILE=PROJECT_ROOT / ".tmp" / "hardware.json",
            LOCAL_SECRETS={},
            LOCAL_SECRETS_FILE=PROJECT_ROOT / ".tmp" / "secrets.json",
        )
        app.register_blueprint(plugins_blueprint)

        runtime = get_process_runtime("brainbit")
        self.assertIsNotNone(runtime)
        with app.app_context():
            runtime._context = _plugin_context(machine_admin=True)

        def driver_response(operation, payload=None, **_kwargs):
            if operation == "admin_action":
                return {
                    "last_message": "BrainBit band saved and restart requested",
                    "target_device": {"index": 1, "name": "Band", "address": "AA", "serial_number": "BB"},
                    "restart": {"status": "waiting"},
                    "restart_error": "",
                }
            if operation == "status":
                return {"status": "waiting"}
            raise AssertionError(f"Unexpected process operation: {operation}")

        with patch.object(runtime, "request", side_effect=driver_response) as request_rpc:
            response = app.test_client().post(
                "/api/admin/plugins/brainbit/actions/select_device",
                json={"index": 1, "name": "Band", "address": "AA", "serial_number": "BB"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["result"]["target_device"]["index"], 1)
        admin_request = next(
            call for call in request_rpc.call_args_list if call.args[0] == "admin_action"
        )
        self.assertEqual(
            admin_request.args[1],
            {
                "action": "select_device",
                "payload": {"index": 1, "name": "Band", "address": "AA", "serial_number": "BB"},
            },
        )


class BrainBitManifestActionTests(unittest.TestCase):
    def test_select_device_persists_through_generic_action(self) -> None:
        persisted: list[dict] = []
        context = PluginContext(
            base_dir=PROJECT_ROOT,
            data_dir=PROJECT_ROOT / ".tmp",
            hardware_config={"brainbit": {"enabled": False, "keep": "value"}},
            local_secrets={},
            local_secrets_file=PROJECT_ROOT / ".tmp" / "secrets.json",
            persist_hardware_config=lambda value: persisted.append(value),
        )
        with (
            patch("study_runner.plugins.sensors.brainbit.plugin._restart", return_value={"status": "waiting"}),
            patch("study_runner.plugin_framework.registry.get_plugin_status", return_value={"status": "waiting"}),
        ):
            response = run_admin_action(
                "brainbit",
                "select_device",
                context,
                {"index": 2, "name": "Band", "address": "AA", "serial_number": "BB"},
            )

        self.assertTrue(response["ok"])
        self.assertEqual(persisted[0]["brainbit"]["device_index"], 2)
        self.assertEqual(persisted[0]["brainbit"]["serial_number"], "BB")
        self.assertEqual(persisted[0]["brainbit"]["keep"], "value")


if __name__ == "__main__":
    unittest.main()
