# Changelog

All notable Study Runner changes are documented here. Release tags use
`app-v<version>` and follow semantic versioning.

## Unreleased

### Added

- **Upload again.** In a session's "Completion & uploads" card, every finished upload step now offers "Upload again", for every destination plugin alike. It uploads that one session again with the study's current settings - for example after changing what the study sends to Notion - and leaves the session's status, quality and every other step as they are. A dialog names the destination settings that changed since the session ended. A withdrawn session cannot be uploaded again.

### Changed

- **Notion plugin 2.0.0 (breaking).** The configurable targets, reducers, relations, aggregates, Notion views and both node graphs are replaced by one fixed structure with opt-ins: a **Participants** table on the study's Notion page (choose its columns: sessions, first and last session, stored intake fields); optionally an **Answers** table inside each participant's page, one row per session and card; and optionally a **Card comparison** page with one table per card, one row per session, typed columns (a slider answer is a number, a single choice a select, a semantic differential one number per pair) and a link to the participant's row. One list of values (answer, duration, sensor statistics) applies to every card, each with its own choice of the answers table and the card comparison. A skipped or not shown card still gets its row, with a Status column and missing codes (9999 and "n.a." by default, adjustable). The configurator shows these four steps next to a live "What Notion will look like" tree, checks or creates the tables in Notion, and previews a recorded session. Every write is update-or-create, so a retry or "Upload again" never duplicates rows, and nothing in Notion is ever deleted. Studies of plugin 1.x keep their parent page; their "StudyRunner Participants" table is taken over, the old sessions table is left untouched, and earlier sessions move into the new structure with "Upload again". The setting is now `notion_layout`; `export_mapping` and `sessions_database_id` are no longer read.
- A destination plugin can name the settings that point at its target (`upload_destination.target_settings`). Only a change to those makes its saved ids start over, so editing the Notion layout keeps the found tables.

### Removed

- The Notion backfill ("Review saved sessions", `POST /api/admin/notion/backfill`); "Upload again" replaces it.

### Fixed

- Retrying an upload of a session older than 30 days did nothing: the upload job was looked up only among the last 30 days.
- Notion uploads of large sessions no longer fail on Notion's rate limit: calls are paced and a `rate_limited` answer is waited out.

## 1.7.21 - 2026-10-12

### Added

- Notion targets can link shared participant, session, and card databases. Participant pages show filtered card views; card databases show comparisons by stable card ID, with revision markers and optional participant aggregates. Existing legacy mappings keep their previous upload behavior until explicitly migrated (Notion plugin 1.6.0).
- The Notion configurator can export and import versioned JSON mappings, check configured study sources without a completed session, and backfill saved sessions through resumable upload jobs. Presets add a target without replacing existing targets, and the workspace modal keeps a viewport-sized layout with unsaved-change guards.

## 1.7.20 - 2026-10-12

### Changed

- Each upload destination in Study settings > Data destinations is now one calm block, the same for every destination plugin. A status line names the next missing step (the key, then a required setting) and that control is highlighted; it counts the same things the study start check does. Below it: the per-study key with a short "Save", the fields an operator actually types, and a "Target" row with the plugin's own "Configure…" and its "Test connection". Read-only values sit in a closed "Technical details" section instead of greyed-out inputs. Actions a plugin's configurator uses internally are no longer shown as raw forms (Notion offered seven, with cursor, page ID and mapping fields): a manifest marks them `"surface": "configurator"`, and the dashboard follows the same rule. A panel action no longer asks for a machine setting such as the timeout - Notion and Nextcloud use the one set for this computer.
- Notion: the parent page accepts the whole link from "••• > Copy link"; the ID is read from it wherever Study Runner uses the page, and text that holds no page ID is refused with a hint when the study is saved. The database ID is shown under "Technical details" and set by the configurator. "Target" summarizes where results go, e.g. "„Study Runner Export“ · eine Zeile pro Sitzung · 3 Spalten" (Notion plugin 1.5.0, Nextcloud plugin 1.1.1).

### Fixed

- A failed in-app update now says why. The update panel showed only "install script exited with code 1; see …/update-helper.log", and that log lies in the data folder, which can be elsewhere than the program folder. The message now carries the install script's own reason, and the panel shows this attempt's log lines with a copy button and the log's path. The restart helper writes its own output to that log instead of to the closing terminal, and a macOS prompt about controlling Terminal no longer turns a finished install into a failed one.
- Switching the admin language raised an error from the study settings panel, so an open panel was not re-translated.
- On Windows, text crossing between Study Runner and a plugin's own process lost every character outside plain ASCII - umlauts, "•" - in both directions, because the plugin process wrote in the Windows code page while the server read UTF-8. Plugin processes now use UTF-8 on every system; macOS was not affected.

## 1.7.19 - 2026-10-12

### Fixed

- The Notion "Configure…" button works again. Since 1.7.17 it only showed "The configurator could not be loaded": the configurator loads two more files for its node view, and the server hands out only plugin files the manifest declares, which these two were not. A new test now checks for every plugin that each file its interface modules import is declared (Notion plugin 1.4.3).

## 1.7.18 - 2026-10-08

### Fixed

- Saving the Notion export mapping no longer reports success and closes the configurator when the server actually rejected it (e.g. an invalid mapping) - this previously happened silently, because the generic save path never raised the failure to the configurator. The preset buttons, removing a target, and creating a Notion database now ask for confirmation first, since each of those discards or creates something that cannot be undone from inside the modal. The node view's "+ Source" now offers the same catalog and sensor sources as the column list, instead of asking for a typed id; its other "+" prompts report a clear error instead of silently doing nothing on bad input. The session picker now defaults to the study being configured, and the study sent to the server to build the source catalog is reduced to just its questions (Notion plugin 1.4.2).

## 1.7.17 - 2026-10-08

### Added

- The Notion configurator's column mapping can now be edited as a node graph (sources, reducers, Round, Default-if-empty, columns), as an alternative to the column list - both edit the same mapping. Sensor sources (stream/channel/statistic) and session/participant fields can be picked from a real finished session instead of typed by hand, and the preview session is chosen from a list of completed sessions instead of a typed folder path (Notion plugin 1.4.0).

### Fixed

- A column mapped to a sensor stream (e.g. "mean of the alpha channel") could silently read the wrong stream, or none, in a later session: the key it was stored under was the recording's own per-session stream number, not a stable identifier. It is now keyed on the plugin's declared, stable stream identity instead (Notion plugin 1.3.1).
- The node graph's "Other (type manually)" source option did nothing when the current source already matched a known one, and left stale window-level listeners behind each time the node view was opened again (Notion plugin 1.4.1).

## 1.7.16 - 2026-10-08

### Added

- The Notion settings have a "Configure…" button that opens a large configurator: browse the study's Notion parent page, attach an existing database or create a new one, and decide which column gets which value. Start from a preset - one row per session, one row per card, or exactly as before - and adjust from there. Values per card (answers, sensor statistics) can be reduced to one value per session (mean, min, max, count, join, first, last) or taken from one specific card. A preview computes the rows from a real finished session before anything is written. Existing studies keep uploading exactly as before until a different preset is saved (Notion plugin 1.2.0).
- When the Notion configurator creates a database from a preset, its column names can be German or English, starting from the language of the interface; the key column follows the same choice ("Sitzungs-ID" / "Session ID"). An attached existing database keeps its own title column as the key. Each target's "one row per" level can now be chosen in the configurator, not only through a preset (Notion plugin 1.3.0).
- Plugins can now offer their own study-settings configurator and store a structured setting that the plugin itself validates; a mapping a plugin cannot accept is refused when the study is saved, not later at upload.

### Fixed

- A Notion upload whose session row arrived no longer fails because Notion refused to update the participant summary row afterwards (typically an integration allowed to insert but not to update content). The upload counts as done, and the session's progress shows a warning naming the reason. When Notion refuses access ("403"), the message now keeps Notion's own explanation and names both possible fixes: share the page with the integration, or allow it to read, update and insert content (Notion plugin 1.2.1).
- The "Upload needs attention" notice no longer reappears at every start for uploads whose session no longer exists (deleted, withdrawn, or from a moved data folder), and no longer leads to a session that cannot be found. Marking an upload notice as seen is now remembered across restarts until the upload changes again.
- When a BrainBit is not found and Windows has it paired, the dashboard now says so and explains the fix: remove the BrainBit under Windows Settings > Bluetooth & devices, do not pair it again, switch it off and on, and search again. A Windows pairing hides the band from the scan (BrainBit plugin 4.0.2).

## 1.7.15 - 2026-10-08

### Changed

- Cards the participant operates by touch show a gently tapping finger icon instead of a written instruction: the affect map and mood meter field and feelings wheel, and the word cloud. The finger disappears at the first touch on the card. Screen readers still read the instruction, and the finger keeps still when the device asks for less motion (affect_map plugin 1.3.0, mood_meter plugin 1.3.0, word_cloud plugin 1.2.0).

### Fixed

- Retrying a failed upload for a finished session no longer silently replays the destination settings the session happened to end with. If a study's Notion page (or any other upload destination's settings) changed since then, the retry now asks whether to use the current or the original target before continuing. Changing a destination's hand-entered setting also clears its stale auto-discovered values (such as a database id tied to the previous page), instead of leaving them to point at the old target. Notion additionally re-verifies a cached data source and sessions database against the current one before reusing it (Notion plugin 1.1.1).

## 1.7.13 - 2026-10-07

### Fixed

- A sensor whose calibration finished is ready to start even if its contact measurement before was poor. Contact that was not measured for the current participant still blocks the start (BrainBit plugin 4.0.1).
- The stimulus end sound plays on iPad Safari: audio is unlocked by a real tap and again after the screen was locked, and a sound the browser still blocks is reported to the admin instead of failing silently. It also plays with the iPad's silent switch on (stimulus card plugin 2.0.1).
- The session review shows signals and card markers on the same time axis. Question cards no longer collapse to the start and end, the recorder's clock is mapped to wall time from the study markers for display only, and a plugin's preferred channels are selected again.
- The recorder takes its streams from its own computer when another Study Runner on the same network publishes the same stream names, instead of refusing or recording the other computer's stream.
- Stimulus cards longer than about a minute can be prepared again; a tablet clock calibration mistook a far-future planned deadline for its own age and refused to convert it. Preparation, start and stop now reuse one fixed schedule per attempt, so a clock re-sync between them no longer conflicts.
- In the preparation-failed dialog, Retry, Skip card and Stop study all work again: the tablet and server now agree on the cancellation reasons. A stray tap outside the dialog or Escape no longer stops the study.
- A study event the server permanently rejected no longer blocks every later start, stop and marker behind it in the same session.
- A stimulus the operator skipped past a preparation failure is recorded as not started, instead of appearing as a completed stimulus interval.
- Several settings controls (the recording-timing wait fields, the stimulus end-sound and plugin-action fields, the font picker, study plugin toggles and fields) now match the rest of the app's look (camera_emotion plugin 3.0.1).

## 1.7.12 - 2026-10-07

### Fixed

- 1.7.11 was not published either: a release-time code-structure check stopped it. This release contains every 1.7.10 and 1.7.11 change below.

## 1.7.11 - 2026-10-07

### Fixed

- 1.7.10 was never published: its source package contained an operator's study configuration in place of the shipped example study, and the release checks stopped it. This release ships the example study again and contains every 1.7.10 change below.

## 1.7.10 - 2026-10-07

### Fixed

- Participant pages load again and appear under connected devices. Since 1.7.9 a script error left the page blank after the certificate warning, so no device could connect.
- BrainBit EEG no longer falls behind real time after a paused device callback. A paused batch is re-anchored on its arrival time and reported as a timing discontinuity; no sample is invented. Receipt and measurement times now use the same clock, and the quality and battery streams are declared with their real arrival timestamps (BrainBit plugin 4.0.0).
- Start and end checks compare a sensor's timestamps with the end marker only through a recorded LSL clock correction. Without one, the end check uses the recorder's own receipt time and the session is flagged for review as clock-uncertain, never as data loss. The start check waits for the first correction instead of warning on every session.
- A late session end from one participant can no longer reset sensors already claimed by the next session or by the operator's setup for the next person. A sensor whose reset timed out is held back until the plugin confirms the reset.
- A recording checkpoint counts only samples written before its durable flush.
- Saving a study while a tablet is connected no longer fails with "access denied" on Windows. Reading the study waits for a save in progress instead of treating it as interrupted, and an atomic file replace briefly retries while a virus scanner or indexer holds the file.

### Changed

- New clock core: one module defines who owns each clock, maps plugin source times and server marker times onto the LSL clock without coarse system-clock rounding, and holds the comparison rules shared by live checks and offline validation. Every session gains `meta/clock-report.json` with each stream's declared timestamp source, clock corrections, timeline discontinuities and both boundary checks. Plugin rules for clocks are documented and enforced by tests for every plugin (AM Hub plugin 5.0.1: import path only).

## 1.7.9 - 2026-10-07

### Fixed

- Recording start now follows each selected plugin's manifest: only declared start conditions are checked, and the recorder opens all sources before checking real samples from required regular streams. The machine settings provide bounded start and end wait times.
- BrainBit recalculates its source-clock to LSL-clock mapping when publishing samples, avoiding a stale offset after an outlet opens. At study end, the server writes one end marker, waits for regular streams to pass it, and freezes the recorder before notifying sensor plugins that the participant session ended.
- Finalization completes and validates local artifacts before queuing independent destination uploads. Failed Nextcloud or Notion publication stays retryable without repeating a successful upload or blocking a participant's finish screen. The admin sees one consolidated error per problem.

### Changed

- Plugin manifests and shared runtime contracts define the same lifecycle and sample policies for future sensors, cards, outputs, and backup destinations. Plugin templates and recording workflow documentation follow these contracts.

## 1.7.8 - 2026-10-06

### Fixed

- Participant Start now targets one selected waiting connection. Each waiting page shows a short connection ID, the movable dashboard tile lists eligible pages, and additional open pages remain waiting.
- The dashboard distinguishes a run released by the server from a run seen by the selected Participant page. Start timeouts reconcile against durable run state, delayed responses cannot restore an older state, and an unselected page cannot bypass the server assignment.
- Participant runtime polling and heartbeats now apply the received run state even when supplementary sensor status changes. This fixes the reproduced failure where the server was running but the selected tablet remained on its waiting screen.
- The release helper now rejects an empty release-notes section locally, before it commits or pushes a release tag.

## 1.7.7 - 2026-10-06

## 1.7.6 - 2026-10-06

## 1.7.5 - 2026-10-06
Adding a (participant) device managment system and fixing a major bug who prohibits the start of a study.

## 1.7.4 - 2026-10-05

### Fixed

- **Reliable XDF finalization.** A slow LSL stream now gets a bounded drain period before the recorder safely fences further writes and closes every stream with an honest footer. A safely closed but unconfirmed tail is reported by source ID and recorded sample count; only a reasoned operator acceptance lets merge, parity checks, summaries, CSV, and the manifest continue as `completed_degraded`. Missing or invalid footers, count mismatches, and a failed durable close remain blocking. Freeze retries preserve the original source outcomes.
- A study-required sensor (e.g. AM Hub) whose machine switch is off no longer reports "switched off" when it is actually about to run: the readiness check now uses the exact formula that decides what starts (study selection or session override), not the on-disk machine flag, which the real start logic never consulted either.
- **AM Hub timing and recording (am_hub 5.0.0, data contract change).** Every received hub event, including every normal board frame, is now also kept verbatim in `hub_events`, in addition to its numeric projection - nothing the hub sends is discarded. `hub_clock` records one row per ping attempt, successful or not, with a new `reply_valid` channel; a failed attempt invents no hub time or round trip. The dashboard's clock-offset validity now ages against the real time instead of never expiring on its own last exchange. Each board frame also records the raw radio round trip and clock offset actually used (`radio_rtt_ms`, `clock_offset_ms`) alongside the combined `latency_ms`, and a board status older than 2 s no longer stands in for a current radio RTT. Documentation and UI text now consistently call this latency an estimate, not a measurement.

## 1.7.3 - 2026-10-05

### Fixed

- The 1.7.1 flat session-folder layout change is confirmed intentionally not backward-compatible with the older, nested layout: the pinned 0.7.0 compatibility fixture and its test now live in the flat layout too, and `docs/sensors-and-data.md` / `docs/plugin-recording-architecture.md` spell out that older result folders are left untouched on disk but are not read by the session browser.

## 1.7.2 - 2026-10-05

## 1.7.1 - 2026-10-05

### Changed

- Admin aborts enter a durable `aborting` state, stop open stimulus attempts and the active recorder before reporting success, and can be retried after a failed stop.
- New result folders use `<DATA_DIR>/<study>/<participant>/<UTC>__<session-id>/`; per-study recovery work lives in `_work/`, and a rebuildable `sessions-index.csv` provides an overview. The bundled demo result follows the new layout. **Breaking:** the session browser only reads this flat layout; older, nested `participants/<participant>/sessions/<session>/` result folders are not migrated and will not appear after upgrading (see `docs/sensors-and-data.md`).
- The session file list separates high-resolution recordings, 1-Hz exports, and operational files. The CSV is named `session_1hz.csv`.
- Interrupted tablets offer only checkpoint continuation; a fresh run requires an admin abort and new start.

### Fixed

- A worker-mode crash (`--emotion-worker`, `--recording-worker`, `--plugin-driver`, `--brainbit-cli`, `--apply-update`, and related CLI modes) now prints which mode failed and exits with a clear error instead of a bare traceback.
- The card session-isolation check now also refuses a card that hides mutable state inside a top-level IIFE, which the previous column-0 scan could not see into.

## 1.7.0 - 2026-10-04

### Added

- **Citable software versions.** Every session records the Study Runner version and the version of each plugin that produced it (sensors as recorded, cards, upload destinations) in `meta/manifest.json` → `provenance.software`. The session view lists them and copies a sentence for the methods section, each plugin's settings page shows its version, and the release notes carry a plugin version table. Older sessions show what they recorded, marked as incomplete.
- Plugin versions are kept up to date: a plugin whose files change needs a new semantic version (MAJOR when recorded data changes meaning, MINOR for a compatible feature, PATCH for a fix), recorded with `python tools/plugin_versions.py --update`; a test enforces it. The rules are in `CONTRIBUTING.md` (section 11), `AGENTS.md`, and `CLAUDE.md`.
- **One data contract for every sensor.** Each real sample is one LSL sample, published only through the shared `SensorStreams` helper: outlets come from the manifest, timestamps are explicit and never go backwards, a failed push is counted and shown instead of stopping acquisition. The backup is fixed at 1 Hz. A manifest-declared `live_view` gives every sensor the same live graphs (mean per 0.5 s over the last 60 s). Every stream states where its timestamps come from, and a corrected timestamp stays reversible. The sensor template follows the contract and a test holds every sensor to it.
- **AM Hub measures its timing (data contract change, version 4.0.0).** A ping every second records the hub's clock, the round trip and the estimated clock offset in a new `hub_clock` stream; every radar, bio and valve frame records its latency from the board to Study Runner (`latency_ms`: half the radio round trip the hub measures plus the hub-to-here time). Timestamps stay the arrival time by default; the new setting "Correct timestamps by the measured latency" subtracts it, and `correction_ms` keeps every corrected timestamp reversible. The AM Hub is on the sensor data contract: its graphs are the core's live view, a failed push no longer stops the hub stream, and the dashboard shows a Timing row.
- **MR60 on the sensor data contract (data contract change, version 2.0.0).** Its distance is labelled in centimetres, as the firmware sends it (it was labelled metre; the backup output is now `distance_cm`). `vitals` and `phases` are `double64` and record the board's flags, packet counter (`seq`, the sequence channel) and its own millisecond clock (`device_ms`); timestamps are taken when a packet arrives. The dashboard shows live graphs of heart and breathing rate and distance.
- **Tablet camera emotion on the sensor data contract (data contract change, version 2.0.0).** Each stream gets `correction_ms`: the frame is still dated back to the tablet's capture time, now taken from its arrival before the analysis and reversible, and a capture time in the future or more than 60 s old keeps the arrival time. The frame counters reset with every start. The Emotion Worker's unused second LSL outlet (`--lsl`) is gone. The dashboard shows live graphs of the emotion scores and the face detection.
- **BrainBit on the sensor data contract (version 2.0.0).** Its streams are published through the shared helper and say in their header where their timestamps come from (`host_callback_reconstructed`; diagnostics `host_arrival`); band power and the SDK indices are the core's live graphs, broken where a calibration or an artifact makes them untrustworthy. A push failure now marks BrainBit as failed until it is restarted. Every sensor is on the contract now: manifest validation rejects a recording sensor without a live view or without a timestamp source per stream.
- **Stimulus card: sound, auto-advance and overtime (stimulus 2.0.0).** A card can play a sound when its time is up (built-in gong, bell or beep, or an own file by URL, with volume and a "Play sound" button in the editor). "Continue to the next card automatically" can be switched off: the duration then becomes a minimum, the card stays, Next becomes available, and the time until Next is saved as overtime (`overtime_ms`, at most the card's maximum overtime, after which the card continues by itself and records `overtime_capped`). Whether the stimulus content and the selected actuators keep running during overtime is set per card. A new `stimulus_time_up` marker marks the end of the duration; the card's statistics window ends there and the overtime gets a window of its own in `card-summary.json` (`window: "overtime"`), so both stay comparable between participants.
- **Stimulus cards select their actuators from the manifests.** A plugin that declares trial `start` and `stop` is an actuator, and every stimulus card lists it automatically under "Control actuators (start/stop)"; only the selected ones receive start and stop, also from the server's safety stop. New cards select none. Cards saved before keep what they did: every actuator, except where the old card switched one off. OSC lost its own per-card switch (`forward_marker`, osc 1.1.0). The plugin templates show the pattern and a test holds every plugin to it.
- **Camera emotion marks overtime (camera_emotion 2.1.0).** A snapshot captured after a stimulus card's duration, while the participant stays during overtime, is tagged `phase: "stimulus_overtime"` instead of `stimulus_active`.
- **AM Hub drives actuators (4.1.0).** Selected on a stimulus card, it sends `POST /api/v2/stimulus/start` and `/stop` with the card's identity to the hub, which decides what happens. Prepared ahead of the hub: an unknown endpoint or an unreachable hub never holds up a stimulus and is shown as `last_stimulus_command` in the status.
- **Dashboard tiles can be arranged.** Drag a plugin tile by its title bar (or use the arrow keys on the focused title) into another place or column; this computer remembers the order for every browser and after a restart (`settings/dashboard_layout.local.json`), and "Reset tile order" goes back to the default.

### Changed

- **Sensors record continuously and only receive markers.** BrainBit, the MR60 radar and the AM Hub's sensing no longer react to stimulus start/stop; the misleading "recording on/off" flag of AM Hub and radar is gone (mini_radar 2.1.0). BrainBit no longer forwards EEG to TouchDesigner during stimuli; that option and its settings are removed (brainbit 2.1.0). Notion labels a card's overtime row (notion 1.1.0).
- AM Hub card averages leave out the ESP's 0 for "no value" (counted in `zero_frames`); the XDF keeps every value. Each session's recording plan names the Study Runner version. `docs/sensors-and-data.md` has a methods-and-limitations text per sensor and a hardware acceptance checklist.
- AM Hub dashboard behaves like BrainBit: the graphs stay (empty) when it is off, without an extra off notice.
- One dropdown style everywhere (text-field surface with a chevron, no grey fill), also for the device bars and the study settings; checkboxes in editor fields keep their native size.
- Affect Map: each region has a name, a color and its words in one editor block, in the card's reading order. Named regions replace the default directions in Field and Orbit; a region left empty then has no caption. "Show colors" is a switch that greys out the color pickers. The Mood Meter editor lists its word lists the same way, one column so the words stay readable in the sidebar. Plugin versions: affect_map 1.1.0, mood_meter 1.1.1.

### Fixed

- **Data-integrity recovery:** Acknowledged Card answers and their state now use sequenced, revision-bound partial checkpoints. Reload reconciliation confirms an interrupted stimulus stopped before offering a fresh attempt; stale checkpoints and study changes during acquisition fail closed.
- **Timing evidence:** Tablet/server clock exchanges refresh during a session and retain the selected offset with its round-trip delay. Events label stale or missing evidence as server-arrival estimates; new timed stimuli require a fresh estimate. Packet-gap checks also cover `seq` channels.
- **Camera validity (camera_emotion 3.0.0):** Face-detection confidence now comes from the detector, separately from emotion confidence. Missing faces and analysis errors have distinct validity, unavailable numbers are `NaN`, and publication failures are reported.
- Optional unanswered Cards are recorded as skipped, and later answer revisions retain their timestamps. Sensor timing and inference limitations are documented without claiming physical exposure-time accuracy.
- AM Hub is back in the 60 s flush and crash-recovery sidecars (its manifest had lost `runtime.sidecar`). A plugin that declares `sidecar_export` must now name its sidecar in the manifest, the only place the host reads it from.


## 1.6.0 - 2026-09-30

### Changed

- Participant navigation is anchored to the screen bottom and the optional progress bar to the top; study-specific card frames can be switched off. Mood Meter and Affect Map layouts adapt to narrow and short screens. Header logos are vertically centered.
- **AM Hub records like BrainBit (data contract change).** One sample per real board frame in its own numeric stream -- `radar`, `bio`, `valves` -- with the hub's value names and the ESP's own units, plus `seq` and the hub timestamp; no 10 Hz tick, no conversion, nothing carried forward. Status, hello, gap and other hub events go verbatim to `hub_events`. The standard 1 Hz backup projection and per-card averages remain. The streams `presence`, `position`, `vitals`, `radar_detail`, `hub_status` of 1.5.x are gone, as is the v1 fallback.
- AM Hub readiness needs fresh radar and bio frames; switching it off hides all live values. The unused "reconnect delay" setting is removed. Irregular string streams can declare a capacity estimate.

## 1.5.8 - 2026-09-30

### Added

- Affect Map card with independent Field/Orbit views, four configurable region colors, and an all-gray display mode; Mood Meter remains unchanged.

## 1.5.7 - 2026-09-29

### Added

- English/German software labels for card types, participant controls, Mood Meter views, and stimulus warnings. A study now fixes the participant UI language independently of the admin language.
- Repository and source archive guards allow only the three curated example studies.
- Upload destination machine controls appear under Integrations / Uploads in the settings hub.
- Sensor tiles now use column layouts with a separate device bar, calmer graphs, and a three-position Off / On / Restart control.

### Fixed

- The translation-key test now scans actual `t()` calls and HTML keys. German UI quality checks catch uppercase ASCII umlaut spellings.
- AM Hub tests now reset frame bookkeeping between cases, preventing a false failure in the full release suite.

## 1.5.6 - 2026-09-29

### Changed

- Sensor tiles: the device list on the left, four round buttons beside it
  (search, measure contact, initialize, auto-reconnect), always visible and
  greyed out with the reason until their turn. Tiles no longer overflow onto
  their neighbour.
- BrainBit never searches or connects by itself while the study is being set
  up: *Starting …* → *Ready to connect* with the band used last time offered
  in the list. Search runs once; one band found connects at once. Poor contact
  keeps *Measure contact* as the next step, but *Initialize* stays possible.
- Auto-reconnect (on by default, switchable per sensor, also during a
  recording) restores a lost connection only once a device was connected and
  the study runs. AM Hub and radar offer the same switch.

### Fixed

- AM Hub: a dropped connection is noticed within 2 s and replaced at once;
  the status no longer shows "connected" while no data arrives. Events are
  handled the moment they arrive.
- AM Hub: a person counts as detected from any sensor (presence flag, radar
  position, heart/breathing), not only the presence flag.
- AM Hub: every hub event is recorded verbatim in the new `hub_events`
  stream, including values the plugin does not interpret; the tile lists all
  values the hub sends and warns when WiFi power saving is on at the hub.

## 1.5.5 - 2026-09-29

### Fixed

- Installs without `.git` update themselves even without the release marker,
  e.g. from GitHub's automatic "Source code" archive. The download is still
  checked against the published release's SHA-256. The install guides name
  the right download (`study-runner-source` under Assets).
- CI: the architecture structure baseline records the 1.5 features.

## 1.5.4 - 2026-09-28

First published 1.5 release. 1.5.0 to 1.5.3 were tagged but not published
(a too-long fixture path, empty changelog sections, and `.study-runner`
packages that differed by operating system); their tags were removed. Study
packages are now byte-identical on Windows, macOS and Linux.

### Added

- Every sensor tile has the same connection panel: status line with signal
  and setup ("Connected – electrode contact good · calibration done"), a
  Ready badge, the on/off switch, and the next step highlighted. Choosing a
  device connects at once. The dashboard has a study bar with Start.
- BrainBit: Search only on request, electrode contact measured again without
  reconnecting, and calibration only via Initialize while the participant
  wears the band. After each session the next person needs contact and
  Initialize again.
- Finalization can continue after quality warnings: merge, card statistics,
  CSV and manifest are still built, and the session ends degraded with the
  reason. Sessions stopped by an earlier version can be processed further.

- The cards next to the study editor are live: animations run, and the card
  being edited can be tried out right there (nothing is recorded). Every card
  is mounted the same way on the tablet and in the editor (`card-mount.js`),
  and animations share one loop that rests out of view (`card-motion.js`).
- Nextcloud and Notion offer the same settings on this computer, each with its
  own values: on/off, timeout, retry automatically, and how long to retry. The
  upload queue follows them per destination, and the plugin template starts
  with them.

### Changed

- Credentials that belong to a study (Notion key, Nextcloud password) are no
  longer offered in the settings for this computer; they stay in the study's
  settings.
- The loaded study decides which sensors run. Participant sessions no longer
  re-initialize sensors at start or stop them at the end; sensors stay
  connected between participants.
- The study-start marker waits until every sensor stream has data in the
  file, and the recording closes only after every stream passed the end
  marker.

### Fixed

- Animated Mood Meter views now also move on the tablet: their loop started
  before the card was attached to the page and stopped at once.
- The dashboard's plugin switch showed "on" after every click and always sent
  Stop; it now shows the real running state and says what happened.
- A derived BrainBit stream that started a few milliseconds after the start
  marker failed the whole session although every sample was recorded.
- A study with capitals or spaces in its name got a second, mostly empty
  folder for answer snapshots; snapshots, flush and recovery files now live in
  the study's one folder, and empty helper folders are removed.
- A failed recording start no longer creates a new session folder on every
  retry, and a participant ID edited after the start no longer moves the
  answers away from the recording.
- The BrainBit toolbar's buttons no longer wrap below the device list
  depending on the device name's length.

### Added

- Added a complete operator card catalog, localized in-app help for every
  configurable integration setting, and a sensor-free Card Gallery example
  containing every registered question type.

### Changed

- Refreshed all shipped study presets through the canonical validator/package
  writer. The portable Sensors example now lists every current sensor but
  keeps hardware disabled until the operator enables connected devices.
- The structure ratchet now measures JavaScript lines, local ES-module edges,
  cycles, and largest files across separate admin, participant, shared,
  settings, and card areas. The two large page controllers were split along
  their runtime responsibilities before recording the expanded baseline.
- Corrected live plugin framework and driver documentation to API v5.

### Removed

- Removed the unsupported `brainbit_old` fallback plugin. Studies that name it
  keep an explicit missing-plugin selection and historical recordings keep
  their original stream identity; neither is silently relabeled as `brainbit`.

## 1.4.0 - 2026-09-28

### Added

- **Settings > Data folder:** studies, results, settings, credentials, logos
  and the iPad certificate can live in a folder outside the program, for
  example on an external drive. An empty folder is set up like a clean install
  (optionally with the current data copied in), an existing data folder is
  linked as it is, and after a reinstall the last used folder is offered for
  relinking. A data folder that is not reachable stops the start with a clear
  message instead of creating an empty one.
- **Mood Meter views:** the Mood Meter card offers four views in its settings.
  Classic (as before), Blobs (breathing quadrant shapes that grow into the
  word space), Field (an Affect Grid with a shape-shifting orb) and Orbit (a
  feelings wheel with a fisheye). Field and Orbit also record the position
  (`pleasantness`, `energy`, orbit also `intensity`, each 0-1). A "?" in the
  card settings explains each view and links the papers it is based on.
  Existing studies keep the classic view.

### Changed

- Cards report non-input changes through one generic `card:changed` event, and
  a card may split its browser code into several declared modules; every one
  of them is checked for state that could leak between participants.
- Answers made of several values read as `words: calm, content · energy: 0.3`
  in the session view and in Notion.

## 1.3.2 - 2026-09-28

### Changed

- AM Hub values are recorded in the declared units: the bio board's distance
  (cm) and the radar's target speed (cm/s) are converted to mm and mm/s.
- AM Hub records the firmware's "0 = no value" as missing (NaN): heart and
  breathing rate or distance of 0, empty target slots, and the nearest target
  while no target is tracked. "Lost" counts only packets lost in the current
  session. The dashboard details show per-board link, rate, latency and losses.
- Plugin logs and state (BrainBit, emotion worker) are written next to the
  results (`runtime/<plugin>/`), no longer inside the program files that an
  update replaces. Settings that still point into the program files are
  redirected.
- Without an emotion worker the camera plugin reports "unknown" with an
  error; it no longer echoes the browser's emotion as a reading.

### Fixed

- Updates of release-archive installs work on Windows without the "long
  paths" policy (for example in a Documents folder), start the old version
  again when a file is locked during the swap, and never leave the Update
  panel stuck ("interrupted" downloads, stale "Restart now"). On macOS the
  restart keeps port, HTTPS and data-folder settings. Ctrl+C in the terminal
  updater also restores the old version.
- MR60 writes missing values as NaN instead of 0, and a restart can no longer
  leave two readers on one port.
- The release script no longer prints "fatal: Needed a single revision".

### Removed

- The fixed-key camera/emotion-worker compatibility routes
  (`/api/admin/camera/*`, `/api/admin/emotion-worker/*`,
  `/api/study/camera-monitor/start`); use the generic plugin routes.
- The undeclared OpenCV analysis modes of the camera plugin.

### Release

- Every release now updates an installed copy to a synthetic next version on
  Windows (long paths off) and both Mac architectures before it is published.

### Not announced earlier

- 1.3.1 already contained AM Hub API v2 (complete board data, valves, link
  quality and latency per board) and renamed the backup output
  `distance_m` to **`distance_mm`** (the value was always in mm).
- 1.1.0 already contained the AM Hub plugin and the BrainBit rebuild with the
  earlier implementation kept as the fallback plugin `brainbit_old`.

## 1.3.1 - 2026-09-24

### Added

- AM Hub dashboard shows movement, breathing rate and heart rate as headline
  values plus a 60-second heart/breathing-rate graph.

### Fixed

- AM Hub dashboard graphs are cleared on every start, so a new session no
  longer shows the previous participant's curves.
- German UI shows the AM Hub dashboard in German (translations were missing).
- Dashboard order: MR60 moves to the end next to BrainBit (old); Camera
  Emotion no longer shares its position with AM Hub.

## 1.3.0 - 2026-09-24

### Added

- Session detail shows a progress rail (started, ended, every save/merge/
  upload step with retry). A click on the finalization notice opens it and
  hides the notice until something new happens; the finalization modal and
  "Back to hub" button are gone.
- Every error the tablet shows, and every refused session start, reaches
  the admin as a toast and a notice that stays until clicked.
- Play warns when a sensor the study needs is not delivering data; the admin
  can start anyway.
- Notion: a "StudyRunner Sessions" database with one row per session, and
  real tables for every card's answer and each sensor's per-card statistics.

### Fixed

- No card value can carry over to the next participant (the Mood Meter and
  the participant ID used to). The tablet reloads after every session, cards
  keep state only in a shared per-session store, and plugin discovery refuses
  a card that keeps module-level state.
- Nextcloud and Notion uploads had only one second; they now get enough time,
  a timed-out upload is stopped instead of racing its retry, and status polls
  are answered while an upload runs. Notion no longer creates duplicate
  databases.
- Nextcloud "Test connection" writes, reads back and deletes a test file, so a
  read-only or file-drop share is reported as such.
- "Abort study" works whenever the run shows running, also when the session
  never started recording.
- Nextcloud is a pure backup: local raw XDF files are always kept.

- The image picker for the cover page and info cards no longer greys out PNG
  and JPG files on macOS; unsupported images (e.g. iPhone HEIC) get a clear
  message with how to convert them.

## 1.2.0 - 2026-09-23

### Added

- Release-archive installs update themselves: Update now in the admin panel
  downloads the archive, checks its SHA-256, keeps the old version in
  `.tools/update-backup/`, installs and restarts in a new window. A failed
  install restores the old version. Studies, results and settings are kept.
- Terminal update: `tools/update-macos.sh` / `tools\update-windows.cmd`
  (`--check` only reports).

### Changed

- An update ends everything after confirmation instead of refusing: a running
  session is aborted with the reason "Software update" (data kept), the study
  run ends; finalizations and uploads continue after the restart.
- Study files in `study_content/studies/` are zip packages with their images;
  plain-JSON files there are converted once at startup, originals kept in
  `studies/_backup-json/`.
- Error and warning messages use solid colors, stay longer and close on click.

### Fixed

- Restarting from the admin page (restart and update) did nothing on Flask 3,
  because it relied on a removed Werkzeug shutdown hook.

## 1.1.1 - 2026-09-23

### Added

- Info card: text for the participant to read, with an optional image to the
  left or right of it. It records no answer but keeps its own viewing interval,
  so it can serve as a recorded baseline phase.
- Optional cover page (Study settings, Participant experience), shown after the
  admin releases the study and before the Participant ID card, continued with a
  centered button. Nothing is recorded yet while it is shown.
- Exported `.study-runner` files are now packages that carry the study's
  images (zip with a SHA-256 manifest); plain JSON studies still import.
- Fonts menu next to Logos: choose or upload the heading and body font for the
  admin and participant pages of this computer.
- Session detail view: a "Completion & uploads" card lists every step after
  submit with its status and error, and retries a failed upload at any time.
- Plugin settings explain themselves: example values in every field, real field
  names, and a "(?)" button with a plain-language setup guide (Notion,
  Nextcloud; available to every plugin through manifest keys).

### Changed

- Opening an attention-required finalization acknowledges it: the floating
  notice disappears, while the session keeps its attention status.

### Fixed

- A failed upload no longer leaves a session stuck on "finalizing": the valid
  local data is completed, the failure is shown as "upload failed" with the
  real reason, and the upload can be retried later.
- Wrong credentials, an unshared Notion page, or a deleted Nextcloud share now
  fail at once with a clear message instead of retrying silently for 48 hours.
- Notion uploads and "Test connection" now use the API key stored for the
  study (before, only a computer-wide key was used). "Test connection" also
  checks access to the Notion page and reports failures instead of success.
- The BrainBit contact and battery streams, which only report before recording
  starts, no longer put short sessions into attention_required; the device
  select shows the connected band.
- The shipped demo result is listed again after the session folder layout
  change, and a new external data folder is seeded with it.

## 1.1.0 - 2026-09-23

### Added

- The main and German getting-started guides now lead non-developers through a
  release download, first installation, first start, and every later start as
  separate copyable steps.
- macOS Intel and Apple Silicon operators can create an executable
  `Study Runner.command` desktop launcher from the Settings page. Source
  shortcuts use the same checked-in daily-start script as the Terminal flow.
- Windows source installs and daily starts now use `.cmd` entry points that
  invoke the checked-in PowerShell scripts with a process-local execution-policy
  bypass. Downloaded source archives therefore work without changing the user's
  or machine's PowerShell policy.
- Windows and macOS daily-start scripts expose a non-persistent self-check used
  by the clean platform release matrix.

### Changed

- Installing no longer compiles anything and needs no administrator rights.
  Every release now carries a tested XDF recording core for Windows x64, macOS
  Intel, and macOS Apple Silicon (`study-runner-xdf-core-<platform>.zip`). The
  installers download it, check its SHA-256 against the new
  `study-runner-release.json` inside the source archive, and repeat the
  synthetic XDF test on the machine. Xcode, Visual Studio Build Tools, WinGet,
  and the python.org installer are no longer needed; macOS 13 or newer is
  enough.
- Both installers now bootstrap the same pinned uv and Python 3.12
  (`software/constraints/uv-bootstrap.txt`) into the project-local `.tools`
  folder. `--install-system-dependencies` / `-InstallSystemDependencies` are
  accepted but no longer needed. Developers who change the native sources use
  `--build-core-from-source` / `-BuildCoreFromSource`, which accepts Apple's
  Command Line Tools again.
- The release workflow builds the cores first and then installs every
  extracted archive like a user -- in a folder with spaces, on macOS with no
  compiler reachable -- before publishing.

### Fixed

- Creating a desktop shortcut from Settings now sends one request per click.
- Source releases again exclude the optional BrainBit TouchDesigner reference
  after the plugin-directory rename.
- A stale native-core CMake cache from a different Apple toolchain is rebuilt
  safely, avoiding the misleading `cstdint file not found` vendor-build failure
  while preserving `.venv` and staged/user data.
- The Windows core is linked against the static C runtime, so recording no
  longer depends on an installed Visual C++ redistributable.
- The German guide no longer tells archive users to update with `git pull`, and
  the maintainer release commands moved out of its user update section.

## 1.0.0 - 2026-09-14

Study Runner 1.0 rebuilds the application on a clearer internal architecture
(`docs/archive/architecture-1.0-umbau.md`) with no removed capability. The user- and
operator-facing highlights:

### Added

- Redesigned admin interface: a persistent header bar with a dropdown
  language switcher, and every settings field brought onto the card editor's
  visual style throughout, including the sensor/machine settings that had
  been left behind.
- Every question type (participant ID, sliders, choice, ranking, semantic
  differential, word cloud, mood meter, stimulus, and more) is now a
  self-contained, process-isolated extension with its own manifest, defaults,
  and validation, rather than a branch inside shared code. Authors can build
  new ones with the new extension SDK (`tools/extension_sdk.py`): scaffold a
  sensor, card, destination, or output extension from a working template,
  validate it, and boot-test it before writing a line of adapter code.
- Explicit session lifecycle (recording, finalizing, sealed, withdrawn, ...)
  shown in the session browser, derived from the same documents that already
  governed it, so it can never disagree with them.
- Live recording quality tracking (gaps, timing jitter, effective rate) and
  a bounded recovery journal, surfaced in the session detail view.
- Preflight checks before a recording can start: available storage, a
  plausible system clock, and every required sensor connected -- each with
  a specific, actionable message instead of a generic failure.
- A documented withdrawal workflow: withdrawing a session empties its folder
  down to a single tombstone marker, leaving no doubt that withdrawn data is
  gone rather than quietly still present.
- A git checkout can now update itself from the admin dashboard's Update
  panel -- Check, then Update now runs `git pull` and the install script and
  restarts the server, refusing safely if a study is active, the checkout
  has local changes, or it is not on the branch that receives releases.

### Changed

- Study Runner is now licensed under the MIT License instead of a
  proprietary, all-rights-reserved license. Third-party components keep
  their own separate licenses (see `THIRD_PARTY_NOTICES.md`), unaffected by
  this change.
- The Materiability heading font no longer ships in this repository: its
  rights belong to the Materiability Research Group, a third party, so it
  could not honestly ship under an MIT grant this project does not hold for
  it. Geist, already used for body text, is now the default for headings
  too. An operator with their own rights to Materiability can still add it
  locally; see `software/study_runner/apps/ui/fonts/README.md`.
- The plugin manifest contract moved to API version 5: two capability flags
  that had no real effect (`runtime_control`, and `health` being declared
  without gating anything) were retired or given real meaning, and the
  `readiness` capability was renamed to `runtime_modes` to stop colliding
  with the unrelated `readiness_requirements` capability. Every built-in
  extension migrated in the same change; a third-party extension manifest
  still declaring the old names is rejected with a message naming the
  replacement.
- Recording, plugin discovery, and delivery are reorganized into clearer
  internal packages (`docs/archive/architecture-1.0-umbau.md` has the full record).
  No recording format, HTTP route, or operator-facing behavior changed as a
  result; existing studies, settings, and sessions from 0.7.0 continue to
  work unchanged, including sessions saved before this release.

## 0.7.0 - 2026-08-11

### Changed

- The dev server's terminal output is quiet by default: only failed requests
  (4xx/5xx) print an access-log line, instead of every single request. This
  keeps the startup banner (admin URL, data folder, certificate paths) and
  the app's own rare status lines (`[CONFIG] Saved.`, plugin
  restarts/failures) readable instead of buried under the admin dashboard's
  routine status polling. Set `STUDY_RUNNER_DEBUG=1` for the full per-request
  access log.

## 0.6.0 - 2026-08-11

### Added

- Plugin API v4: every built-in plugin now runs as a supervised subprocess
  behind a single `driver.py` entry point, talking to the core over a
  line-oriented stdio protocol. The core no longer imports any plugin's
  Python module directly.
- A generic admin diagnostics console for every plugin: a guided status view
  plus a line-oriented expert console, with bounded automatic restarts, log
  rotation, and a read-only-during-study stdin gate that requires an
  explicit, recorded local unlock.
- BrainBit: startup-time validation of the pinned EmotionalMath SDK surface
  (fails closed before any device scan instead of only on the first EEG
  batch), queue-overflow protection with a counted drop metric, a measured
  (not nominal) sample-rate field on the live EEG stream, and a faster
  status refresh.
- An explicit, confirmation-gated endpoint to erase a removed plugin's
  leftover machine-config and secret sections, once an operator is sure the
  plugin is gone for good.
- A README for every built-in plugin describing its architecture and, where
  applicable, exactly which parts of its code come from an official vendor
  SDK (BrainBit NeuroSDK/EmotionalMath, Seeed's MR60BHA2 Arduino library,
  DeepFace, the Notion and OSC SDKs) versus project-original code.

### Changed

- Trial timing and persistence hardened: durable prepare-before-stimulus
  journaling with an armed emergency stop, fail-closed admin overrides that
  persist before releasing a card, idempotent stop handling, and a shared
  atomic writer with revision checks for study, hardware, and secret files.
- Every built-in plugin folder is fully removable without breaking the app,
  admin page, hardware save, or build. A `recording-plan.json` contract
  snapshot pins each session's manifests, streams, and backup projections
  for recovery and finalization.

### Fixed

- Hardware-config saves triggered from a plugin's background thread no
  longer fail with a false "changed concurrently" conflict when a section
  had simply never been persisted before.
- Study readiness no longer reports "not ready" for a missing *optional*
  plugin, which is informational, not a misconfiguration; it still blocks
  correctly when the plugin is required.
- Two admin-action tests (BrainBit device selection, Nextcloud connection
  test) were mocking server-process state while the real work runs inside
  each plugin's supervised child process; both now observe the actual
  process boundary. A related test-isolation bug that could leave a stale
  plugin-runtime singleton behind between test files is also fixed.

### Security

- The shipped `hardware_settings.json` template had accidentally picked up
  one real BrainBit headset's MAC address and serial number; reset to empty
  placeholders, and a test now guards against that happening a third time.
  Curated example studies and one demo result are tracked deliberately;
  everything else under `study_content/studies/` and `saved_results/` stays
  gitignored.
- Third-party license texts are now collected in one place (`licenses/`) in
  addition to living next to the vendored code each one covers.

## 0.5.0 - 2026-08-06

### Added

- Plugin API v3 with trusted directory discovery, manifest validation,
  capability-driven settings/actions/readiness, and failure isolation.
- A detached Python recording worker and a small audited native XDF core based
  on the pinned App-LabRecorder/XDFWriter v1.17.1 source.
- Per-plugin segmented raw XDF recording, a labelled slowest-rate backup XDF,
  bounded-memory lossless merge, raw PyXDF validation, and merge-parity checks.
- Crash-safe canonical session directories and a persistent, retryable
  finalization state machine for XDF validation, card summaries, Notion,
  Nextcloud, and guarded local-source cleanup.
- Deadline-based participant timing, durable marker/card event replay, tab
  visibility quality metadata, worker leases, and recording recovery.
- Generic finalization progress, warnings, retries, degraded confirmation, and
  artifact inspection in the Admin UI.
- Source-first Windows and macOS installation/start scripts and verified GitHub
  source-release artifacts with checksums and build metadata.

### Changed

- Camera capture and emotion analysis now share the single public plugin key
  `camera_emotion`; previous packages remain compatibility shims only.
- LAN/WLAN acquisition requires native LSL. BLE, serial, browser HTTPS, and
  local adapters publish through a host LSL bridge.
- XDF is recording infrastructure rather than a selectable UI plugin.
- Notion and Nextcloud are manifest-declared upload destinations and remain
  hidden from the sensor dashboard/settings hub.
- Canonical card statistics are derived only from validated merged XDF data;
  RAM summaries are no longer authoritative.
- The interface draws headings in Materiability and body text in Geist. Both
  now ship in source releases, so a source build renders the same as a packaged
  one. Fonts remain forbidden by default and are exempted only per folder that
  documents its terms.
- Releases use the normal Python source-server workflow. Signed/notarized app
  bundles remain a separate future distribution channel.

### Reliability and security

- Submission acknowledgement now follows an atomic local commit and is
  idempotent by `submission_id`.
- Missing/corrupt required streams, incomplete footers, merge mismatches, and
  statistics failures cannot silently become `completed`.
- Browser sensor ingest requires direct HTTPS plus heartbeat, monotonic
  sequence numbers, and source timestamps.
- Nextcloud uploads immutable artifacts checksum-first and publishes the final
  completion marker last; local raw XDFs are purged only after verified remote
  parity.

## 0.4.0

- Last release before the plugin-based canonical recording architecture.
