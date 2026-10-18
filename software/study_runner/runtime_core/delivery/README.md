# Delivery

Getting a finished session out of the building: turning a submitted result
into a safely saved, checked, and (if the study uses one) uploaded session —
plus the two things needed to let a tablet reach this computer at all: the
HTTPS certificate download and moving that certificate between computers.

## Files

| File | What it does | In / Out |
|---|---|---|
| `migrate.py` | Converts the former Notion JSONL queue into deterministic journaled upload jobs, redacting secrets and retaining malformed source files for repair and retry. | a legacy queue file to current upload jobs |
| `finalization_service.py` | The main pipeline, and the biggest file here: a durable, crash-safe, replayable state machine that takes a submitted result through freezing the recording, validating it, merging XDF, building card statistics, writing the result manifest, publishing to any configured destination, and finally purging local sources. Every step is journaled, so a restart resumes exactly where it left off instead of redoing or skipping work. | a committed submission → a step-by-step finalization job, through to `completed`/`completed_degraded`/`attention_required` |
| `finalization_runtime.py` | Five lines that wire `FinalizationService` into the Flask app once at startup, with its real recording adapter and destination handler. | the Flask app → a configured `FinalizationService` in `app.config` |
| `recording_finalization_adapter.py` | The bridge between the finalization pipeline above and the actual recording runtime (`data_core/host/`): "freeze", "validate sources", "merge", "validate merge" as finalization steps, each translating a recording result into the pipeline's pass/fail language. | a finalization step → a recording-runtime call, translated into a `StepResult` |
| `destination_plugin_service.py` | What a finalization job needs to know about an upload destination plugin (Notion, Nextcloud, ...) *without* running any network code itself: its step key, whether a study enabled it, and its scientific/recovery policy (may it publish a degraded result? may it purge the local copy afterwards?). Persisted with each job so a replay never depends on whatever plugin version happens to be installed later. | an installed destination plugin's manifest → a `DestinationPluginDefinition` |
| `upload_job_destination_handler.py` | Turns each upload step of a finalization job into one persistent upload job with a fixed id (`<finalization>-g<generation>-<destination>`), so a restart finds the same job instead of uploading twice. "Upload again" on a finished upload adds `-r<n>`: a fresh job that uses the study's current destination settings. | a destination step → a queued upload job, and its outcome back as the step's result |
| `upload_jobs_service.py` | The actual background upload queue: a journaled, retried-with-backoff job runner. Used by the main pipeline above (through `UploadJobDestinationHandler`) to run each destination's real publish call, and separately as the direct path for older, pre-finalization-pipeline recovery artifacts. | a queued job → executed, retried on failure, status reported |
| `withdrawal_service.py` | Carries out a consent withdrawal: stop any active recording, cancel queued uploads, then empty the session folder down to one `WITHDRAWN.json` tombstone — replayable, so an interrupted withdrawal can always resume rather than leaving a half-deleted folder. Never claims to delete an already-uploaded copy from a remote server; it names that destination in the tombstone instead. | a session + a reason → a withdrawn session, or a clear resumption of an interrupted one |
| `artifact_manifest_service.py` | Writes the deterministic inventory of a finished session's files (checksums, provenance) plus its terminal `COMPLETE.json`/`ATTENTION_REQUIRED.json` marker, and guards the one-way local-source cleanup that may only happen after a verified remote copy exists. | a session's artifacts → a signed manifest + completion marker |
| `certificate_download_service.py` | A tiny, separate plain-HTTP server with exactly one file at one fixed path: the local root certificate. Needed because a tablet can't yet trust the real HTTPS server to fetch the certificate that would make it trusted. | — → the certificate's bytes, over plain HTTP, nothing else served |
| `certificate_transfer_service.py` | Move that same root certificate to a replacement computer: export it (with a timestamped backup kept locally) and import it elsewhere, fully validated before anything on disk is touched, so a bad import file can never leave the server unable to start HTTPS. | a certificate + private key → a portable export file, or back |
| `upload_runtime.py` | Five lines' worth of wiring, plus one migration: at startup, registers every installed `upload_destination` plugin with the job queue above, and migrates any leftover pre-1.0 Notion-only queue entries into the current shape. | the Flask app → a configured `UploadJobService` with every destination registered |
| `__init__.py` | One-paragraph orientation: finalization, the upload queue and its destinations, and the certificate a tablet needs. | — |

## How the pieces fit together

A normal submission's path through this folder: `results.py` (in
`apps/server`) commits the raw answers, then hands off to
`finalization_service.py`, which runs its fixed sequence of steps —
`recording_finalization_adapter.py` for anything recording-related,
`artifact_manifest_service.py` at the end for the inventory and completion
marker, and `destination_plugin_service.py` + `upload_jobs_service.py` for
actually publishing to Notion/Nextcloud/whatever a study configured. A
withdrawal, at any point in that lifecycle, goes through
`withdrawal_service.py` instead, which knows how to stop all of the above
safely rather than letting it finish.

**Upload again.** A finished upload stays repeatable: `FinalizationService.republish()` resets only that one destination step of a completed session (status, quality and every other step stay as they are), and the next upload job is built with the study's current destination settings. It is the same for every destination plugin; each plugin must therefore publish idempotently, updating what a session sent before instead of adding a copy. The two certificate files are
unrelated to any of this — they only exist so a tablet can reach the server
at all.
