# Built-in plugins

Each trusted plugin shipped with Study Runner owns one directory containing:

```text
plugins/<category>/<folder>/
  manifest.json
  plugin.py
  optional adapter.py, ui/*.js, assets, and focused helpers
```

The trusted category roots are `sensors`, `cards`, `destinations`, and
`outputs`. The manifest remains authoritative for runtime capabilities and its
stable `plugin_key`; the directory category controls code ownership and import
boundaries. Discovery combines every root before checking global uniqueness.
The framework lives in `../plugin_framework/`.

The server discovers these folders automatically. There is no central import
list and no web upload or dependency installer for plugins. A malformed
manifest, duplicate plugin key, duplicate stream source ID, or incompatible
handler is reported as `invalid` by `GET /api/plugins/catalog`; it does not
stop other plugins or the server from loading.

## Plugins in this folder

| Folder | `plugin_key` | What it does |
| --- | --- | --- |
| `sensors/am_hub/` | `am_hub` | Parasite AM Hub: presence, position, movement, heart/breathing rate, valves, link quality and latency |
| `sensors/brainbit/` | `brainbit` | BrainBit EEG through the NeuroSDK CLI |
| `sensors/camera_emotion/` | `camera_emotion` | Tablet camera plus local or remote emotion worker |
| `sensors/mr60_mini_radar/` | `mini_radar` | MR60 radar vitals over ESP32-C6 BLE (or serial) |
| `destinations/notion_upload/` | `notion` | Session summaries and tables in Notion |
| `destinations/nextcloud_upload/` | `nextcloud` | Upload of finished session folders to Nextcloud |
| `outputs/osc_touchdesigner/` | `osc` | Live trial markers and signals over OSC |
| `cards/<type>/` (14 folders) | card type | Study cards: affect_map, choice, finish, info, likert, mood_meter, multi_slider, participant_id, ranking, semantic, slider, stimulus, text, word_cloud |

Sensor, destination and output plugins each have a `README.md` in their
folder; card plugins are described by their manifest and
`docs/developer-guide.md`. The operator-visible configuration and answer
contracts for every question type are in [`docs/card-catalog.md`](../../../docs/card-catalog.md).

## Versions

A plugin's `version` in `manifest.json` (`MAJOR.MINOR.PATCH`) is what each
session records and a methods section cites. Raise it with every change to the
plugin's folder (which step: `CONTRIBUTING.md` section 11), then run
`python tools/plugin_versions.py --update`. That writes
`plugin_versions.lock.json` in this folder: each plugin's version plus a
fingerprint of its files and data contract, which the tests compare. Never
edit the lock by hand.

## Manifest contract

Every current manifest uses `api_version: 5` and declares identity, plugin version,
category, config key, `runtime.entrypoint` for its process driver, UI metadata,
settings schemas, timing limits, and capabilities. Important capability names are:

The child process accepts only the operations declared by these capabilities
and `runtime.actions`/`runtime.trial_events`. It checks the matching Python
handlers before serving requests, including `get_status` when `health` is
declared. A sensor's device adapter may reconnect internally, while the shared
process host handles a crashed child process and the coordinator handles study
selection and session boundaries.

- `study_sensor`: selectable for a study, required by default when selected.
- `study_sensor.start_condition`: optional path and expected value in that
  plugin's status. BrainBit declares `connection.ready = true` because its
  preparation includes contact measurement and calibration. Other sensors do
  not inherit that condition. The server evaluates the declaration without
  recognizing the plugin's name; the XDF worker sees only stream data.
- `lsl_stream_provider`: owns stable stream/source IDs and channel metadata.
- `recording_source`: contributes native XDF segments.
- `recording_source.start_sample_policy`: `all_regular` by default; every
  declared stream above 0 Hz must deliver fresh, recorded samples before the
  start marker. `primary_only` permits late secondary streams with a warning;
  `headers_only` waits for headers only. Event-only 0 Hz sources never require
  an event exactly at a session boundary.
- `backup_projection`: declares the numeric channels the core samples on its fixed 1 Hz backup grid.
- `live_view`: declares up to four series of recorded channels the dashboard draws live (2 Hz, last 60 s).
- `acquisition_transport`: declares how samples reach LSL; browser sources also
  guarantee heartbeat, sequence, and source timestamps.
- `runtime_modes`, `health`, and `admin_actions`: lifecycle, diagnostics, and
  generic manifest-declared operator actions. `health` gates whether the
  admin coordinator polls this plugin's status. `runtime_modes` declares
  optional platform-mode support. An action marked `"surface": "configurator"`
  is called only by the plugin's own configurator; the generic study settings
  and dashboard panels never offer it as a raw form. A panel action never asks
  for a payload field a study setting, the credential or a machine setting
  already answers - the plugin applies its machine setting (a timeout, say)
  itself.
- `readiness_requirements`: distinct from `runtime_modes` above - what a study
  needs before this plugin can actually deliver results:
  `requires_secret`, `requires_settings` (a list; any one satisfies it), and
  `requires_machine_enabled`. `runtime_core/studies/study_readiness_service.py` checks every
  plugin that declares this the same way, before the study can start.
- `participant_actions` and `participant_ingest`: closed allow-lists for
  participant lifecycle commands and browser payloads through generic routes.
- `machine_settings`, `study_settings`, and `card_actions`: generic UI schemas.
  A study field marked `read_only` or `advanced` leaves the main area of a
  destination's block for its closed "Technical details". A `string` or `url`
  field with a `format` is checked by the plugin's `validate_study_setting`
  when the study is saved.
- `upload_destination`: a background publication target with a required
  `publish_destination(context, payload)` handler.
- `connection`: every `study_sensor` declares it. It names the device and the
  signal for the shared connection panel (`device_noun_key`,
  `signal_label_key`, `setup_label_key`) and whether the setup belongs to one
  participant (`setup_per_participant`). See "Connection pattern" below.
- `credentials`: declares the one secret field a plugin needs
  (`config_field`, `env_var`, `per_study`) so `context.secret(plugin_key)`
  resolves it env > per-study > machine > legacy config. The manifest never
  carries the value - see `plugin_framework/plugin_secrets.py`.

Only manifests in the application package are trusted. The entry point is
resolved inside its own folder after schema and duplicate checks pass.

Optional rich UI stays inside the plugin. Declare entry modules as
`ui.extensions.dashboard`, `ui.extensions.participant` and/or
`ui.extensions.study_settings` (a configurator, opened by "Configure…"; it may
also export `summarize(settings)` for the one-line target summary); declare
relative JavaScript and CSS files under `ui.assets` - every file an extension
module imports, or the browser cannot load the module (a test checks this).
Extension entries remain JavaScript modules. Discovery rejects absolute, traversing, unsupported, or missing paths,
and the asset endpoint serves only exact manifest entries. A failed extension
is isolated and the generic UI remains usable. `ui.timeline.lane_aliases` and
`preferred_channels` control completed-
session lanes without adding sensor keys to the renderer.

Admin actions use a closed `payload_schema`. Dynamic buttons may map cached
status candidates with `instances.status_paths`, `payload_map`, and
`label_fields`. The server rejects unknown fields and invalid types before it
calls `run_admin_action(context, action_key, payload)`. An action may take one
connection `role` (`select`, `scan`, `measure_signal`, `initialize`,
`auto_reconnect`); the shared connection panel then draws it and the generic
action list skips it. Each role may appear once, and `select` needs
`instances.presentation: select`. `auto_reconnect` receives
`{"enabled": true|false}` and is the only role usable while a participant
session records.

## Connection pattern

Every sensor is prepared, shown and recorded the same way:

- **Facts from the plugin.** Its status carries `running` (acquisition runs
  right now) and a `connection` block: `phase` (`off`, `starting`, `idle`,
  `searching`, `selection_required`, `connecting`, `connected`,
  `reconnecting`, `failed`), an optional `detail` for `idle`/`failed`
  (`not_found`, `connection_lost`, …), `device`, `candidates` (a remembered
  device carries `note: "last_used"`), `signal.state` (`good`, `fair`,
  `poor`, `measuring`, `stale`, `unknown`), `setup.state` (`needed`,
  `running`, `done`, `stalled`, `not_needed`), `streaming` and, with the
  `auto_reconnect` role, `auto_reconnect: {enabled, had_connection}`.
- **Decisions in the core.** `plugin_framework/sensor_connection.py` adds
  `ready`, `next_step` and `auto_reconnect.active` for every sensor alike.
  The dashboard panel and the Start check use exactly these. A plugin without
  a block gets one derived from its plain `status`.
- **Nothing connects behind the operator's back.** Switched on, a sensor the
  operator connects by hand (`scan`/`select`) waits in `idle` ("Ready to
  connect") and offers the device used last time. It searches once per
  Search and tries a chosen device once. Only when a device was connected
  and the study runs (`PluginContext.study_running`) does auto-reconnect
  restore a lost connection by itself; plugins ask
  `auto_reconnect_active()` so the plugin and the dashboard never disagree.
  Sensors without a manual connection (hub, radar) reconnect whenever their
  switch is on.
- **Lifecycle.** Loading a study starts the sensors it needs and stops the
  rest. A participant session neither re-initializes nor stops a running
  sensor; the core also never re-sends `initialize` for an unchanged
  configuration. When a session's recording closes, plugins that list
  `session_end` in `runtime.trial_events` receive `on_session_end` and reset
  what belonged to that person (for example a calibration) while acquisition
  continues.

Browser ingest manifests declare acceptable source timestamp fields and the
route also requires a sequence number. Upload destinations declare their queue
key, legacy load-only aliases, and the policies `requires_valid_result` and
`purge_verified_sources`. All destinations run after local finalization, and
independent destination jobs can run concurrently. Only one installed destination may grant verified
source purge. Adding a destination needs no upload-runtime or finalization key
change: discovery registers its handler and persists a `publish_<plugin-key>`
step. A destination that needs a secret declares `credentials`, and a
connection to test declares a `test_connection` admin action - both existing
capabilities, not a special case for uploads. The value itself is never in
the manifest, machine settings, or exported study data.

Set `lifecycle.reinitialize_on_disable: true` only when disabling a plugin must
rebuild its inert adapter state. Registry lifecycle behavior is read from this
flag and never from a hard-coded plugin-key set.

## Adding a sensor

`python tools/plugin_sdk.py new sensors <plugin_key>` writes and checks
the folder structure above for you; steps 2-5 below are the sensor-specific
work it cannot do.

1. Choose a stable lowercase `plugin_key`.
2. Define LSL streams with unique, stable `source_id` values, nominal rates,
   clock domains, channel types, labels, and units.
3. Add `study_sensor`, `lsl_stream_provider`, `recording_source`, a valid
   `backup_projection` and the `live_view` series when the sensor
   participates in recording, and publish every sample through
   `SensorStreams` (the sensor data contract,
   `docs/plugin-recording-architecture.md`).
4. Implement `PLUGIN` with the handlers promised by the manifest, including
   `running` and the `connection` block in the status (the template shows
   both).
5. Add a fixture test proving discovery, settings, readiness, recording,
   backup projection, and card statistics without a core registry change.

The former aggregate `plugin_manifests.json` is intentionally gone. Discovery
reads only per-folder manifests. A top-level package without a manifest is
reported as invalid; an intentional internal helper or compatibility shim must
carry an explicit `.pluginignore` marker and is never imported by discovery.

`camera_emotion/` is the single camera plugin and owns its internal
`worker/`. The old `tablet_camera_emotion` and `local_emotion_worker` packages
no longer exist; only the emotion worker's model cache from a v2 install
(`runtime/local_emotion_worker/`) is still reused so offline PCs keep their
models.
