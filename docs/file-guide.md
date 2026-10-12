# File guide - what every source file does

One line per source file: path, purpose, and whether a non-coder can
edit it safely.

**When you add a `.py` or `.js` file, add its line here in the same commit.**
This is not a convention you can forget: `software/tests/test_file_guide.py`
fails the moment a source file has no entry, and it names the missing paths.

Edit-safety legend:
- **yes** - text/config-like, safe to adjust with care
- **careful** - logic; small changes OK if you run the tests afterwards
- **no** - security- or data-critical; change only with review

## Entry points

| File | Purpose | Edit? |
|---|---|---|
| `software/server.py` | Start Study Runner from source (`python server.py`); also dispatches the detached `--recording-worker`, emotion-worker, BrainBit CLI, and updater modes | careful |
| `software/study_runner/app_server.py` | Stable server import/module entrypoint delegating to `apps/server/application.py` | careful |
| `software/study_runner/apps/server/application.py` | Wires up the Flask server: port check, HTTPS, startup banner, browser open | careful |
| `software/study_runner/version.py` | The single version number of the app | yes |
| `software/study_runner/self_check.py` | `server.py --self-check`: checks the app without starting HTTP or hardware; packaging CI can also name harmless plugin/card keys to prove a real child RPC and declared card asset work in the bundle | careful |
| `tools/study_runner_manager.py` | Standalone Install & Repair Wizard (downloads, verifies, installs releases) | no |
| `tools/setup_recording_worker.py` | Native XDF core helper: installs and locally tests a downloaded prebuilt core, or builds the current platform (CTest plus Python/PyXDF smoke) and packs it as a release asset | no |
| `tools/make_timeline_fixture.py` | Writes a synthetic completed session with a real multi-stream XDF, so the timeline can be seen without recording hardware | no |
| `tools/measure_structure.py` | Python/JavaScript structure ratchet: cross-area import edges, cycles, lines and largest files (including distinct frontend areas), checked against `tools/structure_baseline.json` | careful |
| `tools/refresh_example_presets.py` | Rebuilds all shipped example study packages through the canonical validator/writer and makes the Basic example active | careful |
| `tools/plugin_sdk.py` | Plugin SDK: scaffold (`new`), validate, and boot-test (`check-runtime`) a new plugin outside `plugins/`, plus a generated manifest reference (`schema`) — see `tools/plugin_templates/` | careful |
| `tools/plugin_versions.py` | Keeps plugin versions citable: `--check` fails when a plugin changed without a new version (or its data contract without a MAJOR step), `--update` records the raised versions in `software/study_runner/plugins/plugin_versions.lock.json`, `--list` prints them | careful |
| `tools/synthetic_lsl_source.py` | Pushes fake-but-plausible LSL samples for a sensor plugin's own declared stream, so it can be tested without real hardware | careful |
| `tools/install-windows.cmd` / `tools/start-windows.cmd` | Primary Windows entry points; quote their adjacent scripts, use a process-local PowerShell execution-policy bypass, forward arguments, and preserve exit codes | careful |
| `tools/install-windows.ps1` / `tools/install-macos.sh` | Idempotent first-install/repair implementations: pinned uv and Python in `.tools`, `.venv`, Python requirements, and the downloaded, locally re-tested XDF core; no admin rights or compiler | careful |
| `tools/start-windows.ps1` / `tools/start-macos.sh` | Daily source-server implementations that use the repository `.venv` directly and expose a non-persistent self-check | careful |
| `tools/update_study_runner.py` / `tools/update-macos.sh` / `tools/update-windows.cmd` | Terminal update while Study Runner is stopped: release archive (download, SHA-256 check, swap program files, install, rollback on failure) or git clone (`git pull --ff-only` + install); `--check` only reports | careful |
| `software/constraints/py312-*.txt` | Bounded release-tested Python 3.12 compatibility pins: bootstrap, common runtime, platform-selected local emotion stack, and CMake for source builds of the core | careful |
| `software/constraints/uv-bootstrap.txt` | The uv release (with per-platform SHA-256) and exact Python 3.12 version both installers download into `.tools` | careful |

## Contracts (`software/study_runner/contracts/`)

| File | Purpose | Edit? |
|---|---|---|
| `software/study_runner/contracts/manifest.py` | Pure manifest/command-payload validation and API versions; imports only the standard library, shared by recorder and plugin framework | no |
| `software/study_runner/contracts/plugin_api.py` | Dependency-light runtime context and plugin protocol shared by server, framework, and plugin subprocesses | no |
| `software/study_runner/contracts/stream_contract.py` | Turns one manifest-declared stream contract into `desc/study_runner` XDF header fields; every LSL-producing module calls it before creating its outlet | no |
| `software/study_runner/contracts/sensor_contract.py` | The sensor data contract in one place: the fixed 1 Hz backup, the 2 Hz / 60 s live view, and where each stream's timestamps come from (`timing.timestamp_source`, reversible `correction_channel`) | careful |
| `software/study_runner/contracts/session_lifecycle.py` | The one explicit session state (`IDLE`/`PREFLIGHT`/`RECORDING`/`FINALIZING`/`SEALED`/`WITHDRAWN`/`FAILED`), derived from the recording plan and finalization job that own the detail, plus the allowed transitions | no |
| `software/study_runner/contracts/recording_checkpoint.py` | The confirmed-prefix contract: how far a segment is *known* to be on disk, written by the worker after each durable flush and read by the host during recovery to name the unconfirmed tail | careful |
| `software/study_runner/contracts/quality_journal.py` | Streaming quality counters (gaps, timestamp regressions, jitter, effective rate), wall-clock jump detection, ingest-backlog monitoring against the transport's bounded buffer, and the versioned quality profile whose thresholds turn a number into an event | careful |
| `software/study_runner/contracts/card_validation_primitives.py` | `CardValidationError` and the small `_normalize_*`/`_require_*` input primitives every card plugin's driver imports; card semantics themselves live in the plugins, not here | no |
| `software/study_runner/contracts/media_content.py` | Shared field rules (title, formatted text, image id, layout) for read-only participant content: the info card and the study cover page | no |
| `software/study_runner/contracts/card_options.py` | Shared options-list normalization for choice/single/ranking cards | no |

## Apps server - HTTP routes (`software/study_runner/apps/server/routes/`)

| File | Purpose | Edit? |
|---|---|---|
| `routes/__init__.py` | Registers all route groups; ValidationError -> 400 handler | careful |
| `routes/pages.py` | Serves the two pages: `/` (participant) and `/admin`. The audit text moved into the settings shell, so there is no separate `/audit` page any more | careful |
| `routes/study.py` | Everything the tablet calls: config, sessions, triggers, heartbeat, clock sync | careful |
| `routes/results.py` | Saving results - crash-safe, with recovery files and partial snapshots | no |
| `routes/admin.py` | Operator endpoints: health, studies list/activate/delete, status, restart, data folder (show, choose, link, set up, reset) | careful |
| `routes/sensors.py` | Hardware config and generic plugin start/stop/restart (no plugin is named here) | careful |
| `routes/update.py` | In-app updater endpoints (check/download/install) | no |
| `routes/sessions.py` | Read-only completed-session list, detail, and timeline-signal APIs | careful |
| `routes/certificate.py` | Certificate status plus guarded root-CA export/import endpoints | no |
| `routes/branding.py` | Uploads, removes, and serves the operator's group and funder logos | no |
| `routes/study_assets.py` | Uploads and serves study images; exports and imports `.study-runner` study packages | no |
| `routes/uploads.py` | Background-upload status/retry and validated result-folder opening | no |
| `routes/recovery.py` | Lists crash-orphaned sessions and finalizes or discards them | no |
| `routes/finalization.py` | Read-only status plus guarded retry, degraded-confirmation, and exact-session-folder actions for durable finalization jobs | no |
| `routes/plugins.py` | Serves the manifest-derived plugin catalog used by generic admin UI | careful |
| `routes/helpers.py` | Shared request helpers: runtime config, sessions, sensor runtime | careful |

## Clock core (`software/study_runner/clock_core/`)

The one place that knows how clocks relate; see `docs/recording-finalization-workflow.md`, "Clock core and responsibilities".

| File | Purpose | Edit? |
|---|---|---|
| `software/study_runner/clock_core/__init__.py` | Orientation: contract, producer, assessment | yes |
| `software/study_runner/clock_core/contract.py` | Who owns which clock, the alignment states (`corrected`/`local`/`uncertain`), the coverage bases, and the named timing tolerances | no |
| `software/study_runner/clock_core/producer.py` | Turns source times into LSL times: re-anchoring callback timelines after a pause, a plugin's `perf_counter` source clock and its mapping, and the marker outlet's wall-to-LSL mapping read on a tick edge | no |
| `software/study_runner/clock_core/assessment.py` | Compares recorded times by one set of rules for the live start/end barriers and the offline validation, and builds `meta/clock-report.json` | no |
| `software/study_runner/clock_core/round_trip.py` | Estimates another computer's clock from ping round trips (the NTP method): trusts the fastest recent round trip, follows drift and accepts a clock step only after three agreeing exchanges; used by the AM Hub's hub-clock pings | careful |

## Runtime and shared services (`software/study_runner/runtime_core/`, `software/study_runner/shared/`)

| File | Purpose | Edit? |
|---|---|---|
| `software/study_runner/runtime_core/studies/__init__.py` | One-paragraph orientation for the folder: a study, its run, and everything that run produces | yes |
| `software/study_runner/shared/atomic_io.py` | Crash-safe JSON writes (temp file + replace) for all study data | no |
| `software/study_runner/shared/software_root.py` | Locates `software/` by marker (`server.py` + `study_runner/`) instead of counting parent levels | no |
| `software/study_runner/shared/runtime_mode.py` | `is_frozen`/`get_app_mode`/`get_project_base_dir` shared by plugins, plugin framework, and runtime settings | no |
| `software/study_runner/shared/study_identifiers.py` | `normalize_study_id` -- the one place a study's stable filename/credential key gets computed; re-exported from `study_config_service.py` | no |
| `software/study_runner/shared/dependency_utils.py` | `ensure_requirements` -- shared by `data_core/host` and plugin framework without creating an area dependency | no |
| `software/study_runner/contracts/participant_fields.py` | Canonical participant-field order, defaults, and configurable options shared by study validation, the participant-id card, and destination plugins | no |
| `software/study_runner/shared/filename_sanitizer.py` | `sanitize_identifier_for_filename` -- needed by both `runtime_core/studies` (results/recovery) and `data_core/host` (sensor flush), so it belongs to neither | no |
| `software/study_runner/data_core/contract/recording_errors.py` | Typed recording/worker errors shared across the host/worker boundary | no |
| `software/study_runner/data_core/contract/worker_protocol.py` | The authenticated, idempotent loopback wire protocol between host and worker | careful |
| `software/study_runner/data_core/contract/backup_projection.py` | Slowest-grid backup projection model (`BackupProjection`/`BackupSampler`) shared by host and worker | careful |
| `software/study_runner/data_core/contract/recording_lease.py` | The persistent 15-minute recording lease shared by host and worker | careful |
| `software/study_runner/data_core/contract/native_core_probe.py` | `CoreProbe`/`probe_core_library` lets the host validate a build without depending on the worker | careful |
| `software/study_runner/data_core/contract/lsl_dependency.py` | `require_pylsl`/`lsl_version_info` shared by host preflight and worker inlet setup | no |
| `software/study_runner/shared/system_clock_probe.py` | Package 5b preflight: system-clock plausibility bounds plus a per-platform time-sync-service check; no network access | careful |
| `software/study_runner/runtime_core/settings/dashboard_layout_service.py` | The operator's dashboard tile order for this computer (`settings/dashboard_layout.local.json`): validate, store, reset | no |
| `software/study_runner/data_core/host/recording_capacity.py` | Package 5b preflight: predicts required storage from the negotiated recording contract's declared stream rates (never disk throughput) against the study's planned duration | careful |
| `software/study_runner/runtime_core/studies/validation.py` | Validates study configs and submitted results (has a TOC docstring) | careful |
| `software/study_runner/runtime_core/studies/card_extension_bridge.py` | Package 5g.B5: dispatches `card_defaults`/`card_normalize`/`card_validate_answer` to the owning card plugin's process, resolves host-supplied data (e.g. stimulus's `plugin_actions`), and turns a card process fault into `CardExtensionUnavailableError` (503) vs. an invalid answer into `CardValidationError` (400) | careful |
| `software/study_runner/runtime_core/studies/results_service.py` | Builds answer details, slices biosignals per card, writes result files | no |
| `software/study_runner/runtime_core/studies/sessions_index_service.py` | Scans completed results and builds bounded timeline envelopes | careful |
| `software/study_runner/runtime_core/studies/session_index_csv.py` | Rebuilds each study's compact, atomic sessions-index.csv from completed session folders | careful |
| `software/study_runner/runtime_core/studies/session_quality_summary.py` | Reduces quality.jsonl (5c) and its unconfirmed-tail entries (5h) to a UI-sized health level and structured findings | careful |
| `software/study_runner/runtime_core/studies/session_store.py` | Persistent, rehydrating registry of active tablet study sessions | no |
| `software/study_runner/runtime_core/studies/sensor_session_ownership.py` | Which session or operator setup owns each sensor's per-participant state, so a late `session_end` never resets the next person, and a timed-out one blocks the sensor until the plugin confirms it | no |
| `software/study_runner/data_core/host/sensor_flush_service.py` | Periodic background export of live sensor history for crash recovery | no |
| `software/study_runner/data_core/host/sensor_coordinator_service.py` | Central plugin lifecycle/status wrapper with manifest, backpressure, and timing diagnostics | careful |
| `software/study_runner/data_core/host/clock_sync_service.py` | Bounded tablet/worker offset and RTT histories for timing diagnostics | careful |
| `software/study_runner/runtime_core/studies/recovery_service.py` | Finds crash-orphaned sessions and finalizes or discards them | no |
| `software/study_runner/runtime_core/settings/update_service.py` | In-app updater: manifest fetch, signature check, download, staging | no |
| `software/study_runner/runtime_core/settings/ssl_service.py` | Local HTTPS certificate authority for tablet camera access | no |
| `software/study_runner/runtime_core/delivery/certificate_download_service.py` | Plain-HTTP, one-file bootstrap download for the local root CA | careful |
| `software/study_runner/runtime_core/delivery/certificate_transfer_service.py` | Validates, exports, and transactionally imports the reusable local root CA | no |
| `software/study_runner/runtime_core/settings/branding_service.py` | Validates logo uploads and resolves a slot to a stored file, never to a caller's path | no |
| `software/study_runner/runtime_core/settings/font_service.py` | Heading/body font choice per computer: built-in stacks or one uploaded font per slot, checked by file signature and resolved only through its manifest | no |
| `software/study_runner/runtime_core/studies/study_assets_service.py` | Content-addressed study images (id = SHA-256 + detected type), with type/size checks and the rule that a saved study may only point at stored images | no |
| `software/study_runner/runtime_core/studies/study_package_service.py` | Builds and verifies portable study packages (zip: study.json, manifest with SHA-256, assets/); still reads plain-JSON studies | careful |
| `software/study_runner/runtime_core/delivery/withdrawal_service.py` | Consent withdrawal: stops writers, cancels pending uploads, deletes the session tree and the journal copies outside it, then leaves a `WITHDRAWN.json` tombstone; its ledger lives outside the folder it empties so an interrupted run can resume | dangerous |
| `software/study_runner/runtime_core/delivery/upload_jobs_service.py` | Persistent upload journal, crash replay, backoff worker, and retry state | no |
| `software/study_runner/runtime_core/delivery/upload_runtime.py` | Registers manifest-declared destination plugin handlers with persistent upload jobs | no |
| `software/study_runner/runtime_core/settings/folder_open_service.py` | Validates and opens result folders on Windows or macOS | no |
| `software/study_runner/runtime_core/settings/data_folder.py` | Where studies, results, settings and the certificate live: `<install>/data-folder.json` > program folder (`STUDY_RUNNER_DATA_DIR` by hand wins); folder marker, check before linking, missing drive stops the start, remembered folders for relinking, native folder dialog | careful |
| `software/study_runner/runtime_core/settings/runtime_config.py` | Paths, ports, app mode, data-folder resolution | careful |
| `software/study_runner/runtime_core/studies/study_config_service.py` | Load/save the active study and the saved-studies folder | careful |
| `software/study_runner/runtime_core/studies/study_run_state_service.py` | Persists the operator-controlled loaded/running/completed run state | careful |
| `software/study_runner/runtime_core/studies/study_run_abort.py` | Admin abort: freezes a recording session, or closes a running run that never started recording | careful |
| `software/study_runner/runtime_core/studies/operator_notices.py` | Persisted notices for the admin: every tablet error and every refused session start, until clicked | careful |
| `software/study_runner/runtime_core/studies/live_sensor_readiness.py` | Live check on Play: which selected study sensors are not delivering data right now | careful |
| `software/study_runner/data_core/host/study_sensor_runtime.py` | Which sensors are effectively on (study settings + overrides) | careful |
| `software/study_runner/runtime_core/studies/trial_service.py` | Sends stimulus start/stop to the card's selected actuator plugins and every marker to the two built-in recording sources | careful |
| `software/study_runner/runtime_core/studies/study_client_service.py` | Tablet heartbeat bookkeeping | careful |
| `software/study_runner/runtime_core/settings/secrets_service.py` | Local-secrets file I/O plus manifest-driven hardware-config redaction | no |
| `software/study_runner/plugin_framework/plugin_secrets.py` | Per-study credential overrides and secret resolution, never written into the exported study | no |
| `software/study_runner/runtime_core/studies/study_readiness_service.py` | Pre-run check: what would stop the loaded study from delivering results | careful |
| `software/study_runner/runtime_core/settings/plugin_settings_service.py` | Manifest-driven machine settings: schema, validation, targeted deep-merge writes | no |
| `software/study_runner/runtime_core/settings/hardware_settings_service.py` | Saves hardware_settings.json | careful |
| `software/study_runner/runtime_core/settings/shortcut_service.py` | Creates the desktop shortcut | careful |
| `software/study_runner/runtime_core/settings/admin_status_service.py` | Aggregates plugin status for the dashboard | careful |
| `software/study_runner/runtime_core/delivery/artifact_manifest_service.py` | Owns artifact provenance, checksums, completion markers, and guarded source purge | no |
| `software/study_runner/runtime_core/delivery/csv_export_service.py` | Converts the backup grid XDF into one SPSS/R-friendly CSV | no |
| `software/study_runner/runtime_core/studies/card_summary_service.py` | Pure merged-XDF-to-card-statistics derivation | no |
| `software/study_runner/runtime_core/delivery/finalization_runtime.py` | Wires the persistent finalizer to recording and upload adapters | no |
| `software/study_runner/runtime_core/delivery/finalization_service.py` | Durable, idempotent session-finalization state machine and journal replay | no |
| `software/study_runner/runtime_core/delivery/software_provenance.py` | Which Study Runner and plugin versions produced a session (recording, card, destination), fixed when its finalization job is created and written to `meta/manifest.json` → `provenance.software` | careful |
| `software/study_runner/runtime_core/delivery/destination_plugin_service.py` | Converts upload-destination manifests into persisted finalization steps and recovery/purge policies | no |
| `software/study_runner/data_core/host/plugin_health_poll_service.py` | Manifest-paced, non-blocking per-plugin health cache and bounded poll executor | careful |
| `software/study_runner/data_core/host/recording_runtime.py` | Public compatibility facade plus session-level recording orchestration; contains no process-launch or scientific-validation implementation | no |
| `software/study_runner/data_core/host/recording_dependencies.py` | liblsl dependency probe and capability-based selection of study/internal recording providers | no |
| `software/study_runner/data_core/host/recording_worker_launcher.py` | Detached Python/self worker process command, isolation flags, and startup health handshake | no |
| `software/study_runner/data_core/host/recording_runtime_support.py` | Shared recording error, constants, safe session-path/JSON helpers, recovery grid, and required-source readiness gate | no |
| `software/study_runner/data_core/host/recording_timing.py` | Validates the machine's bounded start and end wait settings for recording | careful |
| `software/study_runner/data_core/host/recording_quality.py` | Scientific source, backup, gap/drop, lease-expiry, and finalization quality checks | no |
| `software/study_runner/runtime_core/delivery/recording_finalization_adapter.py` | Thin bridge from persistent finalization steps to recording freeze, validation, merge, and shutdown | no |
| `software/study_runner/runtime_core/studies/study_plugin_config.py` | Migrates legacy sensor/upload/card fields into the manifest-driven plugin settings shape | careful |
| `software/study_runner/runtime_core/studies/migrate.py` | One-way compatibility rewrites for saved study files: the pre-card `stimulus_duration_ms` field becomes a real stimulus card. Runs on load, never on save, so an old study opens without the old shape leaking back into new files | careful |
| `software/study_runner/runtime_core/settings/migrate.py` | One-way compatibility rewrites for machine settings: repoints plugin paths stored by older installs at the folder layout in use today | careful |
| `software/study_runner/runtime_core/delivery/migrate.py` | One-way compatibility rewrites for delivery state: upload-job metadata and the leftover pre-1.0 queue shape | careful |
| `software/study_runner/runtime_core/studies/trial_event_service.py` | Persists idempotent trial events and backend-enforced deadlines | no |
| `software/study_runner/runtime_core/studies/session_journal_service.py` | Append-only, fsynced per-session audit journals plus terminal-finalization archive | no |
| `software/study_runner/data_core/host/recording_contract.py` | Builds and persists the immutable, hash-checked `recording-plan.json` snapshot each session starts with | no |

## Recording, host side (`software/study_runner/data_core/host/`)

`backup.py`, `errors.py`, `recovery.py`, and `worker_protocol.py` used to live
here; they moved to `software/study_runner/data_core/contract/` (see that
folder's own README) because both the host and the detached worker need
them and may not import each other. The table below is the current list.

| File | Purpose | Edit? |
|---|---|---|
| `artifacts.py` | Canonical immutable session identity, path layout, and SHA-256 helpers | no |
| `coordinator.py` | Allocates append-never plugin segments and sends idempotent worker commands | no |
| `worker_binary.py` | Locates and validates the tiny platform-native XDF core and its build manifest | no |
| `xdf.py` | Native-worker backend contract plus pinned PyXDF source/merge validation | no |
| `markers.py` | The study's own event-marker LSL outlet; every session carries it, not a plugin | careful |
| `clock_diagnostics.py` | Wall/LSL/client clock observations at event boundaries; every session carries it, not a plugin | careful |
| `clock_sync_service.py` | Bounded tablet/worker clock-offset and RTT history for the admin dashboard | careful |
| `sensor_coordinator_service.py` | Central plugin lifecycle/status wrapper with a stale-while-revalidate health cache | careful |
| `plugin_health_poll_service.py` | The bounded, per-plugin polling engine `sensor_coordinator_service.py` is built on | careful |
| `sensor_flush_service.py` | Periodic background export of live sensor history for crash recovery | no |
| `study_sensor_runtime.py` | Which sensors are effectively on right now (hardware config + study settings + session overrides) | careful |
| `recording_capacity.py` | Preflight storage check: required space from the declared stream contract, never a disk benchmark | careful |
| `recording_contract.py` | Snapshots the exact manifests/streams/backup projections a recording started with, hash-checked | no |
| `recording_dependencies.py` | liblsl dependency probe and capability-based selection of study/internal recording sources | no |
| `recording_quality.py` | Turns a validation report, lease state, and source health into finalization's pass/fail checks | no |
| `recording_runtime.py` | `RecordingRuntimeService`: the orchestrator `apps/server` actually calls (preflight, start, freeze, merge, shut down) | no |
| `recording_runtime_support.py` | Small helpers `recording_runtime.py` needs: session identity, port reservation, the public recording plan | no |
| `recording_worker_launcher.py` | Detached Python/self worker process command, isolation flags, and startup health handshake | no |
| `markers.manifest.json`, `clock_diagnostics.manifest.json` | Declare each built-in source's streams the same way a plugin manifest does, loaded through the same validator, never discovered from a directory | no |

## Detached recording worker (`software/study_runner/data_core/worker/`)

| File | Purpose | Edit? |
|---|---|---|
| `application.py` | Authenticated loopback command server with leases, generation fencing, and durable command replay | no |
| `core.py` | Checked `ctypes` binding for the stable C ABI exposed by `xdf_core` | no |
| `lsl_recording.py` | Threaded LSL inlet capture, validated stream headers, bounded drain, and backup projection | no |
| `session_journals.py` | Appends `quality.jsonl`/`timing.jsonl` while recording and fsyncs them on the same checkpoint tick as the XDF data, so an aborted session still leaves quality evidence | no |
| `runtime.py` | Session lifecycle, segment control, merge orchestration, and worker health state | no |

The audited native XDF writer lives in `software/recording_worker/native/`.
Its CMake target wraps the pinned LabRecorder XDFWriter behind a small C ABI;
it deliberately contains no HTTP, LSL, plugin, or study logic.

## Updater trust chain

| File | Purpose | Edit? |
|---|---|---|
| `software/study_runner/updates/signatures.py` | THE shared signed-update wire format + Ed25519 verification | no |
| `software/study_runner/updates/trusted_keys.py` | Trusted public keys for the dormant packaged-build updater; the source release uses none | no |
| `software/study_runner/updates/installer.py` | Applies a staged update on restart (`--apply-update`) | no |
| `software/study_runner/updates/archive_update.py` | Release-archive updates: safe extract, checksum, program-file swap that keeps studies/results/settings, rollback | no |

## Plugin framework (`software/study_runner/plugin_framework/`)

Nothing in this folder is a plugin -- it's the machinery that finds,
validates, and talks to the plugins in `plugins/` below; see that
folder's own README. Note that "plugin" and "extension" are not synonyms
here: a plugin is a Python package under `plugins/`, while an extension is a
browser module a manifest declares under `ui.extensions`.

| File | Purpose | Edit? |
|---|---|---|
| `plugin_secrets.py` | Per-study credential storage and env/study/machine/legacy resolution used by the host and each plugin subprocess | careful |
| `adapter_utils.py` | Shared timestamps, locked state updates, config-section lookup, and `plugin_runtime_dir`/`runtime_path_setting` (plugin logs and state live next to the results, never in the program files) | careful |
| `registry.py` | The façade almost everything else calls: manifest-driven plugin lookup, generic actions, interval summaries, sidecar exports | careful |
| `sensor_connection.py` | The one connection pattern for every sensor: normalizes the reported phase/signal/setup and decides `ready` and the next step (the dashboard's call to action) | careful |
| `sensor_streams.py` | The one way a sensor publishes data: LSL outlets built from the manifest, explicit and never-decreasing timestamps, reversible corrections, counted push failures, and the live view fed by the same pushes | careful |
| `live_view.py` | The dashboard's live view of recorded samples (mean per 0.5 s, last 60 s) in the plugin process, and the host's one standardized `live` shape | careful |
| `plugin_catalog.py` | Discovers trusted plugin folders and validates API-v5 manifests before dispatch | no |
| `plugin_layout.py` | Defines trusted plugin category roots shared by discovery, drivers, UI assets, and self-check | no |
| `card_session_isolation.py` | Refuses a card whose `card.js` keeps mutable module-level state, so no answer can reach the next participant | careful |
| `driver_runtime.py` | Runtime used by the single `driver.py` entry point every API-v5 plugin process runs | careful |
| `runtime_contract.py` | Manifest-derived operation allowlist and startup checks for every plugin callback | careful |
| `process_host.py` | Host-side supervisor for API-v5 drivers: start/stop/restart, line-oriented console, reserved-prefix RPC; cards additionally get no app context and terminate their process on an RPC timeout, everyone else does not | careful |
| `card_catalog.py` | Package 5g.B5: `card_bindings()` -- the manifest-driven `question_type -> (catalog entry, card_contract)` lookup every card-aware caller reads instead of a hardcoded type list; `QuestionTypes` is the live set view `ALLOWED_QUESTION_TYPES`/`NON_ANSWER_QUESTION_TYPES` wrap | careful |
| `history_buffer.py` | Session-sized ring buffers + gap/truncation detection for all sensors | careful |

(`dependency_utils.py` -- optional auto-install of Python packages a plugin
needs -- actually lives in `software/study_runner/shared/`, not here; see
the "Runtime and shared services" section above. A previous version of this
guide listed it in this section by mistake.)

## Plugins (`software/study_runner/plugins/`)

The folder name, public plugin key, and hardware-config key are deliberately
not assumed to be identical (the manifest's `plugin_key` and `config_key`
decide; tests derive the mapping from the manifests):

| Folder | Plugin key | Config key |
|---|---|---|
| `am_hub` | `am_hub` | `am_hub` |
| `brainbit` | `brainbit` | `brainbit` |
| `mr60_mini_radar` | `mini_radar` | `mini_radar` |
| `camera_emotion` | `camera_emotion` | `camera_emotion` |
| `osc_touchdesigner` | `osc` | `osc` |
| `notion_upload` | `notion` | `notion` |
| `nextcloud_upload` | `nextcloud` | `nextcloud` |

`lsl_markers` and `clock_diagnostics` used to be here. Removing either broke
recording -- see `tests/test_plugin_removability.py` -- so they are core
recording code now, not plugins: `data_core/host/markers.py` and
`data_core/host/clock_diagnostics.py`.

| File | Purpose | Edit? |
|---|---|---|
| `__init__.py` (all) | Empty package markers | yes |
| `brainbit/adapter.py` | Supervises the BrainBit CLI process (has a TOC docstring) | careful |
| `brainbit/monitor.py` | Connection-scoped diagnostics, battery freshness and bounded dashboard trend history | careful |
| `brainbit/brainbit_realtime_cli.py` | The external BrainBit EEG CLI itself (SOURCE OF TRUTH; in a lab-workspace checkout, `../../Sensorik/` is the separate external hardware/sensor reference folder this mirrors into — see `sensors-and-data.md` — not generated by the app and not part of this repo); also runs inside packaged builds via `--brainbit-cli`. See `plugins/sensors/brainbit/README.md` for NeuroSDK/BrainFlow provenance. | careful |
| `brainbit/plugin.py` | Plugin wrapper: config defaults + lifecycle for BrainBit | careful |
| `brainbit/driver.py` | API-v5 process entry point (`run_plugin_driver("brainbit")`) | no |
| `brainbit/diagnose_backends.py` | Standalone 30-second NeuroSDK vs. BrainFlow A/B diagnostic, outside the acquisition path | careful |
| `brainbit/tools/ble_brainbit_probe.py` | Standalone discovery probe: lists the bands this computer can see and which one the production selector would pick | yes |
| `mr60_mini_radar/adapter.py` | MR60 heart/breathing radar via serial or BLE, with auto-reconnect | careful |
| `mr60_mini_radar/plugin.py` | Plugin wrapper for the radar | careful |
| `mr60_mini_radar/driver.py` | API-v5 process entry point (`run_plugin_driver("mini_radar")`) | no |
| `mr60_mini_radar/tools/ble_mr60_receiver.py` | Standalone BLE test receiver for debugging | yes |
| `am_hub/adapter.py` | Parasite AM Hub API-v2 SSE client: one LSL sample per real board frame in `radar`/`bio`/`valves`, every other hub event verbatim in `hub_events`; connection watchdog, card summaries | careful |
| `am_hub/monitor.py` | What the AM Hub dashboard shows (latest values, boards, person, bounded graphs), derived only from received events and cleared on every start/stop - like `brainbit/monitor.py` | careful |
| `am_hub/plugin.py` | Plugin wrapper for the AM Hub adapter | careful |
| `am_hub/driver.py` | API-v5 process entry point (`run_plugin_driver("am_hub")`) | no |
| `camera_emotion/adapter.py` | Accepts tablet camera frames and publishes stable LSL streams | careful |
| `camera_emotion/plugin.py` | Single public camera/emotion plugin and generic admin actions | careful |
| `camera_emotion/driver.py` | API-v5 process entry point (`run_plugin_driver("camera_emotion")`) | no |
| `camera_emotion/worker/server.py` | Internal DeepFace worker process (Flask, `--emotion-worker`) | careful |
| `camera_emotion/worker/plugin.py` | Starts, monitors, and repairs the internal worker | careful |
| `camera_emotion/worker/analyzer.py` | Runs DeepFace on one frame | careful |
| `camera_emotion/worker/model_errors.py` | Shared DeepFace error classification + suggested fixes | careful |
| `osc_touchdesigner/adapter.py` + `plugin.py` | Forwards values to TouchDesigner via OSC | careful |
| `osc_touchdesigner/driver.py` | API-v5 process entry point (`run_plugin_driver("osc")`) | no |
| `notion_upload/adapter.py` + `plugin.py` | Uploads result summaries to Notion (with offline queue) | careful |
| `notion_upload/mapping.py` | The export mapping's evaluator: output catalog, reducers, column values for one session - no Notion client | careful |
| `notion_upload/notion_ids.py` | Reads a Notion page or database ID from a pasted link or ID - standard library only | careful |
| `notion_upload/ui/configurator.js` | The study-settings configurator modal: page tree, target list, column mapping | careful |
| `notion_upload/ui/configurator-node-view.js` | Compiles one target's columns to a node graph and back | careful |
| `notion_upload/ui/node-canvas.js` | Generic pan/zoom/wire node canvas, no dependency on Notion | careful |
| `notion_upload/driver.py` | API-v5 process entry point (`run_plugin_driver("notion")`) | no |
| `nextcloud_upload/plugin.py` | Declares the hidden Nextcloud destination capability, publishes, and validates its own share-link setting | careful |
| `nextcloud_upload/webdav_client.py` | The WebDAV client: uploads session files to a writable Nextcloud public share, checksum-first | careful |
| `nextcloud_upload/driver.py` | API-v5 process entry point (`run_plugin_driver("nextcloud")`) | no |

## Cards (`software/study_runner/plugins/cards/`)

Package 5g.B5: every question/card type is a genuine, process-isolated API-v5
plugin like any sensor or destination above, not a hardcoded type string.
`participant-id`/`finish` are ordinary card plugins too -- but the
mandatory bookends of every study, so a catalog with neither cannot author a
*playable* study (see `tests/test_plugin_removability.py`'s module docstring
for why that is accepted, not a regression).

Fourteen folders, one per card (`choice` alone answers both `choice` and
`single`); each one is the same four files:

| File | Purpose | Edit? |
|---|---|---|
| `<card>/manifest.json` | Declares `capabilities.card_contract`: `question_types`, `answerless_types`, and any `host_data` the card needs from the host (stimulus's `plugin_actions` and `actuator_plugins`); `ui.extensions.card` names its JS entry point | careful |
| `<card>/driver.py` | API-v5 process entry point (`run_plugin_driver("<card>")`) | no |
| `<card>/plugin.py` | Implements the executable card contract: `get_card_defaults`/`normalize_card_config`/`validate_card_answer`; imports only `contracts`, never `runtime_core` | careful |
| `<card>/card.js` | Renderer/editor module: `metaByType`, `configureCard`, `renderStudy`, `renderEditor`, `collectConfig`, `collectAnswer`, and the optional `isAnswered`/`bindInteractions` hooks (a stimulus card may also export `onStimulusPrepared`/`onTimeUp`, called by the stimulus runtime); loaded on demand by `apps/ui/scripts/cards/index.js` | careful |

| `mood_meter/mood-core.js` | Mood Meter core: quadrants, each word's place on the energy x pleasantness plane (Brackett's 10 x 10 grid), colors, morphing shape, spring, animation loop | careful |
| `mood_meter/word-space.js` | Mood Meter fullscreen word space (pan with momentum, zoom or circle reveal), shared by the classic and blob views | careful |
| `mood_meter/view-classic.js`, `view-blobs.js`, `view-field.js`, `view-orbit.js` | The four Mood Meter views behind one small interface (`render`, `bind`, `onClick`, `refresh`, `records`); chosen by the card's `variant` setting | careful |
| `affect_map/README.md` | Affect Map's independent Field/Orbit card, color controls, answer shapes and scientific context | careful |
| `affect_map/mood-core.js` | Independent positional and color core, copied from Mood Meter for plugin removability | careful |
| `affect_map/view-field.js`, `affect_map/view-orbit.js` | Affect Map's Field/Orbit views with per-card palette support | careful |
| `affect_map/card.css` | Affect Map's self-contained visual styles | careful |
| `stimulus/sound-cues.js` | The stimulus card's end-of-time sound: built-in gong/bell/beep synthesized with WebAudio, an optional custom file decoded ahead, the iPad audio unlock; played through the card's `onTimeUp` hook | careful |

Folders: `affect_map`, `choice`, `finish`, `info`, `likert`, `mood_meter`, `multi_slider`,
`participant_id`, `ranking`, `semantic`, `slider`, `stimulus`, `text`,
`word_cloud` (folder, plugin key, and config key are the same string for every
one, unlike the plugin table above).

## Frontend (`software/study_runner/apps/ui/scripts/`)

| File | Purpose | Edit? |
|---|---|---|
| `participant/study-controller.js` | Participant page initialization, card rendering, and page orchestration | careful |
| `participant/tablet-clock.js` | Repeated tablet-server clock exchanges, low-delay selection, freshness, and event-time evidence | careful |
| `participant/participant-event-payload.js` | Shared participant marker and trial payload with timing provenance | careful |
| `participant/participant-pending-submission.js` | Durable browser-side retry state for a final result submission | careful |
| `participant/participant-navigation.js` | Card navigation and local finish transition after submission | careful |
| `participant/participant-session-recovery.js` | Clock sync, partial snapshots, lifecycle events, cover page, and reload recovery | careful |
| `participant/participant-stimulus-execution.js` | Prepared-trial timing, stimulus content lifecycle, and the end of the time: auto-advance or overtime until Next, with the time-up marker and one actuator stop | careful |
| `participant/participant-result-submission.js` | Answer/card metrics, sensor and plugin session state, navigation readiness, and submission | careful |
| `admin/admin-controller.js` | Admin initialization, header, QR/access information, and view navigation | careful |
| `admin/admin-run-control.js` | Readiness gates and active study-run controls | careful |
| `admin/admin-update-handling.js` | Update status, download/install confirmation, progress, and restart handling | careful |
| `admin/admin-study-editor.js` | Card editor/list, study persistence, packages, and recent-study behavior | careful |
| `admin/admin-dashboard-controller.js` | Dashboard polling, tile placement, sensor controls, and participant target actions | careful |
| `admin/participant-devices-panel.js` | Renders participant connections, short device codes, target choices, and start acknowledgement | careful |
| `admin/study-start-request.js` | Reconciles an ambiguous Start request timeout with the server's saved run state | careful |
| `admin/dashboard-ui-helpers.js` | Formatting helpers for sensor tiles and the `ui` object handed to plugin dashboard extensions | careful |
| `admin/live-trend.js` | Draws every sensor's declared live-view series the same way (2 Hz points over 60 s) | careful |
| `admin/sensor-columns.js` | Sensor dashboard column layout and tile placement | careful |
| `admin/sensor-tile-order.js` | The operator's two-column tile arrangement, including sensor and participant tiles | careful |
| `admin/sensor-tile-drag.js` | Moving a tile by its title bar with the mouse, touch or the arrow keys; reports only the drop | careful |
| `admin/runtime-switch-input.js` | Accessible Off/On/Restart switch behavior for live sensors | careful |
| `admin/sensor-connection-panel.js` | The connection panel on every sensor tile: status line, Ready badge, switch, device list and guided steps; also the study bar's state | careful |
| `settings/machine/machine-settings-panel.js` | Machine settings shell: nav, generated sensor forms, tablet links | careful |
| `settings/machine/recording-timing-panel.js` | Machine settings for recording start and end wait times | careful |
| `settings/machine/certificate-settings-controller.js` | Certificate status, setup, export, and import, inside the machine settings shell | no |
| `settings/machine/branding-settings-controller.js` | Upload and remove the group and funder logos, inside the machine settings shell | no |
| `settings/study/study-settings-panel.js` | Per-study settings shell (editor only): sensors, participant, data destinations, export. Destination plugin settings (Notion, Nextcloud, ...) are generated from the catalog through `destination-block.js`, not from a per-destination file | careful |
| `settings/study/destination-block.js` | One upload destination in the study settings: status line, credential, editable fields, target row with the plugin's configurator, closed "Technical details" | careful |
| `settings/study/destination-rules.js` | Pure rules for that block: which actions and fields show, what an action still asks, the next missing step, an action's payload | careful |
| `admin/session-timeline.js` | Renders completed-session sensor lanes and answer markers as offline SVG | careful |
| `admin/sessions-browser.js` | Completed-session hub list, detail panel, and timeline data fetching | careful |
| `admin/upload-monitor.js` | Corner progress widget for background finalization/uploads; a click opens the session's progress rail | careful |
| `admin/operator-notices.js` | Shows operator notices as a toast and keeps them in a stack until clicked | careful |
| `admin/session-progress-rail.js` | Session detail progress rail: started, ended, every finalization step left to right, with retry and degraded confirmation | careful |
| `admin/finalization-actions.js` | Retry, degraded confirmation and open-folder for a finalization job, shared by the live notice and the session detail's "Completion & uploads" card | no |
| `admin/recovery-panel.js` | Hub banner listing crash-orphaned sessions, with finalize/discard actions | careful |
| `admin/plugin-console.js` | Diagnostics modal: guided plugin status view plus the line-oriented expert console (SSE-fed) | careful |
| `shared/study-settings.js` | THE client-side study-settings shape; mirrors `_validate_study_settings` | no |
| `shared/dashboard-graph.js` | Shared graph markup and tooltip helpers for sensor dashboard extensions | careful |
| `shared/deadline-timer.js` | Monotonic deadline timer whose UI ticks never define elapsed study time | no |
| `shared/finalization-view-model.js` | Pure finalization status/progress view model | careful |
| `shared/software-provenance-view-model.js` | Pure view model for a session's software versions: the rows of the session view and the methods sentence it copies | careful |
| `shared/timeline-view-model.js` | Pure timeline model: stream grouping, waveform/line classification from the LSL header, zoom window maths | careful |
| `shared/plugin-catalog.js` | Fetches and indexes manifest-derived plugin UI capabilities | careful |
| `shared/participant-plugin-extensions.js` | Failure-isolated lifecycle manager for manifest-declared participant extensions | careful |
| `shared/reliable-event-queue.js` | Session-local idempotent marker/event buffering and retry | no |
| `shared/view-transition.js` | Full-screen sweep between admin views; swaps the view while covered | careful |
| `shared/settings-shell.js` | Shared left-nav/right-panel wiring for both settings surfaces | careful |
| `shared/dom-utils.js` | Shared safe DOM lookup, text/HTML assignment, and escaping helpers | careful |
| `shared/rich-text.js` | Escape-first formatted text (a small Markdown subset) and the text-beside-image layout used by the info card and cover page | careful |
| `shared/plugin-help.js` | The "(?)" setup help for a plugin: labels, example values and plain-language explanations, all from manifest translation keys | no |
| `shared/media-editor.js` | Editor fields for that content: title, text, image upload/removal, image description, layout | careful |
| `shared/modal.js` | Shared accessible modal lifecycle, modal-shell markup, and the yes/no confirmation | careful |
| `shared/branding.js` | Shared branding fetch and logo rendering for the waiting slide and the hub | careful |
| `settings/machine/font-settings-controller.js` | The Fonts settings page next to Logos: choose, upload, or remove the heading and body fonts | no |
| `settings/machine/data-folder-settings-controller.js` | The Data folder settings page: current folder, choose/link another one (optionally bringing the data along), back to the program folder; restarts Study Runner | careful |
| `shared/ambient-bubbles.js` | Self-contained morphing background for the waiting slide; tune CONFIG at the top | no |
| `shared/settings-page.js` | Shared navigation, setup-step state, and action feedback for settings pages | careful |
| `shared/api-client.js` | Tiny fetch helpers (getJson/postJson) | careful |
| `shared/clipboard.js` | Copies text to the clipboard, with a fallback for admin pages opened over plain http | careful |
| `shared/i18n.js` | Translation loading and the `t()` helper | careful |
| `plugins/sensors/camera_emotion/ui/participant.js` | Camera/emotion participant lifecycle extension for preview, stimuli, submit, and heartbeat status | careful |
| `plugins/sensors/camera_emotion/ui/camera-capture.js` | Plugin-owned tablet camera capture and frame upload adapter | careful |
| `plugins/sensors/am_hub/ui/dashboard.js` | AM Hub dashboard: movement/breathing/heart-rate tiles, 60 s trend graphs, per-board link details | careful |
| `plugins/sensors/brainbit/ui/dashboard.js` | Optional BrainBit rich-status renderer loaded through the manifest extension hook | careful |
| `plugins/sensors/mr60_mini_radar/ui/dashboard.js` | Optional MR60 rich-status renderer loaded through the manifest extension hook | careful |
| `plugins/sensors/camera_emotion/ui/dashboard.js` | Optional camera/emotion rich-status renderer loaded through the manifest extension hook | careful |
| `participant/study-client-heartbeat.js` | Keeps the tablet visible on the dashboard | careful |
| `shared/qr-code.js` | QR code rendering for the access card | no |
| `cards/index.js` | Package 5g.B5: `loadCards()` fetches each installed card's `card.js` (`/api/plugins/<key>/assets/card.js`) and Python-authoritative defaults (`/api/plugins/<key>/card-defaults`) instead of a static import list; the 13 card modules themselves now live in `plugins/cards/<name>/card.js` | careful |
| `cards/card-info.js` | The shared editor frame every card composes into: question text, instruction, note, toggle group | careful |
| `cards/card-mount.js` | `mountCard()` renders and binds a card the same way on the participant page and in the editor's live preview, and takes a card's touch finger away at its first touch; `dispatchCardHook()` routes delegated clicks/inputs | careful |
| `cards/card-motion.js` | `runAnimation()` for any card: rests out of view, ends when the card is replaced, stops on every session reset; `touchHint()` shows a gently tapping finger instead of a "touch here" sentence (the sentence stays its accessible name) | careful |
| `cards/session-state.js` | `cardState()` / `onSessionReset()`: the only place a card keeps non-DOM state; cleared at every session boundary | careful |

Locales (`apps/ui/locales/en.json`, `de.json`) hold every UI string; both
files must have identical keys (a test checks this). `apps/ui/vendor/`
holds offline copies of third-party assets (icons).

## Release tooling (`release_tools/`, repo root)

| File | Purpose | Edit? |
|---|---|---|
| `release_tools/build_source_release.py` | Builds and verifies the source-release archives, checksums, metadata, and release notes | careful |
| `release_tools/update_acceptance.py` | Release gate: the installed release updates itself to a synthetic next version from a local server (Windows with long paths off) and checks that user data stayed | careful |
| `release_tools/tests/test_build_source_release.py` | Regression tests for safe source archives, metadata, release notes, and workflow contracts | yes |
| `release_tools/build_python_onedir.py` | Legacy/future non-recording experiment: runs PyInstaller; not used by the active release | careful |
| `release_tools/package_python_onedir.py` | Legacy/future non-recording experiment: packages PyInstaller output; not used by the active release | careful |
| `release_tools/build_python_update_manifest.py` | Legacy packaged-updater manifest signer; not used by the active release | no |
| `release_tools/write_python_update_key.py` | Legacy packaged-updater key helper; not used by the active release | no |
| `release_tools/fetch_deepface_model_assets.py` | Explicitly provisions the separately licensed, SHA-256-pinned DeepFace emotion model after terms acceptance | careful |
| `release_tools/build_offline_wheelhouse.py` | Builds an offline pip wheelhouse | careful |
| `release_tools/pyinstaller/study_runner_server_common.py` | Legacy/future PyInstaller spec pieces; not used by the active source release | careful |
| `release_tools/tests/test_pyinstaller_common.py` | Regression tests for the frontend runtime path and dynamic plugin-bundle collection | yes |
| `release_tools/release-study-runner.mjs` | The release script: bump, check, tag, push (run via release.ps1) | no |
| `release_tools/verify-release-version.mjs` | CI guard: tag matches version.py | no |

Tests live in `software/tests/` - one file per area, named
`test_<area>.py`. Run everything with:
`python -m unittest discover software/tests`

`software/tests/test_source_install_scripts.py` protects the pinned uv/Python
bootstrap, the project-local `.tools` and `.venv`, the admin- and compiler-free
installer paths, the prebuilt-core download, the non-destructive installer,
and daily-start contracts.
`software/tests/test_python_constraints.py` protects the exact scientific pins,
platform split, shared installer/CI consumption, and honest non-lockfile scope.
