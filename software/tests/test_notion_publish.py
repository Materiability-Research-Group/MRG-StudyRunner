"""A whole Notion upload against an in-memory Notion (tests/support/fake_notion.py).

What the operator relies on: one upload builds the layout's tables, every
later upload of the same session (a retry or "Upload again") updates the
same rows instead of adding new ones, and nothing they added in Notion is
removed.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.notion_upload import adapter, notion_api
from study_runner.plugins.destinations.notion_upload.notion_api import NotionApi
from study_runner.runtime_core.studies.validation import validate_and_normalize_config
from support.fake_notion import FakeNotion, FakeNotionError, plain


def study_config() -> dict:
    return validate_and_normalize_config({
        "study_id": "Notion Study",
        "questions": [
            {"type": "participant-id", "prompt": "Who are you?", "fields": {"age_group": {"enabled": True, "store": True}}},
            {"type": "slider", "prompt": "How warm?", "label_min": "cold", "label_max": "warm"},
            {"type": "single", "prompt": "Pick one", "options": ["red", "green"]},
            {"type": "semantic", "prompt": "Rate it", "pairs": [["cold", "warm"]]},
            {"type": "info", "title": "Read this", "text": "Hello"},
            {"type": "finish", "title": "Done"},
        ],
    })


LAYOUT = {
    "version": 1,
    "participants": {"columns": ["session_count", "first_session", "last_session", "age_group"]},
    "answers_per_participant": True,
    "values": [
        {"source": "answer", "answers": True, "comparison": True},
        {"source": "duration_seconds", "answers": True, "comparison": True},
    ],
    "card_comparison": True,
}


def session(config: dict, session_id: str = "session-1", participant_id: str = "p01", start: str = "2026-10-01T10:00:00Z") -> dict:
    questions = config["questions"]
    return {
        "session_id": session_id,
        "participant_id": participant_id,
        "study_id": config["study_id"],
        "timestamp_start": start,
        "participant_metadata": {"age_group": "26-35"},
        "answer_details": [
            {"question_index": 1, "card_id": questions[1]["card_id"], "question_type": "slider", "answer": 72, "interval_seconds": 3.5},
            {"question_index": 2, "card_id": questions[2]["card_id"], "question_type": "single", "answer": "green", "interval_seconds": 2.0},
            {"question_index": 3, "card_id": questions[3]["card_id"], "question_type": "semantic", "answer": None, "skipped": True},
        ],
    }


CARD_SUMMARY = {
    "schema": "study-runner/card-summary/v1",
    "cards": [{"question_index": 1, "streams": {"7": {
        "plugin_key": "eda", "source_id": "study_runner.eda.signal", "channels": {"eda": {"mean": 0.42}},
    }}}],
}


class NotionPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notion = FakeNotion()
        self.parent = self.notion.new_page()
        self.config = study_config()
        self.settings = {"parent_page_id": self.parent, "notion_layout": deepcopy(LAYOUT)}
        no_pause = patch.object(notion_api, "MIN_SECONDS_BETWEEN_CALLS", 0)
        no_pause.start()
        self.addCleanup(no_pause.stop)

    def upload(self, result: dict, *, settings: dict | None = None) -> dict:
        config = deepcopy(self.config)
        config["study_settings"] = {"plugins": {"notion": {"enabled": True, "settings": settings or self.settings}}}
        with patch.object(adapter, "get_client", return_value=self.notion):
            outcome = adapter.upload_study_result(
                result_payload=result,
                saved_output={"session_relative_path": "x", "card_summary": CARD_SUMMARY},
                config_data=config,
                api_key="key",
            )
        self.settings.update(outcome.get("study_config_updates") or {})
        return outcome

    def participants(self) -> str:
        return self.notion.database_titled("Participants", inside=self.parent)

    def comparison_db(self, label_start: str) -> str:
        page = self.settings["comparison_page_id"]
        return next(db for db in self.notion.databases_inside(page) if self.notion.database_store[db]["title"].startswith(label_start))

    def test_the_first_upload_builds_the_whole_layout(self) -> None:
        outcome = self.upload(session(self.config))

        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(set(outcome["study_config_updates"]), {"database_id", "data_source_id", "comparison_page_id"})
        participant = self.notion.rows(self.participants())[0]
        self.assertEqual(plain(participant["properties"]["Participant ID"]), "p01")
        self.assertEqual(participant["properties"]["Sessions"], {"number": 1.0})
        self.assertEqual(participant["properties"]["Age Group"], {"select": {"name": "26-35"}})

        answers = self.notion.database_titled("Answers", inside=participant["id"])
        # Slider, single choice and semantic; the info card is off by default.
        self.assertEqual(len(self.notion.rows(answers)), 3)

        slider = self.comparison_db("02 ·")
        self.assertEqual(self.notion.columns(slider)["Answer"], "number")
        self.assertEqual(self.notion.columns(slider)["Participant"], "relation")
        row = self.notion.rows(slider)[0]
        self.assertEqual(row["properties"]["Answer"], {"number": 72.0})
        self.assertEqual(row["properties"]["Participant"], {"relation": [{"id": participant["id"]}]})
        self.assertEqual(self.notion.columns(self.comparison_db("03 ·"))["Answer"], "select")

    def test_a_skipped_card_gets_its_row_with_the_missing_codes(self) -> None:
        self.upload(session(self.config))

        semantic = self.comparison_db("04 ·")
        row = self.notion.rows(semantic)[0]["properties"]
        self.assertEqual(row["Status"], {"select": {"name": "skipped"}})
        self.assertEqual(row["Answer · cold–warm"], {"number": 9999.0})
        self.assertEqual(row["Duration (s)"], {"number": 9999.0})

    def test_uploading_the_same_session_again_changes_nothing_but_the_values(self) -> None:
        self.upload(session(self.config))
        pages_before = len(self.notion.page_store)
        databases_before = len(self.notion.database_store)

        again = session(self.config)
        again["answer_details"][0]["answer"] = 55
        self.assertTrue(self.upload(again)["ok"])

        self.assertEqual(len(self.notion.page_store), pages_before)
        self.assertEqual(len(self.notion.database_store), databases_before)
        self.assertEqual(self.notion.rows(self.comparison_db("02 ·"))[0]["properties"]["Answer"], {"number": 55.0})
        participant = self.notion.rows(self.participants())[0]
        self.assertEqual(participant["properties"]["Sessions"], {"number": 1.0}, "the same session never counts twice")

    def test_a_second_session_of_the_participant_adds_rows_to_the_same_tables(self) -> None:
        self.upload(session(self.config))
        self.upload(session(self.config, session_id="session-2", start="2026-10-02T10:00:00Z"))

        participant = self.notion.rows(self.participants())
        self.assertEqual(len(participant), 1)
        self.assertEqual(participant[0]["properties"]["Sessions"], {"number": 2.0})
        self.assertEqual(participant[0]["properties"]["Last Session"], {"date": {"start": "2026-10-02T10:00:00Z"}})
        answers = self.notion.database_titled("Answers", inside=participant[0]["id"])
        self.assertEqual(len(self.notion.rows(answers)), 6)
        self.assertEqual(len(self.notion.rows(self.comparison_db("02 ·"))), 2)

    def test_a_changed_layout_adds_columns_and_leaves_old_ones_in_place(self) -> None:
        self.upload(session(self.config))
        slider = self.comparison_db("02 ·")

        changed = deepcopy(self.settings)
        changed["notion_layout"]["values"] = [
            {"source": "answer", "answers": True, "comparison": True},
            {"source": "stream.eda_signal.channel.eda.mean", "answers": False, "comparison": True},
        ]
        self.assertTrue(self.upload(session(self.config), settings=changed)["ok"])

        columns = self.notion.columns(slider)
        self.assertIn("eda_signal · eda · mean", columns)
        self.assertIn("Duration (s)", columns, "a column the layout dropped stays in Notion")
        row = self.notion.rows(slider)[0]["properties"]
        self.assertEqual(row["eda_signal · eda · mean"], {"number": 0.42})

    def test_lost_ids_find_the_same_tables_again(self) -> None:
        self.upload(session(self.config))
        pages_before = len(self.notion.page_store)

        forgotten = {"parent_page_id": self.parent, "notion_layout": deepcopy(LAYOUT)}
        outcome = self.upload(session(self.config), settings=forgotten)

        self.assertTrue(outcome["ok"])
        self.assertEqual(len(self.notion.page_store), pages_before)
        self.assertEqual(outcome["study_config_updates"]["database_id"], self.participants())

    def test_the_participants_table_of_plugin_1_x_is_taken_over(self) -> None:
        legacy = self.notion.add_database(self.parent, "StudyRunner Participants", {
            "Participant ID": "title", "Study Count": "number", "First Session": "date", "Last Session": "date",
        })
        self.upload(session(self.config))

        self.assertEqual(self.settings["database_id"], legacy)
        self.assertEqual(len(self.notion.rows(legacy)), 1)
        self.assertIn("Sessions", self.notion.columns(legacy))

    def test_a_renamed_title_column_still_finds_and_writes_the_participant(self) -> None:
        own = self.notion.add_database(self.parent, "Participants", {"Name": "title"})
        self.upload(session(self.config))
        self.upload(session(self.config))

        rows = self.notion.rows(own)
        self.assertEqual(len(rows), 1)
        self.assertEqual(plain(rows[0]["properties"]["Name"]), "p01")

    def test_a_study_with_only_a_database_id_creates_its_tables_next_to_it(self) -> None:
        legacy = self.notion.add_database(self.parent, "StudyRunner Participants", {"Participant ID": "title"})
        settings = {"database_id": legacy, "notion_layout": deepcopy(LAYOUT)}
        self.assertTrue(self.upload(session(self.config), settings=settings)["ok"])

        comparison = next(page for page, title in self._child_pages(self.parent) if title == "Card comparison")
        self.assertEqual(len(self.notion.databases_inside(comparison)), 3)

    def _child_pages(self, page_id: str):
        return [(block["id"], block["child_page"]["title"]) for block in self.notion.children[page_id] if block["type"] == "child_page"]

    def test_without_the_opt_ins_only_the_participants_table_is_made(self) -> None:
        settings = {"parent_page_id": self.parent, "notion_layout": {}}
        self.assertTrue(self.upload(session(self.config), settings=settings)["ok"])

        self.assertEqual(len(self.notion.database_store), 1)
        participant = self.notion.rows(self.participants())[0]
        self.assertEqual(self.notion.databases_inside(participant["id"]), [])

    def test_call_count_stays_proportional_to_the_cards(self) -> None:
        self.upload(session(self.config))
        self.notion.calls.clear()
        self.upload(session(self.config, session_id="session-2"))

        cards = 3
        self.assertLessEqual(len(self.notion.calls), 5 * cards + 15, self.notion.calls)

    def test_a_disabled_study_is_skipped_without_any_call(self) -> None:
        config = deepcopy(self.config)
        config["study_settings"] = {"plugins": {"notion": {"enabled": False, "settings": self.settings}}}
        with patch.object(adapter, "get_client", return_value=self.notion):
            outcome = adapter.upload_study_result(session(self.config), {}, config, api_key="key")
        self.assertTrue(outcome["skipped"])
        self.assertEqual(self.notion.calls, [])

    def test_a_refused_page_fails_permanently_and_keeps_what_was_created(self) -> None:
        self.upload(session(self.config))
        self.notion.fail_next = [FakeNotionError("restricted_resource", "Insufficient permissions")]
        outcome = self.upload(session(self.config, session_id="session-2"))

        self.assertFalse(outcome["ok"])
        self.assertTrue(outcome["permanent"])
        self.assertIn("Insufficient permissions", outcome["error"])


class NotionApiPacingTests(unittest.TestCase):
    def test_rate_limits_are_waited_out_as_notion_asks(self) -> None:
        waits: list[float] = []
        attempts = []

        def flaky(**kwargs):
            attempts.append(kwargs)
            if len(attempts) < 3:
                raise FakeNotionError("rate_limited", headers={"retry-after": "2"})
            return {"ok": True}

        api = NotionApi(object(), sleep=waits.append, clock=lambda: 0.0)
        self.assertEqual(api.call(flaky, x=1), {"ok": True})
        self.assertEqual(len(attempts), 3)
        self.assertIn(2.0, waits)

    def test_other_errors_are_not_retried(self) -> None:
        api = NotionApi(object(), sleep=lambda _seconds: None)
        calls = []

        def refused(**_kwargs):
            calls.append(1)
            raise FakeNotionError("unauthorized")

        with self.assertRaises(FakeNotionError):
            api.call(refused)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
