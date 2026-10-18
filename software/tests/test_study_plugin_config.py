from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.runtime_core.studies.study_plugin_config import (
    migrate_study_plugin_config,
    normalize_card_plugin_actions,
    normalize_study_settings_plugins,
    reset_stale_upload_discoveries,
)
from study_runner.runtime_core.studies.study_readiness_service import check_study_readiness
from study_runner.runtime_core.studies.validation import validate_and_normalize_config


class StudyPluginMigrationTests(unittest.TestCase):
    def test_legacy_sensor_and_destination_fields_create_v3_entries(self) -> None:
        migrated = normalize_study_settings_plugins(
            {
                "sensors": {"brainbit": True, "mini_radar": False, "camera_emotion": True},
                "notion_enabled": True,
                "notion_database_id": "db-1",
                "nextcloud_enabled": True,
                "nextcloud_share_link": "https://cloud.example/s/token",
            }
        )

        self.assertEqual(
            migrated["plugins"]["brainbit"],
            {"enabled": True, "required": True, "settings": {}},
        )
        self.assertFalse(migrated["plugins"]["mini_radar"]["enabled"])
        self.assertTrue(migrated["plugins"]["camera_emotion"]["enabled"])
        self.assertEqual(migrated["plugins"]["notion"]["settings"]["database_id"], "db-1")
        self.assertEqual(
            migrated["plugins"]["nextcloud"]["settings"]["share_link"],
            "https://cloud.example/s/token",
        )

    def test_v3_plugin_values_override_legacy_without_reemitting_flat_fields(self) -> None:
        migrated = normalize_study_settings_plugins(
            {
                "sensors": {"brainbit": True},
                "notion_enabled": False,
                "plugins": {
                    "brainbit": {"enabled": False, "required": False, "settings": {}},
                    "notion": {
                        "enabled": True,
                        "required": False,
                        "settings": {"database_id": "v3-db"},
                    },
                },
            }
        )

        self.assertFalse(migrated["sensors"]["brainbit"])
        self.assertTrue(migrated["plugins"]["notion"]["enabled"])
        self.assertEqual(
            migrated["plugins"]["notion"]["settings"]["database_id"],
            "v3-db",
        )
        self.assertNotIn("notion_enabled", migrated)
        self.assertNotIn("notion_database_id", migrated)

    def test_missing_required_plugin_reference_is_preserved(self) -> None:
        validated = validate_and_normalize_config(
            {
                "study_id": "Future sensor",
                "questions": [{"type": "finish"}],
                "study_settings": {
                    "plugins": {
                        "future_sensor": {
                            "enabled": True,
                            "required": True,
                            "settings": {"mode": "fast"},
                        }
                    }
                },
            }
        )

        self.assertEqual(
            validated["study_settings"]["plugins"]["future_sensor"],
            {"enabled": True, "required": True, "settings": {"mode": "fast"}},
        )

    def test_removed_brainbit_old_stays_explicit_and_is_never_remapped(self) -> None:
        source_settings = {"legacy_gain": 7, "device_address": "AA:BB:CC:DD"}
        validated = validate_and_normalize_config(
            {
                "study_id": "Historic BrainBit study",
                "questions": [{"type": "finish"}],
                "study_settings": {
                    "plugins": {
                        "brainbit_old": {
                            "enabled": True,
                            "required": True,
                            "settings": source_settings,
                        }
                    }
                },
            }
        )
        selection = validated["study_settings"]["plugins"]["brainbit_old"]
        self.assertEqual(selection["settings"], source_settings)
        self.assertNotEqual(selection, validated["study_settings"]["plugins"]["brainbit"])

        readiness = check_study_readiness(validated, {}, {}, https_active=True)
        missing = [
            blocker for blocker in readiness["blockers"]
            if blocker.get("code") == "plugin_unavailable" and blocker.get("plugin") == "brainbit_old"
        ]
        self.assertEqual(len(missing), 1)
        self.assertTrue(missing[0]["blocking"])

    def test_empty_catalog_preserves_missing_plugins_and_card_actions_opaquely(self) -> None:
        from study_runner.data_core.host import study_sensor_runtime
        from study_runner.plugin_framework import registry

        source = {
            "study_id": "Portable study",
            "questions": [
                {
                    "type": "stimulus",
                    "duration_ms": 1000,
                    "plugin_actions": {
                        "future_sensor": {
                            "nested": {"thresholds": [1, 2, 3]},
                            "mode": "developer-v2",
                        }
                    },
                },
                {"type": "finish"},
            ],
            "study_settings": {
                "sensors_enabled": True,
                "sensors": {"future_sensor": True},
                "plugins": {
                    "future_sensor": {
                        "enabled": True,
                        "required": True,
                        "settings": {"nested": {"gain": [1, 2]}, "mode": "fast"},
                    },
                    "optional_destination": {
                        "enabled": True,
                        "required": False,
                        "settings": {"target": {"folder": "raw"}},
                    },
                },
            },
        }
        with (
            mock.patch.object(registry, "get_plugin_manifests", return_value={}),
            mock.patch.object(study_sensor_runtime, "STUDY_SENSOR_KEYS", ()),
        ):
            validated = validate_and_normalize_config(source)
            readiness = check_study_readiness(validated, {}, {}, https_active=True)

        settings = validated["study_settings"]
        self.assertEqual(settings["sensors"], {})
        self.assertFalse(settings["sensors_enabled"])
        self.assertEqual(
            settings["plugins"]["future_sensor"]["settings"],
            source["study_settings"]["plugins"]["future_sensor"]["settings"],
        )
        self.assertEqual(
            validated["questions"][0]["plugin_actions"],
            source["questions"][0]["plugin_actions"],
        )
        unavailable = {
            blocker["plugin"]: blocker
            for blocker in readiness["blockers"]
            if blocker["code"] == "plugin_unavailable"
        }
        self.assertTrue(unavailable["future_sensor"]["blocking"])
        self.assertFalse(unavailable["optional_destination"]["blocking"])
        self.assertTrue(readiness["start_blocked"])

    def test_unknown_card_actions_are_deep_copied_without_a_manifest(self) -> None:
        actions = {
            "future_sensor": {
                "nested": {"labels": ["a", "b"]},
                "enabled": "vendor-specific",
            }
        }
        normalized = normalize_card_plugin_actions(
            {"type": "stimulus", "plugin_actions": actions},
            manifests={},
        )
        self.assertEqual(normalized, actions)
        self.assertIsNot(normalized, actions)
        self.assertIsNot(normalized["future_sensor"]["nested"], actions["future_sensor"]["nested"])

    def test_legacy_emotion_worker_plugin_becomes_camera_emotion(self) -> None:
        migrated = normalize_study_settings_plugins(
            {
                "plugins": {
                    "emotion_worker": {
                        "enabled": True,
                        "required": True,
                        "settings": {"worker_mode": "remote_worker"},
                    }
                }
            }
        )

        self.assertNotIn("emotion_worker", migrated["plugins"])
        self.assertEqual(
            migrated["plugins"]["camera_emotion"],
            {
                "enabled": True,
                "required": True,
                "settings": {"worker_mode": "remote_worker"},
            },
        )
        readiness = check_study_readiness(
            {"study_id": "Legacy emotion", "study_settings": migrated},
            {"camera_emotion": {"enabled": True}},
            {},
            https_active=True,
        )
        self.assertNotIn(
            "plugin_unavailable",
            [blocker["code"] for blocker in readiness["blockers"]],
        )

    def test_explicit_camera_emotion_wins_and_inherits_missing_legacy_settings(self) -> None:
        migrated = normalize_study_settings_plugins(
            {
                "plugins": {
                    "emotion_worker": {
                        "enabled": True,
                        "required": True,
                        "settings": {
                            "worker_mode": "remote_worker",
                            "emotion_worker_url": "http://legacy.example:3001",
                        },
                    },
                    "camera_emotion": {
                        "enabled": False,
                        "required": False,
                        "settings": {
                            "worker_mode": "local_worker",
                        },
                    },
                }
            }
        )

        self.assertNotIn("emotion_worker", migrated["plugins"])
        self.assertEqual(
            migrated["plugins"]["camera_emotion"],
            {
                "enabled": False,
                "required": False,
                "settings": {
                    "worker_mode": "local_worker",
                    "emotion_worker_url": "http://legacy.example:3001",
                },
            },
        )

    def test_legacy_stimulus_fields_become_plugin_actions_and_an_actuator_selection(self) -> None:
        migrated = migrate_study_plugin_config(
            {
                "study_id": "Legacy cards",
                "questions": [
                    {
                        "type": "stimulus",
                        "send_signal": False,
                        "brainbit_to_touchdesigner": True,
                        "camera_capture_enabled": True,
                        "camera_snapshot_interval_ms": 1500,
                        "mini_radar_recording_enabled": False,
                    }
                ],
            }
        )
        actions = migrated["questions"][0]["plugin_actions"]

        self.assertEqual(actions["camera_emotion"]["snapshot_interval_ms"], 1500)
        self.assertNotIn("lsl", actions)
        self.assertNotIn("mini_radar", actions)
        self.assertNotIn("brainbit", actions, "a sensor has no start/stop option of its own")
        self.assertNotIn("capture_enabled", actions["camera_emotion"])
        self.assertNotIn("osc", actions, "the actuator selection replaced OSC's own switch")
        # send_signal=False switched every start/stop signal off.
        self.assertEqual(migrated["questions"][0]["actuator_plugins"], [])
        self.assertNotIn("send_signal", migrated["questions"][0])
        self.assertNotIn("camera_capture_enabled", migrated["questions"][0])

    def test_v3_card_actions_survive_validation_without_legacy_projections(self) -> None:
        validated = validate_and_normalize_config(
            {
                "study_id": "V3 cards",
                "questions": [
                    {
                        "type": "stimulus",
                        "duration_ms": 1000,
                        "plugin_actions": {
                            "brainbit": {"to_lsl": False, "to_touchdesigner": True},
                            "mini_radar": {"recording_enabled": False},
                            "camera_emotion": {
                                "capture_enabled": True,
                                "snapshot_interval_ms": 250,
                            },
                            "lsl": {"send_marker": False},
                        },
                    }
                ],
            }
        )
        card = validated["questions"][0]

        self.assertNotIn("send_signal", card)
        self.assertNotIn("brainbit_to_lsl", card)
        self.assertNotIn("brainbit_to_touchdesigner", card)
        self.assertNotIn("camera_capture_enabled", card)
        self.assertNotIn("camera_snapshot_interval_ms", card)
        self.assertNotIn("mini_radar_recording_enabled", card)
        self.assertNotIn("to_lsl", card["plugin_actions"]["brainbit"])
        self.assertNotIn("capture_enabled", card["plugin_actions"]["camera_emotion"])
        self.assertNotIn("lsl", card["plugin_actions"])
        self.assertNotIn("mini_radar", card["plugin_actions"])
        self.assertIn("plugin_actions", card)


def _notion_plugins(**settings: str) -> dict:
    return {"plugins": {"notion": {"enabled": True, "required": False, "settings": settings}}}


class ResetStaleUploadDiscoveriesTests(unittest.TestCase):
    """A hand-entered destination setting changing must not leave a stale
    auto-discovered id (e.g. a Notion database under the previous parent
    page) pointing at the old target. See docs/archive for the bug this
    guards: retrying an old session, or just saving a changed parent page,
    silently kept uploading to the page the database was first created
    under."""

    def test_changing_the_parent_page_clears_its_discovered_database(self) -> None:
        previous = _notion_plugins(parent_page_id="page-a", database_id="db-a", data_source_id="ds-a")
        next_settings = _notion_plugins(parent_page_id="page-b", database_id="db-a", data_source_id="ds-a")

        reset = reset_stale_upload_discoveries(previous, next_settings)

        settings = reset["plugins"]["notion"]["settings"]
        self.assertEqual(settings["parent_page_id"], "page-b")
        self.assertEqual(settings["database_id"], "")
        self.assertEqual(settings["data_source_id"], "")

    def test_an_explicit_database_override_in_the_same_save_survives(self) -> None:
        previous = _notion_plugins(parent_page_id="page-a", database_id="db-a", data_source_id="ds-a")
        next_settings = _notion_plugins(parent_page_id="page-b", database_id="operator-chosen-db", data_source_id="ds-a")

        reset = reset_stale_upload_discoveries(previous, next_settings)

        settings = reset["plugins"]["notion"]["settings"]
        self.assertEqual(settings["database_id"], "operator-chosen-db", "an explicit change in this save is not a leftover")
        self.assertEqual(settings["data_source_id"], "", "unrelated discovered field still clears")

    def test_unrelated_edits_leave_discovered_settings_untouched(self) -> None:
        previous = _notion_plugins(parent_page_id="page-a", database_id="db-a")
        next_settings = _notion_plugins(parent_page_id="page-a", database_id="db-a")
        next_settings["plugins"]["notion"]["enabled"] = False

        reset = reset_stale_upload_discoveries(previous, next_settings)

        self.assertEqual(reset["plugins"]["notion"]["settings"]["database_id"], "db-a")

    def test_declared_target_settings_limit_what_moves_the_target(self) -> None:
        manifests = {"fixture": {
            "capability_config": {"upload_destination": {
                "discovered_settings": ["database_id"],
                "target_settings": ["parent_page_id"],
            }},
            "study_settings_schema": {
                "parent_page_id": {"type": "string"},
                "database_id": {"type": "string"},
                "layout": {"type": "object"},
            },
        }}

        def plugins(**settings):
            return {"plugins": {"fixture": {"enabled": True, "settings": settings}}}

        layout_only = reset_stale_upload_discoveries(
            plugins(parent_page_id="a", database_id="db", layout={"x": 1}),
            plugins(parent_page_id="a", database_id="db", layout={"x": 2}),
            manifests=manifests,
        )
        self.assertEqual(layout_only["plugins"]["fixture"]["settings"]["database_id"], "db")
        moved = reset_stale_upload_discoveries(
            plugins(parent_page_id="a", database_id="db"),
            plugins(parent_page_id="b", database_id="db"),
            manifests=manifests,
        )
        self.assertEqual(moved["plugins"]["fixture"]["settings"]["database_id"], "")

    def test_a_newly_added_plugin_selection_is_left_alone(self) -> None:
        next_settings = _notion_plugins(parent_page_id="page-a", database_id="")
        reset = reset_stale_upload_discoveries(None, next_settings)
        self.assertEqual(reset["plugins"]["notion"]["settings"]["database_id"], "")

    def test_missing_or_malformed_input_is_handled_without_error(self) -> None:
        self.assertEqual(reset_stale_upload_discoveries(None, None), {})
        self.assertEqual(
            reset_stale_upload_discoveries({"plugins": "not-a-dict"}, _notion_plugins(parent_page_id="page-a")),
            _notion_plugins(parent_page_id="page-a"),
        )


if __name__ == "__main__":
    unittest.main()
