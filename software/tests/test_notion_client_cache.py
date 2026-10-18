"""The Notion client cache that replaced the module-level singleton.

The adapter used to hold exactly one `_client` built at initialize() time from
the one machine-wide API key. Per-study keys need more than one client alive at
once, so clients are now cached by a hash of their key. These tests pin the
behaviour that the old singleton provided, so the refactor cannot regress
uploads, plus the new multi-key behaviour.
"""
from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.notion_upload import adapter


class FakeClient:
    """Stands in for notion_client.Client; records what it was built with."""

    def __init__(self, auth: str, timeout_ms: int = 0) -> None:
        self.auth = auth
        self.timeout_ms = timeout_ms


def _fake_notion_module() -> types.ModuleType:
    module = types.ModuleType("notion_client")
    module.Client = FakeClient
    return module


class NotionClientCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._modules = patch.dict(sys.modules, {"notion_client": _fake_notion_module()})
        self._modules.start()
        self._requirements = patch.object(adapter, "ensure_requirements", return_value=True)
        self._requirements.start()
        self.addCleanup(self._modules.stop)
        self.addCleanup(self._requirements.stop)
        adapter._clients.clear()
        adapter._config = {}

    def _initialize(self, **overrides) -> None:
        arguments = {
            "enabled": True,
            "api_key": "machine-key",
            "timeout_seconds": 10,
            "data_dir": Path("."),
        }
        arguments.update(overrides)
        adapter.initialize(**arguments)

    def test_initialize_warms_a_client_for_the_machine_key(self) -> None:
        self._initialize()

        self.assertEqual(len(adapter._clients), 1)
        self.assertTrue(adapter.get_status()["connected"])

    def test_same_key_is_reused_not_rebuilt(self) -> None:
        self._initialize()

        first = adapter.get_client("machine-key")
        second = adapter.get_client("machine-key")

        self.assertIs(first, second)
        self.assertEqual(len(adapter._clients), 1)

    def test_different_keys_get_different_clients(self) -> None:
        self._initialize()

        machine = adapter.get_client("machine-key")
        study = adapter.get_client("study-key")

        self.assertIsNot(machine, study)
        self.assertEqual(machine.auth, "machine-key")
        self.assertEqual(study.auth, "study-key")
        self.assertEqual(len(adapter._clients), 2)

    def test_initialize_clears_previously_cached_clients(self) -> None:
        """The hardware-config save path relies on this to pick up a new key."""
        self._initialize()
        adapter.get_client("study-key")
        self.assertEqual(len(adapter._clients), 2)

        self._initialize(api_key="rotated-key")

        self.assertEqual(len(adapter._clients), 1)
        self.assertEqual(adapter.get_client("rotated-key").auth, "rotated-key")

    def test_disabled_integration_yields_no_client(self) -> None:
        self._initialize(enabled=False)

        self.assertIsNone(adapter.get_client("machine-key"))
        self.assertFalse(adapter.get_status()["connected"])

    def test_missing_key_yields_no_client(self) -> None:
        self._initialize(api_key="")

        self.assertIsNone(adapter.get_client(""))
        self.assertFalse(adapter.get_status()["connected"])

    def test_timeout_is_passed_through_to_the_client(self) -> None:
        self._initialize(timeout_seconds=7)

        self.assertEqual(adapter.get_client("machine-key").timeout_ms, 7000)

    def test_cache_key_never_contains_the_plaintext_key(self) -> None:
        self._initialize()
        adapter.get_client("super-secret-key")

        self.assertNotIn("super-secret-key", "".join(adapter._clients))

    def test_upload_reports_not_ready_without_a_client(self) -> None:
        self._initialize(enabled=False)

        result = adapter.upload_study_result(
            result_payload={"participant_id": "p01"},
            saved_output={},
            config_data={"study_settings": {"plugins": {"notion": {"enabled": True, "settings": {}}}}},
        )

        self.assertFalse(result["ok"])
        self.assertIn("not ready", result["error"])
        self.assertTrue(result["permanent"], "a switched-off integration cannot fix itself by retrying")

    def test_upload_still_skips_when_the_study_disabled_notion(self) -> None:
        self._initialize()

        result = adapter.upload_study_result(
            result_payload={"participant_id": "p01"},
            saved_output={},
            config_data={"study_settings": {"plugins": {"notion": {"enabled": False, "settings": {}}}}},
        )

        self.assertTrue(result["skipped"])


if __name__ == "__main__":
    unittest.main()
