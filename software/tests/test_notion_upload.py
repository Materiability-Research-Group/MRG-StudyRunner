from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from study_runner.runtime_core.studies.validation import validate_and_normalize_config
from study_runner.plugins.destinations.notion_upload import adapter, plugin


class NotionParticipantMetadataTests(unittest.TestCase):
    def _config(self) -> dict:
        return validate_and_normalize_config(
            {
                "study_id": "Notion Metadata",
                "questions": [
                    {"type": "participant-id", "prompt": "Identify yourself."},
                    {"type": "finish"},
                ],
            }
        )

    def test_metadata_schema_uses_only_stored_fields_by_default(self) -> None:
        schema = adapter._build_participant_metadata_schema(self._config())

        self.assertNotIn("First Name", schema)
        self.assertNotIn("Last Name", schema)
        self.assertEqual(schema["Age Group"], {"select": {}})
        self.assertEqual(schema["Childhood Area"], {"select": {}})
        self.assertEqual(schema["Childhood Nearest City"], {"rich_text": {}})

    def test_metadata_schema_includes_names_when_configured_for_storage(self) -> None:
        config = self._config()
        config["questions"][0]["fields"]["first_name"]["store"] = True

        schema = adapter._build_participant_metadata_schema(config)

        self.assertEqual(schema["First Name"], {"rich_text": {}})

    def test_metadata_properties_match_notion_property_types(self) -> None:
        props = adapter._build_participant_metadata_properties(
            {
                "participant_metadata": {
                    "age_group": "18-25",
                    "childhood_area": "urban",
                    "childhood_nearest_city": "Munich",
                }
            },
            self._config(),
        )

        self.assertEqual(props["Age Group"]["select"]["name"], "18-25")
        self.assertEqual(props["Childhood Area"]["select"]["name"], "urban")
        self.assertEqual(
            props["Childhood Nearest City"]["rich_text"][0]["text"]["content"],
            "Munich",
        )

    def test_optional_metadata_fields_have_complete_notion_mappings(self) -> None:
        config = self._config()
        fields = config["questions"][0]["fields"]
        for field_key in ("gender", "birth_place", "birth_date"):
            fields[field_key]["enabled"] = True
            fields[field_key]["store"] = True

        schema = adapter._build_participant_metadata_schema(config)
        props = adapter._build_participant_metadata_properties(
            {
                "participant_metadata": {
                    "gender": "Non-binary",
                    "birth_place": "Berlin",
                    "birth_date": "1990-05-04",
                }
            },
            config,
        )

        self.assertEqual(schema["Gender"], {"select": {}})
        self.assertEqual(schema["Birth Place"], {"rich_text": {}})
        self.assertEqual(schema["Birth Date"], {"date": {}})
        self.assertEqual(props["Gender"], {"select": {"name": "Non-binary"}})
        self.assertEqual(
            props["Birth Place"]["rich_text"][0]["text"]["content"],
            "Berlin",
        )
        self.assertEqual(props["Birth Date"], {"date": {"start": "1990-05-04"}})

    def test_answer_table_lists_every_card_with_answer_and_duration(self) -> None:
        rows = adapter._answer_table_rows(
            {
                "answer_details": [
                    {"question_number": 1, "question_type": "stimulus", "question_prompt": "Observe", "answer": "stimulus",
                     "interval_seconds": 7.0},
                    {"question_number": 2, "question_type": "mood-meter", "question_prompt": "How do you feel?",
                     "answer": ["Calm", "Content"], "shown_at": "2026-01-01T10:00:00Z", "answered_at": "2026-01-01T10:00:04Z"},
                    {"question_number": 3, "question_type": "likert", "question_prompt": "Optional", "answer": None,
                     "skipped": True},
                ]
            }
        )

        self.assertEqual(rows[0], ["1", "stimulus", "Observe", "(Stimulus)", "7.0"])
        self.assertEqual(rows[1], ["2", "mood-meter", "How do you feel?", "Calm, Content", "4.0"])
        self.assertEqual(rows[2][3], "\u2014 (übersprungen)")

    def test_canonical_answers_never_include_embedded_ram_biomarkers(self) -> None:
        rows = adapter._answer_table_rows(
            {"answer_details": [{"question_number": 1, "question_type": "likert", "answer": 4,
                                 "biosignal_interval": {"brainbit": {"available": True, "avg_attention": 12345.0}}}]}
        )
        self.assertNotIn("12345", json.dumps(rows))

    def test_biosignal_table_renders_unknown_plugins_per_card_without_core_changes(self) -> None:
        rows = adapter._biosignal_table_rows(
            {
            "schema": "study-runner/card-summary/v1",
            "cards": [
                {
                    "question_index": 1,
                    "streams": {
                        "future.metrics": {
                            "plugin_key": "future_sensor",
                            "count": 10,
                            "valid_count": 9,
                            "coverage": 0.9,
                            "missing_count": 1,
                            "drop_count": 2,
                            "max_gap_seconds": 0.3,
                            "channels": {
                                "temperature": {"kind": "numeric", "mean": 21.5, "min": 20.0, "max": 23.0, "stddev": 1.0},
                                "state": {"kind": "categorical", "mode": "calm", "frequencies": {"calm": 9}},
                            },
                        }
                    },
                }
            ],
        },
            {"answer_details": [{"question_index": 1, "question_number": 2, "question_type": "mood-meter"}]},
        )

        self.assertEqual(
            rows[0],
            ["Q2 · mood-meter", "future_sensor / future.metrics", "temperature", "21.50", "20.00", "23.00", "1.00", "90 %", "0.30"],
        )
        self.assertEqual(rows[1][2:4], ["state", "calm"])

    def test_tables_split_at_the_notion_row_limit(self) -> None:
        tables = adapter._tables(["a"], [[str(n)] for n in range(150)])
        self.assertEqual(len(tables), 2)
        self.assertEqual(len(tables[0]["table"]["children"]), 100)
        self.assertEqual(tables[0]["table"]["children"][0]["table_row"]["cells"][0][0]["text"]["content"], "a")

    def test_session_page_uses_only_the_canonical_card_summary(self) -> None:
        blocks = adapter._session_page_blocks(
            {"study_id": "Study", "timestamp_start": "2026-01-01T10:00:00Z", "timestamp_end": "2026-01-01T10:01:00Z",
             "answer_details": [{"question_index": 1, "question_number": 2, "question_type": "likert", "answer": 4}]},
            {"brainbit": {"enabled": True}},
            {"card_summary": {
            "schema": "study-runner/card-summary/v1",
            "cards": [
                {
                    "question_index": 1,
                    "streams": {
                        "future.metrics": {
                            "plugin_key": "future_sensor",
                            "count": 10,
                            "valid_count": 9,
                            "coverage": 0.9,
                            "missing_count": 1,
                            "drop_count": 2,
                            "max_gap_seconds": 0.3,
                            "channels": {
                                "temperature": {"kind": "numeric", "mean": 21.5, "min": 20.0, "max": 23.0, "stddev": 1.0},
                                "state": {"kind": "categorical", "mode": "calm", "frequencies": {"calm": 9}},
                            },
                        }
                    },
                }
            ],
        }, "biosignal_summary": {"brainbit": {"active": True, "mean": 98765}}},
        )
        rendered = json.dumps(blocks, ensure_ascii=False)
        self.assertEqual([b["type"] for b in blocks].count("table"), 2)
        self.assertIn("21.50", rendered)
        self.assertNotIn("98765", rendered)

    def test_canonical_notion_payload_requires_valid_card_summary(self) -> None:
        result = adapter.upload_study_result(
            result_payload={
                "session_id": "session-1",
                "server_finalization": {"card_summary_file": "card-summary.json"},
            },
            hardware_config={},
            saved_output={"card_summary_file": "sessions/session-1/card-summary.json"},
            config_data={"study_settings": {"notion_enabled": True}},
        )

        self.assertFalse(result["ok"])
        self.assertIn("requires finalized card-summary.json", result["error"])


class NotionValidateStudySettingTests(unittest.TestCase):
    """`Plugin.validate_study_setting`, the hook validation.py calls for the
    parent page (a field with a format) and the "object" export_mapping
    field - see runtime_core/studies/validation.py."""

    PAGE_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"

    def test_ignores_fields_it_does_not_own(self) -> None:
        plugin.PLUGIN.validate_study_setting("database_id", "not json at all")  # does not raise

    def test_accepts_a_pasted_link_or_an_id_as_the_parent_page(self) -> None:
        for value in (
            self.PAGE_ID,
            "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
            f"https://www.notion.so/team/Study-results-{self.PAGE_ID}?source=copy_link",
            "",
        ):
            plugin.PLUGIN.validate_study_setting("parent_page_id", value)  # does not raise

    def test_rejects_a_parent_page_without_an_id_and_says_how_to_get_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "Copy link"):
            plugin.PLUGIN.validate_study_setting("parent_page_id", "Study results")

    def test_rejects_malformed_json(self) -> None:
        with self.assertRaises(ValueError):
            plugin.PLUGIN.validate_study_setting("export_mapping", "{not json")

    def test_rejects_an_invalid_mapping_shape(self) -> None:
        with self.assertRaises(ValueError):
            plugin.PLUGIN.validate_study_setting("export_mapping", json.dumps({"preset": "not-a-preset", "targets": []}))

    def test_accepts_the_default_mapping(self) -> None:
        plugin.PLUGIN.validate_study_setting("export_mapping", json.dumps({"preset": "as_before", "targets": []}))


class NotionAdminActionDispatchTests(unittest.TestCase):
    """plugin.py#_run_admin_action routes each configurator action."""

    def _context(self):
        return SimpleNamespace(hardware_config={}, data_dir=Path("/tmp"), secret=lambda *_: "study-key")

    def test_unknown_action_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plugin._run_admin_action(self._context(), "not_a_real_action", {})

    def test_list_children_dispatches_with_the_resolved_api_key(self) -> None:
        with patch.object(adapter, "list_children", return_value={"ok": True, "children": []}) as called:
            plugin._run_admin_action(self._context(), "list_children", {"page_id": "page-1"})
        called.assert_called_once_with(api_key="study-key", page_id="page-1", cursor=None)

    def test_create_database_dispatches_every_field(self) -> None:
        with patch.object(adapter, "create_notion_database", return_value={"ok": True}) as called:
            plugin._run_admin_action(self._context(), "create_database", {
                "parent_page_id": "page-1", "title": "X", "row_level": "session", "columns_json": "[]",
            })
        called.assert_called_once_with(
            api_key="study-key", parent_page_id="page-1", title="X", row_level="session", columns_json="[]",
            key_column="",
        )

    def test_test_connection_uses_the_machine_timeout_when_the_panel_sends_none(self) -> None:
        # The study settings panel never asks for a timeout; it is a machine setting.
        context = SimpleNamespace(
            hardware_config={"notion": {"timeout_seconds": 42}}, data_dir=Path("/tmp"), secret=lambda *_: "study-key",
        )
        with patch.object(adapter, "test_connection", return_value={"ok": True}) as called:
            plugin._run_admin_action(context, "test_connection", {"parent_page_id": "page-1"})
        self.assertEqual(called.call_args.kwargs["timeout_seconds"], 42)


class NotionParentPageIsCheckedWhenTheStudyIsSavedTests(unittest.TestCase):
    """validation.py hands a text field with a declared format to its plugin."""

    def _config(self, parent_page_id: str) -> dict:
        return validate_and_normalize_config({
            "study_id": "Notion Parent Page",
            "questions": [{"type": "participant-id", "prompt": "Identify yourself."}, {"type": "finish"}],
            "study_settings": {
                "plugins": {"notion": {"enabled": True, "settings": {"parent_page_id": parent_page_id}}},
            },
        })

    def test_a_pasted_link_is_accepted_and_kept_as_typed(self) -> None:
        link = "https://www.notion.so/team/Results-1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d?source=copy_link"
        config = self._config(link)
        self.assertEqual(config["study_settings"]["plugins"]["notion"]["settings"]["parent_page_id"], link)

    def test_text_without_a_page_id_is_refused_with_a_clear_message(self) -> None:
        from study_runner.runtime_core.studies.validation import ValidationError

        with self.assertRaisesRegex(ValidationError, "parent_page_id: is not a Notion page link or ID") as raised:
            self._config("Study results")
        # The message crosses the plugin process boundary intact, "•••" included.
        self.assertIn("••• > Copy link", str(raised.exception))


class NotionIdFromInputTests(unittest.TestCase):
    """Operators paste the parent page as a whole "Copy link" link."""

    ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"

    def test_reads_the_id_from_ids_and_links(self) -> None:
        from study_runner.plugins.destinations.notion_upload.notion_ids import notion_id_from_input

        cases = {
            self.ID: self.ID,
            "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d": self.ID,
            f"https://www.notion.so/team/Study-results-{self.ID}?source=copy_link": self.ID,
            # Removing every dash would glue a hex-letter title to the ID.
            f"https://www.notion.so/Cafe-{self.ID}": self.ID,
            f"notion.so/team/Page-{self.ID}?pvs=4": self.ID,
            f"https://www.notion.so/team/{'0' * 32}?v={'f' * 32}&p={self.ID}&pm=s": self.ID,
            f"https://www.notion.so/team/Study-results-{self.ID.upper()}/": self.ID,
            "Study results": "",
            "https://example.com/page": "",
            "": "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(notion_id_from_input(value), expected)

    def test_the_adapter_reads_pasted_links_wherever_it_takes_an_id(self) -> None:
        self.assertEqual(adapter._notion_id(f"https://www.notion.so/team/Results-{self.ID}"), self.ID)
        self.assertEqual(adapter._notion_id("1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"), self.ID)


class NotionDescribeOutputCatalogTests(unittest.TestCase):
    def test_lists_session_fields_and_stored_participant_fields(self) -> None:
        config_data = {"questions": [{"type": "participant-id", "fields": {"age_group": {"store": True}}}]}
        result = adapter.describe_output_catalog(config_data_json=json.dumps(config_data))
        sources = {entry["source"] for entry in result["sources"]}
        self.assertTrue(result["ok"])
        self.assertIn("session.participant_id", sources)
        self.assertIn("participant.age_group", sources)

    def test_rejects_malformed_json(self) -> None:
        result = adapter.describe_output_catalog(config_data_json="not json")
        self.assertFalse(result["ok"])


class NotionDescribeSessionSourcesTests(unittest.TestCase):
    def _write_session(self, data_dir: Path) -> None:
        session = data_dir / "study-a" / "p01" / "20260101T100000Z__session-1"
        (session / "answers").mkdir(parents=True)
        (session / "COMPLETE.json").write_text("{}", encoding="utf-8")
        (session / "answers" / "result.json").write_text(json.dumps({
            "session_id": "session-1", "participant_id": "p01", "study_id": "study-a",
            "answer_details": [{"question_index": 0, "question_prompt": "Q", "answer": "a", "question_type": "text"}],
        }), encoding="utf-8")
        (session / "answers" / "card-summary.json").write_text(json.dumps({
            "schema": "study-runner/card-summary/v1",
            "cards": [{"question_index": 0, "streams": {"1": {
                "plugin_key": "brainbit", "source_id": "study_runner.brainbit.eeg",
                "channels": {"alpha": {"mean": 1.0}},
            }}}],
        }), encoding="utf-8")

    def test_lists_real_stream_sources_by_their_stable_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            self._write_session(data_dir)
            result = adapter.describe_session_sources(
                data_dir=data_dir, session_path="study-a/p01/20260101T100000Z__session-1",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sources"][0]["source"], "card.stream.brainbit_eeg.channel.alpha.mean")

    def test_rejects_a_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = adapter.describe_session_sources(data_dir=Path(temp_dir), session_path="study-a/p01/no-such-session")
        self.assertFalse(result["ok"])


class NotionPreviewMappingTests(unittest.TestCase):
    """preview_mapping reads a real finished session from disk - no Notion
    API call - and must stay inside data_dir exactly like the host's own
    session-folder safety check (duplicated here: plugins must not import
    runtime_core, see tests/test_import_boundaries.py)."""

    def _write_session(self, data_dir: Path) -> Path:
        session = data_dir / "study-a" / "p01" / "20260101T100000Z__session-1"
        (session / "answers").mkdir(parents=True)
        (session / "COMPLETE.json").write_text("{}", encoding="utf-8")
        (session / "answers" / "result.json").write_text(json.dumps({
            "session_id": "session-1", "participant_id": "p01", "study_id": "study-a",
            "timestamp_start": "2026-01-01T10:00:00Z", "timestamp_end": "2026-01-01T10:01:00Z",
            "answer_details": [],
        }), encoding="utf-8")
        return session

    def test_computes_values_from_a_real_finished_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            self._write_session(data_dir)
            mapping = {"preset": "simple", "targets": [{
                "id": "t1", "title": "Sessions", "row_level": "session", "database_id": "db-1",
                "columns": {"Participant": {"type": "rich_text", "source": "session.participant_id"}},
            }]}
            result = adapter.preview_mapping(
                data_dir=data_dir,
                mapping_json=json.dumps(mapping),
                session_path="study-a/p01/20260101T100000Z__session-1",
            )
        self.assertTrue(result["ok"], result)
        [target] = result["targets"]
        self.assertEqual(target["rows"][0]["key"], "session-1")
        self.assertEqual(target["rows"][0]["properties"]["Participant"], "p01")

    def test_rejects_a_path_outside_the_three_segment_session_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = adapter.preview_mapping(
                data_dir=Path(temp_dir),
                mapping_json=json.dumps({"preset": "as_before", "targets": []}),
                session_path="../../etc/passwd",
            )
        self.assertFalse(result["ok"])

    def test_rejects_a_session_that_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = adapter.preview_mapping(
                data_dir=Path(temp_dir),
                mapping_json=json.dumps({"preset": "as_before", "targets": []}),
                session_path="study-a/p01/no-such-session",
            )
        self.assertFalse(result["ok"])

    def test_rejects_an_invalid_mapping_before_touching_the_filesystem(self) -> None:
        result = adapter.preview_mapping(
            data_dir=Path("/does/not/matter"),
            mapping_json=json.dumps({"preset": "not-a-preset", "targets": []}),
            session_path="study-a/p01/session-1",
        )
        self.assertFalse(result["ok"])


class NotionPublishContractTests(unittest.TestCase):
    def payload(self):
        return {
            "config_data": {"study_id": "Study A", "study_settings": {"plugins": {
                "notion": {"enabled": True, "settings": {"parent_page_id": "parent-1"}},
            }}},
            "result_payload": {"session_id": "session-1", "participant_id": "p1", "answers": {}},
            "saved_output": {},
        }

    def client(self, existing_children=None):
        created = iter([
            {"id": "created-db", "data_sources": [{"id": "source-1"}]},
            {"id": "sessions-db", "data_sources": [{"id": "sessions-source"}]},
        ])
        sources = {"createddb": "source-1", "sessionsdb": "sessions-source"}
        return SimpleNamespace(
            databases=SimpleNamespace(
                create=Mock(side_effect=lambda **_: next(created)),
                retrieve=Mock(side_effect=lambda database_id: {"data_sources": [{"id": sources.get(database_id, database_id)}]}),
            ),
            data_sources=SimpleNamespace(query=Mock(return_value={"results": []})),
            pages=SimpleNamespace(
                create=Mock(side_effect=[{"id": "participant-page"}, {"id": "session-page"}]),
                retrieve=Mock(return_value={"properties": {}}), update=Mock(),
            ),
            blocks=SimpleNamespace(children=SimpleNamespace(
                list=Mock(return_value={"results": existing_children or [], "has_more": False}),
                append=Mock(return_value={"results": []}),
            )),
        )

    def test_real_publish_adapter_participant_path_reports_created_target(self):
        payload = self.payload()
        original = deepcopy(payload)
        client = self.client()
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["upsert"], "created")
        self.assertEqual(
            result["study_config_updates"],
            {"database_id": "createddb", "data_source_id": "source-1", "sessions_database_id": "sessionsdb"},
        )
        participant_query, session_query = client.data_sources.query.call_args_list
        self.assertEqual(participant_query.kwargs["data_source_id"], "source-1")
        self.assertEqual(session_query.kwargs["data_source_id"], "sessions-source")
        session_row = client.pages.create.call_args_list[1].kwargs
        self.assertEqual(session_row["properties"]["Participant"], {"relation": [{"id": "participant-page"}]})
        self.assertEqual(client.blocks.children.append.call_args.kwargs["block_id"], "session-page")
        self.assertEqual(payload, original)

    def test_existing_databases_under_the_parent_page_are_reused_not_duplicated(self):
        children = [
            {"type": "child_database", "id": "existing-participants", "child_database": {"title": "StudyRunner Participants"}},
            {"type": "child_database", "id": "existing-sessions", "child_database": {"title": "StudyRunner Sessions"}},
        ]
        client = self.client(existing_children=children)
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), self.payload())
        self.assertTrue(result["ok"], result)
        client.databases.create.assert_not_called()
        self.assertEqual(result["study_config_updates"]["database_id"], "existingparticipants")
        self.assertEqual(result["study_config_updates"]["sessions_database_id"], "existingsessions")

    def test_failure_after_target_creation_returns_discoveries_for_retry(self):
        client = self.client()
        client.pages.create.side_effect = OSError("connection interrupted")
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), self.payload())
        self.assertFalse(result["ok"])
        self.assertEqual(result["study_config_updates"], {"database_id": "createddb", "data_source_id": "source-1"})
        self.assertIn("connection interrupted", result["error"])


class NotionForbiddenUpdateTests(unittest.TestCase):
    """Notion answers 403 restricted_resource for an unshared page *and* for
    an integration without a capability. When only the participant summary
    update is refused, the session row - the data - has already arrived."""

    def _forbidden(self) -> Exception:
        error = RuntimeError("Insufficient permissions for this endpoint.")
        error.code = "restricted_resource"
        return error

    def _client(self):
        return NotionPublishContractTests().client()

    def test_a_refused_participant_update_keeps_the_upload_successful_with_a_warning(self) -> None:
        client = self._client()
        client.pages.update = Mock(side_effect=self._forbidden())
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(
                SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"),
                NotionPublishContractTests().payload(),
            )
        self.assertTrue(result["ok"], result)
        self.assertIn("participant summary row could not be updated", result["message"])
        self.assertIn("Insufficient permissions", result["message"])

    def test_a_refused_create_still_fails_permanently_and_keeps_notions_text(self) -> None:
        client = self._client()
        client.pages.create = Mock(side_effect=self._forbidden())
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(
                SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"),
                NotionPublishContractTests().payload(),
            )
        self.assertFalse(result["ok"])
        self.assertTrue(result["permanent"])
        self.assertIn("Insufficient permissions", result["error"])
        self.assertIn("Capabilities", result["error"])


class NotionStaleCacheTests(unittest.TestCase):
    """A cached data source or sessions database must be re-verified against
    the current database/parent page before it is reused - otherwise a
    changed target (new parent page, new database) keeps silently writing
    into the previous one. See CHANGELOG 'Fixed' for the bug this guards."""

    def test_cached_data_source_belonging_to_the_current_database_is_kept(self) -> None:
        client = SimpleNamespace(
            databases=SimpleNamespace(retrieve=Mock(return_value={"data_sources": [{"id": "source1"}]})),
            data_sources=SimpleNamespace(),  # only hasattr() matters to the adapter here
        )
        study_settings = {"notion_data_source_id": "source1"}
        updates: dict[str, str] = {}

        result = adapter._get_data_source_id(client, "db1", study_settings, {}, updates)

        self.assertEqual(result, "source1")
        self.assertEqual(updates, {}, "an already-correct cache is not rewritten")

    def test_cached_data_source_from_a_different_database_is_rediscovered(self) -> None:
        client = SimpleNamespace(
            databases=SimpleNamespace(retrieve=Mock(return_value={"data_sources": [{"id": "sourcenew"}]})),
            data_sources=SimpleNamespace(),
        )
        study_settings = {"notion_data_source_id": "sourceold"}
        updates: dict[str, str] = {}

        result = adapter._get_data_source_id(client, "db1", study_settings, {}, updates)

        self.assertEqual(result, "sourcenew")
        self.assertEqual(study_settings["notion_data_source_id"], "sourcenew")
        self.assertEqual(updates, {"data_source_id": "sourcenew"})

    def test_data_source_cache_survives_an_unreadable_database(self) -> None:
        client = SimpleNamespace(
            databases=SimpleNamespace(retrieve=Mock(side_effect=OSError("network unavailable"))),
            data_sources=SimpleNamespace(),
        )
        study_settings = {"notion_data_source_id": "sourceold"}

        result = adapter._get_data_source_id(client, "db1", study_settings, {}, {})

        self.assertEqual(result, "sourceold", "a transient read error must not fail the whole upload")

    def test_cached_sessions_database_under_the_current_parent_is_kept(self) -> None:
        client = SimpleNamespace(
            databases=SimpleNamespace(
                retrieve=Mock(return_value={"parent": {"page_id": "parenta"}}),
                create=Mock(),
            ),
        )
        study_settings = {"notion_sessions_database_id": "sessionsdb", "notion_parent_page_id": "parenta"}

        result = adapter._ensure_sessions_database(client, "participantsdb", study_settings, {})

        self.assertEqual(result, "sessionsdb")
        client.databases.create.assert_not_called()

    def test_cached_sessions_database_under_a_different_parent_is_rediscovered(self) -> None:
        client = SimpleNamespace(
            databases=SimpleNamespace(
                retrieve=Mock(return_value={"parent": {"page_id": "parentold"}}),
                create=Mock(return_value={"id": "newsessionsdb"}),
            ),
            blocks=SimpleNamespace(children=SimpleNamespace(
                list=Mock(return_value={"results": [], "has_more": False}),
            )),
        )
        study_settings = {"notion_sessions_database_id": "sessionsdb", "notion_parent_page_id": "parentnew"}
        updates: dict[str, str] = {}

        result = adapter._ensure_sessions_database(client, "participantsdb", study_settings, updates)

        self.assertEqual(result, "newsessionsdb")
        self.assertEqual(updates["sessions_database_id"], "newsessionsdb")
        client.databases.create.assert_called_once()


class NotionSessionIdempotencyTests(unittest.TestCase):
    """A retried upload never adds a second page for the same session."""

    MARKER = "study-runner-session-commit:S1"

    def _client(self, rows, page_text):
        client = SimpleNamespace(
            databases=Mock(),
            pages=Mock(),
            blocks=SimpleNamespace(children=Mock()),
        )
        client.databases.query.return_value = {"results": rows}
        client.pages.create.return_value = {"id": "new-page"}
        client.blocks.children.list.side_effect = lambda block_id, **_: {
            "results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": page_text.get(block_id, "")}]}}],
            "has_more": False,
        }
        return client

    def _upsert(self, client):
        with (
            patch.object(adapter, "_session_properties", return_value={}),
            patch.object(adapter, "_session_page_blocks", return_value=[]),
        ):
            return adapter._upsert_session_page(client, "db", "participant", {"session_id": "S1"}, {}, {})

    def test_a_complete_page_is_left_unchanged(self) -> None:
        client = self._client([{"id": "old"}], {"old": self.MARKER})
        self.assertEqual(self._upsert(client), "unchanged")
        client.pages.create.assert_not_called()
        client.pages.update.assert_not_called()

    def test_a_page_cut_short_is_replaced_once(self) -> None:
        client = self._client([{"id": "old"}], {"old": "half written"})
        self.assertEqual(self._upsert(client), "updated")
        client.pages.update.assert_called_once_with(page_id="old", archived=True)
        client.pages.create.assert_called_once()
        appended = client.blocks.children.append.call_args.kwargs["children"]
        self.assertIn(self.MARKER, json.dumps(appended))


class NotionConfiguratorActionsTests(unittest.TestCase):
    """The tree/column-mapping modal's own admin actions - see
    docs/notion-plugin-configurator-plan.md, Phase 2."""

    def test_list_children_separates_databases_from_pages_and_reports_a_cursor(self) -> None:
        client = SimpleNamespace(blocks=SimpleNamespace(children=SimpleNamespace(list=Mock(return_value={
            "results": [
                {"type": "child_database", "id": "db-1", "child_database": {"title": "Sessions"}},
                {"type": "child_page", "id": "page-1", "child_page": {"title": "Notes"}},
                {"type": "paragraph"},
            ],
            "has_more": True,
            "next_cursor": "cursor-2",
        }))))
        with patch.object(adapter, "get_client", return_value=client):
            result = adapter.list_children(api_key="key", page_id="parent-page")
        self.assertTrue(result["ok"])
        self.assertEqual(result["children"], [
            {"id": "db1", "type": "database", "title": "Sessions"},
            {"id": "page1", "type": "page", "title": "Notes"},
        ])
        self.assertEqual(result["next_cursor"], "cursor-2")

    def test_list_children_reports_an_api_error_instead_of_raising(self) -> None:
        client = SimpleNamespace(blocks=SimpleNamespace(children=SimpleNamespace(
            list=Mock(side_effect=RuntimeError("network unavailable")),
        )))
        with patch.object(adapter, "get_client", return_value=client):
            result = adapter.list_children(api_key="key", page_id="parent-page")
        self.assertFalse(result["ok"])
        self.assertIn("network unavailable", result["error"])

    def test_list_children_requires_a_page_id(self) -> None:
        with patch.object(adapter, "get_client", return_value=SimpleNamespace()):
            result = adapter.list_children(api_key="key", page_id="")
        self.assertFalse(result["ok"])

    def test_describe_database_lists_columns_with_their_notion_type(self) -> None:
        client = SimpleNamespace(databases=SimpleNamespace(retrieve=Mock(return_value={
            "title": [{"plain_text": "StudyRunner Sessions"}],
            "properties": {
                "Session": {"title": {}},
                "Duration (min)": {"number": {"format": "number"}},
            },
        })))
        with patch.object(adapter, "get_client", return_value=client):
            result = adapter.describe_database(api_key="key", database_id="db-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "StudyRunner Sessions")
        self.assertEqual(
            sorted(result["columns"], key=lambda c: c["name"]),
            [{"name": "Duration (min)", "type": "number"}, {"name": "Session", "type": "title"}],
        )

    def test_create_notion_database_adds_the_fixed_key_column_as_the_title(self) -> None:
        client = SimpleNamespace(databases=SimpleNamespace(create=Mock(return_value={"id": "new-db"})))
        with patch.object(adapter, "get_client", return_value=client):
            result = adapter.create_notion_database(
                api_key="key", parent_page_id="parent-page", title="My export",
                row_level="session", columns_json=json.dumps([{"name": "Duration", "type": "number"}]),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["database_id"], "newdb")
        self.assertEqual(result["key_column"], "Session ID")
        schema = client.databases.create.call_args.kwargs["properties"]
        self.assertEqual(schema["Session ID"], {"title": {}})
        self.assertEqual(schema["Duration"], {"number": {"format": "number"}})

    def test_create_notion_database_names_the_key_column_in_the_chosen_language(self) -> None:
        client = SimpleNamespace(databases=SimpleNamespace(create=Mock(return_value={"id": "new-db"})))
        with patch.object(adapter, "get_client", return_value=client):
            result = adapter.create_notion_database(
                api_key="key", parent_page_id="parent-page", title="Export",
                row_level="session", columns_json="[]", key_column="Sitzungs-ID",
            )
        self.assertEqual(result["key_column"], "Sitzungs-ID")
        self.assertEqual(client.databases.create.call_args.kwargs["properties"], {"Sitzungs-ID": {"title": {}}})

    def test_create_notion_database_rejects_an_invalid_row_level(self) -> None:
        with patch.object(adapter, "get_client", return_value=SimpleNamespace()):
            result = adapter.create_notion_database(
                api_key="key", parent_page_id="parent-page", title="X",
                row_level="not-a-level", columns_json="[]",
            )
        self.assertFalse(result["ok"])

    def test_create_notion_database_rejects_malformed_columns_json(self) -> None:
        with patch.object(adapter, "get_client", return_value=SimpleNamespace()):
            result = adapter.create_notion_database(
                api_key="key", parent_page_id="parent-page", title="X",
                row_level="session", columns_json="not json",
            )
        self.assertFalse(result["ok"])


class NotionMappedUploadTests(unittest.TestCase):
    """The flexible-targets upload path, alongside the untouched legacy one."""

    def payload(self, export_mapping):
        return {
            "config_data": {"study_id": "Study A", "study_settings": {"plugins": {
                "notion": {"enabled": True, "settings": {"export_mapping": export_mapping}},
            }}},
            "result_payload": {
                "session_id": "session-1", "participant_id": "p1", "study_id": "Study A",
                "answer_details": [{"question_index": 0, "question_prompt": "Q", "answer": "a", "question_type": "text"}],
            },
            "saved_output": {"card_summary": {"schema": "study-runner/card-summary/v1", "cards": []}},
        }

    def test_a_custom_mapping_upserts_each_target_by_its_key_column(self) -> None:
        export_mapping = {"preset": "simple", "targets": [{
            "id": "t1", "title": "Sessions", "row_level": "session", "database_id": "db-1",
            "columns": {"Participant": {"type": "rich_text", "source": "session.participant_id"}},
        }]}
        client = SimpleNamespace(
            databases=SimpleNamespace(query=Mock(return_value={"results": []})),
            pages=SimpleNamespace(create=Mock(return_value={"id": "new-page"}), update=Mock()),
        )
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), self.payload(export_mapping))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["upsert"], "custom_mapping")
        created_properties = client.pages.create.call_args.kwargs["properties"]
        self.assertEqual(created_properties["Session ID"]["title"][0]["text"]["content"], "session-1")
        self.assertEqual(created_properties["Participant"]["rich_text"][0]["text"]["content"], "p1")

    def test_a_custom_key_column_is_used_for_writing_and_matching(self) -> None:
        export_mapping = {"preset": "simple", "targets": [{
            "id": "t1", "title": "Sitzungen", "row_level": "session", "database_id": "db-1",
            "key_column": "Sitzungs-ID",
            "columns": {"Teilnehmer-ID": {"type": "rich_text", "source": "session.participant_id"}},
        }]}
        client = SimpleNamespace(
            databases=SimpleNamespace(query=Mock(return_value={"results": []})),
            pages=SimpleNamespace(create=Mock(return_value={"id": "new-page"}), update=Mock()),
        )
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), self.payload(export_mapping))
        self.assertTrue(result["ok"], result)
        self.assertEqual(client.databases.query.call_args.kwargs["filter"]["property"], "Sitzungs-ID")
        self.assertIn("Sitzungs-ID", client.pages.create.call_args.kwargs["properties"])

    def test_an_existing_row_for_the_same_key_is_updated_not_duplicated(self) -> None:
        export_mapping = {"preset": "simple", "targets": [{
            "id": "t1", "title": "Sessions", "row_level": "session", "database_id": "db-1",
            "columns": {"Participant": {"type": "rich_text", "source": "session.participant_id"}},
        }]}
        client = SimpleNamespace(
            databases=SimpleNamespace(query=Mock(return_value={"results": [{"id": "existing-page"}]})),
            pages=SimpleNamespace(create=Mock(), update=Mock()),
        )
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), self.payload(export_mapping))
        self.assertTrue(result["ok"], result)
        client.pages.create.assert_not_called()
        client.pages.update.assert_called_once()
        self.assertEqual(client.pages.update.call_args.kwargs["page_id"], "existing-page")

    def test_a_target_without_a_database_fails_clearly_before_any_api_call(self) -> None:
        export_mapping = {"preset": "simple", "targets": [{
            "id": "t1", "title": "Sessions", "row_level": "session", "database_id": "",
            "columns": {"Participant": {"type": "rich_text", "source": "session.participant_id"}},
        }]}
        client = SimpleNamespace(databases=SimpleNamespace(query=Mock()), pages=SimpleNamespace(create=Mock()))
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), self.payload(export_mapping))
        self.assertFalse(result["ok"])
        self.assertTrue(result["permanent"])
        self.assertIn("Sessions", result["error"])
        client.databases.query.assert_not_called()

    def test_the_as_before_preset_still_uses_the_legacy_two_database_flow(self) -> None:
        """The default preset must not change a single existing study's output."""
        payload = self.payload({"preset": "as_before", "targets": []})
        payload["config_data"]["study_settings"]["plugins"]["notion"]["settings"]["parent_page_id"] = "parent-1"
        client = SimpleNamespace(
            databases=SimpleNamespace(
                create=Mock(return_value={"id": "createddb"}),
                retrieve=Mock(return_value={"data_sources": []}),
            ),
            data_sources=SimpleNamespace(query=Mock(return_value={"results": []})),
            pages=SimpleNamespace(create=Mock(return_value={"id": "page"}), update=Mock(), retrieve=Mock(return_value={"properties": {}})),
            blocks=SimpleNamespace(children=SimpleNamespace(
                list=Mock(return_value={"results": [], "has_more": False}),
                append=Mock(return_value={"results": []}),
            )),
        )
        with patch.object(adapter, "get_client", return_value=client):
            result = plugin._publish(SimpleNamespace(hardware_config={}, secret=lambda *_: "study-key"), payload)
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result.get("upsert"), "custom_mapping")
        client.databases.create.assert_called()  # the legacy participants/sessions flow ran


if __name__ == "__main__":
    unittest.main()
