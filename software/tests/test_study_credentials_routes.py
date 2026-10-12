"""Flask-level contract for per-study credentials.

The rule the routes have to keep: a credential can be stored and used, but its
value must never come back out over HTTP, and it must never reach the study
file that gets exported.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.apps.server import create_app
from study_runner.runtime_core.settings.secrets_service import load_local_secrets
from study_runner.plugin_framework.plugin_secrets import (
    _credential_declarations,
    get_study_secret,
)

# The env var names now live only in each plugin's manifest; read them from
# there rather than hardcoding them a second time in the test.
NOTION_API_KEY_ENV = _credential_declarations()["notion"]["env_var"]
NEXTCLOUD_PASSWORD_ENV = _credential_declarations()["nextcloud"]["env_var"]


def _app(data_dir: str):
    with patch.dict(
        os.environ,
        {
            "STUDY_RUNNER_DATA_DIR": data_dir,
            "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            NOTION_API_KEY_ENV: "",
            NEXTCLOUD_PASSWORD_ENV: "",
        },
        clear=False,
    ):
        return create_app()


def _save_study(client, study_id: str) -> None:
    response = client.post(
        "/api/config",
        json={
            "study_id": study_id,
            "study_settings": {"notion_enabled": True, "notion_parent_page_id": "11111111111111111111111111111111"},
            "questions": [{"type": "participant-id"}, {"type": "finish"}],
        },
    )
    assert response.status_code == 200, response.get_json()


class StudyCredentialRouteTests(unittest.TestCase):
    def test_get_reports_nothing_configured_initially(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _app(temp_dir).test_client()
            _save_study(client, "Study A")

            body = client.get("/api/admin/studies/Study A/credentials").get_json()

        self.assertTrue(body["ok"])
        self.assertFalse(body["credentials"]["notion"]["configured"])
        self.assertEqual(body["credentials"]["notion"]["scope"], "none")

    def test_post_stores_a_key_and_get_reports_study_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = _app(temp_dir)
            client = app.test_client()
            _save_study(client, "Study A")

            stored = client.post("/api/admin/studies/Study A/credentials", json={"notion": "secret-key"})
            body = client.get("/api/admin/studies/Study A/credentials").get_json()
            on_disk = load_local_secrets(app.config["LOCAL_SECRETS_FILE"])

        self.assertEqual(stored.status_code, 200)
        self.assertTrue(body["credentials"]["notion"]["configured"])
        self.assertEqual(body["credentials"]["notion"]["scope"], "study")
        self.assertEqual(get_study_secret(on_disk, "Study A", "notion"), "secret-key")

    def test_no_route_ever_returns_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = _app(temp_dir)
            client = app.test_client()
            _save_study(client, "Study A")
            client.post(
                "/api/admin/studies/Study A/credentials",
                json={"notion": "top-secret-key", "nextcloud": "top-secret-pw"},
            )

            # Real asynchronous driver polls could outlive the temporary data
            # tree and write logs into it while it is being removed.
            with patch(
                "study_runner.data_core.host.sensor_coordinator_service.get_plugin_status",
                return_value={"status": "disabled"},
            ):
                try:
                    responses = [
                        client.get("/api/admin/studies/Study A/credentials"),
                        client.get("/api/hardware-config"),
                        client.get("/api/admin/status"),
                        client.get("/api/config"),
                    ]
                finally:
                    app.config["SENSOR_COORDINATOR"].close(wait=True)
            self.assertTrue(all(response.status_code == 200 for response in responses))
            bodies = [response.get_data(as_text=True) for response in responses]

        for body in bodies:
            self.assertNotIn("top-secret-key", body)
            self.assertNotIn("top-secret-pw", body)

    def test_clearing_removes_the_stored_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = _app(temp_dir)
            client = app.test_client()
            _save_study(client, "Study A")
            client.post("/api/admin/studies/Study A/credentials", json={"notion": "secret-key"})

            client.post("/api/admin/studies/Study A/credentials", json={"clear_notion": True})
            body = client.get("/api/admin/studies/Study A/credentials").get_json()
            on_disk = load_local_secrets(app.config["LOCAL_SECRETS_FILE"])

        self.assertFalse(body["credentials"]["notion"]["configured"])
        self.assertEqual(get_study_secret(on_disk, "Study A", "notion"), "")

    def test_post_without_any_credential_is_a_bad_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _app(temp_dir).test_client()
            _save_study(client, "Study A")

            response = client.post("/api/admin/studies/Study A/credentials", json={})

        self.assertEqual(response.status_code, 400)

    def test_studies_do_not_share_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _app(temp_dir).test_client()
            _save_study(client, "Study A")
            client.post("/api/admin/studies/Study A/credentials", json={"notion": "key-a"})

            other = client.get("/api/admin/studies/Study B/credentials").get_json()

        self.assertFalse(other["credentials"]["notion"]["configured"])

    def test_renaming_a_study_carries_its_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = _app(temp_dir)
            client = app.test_client()
            _save_study(client, "Old Name")
            client.post("/api/admin/studies/Old Name/credentials", json={"notion": "carried-key"})

            _save_study(client, "New Name")
            renamed = client.get("/api/admin/studies/New Name/credentials").get_json()
            original = client.get("/api/admin/studies/Old Name/credentials").get_json()

        self.assertEqual(renamed["credentials"]["notion"]["scope"], "study")
        # Copied, not moved: the old study file still exists and still works.
        self.assertEqual(original["credentials"]["notion"]["scope"], "study")

    def test_deleting_a_study_forgets_its_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = _app(temp_dir)
            client = app.test_client()
            _save_study(client, "Study A")
            client.post("/api/admin/studies/Study A/credentials", json={"notion": "secret-key"})

            deleted = client.delete("/api/admin/studies/Study A")
            on_disk = load_local_secrets(app.config["LOCAL_SECRETS_FILE"])

        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(get_study_secret(on_disk, "Study A", "notion"), "")

    def test_exported_study_file_carries_targets_but_no_credentials(self) -> None:
        """The whole reason credentials live outside study_settings."""
        with tempfile.TemporaryDirectory() as temp_dir:
            client = _app(temp_dir).test_client()
            _save_study(client, "Study A")
            client.post("/api/admin/studies/Study A/credentials", json={"notion": "must-not-travel"})

            exported_response = client.get("/api/admin/studies/Study A")
            exported = exported_response.get_data(as_text=True)
            exported_payload = exported_response.get_json()

        self.assertNotIn("must-not-travel", exported)
        # The target still travels, so the study runs on another computer.
        self.assertIn("11111111111111111111111111111111", exported)
        settings = exported_payload["study_settings"]
        self.assertNotIn("notion_enabled", settings)
        self.assertNotIn("notion_parent_page_id", settings)
        self.assertTrue(settings["plugins"]["notion"]["enabled"])
        self.assertEqual(
            settings["plugins"]["notion"]["settings"]["parent_page_id"],
            "11111111111111111111111111111111",
        )

    def test_stored_key_is_used_for_uploads_of_that_study(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app = _app(temp_dir)
            client = app.test_client()
            _save_study(client, "Study A")
            client.post("/api/admin/studies/Study A/credentials", json={"notion": "study-key"})

            from study_runner.plugin_framework.plugin_secrets import resolve_plugin_secret

            resolved = resolve_plugin_secret(
                "notion",
                app.config.get("HARDWARE_CONFIG", {}),
                load_local_secrets(app.config["LOCAL_SECRETS_FILE"]),
                "Study A",
            )

        self.assertEqual(resolved, "study-key")


if __name__ == "__main__":
    unittest.main()
