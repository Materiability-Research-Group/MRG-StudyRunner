# Notion upload plugin

Publishes finalized result and card-summary JSON to a Notion database, as an
optional per-study upload destination. Like the Nextcloud plugin, it only
runs after a session has finalized — never during acquisition.

## Architecture

Study Runner plugins are API v5: the core process supervises this plugin as
a subprocess and never imports its Python modules directly (see
`docs/file-guide.md`). This folder follows that same shape:

- `driver.py` — the only executable entry point. A one-line wrapper that
  calls `run_plugin_driver("notion")`; the core never imports anything else
  in this folder.
- `plugin.py` — the plugin logic running inside the supervised subprocess:
  status, publish, and configurator admin actions.
- `adapter.py` — the Notion API integration itself: client construction and
  caching, database/data-source resolution, participant-metadata property
  mapping, and the actual page upload.
- `linked_targets.py` — prepares mapped relations, participant summaries,
  and filtered Notion views.
- `manifest.json` — declares this as an `upload_destination` capability
  plugin, its one credential (`STUDY_RUNNER_NOTION_API_KEY`), its study
  settings (`parent_page_id`, `database_id`, and the discovered
  `data_source_id`/`sessions_database_id`), and its admin actions
  for connection checks, previews, and schema preparation.
  `sample_delivery: none` — like Nextcloud, it moves
  already-finalized files, not live samples.

## Where the code comes from

`adapter.py` is built directly on the **official `notion-client` Python
package** (`notion-client>=3.0.0`, pinned in `software/requirements.txt`) —
`Client(auth=api_key, timeout_ms=...)` from `notion_client`, called the same
way Notion's own SDK documentation shows. There is no bundled or copied
Notion example code: `get_client()`, `test_connection()`, and
`upload_study_result()` are original code that wraps the official client for
this project's specific needs (database/data-source discovery, participant
metadata property creation, error classification via `APIErrorCode`/
`APIResponseError`). Everything Notion-specific here is standard, documented
SDK usage, not adapted from a vendor example.

## Legacy layout

Two databases under the study's parent page, each found by its title before
anything is created, so a lost response or a retry never duplicates them:

- **StudyRunner Participants** — one row per pseudonymized participant ID,
  with the intake fields the participant card marks as stored, a session
  count, and the first/last session date.
- **StudyRunner Sessions** — one row per session, related to its participant
  row, with start, end, duration, card/answered/skipped counts and the
  sensors that delivered data. The session's page holds two real Notion
  tables: every card's answer with its duration, and every sensor stream's
  per-card statistics (mean/mode, min, max, std, coverage, longest gap) from
  the finalized `card-summary.json`. Nothing here names a specific sensor.

The Session ID column is the idempotency key; a commit marker is the page's
last block, so a retry finishes or replaces a cut-short row.

## Settings

- `parent_page_id` / `database_id` / `data_source_id` (study settings) —
  where results get written. The adapter can create the results database
  under the parent page on first use if it does not exist yet.
- `api_key` (per-study credential, env var `STUDY_RUNNER_NOTION_API_KEY` or
  the local secret store) — the Notion integration token. Never written into
  an exported study file.

The admin action **Test connection** (in the machine settings hub) exercises
the same `notion_client.Client` call a real publish would use, so an invalid
API key is caught before a study ever tries to upload.

## Configurable targets

The study's `export_mapping` is JSON in its Notion study settings. The
configurator can add session, participant, and card targets. **Simple** adds
a session target; **Analysis** adds a card target. Each click appends a new
target with its own ID, so existing targets and their database connections
remain in place. The older two-database layout still uploads unchanged for
studies with no configurable targets. Moving such a study to configurable
targets requires an explicit confirmation: its old databases stop receiving
new rows after the switch.

Connect a card target to a participant target to get a Notion relation. A
participant page can then contain a linked card view filtered to that
participant. One shared card database holds every card row; comparison views
filter this database by each card's stable `card_id`. A card target can also
connect to a session target, and a session target to a participant target.
The relation list and target graph edit the same connections. Participant
targets can summarize related rows with count, mean, min, max, or the last
non-empty value ordered by session start. These summaries are recalculated
after each upload or retry.

New study cards receive a persistent `card_id`. Finalized card details carry
that ID; the card database also records a content revision marker. The card
row's per-session key remains the upload retry key. Existing historical cards
can be matched by their original position only when the card type and prompt
still match. Ambiguous cards are listed for operator review by the backfill
dry run and their sessions are not queued.

Use **Check sources and types** before a study has any finished sessions.
It lists the current study's enabled sensor plugins and declared streams,
channels, and cards. A declared sensor source can have no value in a
particular run. Selecting a finished session from this study adds actual
values to the preview; sessions from other studies are never offered.

Use **Review required properties** after connecting targets to databases.
The configurator lists the relation and system properties it needs and asks
for confirmation before adding them in Notion. Uploads fail with the missing
property names until this preparation is done. Automatic view creation uses
the Notion Views API version `2026-03-11`; an integration must be allowed to
read and create views. Existing views are discovered before creation so a
retry does not duplicate them.

**Export JSON** writes version 1 of the parent page and mapping without any
API key. **Import JSON** validates the file and previews its targets before
saving. When restoring the same study, the operator can keep Notion IDs or
remove them. An import into another study always removes IDs and requires
reconnecting databases. **Review saved sessions** is a dry run of the
historical backfill. Confirming it queues resumable uploads through the
ordinary upload-job service with the current study mapping. Repeating the
same backfill does not create duplicate jobs or Notion rows.
