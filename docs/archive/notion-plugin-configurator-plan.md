# Notion plugin: a configurator instead of raw IDs

This is the implementation plan for turning the Notion destination plugin's
settings from three raw Notion IDs into a guided configurator: a page tree to
pick or create a database in, and a mapping from Study Runner's outputs onto
that database's columns. It also records the decisions already made about
scope, so a later session does not have to re-derive them.

Phase 1 (the retry/target-drift bugfix below) is shipped. Phases 2 and 3 are
not started.

## Context

An operator reported that after a failed Notion upload (a flaky connection),
retrying it later for an already-finished session "jumped back" to the old
Notion page, and that changing the parent page in Study Runner's settings did
not fix it even after deleting the stored credentials. The real cause (fixed
in Phase 1): a retry always replays the exact destination settings the
session was frozen with when it finished, and a changed hand-entered setting
(the parent page) left the auto-discovered settings (the database id) stale,
pointing at the previous target. See `CHANGELOG.md` and
`software/study_runner/plugins/destinations/notion_upload/README.md`.

While discussing the fix, the operator asked for a much larger change: fold
the plugin's three nearly-invisible settings (parent page id, database id,
data source id) into a proper configurator, similar in spirit to Grasshopper's
node editor, so the Notion side of a study can be shaped without typing raw
Notion ids by hand.

## Decisions already made with the operator

- **No fixed set of databases.** Earlier drafts of this plan assumed exactly
  three databases (participants, sessions, answers). The operator corrected
  that: Study Runner has no business deciding how many Notion databases a
  study needs. The mapping instead holds a list of *targets* the operator
  defines; each target is one Notion database with one declared **row
  level** - one row per session, per participant, or per card. Presets exist
  only as a starting point (they create targets and columns, then everything
  is editable):
  - "Simple": one database, one row per session. This is the new default for
    a study that has never configured Notion before.
  - "Analysis": one database, one row per card - good for filtering and
    grouping answers across sessions in Notion itself.
  - "As before": a participants database and a sessions database with a
    relation between them, matching exactly what the plugin produces today.
    An existing study's settings migrate into this preset unchanged, so
    nothing already uploading breaks.
- **Retry asks each time the target differs**, rather than always using the
  current settings or always freezing the original ones (Phase 1, shipped).
- **Desktop with a mouse** is the only interaction target for the mapping
  editor; no touch/tablet requirement.
- **Modal size** follows this codebase's existing large-modal convention
  (`.settings-modal--console`, see `apps/ui/styles/admin.css`), scaled up
  further for a two-pane workspace: roughly `min(1520px, 100vw - s4)` wide by
  `min(940px, 100vh - s4)` tall, with a left rail of `clamp(220px, 18vw,
  280px)`. (The operator pointed at a comparable external product, OCYRA, as
  a size reference; OCYRA's source is not in this repository, so this is this
  codebase's own equivalent, not a copy of OCYRA's CSS.)
- **Node-editor library**: none. A GitHub survey (2026-10) found every
  maintained vanilla-JS node-editor library either requires a framework and a
  build step (React Flow, Rete.js v2, Baklava.js - all excluded by this
  project's "no heavy framework", "no build step" rules in
  `CONTRIBUTING.md`) or draws on a `<canvas>` and is unmaintained
  (litegraph.js, last push 2024). Drawflow (MIT, vanilla JS, HTML nodes) is
  the only real candidate and is itself stale since 2024-10. Phase 3 therefore
  builds a small purpose-made canvas instead of adding a dependency; vendoring
  Drawflow under `apps/ui/vendor/drawflow/` (like `vendor/iconoir/`) remains a
  fallback if the custom canvas turns out to be too large a scope on its own.

## Phase 1 - retry/target drift (shipped)

Generic for every `upload_destination` plugin, not Notion-specific:

- A retry now compares the job's frozen destination settings with the
  study's *current* ones (`describe_retry_target` in
  `runtime_core/delivery/upload_runtime.py`, `upload_jobs_service.py`,
  `finalization_service.py`) and, when they differ, lets the operator choose
  "use current target" or "keep original target"
  (`apps/ui/scripts/admin/finalization-actions.js`) before the retry runs.
- Saving a study now clears a destination's stale auto-discovered settings
  (e.g. a database id) when a hand-entered setting (e.g. the parent page)
  changed without the discovered ones also changing in the same save
  (`reset_stale_upload_discoveries` in
  `runtime_core/studies/study_plugin_config.py`). The comparison only runs
  when the study being saved is the one that was already active - comparing
  across two different studies (e.g. switching which study is active) must
  never be mistaken for an edit and would otherwise risk wiping the
  newly-activated study's own legitimate discoveries.
- The Notion adapter re-verifies a cached data source id and sessions
  database id against the current database/parent page before reusing them,
  instead of trusting an unconditionally cached value
  (`plugins/destinations/notion_upload/adapter.py`).

## Phase 2 - the configurator modal

### A generic plugin UI slot (core)

Destination plugins already get a `dashboard` UI extension surface
(`contracts/manifest.py`'s `UI_EXTENSION_SURFACES`,
`apps/ui/scripts/shared/plugin-catalog.js`'s `EXTENSION_EXPORTS`). Add a
second surface, `study_settings`, the same way: a plugin that declares
`ui.extensions.study_settings` gets a "Configure..." button in the study
settings panel (`apps/ui/scripts/settings/study/study-settings-panel.js`)
that opens the plugin's own module instead of the generic field renderer.
This is core work because the same mechanism will suit any future
destination that outgrows a handful of flat settings, not because Notion
needs special-casing.

- New modal variant in `apps/ui/styles/admin.css` (see "Modal size" above),
  a two-column grid: a left rail and a larger right pane.
- New study-settings field type, `"object"` (opaque JSON, bounded size, not
  rendered as an input) in
  `runtime_core/studies/validation.py#_validate_plugin_study_settings`. Its
  contents are validated by an optional `validate_study_settings` hook on
  `Plugin` (`contracts/plugin_api.py`) so core stays ignorant of what a
  mapping actually means. Update `tools/plugin_templates/` and add a test
  that holds every plugin declaring an `"object"` field to providing the
  hook.

### The Notion plugin's own configurator

New admin actions (string payloads only, like the existing `test_connection`
action):

- `list_children(page_id)` - the databases and subpages directly under a
  page, one level at a time, paginated. The tree only ever shows this one
  parent page's own subtree; a workspace-wide search is not worth building,
  because Notion already restricts an integration to the pages it was
  explicitly shared with.
- `describe_database(database_id)` - a database's title and its properties
  (name + type), for the column-mapping list.
- `create_database(parent_page_id, title, row_level, columns_json)`.
- `preview_mapping(mapping_json, session_ref)` - evaluates the mapping
  against one real finished session and returns the exact property values
  Notion would receive, without writing anything.

New study setting, `export_mapping` (type `"object"`):

```
{
  "preset": "simple" | "analysis" | "as_before" | "custom",
  "targets": [
    {
      "id": "...",
      "title": "...",
      "database_id": "...",
      "row_level": "session" | "participant" | "card",
      "columns": { "<notion column name>": { "type": "...", "source": "..." } },
      "relations": [ ... ]
    }
  ]
}
```

- The key column (what makes an upload idempotent) is derived from a
  target's row level, not chosen freely: Session ID for a session-level
  target, Participant ID for a participant-level one, Session ID + card
  index for a card-level one.
- A relation between two targets is offered only between compatible levels
  (card -> session, session -> participant) and is never mandatory.
- **Row level decides cardinality**, the same role "item" vs. "list" plays
  in Grasshopper. A per-card source (an answer, a sensor summary for one
  card) is a single value on a card-level target but a *list* on a session-
  or participant-level target, and a list can only reach a column through an
  explicit reducer - mean/min/max/count over cards, pick one card, join as
  text, or "one column per card" (expands into literal `Q1`, `Q2`, ...
  columns). Mapping an unreduced list is rejected with that explanation
  rather than silently stringified. A participant-level target reduces
  across that person's sessions the same way.
- The **output catalog** (what can be mapped) is built from the study's own
  configuration and plugin manifests, so it never names a specific sensor or
  card plugin in core logic: session identity/timing, participant metadata
  fields, each card's prompt/answer/duration from `answer_details`, and each
  card x stream x channel's mean/min/max/standard deviation/mode/coverage/
  max-gap from `card-summary.json`. Raw per-sample signal values are
  deliberately not offered - a Notion property holds one value of at most
  2000 characters, so raw samples never fit and should not be attempted.
- `adapter.py` becomes mapping-driven, with the "as before" preset as the
  built-in default so an existing study's upload output does not change.
  Each target is upserted by its own key column; the existing idempotency
  trick (a trailing "commit marker" block proving a page write finished) is
  kept for whichever target holds the per-session page content.
- Browser module `ui/configurator.js` (+ `configurator-tree.js`,
  `configurator-mapping-list.js`, styles), each file kept under the existing
  1,000-line frontend ceiling (`tools/measure_structure.py`):
  - **Left:** the preset picker (first open only), the list of configured
    targets with their row level, and the page tree to attach a target to an
    existing or new database.
  - **Right (Phase 2):** a column-list mapping - auto-map by name, type
    badges, add-column, and validation (title column mapped, types
    compatible, the key column present).
  - **Footer:** "Preview with session...", then Save/Discard.
- Readiness gains a schema-drift check (a renamed/removed Notion column
  blocks study start with a clear message instead of failing at upload
  time), and the upload result additionally records a hash of the mapping
  that produced each row, next to the existing plugin-version provenance.
- "Upload all sessions again with this mapping" (a backfill action) is safe
  because every target is upserted by its key column.
- The configurator shows a privacy note when a column maps a free-text
  answer to an external service.
- Version bump: Notion plugin MINOR (existing data keeps its meaning; the
  default mapping reproduces today's output).

## Phase 3 - node canvas

- `export_mapping` gains `nodes` and `edges` with positions. The Phase 2
  column list remains available as the "simple view" over the same
  underlying model - a column mapped straight from one source is exactly a
  two-node graph, so the two views can stay interchangeable rather than
  becoming two separate formats.
- Own small SVG/DOM canvas (`ui/node-canvas/`, split into files under the
  1,000-line ceiling): CSS-transform pan/zoom, bezier wires, pointer-drag
  ports. Ports are color-coded by type (number, text, date, list, select,
  boolean); an incompatible wire is refused with a tooltip instead of
  silently coercing.
- Node set, deliberately small: source nodes (from the output catalog),
  column nodes (from the selected database), and function nodes - round(n
  decimals), scale/unit, mean/min/max/count over cards, pick card, format
  date, join list, text template, default-if-empty, number-to-select.
- UX: auto-layout/zoom-to-fit, a right-click/search add-node menu,
  delete/undo/redo, a live preview value shown on each wire, a dirty
  indicator, and "discard changes" on close.
- Evaluation of a mapping (list or graph form) stays entirely in Python
  (the plugin), so a preview and the real upload can never disagree; the
  browser canvas only checks port types for immediate connection feedback.

## Critical files

- `software/study_runner/plugins/destinations/notion_upload/` (`manifest.json`,
  `plugin.py`, `adapter.py`, `README.md`, new `ui/`)
- `software/study_runner/contracts/manifest.py`, `contracts/plugin_api.py`
- `software/study_runner/runtime_core/studies/validation.py`,
  `study_plugin_config.py`
- `software/study_runner/apps/ui/scripts/settings/study/study-settings-panel.js`,
  `scripts/shared/plugin-catalog.js`, `styles/admin.css`
- `tools/plugin_templates/`, `CHANGELOG.md`, `docs/operator-guide.md`

## Verification

Each phase: `python -m pytest -q -p no:cacheprovider` and
`node --test tests/js/*.test.mjs` from `software/`; `python -m pytest
release_tools/tests` and `python tools/measure_structure.py --check` from the
repository root; `python tools/plugin_versions.py --update` whenever the
Notion plugin's files change.

Manual check for Phase 2/3: an existing study opens in the "as before"
preset and uploads unchanged; switching to "simple" and creating one
database works; mapping a per-card value without a reducer is rejected,
with a reducer it is accepted; a preview against a real session matches the
later upload; renaming a column in Notion blocks study start at the
readiness check; the backfill action re-uploads every session once under a
changed mapping.
