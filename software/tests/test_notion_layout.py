"""The Notion layout setting: its defaults, what it refuses, which cards it lists."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.notion_upload.layout import (
    LayoutError,
    card_label,
    effective_layout,
    included_cards,
    listed_cards,
    validate_layout,
    value_column_name,
)

CONFIG = {
    "questions": [
        {"type": "participant-id", "card_id": "c0", "fields": {"age_group": {"enabled": True, "store": True}, "gender": {"enabled": True}}},
        {"type": "slider", "card_id": "c1", "prompt": "How   warm\nis it?"},
        {"type": "info", "card_id": "c2", "title": "Read"},
        {"type": "stimulus", "card_id": "c3", "title": "Clip"},
        {"type": "text", "prompt": "Not saved yet"},
        {"type": "finish", "card_id": "c5", "title": "Done"},
    ],
}


class NotionLayoutTests(unittest.TestCase):
    def test_an_empty_layout_sends_only_the_participants_table(self) -> None:
        layout = effective_layout({}, CONFIG)
        self.assertEqual(layout["participants"]["columns"], ["session_count", "first_session", "last_session", "age_group"])
        self.assertFalse(layout["answers_per_participant"])
        self.assertFalse(layout["card_comparison"])
        self.assertEqual([item["source"] for item in layout["values"]], ["answer", "duration_seconds"])
        self.assertEqual(layout["missing"], {"number": 9999, "text": "n.a."})

    def test_valid_layouts_pass(self) -> None:
        validate_layout({})
        validate_layout({
            "version": 1,
            "participants": {"columns": ["session_count", "birth_date"]},
            "answers_per_participant": True,
            "values": [
                {"source": "answer", "answers": True, "comparison": True},
                {"source": "stream.brainbit_eeg.channel.alpha.mean", "answers": False, "comparison": True},
            ],
            "cards": {"c1": False},
            "card_comparison": True,
            "missing": {"number": -99, "text": "NA"},
        })

    def test_anything_an_upload_would_have_to_guess_is_refused(self) -> None:
        invalid = [
            [],
            {"version": 2},
            {"version": 1, "extra": True},
            {"version": 1, "participants": {"columns": ["shoe_size"]}},
            {"version": 1, "answers_per_participant": "yes"},
            {"version": 1, "values": [{"source": "card.answer"}]},
            {"version": 1, "values": [{"source": "answer"}, {"source": "answer"}]},
            {"version": 1, "values": [{"source": "stream.x.channel.y.median"}]},
            {"version": 1, "cards": {"c1": "off"}},
            {"version": 1, "missing": {"number": "nine"}},
            {"version": 1, "missing": {"text": "n,a"}},
            {"version": 1, "missing": {"number": float("nan")}},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(LayoutError):
                validate_layout(value)

    def test_cards_list_what_can_appear_and_include_by_default(self) -> None:
        self.assertEqual([card["card_id"] for card in listed_cards(CONFIG)], ["c1", "c2", "c3"])
        layout = effective_layout({}, CONFIG)
        self.assertEqual([card["card_id"] for card in included_cards(layout, CONFIG)], ["c1", "c3"])
        layout["cards"] = {"c2": True, "c3": False}
        self.assertEqual([card["card_id"] for card in included_cards(layout, CONFIG)], ["c1", "c2"])

    def test_a_card_label_is_position_and_one_line_of_text(self) -> None:
        self.assertEqual(card_label(1, CONFIG["questions"][1]), "02 · How warm is it?")
        self.assertTrue(card_label(0, {"prompt": "x" * 200}).endswith("…"))
        self.assertEqual(value_column_name("stream.brainbit_eeg.channel.alpha.mean"), "brainbit_eeg · alpha · mean")
        self.assertEqual(value_column_name("duration_seconds"), "Duration (s)")


if __name__ == "__main__":
    unittest.main()
