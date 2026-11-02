# Study Runner V2: plan

This is the plan for Study Runner 2.0. It describes three new features, the decisions
already made about them, what has to change in the code, and the order of the work. **Nothing
here is built yet.** V2 is developed on its own branch (`v2`) while `main` keeps receiving 1.x
fixes and stabilisation.

Status: planning, written 2026-10-10. The `v2` branch was started the same day; CI runs on it.

How paths are written:
- Paths in code sections are relative to `software/study_runner/`, unless they start with
  `software/`, `tools/`, `release_tools/` or `.github/`.
- `<settings>` is the settings folder (`software/study_content/settings/` by default).
- `<data>` is the results folder (`saved_results`).

## 1. V2 in one page

Study Runner 1.x shows a study as one fixed list of cards:
- the participant-ID card is always first;
- recording starts right after it;
- every participant sees every card in the same order;
- one computer runs one study with one tablet at a time.

V2 changes three things.

**1. Cards before the participant ID, and a movable recording start.** A study can open with
welcome, information or consent cards before the participant-ID card. The researcher decides
where recording starts: right after the ID card (as today) or at any later card.

**2. A node view for study logic.** The study editor gets a second view next to today's list.
There, every card is a box (a *node*), and arrows (*wires*) decide what comes next. Between the
cards sit two other kinds of node:
- *logic nodes*, such as "if/then", "random group" or "repeat";
- *device nodes*, such as "BrainBit: calibrate" or "wait until relaxation is high".

Every node comes from a plugin, as before. The plugins describe what they can do, and the editor
turns that into nodes.

**3. Several studies at the same time.** One computer runs several lab places (*stations*) at
once. Each station has its own tablet and its own sensors, and runs the same study as the others
or a different one. A second node view in the dashboard shows, live, which device belongs to which
station. Room devices can serve several stations.

The example the plan is built around:

```
(Start) → [Welcome] → [Consent] ── no ──→ (End: discard, nothing is saved)
                          │ yes
                          ▼
                  [Participant ID] → ● Begin recording → [Breathing exercise]
                                                                │
                                                                ▼
                                    ⟨BrainBit: calibrate⟩ ── failed / timeout ──→ (operator decides)
                                                                │ done
                                                                ▼
                                                     [Questionnaire …] → (End)
```

So there are two node views:
- **study editor:** what happens in a study, and in which order;
- **dashboard:** which device serves which station.

Both use the same drawing component and the same saved format. The AM Hub's new graph uses that
format too.

## 2. Terms

| Term | Meaning |
| --- | --- |
| Node | A box in a graph. A card, a logic step, a device action, or Start/End. |
| Flow wire | Arrow that says what happens next. Leaves a node through a named exit such as `next`, `true` or `done`. |
| Data wire | Line that carries a value, for example a Likert answer or a live sensor value, into another node. |
| Logic node | A step that decides or waits: Branch, Switch, Randomise, Counterbalance, Order, Repeat, Wait. Delivered by *logic plugins*. |
| Device node | A command, value or event of a device, delivered by that device's plugin. |
| Device instance | One physical device known to Study Runner, for example "BrainBit A" with its serial number. Several instances can use the same plugin. |
| Device role | What a study needs, without naming a concrete device, for example "EEG headband (BrainBit)". |
| Station | A lab place: one paired tablet plus the device instances assigned to it. Runs one study at a time. |
| Run | One station running one study, for one participant after another. |
| Session | One participant's way through a study. Belongs to one run. |
| Study revision | A frozen copy of a study. A run always uses one revision; editing creates a new one. |
| Personal / shared device | Personal: records one person, so it can serve only one session at a time (BrainBit, heart rate, tablet camera). Shared: a room device that may be recorded into several sessions. |
| Journal | The server's append-only log of everything that happened in a session. Used to resume after a crash. |

## 3. Decisions already made

Agreed with the user on 2026-10-09, so later sessions do not have to re-derive them.

- **Every card is its own node** in the node view. The list view stays as the second view.
- **Logic in V2.0:**
  - branch and switch on answers;
  - randomisation and counterbalancing;
  - loops and repeats;
  - live sensor values as conditions;
  - device commands with waiting.

  The user's conditions: "it is still for studies" (rigour, logging, validation), and "plugin-based
  as before: the plugins deliver the interfaces and capabilities, also as nodes".
- **Every node type comes from a plugin manifest.** The core only owns Start, End, Begin recording
  and the engine that walks the graph.
- **Parallel in V2.0:** several independent stations on **one** computer, plus shared room devices.
  - Not in V2.0: coupled sessions (several participants in one session) and several computers.
- **Two node views:** the study editor (study logic) and the dashboard (which device serves which
  station).
- **AM Hub stays its own program.**
  - Study Runner starts and stops hub projects and waits for hub events through the `am_hub`
    plugin.
  - Both programs share one graph format.
- **Recording only from the participant ID on.**
  - Cards before the ID are allowed. Their answers wait in the session journal until the ID card
    creates the session folder.
  - The folder layout `<study>/<participant>/<UTC>__<session>/` stays.
  - If someone leaves before the ID card, nothing is written to the data folder.
- **V2 lives on the `v2` branch.** `main` keeps getting 1.x fixes.
- **The UX in this plan is a first sketch** (user, 2026-10-10). A separate UX round comes before
  the editor and the dashboard view are built. See section 11.

## 4. Feature 1: cards before the participant ID, configurable recording start

### For researchers

- **Cards before the ID:** welcome, information, consent, instructions or a short questionnaire
  may come before the participant-ID card. Their answers are kept and saved with the session.
- **The ID card can sit anywhere.** Every path that ends normally passes exactly one ID card.
- **Choosing where recording starts:**
  - In the list view, a line "● Recording starts here" can be dragged to any position after the
    ID card.
  - In the node view, the same thing is the *Begin recording* node.
  - New studies and migrated 1.x studies have it right after the ID card, which is the 1.x
    behaviour.
- **Recording before the ID is not possible, by design.** Biosignals are recorded only once the
  person has agreed and is known. A study that should record from its first screen puts the ID
  card first.
- **Leaving before the ID** writes nothing to the data folder, for example when someone declines
  consent. The "no" path of a consent card ends in an *End (discard)* node.
- **What is in the XDF file.** Cards shown before *Begin recording* have no markers there, because
  nothing is recorded yet. Their times are still kept in the session result.
- **On the dashboard,** a session appears from its first card as "waiting for participant ID"
  and shows the ID once it is entered.

### In the code

Today four things happen together when the participant presses Next on the first card:

1. Card 0 must be the ID card. This is enforced in two places:
   - `hasParticipantIdStartCard()` in `apps/ui/scripts/participant/study-controller.js`;
   - `ensureBookends()` in `apps/ui/scripts/admin/admin-study-editor.js`.
2. The session starts: `/api/study/session/start` in `apps/server/routes/study.py`, which calls
   `SessionStore.start_or_reuse` in `runtime_core/studies/session_store.py`.
3. Sensors are claimed and the XDF recording starts (`RecordingRuntimeService.start_session`).
4. The session folder `<study>/<participant>/<UTC>__<session>/` is created and bound to that
   participant (`data_core/host/artifacts.py`).

V2 separates them:

- **The session starts at the first card.** At that point it exists only in the journal
  (`runtime_core/studies/session_journal_service.py`) and has no folder. Every card visit is
  journaled. Today a card shown before the session cannot even save a checkpoint.
- **The folder is created when the ID card is completed,** and the earlier answers are written
  into it then.
- **Recording starts when the flow reaches *Begin recording*.**
  - The readiness check that runs at session start today (each sensor's `start_condition`,
    evaluated in `runtime_core/studies/live_sensor_readiness.py`) moves to this node.
  - A plugin can declare which of its commands makes the device ready (BrainBit: `calibrate`).
    If the flow runs that command itself after *Begin recording*, the check there waits only for
    a connected device. Otherwise it checks the full condition, as today.
- **Migration of old fields:** the existing `cover_page`, a start screen that is "not a card",
  becomes an ordinary info card before the ID.
- **Tests:** test fixtures that assume `[participant-id, …, finish]` get flow-based
  replacements.

## 5. Feature 2: the node-based study editor

### 5.1 What other tools do

| Tool | How it models study logic | What V2 takes from it |
| --- | --- | --- |
| Gorilla Experiment Builder | A tree of task nodes plus control nodes: Branch, Randomiser, Counterbalance, Order, Repeat, Allocator, Quota, Delay, Reject. Earlier results live in a "Store". | The node set and its meaning. Randomisation inside a repeat decides once. A discard end. |
| lab.js | Sequence, Loop and Parallel components that nest. | Loops are explicit building blocks, never free cycles. |
| jsPsych | Timelines with `conditional_function` (run or skip) and `loop_function` (checked after each pass, with that pass's data). | A loop condition is checked after each pass, on that pass's answers. |
| PsychoPy Builder | A Flow of routines, with loops driven by condition files. | Loop types researchers already know (fixed count, random order). |
| Labvanced | Events made of a trigger plus actions; multi-user studies with role ids. | Trigger → action thinking for device events. Multi-user is noted for later. |
| Unreal Engine Blueprints | Execution pins decide what runs next; data pins carry values. Branch has true/false exits, Switch picks an exit by value. | Flow wires vs. data wires; Branch and Switch. |
| Bonsai, OpenViBE | Dataflow graphs for neuroscience: closed-loop experiments, BCI calibration scenarios. | Live sensor values as typed data flowing into logic nodes. |
| AM Hub graph (our own) | Typed ports (number, boolean, event, command). One source per input. An unwired input uses its set value. Cycles need an explicit node. An output device belongs to one project at a time. | The same format and rules, so both programs speak one language. |

**Library choice: keep our own node canvas.**
- LiteGraph, the canvas library behind ComfyUI, was archived as a standalone project in August
  2025 and now lives inside the ComfyUI frontend.
- The other maintained editors need a framework and a build step, which this project excludes.
  `docs/notion-plugin-configurator-plan.md`, phase 3, explains this.

### 5.2 The graph

There are two kinds of wire.

**Flow wires** say what happens next.
- Every node has one flow input.
- Outputs are named: `next`, `true`/`false`, `done`/`failed`/`timeout`, and so on.
- An output leads to exactly one node.
- Several wires may arrive at one input, so paths can join again.

**Data wires** carry values, for example a Likert answer (number), a consent answer (text) or
the BrainBit relaxation value (number, %, live).
- A data input has one source.
- An input that is not wired uses the value typed into the node's settings.

The consent example as saved data:

```json
{
  "schema_version": 1,
  "nodes": [
    {"id": "consent", "plugin": "choice", "node": "card", "settings": {"…": "card settings"}, "x": 240, "y": 0},
    {"id": "consent_given", "plugin": "branch", "node": "branch",
     "settings": {"operator": "equals", "compare_to": "yes"}, "x": 480, "y": 0},
    {"id": "calibrate", "plugin": "brainbit", "node": "calibrate",
     "settings": {"role": "eeg", "timeout_s": 60}, "x": 960, "y": 0}
  ],
  "edges": [
    {"from": {"node": "consent", "port": "next"},   "to": {"node": "consent_given", "port": "in"}},
    {"from": {"node": "consent", "port": "answer"}, "to": {"node": "consent_given", "port": "value"}}
  ]
}
```

The format is the AM Hub's: `nodes` with `plugin` and `settings`, and `edges` from `{node, port}`
to `{node, port}`. Study Runner adds `node`, because one plugin can offer several node types, and
the position `x`, `y`. The three core nodes use `"plugin": "core"`.

A pure validator in `contracts/` checks these rules, as AM Hub's `contracts/graph.py` does:
- **Port types** are `flow`, `number`, `boolean`, `text`, `event` and `command`. The last four are
  the AM Hub's; `flow` and `text` are Study Runner additions.
- **A wire connects matching types** and matching units.
- **No cycles,** except through a Repeat or Order node. AM Hub's validator already asks for this
  ("cycles require an explicit state/delay node"). Every loop has a maximum number of passes.
- **Size limits** on nodes and wires. AM Hub allows 128 nodes and 512 wires. Study Runner needs
  more for long questionnaires; the exact limit is set during M3.

### 5.3 Where nodes come from

Every node type comes from a plugin manifest. The plugins deliver the interfaces and
capabilities, and the editor builds its node list from them.

| Plugin kind | Nodes it delivers | Example |
| --- | --- | --- |
| Cards (`plugins/cards/*`, 14 today) | One card node per card type, with typed answer outputs | Likert → `answer` (number 1–7) |
| Sensors (`plugins/sensors/*`, 4 today) | Command, value and event nodes | BrainBit → "Calibrate", "Relaxation (live, %)" |
| Outputs (`plugins/outputs/*`) | Command nodes | OSC → "Send message" |
| Logic (new, `plugins/logic/*`) | Branch, Switch, Randomise, Counterbalance, Order, Repeat, Wait, Wait until, Threshold, Window, Read value | see 5.4 |

**Core nodes** are Start, End (complete or discard) and Begin recording. They stay in the core
because they decide when a session exists, when its folder is created and when recording runs,
which is the core's job. The engine that walks the graph is core too.

**Logic nodes are plugins, not core:**
- their versions are recorded and cited like any other plugin's, so a changed randomiser is a
  MAJOR version step that shows up in every paper;
- they can be removed or replaced like cards;
- they use the same catalog, SDK and templates;
- the AM Hub does the same (`plugins/logic/graph_math`, `graph_threshold`, `graph_window`).

A logic plugin is a pure function:
`evaluate(settings, inputs, state, draws) → {port, outputs, state, log}`. The engine keeps the
state, so a retry after a crash gives the same result.

**Command nodes** come from sensor and output plugins. A command declares when it has finished
and when it has failed, as a condition on the device status. This is the same `{path, equals}`
form that sensors already use for `start_condition`. BrainBit example:

```json
"commands": [{
  "key": "calibrate",
  "label": {"en": "Calibrate", "de": "Kalibrieren"},
  "completes_when": {"path": "connection.setup.state", "equals": "done"},
  "fails_when": {"path": "connection.setup.state", "equals": "stalled"},
  "timeout_s": 60,
  "makes_device_ready": true,
  "participant_screen": {"en": "Please sit still – calibrating…", "de": "Bitte ruhig sitzen – Kalibrierung läuft…"}
}]
```

- **Exits:** `done`, `failed` and `timeout`.
- **If `failed` or `timeout` is not wired,** the session pauses and the operator gets a highlighted
  button on the dashboard: repeat, skip or end the session. This follows the guided operator
  set-up the dashboard already uses.
- **Each command is sent exactly once,** through the existing trial event journal
  (`runtime_core/studies/trial_event_service.py`).

**Value nodes** come from plugins that declare readable values: a key, the stream and channel,
the unit and a maximum age.
- The engine reads them from the same LSL stream that is recorded, never from the dashboard live
  view, so every decision can be recomputed from the XDF file afterwards.
- A value older than its maximum age counts as missing. A waiting node then takes its `timeout`
  exit.

**Event nodes** are "something happened" signals from a device, for example "AM Hub scene
ended". They travel as a new kind of message from the plugin process to the server, next to the
messages `plugin_framework/process_host.py` already accepts.

### 5.4 Logic nodes in V2.0

| Node | What it does | Exits / outputs | Notes |
| --- | --- | --- | --- |
| Branch | If/then: compares a value with a set value | `true`, `false` | Operators: equals, not equals, <, >, ≤, ≥, contains, is empty |
| Switch | Picks one of several paths by value | one per case, `default` | For example by group or by answer |
| Randomise | Sends each participant down one of several paths | one per group | Weights. Mode *random* (seeded) or *balanced* (ledger, see 5.5) |
| Counterbalance | Assigns one condition from a list, for example a Latin-square row | `next` + data output `condition` | Balanced through the ledger |
| Order | Visits several sub-paths once each, in random order | one per sub-path, `done` | Like Gorilla's Order node |
| Repeat | Runs a sub-path N times, or until a condition holds | `body`, `done` | A maximum is required. Pass number is stored. |
| Wait | Pauses for a set time | `next` | Shows a text to the participant |
| Wait until | Waits until a condition holds for a set time | `met`, `timeout` | A timeout is required. Made for live values. |
| Threshold | Turns a number into yes/no (above or below a limit) | `result` | Mirrors AM Hub `graph_threshold` |
| Window | Rolling mean, minimum or maximum of a number over some seconds | `value` | Mirrors AM Hub `graph_window` |
| Read value | Fetches an earlier node's output by name | the value | Saves long wires across the canvas, like a variable in Blueprints |

As in Gorilla, Randomise, Order and Counterbalance inside a Repeat decide once and keep that
choice in every pass.

### 5.5 Research rigour

"It is still for studies" means every decision must be traceable, repeatable and checked before
a single participant sees it.

- **Everything is logged.** Each node visit, decision (with the values it used), random draw,
  command result and live value used goes to three places:
  - the session journal;
  - an LSL marker, so it sits in the XDF next to the signals;
  - `meta/flow-trace.jsonl` in the session folder.
- **Randomness can be repeated.**
  - Each session gets a random seed, which is journaled.
  - Draws are computed from the seed, the node and the visit with a hash function, not with
    Python's `random`, so the same seed always gives the same path.
  - For pilot runs the operator can fix the seed.
- **Counterbalancing never mixes sessions.**
  - Balanced allocation needs a counter shared by all sessions of a study. It lives in
    `<data>/<study>/_allocation/<node>.jsonl`.
  - It holds only slot, condition and session id. It never holds answers.
  - A slot of an aborted session is released or used up, as the node's settings say.
  - This is the only state shared across a study's sessions, and it is deliberately this narrow.
    The rule "no data ever carries from one session into the next" stays.
- **Validation before a run can start.** Errors block the start; warnings only inform.
  - Every path from Start reaches an End.
  - Every loop has a maximum.
  - Every path to *End (complete)* passes exactly one participant-ID card.
  - *Begin recording* comes after the ID card and before every node that needs a recording.
  - Every command and every *Wait until* has a timeout, either wired or through the operator
    fallback.
  - Every data input has a source or a set value, and the source runs before it on every path.
  - Every device role the study uses is assigned.
  - No node is unreachable.
- **Dry run in the editor.**
  - The researcher steps through the study without a tablet, choosing answers, simulated device
    values and a seed, and sees the path it takes highlighted.
  - A checklist shows which exits the dry runs have covered so far.
- **Frozen study versions.**
  - A running station uses a study revision. Editing creates a new revision, and each session
    records which revision it ran.
  - This also ends today's rule that a study cannot be edited while anything runs
    (`study_busy_reason` in `runtime_core/studies/study_config_service.py`).
- **Export for papers.**
  - The graph can be exported as an SVG figure for preregistration and the methods section.
  - The session view's methods sentence (today: software and plugin versions) also names the
    study revision and the randomisation scheme.
- **Live-value conditions are a methodological choice.** They get freshness limits, required
  timeouts and logged values. The researcher guide advises preregistering thresholds.

### 5.6 The editor

- **One shared canvas.** `plugins/destinations/notion_upload/ui/node-canvas.js` (self-built, no
  dependencies, its CSS already in core `admin.css`) moves to
  `apps/ui/scripts/shared/node-canvas.js`. The Notion configurator and both new views use it. It
  gains:
  - separate styles for flow and data ports;
  - inputs that accept several flow wires;
  - group frames;
  - a search palette (double-click on empty space, as in Grasshopper);
  - error badges on nodes;
  - undo, zoom-to-fit and SVG export;
  - automatic layout for migrated 1.x studies.
- **Look:** like Grasshopper. Boxes have inputs on the left and outputs on the right, wires are
  curved, and colour shows the kind (cards, logic, devices, recording).
- **Card settings:** opening a card node opens the same settings panel as in today's list.
- **The list view stays.** It shows the cards in reading order.
  - In a straight chain, cards can be added, moved and deleted there, and the chain is re-wired
    automatically.
  - Where the flow splits, the list shows the paths as sections and points to the node view for
    changes.
- **File size:** a front-end file may have at most 1,000 lines (`tools/measure_structure.py`).
  The editor is split into small modules from the start.

### 5.7 On the tablet

- **The server decides what comes next.**
  - A new flow engine in `runtime_core/flow/` walks the graph for each session.
  - The tablet sends the answer and gets back the next card or a waiting screen.
  - Today the tablet already sends a checkpoint to the server on every Next. What is new is that
    it must wait for the reply before showing the next card. On a lab network that is a few tens
    of milliseconds; M2 measures it.
- **One card at a time.** Today all cards are built when the study loads (`study-controller.js`).
  In V2 the tablet builds only the card it is told to show. Card modules are still loaded once.
- **Waiting screens.** During a command or a *Wait until*, the tablet shows the plugin's text for
  the participant and asks the server again (long polling). No new push channel is needed.
- **The Back button** goes back only to cards shown since the last non-card node. Once a branch,
  a random draw, a command or the recording start has happened, earlier cards are closed. This
  keeps every decision final and the data simple.
- **The progress bar** shows done ÷ (done + longest possible remaining path). It never moves
  backwards, except on Back.
- **Loops and card state.**
  - Every visit of a card gets its own key, so a card shown again in a loop starts empty.
  - Answers are stored per pass (`<card>#2`).
  - Outside loops the `#1` is dropped, so a migrated linear study keeps its 1.x column names.
- **Reload or crash.** The server rebuilds the session from its journal, and the tablet continues
  at the current node.

### 5.8 In the code

- **Study format v2:** `{schema_version: 2, study_id, flow, device_roles, study_settings}`.
  - Card settings live inside their nodes, and node ids are stable.
  - `device_roles` example: `{"eeg": {"plugin": "brainbit", "label": "EEG headband",
    "required": true}}`.
- **Migration from 1.x**, a step in `runtime_core/studies/`:
  - `questions[i]` become card nodes in one chain Start → … → End.
  - *Begin recording* goes right after the ID card.
  - The finish card keeps the role `closing` and is shown after End, as today.
  - `cover_page` becomes an info card.
  - The sensors a study enabled become device roles.
  - Version-1 `.study-runner` files are migrated on import and keep a `migrated_from` note.
- **Answers keyed by node and visit** instead of `q{index}`. This touches:
  - `runtime_core/studies/validation.py`;
  - the checkpoints in `apps/server/routes/results.py`;
  - `runtime_core/studies/results_service.py`;
  - `runtime_core/studies/card_summary_service.py`;
  - the Notion export mapping, whose `card[<index>]` sources become node ids
    (`plugins/destinations/notion_upload/mapping.py`, MAJOR).
- **Card roles instead of card names in the core.** Today core code names the participant-ID,
  finish and stimulus card types directly (for example in `validation.py`, `results_service.py`
  and `study-controller.js`), which CONTRIBUTING.md §7 forbids. In V2 card manifests declare a
  role, and the core asks for the role:
  - `participant_identity`;
  - `closing`;
  - `timed_trial`.
- **Split `study-controller.js` first.** It has 968 of its 1,000 allowed lines.

## 6. Feature 3: several stations in parallel

### 6.1 For researchers

Four new things:

- **Device library** (dashboard → Devices).
  - Every physical device is registered once, with a label ("BrainBit A"), its type, what
    identifies it (serial number, address or URL), and whether it is *personal* or *shared*.
  - Personal is the default. Sensors that record a person (BrainBit, heart rate, tablet camera)
    are always personal.
- **Device roles in a study.** A study says what it needs, without naming a concrete device:
  "EEG headband (BrainBit, required)", "room hub (AM Hub, optional)". The sensors a 1.x study
  enabled become roles automatically.
- **Stations.** A station is a lab place: a paired tablet plus the devices assigned to it. It
  runs one study at a time, one participant after another.
- **The dashboard node view.**
  - Device nodes are on the left and station nodes on the right. A station node shows its study
    and has one input per device role. Wiring a device to a role assigns it.
  - A personal device can be wired to only one running station; a shared device to several.
  - Nodes show live state: connected, signal, calibrated, recording. A station also shows its
    current participant, card and progress.
  - The next step is a highlighted button, the same guided set-up as today: Search → Measure
    contact → Initialize. Clicking a device opens its familiar connection panel.

Day-to-day use:
1. Register the devices once.
2. Pair each tablet with a station: the station shows a code and a QR code, and the tablet
   scans it. The pairing survives reloads and restarts.
3. For each station, choose a study, wire the devices and follow the highlighted button until
   the station shows "Ready".
4. Start. Each station runs on its own, with pause, abort and next participant per station.

What the data shows:
- Each session folder records the station, the device labels and serial numbers, the role
  assignments and the study revision. The methods section can then say which device recorded
  whom.
- Each session's XDF contains only its own devices, its own markers, and the shared room devices
  wired to its station.

### 6.2 In the code

Most of today's "one at a time" sits in a few server-wide values. Each gets a replacement per run
or per session:

| Today: one at a time | Where | V2 |
| --- | --- | --- |
| One run state, `<data>/runtime/study_run_state.json` | `runtime_core/studies/study_run_state_service.py` | A run registry, with one record and one lock per station run |
| One loaded study, `<settings>/study_config.json` | `runtime_core/studies/study_config_service.py` | A study library with revisions. Runs keep their revision. |
| `ACTIVE_STUDY_*` and `SESSION_SENSOR_OVERRIDES` app settings | `apps/server/routes/helpers.py` | A context object per session |
| `trial_service._RUNTIME` | `runtime_core/studies/trial_service.py` | Built per call from the session context |
| The `single_tablet` gate | `runtime_core/studies/study_client_service.py` | Tablet pairing. A station token comes with every request. |
| One marker stream (`study_runner.markers`) and one clock-diagnostics stream | `data_core/host/markers.py`, `clock_diagnostics.py` | One pair per session, closed when its recording is frozen |
| `current_status()` reports only the newest recording | `data_core/host/recording_runtime.py` | Status per session |
| One lock around session start, trial prepare and trial start | `apps/server/routes/study.py` | Locks per run. A global lock only for claiming devices. |
| No study edits while anything runs (`study_busy_reason`) | `study_config_service.py` | Editing creates a new revision; running sessions keep theirs |
| `SensorFlushService` writes each plugin's history into every active session | `data_core/host/sensor_flush_service.py` | Bound to the session's own devices, or removed if it is no longer needed |
| Participant uploads (camera frames) are not bound to a session | `apps/server/routes/plugins.py` | Resolved from the tablet's station to its session and device |
| Trial events and markers go to every enabled plugin | `plugin_framework/registry.py` | Only to the devices assigned to that session |

**Device instances.**
- **One child process per instance.** Today one plugin means one device and one child process
  (`_RUNTIMES` in `plugin_framework/process_host.py`). Plugin code keeps its state in
  module-level variables, for example the BrainBit adapter. With one process per instance that
  code stays correct without a rewrite.
- **Stream identity.**
  - Today every stream has a fixed `source_id` that must be unique, and the recording worker
    refuses duplicates (`data_core/worker/lsl_recording.py`).
  - V2 appends the instance, for example `study_runner.brainbit.eeg@b`.
  - The first (default) instance keeps the bare id, so a single-device set-up records exactly as
    in 1.x.
  - This is built in one place, `plugin_framework/sensor_streams.py`.
- **Settings per instance.** Device settings move from one section per plugin in
  `<settings>/hardware_settings.json` into the device library. Runtime files go into one folder
  per instance.
- **Routes and dashboard tiles** carry an instance id.
- **Scans** skip devices that another instance has already claimed.
- **The health poller** (`data_core/host/plugin_health_poll_service.py`, 4 workers by default)
  grows with the number of instances.
- **Finalization runs on one thread** (`runtime_core/delivery/finalization_service.py`). With
  three stations, XDF merges may queue. The soak test measures this before anything changes.

**Saved data for the dashboard view.** `<settings>/deployment.json` holds the device library,
the stations and their assignments, in the same graph format:
- device-instance nodes, each with one typed output;
- station nodes with one input per device role;
- wires that are the assignments.

**Tablet pairing.** The station shows a code and a QR code; `apps/ui/scripts/shared/qr-code.js`
already exists. The tablet keeps a station token and sends it with every participant request,
and the server resolves station → run → session.

### 6.3 AM Hub

The AM Hub keeps its own graph editor and runtime: repository `MRG-ParasiteV2`, branch
`feature/amhub-architecture-v1`, see `docs/AMHUB_GRAPH_V1.md` there. Study Runner talks to it
only through its `am_hub` plugin.

**Nodes the `am_hub` plugin delivers:**
- "Start hub project" and "Stop hub project", with the project chosen from a list the hub
  provides;
- "Wait for hub event", one for each of the hub's `event` ports;
- values (radar distance, heart and breathing rate) for live conditions.

**Needed on the hub side** — a small API, agreed between the two repositories:
- list projects with name, active revision and event ports;
- start and stop a project for a session, with session and event ids;
- events through the existing event stream.

The hub project's active revision is recorded in the session data.

**Shared graph format.**
- Both repositories keep the same JSON shape and port types.
- Shared test files list graphs that both validators must accept or reject.
- One difference must be agreed: the AM Hub rejects all cycles, while Study Runner allows them
  through Repeat and Order nodes.

**Sharing.** The hub's own rule fits V2: sensor streams are shared, and an output device belongs
to one project at a time. The hub's bio radar measures a person, so it is personal by default.
Presence and room values can be marked shared.

## 7. Plugin API 6: one pattern for all plugins

All plugins move together from API 5 to API 6 (`SUPPORTED_PLUGIN_API_VERSIONS` in
`contracts/manifest.py`). There is one pattern, one template per kind and one test per rule.

| Kind | New in the manifest |
| --- | --- |
| Cards | `card_contract` v2: typed answer outputs (number, boolean, text, choice, with unit) and a `role` (`participant_identity`, `closing`, `timed_trial`). Normalisation receives the node id instead of the card index. |
| Sensors | `values` (key, stream, channel, unit, maximum age), `events`, `commands` (payload schema, `completes_when`, `fails_when`, timeout, participant text, allowed while recording, `makes_device_ready`), `instances` (maximum, personal or shared) |
| Outputs | `commands` (start, stop, send …), `instances` |
| Logic (new) | `ports`, `settings` and a pure `evaluate` |

Rules:

- **Flow commands use a new driver operation, `node_command`,** separate from `admin_action`. The
  operator lock that refuses dashboard actions during a recording (for example in
  `plugins/sensors/brainbit/plugin.py`) stays. Declared flow commands may run during a session.
- **Commands answer at once and finish in the background,** because a plugin process handles one
  request at a time (`plugin_framework/driver_runtime.py`).
  - The engine reads completion from the device status.
  - A new `status_changed` message from the plugin makes the server read the status at once,
    instead of at the next poll.
- **Reused, not rebuilt:**
  - the closed payload schemas of admin actions;
  - the `start_condition` evaluator, generalised into one status-condition helper in
    `contracts/`;
  - the standard `connection.setup` block (`plugin_framework/sensor_connection.py`);
  - the trial event journal.
- **Templates** in `tools/plugin_templates/`: a new `logic/` template, and `cards/`, `sensors/`
  and `outputs/` are updated, together with `tools/plugin_sdk.py`.
- **Tests that hold every plugin to the pattern:**
  - every node declaration validates;
  - every command has finish, failure and timeout rules;
  - every value has a unit and a maximum age;
  - every card starts empty on each visit;
  - no core module names a plugin or a card type.
- **Versions** (CONTRIBUTING.md §11): every plugin changes, so every version rises.
  - Card plugins take a MAJOR step, because answer keys change from index to node id.
  - Notion takes a MAJOR step, because its mapping moves to node ids.
  - All others rise by their own change.

## 8. Data and compatibility

**What stays the same:**
- the session folder layout `<study>/<participant>/<UTC>__<session>/`;
- one XDF per session, the LSL streams, and stream ids for single-device set-ups;
- the column names of migrated linear studies (no `#1` outside loops).

**What is new in each session:**
- `meta/flow-trace.jsonl`, with visits, decisions, draws, command results and the live values
  used;
- `visits` and `decisions` in the result;
- the station, device instances with serial numbers, role assignments and the study revision in
  the provenance record;
- a marker stream per session.

**Studies:**
- The format has `schema_version: 2`.
- V2 opens 1.x studies and migrates them, keeping the original file.
- 1.x refuses V2 studies with a clear message ("needs Study Runner 2").

**Analysis scripts:** answer keys, the flow trace and instance suffixes on stream ids are new.
The researcher guide gets a "1.x → 2.0 data" page.

**Comparing sessions across software versions.** Every session already records the Study Runner
version and the version of each plugin that took part (`meta/manifest.json`,
`provenance.software`). To judge whether two sessions are comparable, compare those versions.
The rules below are a recommendation for researchers. Study Runner does not block anything.

| Step | Example | Meaning for comparability |
| --- | --- | --- |
| PATCH | 1.2.3 → 1.2.4 | Bug fix. Not relevant for comparability. |
| MINOR | 1.2.4 → 1.3.0 | Larger change. Read the changelog and check whether it touches the study. |
| MAJOR | 1.3.0 → 2.0.0 | Do a comparability check before pooling data. |

Plugin versions follow the same steps. A plugin MAJOR step always means the recorded data
changed meaning (CONTRIBUTING.md §11), so sessions that differ in a recording plugin MAJOR
version should not be pooled without checking. The researcher guide gets this table.
Sessions recorded before version tracking are marked `"partial": true`.

## 9. Branch and release strategy

- **Branches.**
  - The `v2` branch starts from `main`. `main` keeps getting 1.x fixes and stabilisation.
  - Fixes land on `main` first. `main` is then merged into `v2`, at the latest after every 1.x
    release, so the branches do not drift apart.
  - `v2` is merged into `main` only for 2.0.0.
- **Merge conflicts in generated files are not fixed by hand.**
  1. Take the code from both sides.
  2. Run `python tools/plugin_versions.py --update`.
  3. Last, run `python tools/measure_structure.py --write-baseline`, then `--check`.

  For a plugin version conflict, take the higher version and raise it by the rule in
  CONTRIBUTING.md §11.
- **Groundwork on `main`.** Some V2 groundwork changes no behaviour and also fixes an existing rule
  violation, such as core naming card types, or the node canvas living inside one plugin. It may
  land on `main` first to keep `v2` small, but only after the user agrees to each change, because
  `main` is being stabilised.
- **CHANGELOG.** `v2` keeps its notes in their own section at the top ("2.0.0, unreleased") to
  avoid conflicts with 1.x entries.

**The release tooling must change before the first V2 build is published.** Today:
- `release_tools/release-study-runner.mjs`:
  - refuses to run on any branch but `main`;
  - accepts only `X.Y.Z` versions;
  - pushes the release commit to `main`.
- `.github/workflows/release.yml` publishes every tag with `--latest`.
- The in-app updater, the manager and the recording-core set-up download from
  `releases/latest/download/`:
  - `software/study_runner/runtime_core/settings/update_service.py`;
  - `tools/study_runner_manager.py`;
  - `tools/setup_recording_worker.py`.

So a V2 test build released today would be offered to the study Mac as an update. Before any V2
release:
- The script accepts `2.0.0-alpha.N` and `2.0.0-beta.N` from `v2` and pushes to `v2`.
- The workflow publishes tags with such a suffix as `--prerelease`, without `--latest`.
- A test proves that a stable installation is never offered a prerelease.
- The study Mac stays on 1.x until 2.0.0. V2 test builds run on a separate test machine.

## 10. Milestones

The riskiest work comes first. Every milestone ends with the checks from `AGENTS.md` green. From
M2 on, every milestone also ends with a prerelease.

### M0 — spikes and branch set-up (go / no-go)

- **S1 Bluetooth.**
  - Three BrainBits and three tablets for two hours, on the study Mac and on the Windows PC,
    scanning and calibrating in parallel.
  - Pass: under 0.1 % lost samples, and one band reconnecting does not disturb the others.
  - Otherwise: one Bluetooth adapter per band, or fewer stations.
- **S2 Recording.**
  - Three recording workers at once, with per-session marker streams and one shared stream.
  - Pass: each XDF holds only its own streams, and CPU, disk and merge time are acceptable.
- **S3 Journal replay.** Rebuild a migrated 1.x session from its journal and get the same result.
- **Branch set-up:** the prerelease tooling. The `v2` branch and CI on it have existed since
  2026-10-10.

**Exit:** S1–S3 pass, or a fallback is chosen. A test prerelease is published as a prerelease and
is not offered as an update.

### M1 — contexts instead of globals (still linear studies)

- Run registry, per-session context, device instances, per-session marker streams.
- Events sent only to assigned devices, and tablet pairing.
- A simple list for assigning devices.

**Exit:**
- Two stations run two different linear studies at the same time.
- Tests prove that no session sees another session's data or events.
- The 1.x test suite passes on migrated studies.

### M2 — flow engine and feature 1 → `2.0.0-alpha.1`

- Study format v2 and its migration.
- The server-side engine with its journal, and one card per visit on the tablet.
- Cards before the ID, and *Begin recording* as a line in the list and as a node.
- Answers keyed by node and visit, the Notion mapping by node id, and card roles.

**Exit:**
- A migrated 1.x study produces the same data as in 1.x (a fixed comparison test).
- A server restart in the middle of a session resumes correctly.
- A study with consent before the ID works, and "no" leaves no folder.

### M3 — node editor and logic plugins → `alpha.2`

- Starts after the editor part of the UX round (section 11).
- The shared canvas.
- The node view (every card a node) next to the list view.
- The validator and the dry run.
- The logic plugins Branch, Switch, Randomise, Counterbalance, Order, Repeat, Wait and Read
  value, with seeds and the allocation ledger.

**Exit:**
- The same seed gives the same path.
- The validator catches every rule in 5.5.
- A researcher builds the example study without help.

### M4 — device nodes and live conditions → `2.0.0-beta.1`

- Plugin API 6 for sensors and outputs.
- BrainBit calibration inside the flow, and value nodes.
- Wait until, Threshold and Window.
- AM Hub start, stop and wait, together with the hub-side API.

**Exit:**
- The breathing → calibrate → continue example works on real hardware.
- "Relaxation above X for 10 s" works.
- A stale value takes the timeout exit.

### M5 — dashboard node view and release → `2.0.0`

- Starts after the dashboard part of the UX round (section 11).
- The live assignment view and shared room devices.
- A two-hour soak test with three stations.
- Failure tests: Bluetooth loss, tablet reload, and a server restart mid-flow.
- Operator and researcher guides, the file guide and migration notes.

**Exit:** the soak and failure tests pass, the docs match the software, 2.0.0 is released, and
the study Mac is updated.

## 11. The UX round

The screens described in this plan are a first sketch. A UX round decides how they really look
and behave:
- before M3 for the editor;
- before M5 for the dashboard view.

**Rules that stay,** because they came from lab tests:
- In guided set-up, the next step is the highlighted button. Buttons that cannot help yet are
  greyed out with a reason.
- Switches and status lines show the real state, and messages say what actually happened.
- Nothing searches or connects by itself while the operator sets up.

**Questions for the UX round:**
- **Editor:** how a researcher without coding experience finds nodes and connects them; when to
  use the list and when the node view; how errors and the dry run are shown.
- **Large studies:** how a 40-card questionnaire stays readable when every card is a node (group
  frames, collapsing, zoom levels).
- **Dashboard:** how the node view and the guided step-by-step set-up fit together; how three
  stations look on one laptop screen.
- **Tablet:** the waiting screens during calibration or *Wait until*, and what the participant
  sees when something fails.
- **Pairing:** how a tablet joins a station.

**How:**
- clickable prototypes, without a build step and on the same canvas;
- walkthroughs with researchers and with an operator in the lab;
- the example study from section 1 as the test case.

## 12. Risks and open questions

**Risks and what limits them:**

1. **Bluetooth capacity** for several BrainBits on one computer → S1 comes first, with fallbacks.
   The vendor's NeuroSDK 2 notes already cover several devices at the same time.
2. **Waiting for the server on every Next** over lab Wi-Fi → M2 measures it.
3. **Large graphs overwhelm researchers** → validation, dry run, group frames and example
   studies, and the list view stays.
4. **Editor size against the 1,000-line limit** → small modules from the start.
5. **Concurrency bugs after removing the globals** → M1 does only this, with leak tests.
6. **Closed-loop latency and methodology** → freshness limits, timeouts, logged values and
   guidance in the researcher guide.
7. **Branch drift** → merge `main` into `v2` after every 1.x release.
8. **The graph format drifting from the AM Hub** → shared test files in both repositories.
9. **Moving all 21 plugins at once** → templates and tests first, then one plugin at a time.

**Open questions, decided during the milestones:**

- **Counterbalancing by participant number.** The ID card stores a hash of its fields, not a
  running number. Which field gives the number, or is the ledger the only mode?
- **Tablet camera (`camera_emotion`) per station:** one instance per tablet?
- **How many stations one computer supports:** decided by S1.
- ***End (discard)* after the ID card:** does it behave like today's consent withdrawal? Before
  the ID, nothing is written anyway.
- **The dashboard node view on small screens:** is a list fallback needed?
- **The UX of both node views, the tablet waiting screens and pairing:** decided in the UX round
  (section 11).

## 13. Not in V2.0

- **Coupled sessions:** several participants in one session, with a shared flow and "wait for
  everyone" points (pairs of participants, hyperscanning). Labvanced shows one way to do it,
  with role ids and synchronised screens. Nothing is built or prepared for it.
- **Several computers:** devices attached to other computers in other rooms. LSL already works
  across a network, but the plugin processes would need a network link. Nothing is built or
  prepared for it.
- **A real desktop app with an integrated kiosk view:** an installed PySide6 program with its own
  windows, a controlled full-screen window for participant views on the study computer, and a
  dialog when Study Runner is already running (section 13b). Nothing is built or prepared for it.

## 13a. Vision: reliable over-the-air updates

Status: vision, not committed scope. Nothing here is scheduled and no milestone depends on it.

**Today.** The dashboard checks the latest GitHub Release, downloads the source archive,
verifies its SHA-256, swaps the program files, runs the install script, restarts, and rolls
back on failure (`docs/release-and-update.md`). The mechanism works. What is missing are the
guarantees around it.

**Goal.** An operator who is not technical presses one button and the installation moves to a
newer version without losing data, and with a way back.

**Principles.**
- **Versions are recorded, comparability is the researcher's call.** Every session records the
  Study Runner version and all plugin versions. Whether two versions are comparable is a
  recommendation (section 8). Updates are never blocked by it.
- **No update during a recording.** An update waits until no station records. On a computer
  with several stations it waits until all are idle and names the one blocking.
- **Apply, verify, or go back.** After the restart the app runs its own self-check
  (`server.py --self-check` today) automatically. If it fails, the previous version is
  restored without operator action, and the dashboard says what happened in plain language.
- **Stable install location.** The install folder has a fixed, version-free name, and the
  desktop shortcut keeps working across versions. The version is shown in the dashboard, not
  in a folder name. Today the release archive's root folder carries the version, the updater
  never renames it, and the macOS shortcut stores the absolute folder path.
- **Quiet and visible.** The update downloads in the background and is applied when the
  operator chooses. Applying shows one progress screen, not a Terminal window that must stay
  open.
- **Works on a bad network.** Everything an update needs is fetched and checked before the old
  version is touched, or comes from an offline bundle for lab networks.
- **Channels.** Stable installations are never offered prereleases (section 9).
- **One way back.** The last working version stays available with a single "go back" action.
  Study data is never touched by updates.

**To be decided later.**
- **Signed releases.** Today the checksum comes from the same release as the archive, so it
  shows that the download is intact, not that it is authentic. Whether to sign releases, and
  with which key handling, is not decided and is not part of any milestone.
- Whether the Python environment is rebuilt on update or shipped prebuilt per platform.
- How study files with a `schema_version` change are migrated on update.

## 13b. Vision: a real desktop app with a kiosk view

Status: vision, not committed scope. Nothing here is scheduled and no milestone depends on it.
The idea comes from the planning of MRG TRACE, the proposed successor of Study Runner
(2026-11-01, revised 2026-11-02). Nothing is built or prepared for it in V2.0.

**Goal.** A program in the way users understand it, on Windows and macOS: an installed app
with its own window, started from the Start menu or the Applications folder, with no terminal
window and no browser tab for the operator. Today Study Runner behaves like a server. It
starts from a script, keeps a terminal open and is used in a browser.

**Idea.** Inside that app, the participant views, stimuli and questionnaires of the study
computer are shown in a controlled full-screen window, the kiosk view. Navigation follows the
study session. Browser and stimulus events are logged with timestamps on the shared time base.

**Scope.**
- In: the app shell, the operator window and the kiosk view on the study computer, for example
  on a second screen.
- Out: external tablets and iPads. They stay normal browser clients, paired by station code
  (sections 5.7 and 6). The tablet camera stays on the tablet.

**Why.**
- In a normal browser a participant can leave the study page, resize the window or reload it.
- The participant sees the cards on the tablet, so the delay between a server event and the
  moment the participant sees it is the timing question that counts. It has three parts: network
  delay and jitter, the offset between the server clock and the tablet clock, and the screen.
  Frame-accurate stimulus timing is not a goal for the current studies. The honest answer is to
  log times together with an uncertainty and not to promise exact onsets. This follows the
  Recorder Core rule of honest timing uncertainty.
- Different browser engines on different computers make rendering and timing differ between
  sites.

**Options for the app shell.**

| Option | For | Against |
|---|---|---|
| Electron shell with the Python server as a child process | One Chromium on every system, so rendering and timing are the same. Windows, tray, kiosk windows on a second screen, installers and updater in one place. The Python code stays. | A second runtime (Node), a large package, Chromium security updates become our duty, signing is needed. |
| PySide6 with QtWebEngine (Chromium inside Qt), all in Python | One language, one engine everywhere, native windows. No H.264 decoder has to be bundled. | Qt licence duties (LGPL). Open Qt builds do not play H.264 or AAC, so desktop stimuli need WebM or a conversion on import. On macOS the QtWebEngine helper process needs extra entitlements for notarization. QtWebEngine follows Chromium with a delay, a large package, Qt skills are needed. |
| Tauri shell with the Python server as a sidecar | Small package, same stack as OCYRA and TAZAMA. | WebView2 on Windows and WKWebView on macOS are two engines, so a study could behave differently per system. A Tauri wrapper existed in 1.x and was dropped, the repo does not record why. |
| Pinned Chromium started by the server and controlled over the DevTools Protocol | The smallest change, no new shell. | Still no app of its own: no installer and no window the user knows. Only the kiosk view improves. |

**Direction (owner, 2026-11-02): PySide6 with QtWebEngine.** A clear Python app, open source,
one language from the server to the window. The tablets are not affected: Safari on the iPad
plays MP4 with the system decoders.

| | PySide6 + QtWebEngine | Electron |
|---|---|---|
| Languages | Python only | Python server plus Node and JavaScript for the shell |
| Engine | One Chromium everywhere (follows upstream with a delay) | One Chromium everywhere (close to upstream) |
| H.264 and AAC on the desktop | Not in open builds; WebM or conversion on import | Included; patent question stays open |
| Licence | LGPL v3 for Qt and PySide6: notices, replaceable libraries (one-folder build, not one file) | MIT, plus Chromium licences |
| Packaging and update | No standard updater; the existing source updater or a small own one | Mature builders and an auto-updater |
| macOS signing | Possible; the QtWebEngine helper needs extra entitlements | Well documented |
| Package size | Large (Qt plus QtWebEngine) | Large (Chromium plus Node) |

Electron stays the fallback. Exit criterion: the spike shows that QtWebEngine cannot be signed
and notarized on macOS, or cannot show the kiosk view on a second screen reliably.

**Media on the study computer (owner, 2026-11-02): convert on import.** Researchers keep
importing MP4 or other files. The app converts them once into WebM (VP9 or AV1 video, Opus
audio), which QtWebEngine plays without patent-encumbered codecs.
- The original file stays untouched next to the converted one. Both get a SHA-256.
- The study package records the conversion: tool and version, settings, source and target
  hash, and duration and frame rate before and after. A study run uses only the converted file.
- Decoding the source should use the system decoders where possible (AVFoundation on macOS,
  Media Foundation on Windows), so that no H.264 decoder has to be bundled. Encoding uses
  libvpx (BSD) or SVT-AV1 (BSD). Whether the system decoders cover all needed formats on both
  systems is part of the spike. If a bundled decoder is needed after all, the patent question
  goes to the owner before release.
- Conversion runs in a background job with progress and can be cancelled. It never runs during
  a recording.
- A quick check after conversion: duration, frame count and audio length within a stated
  tolerance, otherwise the import fails with a clear message.

**Spike step 1: sign and notarize on macOS.** This comes first because it is the exit
criterion. Done on a real Mac, Apple Silicon and Intel if possible.
1. Build a minimal PySide6 app with one QtWebEngine window that loads the local admin page, as
   a one-folder `.app` (PyInstaller or `pyside6-deploy`, both tried if the first fails).
2. Ad-hoc sign and run it with hardened runtime. This needs no Apple account and shows early
   whether the QtWebEngine helper starts.
3. Sign with a Developer ID, hardened runtime on, inside out (helpers and frameworks first,
   app last). Give the `QtWebEngineProcess` helper only the entitlements it needs and record
   each one with its reason.
4. Notarize with `notarytool`, staple the ticket, check with `spctl --assess` and
   `codesign --verify --deep --strict`.
5. Download the result on a second, clean Mac and open it by double-click. Pass: no Gatekeeper
   warning, the window renders the page, the kiosk view opens on a second screen, the app
   quits cleanly.
6. Record build tool, versions, entitlements, size and the exact commands in a short spike
   report.
Needs: an Apple Developer account (Developer ID certificate). Who holds it is an owner question.
Windows signing follows as step 2 with the same pass criteria.

**Starting when Study Runner is already running.** Today a second start prints "Port is already
in use" in the terminal and quits (`apps/server/application.py`, `_ensure_port_is_free`). In the
app this becomes a dialog.
- *Detect.* An instance file in the data folder records process id, port, version, data folder,
  start time and a random local token. The app checks it, then asks `/api/health` on that port
  whether a Study Runner with the same data folder answers. Only both together count as
  "running". A stale file (process gone, no answer) is removed and the start continues.
- *Ask.* "Study Runner is already running (version, since when). Open it, or restart it?"
  - **Open:** brings the running window to the front, or opens its admin view. No second server.
  - **Restart:** asks the running server to stop through a local-only endpoint that needs the
    token, waits until the port is free, then starts.
- *Never break a recording.* While a station records or a session is active, Restart is not
  offered. The dialog names the station and offers only Open. Stopping a recording stays a
  deliberate action in the admin view.
- *Another program on the port.* If something answers that is not Study Runner, say so plainly
  and offer to start on another free port.
- *One app window.* A second launch of the app hands over to the first one (Qt local socket),
  like any normal program.
- Tests: running, stale file, foreign program on the port, active recording.

**Timing without shortcuts.** These rules cost little now and keep recorded data honest.
- Estimate the clock offset between the server and each tablet with repeated pings. Keep the
  sample with the smallest round trip. Store the offset and its uncertainty (about half the
  round trip) with the session.
- For each time-critical event log the server time, the tablet time, the mapped time, the
  uncertainty and the clock source. "Unknown" stays allowed, as in the Recorder Core.
- Hide the network: load the next cards and media on the tablet in advance. For a
  time-critical event send "show at time T" and let the tablet schedule it locally, instead of
  showing it when the message arrives.
- Lab setup: a dedicated router or a wired link, 5 GHz Wi-Fi, all devices on the same network,
  notifications and auto-lock off on the tablet (Guided Access helps).
- Flag sessions with high jitter in the session summary.

**Later, only if a study needs frame-accurate stimuli.** A stimulus module as a plugin that uses
the same event fields: a native player built on a mature library (for example libmpv), the
system decoders for H.264 so that no patent-encumbered decoder is bundled, presentation
synchronised to the screen refresh, and a check with a light sensor. Not planned. The fields
above make it possible later without changing the recorded data.

**Rules for any option.**
- Navigation only to the URLs of the running session. No remote URLs.
- Never lock a participant in. The operator keeps a visible way to end the session, and
  consent withdrawal stays possible.
- No telemetry and no network traffic from the shell itself. Check this with a network capture
  in the spike, because browser builds can contact update or metrics services.
- Local-first, no CDN.

**Legal notes.** These are facts collected for the owner, not legal advice. The owner and the
university should check them before anything is bundled.
- Chromium is open source. Google's own code is BSD-licensed and the bundle contains many
  third-party licences. The most restrictive is LGPL 2.1 (for example FFmpeg). Redistribution
  needs the licence texts to be shipped (`chrome://credits`, or `LICENSES.chromium.html` in
  Electron) and the LGPL parts to stay replaceable.
- Google Chrome, the branded binary, is a different thing. It falls under the Google Chrome
  Terms of Service. Do not bundle it. Use open Chromium builds, Electron or QtWebEngine.
- Do not use the names or logos "Chrome" or "Chromium" for the product.
- Codecs: open Chromium builds leave out H.264 and AAC. The default Electron build includes
  them. For H.264 the patent pool of Via LA lists no royalty for the first 100,000 units per
  year and fees above that. A free research tool looks low-risk, but this is not clarified.
  A simple way out: stimuli in VP9 or AV1 (WebM) with Opus, and a build without the proprietary
  codecs.
- Qt route: PySide6 and QtWebEngine are LGPL or commercial. The LGPL duties (licence notices,
  replaceable libraries) must be met.
- macOS and Windows: a real app needs signing. On macOS that means Apple signing and
  notarization. `docs/release-and-update.md` states that this project holds no such
  credentials today, and the current source release was chosen partly for that reason. Whether
  the group or the university has an Apple Developer account and a Windows signing certificate
  is open.

**Measure first, assume no numbers.**
- Jitter and offset of event timestamps against the shared time base.
- Round trip and jitter between the server and a tablet on the lab network, and how stable the
  clock offset stays over a session.
- The effect of loading cards in advance on the delay the participant sees.
- Full-screen behaviour on a second screen and window focus when the system shows a
  notification.
- Package size, start time and update path of each shell candidate.

**Open questions.**
- Owner: who holds the signing credentials, and who pays for them.
- Spike for PySide6: signed and notarized on macOS, kiosk view on a second screen, package size,
  start time, update path. Electron only if the exit criterion is met.
- The stop endpoint for Restart is a new local control right. Its token handling is part of the
  spike.
- Which events are time-critical in the studies, and how large an uncertainty is acceptable?
- Which source formats must import support, and do the system decoders cover them on both
  systems?
- Platforms: Windows x64 and macOS 13+ like today?
- How the server runs inside the app: child process, ports, restart after a crash.

## 14. Sources

- Gorilla, Experiment Tree nodes:
  <https://support.gorilla.sc/support/tools/experiment-builder/tree-nodes>
- lab.js, flow control: <https://labjs.readthedocs.io/en/latest/reference/flow.html>
- jsPsych, conditional and loop timelines: <https://www.jspsych.org/6.3/overview/timeline/>
  and <https://github.com/jspsych/jsPsych/blob/main/examples/conditional-and-loop-functions.html>
- PsychoPy, Builder Flow: <https://psychopy.org/builder/flow.html>
- Labvanced, event system:
  <https://www.labvanced.com/content/learn/en/guide/task-editor/event-system>; multi-user studies:
  <https://www.labvanced.com/content/learn/en/guide/task-editor/multi-participant.html>
- Unreal Engine, flow control:
  <https://dev.epicgames.com/documentation/en-us/unreal-engine/flow-control-in-unreal-engine>
- Bonsai (Lopes et al., 2015):
  <https://www.frontiersin.org/journals/neuroinformatics/articles/10.3389/fninf.2015.00007/full>
- OpenViBE Designer: <https://openvibe.inria.fr/designer-130/>
- LiteGraph, archived as a standalone project: <https://github.com/Comfy-Org/litegraph.js/>
- NeuroSDK 2 (note on several devices at once): <https://github.com/BrainbitLLC/neurosdk2>
- Two wireless EEG systems recorded with LSL/LabRecorder:
  <https://www.biorxiv.org/content/10.1101/2021.08.04.454932v1.full>
- AM Hub graph: repository `MRG-ParasiteV2`, branch `feature/amhub-architecture-v1`, files
  `docs/AMHUB_GRAPH_V1.md` and `am_hub/amhub/contracts/graph.py`
