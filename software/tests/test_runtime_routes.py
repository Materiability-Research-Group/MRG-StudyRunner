from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.apps.server import create_app
from study_runner.apps.server.routes.helpers import _plugin_context
from study_runner.plugin_framework.process_host import get_process_runtime


class RuntimeRoutesTests(unittest.TestCase):
    def test_concurrent_start_prevents_a_prevalidated_study_save(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            client = app.test_client()
            config = {
                "study_id": "study-a",
                "study_settings": {"sensors_enabled": False, "sensors": {}, "plugins": {}},
                "questions": [{"type": "participant-id"}, {"type": "finish"}],
            }
            self.assertEqual(client.post("/api/config", json=config).status_code, 200)
            self.assertEqual(client.post("/api/study-client/heartbeat", json={
                "client_id": "tablet-1", "study_id": "study-a", "waiting_for_admin_start": True,
            }).status_code, 200)

            from study_runner.apps.server.routes import study as study_routes
            original_validate = study_routes.validate_and_normalize_config
            validation_started = threading.Event()
            allow_save = threading.Event()
            responses = []

            def paused_validation(value):
                result = original_validate(value)
                validation_started.set()
                if not allow_save.wait(timeout=10):
                    raise TimeoutError("test save validation was not released")
                return result

            def save_from_other_thread():
                responses.append(app.test_client().post("/api/config", json={
                    **config, "questions": [
                        {"type": "participant-id"},
                        {"type": "text", "prompt": "New question"},
                        {"type": "finish"},
                    ],
                }))

            with patch.object(study_routes, "validate_and_normalize_config", side_effect=paused_validation):
                worker = threading.Thread(target=save_from_other_thread)
                worker.start()
                self.assertTrue(validation_started.wait(timeout=10))
                started = client.post("/api/admin/study-run/start", json={})
                allow_save.set()
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
            self.assertEqual(started.status_code, 200)
            self.assertEqual(responses[0].status_code, 409)
            self.assertEqual(responses[0].get_json()["code"], "study_busy")

    def test_machine_admin_plugin_persistence_works_from_reader_thread(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            app.config["HARDWARE_CONFIG"] = {"brainbit": {"enabled": False}}
            with app.app_context():
                context = _plugin_context(machine_admin=True)

            errors: list[BaseException] = []

            def persist_from_reader() -> None:
                try:
                    context.persist_hardware_config(
                        {"brainbit": {"enabled": False, "serial_number": "BB-123"}}
                    )
                except BaseException as error:  # surfaced below in the test thread
                    errors.append(error)

            with patch("study_runner.apps.server.routes.helpers._refresh_trial_runtime") as refresh:
                reader_thread = threading.Thread(target=persist_from_reader)
                reader_thread.start()
                reader_thread.join(2.0)

            persisted = json.loads(Path(app.config["HARDWARE_CONFIG_FILE"]).read_text("utf-8"))

        self.assertFalse(reader_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(persisted["brainbit"]["serial_number"], "BB-123")
        self.assertEqual(app.config["HARDWARE_CONFIG"], persisted)
        refresh.assert_called_once_with()

    def test_health_and_runtime_info_routes(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_APP_MODE": "packaged",
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_PORT": "3123",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            health = client.get("/api/health")
            runtime_info = client.get("/api/runtime-info")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(runtime_info.status_code, 200)

        health_payload = health.get_json()
        info_payload = runtime_info.get_json()
        self.assertEqual(health_payload["status"], "running")
        self.assertEqual(health_payload["app_mode"], "packaged")
        self.assertEqual(info_payload["port"], 3123)
        self.assertEqual(info_payload["admin_url"], "https://localhost:3123/admin")
        self.assertTrue(info_payload["participant_url"].startswith("https://"))
        self.assertTrue(info_payload["uses_external_storage"])

    def test_packaged_restart_returns_packaged_message(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_APP_MODE": "packaged",
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            response = app.test_client().post("/api/admin/restart")

        payload = response.get_json()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(payload["ok"])
        self.assertIn("packaged builds", payload["error"])

    def test_study_session_start_requires_participant_id(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            response = app.test_client().post(
                "/api/study/session/start",
                json={"study_id": "test", "participant_id": ""},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Participant ID", payload["error"])

    def test_admin_study_run_start_gates_new_tablet_flow_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            saved = client.post(
                "/api/config",
                json={
                    "study_id": "study-a",
                    "questions": [
                        {"type": "participant-id"},
                        {"type": "likert", "prompt": "How do you feel?"},
                        {"type": "finish"},
                    ],
                },
            )
            blocked = client.post(
                "/api/study/session/start",
                json={
                    "study_id": "study-a",
                    "participant_id": "p01",
                    "require_admin_start": True,
                },
            )
            heartbeat = client.post(
                "/api/study-client/heartbeat",
                json={
                    "client_id": "tablet-1",
                    "study_id": "study-a",
                    "waiting_for_admin_start": True,
                },
            )
            started = client.post("/api/admin/study-run/start", json={})
            allowed = client.post(
                "/api/study/session/start",
                json={
                    "client_id": "tablet-1",
                    "study_id": "study-a",
                    "participant_id": "p01",
                    "require_admin_start": True,
                },
            )

            with patch.dict(os.environ, env, clear=False):
                restarted_app = create_app()
            persisted = restarted_app.test_client().get("/api/admin/study-run")

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.get_json()["run_state"]["status"], "running")
        self.assertEqual(started.get_json()["run_state"]["active_client_id"], "tablet-1")
        self.assertEqual(started.get_json()["run_state"]["study_revision"], saved.get_json()["config"]["_revision"])
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["study_run_state"]["status"], "running")
        self.assertEqual(allowed.get_json()["session"]["study_revision"], saved.get_json()["config"]["_revision"])
        self.assertEqual(persisted.status_code, 200)
        self.assertEqual(persisted.get_json()["run_state"]["status"], "running")

    def test_admin_study_run_load_rejects_active_run_then_allows_stopped_run(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            saved = client.post(
                "/api/config",
                json={
                    "study_id": "study-a",
                    "questions": [
                        {"type": "participant-id"},
                        {"type": "finish"},
                    ],
                },
            )
            client.post(
                "/api/study-client/heartbeat",
                json={"client_id": "tablet-1", "study_id": "study-a", "waiting_for_admin_start": True},
            )
            started = client.post("/api/admin/study-run/start", json={})
            loaded = client.post("/api/admin/study-run/load", json={"id": "study-a"})
            config = client.get("/api/config")
            stopped = client.post("/api/admin/study-run/stop", json={})
            loaded_after_stop = client.post("/api/admin/study-run/load", json={"id": "study-a"})

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(started.get_json()["run_state"]["status"], "running")
        self.assertEqual(loaded.status_code, 409)
        self.assertEqual(loaded.get_json()["code"], "study_busy")
        self.assertEqual(config.get_json()["_runtime"]["study_run_state"]["status"], "running")
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(loaded_after_stop.status_code, 200)
        self.assertEqual(loaded_after_stop.get_json()["run_state"]["status"], "loaded")

    def test_admin_study_run_start_requires_exactly_one_matching_tablet(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            saved = client.post(
                "/api/config",
                json={"study_id": "study-a", "questions": [{"type": "participant-id"}, {"type": "finish"}]},
            )
            no_tablet = client.post("/api/admin/study-run/start", json={})
            client.post(
                "/api/study-client/heartbeat",
                json={"client_id": "tablet-1", "study_id": "study-a", "waiting_for_admin_start": True},
            )
            client.post(
                "/api/study-client/heartbeat",
                json={"client_id": "tablet-2", "study_id": "study-a", "waiting_for_admin_start": True},
            )
            conflict = client.post("/api/admin/study-run/start", json={})

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(no_tablet.status_code, 409)
        self.assertEqual(no_tablet.get_json()["tablet_gate"]["status"], "waiting_for_tablet")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["tablet_gate"]["status"], "conflict")

    def test_operator_selects_one_of_two_waiting_connections_and_sees_ack(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            client = app.test_client()
            client.post("/api/config", json={
                "study_id": "study-a",
                "questions": [{"type": "participant-id"}, {"type": "finish"}],
            })
            self.assertEqual(client.post("/api/admin/study-run/target", json=[]).status_code, 400)
            client.post("/api/study-client/heartbeat", json={
                "client_id": "loading", "study_id": "", "waiting_for_admin_start": False,
            })
            self.assertEqual(client.post("/api/admin/study-run/target", json={"client_id": "loading"}).status_code, 409)
            client.post("/api/study-client/heartbeat", json={
                "client_id": "other-study", "study_id": "study-b", "waiting_for_admin_start": True,
            })
            self.assertEqual(client.post("/api/admin/study-run/target", json={"client_id": "other-study"}).status_code, 409)
            for client_id in ("tablet-1", "tablet-2"):
                response = client.post("/api/study-client/heartbeat", json={
                    "client_id": client_id, "study_id": "study-a", "waiting_for_admin_start": True,
                })
                self.assertRegex(response.get_json()["display_id"], r"^D-[A-Z2-9]{5}$")
            self.assertEqual(client.post("/api/admin/study-run/start", json={}).status_code, 409)
            selected = client.post("/api/admin/study-run/target", json={"client_id": "tablet-2"})
            self.assertEqual(selected.status_code, 200, selected.get_json())
            started = client.post("/api/admin/study-run/start", json={})
            self.assertEqual(started.status_code, 200, started.get_json())
            run = started.get_json()["run_state"]
            self.assertEqual(run["active_client_id"], "tablet-2")
            before = client.get("/api/admin/study-run").get_json()["tablet_gate"]
            self.assertFalse(before["observed"])
            wrong = client.post("/api/study/session/start", json={
                "client_id": "tablet-1", "participant_id": "p01", "study_id": "study-a",
            })
            self.assertEqual(wrong.status_code, 409)
            client.post("/api/study-client/heartbeat", json={
                "client_id": "tablet-2", "study_id": "study-a",
                "observed_run_id": run["run_id"], "waiting_for_admin_start": False,
            })
            after = client.get("/api/admin/study-run").get_json()["tablet_gate"]
            self.assertTrue(after["observed"])
            self.assertEqual(client.post("/api/admin/study-run/start", json={}).get_json()["run_state"]["run_id"], run["run_id"])
            client.post("/api/admin/study-run/stop", json={})
            moved = client.post("/api/admin/study-run/target", json={"client_id": "tablet-1"})
            self.assertEqual(moved.status_code, 200)
            self.assertEqual(client.get("/api/admin/study-run").get_json()["tablet_gate"]["selected_client_id"], "tablet-1")
            next_run = client.post("/api/admin/study-run/start", json={}).get_json()["run_state"]
            self.assertEqual(next_run["active_client_id"], "tablet-1")
            allowed = client.post("/api/study/session/start", json={
                "client_id": "tablet-1", "participant_id": "p02", "study_id": "study-a",
                "study_run_id": next_run["run_id"], "require_admin_start": True,
            })
            self.assertEqual(allowed.status_code, 200, allowed.get_json())

    def test_non_assigned_tablet_is_blocked_after_admin_start(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            client.post(
                "/api/config",
                json={"study_id": "study-a", "questions": [{"type": "participant-id"}, {"type": "finish"}]},
            )
            client.post(
                "/api/study-client/heartbeat",
                json={"client_id": "tablet-1", "study_id": "study-a", "waiting_for_admin_start": True},
            )
            started = client.post("/api/admin/study-run/start", json={})
            wrong_heartbeat = client.post(
                "/api/study-client/heartbeat",
                json={"client_id": "tablet-2", "study_id": "study-a", "waiting_for_admin_start": True},
            )
            wrong_start = client.post(
                "/api/study/session/start",
                json={
                    "client_id": "tablet-2",
                    "study_id": "study-a",
                    "participant_id": "p02",
                    "require_admin_start": True,
                    "study_run_id": started.get_json()["run_state"]["run_id"],
                },
            )
            allowed = client.post(
                "/api/study/session/start",
                json={
                    "client_id": "tablet-1",
                    "study_id": "study-a",
                    "participant_id": "p01",
                    "require_admin_start": True,
                    "study_run_id": started.get_json()["run_state"]["run_id"],
                },
            )

        self.assertEqual(started.status_code, 200)
        self.assertEqual(wrong_heartbeat.status_code, 200)
        self.assertEqual(wrong_heartbeat.get_json()["study_run_state"]["status"], "blocked")
        self.assertEqual(wrong_start.status_code, 409)
        self.assertEqual(allowed.status_code, 200)

    def test_remove_stored_plugin_config_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            response = app.test_client().delete("/api/admin/plugins/retired_sensor/config", json={})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_remove_stored_plugin_config_rejects_an_installed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            response = app.test_client().delete(
                "/api/admin/plugins/brainbit/config",
                json={"confirm": True},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("installed", response.get_json()["error"])

    def test_remove_stored_plugin_config_deletes_hardware_and_secret_sections(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            hardware_file = Path(app.config["HARDWARE_CONFIG_FILE"])
            hardware_config = json.loads(hardware_file.read_text("utf-8"))
            hardware_config["retired_sensor"] = {"enabled": False, "settings_hidden": True}
            hardware_file.write_text(json.dumps(hardware_config), encoding="utf-8")
            app.config["HARDWARE_CONFIG"] = hardware_config

            secrets_file = Path(app.config["LOCAL_SECRETS_FILE"])
            secrets_file.parent.mkdir(parents=True, exist_ok=True)
            secrets_file.write_text(
                json.dumps({"retired_sensor": {"api_key": "leftover-secret"}}),
                encoding="utf-8",
            )

            response = app.test_client().delete(
                "/api/admin/plugins/retired_sensor/config",
                json={"confirm": True},
            )

            remaining_hardware = json.loads(hardware_file.read_text("utf-8"))
            remaining_secrets = json.loads(secrets_file.read_text("utf-8"))

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["removed"])
        self.assertTrue(payload["secret_removed"])
        self.assertNotIn("retired_sensor", remaining_hardware)
        self.assertNotIn("retired_sensor", remaining_secrets)
        self.assertNotIn("retired_sensor", app.config["HARDWARE_CONFIG"])

    def test_active_study_sensor_toggle_sets_temporary_override(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            app.config["HARDWARE_CONFIG"] = {"brainbit": {"enabled": False}}
            app.config["ACTIVE_STUDY_HARDWARE_CONFIG"] = {"brainbit": {"enabled": True}}
            response = app.test_client().post(
                "/api/admin/plugins/brainbit/enabled",
                json={"enabled": False},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["study_controlled"])
        self.assertTrue(payload["temporary_override"])
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["session_overrides"]["brainbit"])

    def test_internal_lsl_recording_provider_is_not_a_toggleable_plugin(self) -> None:
        """markers/clock_diagnostics are recording code, not plugins -- there is
        nothing at this route for "lsl" to reach, let alone disable."""
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            app.config["HARDWARE_CONFIG"] = {"lsl": {"enabled": True}}
            app.config["ACTIVE_STUDY_HARDWARE_CONFIG"] = {"lsl": {"enabled": True}}
            response = app.test_client().post(
                "/api/admin/plugins/lsl/enabled",
                json={"enabled": False},
            )

            active_config = app.config["ACTIVE_STUDY_HARDWARE_CONFIG"]

        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Unknown plugin", payload["error"])
        self.assertTrue(active_config["lsl"]["enabled"])

    def test_study_runtime_reports_session_override_effective_sensor(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            app.config["SESSION_SENSOR_OVERRIDES"] = {"camera_emotion": True}
            response = app.test_client().get("/api/study/runtime")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["sensor_runtime"]["override_active"]["camera_emotion"])
        self.assertTrue(payload["sensor_runtime"]["effective"]["camera_emotion"])

    def test_admin_status_includes_coordinator_and_clock_sync(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            heartbeat = client.post(
                "/api/study-client/heartbeat",
                json={
                    "client_id": "tablet-1",
                    "study_id": "study-a",
                    "clock_offset_ms": 12.5,
                    "clock_sync_rtt_ms": 24,
                    "plugin_status": {
                        "fixture_sensor": {
                            "state": "warning",
                            "last_error": "sample gap",
                            "active": False,
                            "nested_untrusted": {"ignored": True},
                        }
                    },
                },
            )
            # Exercise route/status assembly with an isolated loader. Real
            # asynchronous driver polls can outlive this temporary data tree.
            with patch(
                "study_runner.data_core.host.sensor_coordinator_service.get_plugin_status",
                return_value={"status": "disabled"},
            ):
                try:
                    status = client.get("/api/admin/status")
                finally:
                    app.config["SENSOR_COORDINATOR"].close(wait=True)

        payload = status.get_json()
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(status.status_code, 200)
        self.assertIn("sensor_coordinator", payload)
        self.assertIn("sample_metadata_model", payload["sensor_coordinator"])
        self.assertIn("recording_infrastructure", payload)
        self.assertIn("canonical_xdf", payload["recording_infrastructure"])
        self.assertIn("supports_merge", payload["recording_infrastructure"])
        self.assertEqual(payload["plugins"]["camera_emotion"]["manifest"]["poll_interval_ms"], 1000)
        self.assertEqual(payload["clock_sync"]["sources"]["tablet-1"]["median_offset_ms"], 12.5)
        client_status = payload["study_clients"]["clients"][0]["plugin_status"]["fixture_sensor"]
        self.assertEqual(client_status["state"], "warning")
        self.assertEqual(client_status["last_error"], "sample gap")
        self.assertFalse(client_status["active"])
        self.assertNotIn("nested_untrusted", client_status)
        self.assertNotIn("camera_permission", payload["study_clients"]["clients"][0])

    def test_emotion_worker_repair_runtime_route_reports_package_and_model_state(self) -> None:
        # The worker log now lives in the data folder and stays open (Windows lock).
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            with patch(
                "study_runner.plugins.sensors.camera_emotion.worker.plugin.repair_runtime",
                return_value={
                    "dependency_install": {"status": "running"},
                    "model_asset_install": {"status": "queued"},
                },
            ):
                response = app.test_client().post(
                    "/api/admin/plugins/camera_emotion/actions/repair_runtime", json={}
                )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["dependency_install"]["status"], "running")
        self.assertEqual(payload["result"]["model_asset_install"]["status"], "queued")

    def test_removed_fixed_key_camera_routes_and_preview_page_are_gone(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            self.assertEqual(client.get("/api/admin/camera/live/status").status_code, 404)
            self.assertEqual(client.post("/api/admin/camera/start").status_code, 404)
            self.assertEqual(client.get("/camera-preview").status_code, 404)

    def test_create_shortcut_route_returns_service_result(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            with patch(
                "study_runner.apps.server.routes.admin.create_desktop_shortcut",
                return_value={"ok": True, "platform": "windows", "path": "C:/Users/test/Desktop/Study Runner.lnk"},
            ):
                response = app.test_client().post("/api/admin/system/create-shortcut")

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("Study Runner", payload["path"])

    def test_nextcloud_password_stays_backend_local_and_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            client = app.test_client()
            saved = client.post(
                "/api/hardware-config",
                json={
                    "nextcloud": {
                        "password": "share-secret",
                    }
                },
            )
            returned = client.get("/api/hardware-config").get_json()
            secrets_file = Path(data_dir) / "settings" / "local_secrets.json"
            secrets = json.loads(secrets_file.read_text(encoding="utf-8"))
            hardware_file = Path(data_dir) / "settings" / "hardware_settings.json"
            hardware = json.loads(hardware_file.read_text(encoding="utf-8"))

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(secrets["nextcloud"]["password"], "share-secret")
        self.assertNotIn("password", hardware["nextcloud"])
        self.assertEqual(returned["nextcloud"]["password"], "")
        self.assertTrue(returned["nextcloud"]["password_configured"])
        self.assertNotIn("share-secret", json.dumps(returned))

    def test_nextcloud_test_connection_is_a_declared_admin_action(self) -> None:
        """Testing a connection has no route of its own -- plugins/nextcloud_upload/
        plugin.py declares it as an admin action and this is the one generic
        dispatch every plugin's actions goes through. API-v5 plugins run in a
        child process, so this test observes the process RPC boundary rather
        than patching a module in the server process (which cannot affect the
        isolated driver)."""
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()

            runtime = get_process_runtime("nextcloud")
            self.assertIsNotNone(runtime)
            # Avoid starting a real child: the contract under test is the
            # generic route -> validated payload -> process request envelope.
            with app.app_context():
                runtime._context = _plugin_context(machine_admin=True)

            def driver_response(operation, payload=None, **_kwargs):
                if operation == "admin_action":
                    return {"ok": True, "endpoint": "dav"}
                if operation == "status":
                    return {"status": "available", "runtime_enabled": True}
                raise AssertionError(f"Unexpected process operation: {operation}")

            with patch.object(runtime, "request", side_effect=driver_response) as request_rpc:
                response = app.test_client().post(
                    "/api/admin/plugins/nextcloud/actions/test_connection",
                    json={
                        "share_link": "https://cloud.example/s/token",
                        "password": "temporary-secret",
                    },
                )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["result"]["ok"])
        admin_request = next(
            call for call in request_rpc.call_args_list if call.args[0] == "admin_action"
        )
        self.assertEqual(
            admin_request.args[1],
            {
                "action": "test_connection",
                "payload": {
                    "share_link": "https://cloud.example/s/token",
                    "password": "temporary-secret",
                },
            },
        )
        self.assertNotIn("temporary-secret", response.get_data(as_text=True))

    def test_changing_a_destinations_parent_setting_clears_its_stale_discovery(self) -> None:
        """Regression: saving a new Notion parent page used to leave the
        previously auto-discovered database id in place, so new sessions
        kept uploading to the old page until some retry happened to notice.
        """
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            client = app.test_client()

            def config(parent_page_id: str, database_id: str) -> dict:
                return {
                    "study_id": "study-a",
                    "study_settings": {
                        "sensors_enabled": False, "sensors": {}, "plugins": {
                            "notion": {
                                "enabled": True, "required": False,
                                "settings": {
                                    "parent_page_id": parent_page_id,
                                    "database_id": database_id,
                                    "data_source_id": "source-1",
                                },
                            },
                        },
                    },
                    "questions": [{"type": "participant-id"}, {"type": "finish"}],
                }

            first = client.post("/api/config", json=config("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "db-1"))
            self.assertEqual(first.status_code, 200)
            notion_settings = first.get_json()["config"]["study_settings"]["plugins"]["notion"]["settings"]
            self.assertEqual(notion_settings["database_id"], "db-1")

            # The operator changes only the parent page; database_id and
            # data_source_id arrive unchanged because they are read-only in
            # the settings UI.
            second = client.post("/api/config", json=config("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "db-1"))

        self.assertEqual(second.status_code, 200)
        saved = second.get_json()["config"]["study_settings"]["plugins"]["notion"]["settings"]
        self.assertEqual(saved["parent_page_id"], "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
        self.assertEqual(saved["database_id"], "", "a database under the old parent page must not be reused")
        self.assertEqual(saved["data_source_id"], "")

    def test_activating_a_different_study_does_not_wipe_its_own_discoveries(self) -> None:
        """Regression: comparing a destination's settings across two
        *different* studies (switching which study is active) must never be
        mistaken for the operator editing one study's target. Study B's own,
        already-discovered database id is unrelated to Study A's and must
        survive activating Study B.
        """
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            client = app.test_client()

            def config(study_id: str, parent_page_id: str, database_id: str) -> dict:
                return {
                    "study_id": study_id,
                    "study_settings": {
                        "sensors_enabled": False, "sensors": {}, "plugins": {
                            "notion": {
                                "enabled": True, "required": False,
                                "settings": {"parent_page_id": parent_page_id, "database_id": database_id},
                            },
                        },
                    },
                    "questions": [{"type": "participant-id"}, {"type": "finish"}],
                }

            self.assertEqual(client.post("/api/config", json=config("study-a", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "db-a")).status_code, 200)
            # Study B is a different study with its own unrelated, already
            # discovered target -- activating it is not an edit of Study A.
            activated = client.post("/api/config", json=config("study-b", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "db-b"))

        self.assertEqual(activated.status_code, 200)
        saved = activated.get_json()["config"]["study_settings"]["plugins"]["notion"]["settings"]
        self.assertEqual(saved["database_id"], "db-b")

    def _save_notion_settings(self, settings: dict):
        with tempfile.TemporaryDirectory() as data_dir:
            env = {
                "STUDY_RUNNER_DATA_DIR": data_dir,
                "STUDY_RUNNER_DISABLE_HARDWARE": "1",
                "STUDY_RUNNER_DISABLE_BACKGROUND": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                app = create_app()
            return app.test_client().post("/api/config", json={
                "study_id": "study-a",
                "study_settings": {
                    "sensors_enabled": False, "sensors": {}, "plugins": {
                        "notion": {"enabled": True, "required": False, "settings": settings},
                    },
                },
                "questions": [{"type": "participant-id"}, {"type": "finish"}],
            })

    def test_an_invalid_notion_layout_is_rejected_at_save_not_at_upload(self) -> None:
        """The route's generic "object" field handling calls the plugin's own
        validator (runtime_core/studies/validation.py), which calls
        layout.validate_layout."""
        response = self._save_notion_settings({"notion_layout": {
            "version": 1, "values": [{"source": "card.stream.brainbit.channel.alpha.mean"}],
        }})

        self.assertEqual(response.status_code, 400)
        self.assertIn("notion_layout", response.get_json()["error"])

    def test_a_valid_notion_layout_round_trips_through_save(self) -> None:
        layout = {
            "version": 1,
            "participants": {"columns": ["session_count"]},
            "answers_per_participant": True,
            "values": [{"source": "answer", "answers": True, "comparison": True}],
            "card_comparison": True,
            "missing": {"number": 9999, "text": "n.a."},
        }
        response = self._save_notion_settings({"notion_layout": layout})

        self.assertEqual(response.status_code, 200)
        saved = response.get_json()["config"]["study_settings"]["plugins"]["notion"]["settings"]["notion_layout"]
        self.assertEqual(saved, layout)

    def test_a_study_saved_by_plugin_1_x_still_saves(self) -> None:
        """Notion 2.0 no longer reads export_mapping; an old study keeps it and saves."""
        response = self._save_notion_settings({
            "export_mapping": {"preset": "as_before", "targets": []},
            "sessions_database_id": "0123456789abcdef0123456789abcdef",
        })

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
