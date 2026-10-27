# Migration

## Move to the Materiability Research Group

On 2026-10-27 this repository was transferred from `realfabianschmidt/MRG-StudyRunner` to `Materiability-Research-Group/MRG-StudyRunner`. History, branches, tags and releases moved with it. GitHub redirects the old URLs, including `releases/latest/download/...`, as long as no new repository named `MRG-StudyRunner` is created under `realfabianschmidt`.

## Open after the move

### Update and install paths

These places still name the old repository. Installed copies keep working through the redirect; the paths should move to the organization, planned together with v2:

| File | What |
| --- | --- |
| `software/study_runner/runtime_core/settings/update_service.py` | `DEFAULT_MANIFEST_URL`, `DEFAULT_SOURCE_RELEASE_URL`, the release-notes URL and the fallback repository in the source-release download |
| `tools/study_runner_manager.py` | `REPO_URL`, `MANIFEST_URL` |
| `tools/setup_recording_worker.py` | `DEFAULT_RELEASE_REPOSITORY` |
| `release_tools/release-study-runner.mjs` | Links to the release workflow and release page printed after a release |
| `README.md`, `docs/start-here.de.md` | Download link and `git clone` URL |

The release workflow passes `$GITHUB_REPOSITORY` to `release_tools/build_source_release.py`, so release metadata from new releases names the organization repository without a code change. Only the hard-coded defaults above need editing.

Changing the update URLs changes where existing installs look for updates. Ship that change in a release that is still reachable through the old URL, and keep the redirect intact afterwards.

### Repository settings

Check that the release workflow's secrets and signing keys are still configured after the transfer, and that branch protection on `main` still applies.

### Relation to TRACE

[MRG-TRACE](https://github.com/Materiability-Research-Group/MRG-TRACE) is planned as the successor identity of StudyRunner. Nothing has moved between the two repositories.
