"""Destination discoveries preserve study edits and survive queue replay."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.runtime_core.delivery.upload_runtime import (
    _current_plugin_selection,
    _plugin_executor,
    _retry_target_describer,
    apply_deferred_upload_targets,
)
from study_runner.runtime_core.delivery.upload_jobs_service import UploadJobError, UploadJobService
from study_runner.runtime_core.studies.study_config_service import (
    StudyRevisionConflict, delete_study, load_config, load_study,
    save_active_study, study_config_revision, study_transaction_path,
)
from study_runner.contracts.plugin_api import Plugin
from study_runner.contracts.manifest import PluginManifestError, validate_and_normalize_manifest


def normalize_manifest(payload):
    return validate_and_normalize_manifest(payload, directory_name="notion_upload")


class FakeApp:
    def __init__(self, root: Path) -> None:
        self.config = {
            "BASE_DIR": root, "DATA_DIR": root / "data",
            "CONFIG_FILE": root / "active.study-runner", "SAVED_STUDIES_DIR": root / "studies",
            "LOCAL_SECRETS_FILE": root / "local_secrets.json",
            "LOCAL_SECRETS": {"notion": {"api_key": "LOCAL_SECRET_MUST_NOT_BE_CHECKPOINTED"}},
        }

    def app_context(self):
        return nullcontext()


def study(study_id: str = "Study A", parent: str = "parent-1") -> dict:
    return {
        "study_id": study_id,
        "questions": [{"type": "text", "prompt": "Original scientific question"}],
        "study_settings": {"plugins": {"notion": {
            "enabled": True, "required": False,
            "settings": {"parent_page_id": parent},
        }}},
    }


def settings(config: dict) -> dict:
    return config["study_settings"]["plugins"]["notion"]["settings"]


class UploadTargetPersistenceTests(unittest.TestCase):
    def test_discovered_target_waits_until_study_is_not_busy(self) -> None:
        with patch("study_runner.runtime_core.delivery.upload_runtime.study_busy_reason", return_value="running"):
            self.executor(self.discovered)(self.queued)
            self.assertNotIn("database_id", settings(load_config(self.app.config["CONFIG_FILE"])))
            self.assertEqual(apply_deferred_upload_targets(self.app), 0)
        self.assertEqual(apply_deferred_upload_targets(self.app), 1)
        self.assertEqual(settings(load_config(self.app.config["CONFIG_FILE"]))["database_id"], "created-db")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = FakeApp(Path(self.temporary.name))
        self.queued = {
            "config_data": study(),
            "result_payload": {"session_id": "session-1", "answers": {"question-1": "original"}},
            "saved_output": {"card_summary": {"schema": "study-runner/card-summary/v1", "cards": []}},
        }
        self.original = deepcopy(self.queued)
        self.save(study())

    def save(self, config: dict) -> str:
        return save_active_study(self.app.config["CONFIG_FILE"], self.app.config["SAVED_STUDIES_DIR"], config)

    def executor(self, handler):
        plugin = Plugin(key="notion", label="Notion", category="storage", config_key="notion", publish_destination=handler)
        return _plugin_executor(self.app, plugin, "notion")

    def discovered(self, *_args):
        return {"ok": True, "study_config_updates": {"database_id": "created-db", "data_source_id": "created-source"}}

    def test_patches_latest_revision_preserving_edits_and_cas(self) -> None:
        edited = study()
        edited["questions"][0]["prompt"] = "Newer question"
        settings(edited)["unrelated_extension"] = "queued-value"
        settings(self.queued["config_data"])["unrelated_extension"] = "queued-value"
        self.original = deepcopy(self.queued)
        previous_revision = self.save(edited)
        self.executor(self.discovered)(self.queued)
        active = load_config(self.app.config["CONFIG_FILE"])
        self.assertEqual(active["questions"], edited["questions"])
        self.assertEqual(settings(active)["unrelated_extension"], "queued-value")
        self.assertEqual(settings(active)["database_id"], "created-db")
        self.assertEqual(active, load_study(self.app.config["SAVED_STUDIES_DIR"], "Study A"))
        self.assertEqual(self.queued, self.original)
        marker = json.loads(study_transaction_path(self.app.config["CONFIG_FILE"]).read_text())
        self.assertEqual(marker["status"], "committed")
        self.assertEqual(marker["revision"], study_config_revision(active))
        with self.assertRaises(StudyRevisionConflict):
            save_active_study(
                self.app.config["CONFIG_FILE"], self.app.config["SAVED_STUDIES_DIR"], edited,
                expected_revision=previous_revision,
            )

    def test_background_upload_never_switches_another_active_study(self) -> None:
        edited = study()
        edited["questions"][0]["prompt"] = "Archived newer question"
        self.save(edited)
        self.save(study("Study B"))
        active_before = self.app.config["CONFIG_FILE"].read_bytes()
        self.executor(self.discovered)(self.queued)
        self.assertEqual(self.app.config["CONFIG_FILE"].read_bytes(), active_before)
        archived = load_study(self.app.config["SAVED_STUDIES_DIR"], "Study A")
        self.assertEqual(archived["questions"], edited["questions"])
        self.assertEqual(settings(archived)["database_id"], "created-db")

    def test_operator_target_changes_win_over_old_queue(self) -> None:
        for changed in ({"parent_page_id": "new-parent"}, {"database_id": "operator-db"}):
            with self.subTest(changed=changed):
                edited = study()
                settings(edited).update(changed)
                self.save(edited)
                before = self.app.config["CONFIG_FILE"].read_bytes()
                self.executor(self.discovered)(self.queued)
                self.assertEqual(self.app.config["CONFIG_FILE"].read_bytes(), before)

    def test_deleted_archive_is_not_recreated(self) -> None:
        self.save(study("Study B"))
        delete_study(self.app.config["SAVED_STUDIES_DIR"], "Study A")
        self.executor(self.discovered)(self.queued)
        with self.assertRaises(FileNotFoundError):
            load_study(self.app.config["SAVED_STUDIES_DIR"], "Study A")

    def test_replay_after_partial_failure_reuses_target_and_keeps_snapshot_bytes(self) -> None:
        calls: list[dict] = []

        def publish(_context, payload):
            calls.append(deepcopy(payload))
            if not settings(payload["config_data"]).get("database_id"):
                return {"ok": False, "error": "Network failed after database creation", "study_config_updates": {"database_id": "durable-db"}}
            return {"ok": True}

        service = UploadJobService(self.app.config["DATA_DIR"], executors={"notion": self.executor(publish)}, clock=lambda: 1000)
        for job_id in ("first", "second"):
            service.enqueue(kind="notion", study_id="Study A", participant_id="participant", session_id=job_id, label="Notion", payload=self.queued, job_id=job_id)
        payload_path = service.payload_dir / "first.json"
        snapshot_bytes = payload_path.read_bytes()
        service.process_due_jobs_once(limit=1)
        self.assertEqual(service.counts()["queued"], 2)
        self.assertEqual(payload_path.read_bytes(), snapshot_bytes)
        replay = UploadJobService(self.app.config["DATA_DIR"], executors={"notion": self.executor(publish)}, clock=lambda: 2000)
        replay.process_due_jobs_once()
        self.assertEqual(replay.counts()["done"], 2)
        self.assertEqual(len(calls), 3)
        self.assertEqual([settings(call["config_data"]).get("database_id") for call in calls], [None, "durable-db", "durable-db"])
        for call in calls:
            self.assertEqual(call["config_data"]["questions"], self.original["config_data"]["questions"])
            self.assertEqual(call["result_payload"], self.original["result_payload"])
            self.assertEqual(call["saved_output"], self.original["saved_output"])
        self.assertEqual(self.queued, self.original)
        target = next((self.app.config["DATA_DIR"] / "upload_targets").glob("*.json"))
        durable_target = json.loads(target.read_text())
        self.assertEqual(durable_target["schema"], "study-runner/upload-target/v1")
        self.assertEqual(durable_target["settings"], {"database_id": "durable-db"})
        self.assertEqual(durable_target["study_id"], "Study A")
        self.assertEqual(durable_target["plugin_key"], "notion")
        self.assertIn("expected_settings", durable_target)

    def test_different_target_does_not_inherit_an_old_discovery(self) -> None:
        self.executor(self.discovered)(self.queued)
        other = deepcopy(self.queued)
        settings(other["config_data"])["parent_page_id"] = "different-parent"
        publish = Mock(return_value={"ok": True})
        self.executor(publish)(other)
        self.assertNotIn("database_id", settings(publish.call_args.args[1]["config_data"]))

    def test_failed_study_refresh_does_not_lose_durable_target(self) -> None:
        with patch("study_runner.runtime_core.delivery.upload_runtime.patch_study_plugin_settings", side_effect=OSError("study locked")):
            self.assertTrue(self.executor(self.discovered)(self.queued)["ok"])
        publish = Mock(return_value={"ok": True})
        self.executor(publish)(self.queued)
        self.assertEqual(settings(publish.call_args.args[1]["config_data"])["database_id"], "created-db")
        self.assertEqual(settings(load_config(self.app.config["CONFIG_FILE"]))["database_id"], "created-db")

    def test_invalid_updates_cannot_change_study_or_create_checkpoint(self) -> None:
        before = self.app.config["CONFIG_FILE"].read_bytes()
        for updates in ({"parent_page_id": "unapproved"}, {"database_id": {"nested": "value"}}, {"database_id": ""}, {"database_id": "x" * 4097}, ["database_id"]):
            with self.subTest(updates=updates):
                with self.assertRaises(UploadJobError):
                    self.executor(Mock(return_value={"ok": True, "study_config_updates": updates}))(self.queued)
                self.assertEqual(self.app.config["CONFIG_FILE"].read_bytes(), before)
                self.assertFalse(list((self.app.config["DATA_DIR"] / "upload_targets").glob("*.json")))

    def test_checkpoint_failure_does_not_mark_queue_job_done(self) -> None:
        service = UploadJobService(self.app.config["DATA_DIR"], executors={"notion": self.executor(self.discovered)})
        service.enqueue(kind="notion", study_id="Study A", participant_id="p", session_id="s", label="Notion", payload=self.queued, job_id="job")
        with patch("study_runner.runtime_core.delivery.upload_runtime.atomic_write_json", side_effect=OSError("disk full")):
            service.process_due_jobs_once()
        self.assertEqual(service.counts()["done"], 0)
        self.assertTrue((service.payload_dir / "job.json").is_file())

    def test_corrupt_checkpoint_blocks_publish_instead_of_creating_another_target(self) -> None:
        self.executor(self.discovered)(self.queued)
        checkpoint = next((self.app.config["DATA_DIR"] / "upload_targets").glob("*.json"))
        checkpoint.write_text('{"schema":"invalid"}', encoding="utf-8")
        publish = Mock(return_value={"ok": True})
        with self.assertRaises(UploadJobError):
            self.executor(publish)(self.queued)
        publish.assert_not_called()

    def describer(self):
        plugin = Plugin(key="notion", label="Notion", category="storage", config_key="notion")
        return _retry_target_describer(self.app, plugin)

    def test_retry_target_describer_reports_no_difference_for_the_same_study(self) -> None:
        comparison = self.describer()({"kind": "notion"}, self.queued)
        self.assertEqual(comparison, {"differs": False, "fields": []})

    def test_retry_target_describer_detects_a_changed_parent_page(self) -> None:
        edited = study()
        settings(edited)["parent_page_id"] = "new-parent"
        self.save(edited)

        comparison = self.describer()({"kind": "notion"}, self.queued)
        self.assertTrue(comparison["differs"])
        [field] = comparison["fields"]
        self.assertEqual(field["name"], "parent_page_id")
        self.assertEqual(field["snapshot"], "parent-1")
        self.assertEqual(field["current"], "new-parent")

    def test_retry_target_describer_reports_a_deleted_study(self) -> None:
        self.save(study("Study B"))  # a different study is now active
        delete_study(self.app.config["SAVED_STUDIES_DIR"], "Study A")
        comparison = self.describer()({"kind": "notion"}, self.queued)
        self.assertEqual(comparison, {"differs": False, "fields": [], "study_missing": True})

    def test_retry_target_describer_without_payload_reports_no_difference(self) -> None:
        # A done job's payload file is deleted; a comparison must not crash.
        self.assertEqual(self.describer()({"kind": "notion"}, None), {"differs": False, "fields": []})

    def test_current_target_replaces_the_frozen_selection_before_publishing(self) -> None:
        edited = study()
        settings(edited)["parent_page_id"] = "new-parent"
        self.save(edited)

        publish = Mock(return_value={"ok": True})
        payload_with_target = {**self.queued, "_retry_target": "current"}
        self.executor(publish)(payload_with_target)

        published_settings = settings(publish.call_args.args[1]["config_data"])
        self.assertEqual(published_settings["parent_page_id"], "new-parent")
        # The frozen snapshot passed in is untouched.
        self.assertEqual(settings(self.queued["config_data"])["parent_page_id"], "parent-1")

    def test_current_target_falls_back_to_snapshot_for_a_deleted_study(self) -> None:
        self.save(study("Study B"))  # a different study is now active
        delete_study(self.app.config["SAVED_STUDIES_DIR"], "Study A")
        publish = Mock(return_value={"ok": True})
        payload_with_target = {**self.queued, "_retry_target": "current"}
        self.executor(publish)(payload_with_target)

        published_settings = settings(publish.call_args.args[1]["config_data"])
        self.assertEqual(published_settings["parent_page_id"], "parent-1")

    def test_current_plugin_selection_returns_none_for_an_unselected_plugin(self) -> None:
        self.assertIsNone(_current_plugin_selection(self.app, "Study A", "fixture_export"))
        self.assertIsNone(_current_plugin_selection(self.app, "", "notion"))
        self.assertIsNone(_current_plugin_selection(self.app, "No Such Study", "notion"))


class DestinationDiscoveryManifestTests(unittest.TestCase):
    def test_discovered_fields_must_be_declared_string_settings(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "study_runner/plugins/destinations/notion_upload/manifest.json").read_text())
        for invalid in ({"type": "number"}, {}, "invalid"):
            with self.subTest(invalid=invalid):
                manifest["settings"]["study"]["database_id"] = invalid
                with self.assertRaisesRegex(PluginManifestError, "declared study string"):
                    normalize_manifest(manifest)

    def test_discovery_allowlist_is_explicit_and_validated(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "study_runner/plugins/destinations/notion_upload/manifest.json").read_text())
        self.assertEqual(normalize_manifest(manifest)["capability_config"]["upload_destination"]["discovered_settings"], ["database_id", "data_source_id", "comparison_page_id"])
        capability = manifest["capabilities"]["upload_destination"]
        for invalid in ("database_id", ["database_id", "database_id"], ["nested.field"], [42], [{}]):
            with self.subTest(invalid=invalid):
                capability["discovered_settings"] = invalid
                with self.assertRaises(PluginManifestError):
                    normalize_manifest(manifest)
        capability.pop("discovered_settings")
        self.assertEqual(normalize_manifest(manifest)["capability_config"]["upload_destination"]["discovered_settings"], [])

    def test_target_settings_must_name_hand_entered_study_fields(self) -> None:
        manifest = json.loads((PROJECT_ROOT / "study_runner/plugins/destinations/notion_upload/manifest.json").read_text())
        capability = manifest["capabilities"]["upload_destination"]
        capability["target_settings"] = ["parent_page_id"]
        self.assertEqual(normalize_manifest(manifest)["capability_config"]["upload_destination"]["target_settings"], ["parent_page_id"])
        for invalid in ("parent_page_id", ["database_id"], ["no_such_field"], ["parent_page_id", "parent_page_id"]):
            with self.subTest(invalid=invalid):
                capability["target_settings"] = invalid
                with self.assertRaises(PluginManifestError):
                    normalize_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
