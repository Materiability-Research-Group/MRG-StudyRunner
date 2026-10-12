from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.plugins.destinations.nextcloud_upload.webdav_client import (
    NextcloudPublicShareClient,
    parse_share_link,
    test_connection,
)


class FakeResponse:
    def __init__(self, status_code: int, *, headers=None, content: bytes = b"") -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.content = content

    def iter_content(self, chunk_size=1024):
        del chunk_size
        yield self.content


class FakeSession:
    def __init__(self, statuses: list[int], *, echo_hash: bool = True) -> None:
        self.statuses = list(statuses)
        self.calls: list[dict] = []
        self.echo_hash = echo_hash
        self.remote_content: dict[str, bytes] = {}

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "GET" and url not in self.remote_content:
            return FakeResponse(404)
        if method == "DELETE" and url not in self.remote_content:
            return FakeResponse(404)
        status = self.statuses.pop(0)
        if method == "PUT":
            data = kwargs.get("data")
            content = data.read() if hasattr(data, "read") else bytes(data or b"")
            self.remote_content[url] = content
            headers = {"X-Hash": kwargs.get("headers", {}).get("X-Hash", "")} if self.echo_hash else {}
            return FakeResponse(status, headers=headers)
        if method == "GET":
            return FakeResponse(status, content=self.remote_content.get(url, b""))
        if method == "DELETE":
            self.remote_content.pop(url, None)
            return FakeResponse(status)
        return FakeResponse(status)


class NextcloudServiceTests(unittest.TestCase):
    def test_parse_share_link_supports_root_and_subpath_installations(self) -> None:
        self.assertEqual(
            parse_share_link("https://cloud.example/s/AbC_123-xy"),
            ("https://cloud.example", "AbC_123-xy"),
        )
        self.assertEqual(
            parse_share_link("https://cloud.example/nextcloud/index.php/s/token123/"),
            ("https://cloud.example/nextcloud", "token123"),
        )

    def test_parse_share_link_rejects_non_share_and_credential_urls(self) -> None:
        for invalid in (
            "",
            "https://cloud.example/files/token",
            "https://user:secret@cloud.example/s/token",
            "https://cloud.example/s/token?password=secret",
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                parse_share_link(invalid)

    def test_primary_endpoint_is_selected_by_propfind(self) -> None:
        session = FakeSession([207, 201, 200, 204])
        client = NextcloudPublicShareClient(
            "https://cloud.example/s/token",
            password="secret",
            session=session,
        )

        result = client.test_connection()

        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "dav")
        self.assertEqual(session.calls[0]["method"], "PROPFIND")
        self.assertEqual(session.calls[0]["headers"], {"Depth": "0"})
        self.assertEqual(session.calls[0]["auth"], ("token", "secret"))
        self.assertNotIn("secret", session.calls[0]["url"])

    def test_legacy_endpoint_is_used_after_primary_404_or_405(self) -> None:
        for fallback_status in (404, 405):
            with self.subTest(status=fallback_status):
                session = FakeSession([fallback_status, 207, 201, 200, 204])
                result = test_connection(
                    "https://cloud.example/s/token",
                    session=session,
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["endpoint"], "legacy_webdav")
                self.assertIn("/public.php/dav/files/token", session.calls[0]["url"])
                self.assertTrue(session.calls[1]["url"].endswith("/public.php/webdav"))

    def test_upload_creates_folders_idempotently_and_puts_each_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "result.json").write_bytes(b"{}")
            (folder / "signals.xdf").write_bytes(b"xdf")
            (folder / "ignored-folder").mkdir()
            session = FakeSession([207, 201, 405, 201, 204])
            client = NextcloudPublicShareClient(
                "https://cloud.example/s/token",
                session=session,
            )

            result = client.upload_session_folder(
                folder,
                study_id="Study A",
                participant_id="P 01",
            )

        self.assertTrue(result["ok"])
        self.assertEqual([item["name"] for item in result["uploaded"]], ["result.json", "signals.xdf"])
        self.assertEqual(
            [call["method"] for call in session.calls],
            ["PROPFIND", "MKCOL", "MKCOL", "GET", "PUT", "GET", "PUT"],
        )
        self.assertIn("/Study%20A", session.calls[1]["url"])
        self.assertIn("/P%2001", session.calls[2]["url"])
        put_calls = [call for call in session.calls if call["method"] == "PUT"]
        self.assertTrue(all(call["headers"]["X-Hash"].startswith("sha256:") for call in put_calls))

    def test_get_hash_fallback_verifies_remote_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "result.json").write_bytes(b"result")
            session = FakeSession([207, 201, 405, 201, 200], echo_hash=False)
            client = NextcloudPublicShareClient(
                "https://cloud.example/s/token",
                session=session,
            )

            result = client.upload_session_folder(
                folder,
                study_id="study",
                participant_id="p01",
            )

        self.assertEqual([call["method"] for call in session.calls][-2:], ["PUT", "GET"])
        self.assertEqual(result["remote_sha256"]["result.json"], result["uploaded"][0]["sha256"])

    def test_canonical_session_tree_is_recursive_and_completion_marker_is_last(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "raw/plugins/sensor").mkdir(parents=True)
            (folder / "raw/plugins/sensor/part-0001.xdf").write_bytes(b"xdf")
            (folder / "result.json").write_bytes(b"{}")
            (folder / "COMPLETE.json").write_bytes(b"{}")
            # endpoint + five canonical collections + raw/plugins/sensor + 3 PUTs
            session = FakeSession([207, 201, 201, 201, 201, 201, 201, 201, 201, 201, 201, 201])
            client = NextcloudPublicShareClient("https://cloud.example/s/token", session=session)

            result = client.upload_session_folder(
                folder,
                study_id="Study",
                participant_id="P01",
                session_relative_path="Study/P01/20260731T120000Z__s1",
            )

        put_urls = [call["url"] for call in session.calls if call["method"] == "PUT"]
        self.assertTrue(put_urls[-1].endswith("/COMPLETE.json"))
        self.assertEqual(
            next(call for call in session.calls if call["method"] == "DELETE")["url"].split("/")[-1],
            "ATTENTION_REQUIRED.json",
        )
        self.assertIn("raw/plugins/sensor/part-0001.xdf", result["remote_sha256"])
        self.assertEqual(result["remote_path"], "Study/P01/20260731T120000Z__s1")

    def test_existing_immutable_artifact_with_different_hash_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "result.json").write_bytes(b"new-result")
            session = FakeSession([207, 201, 405, 200])
            remote_url = (
                "https://cloud.example/public.php/dav/files/token/"
                "study/p01/result.json"
            )
            session.remote_content[remote_url] = b"different-existing-result"
            client = NextcloudPublicShareClient("https://cloud.example/s/token", session=session)

            with self.assertRaisesRegex(Exception, "different content"):
                client.upload_session_folder(
                    folder,
                    study_id="study",
                    participant_id="p01",
                )

        self.assertFalse(any(call["method"] == "PUT" for call in session.calls))

    def test_retrying_a_completed_upload_writes_nothing_again(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            (folder / "signals.xdf").write_bytes(b"xdf")
            session = FakeSession([207, 201, 405, 201, 200])
            client = NextcloudPublicShareClient("https://cloud.example/s/token", session=session)
            client.upload_session_folder(folder, study_id="study", participant_id="p01")
            session.calls.clear()
            session.statuses = [405, 405, 200]

            retry = client.upload_session_folder(folder, study_id="study", participant_id="p01")

        self.assertTrue(retry["ok"])
        self.assertTrue(all(item.get("skipped_existing") for item in retry["uploaded"]))
        self.assertFalse(any(call["method"] in {"PUT", "DELETE"} for call in session.calls))

    def test_connection_test_proves_the_share_is_writable_and_cleans_up(self) -> None:
        session = FakeSession([207, 201, 200, 204])
        result = test_connection("https://cloud.example/s/token", session=session)
        self.assertTrue(result["ok"])
        self.assertIn("writable", result["message"])
        self.assertEqual([call["method"] for call in session.calls], ["PROPFIND", "PUT", "GET", "DELETE"])
        self.assertEqual(session.remote_content, {})

    def test_a_read_only_share_fails_the_connection_test(self) -> None:
        session = FakeSession([207, 403])
        result = test_connection("https://cloud.example/s/token", session=session)
        self.assertFalse(result["ok"])
        self.assertIn("read-only", result["error"])

    def test_a_file_drop_share_fails_the_connection_test(self) -> None:
        session = FakeSession([207, 201, 403])
        result = test_connection("https://cloud.example/s/token", session=session)
        self.assertFalse(result["ok"])
        self.assertIn("file-drop", result["error"])

    def test_authentication_failure_is_plain_and_does_not_echo_secret(self) -> None:
        session = FakeSession([401])

        result = test_connection(
            "https://cloud.example/s/token",
            password="do-not-echo",
            session=session,
        )

        self.assertFalse(result["ok"])
        self.assertNotIn("do-not-echo", result["error"])
        self.assertIn("rejected", result["error"])

    def test_invalid_remote_identifier_is_rejected_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = FakeSession([207])
            client = NextcloudPublicShareClient(
                "https://cloud.example/s/token",
                session=session,
            )
            with self.assertRaises(ValueError):
                client.upload_session_folder(
                    Path(temp_dir),
                    study_id="../escape",
                    participant_id="p01",
                )


class NextcloudAdminActionTests(unittest.TestCase):
    def test_test_connection_uses_the_machine_timeout_when_the_panel_sends_none(self) -> None:
        # The study settings panel never asks for a timeout; it is a machine setting.
        from types import SimpleNamespace
        from unittest.mock import patch

        from study_runner.plugins.destinations.nextcloud_upload import plugin, webdav_client

        context = SimpleNamespace(hardware_config={"nextcloud": {"timeout_seconds": 25}}, secret=lambda *_: "pw")
        with patch.object(webdav_client, "test_connection", return_value={"ok": True}) as called:
            plugin._run_admin_action(context, "test_connection", {"share_link": "https://cloud.example/s/token"})
        self.assertEqual(called.call_args.kwargs["timeout_seconds"], 25)


if __name__ == "__main__":
    unittest.main()
