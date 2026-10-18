# Operator Guide

Study Runner is a local Python server with an admin page, a participant page,
and trusted built-in plugins for lab integrations. After the one-time source
installation, start it with `tools/start-windows.cmd` or
`bash tools/start-macos.sh`; no virtual-environment activation is needed.

## Main Parts

- `software/server.py`: starts the local server.
- `software/study_runner/apps/server/`: Flask app factory, routes, and server startup.
- `software/study_runner/runtime_core/`: study, settings, and delivery services.
- `software/study_runner/data_core/host/`: recording orchestration and validation.
- `software/study_runner/plugins/`: manifest-driven (API v5) plugins —
  AM Hub, BrainBit, MR60 mini-radar,
  camera/emotion, Notion, Nextcloud, OSC/TouchDesigner, and the study cards
  (full list in `software/study_runner/plugins/README.md`).
- `software/study_runner/data_core/worker/`: detached Python recording worker.
- `software/recording_worker/native/`: small native XDF-core source.
- `software/study_runner/apps/ui/`: browser pages, styles, scripts, cards, and
  locales.
- [Study Card Catalog](card-catalog.md): purpose, setup, and recorded answer
  shape for every card available in the editor.
- `software/study_content/settings/`: active study, machine settings, and local
  secrets.
- `software/study_content/studies/`: saved study presets.
- `software/saved_results/`: canonical participant sessions.
- `docs/plugin-recording-architecture.md`: detailed recording/data contract.

## First Install And Daily Start

Install once per computer by following [Install and start](../README.md#install-and-start)
in the README: it has the download, the platform installer, and the macOS
desktop shortcut for every supported platform. Re-running the installer
repairs or updates the environment without deleting study content or results.
Dependency pinning is described in
[Release and Update](release-and-update.md#python-dependency-constraints).

After that, start the server with one command:

```powershell
.\tools\start-windows.cmd
```

```bash
bash tools/start-macos.sh
```

Windows x64 and macOS Intel/Apple Silicon are the supported recording
platforms. If the core is absent or stale, non-recording studies still run; a
study requiring recording is blocked with the setup hint; running the
platform installer again installs the tested core. Advanced developers can
invoke `python tools/setup_recording_worker.py` directly inside the active
environment.

## How A Study Run Works

The study editor's **Participant experience** settings choose English or German
for software labels on the participant tablet and its preview. This choice is
saved in the study package; an older study defaults to English. The admin's
language switch affects only the admin page. Question prompts, answer options,
cover text, and custom Mood Meter words stay exactly as the study author wrote
them. Write those in the language intended for participants.
The same settings can show the progress bar at the top and choose whether
question cards have a framed surface or sit directly on the study background.
Navigation stays at the bottom of the participant screen in both a run and
the non-recording preview; older studies keep framed cards by default.

1. Start the Python server and open `/admin`.
2. Load or edit the study. The loaded study decides which sensors run: the
   ones it needs are started, all others are stopped. A study without
   sensors runs none.
3. Open the participant URL on the assigned tablet over trusted HTTPS. It
   waits with the study name and logo.
4. Prepare the sensors on the dashboard. Every sensor tile shows the same
   connection panel: a status line (for example "Connected – electrode contact
   good · calibration done"), a green **Ready** once it can be recorded, the
   on/off switch, and its steps. The highlighted button is always the next
   step. For BrainBit:
   - **Search** finds headbands. Exactly one is connected automatically;
     with several, choose one in the list, which connects at once. A known
     band reconnects by itself after a restart.
   - Electrode contact is measured automatically when the band connects.
     **Measure contact** repeats it without reconnecting.
   - **Initialize** calibrates the attention and relaxation values for the
     person wearing the band (about 6 s, sitting still, eyes open).
   Data streams live to the preview but is not recorded yet.
5. Press **Start study** in the dashboard's study bar (or in the hub). It is
   highlighted once every needed sensor is ready and the tablet is connected;
   otherwise the bar names what is still missing.
6. The tablet shows the info page, if the study has one, then the
   participant ID. Submitting the ID starts the recording; nothing is
   reconnected or re-initialized then. The study-start marker is written only
   once every sensor stream has data in the file.
7. The detached worker records each plugin to its own XDF, plus the slowest-grid
   QC backup and hidden marker/clock streams.
7. Browser timers use real monotonic deadlines. A hidden tab does not pause a
   stimulus.
8. On Submit, the participant submission is committed locally. Only then does
   the completion page appear. The recording closes once every stream has data
   past the end marker. The sensors keep running for the next participant;
   BrainBit falls back to "measure contact and initialize" for the new person.
9. The session detail's progress rail shows freeze, source validation, merge,
   merge parity, card statistics, manifest, Notion, Nextcloud, and guarded
   purge, with retries; a notice appears when something needs attention.
10. A valid run ends with `COMPLETE.json`; a quality problem uses
    `ATTENTION_REQUIRED.json`.

## Sharing A Study (`.study-runner` Files)

**Download** next to a study in the hub saves a `.study-runner` file,
**Import study** reads one. Since 1.1.1 this file is a small zip package: `study.json` (the study),
`assets/` (every image used by info cards or the cover page) and
`manifest.json` (a SHA-256 per file, checked on import). The extension stays
`.study-runner`.

- **Old files still load:** a plain-JSON `.study-runner` or `.json` file from an
  earlier version imports unchanged.
- **Not the other way round:** Study Runner 1.1.0 and older cannot read the new
  package. Update the receiving computer first.
- Since 1.2.0 the files in `software/study_content/studies/` are packages too,
  including the bundled examples. Plain-JSON files found there are converted
  once at startup; the originals are kept in `studies/_backup-json/`.
- Upload credentials (Notion key, Nextcloud password) are never part of the
  file. Enter them again on the receiving computer.

## Updating

Update panel, **Update now** (or `tools/update-macos.sh` /
`tools\update-windows.cmd` while Study Runner is stopped). An update ends
everything first: a running session is aborted with the reason "Software
update" (its data is kept) and the study run ends. Finalizations and uploads
continue after the restart. Studies, results and settings are never touched.
Details: [Release and Update](release-and-update.md#updating-an-installation).

## Data Folder (External Drive)

Studies, results, settings, credentials, logos and the iPad certificate share
one data folder, by default inside the program folder. **Settings > Data
folder** puts it elsewhere, for example on an external drive:

- An empty folder is set up like a clean install (with a new iPad certificate
  that the tablets must trust once). "Bring the current data along" copies
  everything first; the old copy stays until you delete it.
- An existing Study Runner data folder is linked as it is. After a reinstall
  the page offers the last used folder: one click and everything is back.
- Study Runner restarts after every change; it refuses during a session or a
  running study run.
- If the drive is not connected, Study Runner does not start and names the
  missing folder. Connect it and start again, or delete `data-folder.json` in
  the install folder to use the program folder.

## Uploads That Fail

A failed Notion or Nextcloud upload never blocks a session: the data is saved
locally and the session is marked **upload failed**. Open the session and use
the **Completion & uploads** card to read the reason and **Try again** after
fixing it. Wrong credentials or an unshared page fail at once; network problems
are retried automatically. The **(?)** button in each plugin's settings
explains every field.

A finished upload can be sent again: in the session's **Completion & uploads**
card, every done upload step offers **Upload again**. It uses the study's
current settings, so after changing what a study sends to Notion (Study
settings > Notion > **Configure…**), earlier sessions can be brought up to
date one by one. The destination updates what that session sent before
instead of adding a copy.

## Plugin-Driven UI

The server discovers trusted integration folders and validates their manifests
before import. Sensor choices, settings, readiness, status, and actions come
from the public plugin catalog. Notion/Nextcloud may appear only in destination
settings and finalization. XDF, markers, and clock diagnostics are
infrastructure and have no separate user menu.

Camera capture and emotion analysis are one `camera_emotion` plugin. Its local
and remote workers are operating modes, not additional plugins.

Every sensor tile on the dashboard shows the same live graphs of what is
being recorded (the mean of every 0.5 s over the last minute), right below
its connection panel. The graphs are a view only; the recording itself keeps
every sample.

To arrange the tiles, drag a tile by its title bar into the other column or
to another place; with the keyboard, focus the title and use the arrow keys.
This computer remembers the order for every browser and after a restart
(`settings/dashboard_layout.local.json`). "Reset tile order" in the
integration controls goes back to the default order. On a narrow window the
tiles stand in one column and cannot be dragged.

## Camera And HTTPS

The tablet camera requires HTTPS and trust in the local Study Runner Root CA.
Camera frames can update the admin monitor before a run; raw frames are not
stored as session video. During a selected run, derived emotion values are sent
through the plugin's host LSL bridge.

The local DeepFace worker starts and restarts behind `camera_emotion` on Windows
x64 and macOS Apple Silicon. Current TensorFlow/tf-keras wheels do not provide a
CPython 3.12 macOS Intel build; on Intel, configure `remote_worker`. This does
not limit the server or XDF recording. Repair or dependency actions are exposed
only when declared by the plugin manifest.

Useful diagnostic server flags remain:

- `--emotion-worker-self-test --json`
- `--emotion-worker`
- `--apply-update`

## If Something Goes Wrong

- A missing required plugin blocks Start and shows concrete readiness details.
- A sensor disconnect triggers reconnect attempts and records a visible gap;
  it does not stop the participant timer.
- A lost tablet or Flask process starts a 15-minute recording lease. The worker
  continues and can be reattached after a Flask restart.
- A worker restart creates a new XDF segment instead of modifying an existing
  fragment.
- If the local submission commit fails, the participant completion page is not
  shown and the current browser/snapshot data stays available.
- Source, merge, or statistics failure becomes `attention_required`, never
  silent completion.
- Notion and Nextcloud failures are independent, persistent, retryable jobs and
  do not affect the participant screen.

Open the finalization details to inspect warnings and artifacts. Retry the
specific failed step first. There are two kinds of findings:

- **Quality warnings** (for example a stream that starts a few milliseconds
  late, or a sensor that was not calibrated): the data is readable. Choose
  **Continue with warning** and give a reason. Merge, card statistics, CSV and
  manifest are still created, and the session ends `completed_degraded` with
  the reason stored.
- **Blocking problems** (for example an unreadable file): nothing can be
  derived. **Confirm degraded completion** with a written reason closes the
  session with the raw data as it is.

Sessions that an earlier version stopped right after the confirmation show
**Continue processing** when their data only had quality warnings.

## Data And Purge Safety

Each repeated participant run receives a new UTC/session-ID folder. Nextcloud
mirrors only that folder and uploads the completion marker last. Raw source XDFs
are removed locally only after valid merge parity and SHA-256-verified remote
copies. Without verified Nextcloud publication, raw sources remain local.

The backup XDF is deliberately reduced to the slowest declared rate and is a
recovery/QC artifact, not a substitute for native raw streams.

## Where To Change Common Things

- Change a study in Admin or edit `study_config.json`.
- Change plugin machine settings through the manifest-generated settings UI or
  `hardware_settings.json`.
- Add a card type as a plugin folder below
  `software/study_runner/plugins/cards/<type>/` (manifest plus renderer);
  discovery picks it up, there is no card index to edit.
- Add a plugin package with `manifest.json` and `plugin.py`; no central sensor
  import list is needed.
- Install or repair the complete source environment with the platform script in
  `tools/`; build only the recording core with
  `python tools/setup_recording_worker.py`.
- See `docs/release-and-update.md` for source updates and release acceptance.
