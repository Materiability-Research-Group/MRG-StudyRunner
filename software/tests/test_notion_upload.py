"""The Notion plugin's boundary: its study settings, actions and API key handling.

What an upload writes is covered by test_notion_publish.py, the layout by
test_notion_layout.py.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.runtime_core.studies.validation import validate_and_normalize_config
from study_runner.plugins.destinations.notion_upload import adapter, notion_api, plugin
from support.fake_notion import FakeNotion

LAYOUT = {
    "version": 1,
    "answers_per_participant": True,
    "values": [{"source": "answer", "answers": True, "comparison": True}],
    "card_comparison": True,
}
CONFIG = {
    "study_id": "study-a",
    "questions": [
        {"type": "participant-id", "card_id": "c0", "fields": {"age_group": {"enabled": True, "store": True}}},
        {"type": "slider", "card_id": "c1", "prompt": "Warm?"},
        {"type": "finish", "card_id": "c2"},
    ],
}


def write_session(data_dir: Path, study_id: str = "study-a") -> str:
    session = data_dir / study_id / "p01" / "20260101T100000Z__session-1"
    (session / "answers").mkdir(parents=True)
    (session / "COMPLETE.json").write_text("{}", encoding="utf-8")
    (session / "answers" / "result.json").write_text(json.dumps({
        "session_id": "session-1", "participant_id": "p01", "study_id": study_id,
        "timestamp_start": "2026-01-01T10:00:00Z",
        "answer_details": [{"question_index": 1, "card_id": "c1", "question_type": "slider", "answer": 61}],
    }), encoding="utf-8")
    (session / "answers" / "card-summary.json").write_text(json.dumps({
        "schema": "study-runner/card-summary/v1",
        "cards": [{"question_index": 1, "streams": {"1": {
            "plugin_key": "brainbit", "source_id": "study_runner.brainbit.eeg",
            "channels": {"alpha": {"mean": 1.0}},
        }}}],
    }), encoding="utf-8")
    return f"{study_id}/p01/20260101T100000Z__session-1"


class NotionValidateStudySettingTests(unittest.TestCase):
    """`Plugin.validate_study_setting`, the hook validation.py calls for the
    parent page (a field with a format) and the "object" notion_layout
    field - see runtime_core/studies/validation.py."""

    PAGE_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"

    def test_ignores_fields_it_does_not_own(self) -> None:
        plugin.PLUGIN.validate_study_setting("database_id", "not json at all")  # does not raise
        plugin.PLUGIN.validate_study_setting("export_mapping", "{left by plugin 1.x")  # no longer read

    def test_accepts_a_pasted_link_or_an_id_as_the_parent_page(self) -> None:
        for value in (
            self.PAGE_ID,
            "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
            f"https://www.notion.so/team/Study-results-{self.PAGE_ID}?source=copy_link",
            "",
        ):
            plugin.PLUGIN.validate_study_setting("parent_page_id", value)  # does not raise

    def test_rejects_a_parent_page_without_an_id_and_says_how_to_get_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "Copy link"):
            plugin.PLUGIN.validate_study_setting("parent_page_id", "Study results")

    def test_rejects_malformed_json_and_an_invalid_layout(self) -> None:
        with self.assertRaises(ValueError):
            plugin.PLUGIN.validate_study_setting("notion_layout", "{not json")
        with self.assertRaisesRegex(ValueError, "version"):
            plugin.PLUGIN.validate_study_setting("notion_layout", json.dumps({"version": 9}))

    def test_accepts_an_empty_and_a_full_layout(self) -> None:
        plugin.PLUGIN.validate_study_setting("notion_layout", "{}")
        plugin.PLUGIN.validate_study_setting("notion_layout", json.dumps(LAYOUT))


class NotionAdminActionDispatchTests(unittest.TestCase):
    """plugin.py#_run_admin_action routes each configurator action."""

    def _context(self, hardware_config=None):
        return SimpleNamespace(hardware_config=hardware_config or {}, data_dir=Path("/tmp"), secret=lambda *_: "study-key")

    def test_unknown_and_removed_actions_are_rejected(self) -> None:
        for action in ("not_a_real_action", "list_children", "create_database", "prepare_schema"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                plugin._run_admin_action(self._context(), action, {})

    def test_check_structure_dispatches_with_the_resolved_api_key(self) -> None:
        with patch.object(adapter, "check_structure", return_value={"ok": True}) as called:
            plugin._run_admin_action(self._context(), "check_structure", {
                "layout_json": "{}", "config_data_json": "{}", "settings_json": "{}", "apply": True,
            })
        called.assert_called_once_with(
            api_key="study-key", layout_json="{}", config_data_json="{}", settings_json="{}", apply=True,
        )

    def test_test_connection_uses_the_machine_timeout_when_the_panel_sends_none(self) -> None:
        # The study settings panel never asks for a timeout; it is a machine setting.
        with patch.object(adapter, "test_connection", return_value={"ok": True}) as called:
            plugin._run_admin_action(self._context({"notion": {"timeout_seconds": 42}}), "test_connection", {"parent_page_id": "page-1"})
        self.assertEqual(called.call_args.kwargs["timeout_seconds"], 42)

    def test_publish_reads_the_study_key(self) -> None:
        with patch.object(adapter, "upload_study_result", return_value={"ok": True}) as called:
            plugin._publish(self._context(), {"config_data": {"study_id": "study-a"}, "result_payload": {}, "saved_output": {}})
        self.assertEqual(called.call_args.kwargs["api_key"], "study-key")


class NotionParentPageIsCheckedWhenTheStudyIsSavedTests(unittest.TestCase):
    """validation.py hands a text field with a declared format to its plugin."""

    def _config(self, parent_page_id: str) -> dict:
        return validate_and_normalize_config({
            "study_id": "Notion Parent Page",
            "questions": [{"type": "participant-id", "prompt": "Identify yourself."}, {"type": "finish"}],
            "study_settings": {
                "plugins": {"notion": {"enabled": True, "settings": {"parent_page_id": parent_page_id}}},
            },
        })

    def test_a_pasted_link_is_accepted_and_kept_as_typed(self) -> None:
        link = "https://www.notion.so/team/Results-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?source=copy_link"
        config = self._config(link)
        self.assertEqual(config["study_settings"]["plugins"]["notion"]["settings"]["parent_page_id"], link)

    def test_text_without_a_page_id_is_refused_with_a_clear_message(self) -> None:
        from study_runner.runtime_core.studies.validation import ValidationError

        with self.assertRaisesRegex(ValidationError, "parent_page_id: is not a Notion page link or ID") as raised:
            self._config("Study results")
        # The message crosses the plugin process boundary intact, "•••" included.
        self.assertIn("••• > Copy link", str(raised.exception))


class NotionIdFromInputTests(unittest.TestCase):
    """Operators paste the parent page as a whole "Copy link" link."""

    ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"

    def test_reads_the_id_from_ids_and_links(self) -> None:
        from study_runner.plugins.destinations.notion_upload.notion_ids import notion_id_from_input

        cases = {
            self.ID: self.ID,
            "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d": self.ID,
            f"https://www.notion.so/team/Study-results-{self.ID}?source=copy_link": self.ID,
            # Removing every dash would glue a hex-letter title to the ID.
            f"https://www.notion.so/Cafe-{self.ID}": self.ID,
            f"notion.so/team/Page-{self.ID}?pvs=4": self.ID,
            f"https://www.notion.so/team/{'0' * 32}?v={'f' * 32}&p={self.ID}&pm=s": self.ID,
            f"https://www.notion.so/team/Study-results-{self.ID.upper()}/": self.ID,
            "Study results": "",
            "https://example.com/page": "",
            "": "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(notion_id_from_input(value), expected)

    def test_the_upload_reads_pasted_links_wherever_it_takes_an_id(self) -> None:
        self.assertEqual(notion_api.compact_id(f"https://www.notion.so/team/Results-{self.ID}"), self.ID)
        self.assertEqual(notion_api.compact_id("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"), self.ID)


class NotionUploadGuardTests(unittest.TestCase):
    def _config(self, settings: dict) -> dict:
        return {**CONFIG, "study_settings": {"plugins": {"notion": {"enabled": True, "settings": settings}}}}

    def test_a_missing_key_fails_permanently_with_what_to_do(self) -> None:
        with patch.object(adapter, "_config", {"enabled": True, "api_key": ""}):
            result = adapter.upload_study_result({}, {}, self._config({"parent_page_id": "p"}), api_key="")
        self.assertTrue(result["permanent"])
        self.assertIn("API key", result["error"])

    def test_a_missing_parent_page_fails_permanently(self) -> None:
        with patch.object(adapter, "get_client", return_value=FakeNotion()):
            result = adapter.upload_study_result(
                {"session_id": "s", "participant_id": "p"}, {}, self._config({}), api_key="key",
            )
        self.assertTrue(result["permanent"])
        self.assertIn("Notion page", result["error"])

    def test_an_invalid_layout_is_refused_before_any_call(self) -> None:
        notion = FakeNotion()
        with patch.object(adapter, "get_client", return_value=notion):
            result = adapter.upload_study_result({}, {}, self._config({"notion_layout": {"version": 7}}), api_key="key")
        self.assertTrue(result["permanent"])
        self.assertEqual(notion.calls, [])

    def test_a_broken_card_summary_is_refused(self) -> None:
        result = adapter.upload_study_result(
            {}, {"session_relative_path": "x", "card_summary": {"schema": "other"}}, self._config({}), api_key="key",
        )
        self.assertFalse(result["ok"])


class NotionConfiguratorActionTests(unittest.TestCase):
    def setUp(self) -> None:
        no_pause = patch.object(notion_api, "MIN_SECONDS_BETWEEN_CALLS", 0)
        no_pause.start()
        self.addCleanup(no_pause.stop)

    def test_describe_layout_sources_lists_cards_columns_and_recorded_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_session(Path(temp_dir))
            result = adapter.describe_layout_sources(
                data_dir=temp_dir, config_data_json=json.dumps(CONFIG), session_path=path, study_id="study-a",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual([card["card_id"] for card in result["cards"]], ["c1"])
        available = {column["key"]: column["available"] for column in result["participant_columns"]}
        self.assertTrue(available["age_group"])
        self.assertFalse(available["gender"])
        self.assertIn("stream.brainbit_eeg.channel.alpha.mean", [value["source"] for value in result["values"]])

    def test_preview_reads_a_session_from_disk_without_notion(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_session(Path(temp_dir))
            result = adapter.preview_layout(
                data_dir=temp_dir, layout_json=json.dumps(LAYOUT), config_data_json=json.dumps(CONFIG),
                session_path=path, study_id="study-a",
            )
        self.assertTrue(result["ok"], result)
        [row] = result["rows"]
        self.assertEqual(row["status"], "answered")
        self.assertEqual(row["comparison"]["Answer"], 61.0)

    def test_preview_stays_inside_this_studys_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_session(Path(temp_dir))
            for session_path, study in ((path, "study-b"), ("../../etc/passwd", "study-a"), ("study-a/p01/none", "study-a")):
                with self.subTest(session_path=session_path):
                    result = adapter.preview_layout(
                        data_dir=temp_dir, layout_json="{}", config_data_json=json.dumps(CONFIG),
                        session_path=session_path, study_id=study,
                    )
                    self.assertFalse(result["ok"])

    def test_check_structure_reports_without_writing_and_creates_on_apply(self) -> None:
        notion = FakeNotion()
        parent = notion.new_page()
        payload = {
            "api_key": "key",
            "layout_json": json.dumps(LAYOUT),
            "config_data_json": json.dumps(CONFIG),
            "settings_json": json.dumps({"parent_page_id": parent}),
        }
        with patch.object(adapter, "get_client", return_value=notion):
            dry = adapter.check_structure(**payload, apply=False)
            self.assertNotIn("databases.create", notion.calls)
            applied = adapter.check_structure(**payload, apply=True)

        self.assertTrue(dry["ok"], dry)
        self.assertEqual({table["status"] for table in dry["tables"]}, {"create"})
        self.assertEqual(dry["settings_updates"], {})
        self.assertTrue(applied["applied"])
        self.assertEqual(set(applied["settings_updates"]), {"database_id", "data_source_id", "comparison_page_id"})
        self.assertEqual(len(notion.database_store), 2)  # Participants and the one slider card

    def test_check_structure_needs_the_page_first(self) -> None:
        result = adapter.check_structure(
            api_key="key", layout_json="{}", config_data_json="{}", settings_json="{}", apply=False,
        )
        self.assertFalse(result["ok"])
        self.assertIn("Notion page", result["error"])


class NotionErrorClassificationTests(unittest.TestCase):
    def test_a_rate_limit_is_retried_and_a_refusal_is_not(self) -> None:
        self.assertFalse(adapter.classify_notion_error(SimpleNamespace(code="rate_limited"))[0])
        self.assertTrue(adapter.classify_notion_error(SimpleNamespace(code="APIErrorCode.Unauthorized"))[0])


if __name__ == "__main__":
    unittest.main()
