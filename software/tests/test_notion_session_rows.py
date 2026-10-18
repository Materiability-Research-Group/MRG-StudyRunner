"""One session as Notion rows: statuses, missing codes, older sessions."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.notion_upload.layout import effective_layout
from study_runner.plugins.destinations.notion_upload.session_rows import (
    card_rows,
    comparison_cells,
    decode_sessions,
    encode_sessions,
    fill_missing,
    session_facts,
)

CONFIG = {"questions": [
    {"type": "slider", "card_id": "c0", "prompt": "Warm?"},
    {"type": "text", "card_id": "c1", "prompt": "Why?"},
    {"type": "stimulus", "card_id": "c2", "title": "Clip"},
    {"type": "single", "card_id": "c3", "prompt": "Pick", "options": ["a", "b"]},
]}
LAYOUT = effective_layout({
    "version": 1,
    "values": [
        {"source": "answer", "answers": True, "comparison": True},
        {"source": "stream.eda_signal.channel.eda.mean", "answers": True, "comparison": True},
    ],
    "card_comparison": True,
}, CONFIG)


def result(details):
    return {"session_id": "s1", "participant_id": "p1", "timestamp_start": "2026-10-01T10:00:00Z", "answer_details": details}


SUMMARY = {"cards": [
    {"question_index": 0, "streams": {"3": {"plugin_key": "eda", "source_id": "study_runner.eda.signal", "channels": {"eda": {"mean": 0.5}}}}},
    {"question_index": 1, "streams": {"3": {"plugin_key": "eda", "source_id": "study_runner.eda.signal", "channels": {"eda": {"mean": 0.7}}}}},
]}


class NotionSessionRowsTests(unittest.TestCase):
    def test_each_card_says_what_happened_to_it(self) -> None:
        rows = card_rows(LAYOUT, CONFIG, result([
            {"card_id": "c0", "question_index": 0, "answer": 4},
            {"card_id": "c1", "question_index": 1, "answer": None, "skipped": True},
            {"card_id": "c2", "question_index": 2, "answer": "stimulus"},
        ]), SUMMARY)

        by_card = {row["card"]["card_id"]: row for row in rows}
        self.assertEqual({key: row["status"] for key, row in by_card.items()},
                         {"c0": "answered", "c1": "skipped", "c2": "shown", "c3": "not shown"})
        self.assertEqual(by_card["c0"]["values"]["stream.eda_signal.channel.eda.mean"], 0.5)
        # Skipped means no value at all, even though the sensor kept recording.
        self.assertIsNone(by_card["c1"]["values"]["stream.eda_signal.channel.eda.mean"])
        self.assertIsNone(by_card["c2"]["answer"], "a stimulus has no answer, only its values")

    def test_missing_codes_follow_the_column_type(self) -> None:
        missing = {"number": 9999, "text": "n.a."}
        self.assertEqual(fill_missing("number", None, missing), 9999)
        self.assertEqual(fill_missing("select", "", missing), "n.a.")
        self.assertEqual(fill_missing("multi_select", [], missing), ["n.a."])
        self.assertIsNone(fill_missing("date", None, missing))
        self.assertEqual(fill_missing("number", 0, missing), 0, "zero is a value, not a gap")

    def test_the_comparison_row_is_keyed_by_the_session(self) -> None:
        rows = card_rows(LAYOUT, CONFIG, result([{"card_id": "c3", "question_index": 3, "answer": "b"}]), {})
        row = next(item for item in rows if item["card"]["card_id"] == "c3")
        cells = comparison_cells(LAYOUT, session_facts(result([])), row)
        self.assertEqual(cells["StudyRunner Key"], "s1")
        self.assertEqual(cells["Participant ID"], "p1")
        self.assertEqual(cells["Answer"], "b")

    def test_a_session_recorded_before_card_ids_is_matched_by_position_and_text(self) -> None:
        rows = card_rows(LAYOUT, CONFIG, result([
            {"question_index": 0, "question_prompt": "Warm?", "answer": 2},
            {"question_index": 1, "question_prompt": "A different question", "answer": "x"},
        ]), {})
        by_card = {row["card"]["card_id"]: row["status"] for row in rows}
        self.assertEqual(by_card["c0"], "answered")
        self.assertEqual(by_card["c1"], "not shown", "a card whose text changed is not guessed")

    def test_the_session_list_never_counts_a_session_twice(self) -> None:
        text = encode_sessions([("s2", "2026-10-02"), ("s1", "2026-10-01"), ("s1", "2026-10-01")])
        self.assertEqual(decode_sessions(text), [("s1", "2026-10-01"), ("s2", "2026-10-02")])
        self.assertEqual(decode_sessions(""), [])

    def test_a_result_without_ids_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            session_facts({"session_id": "s1"})


if __name__ == "__main__":
    unittest.main()
