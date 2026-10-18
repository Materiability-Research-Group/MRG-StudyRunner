"""Which Notion upload failures stop retrying, and what the operator is told."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.notion_upload.adapter import classify_notion_error  # noqa: E402


class FakeNotionError(Exception):
    def __init__(self, code: str, message: str = "details") -> None:
        super().__init__(message)
        self.code = code


class NotionErrorClassificationTests(unittest.TestCase):
    def test_access_and_id_errors_are_permanent_with_a_fix(self) -> None:
        permanent, message = classify_notion_error(FakeNotionError("unauthorized"))
        self.assertTrue(permanent)
        self.assertIn("API key", message)
        permanent, message = classify_notion_error(FakeNotionError("APIErrorCode.ObjectNotFound"))
        self.assertTrue(permanent)
        self.assertIn("Connections", message)
        permanent, _ = classify_notion_error(FakeNotionError("restricted_resource"))
        self.assertTrue(permanent)

    def test_missing_page_setting_is_permanent(self) -> None:
        permanent, message = classify_notion_error(ValueError("parent_page_id is required"))
        self.assertTrue(permanent)
        self.assertIn("Notion page", message)

    def test_network_and_rate_limits_keep_retrying(self) -> None:
        for error in (OSError("connection interrupted"), FakeNotionError("rate_limited"), FakeNotionError("internal_server_error")):
            permanent, message = classify_notion_error(error)
            self.assertFalse(permanent)
            self.assertEqual(message, str(error))


if __name__ == "__main__":
    unittest.main()
