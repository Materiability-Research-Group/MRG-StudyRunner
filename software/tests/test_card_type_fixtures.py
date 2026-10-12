"""Package 5g.B1: the golden fixtures actually agree with validation.py.

The point of this suite is narrow on purpose: prove that
``CARD_TYPE_FIXTURES`` (tests/support/card_type_fixtures.py) is not stale
relative to the real code, and that the fixture set itself has not drifted
from the card types the application actually registers. It is the safety
net later 5g stages (B3's table refactor, B5's extension manifests) get
checked against -- not a place to add new card-type behavior coverage,
which belongs in test_validation.py.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from uuid import NAMESPACE_URL, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from study_runner.runtime_core.studies.validation import (
    ALLOWED_QUESTION_TYPES,
    NON_ANSWER_QUESTION_TYPES,
    validate_and_normalize_config,
    validate_and_normalize_results,
)
from support.card_type_fixtures import CARD_TYPE_FIXTURES, STIMULUS_EXCLUDED_KEYS


class FixtureSetCoverageTests(unittest.TestCase):
    """The fixture set itself must track the registry, not just be complete today."""

    def test_every_allowed_question_type_has_a_fixture(self) -> None:
        missing = ALLOWED_QUESTION_TYPES - set(CARD_TYPE_FIXTURES)
        self.assertEqual(missing, set(), f"card types with no golden fixture: {sorted(missing)}")

    def test_the_fixture_set_names_no_type_the_registry_does_not_know(self) -> None:
        """A stale fixture for a removed type would otherwise pass silently forever."""
        extra = set(CARD_TYPE_FIXTURES) - ALLOWED_QUESTION_TYPES
        self.assertEqual(extra, set(), f"fixtures for unknown card types: {sorted(extra)}")

    def test_answerable_fixtures_match_non_answer_question_types_exactly(self) -> None:
        """Whether a fixture carries "answer" must track NON_ANSWER_QUESTION_TYPES."""
        has_answer = {
            card_type for card_type, fixture in CARD_TYPE_FIXTURES.items() if "answer" in fixture
        }
        expected = set(CARD_TYPE_FIXTURES) - NON_ANSWER_QUESTION_TYPES
        self.assertEqual(has_answer, expected)


def _build_config(card_type: str, question: dict) -> dict:
    return validate_and_normalize_config({"study_id": "Fixture Study", "questions": [question]})


class GoldenFixtureTests(unittest.TestCase):
    """Each fixture, run through the real entry points, matches its frozen output."""

    def test_every_fixture_question_normalizes_to_its_frozen_shape(self) -> None:
        for card_type, fixture in CARD_TYPE_FIXTURES.items():
            with self.subTest(card_type=card_type):
                config = _build_config(card_type, fixture["question"])
                actual = config["questions"][0]
                expected = {**fixture["expected_question"],
                            "card_id": str(uuid5(NAMESPACE_URL, "study-runner:Fixture Study:0"))}
                if card_type == "stimulus":
                    # See card_type_fixtures.py's module docstring: this
                    # block depends on which plugins happen to be
                    # installed, not on card-type validation semantics.
                    actual = {key: value for key, value in actual.items() if key not in STIMULUS_EXCLUDED_KEYS}
                    self.assertIn("plugin_actions", config["questions"][0])
                    self.assertIsInstance(config["questions"][0]["plugin_actions"], dict)
                self.assertEqual(actual, expected)

    def test_every_fixture_answer_normalizes_to_its_frozen_shape(self) -> None:
        for card_type, fixture in CARD_TYPE_FIXTURES.items():
            if "answer" not in fixture:
                continue
            with self.subTest(card_type=card_type):
                config = _build_config(card_type, fixture["question"])
                result = validate_and_normalize_results(
                    {
                        "participant_id": "p1",
                        "timestamp_start": "2026-01-01T00:00:00Z",
                        "timestamp_end": "2026-01-01T00:05:00Z",
                        "answers": {"q0": fixture["answer"]},
                    },
                    config,
                )
                self.assertEqual(result["answers"]["q0"], fixture["expected_answer"])

    def test_non_answer_fixtures_reject_a_submitted_answer(self) -> None:
        """participant-id/stimulus/finish are not questions with a stored answer."""
        # The participant-id fixture's own required-and-stored fields, kept
        # separate from card_type_fixtures.py: unrelated to what this test
        # checks (that "no answer" is fine for a non-answer card), and
        # duplicating it there would only be more to keep in sync.
        participant_metadata = {
            "age_group": "18-25",
            "childhood_area": "urban",
            "childhood_nearest_city": "Springfield",
        }
        for card_type in NON_ANSWER_QUESTION_TYPES:
            with self.subTest(card_type=card_type):
                fixture = CARD_TYPE_FIXTURES[card_type]
                config = _build_config(card_type, fixture["question"])
                result = validate_and_normalize_results(
                    {
                        "participant_id": "p1",
                        "timestamp_start": "2026-01-01T00:00:00Z",
                        "timestamp_end": "2026-01-01T00:05:00Z",
                        "answers": {},
                        "participant_metadata": (
                            participant_metadata if card_type == "participant-id" else {}
                        ),
                    },
                    config,
                )
                self.assertEqual(result["answers"], {})


if __name__ == "__main__":
    unittest.main()
