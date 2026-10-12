"""Characterization tests that lock the HTTP surface and updater wire format.

These are the safety net for structural refactors (blueprint split,
updater-crypto dedup): if a route is dropped/renamed or the signed
payload bytes change, an installed older client would break silently.
"""
from __future__ import annotations

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


EXPECTED_ROUTES = {
    ("GET", "/"),
    ("GET", "/admin"),
    ("GET", "/api/admin/data-folder"),
    ("POST", "/api/admin/data-folder"),
    ("POST", "/api/admin/data-folder/choose"),
    ("GET", "/api/admin/certificate/export"),
    ("POST", "/api/admin/certificate/import"),
    ("GET", "/api/admin/certificate/status"),
    ("POST", "/api/admin/branding/<slot>"),
    ("DELETE", "/api/admin/branding/<slot>"),
    ("GET", "/api/branding"),
    ("GET", "/api/branding/asset/<slot>"),
    ("GET", "/api/branding/fonts"),
    ("GET", "/api/branding/font/<slot>"),
    ("POST", "/api/admin/branding/font/<slot>"),
    ("DELETE", "/api/admin/branding/font/<slot>"),
    ("PUT", "/api/admin/branding/fonts"),
    ("GET", "/api/study-assets/<asset_id>"),
    ("POST", "/api/admin/study-assets"),
    ("GET", "/api/admin/studies/<study_id>/package"),
    ("POST", "/api/admin/studies/import"),
    ("GET", "/api/uploads/status"),
    ("GET", "/api/uploads/<job_id>/retry-target"),
    ("POST", "/api/uploads/acknowledge"),
    ("POST", "/api/uploads/retry"),
    ("POST", "/api/admin/notion/backfill"),
    ("POST", "/api/admin/system/open-results-folder"),
    ("POST", "/api/admin/plugins/<plugin_key>/<action>"),
    ("DELETE", "/api/admin/plugins/<plugin_key>/config"),
    ("POST", "/api/admin/plugins/<plugin_key>/enabled"),
    ("GET", "/api/admin/plugin-settings"),
    ("GET", "/api/admin/recording-timing"),
    ("POST", "/api/admin/recording-timing"),
    ("POST", "/api/admin/plugin-settings/<plugin_key>"),
    ("POST", "/api/admin/plugins/<plugin_key>/actions/<action_key>"),
    ("GET", "/api/admin/plugins/<plugin_key>/console"),
    ("GET", "/api/admin/plugins/<plugin_key>/console/events"),
    ("POST", "/api/admin/plugins/<plugin_key>/console/input"),
    ("POST", "/api/admin/plugins/<plugin_key>/console/unlock"),
    ("GET", "/api/admin/recovery"),
    ("POST", "/api/admin/recovery/discard"),
    ("POST", "/api/admin/recovery/finalize"),
    ("POST", "/api/admin/restart"),
    ("POST", "/api/admin/session-overrides/reset"),
    ("GET", "/api/admin/sessions"),
    ("GET", "/api/admin/sessions/<study_id>/<participant_id>"),
    ("GET", "/api/admin/sessions/<study_id>/<participant_id>/signals"),
    ("POST", "/api/admin/sessions/<study_id>/<participant_id>/withdraw"),
    ("GET", "/api/admin/status"),
    ("GET", "/api/admin/dashboard-layout"),
    ("PUT", "/api/admin/dashboard-layout"),
    ("DELETE", "/api/admin/dashboard-layout"),
    ("GET", "/api/admin/study-readiness"),
    ("GET", "/api/admin/study-run"),
    ("POST", "/api/admin/study-run/load"),
    ("POST", "/api/admin/study-run/start"),
    ("POST", "/api/admin/study-run/target"),
    ("POST", "/api/admin/study-run/stop"),
    ("POST", "/api/admin/study-run/abort"),
    ("GET", "/api/admin/study-run/live-check"),
    ("GET", "/api/admin/notices"),
    ("POST", "/api/admin/notices/ack"),
    ("POST", "/api/admin/trials/prepare-overrides"),
    ("GET", "/api/admin/trials/prepare-overrides/<event_id>"),
    ("GET", "/api/admin/studies"),
    ("DELETE", "/api/admin/studies/<study_id>"),
    ("GET", "/api/admin/studies/<study_id>"),
    ("GET", "/api/admin/studies/<study_id>/credentials"),
    ("POST", "/api/admin/studies/<study_id>/credentials"),
    ("POST", "/api/admin/studies/active"),
    ("POST", "/api/admin/system/create-shortcut"),
    ("POST", "/api/admin/update/check"),
    ("POST", "/api/admin/update/download"),
    ("POST", "/api/admin/update/install"),
    ("GET", "/api/admin/update/status"),
    ("GET", "/api/config"),
    ("POST", "/api/config"),
    ("GET", "/api/hardware-config"),
    ("POST", "/api/hardware-config"),
    ("GET", "/api/health"),
    ("POST", "/api/marker"),
    ("GET", "/api/finalization/status"),
    ("GET", "/api/finalization/<job_id>"),
    ("GET", "/api/finalization/<job_id>/retry-target"),
    ("POST", "/api/finalization/<job_id>/retry"),
    ("POST", "/api/finalization/<job_id>/acknowledge"),
    ("POST", "/api/finalization/<job_id>/confirm-degraded"),
    ("POST", "/api/finalization/<job_id>/continue"),
    ("POST", "/api/finalization/<job_id>/open-folder"),
    ("GET", "/api/plugins/catalog"),
    ("GET", "/api/plugins/<plugin_key>/assets/<path:asset_path>"),
    ("GET", "/api/plugins/<plugin_key>/card-defaults"),
    ("POST", "/api/plugins/<plugin_key>/participant/actions/<action_key>"),
    ("POST", "/api/plugins/<plugin_key>/participant/ingest/<ingest_key>"),
    ("POST", "/api/results"),
    ("POST", "/api/results/partial"),
    ("GET", "/api/runtime-info"),
    ("POST", "/api/start"),
    ("POST", "/api/stop"),
    ("POST", "/api/study-client/heartbeat"),
    ("GET", "/api/study/runtime"),
    ("POST", "/api/study/session/client-event"),
    ("POST", "/api/study/session/resume"),
    ("POST", "/api/study/session/start"),
    ("POST", "/api/study/session/stop"),
    ("POST", "/api/sync-clock"),
    ("POST", "/api/trial/prepare"),
    ("POST", "/api/trial/prepare/cancel"),
    ("GET", "/static/<path:filename>"),
}


def _make_app(data_dir: str):
    env = {
        "STUDY_RUNNER_DATA_DIR": data_dir,
        "STUDY_RUNNER_DISABLE_HARDWARE": "1",
    }
    with patch.dict(os.environ, env, clear=False):
        return create_app()


class RouteInventoryTests(unittest.TestCase):
    def test_http_surface_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            app = _make_app(data_dir)
            actual = {
                (method, rule.rule)
                for rule in app.url_map.iter_rules()
                for method in rule.methods
                if method not in {"HEAD", "OPTIONS"}
            }

        missing = EXPECTED_ROUTES - actual
        added = actual - EXPECTED_ROUTES
        self.assertFalse(missing, f"routes disappeared: {sorted(missing)}")
        self.assertFalse(added, f"new routes must be added to EXPECTED_ROUTES: {sorted(added)}")

    def test_pages_and_core_endpoints_respond(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            app = _make_app(data_dir)
            client = app.test_client()

            for path in ("/", "/admin"):
                with client.get(path) as response:
                    self.assertEqual(response.status_code, 200, path)
                    self.assertIn(b"<", response.data)

            # The audit text lives in the settings shell now; the page is gone.
            with client.get("/audit") as response:
                self.assertEqual(response.status_code, 404)

            config = client.get("/api/config")
            self.assertEqual(config.status_code, 200)
            self.assertIn("study_id", config.get_json())

            studies = client.get("/api/admin/studies")
            self.assertEqual(studies.status_code, 200)

            sync = client.post("/api/sync-clock", json={"client_send_ms": 12.5})
            payload = sync.get_json()
            self.assertEqual(sync.status_code, 200)
            self.assertEqual(payload["client_send_ms"], 12.5)
            self.assertLessEqual(payload["server_receive_ms"], payload["server_send_ms"])

            marker = client.post(
                "/api/marker",
                json={
                    "send_signal": False,
                    "study_id": "Study A",
                    "participant_id": "p01",
                    "question_index": 1,
                    "question_type": "likert",
                    "phase": "shown",
                    "marker_event": "question_shown",
                    "clock_offset_ms": 250.5,
                    "client_trigger_epoch_ms": 1760000000123.4,
                },
            )
            self.assertEqual(marker.status_code, 200)
            self.assertTrue(marker.get_json()["ok"])

            runtime = client.get("/api/study/runtime")
            self.assertEqual(runtime.status_code, 200)

    def test_removed_compatibility_routes_return_not_found(self) -> None:
        removed_routes = (
            ("get", "/api/notion/status"),
            ("post", "/api/notion/flush-queue"),
            ("post", "/api/admin/brainbit/start"),
            ("post", "/api/admin/brainbit/stop"),
            ("post", "/api/admin/brainbit/restart"),
            ("post", "/api/admin/brainbit/select-device"),
            ("post", "/api/admin/radar/start"),
            ("post", "/api/admin/radar/stop"),
            ("post", "/api/admin/radar/restart"),
            ("post", "/api/camera/frame"),
        )
        with tempfile.TemporaryDirectory() as data_dir:
            client = _make_app(data_dir).test_client()
            for method, path in removed_routes:
                with self.subTest(path=path):
                    response = getattr(client, method)(path, json={} if method == "post" else None)
                    self.assertEqual(response.status_code, 404)


class UpdaterWireFormatTests(unittest.TestCase):
    def test_canonical_asset_payload_bytes_are_frozen(self) -> None:
        """Installed 0.3.x clients verify signatures over exactly these bytes.

        If this test fails after a refactor, older installations can no
        longer verify new release manifests. Do not change the expected
        bytes; change the code back.
        """
        from study_runner.runtime_core.settings.update_service import canonical_asset_payload

        payload = canonical_asset_payload(
            "9.9.9",
            "windows-x86_64",
            {
                "url": "https://example.com/asset.zip",
                "sha256": "AABBCC",
                "size": 12345,
                "extra_field_is_ignored": True,
            },
        )

        self.assertEqual(
            payload,
            b'{"platform":"windows-x86_64","schema":1,"sha256":"aabbcc",'
            b'"size":12345,"url":"https://example.com/asset.zip","version":"9.9.9"}',
        )


if __name__ == "__main__":
    unittest.main()
