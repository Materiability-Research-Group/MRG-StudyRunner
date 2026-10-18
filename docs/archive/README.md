# Archive

Plans, audits and implementation records that are no longer instructions.
They are kept because they explain *why* the code looks the way it does, and
several source comments cite them as provenance.

**Nothing in this folder is current.** Paths, package names, API versions and
open questions inside these files describe the repository as it was when they
were written — `extensions/` before it became `plugins/`, `web/` before
`apps/ui/`, numbered `docs/` files that no longer exist. Do not treat any of it
as a specification, and do not repair the stale paths: they are part of the
record. The active documentation is in the parent folder, listed in the
[root README](../../README.md).

## What is here

| File | What it recorded |
|---|---|
| `architecture-1.0-initial.md` | The 1.0 target architecture as originally proposed (German). The unchanged input to the rebuild. |
| `architecture-1.0-umbau.md` | The working document of the 1.0 rebuild: phase checklist, file ownership, decision log, and the verified traps. Cited by name from many source comments. |
| `architecture-1.0-handoff.md` | Continuation notes while two assistants worked on the rebuild in parallel. |
| `20260914_hotfix.md` | Implementation record of the README cleanup, the removal of the ten old HTTP routes, the `extensions/` to `plugins/` rename, and the card CSS work. |
| `roadmap-0.5.md` | The 0.5 planning document, baseline version 0.4.0. |
| `roadmap-0.5-t8-letsencrypt-alternative.md` | A publicly trusted HTTPS design that was considered and not pursued. |
| `03_plan_for_clearer_code.md` | An early folder-structure plan. |
| `04_admin_panel_biosignal_console_plan.md` | The admin dashboard and biosignal console design. |
| `06_audit_2026-04-28.md` | A codebase audit from April 2026. |
| `08_ui_localization.md` | How the browser UI became localizable. |
| `09_python_auto_update.md` | The Python-server update flow. |
| `10_biosignal_audit_and_limitations.md` | Sensor limitations and the research-grade boundary. |
| `notion-plugin-configurator-plan.md` | The Notion export-mapping configurator of plugin 1.4-1.6 (targets, reducers, node graph). Replaced by the fixed layout of plugin 2.0, described in the plugin's own README. |

## Dropped from these plans

`architecture-1.0-umbau.md` has one unticked box that will stay unticked:

- **5f, the `mrg` maintenance CLI** — dropped on 2026-09-14. The plan already
  recorded that its premise was wrong: it assumed "the same local command API
  as the UI", which does not exist, because the UI speaks HTTPS to Flask.
  Building it meant either giving a CLI its own TLS and authentication or
  designing a new IPC surface, on top of read-only offline inspection and an
  exclusive maintenance lock that never bypasses a running runtime's
  ownership. Nothing depends on it, no code for it was ever written, and
  `CONTRIBUTING.md` §1 and §10 argue against building it for a need nobody
  has. This reverses the 2026-09-08 decision to keep the full Phase 5 scope;
  that entry stays in the plan's decision log as the record of what was
  decided then.

Item 4.10 in the same file is an unused placeholder, not open work.
