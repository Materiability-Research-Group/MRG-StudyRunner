"""Every card type has a known way into a card comparison table.

One pattern for all card plugins: a new card type fails here until it is
either listed with its own columns in answer_columns.py or knowingly left
to the text fallback - and every answer it can give must arrive in Notion
without losing a part.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.notion_upload.answer_columns import (
    ANSWERLESS_TYPES,
    TYPED_TYPES,
    answer_cells,
    answer_columns,
)
from study_runner.runtime_core.studies.validation import ALLOWED_QUESTION_TYPES
from support.card_type_fixtures import CARD_TYPE_FIXTURES


def leaves(value):
    if isinstance(value, dict):
        for part in value.values():
            yield from leaves(part)
    elif isinstance(value, list):
        for part in value:
            yield from leaves(part)
    else:
        yield value


class NotionAnswerColumnTests(unittest.TestCase):
    def test_every_registered_card_type_is_decided(self) -> None:
        undecided = set(ALLOWED_QUESTION_TYPES) - TYPED_TYPES - ANSWERLESS_TYPES
        self.assertEqual(undecided, set(), "list the new card type in answer_columns.py")

    def test_every_fixture_answer_arrives_without_losing_a_part(self) -> None:
        for kind, fixture in CARD_TYPE_FIXTURES.items():
            if "answer" not in fixture:
                continue
            with self.subTest(kind=kind):
                question = fixture["expected_question"]
                cells = answer_cells(question, fixture["expected_answer"])
                self.assertEqual(set(cells), {name for name, _type in answer_columns(question)})
                self.assertNotIn(None, cells.values())
                written = " ".join(str(item) for item in leaves(cells))
                for part in leaves(fixture["expected_answer"]):
                    self.assertIn(str(float(part) if isinstance(part, (int, float)) else part), written)

    def test_typed_columns(self) -> None:
        self.assertEqual(answer_columns({"type": "slider"}), [("Answer", "number")])
        self.assertEqual(answer_columns({"type": "single", "options": ["a"]}), [("Answer", "select")])
        self.assertEqual(answer_columns({"type": "stimulus"}), [])
        self.assertEqual(
            [name for name, _type in answer_columns({"type": "mood-meter", "variant": "orbit"})],
            ["Answer · words", "Answer · pleasantness", "Answer · energy", "Answer · intensity"],
        )
        self.assertEqual(answer_columns({"type": "mood-meter", "variant": "classic"}), [("Answer", "multi_select")])
        self.assertEqual(answer_columns({"type": "some-future-card"}), [("Answer", "rich_text")])

    def test_a_part_that_is_missing_stays_empty(self) -> None:
        question = {"type": "semantic", "pairs": [["cold", "warm"], ["dull", "bright"]]}
        self.assertEqual(
            answer_cells(question, {"cold_warm": 3}),
            {"Answer · cold–warm": 3.0, "Answer · dull–bright": None},
        )
        ranking = {"type": "ranking", "options": ["a", "b", "c"]}
        self.assertEqual(answer_cells(ranking, ["c", "a"])["Rank · b"], None)
        self.assertEqual(answer_cells(ranking, ["c", "a"])["Rank · c"], 1.0)


if __name__ == "__main__":
    unittest.main()
