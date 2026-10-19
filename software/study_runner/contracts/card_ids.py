"""The stable id of a study card, also for studies saved before card ids existed.

A card without a ``card_id`` gets one derived from its study and position.
The study validator assigns it when a legacy study is saved; a session
recorded before then still carries the id-less copy of its study, and
``with_card_ids`` gives that copy the very ids the saved study got, so both
name the same card.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5


def legacy_card_id(study_id: str, question_index: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"study-runner:{study_id}:{question_index}"))


def with_card_ids(config_data: Any) -> Any:
    """A copy of a study in which every card has its id; ``config_data`` is not changed."""
    if not isinstance(config_data, dict):
        return config_data
    questions = config_data.get("questions")
    if not isinstance(questions, list) or all(
        not isinstance(question, dict) or question.get("card_id") for question in questions
    ):
        return config_data
    study_id = str(config_data.get("study_id") or "").strip()
    copy = deepcopy(config_data)
    for index, question in enumerate(copy["questions"]):
        if isinstance(question, dict) and not question.get("card_id"):
            question["card_id"] = legacy_card_id(study_id, index)
    return copy
