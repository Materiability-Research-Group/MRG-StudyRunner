# Notion upload plugin

Publishes finished sessions to Notion, as an optional per-study upload
destination. Like the Nextcloud plugin, it only runs after a session has
finalized, never during a recording. The full data always stays on the
study computer; Notion receives only the values the study's layout names.

## What appears in Notion

On the study's Notion page (`parent_page_id`, pasted as a "Copy link" link):

```
Your Notion page
 ├─ Participants            one row per participant (always)
 │   └─ Answers             inside each participant's page (opt-in)
 │                          one row per session and card
 └─ Card comparison         a page (opt-in)
     ├─ 02 · How warm?      one table per card, one row per session,
     └─ 03 · Pick one       linked to the participant's row
```

- **Participants** shows the participant ID and what the researcher picks:
  the number of sessions, the first and last session, and the intake fields
  the participant card stores.
- **Answers** (switch: "An answers table for every participant") lists every
  card of every session of that participant, with its status and the values
  ticked under "Answers table". Its answer column is text, because it mixes
  every card type.
- **Card comparison** exists as soon as one value is ticked under "Card
  comparison". Each card's table has typed columns: a slider answer is a
  number, a single choice a select, a semantic differential one number per
  word pair (`answer_columns.py`). The values are copied into the cells, so
  Notion can sort, filter and chart them, and every row links to the
  participant's row.

Every included card gets a row in every session, also when it was skipped
or never shown. Its `Status` column says which (`answered`, `skipped`,
`not shown`, or `shown` for a card without an answer), and its values are the
layout's missing codes: `9999` for numbers and `n.a.` for text and choices by
default. A card that was required but left unanswered cannot be told apart
from one that was never shown, so both are `not shown`.

Column and table names are fixed English names. Rename the tables in Notion
if you like, but keep the column names: the upload finds its columns by name,
and a renamed one is added again.

## Uploading again

Every write is "update or create" by a key: the participant ID, `session id`
plus card id in an answers table, the session id in a card table (the hidden
`StudyRunner Key` column). A retry, or **Upload again** on a finished upload
in a session's detail view (any destination plugin offers it), therefore
updates the rows that session wrote and never adds a second copy. Uploading
again uses the study's current settings, so a changed layout reaches sessions
recorded before it. Columns the layout no longer lists, rows of other sessions
and anything added by hand in Notion stay untouched; the plugin never deletes
or archives anything.

Tables are found again even when a saved ID is lost: the Participants table
by its title on the page ("Participants", or "StudyRunner Participants" from
plugin 1.x, which is taken over), the comparison page by its title, and each
card table and answers table by a marker in its description. A study set up
with only a `database_id` creates its new tables on the page that database is
on.

## The layout setting

`notion_layout` (study setting, edited with **Configure…**):

```json
{
  "version": 1,
  "participants": {"columns": ["session_count", "first_session", "last_session", "age_group"]},
  "answers_per_participant": true,
  "values": [
    {"source": "answer", "answers": true, "comparison": true},
    {"source": "duration_seconds", "answers": true, "comparison": false},
    {"source": "stream.brainbit_eeg.channel.alpha.mean", "answers": false, "comparison": true}
  ],
  "cards": {"<card id>": false},
  "card_comparison": true,
  "missing": {"number": 9999, "text": "n.a."}
}
```

`values` is one list for every card. A sensor value names the stream by its
manifest `source_id`, so it reads the same stream in every session.
`cards` stores only the cards switched away from their default (info cards
are off, every other card on). An empty layout sends the Participants table
only. `layout.py` validates the setting when the study is saved.

The configurator shows the four steps on the left and "What Notion will look
like" on the right. **Check in Notion** reports which tables and columns
exist; **Create tables now** creates the missing ones at once (otherwise the
first upload does). **Preview with a recorded session** shows what that
session would write, read from disk without calling Notion. **Export** and
**Import** move a layout to another study; card choices are kept only within
the same study.

## Files

- `driver.py`: the executable entry point, `run_plugin_driver("notion")`.
- `plugin.py`: status, publish, the configurator's admin actions and the
  study-setting validation.
- `adapter.py`: the Notion client and its cache, the connection test, the
  upload entry point and the configurator actions.
- `layout.py`, `answer_columns.py`, `session_rows.py`: what to write, without
  any Notion call.
- `notion_api.py`: the Notion calls, paced to about three per second; a
  `rate_limited` answer waits as long as Notion asks and tries again.
- `structure.py`, `publisher.py`: find or create the tables, write one session.
- `ui/`: the configurator (`configurator.js`) and its parts.

The plugin uses the official `notion-client` package (version 3, Notion API
2025-09-03, data sources), pinned in `software/requirements.txt`.

## Settings

- `parent_page_id` (study setting): the Notion page the tables go on.
- `database_id`, `data_source_id`, `comparison_page_id` (read-only): found or
  created by the plugin and saved on the study.
- `notion_layout`: see above.
- `api_key` (per-study credential, or `STUDY_RUNNER_NOTION_API_KEY`): the
  integration secret. It stays in the local secret store and is never written
  into a study file.

Changing the page makes the saved IDs start over (`target_settings` in the
manifest); changing the layout keeps them.
