"""Guardrail tests for the web UI conventions.

These keep three owner rules enforced without a JS test runner:
- only slide toggles (.switch), no plain square checkboxes,
- no untranslated research jargon on the participant page,
- the UI must work offline (no CDN links) and never block with alert().
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

WEB = PROJECT_ROOT / "study_runner" / "apps" / "ui"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class LocaleTests(unittest.TestCase):
    def test_locale_key_sets_are_identical(self) -> None:
        en = json.loads(_read(WEB / "locales" / "en.json"))
        de = json.loads(_read(WEB / "locales" / "de.json"))

        self.assertEqual(
            sorted(en.keys()),
            sorted(de.keys()),
            "en.json and de.json must contain exactly the same translation keys",
        )


    def test_every_t_call_resolves_in_both_locales(self) -> None:
        """A missing key renders the English fallback, silently, in both languages.

        That is how "Machine settings" survived on the German settings page:
        t() succeeded, it just returned the second argument.
        """
        en = json.loads(_read(WEB / "locales" / "en.json"))
        de = json.loads(_read(WEB / "locales" / "de.json"))

        pattern = re.compile(r"""\bt\(\s*['"]([A-Za-z0-9_.]+)['"]""")
        sources = list((WEB / "scripts").rglob("*.js"))
        sources += list((PROJECT_ROOT / "study_runner" / "plugins").rglob("*.js"))

        used: set[str] = set()
        for path in sources:
            if path.name == "i18n.js":
                continue  # its docstring example is not a real key
            used.update(pattern.findall(_read(path)))

        self.assertGreater(len(used), 500, "t() discovery appears to be broken")
        self.assertEqual(sorted(used - set(en)), [], "t() keys missing from en.json")
        self.assertEqual(sorted(used - set(de)), [], "t() keys missing from de.json")

    def test_html_and_card_type_keys_resolve_in_both_locales(self) -> None:
        en = json.loads(_read(WEB / "locales" / "en.json"))
        de = json.loads(_read(WEB / "locales" / "de.json"))
        html = "\n".join(_read(path) for path in (WEB / "pages").glob("*.html"))
        html_keys = set(re.findall(r'data-i18n(?:-placeholder|-title|-aria-label)?="([A-Za-z0-9_.]+)"', html))
        self.assertGreater(len(html_keys), 100, "HTML translation discovery appears to be broken")
        card_index = _read(WEB / "scripts" / "cards" / "index.js")
        card_keys = set(re.findall(r"'(cardType\.[A-Za-z]+)'", card_index))
        dynamic_keys = {
            *(f"cards.moodMeter.quadrant.{colour}" for colour in ("red", "yellow", "green", "blue")),
            *(f"cards.moodMeter.examples.{colour}" for colour in ("red", "yellow", "green", "blue")),
            *(f"stimulus.trigger.{kind}" for kind in ("timer", "image", "video", "audio", "html", "js")),
        }
        from study_runner.plugin_framework.registry import get_plugin_manifests
        types = {
            kind
            for manifest in get_plugin_manifests().values()
            for kind in ((manifest.get("capability_config") or {}).get("card_contract") or {}).get("question_types", [])
        }
        self.assertEqual(len(card_keys), len(types), "Every shipped card type needs a translated name")
        for language, messages in (("en", en), ("de", de)):
            self.assertEqual(sorted((html_keys | card_keys | dynamic_keys) - set(messages)), [], f"missing {language} UI keys")

    def test_german_locale_spells_umlauts(self) -> None:
        """The German UI writes oeffnen/fuer/Zurueck nowhere - it has umlauts."""
        de = json.loads(_read(WEB / "locales" / "de.json"))

        banned = (
            "fuer", "oeffn", "schliessen", "Zurueck", "zurueck", "pruef", "Pruef",
            "koennen", "waehrend", "verfuegbar", "Geraet", "Qualitaet", "naechst",
            "Naechst", "loesch", "Loesch", "laeuft", "Laeuft", "laedt", "Laedt",
        )
        offenders = [
            f"{key}: {value}"
            for key, value in de.items()
            if isinstance(value, str) and (any(word.lower() in value.lower() for word in banned) or "\ufffd" in value)
        ]
        self.assertEqual(offenders[:5], [], "write proper umlauts in de.json")


class ModuleSyntaxTests(unittest.TestCase):
    """The web scripts are ES modules and must be syntax-checked as such.

    `node --check foo.js` parses the file as CommonJS, where a top-level
    `export` is simply not looked at - it accepted `async export function`
    without complaint and the page only broke in the browser. Copying to .mjs
    is what makes node use the module grammar.
    """

    def test_every_script_parses_as_an_es_module(self) -> None:
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")

        offenders = []
        scripts = sorted((WEB / "scripts").rglob("*.js"))
        scripts += sorted((PROJECT_ROOT / "study_runner" / "plugins").rglob("ui/*.js"))
        self.assertGreater(len(scripts), 20, "script discovery is broken")

        with tempfile.TemporaryDirectory() as work_dir:
            for path in scripts:
                copy = Path(work_dir) / f"{path.stem}.mjs"
                copy.write_text(_read(path), encoding="utf-8")
                result = subprocess.run(
                    [node, "--check", str(copy)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    first = (result.stderr or "").strip().splitlines()
                    offenders.append(f"{path.name}: {first[2] if len(first) > 2 else result.stderr[:80]}")

        self.assertEqual(offenders, [])

    def test_no_module_level_binding_is_passed_before_it_is_declared(self) -> None:
        # A factory argument such as `{ updateNavigation }` is read while the
        # module evaluates; if that const is declared further down, the module
        # throws a ReferenceError and the whole page stays blank.
        offenders = []
        scripts = sorted((WEB / "scripts").rglob("*.js"))
        for path in scripts:
            source = _blank_strings_and_comments(_read(path))
            declared = _module_level_const_lines(source)
            for call_line, arguments in _module_level_call_arguments(source):
                for name in _bare_property_values(arguments):
                    line = declared.get(name)
                    if line is not None and line > call_line:
                        offenders.append(f"{path.name}:{call_line} passes {name!r} declared on line {line}")
        self.assertEqual(offenders, [])


def _blank_strings_and_comments(source: str) -> str:
    out = list(source)
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        pair = source[index:index + 2]
        if pair == "//":
            end = source.find("\n", index)
            end = length if end < 0 else end
        elif pair == "/*":
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
        elif char in "'\"`":
            end = index + 1
            while end < length and source[end] != char:
                end += 2 if source[end] == "\\" else 1
            end += 1
        else:
            index += 1
            continue
        for position in range(index, min(end, length)):
            if out[position] != "\n":
                out[position] = " "
        index = end
    return "".join(out)


def _module_level_const_lines(source: str) -> dict[str, int]:
    declared: dict[str, int] = {}
    for match in re.finditer(r"^(?:export\s+)?(?:const|let)\s+(\{[^=]*?\}|\w+)\s*=", source, re.M):
        line = source.count("\n", 0, match.start()) + 1
        target = match.group(1)
        if target.startswith("{"):
            for part in target.strip("{}").split(","):
                name = part.split(":")[-1].split("=")[0].strip()
                if name:
                    declared.setdefault(name, line)
        else:
            declared.setdefault(target, line)
    return declared


def _module_level_call_arguments(source: str) -> list[tuple[int, str]]:
    calls = []
    for match in re.finditer(r"^(?:export\s+)?(?:const|let)\s+(?:\{[^=]*?\}|\w+)\s*=\s*[\w.]+\(", source, re.M):
        depth, end = 1, match.end()
        while end < len(source) and depth:
            depth += {"(": 1, ")": -1}.get(source[end], 0)
            end += 1
        calls.append((source.count("\n", 0, match.start()) + 1, source[match.end():end - 1]))
    return calls


def _bare_property_values(arguments: str) -> set[str]:
    shorthand = re.findall(r"(?<=[{,])\s*([A-Za-z_$][\w$]*)\s*(?=[,}])", arguments)
    valued = re.findall(r":\s*([A-Za-z_$][\w$]*)\s*(?=[,}])", arguments)
    return set(shorthand) | set(valued)


class ToggleTests(unittest.TestCase):
    def test_no_legacy_checkbox_row_classes_remain(self) -> None:
        offenders = []
        for path in list(WEB.rglob("*.html")) + list((WEB / "scripts").rglob("*.js")):
            text = _read(path)
            if "consent-row" in text or "checkbox-row" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "convert remaining rows to the .switch pattern")

    def test_every_visible_checkbox_is_a_switch_or_chip(self) -> None:
        # A checkbox is acceptable when it is the hidden input of a .switch
        # or a chips option (hidden by CSS, rendered as pills).
        pattern = re.compile(r'[^\n]*type="checkbox"[^\n]*')
        offenders = []
        for path in list((WEB / "pages").glob("*.html")) + list((WEB / "scripts").rglob("*.js")):
            for match in pattern.findall(_read(path)):
                context = match.strip()
                if "switch" in context or "chips" in context or "querySelector" in context or ".matches(" in context:
                    continue
                if "stimulus-toggle-input" in context or "pid-enabled" in context:
                    continue  # inputs inside existing .switch wrappers (multi-line markup)
                if 'name="q${i}"' in context:
                    continue  # chips option inputs of the choice card
                offenders.append(f"{path.name}: {context[:100]}")
        self.assertEqual(offenders, [])


class ParticipantLanguageTests(unittest.TestCase):
    JARGON = [
        "Visual analog scale</div>",
        "Likert scale</div>",
        "Semantic differential</div>",
        "Word Cloud</div>",
        "Free text</div>",
        "> Ranking</div>",
        "Multi-Slider</div>",
        "Mood Meter</div>",
    ]

    def test_participant_card_tags_are_localized(self) -> None:
        offenders = []
        for path in (WEB / "scripts" / "cards").glob("*.js"):
            text = _read(path)
            for phrase in self.JARGON:
                if phrase in text:
                    offenders.append(f"{path.name}: {phrase}")
        self.assertEqual(offenders, [])

    def test_study_page_never_uses_blocking_alerts(self) -> None:
        text = _read(WEB / "scripts" / "participant" / "study-controller.js")
        self.assertNotIn("alert(", text, "use showStudyNotice() instead of alert()")

    def test_participant_lifecycle_is_manifest_extension_driven(self) -> None:
        controller = _read(WEB / "scripts" / "participant" / "study-controller.js")
        stimulus = _read(WEB / "scripts" / "participant" / "participant-stimulus-execution.js")
        submission = _read(WEB / "scripts" / "participant" / "participant-result-submission.js")
        participant_sources = controller + stimulus + submission
        camera_extension = _read(
            PROJECT_ROOT / "study_runner" / "plugins" / "sensors" / "camera_emotion" / "ui" / "participant.js"
        )
        camera_capture = _read(
            PROJECT_ROOT / "study_runner" / "plugins" / "sensors" / "camera_emotion" / "ui" / "camera-capture.js"
        )

        self.assertNotIn("camera_emotion", controller)
        self.assertNotIn("startCameraCaptureSession", controller)
        self.assertIn("loadPluginUiExtensions('participant')", controller)
        self.assertNotIn("await queueParticipantExtensionSync", controller)
        self.assertIn("participantExtensions.startStimulus", stimulus)
        self.assertIn("participantExtensions.beforeSubmit", submission)
        self.assertIn("plugin_status: participantExtensions.heartbeatStatus()", submission)
        self.assertIn("runParticipantAction", participant_sources)
        self.assertIn("ingestParticipant", participant_sources)
        self.assertIn("createParticipantExtension", camera_extension)
        self.assertIn("startCameraCaptureSession", camera_extension)
        self.assertIn("context.runParticipantAction", camera_extension)
        self.assertIn("context.ingestParticipant", camera_extension)
        self.assertNotIn("/api/study/camera-monitor/start", camera_extension)
        self.assertNotIn("/api/camera/frame", camera_capture)

    def test_generic_core_has_no_active_camera_key_branch(self) -> None:
        core_paths = (
            PROJECT_ROOT / "study_runner" / "apps" / "server" / "routes" / "helpers.py",
            PROJECT_ROOT / "study_runner" / "apps" / "server" / "routes" / "admin.py",
            PROJECT_ROOT / "study_runner" / "apps" / "server" / "routes" / "study.py",
            PROJECT_ROOT / "study_runner" / "plugin_framework" / "registry.py",
        )
        for path in core_paths:
            with self.subTest(path=path.name):
                text = _read(path)
                self.assertNotIn("camera_emotion", text)
                self.assertNotIn("CAMERA_PREVIEW", text)

        # The fixed-key camera/emotion-worker shims are gone: sensors.py names no plugin either.
        self.assertNotIn("camera_emotion", _read(
            PROJECT_ROOT / "study_runner" / "apps" / "server" / "routes" / "sensors.py"
        ))


class EditorFieldOrderTests(unittest.TestCase):
    """The card editor reads in the order an author writes a question."""

    ORDER = ("renderPromptField", "renderInstructionField", "renderEditor", "renderNoteField", "renderEditorToggles")

    def test_open_overlay_composes_the_agreed_order(self) -> None:
        admin = _read(WEB / "scripts" / "admin" / "admin-study-editor.js")
        start = admin.index("editorEl.innerHTML = [")
        block = admin[start : admin.index("].join('');", start)]

        positions = [block.index(name) for name in self.ORDER]
        self.assertEqual(positions, sorted(positions), f"editor field order changed: {block}")

    def test_no_card_module_renders_its_own_prompt_field(self) -> None:
        """The prompt lives in card-info.js only.

        Nine card modules used to carry a byte-identical copy, which is how the
        shared block ended up appended *after* each card's own fields.
        """
        # Every match here is literally named card.js (one per extension
        # folder) -- card-info.js is shared editor chrome and lives under
        # apps/ui/scripts/cards/ instead, so it was never reachable by this
        # glob and needs no exclusion.
        offenders = [
            str(path.relative_to(PROJECT_ROOT))
            for path in sorted((PROJECT_ROOT / "study_runner" / "plugins" / "cards").glob("*/card.js"))
            if 'class="qe-prompt"' in _read(path)
        ]
        self.assertEqual(offenders, [])

    def test_editor_toggles_carry_no_inline_explanation(self) -> None:
        """No explanatory paragraph and no filled row - the title does that job."""
        info = _read(WEB / "scripts" / "cards" / "card-info.js")

        self.assertIn("editor-toggle", info)
        self.assertNotIn("switch-row", info)
        self.assertNotIn("<small", info)

        for path in sorted((PROJECT_ROOT / "study_runner" / "plugins" / "cards").glob("*/card.js")):
            with self.subTest(card=path.name):
                self.assertNotIn('class="switch-row"', _read(path))

    def test_editor_toggle_row_does_not_restyle_the_participant_switch(self) -> None:
        css = _read(WEB / "styles" / "main.css")

        self.assertIn(".editor-toggle {", css)
        # .switch-row still carries its filled background for the participant page.
        block = css[css.index(".switch-row {") : css.index("}", css.index(".switch-row {"))]
        self.assertIn("var(--ink-08)", block)


class PreviewModeTests(unittest.TestCase):
    """Preview looks at a study. It must never act as one."""

    def test_preview_never_sends_a_heartbeat(self) -> None:
        """The heartbeat is what claims the single tablet slot.

        If a preview sent one, opening your own study to look at it would
        block the Play button that starts it.
        """
        controller = _read(WEB / "scripts" / "participant" / "study-controller.js")
        start = controller.index("startStudyClientHeartbeat(getStudyClientHeartbeatPayload")
        guard = controller[max(0, start - 400) : start]

        self.assertIn("IS_PREVIEW", guard, "the heartbeat call must sit behind the preview guard")

    def test_preview_suppresses_every_write_in_one_place(self) -> None:
        """One barrier, not a flag checked at fifteen call sites."""
        controller = _read(WEB / "scripts" / "participant" / "study-controller.js")

        # The real senders are imported under different names and wrapped.
        self.assertIn("postJson as postJsonToServer", controller)
        self.assertIn("sendReliableStudyEvent as sendReliableStudyEventToServer", controller)
        self.assertIn("function sendStudyBeacon(", controller)

        # Nothing may reach the network around the barrier.
        for raw in ("postJsonToServer(", "sendReliableStudyEventToServer("):
            self.assertEqual(controller.count(raw), 1, f"{raw} must only be called by its wrapper")
        self.assertNotIn("navigator.sendBeacon('/api/", controller)

    def test_preview_link_and_play_button_exist(self) -> None:
        admin = _read(WEB / "pages" / "admin.html")

        self.assertIn('href="/?preview=1"', admin)
        self.assertIn('id="btn-workspace-start"', admin)
        self.assertNotIn('data-i18n-title="workspace.openStudy"', admin)

    def test_the_admin_page_has_no_browser_confirm_left(self) -> None:
        """confirm() cannot be translated, is unstyled, and freezes the page."""
        admin = _read(WEB / "scripts" / "admin" / "admin-controller.js")
        stripped = admin.replace("confirmWithModal(", "").replace("confirmAndStartFromEditor(", "")

        self.assertNotIn("confirm(", stripped)
        self.assertNotIn("alert(", stripped)
        self.assertIn("confirmWithModal", admin)

    def test_both_start_buttons_drive_the_same_flow(self) -> None:
        """One start path, two entry points - the spinner follows the button."""
        admin = _read(WEB / "scripts" / "admin" / "admin-run-control.js")

        self.assertIn("buttonId = 'btn-hub-start-study'", admin)
        self.assertIn("buttonId: 'btn-workspace-start'", admin)
        self.assertIn("goToDashboard", admin)
        self.assertNotIn("const button = $('btn-hub-start-study')", admin)


class OfflineTests(unittest.TestCase):
    def test_pages_do_not_load_from_cdns(self) -> None:
        offenders = []
        for path in (WEB / "pages").glob("*.html"):
            text = _read(path)
            if "cdn." in text or "https://unpkg" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "vendored assets only - the lab network has no internet")


class MotionTests(unittest.TestCase):
    """The view sweep must be skippable by anyone who asks the OS to reduce motion."""

    def test_stylesheet_honours_reduced_motion(self) -> None:
        css = _read(WEB / "styles" / "admin.css")

        self.assertIn(
            "@media (prefers-reduced-motion: reduce)",
            css,
            "admin.css must respect the OS reduce-motion setting",
        )

    def test_reduced_motion_block_disables_the_sweep(self) -> None:
        css = _read(WEB / "styles" / "admin.css")
        start = css.index("@media (prefers-reduced-motion: reduce)")
        block = css[start : css.index("\n}", css.index("{", start))]

        self.assertIn(".view-sweep", block, "the sweep overlay must be switched off, not just faded")


class SettingsShellTests(unittest.TestCase):
    """The two settings surfaces share one nav language and one shell."""

    def test_machine_settings_view_exists_with_its_mount_points(self) -> None:
        admin = _read(WEB / "pages" / "admin.html")

        for anchor in ('id="view-machine-settings"', 'id="machine-settings-nav"', 'id="machine-settings-panels"'):
            self.assertIn(anchor, admin)

    def test_every_element_id_a_controller_binds_to_exists(self) -> None:
        """Moving markup between views must not silently break a controller.

        The controllers bind with `byId(...)?.` and optional chaining, so a
        typo or a forgotten block is a no-op at runtime, not an error. This
        turns that silence into a failing test.
        """
        html_ids = set(re.findall(r'\bid="([^"]+)"', _read(WEB / "pages" / "admin.html")))
        # Rendered into the page by JS rather than present in the markup.
        generated = {"notion-study-fields"}

        missing: list[str] = []
        for path in sorted((WEB / "scripts").rglob("*.js")):
            refs = set(re.findall(r"(?:byId|\$)\('([^']+)'\)", _read(path)))
            for ref in sorted(refs - html_ids - generated):
                missing.append(f"{path.name}: {ref}")

        self.assertEqual(missing, [], "controllers reference element ids that admin.html does not define")

    def test_every_named_import_resolves_to_a_real_export(self) -> None:
        """A missing import is a runtime ReferenceError, not a load failure.

        The page keeps working until the exact moment the missing name is
        called, so this class of mistake hides until someone clicks the right
        button. Caught exactly that during the study-panel move.
        """
        scripts = WEB / "scripts"
        export_re = re.compile(r"export\s+(?:async\s+)?function\s+(\w+)|export\s+const\s+(\w+)")
        import_re = re.compile(r"import\s*\{([^}]+)\}\s*from\s*'([^']+)'")

        problems: list[str] = []
        for path in sorted(scripts.rglob("*.js")):
            for names, target in import_re.findall(_read(path)):
                if not target.startswith("."):
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    problems.append(f"{path.name}: imports from missing {target}")
                    continue
                exported = {name for pair in export_re.findall(_read(resolved)) for name in pair if name}
                for raw in names.split(","):
                    local = raw.strip().split(" as ")[0].strip()
                    if local and local not in exported:
                        problems.append(f"{path.name}: '{local}' is not exported by {target}")

        self.assertEqual(problems, [])

    def test_every_manifest_label_key_is_translated(self) -> None:
        """A missing label leaves the operator reading a raw config path.

        The settings form falls back to the dotted path when a label_key is
        absent from the locales, so the field renders as SCAN_SECONDS S rather
        than "Scan duration" - visible, but nothing fails.
        """
        import json as _json

        en = _json.loads(_read(WEB / "locales" / "en.json"))
        de = _json.loads(_read(WEB / "locales" / "de.json"))

        # Every translation key a manifest names: field labels, examples and
        # help, the credential's, and the plugin's help introduction. rglob,
        # because plugins live one level deeper (sensors/, destinations/, ...).
        text_keys = ("label_key", "placeholder_key", "help_key", "description_key")
        referenced: set[str] = set()
        for manifest_path in (PROJECT_ROOT / "study_runner" / "plugins").rglob("manifest.json"):
            manifest = _json.loads(_read(manifest_path))
            fields = []
            for scope in (manifest.get("settings") or {}).values():
                if isinstance(scope, dict):
                    fields.extend(field for field in scope.values() if isinstance(field, dict))
            fields.append((manifest.get("capabilities") or {}).get("credentials") or {})
            fields.append(manifest.get("ui") or {})
            # The live view's graph titles, help and legend (sensor data contract).
            live_view = (manifest.get("capabilities") or {}).get("live_view") or {}
            fields.extend(series for series in live_view.get("series") or [] if isinstance(series, dict))
            prefix = live_view.get("channel_key_prefix")
            if prefix:
                referenced.update(
                    f"{prefix}.{channel}" for series in live_view.get("series") or [] for channel in series.get("channels") or []
                )
            for field in fields:
                for key in text_keys:
                    if field.get(key):
                        referenced.add(field[key])

        self.assertEqual(sorted(referenced - set(en)), [], "label keys missing from en.json")
        self.assertEqual(sorted(referenced - set(de)), [], "label keys missing from de.json")

    def test_every_shipped_manifest_has_operator_description_and_setting_help(self) -> None:
        plugin_root = PROJECT_ROOT / "study_runner" / "plugins"
        problems = []
        for manifest_path in plugin_root.rglob("manifest.json"):
            manifest = json.loads(_read(manifest_path))
            plugin_key = manifest.get("plugin_key") or manifest_path.parent.name
            ui = manifest.get("ui") or {}
            if not str(ui.get("description") or "").strip():
                problems.append(f"{plugin_key}: ui.description is missing")
            if manifest.get("category") != "card" and not ui.get("help_key"):
                problems.append(f"{plugin_key}: ui.help_key is missing")
            for scope_name, scope in (manifest.get("settings") or {}).items():
                if not isinstance(scope, dict):
                    continue
                for field_name, field in scope.items():
                    if isinstance(field, dict) and not field.get("help_key"):
                        problems.append(f"{plugin_key}: {scope_name}.{field_name} has no help_key")
        self.assertEqual(problems, [])

    def test_per_study_settings_are_not_reachable_from_the_machine_hub(self) -> None:
        """Study settings belong to the study, machine settings to the computer.

        A study copied to another computer must not drag that computer's setup
        with it, so the gear hub must not offer a per-study entry.
        """
        controller = _read(WEB / "scripts" / "admin" / "admin-controller.js")

        self.assertNotIn("'study-settings'", controller)
        self.assertIn('id="view-study-settings"', _read(WEB / "pages" / "admin.html"))

    def test_absorbed_views_are_gone(self) -> None:
        admin = _read(WEB / "pages" / "admin.html")

        for removed in (
            'id="view-nextcloud-settings"',
            'id="view-notion-settings"',
            'data-shell-panel="notion"',
            'data-shell-panel="nextcloud"',
            'id="study-settings-modal"',
        ):
            self.assertNotIn(removed, admin, "absorbed into the study settings panel")

    def test_nav_items_reuse_the_existing_tab_styling(self) -> None:
        css = _read(WEB / "styles" / "admin.css")
        self.assertIn(".settings-nav-item {", css)
        self.assertNotIn(".settings-hub-tab", css)


class PluginUiContractTests(unittest.TestCase):
    def test_desktop_shortcut_button_has_one_click_handler(self) -> None:
        admin = _read(WEB / "scripts" / "admin" / "admin-controller.js")

        self.assertEqual(
            admin.count("$('btn-create-shortcut')?.addEventListener('click'"),
            1,
            "one click must create only one desktop shortcut",
        )

    def test_sensor_rich_views_and_timeline_preferences_are_plugin_owned(self) -> None:
        dashboard = _read(WEB / "scripts" / "admin" / "admin-dashboard-controller.js").lower()
        timeline = _read(WEB / "scripts" / "admin" / "session-timeline.js")
        packaging = _read(
            PROJECT_ROOT.parent
            / "release_tools"
            / "pyinstaller"
            / "study_runner_server_common.py"
        )

        # The core modules that draw sensor tiles never name a sensor.
        core_modules = (
            "dashboard-ui-helpers.js", "live-trend.js", "sensor-columns.js", "sensor-tile-order.js", "sensor-tile-drag.js",
        )
        core_sources = [dashboard] + [_read(WEB / "scripts" / "admin" / name).lower() for name in core_modules]
        for sensor_key in ("brainbit", "mini_radar", "camera_emotion", "am_hub"):
            for source in core_sources:
                self.assertNotIn(sensor_key, source)
        self.assertNotIn("PREFERRED_CHANNELS", timeline)
        self.assertIn("ui?.timeline?.preferred_channels", timeline)
        self.assertIn('ui.get("extensions")', packaging)
        self.assertIn('ui.get("assets")', packaging)

    def test_every_relative_import_of_a_plugin_ui_module_is_a_declared_asset(self) -> None:
        # The asset route serves only files the manifest declares. A module
        # that imports an undeclared file fails as a whole in the browser,
        # while every test that reads the files from disk still passes.
        import_pattern = re.compile(r"""(?:\bimport\s*\(\s*|\bfrom\s+|\bimport\s+)(['"])(\.{1,2}/[^'"]+)\1""")
        problems = []
        for manifest_path in sorted((PROJECT_ROOT / "study_runner" / "plugins").glob("*/*/manifest.json")):
            plugin_root = manifest_path.parent.resolve()
            plugin = plugin_root.name
            ui = json.loads(_read(manifest_path)).get("ui") or {}
            declared = {*(ui.get("extensions") or {}).values(), *(ui.get("assets") or [])}
            pending = sorted(entry for entry in declared if entry.endswith(".js"))
            seen: set[str] = set()
            while pending:
                entry = pending.pop()
                if entry in seen:
                    continue
                seen.add(entry)
                for _, specifier in import_pattern.findall(_read(plugin_root / entry)):
                    resolved = (plugin_root / entry).parent.joinpath(specifier).resolve()
                    try:
                        target = resolved.relative_to(plugin_root).as_posix()
                    except ValueError:
                        problems.append(f"{plugin}: {entry} imports {specifier}, outside the plugin folder")
                        continue
                    if target not in declared:
                        problems.append(f"{plugin}: {entry} imports {target}, which the manifest does not declare")
                    if resolved.is_file():
                        pending.append(target)
        self.assertEqual(problems, [], "list every imported file under ui.assets in the plugin's manifest.json")

    def test_admin_plugin_lists_have_no_recorder_or_worker_key_maps(self) -> None:
        for name in ("admin-controller.js", "admin-dashboard-controller.js"):
            source = _read(WEB / "scripts" / "admin" / name).lower()
            self.assertNotIn("labrecorder", source)
            self.assertNotIn("emotion_worker", source)
            self.assertNotIn("plugin_tile_icons", source)
            self.assertNotIn("plugin_icons", source)

        dashboard = _read(WEB / "scripts" / "admin" / "admin-dashboard-controller.js")
        self.assertIn("status.recording_infrastructure", dashboard)
        self.assertIn("status.recording_worker", dashboard)
        self.assertIn("recording-worker-issues", dashboard)
        self.assertNotIn("pluginsWithCapability('recording_worker')", dashboard)

    def test_catalog_visibility_drives_all_generic_plugin_surfaces(self) -> None:
        catalog = _read(WEB / "scripts" / "shared" / "plugin-catalog.js")
        machine_panel = _read(WEB / "scripts" / "settings" / "machine" / "machine-settings-panel.js")
        dashboard = _read(WEB / "scripts" / "admin" / "admin-dashboard-controller.js")
        study_panel = _read(WEB / "scripts" / "settings" / "study" / "study-settings-panel.js")

        self.assertIn("PLUGIN_UI_SURFACES", catalog)
        self.assertIn("PLUGIN_UI_SURFACES.SETTINGS_HUB", machine_panel)
        self.assertIn("PLUGIN_UI_SURFACES.DASHBOARD", dashboard)
        self.assertGreaterEqual(dashboard.count("PLUGIN_UI_SURFACES.DASHBOARD"), 2)
        self.assertIn("PLUGIN_UI_SURFACES.STUDY_SETTINGS", study_panel)
        self.assertIn("PLUGIN_UI_SURFACES.DESTINATION_SETTINGS", study_panel)
        self.assertNotIn("pluginByKey('notion')", study_panel)
        self.assertNotIn("pluginByKey('nextcloud')", study_panel)
        self.assertNotIn("settingsHubAction('notion'", machine_panel)
        self.assertNotIn("settingsHubAction('nextcloud'", machine_panel)

    def test_destination_credentials_and_actions_are_manifest_driven(self) -> None:
        study_folder = WEB / "scripts" / "settings" / "study"
        # The panel, the destination block it renders, and the block's rules.
        study_panel = "\n".join(
            _read(study_folder / name)
            for name in ("study-settings-panel.js", "destination-block.js", "destination-rules.js")
        )
        machine_panel = _read(WEB / "scripts" / "settings" / "machine" / "machine-settings-panel.js")
        admin_html = _read(WEB / "pages" / "admin.html").lower()

        self.assertIn("capability_config?.credentials", study_panel)
        self.assertIn("capability_config?.admin_actions", study_panel)
        self.assertIn("data-study-plugin-credential", study_panel)
        self.assertIn("data-study-plugin-action", study_panel)
        self.assertIn("capability_config?.credentials", machine_panel)
        self.assertNotIn("notion", study_panel.lower())
        self.assertNotIn("nextcloud", study_panel.lower())
        self.assertNotIn("btn-notion", admin_html)
        self.assertNotIn("btn-nextcloud", admin_html)
        self.assertFalse((WEB / "scripts" / "settings" / "study" / "notion-settings-controller.js").exists())
        self.assertFalse((WEB / "scripts" / "settings" / "study" / "nextcloud-settings-controller.js").exists())

    def test_missing_plugin_placeholder_is_read_only_until_confirmed_removal(self) -> None:
        study_panel = _read(WEB / "scripts" / "settings" / "study" / "study-settings-panel.js")

        self.assertIn("data-unavailable-plugin", study_panel)
        self.assertIn("data-remove-unavailable-plugin", study_panel)
        self.assertIn("window.confirm(message)", study_panel)
        self.assertIn("const plugins = { ...current.plugins }", study_panel)
        self.assertIn("const sensors = { ...current.sensors }", study_panel)

    def test_study_credentials_are_not_offered_for_the_whole_computer(self) -> None:
        panel = _read(WEB / "scripts" / "settings" / "machine" / "machine-settings-panel.js")
        self.assertIn("credential.per_study === true", panel)
        self.assertIn("capability_config?.upload_destination ? groupUploads : groupSensors", panel)
        self.assertIn("visibility?.dashboard === false", panel)

    def test_editor_preview_cards_are_mounted_live(self) -> None:
        editor = _read(WEB / "scripts" / "admin" / "admin-study-editor.js")
        admin_css = _read(WEB / "styles" / "admin.css")
        self.assertIn("mountCard(", editor)
        self.assertIn("{ mode: 'preview' }", editor)
        self.assertIn(".preview-card-wrap.selected .q-card-study {\n  pointer-events: auto;", admin_css)

    def test_participant_layout_and_open_card_surface_are_shared_with_preview(self) -> None:
        study_html = _read(WEB / "pages" / "study.html")
        study_css = _read(WEB / "styles" / "study.css")
        main_css = _read(WEB / "styles" / "main.css")
        participant = _read(WEB / "scripts" / "participant" / "study-controller.js")
        navigation = _read(WEB / "scripts" / "participant" / "participant-result-submission.js")
        panel = _read(WEB / "scripts" / "settings" / "study" / "study-settings-panel.js")

        self.assertIn("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)", study_css)
        self.assertRegex(study_html, r'id="screen-questions"[^>]*>\s*<div id="q-container"></div>\s*</div>\s*<div class="q-nav" hidden>')
        self.assertIn("bottom: 0;", study_css)
        self.assertIn(".study-progress-bar {\n  position: fixed;\n  left: 0;\n  right: 0;\n  top: 0;", study_css)
        self.assertIn("nav.hidden = false", navigation)
        self.assertIn("body:not(:has(#screen-questions.active)) .q-nav", study_css)
        # `hidden` must win over `display: grid`, or the finish card keeps its navigation.
        self.assertIn(".q-nav[hidden] {\n  display: none;", study_css)
        self.assertIn("toggleAttribute('hidden', screenName !== 'questions')", participant)
        self.assertIn("body:has(#screen-questions.active) .study-preview-banner", study_css)
        self.assertIn("#study-toast {\n  position: fixed;", study_css)
        # card_frame_enabled toggles the frame for a real participant (and the
        # real `?preview=1` run of study.html, which shares this same code).
        # The editor's own stacked card list is a different "preview" - an
        # always-framed editing list, not a rendering of the study - and does
        # not mirror this setting; see test_editor_preview_cards_are_mounted_live.
        self.assertIn(".study-card-frame--off .q-card-study", main_css)
        self.assertIn("document.body.classList.toggle('study-card-frame--off'", participant)
        self.assertIn("'study-card-frame-enabled'", panel)

    def test_mood_and_affect_views_have_responsive_visuals(self) -> None:
        for card_name in ("mood_meter", "affect_map"):
            with self.subTest(card=card_name):
                css = _read(WEB.parents[1] / "plugins" / "cards" / card_name / "card.css")
                self.assertIn("62dvh", css)
                self.assertIn(".mm-field-hint", css)
                self.assertIn("overflow-wrap: anywhere;", css)

    def test_finalization_view_accepts_unknown_steps_and_is_accessible(self) -> None:
        view = _read(WEB / "scripts" / "admin" / "session-progress-rail.js")
        monitor = _read(WEB / "scripts" / "admin" / "upload-monitor.js")
        admin_html = _read(WEB / "pages" / "admin.html")

        self.assertNotIn("STEP_LABELS", view)
        self.assertIn("finalizationStepLabel(step, t)", view)
        self.assertIn("sessions.rail.title", view)
        self.assertIn('role="progressbar"', monitor)
        # The notice leads to the session instead of opening a modal.
        self.assertNotIn("createModal", monitor)
        self.assertIn("onOpenSession", monitor)
        self.assertIn('id="session-progress-rail"', admin_html)
        self.assertNotIn("btn-session-back", admin_html)
        self.assertNotIn('iconoir-cloud-upload"></i>', monitor)

    def test_required_readiness_blockers_cannot_be_confirmed_away(self) -> None:
        admin = _read(WEB / "scripts" / "admin" / "admin-run-control.js")
        start = admin.index("async function startLoadedStudyRun")
        blocked = admin.index("state.readiness?.start_blocked === true", start)
        warning = admin.index("state.readiness?.ready === false", blocked)
        blocked_branch = admin[blocked:warning]

        self.assertIn("await showBlockingReadinessDialog(blockers)", blocked_branch)
        self.assertIn("return;", blocked_branch)
        self.assertNotIn("showToast(", blocked_branch)
        self.assertNotIn("confirmWithModal(", blocked_branch)

    def test_readiness_failures_are_persistent_and_survive_a_stale_precheck(self) -> None:
        admin = _read(WEB / "scripts" / "admin" / "admin-run-control.js")
        admin_html = _read(WEB / "pages" / "admin.html")

        self.assertIn('id="hub-study-readiness"', admin_html)
        self.assertIn('id="hub-study-readiness-list"', admin_html)
        self.assertIn('id="btn-readiness-settings"', admin_html)
        self.assertIn("readinessListMarkup(blockers)", admin)
        self.assertIn("Array.isArray(blocker?.details)", admin)
        self.assertIn("error.status === 409 && error.payload?.readiness", admin)
        self.assertIn("state.readiness = error.payload.readiness", admin)
        self.assertIn("recording_worker_unavailable", admin)

    def test_stimulus_deadline_is_fixed_before_prepare_and_stop_does_not_wait(self) -> None:
        source = _read(WEB / "scripts" / "participant" / "participant-stimulus-execution.js")
        start = source.index("async function startStimulusCard")
        schedule = source.index("const scheduleStartMs = performance.now()", start)
        prepare = source.index("'/api/trial/prepare'", start)

        self.assertLess(schedule, prepare)
        self.assertNotIn("await sendReliableStudyEvent('/api/stop'", source)
        self.assertIn(
            "stimulusRun.plannedDeadlineEpochMs = toEpoch(stimulusRun.plannedDeadlinePerfMs)",
            source[schedule:prepare + 700],
        )
        self.assertIn(
            "planned_deadline_epoch_ms: stimulusRun.plannedDeadlineEpochMs",
            source[schedule:prepare + 700],
        )


if __name__ == "__main__":
    unittest.main()
