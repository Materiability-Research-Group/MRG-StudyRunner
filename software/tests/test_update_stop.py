"""What an in-app update stops before it restarts the server.

Only a session that is still live may be aborted. The recording runtime
also reports the last session after it ended (status ``frozen``); aborting
that one marked a finished, uploaded session "aborted" for good.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.apps.server.routes import update


class StopForUpdateTests(unittest.TestCase):
    def stop(self, current_status: dict | None) -> tuple[dict, Mock]:
        app = Flask(__name__)
        recording = Mock()
        recording.current_status.return_value = current_status
        recording.find_paths.return_value = Mock(root=Path("session-root"))
        withdrawal = Mock()
        withdrawal.abort.return_value = {"status": "withdrawn"}
        app.config.update(RECORDING_RUNTIME_SERVICE=recording, WITHDRAWAL_SERVICE=withdrawal)
        with app.app_context(), \
                patch.object(update, "_study_run_state", return_value={"status": "idle"}), \
                patch.object(update, "_active_study_id", return_value="study"), \
                patch.object(update, "_stop_study_sensor_runtime"):
            return update._stop_everything_for_update(), withdrawal

    def test_a_live_recording_is_aborted(self) -> None:
        stopped, withdrawal = self.stop({"session_id": "s1", "status": "recording"})
        self.assertTrue(stopped["session_aborted"])
        withdrawal.abort.assert_called_once()

    def test_a_session_that_already_ended_is_left_alone(self) -> None:
        stopped, withdrawal = self.stop({"session_id": "s1", "status": "frozen"})
        self.assertFalse(stopped["session_aborted"])
        withdrawal.abort.assert_not_called()


if __name__ == "__main__":
    unittest.main()
