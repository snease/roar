# Floorplan branch refactoring record

Started 2026-10-18 on `floorplan-align` at
`ec83f218260847699e0fa9e6b3f56328e56cbd95`. This records the refactoring and
necessity review, including decisions to retain existing code. Implementation
and validation below are complete.

## Review standard

Every component, test and refactoring should earn its review and maintenance
cost. Prefer removing obsolete paths and putting responsibility in an existing
owner over adding a layer. Preserve supported workflows, public APIs, saved
artifacts, rollback guarantees and native ALIGN compatibility. Neither test count
nor line count is a goal. Passing tests alone does not establish good architecture.

For each test, trace **test → behavior → supported entry point → observable
failure**, and decide **keep, retarget, consolidate or remove**. Check:

- Reachability through UI, CLI, documented API, framework callback or worker.
- A meaningful regression and an expectation independent of the implementation.
- Realistic state transitions and a distinct responsibility or boundary.
- Coupling to behavior rather than private names, call order or representation.
- Appropriate cost, deterministic cleanup and an environment that exercises it.

Also trace supported entry points back to tests. Missing static callers are not
sufficient evidence of dead code: documented APIs, Qt paint callbacks and the
worker's `runpy` adapter calls are valid entry points. Use focused fault injection
where useful; no mutation-testing framework was added.

## Refactoring decisions and results

### R01 — Consistent document identity across entry points — done

`launch(path)` discarded the saved path and opened a dirty document; the CLI
separately recorded identity. `Session.from_file` now shares immediate loading
and lazy GUI initialization, then establishes the saved identity/baseline.
CLI, Python factory and `launch(path)` parity are checked in real subprocesses.
Existing GUI document lifecycle and cancellation tests remain.

### R02 — Remove obsolete UI interfaces — done

Removed `_read_roar`, the `refresh_bindings_button` alias, panel `saved_as`, and
the unused panel direct-generate wrapper. Tests use actual source-selection,
Generate and Open controls. Real Save As transaction/ownership coverage remains.

### R03 — Test necessity and regression-value audit — done

All **354 current tests** have dispositions tied to supported entry points and
observable failures. The earlier instrumented 341-test inventory distinguished
fixture calls from test-body calls; subprocess/shell targets were inspected
separately. Execution traces guided review but were not treated as proof that
assertions detect regressions.

Concrete reductions and retargets:

- Consolidated synchronous browser rejection/exception cases; retained async
  process failure as a distinct boundary.
- Removed a mock-echo raw-log assertion; retained actual rendered-log sanitation.
- Moved the Qt-gated saved-source-loader case into the headless workspace's
  invalid-target matrix.
- Removed the skipped checkout missing-GUI duplicate: installed-wheel acceptance
  owns actual missing-dependency behavior; doctor tests own contextual advice.
- Removed a sketch relative-link test duplicated by richer companion integration.
- Removed the redundant pre-accept form-value assertion; checked accepted output.
- Retargeted sketch toolbar cases to button clicks and launch preflight to
  `Session.from_file`; the former `Design.load` spy had become vacuous.
- Gave setup receipts independent required-input expectations and checked all five
  setup inputs. Renamed misleading tests to the behavior they actually assert.

Retained domain errors versus Qt containment, Add-form defaults versus editing
existing rules, native interpretation versus local serialization, and distinct
rollback, cancellation, process-race and ownership-transfer tests. Similar setup
does not imply duplicate responsibility. Selected temporary faults in signals,
preview mode, log sanitization, copy independence, batch atomicity and row identity
were detected; no repository mutation-testing dependency was introduced.

### R04 — One coherent run/document snapshot per refresh — done

The old 250 ms timer captured the project twice, polled three times and calculated
tool identity three times. The panel now captures one project and one
`RunSnapshot`. The runner advances the current worker once and calculates current
fingerprint/tool identity once; historical descriptors do not poll, signal or
write records. Legacy freshness uses each historical run's own captured document.
Existing history getters remain small wrappers. Real-child checks cover efficiency,
read-only snapshots and successful-preview retention after failure.

### R05 — Explicit internal snapshots — done, with boundary exception

Dialog edits, undo history, constraint forms and handoff preparation now use a
detached model snapshot instead of persistence decoding and SPICE reparsing.
It preserves incomplete drafts, nested independence, stable IDs and effective
enabled flags, including supported direct constraint-list assignment.

**Retain persistence decoding at run capture.** Independent review demonstrated
that valid direct device edits can disagree with serialized parameters/overrides:
geometry validation passes but the captured floorplan cannot reopen. The capture
boundary rejects that inconsistency. Handoff retains recipient-reopen validation.
Do not mechanically replace every serialization round trip.

### R06 — Model-owned batch binding and constraint edits — done

Source migration stages all explicit bindings and rebuilds once, preserving atomic
failure, units, manual values and live/linked semantics. A ten-binding UI refresh
now performs one rebuild and no persistence decodes, previously 12 rebuilds.
Constraint replacement/duplication owns row identity and enabled state in the
model while preserving opaque sibling rules. Review and existing tests caught
and corrected Save As history and effective-flag adaptation errors.

### R07 — Environment-appropriate diagnosis — done

Removed generic Ubuntu package advice. Diagnostics distinguish missing native
libraries, display failures, managed versus custom ALIGN, and checkout versus
installed-package repair. WSL advice requires WSL context. Documented Linux
x86-64 runtime constraints and the explicitly selected Ubuntu CI host remain.

### R08 — Shared option definitions — done

Defaults live in the profile and limits/validation in `run_options.py`; widgets
use the same definitions. Formatting and Qt construction stay in the UI.
No settings framework was added.

### R09 — Validation separate from storage preparation — done

Configuration validation no longer creates directories or probes writability by
writing a file. Start/open explicitly prepare storage. Inspection and saving an
unconfigured authoring draft retain their availability without native tools.

### R10 — Test doubles with narrow responsibilities — done

Replaced `FakeWorkspace`'s duplicated save/switch logic with real `DesignWorkspace`
and in-memory callbacks/spies. Removed its production `has_content` fallback.
Runner doubles now follow preview/open and snapshot contracts; actual subprocess
and native acceptance tests own process behavior they cannot prove.

### R11 — Redundant mechanics and atomic writers — decision complete

Removed the runner's no-op `except Exception: raise` block. Retained local writers:
model/workspace use private temporary files; workspace has companion commit hooks;
runner rollback restores multiple files and permissions; the isolated worker has
its own runtime; handoff writes a deterministic ZIP with flush/fsync. A generalized
writer would require policy flags/callbacks or change permissions/durability.

### R12 — Acceptance follows the user workflow — done

Native viewer acceptance now generates and validates a physical preview, then
explicitly opens that same capture. It retains real layout/assets, both Dash
callbacks and capability/Host/Origin/cross-site protections. Visible desktop
acceptance remains distinct from offscreen widget and HTTP/native checks.

### R13 — Shared infrastructure ownership — decision complete

Retain the current headless components. `DesignWorkspace` already owns identity,
baseline, save/switch and rollback through callbacks; Qt owns prompts/rendering.
The model now owns batch edits. Small `probe_result` and `run_records` modules
own actual shared parsing boundaries; the runner owns process lifecycle.

A neutral namespace move alone would not remove a dependency or duplicated
policy. Defer that churn and a new controller/history-store framework until a
concrete additional consumer or responsibility justifies extraction.

### R14 — Compatibility commitments — decision complete

Retain documented floorplan formats 1–6 and mutable public constraint APIs. The
checked-in example is v6 and an existing example outside this checkout uses v3.
Existing local run history contains seven v1 and five v2 manifests, including
`generate`, `preview` and `reopen`; all pass the new decoder. Preserve reading,
reconnecting and stopping those runs. A reduced native geometry fixture manifest
is not an ownership record and should not be validated as one.

### R15 — Contribution reviewability — done for this pass

Use the review map below, keep tests/docs adjacent to the behavior they explain,
and describe the final design rather than implementation chronology. The original
branch already had 37 commits, 87 changed files and about 25,000 added lines,
including 4,632 lockfile lines. Cleanup cannot turn that into a small feature.

Do not mechanically rewrite history or split by file category. Runtime setup
currently imports floorplan profile/probe/doctor components; any future split
must expose independently usable, passing entry points. Follow-up commits group
document/domain work, ALIGN runtime work, and review documentation; existing
history is retained.

### R16 — Reliable asynchronous Qt tests — done

One test-only helper scopes 30 modal interactions, captures callback exceptions,
closes nested dialogs on failure and stops unused timers. A watchdog reports
stuck dialogs through unittest. Signal collectors and subprocess-owned callbacks
remain simple. No production exception hook was introduced.

### R17 — Shared subprocess probe validation — done

GUI, doctor and managed setup now share shape/type and process-outcome validation.
Lists, non-text messages, truthy string flags, failed exits, crashes, timeout and
deep JSON become bounded diagnostics. Cleanup covers decoding and completion.
Independent review caught both nested-JSON `RecursionError` and the initially
missed managed-setup consumer; both are fixed and covered.

### R18 — Auxiliary history versus authoritative ownership — done

Bounded record decoding checks required identities, versions, types, paths,
hashes and finite timestamps. Optional corrupt history does not block authoring;
uncertain ownership cannot authorize a new worker or ownership transfer.
Original corrupt evidence is retained before deliberate replacement.

Independent real-process reproductions caught direct duplicate launches and old
controllers republishing stale history after index corruption. Strict mutation
reads now reject both, including empty/wrong-shaped indexes. Existing ownership,
transfer, rollback and cancellation races remain covered.

### R19 — Remove unsupported new direct-generate operation — done

`AlignRunner` is not a documented exported API, and both production callers already
used preview then explicit open. `start(project)` now generates only a preview;
worker operations are preview/reopen. Serving lifecycle tests use actual preview
completion before opening its capture. Historical `generate` records remain
readable; a real legacy process reconnect/stop test protects that distinction.

## Suggested review order

| Unit | Main files | Review focus |
| --- | --- | --- |
| Document/domain | `model.py`, `constraints.py`, `workspace.py`, `documents.py`, `handoff.py` | Drafts, units, source association, row identity, format compatibility, atomic exports. |
| Desktop integration | `dialog.py`, constraint forms, canvas/sketch, preview, GUI lifecycle hooks, `Session` | User actions, cancellation, undo/Save As, selection, entry-point parity. |
| Native placement | Runner/records, worker, geometry, run controls | One owner, captured inputs, preview/open, provenance, native geometry and viewer protection. |
| Installation/distribution | Manifest/lock, bootstrap/configure/provision, doctor/probe, CI | Reproducible isolated runtimes, verified assets, offline launch, installed wheel and display checks. |

Keep pre-Python bootstrap, installed-Python configuration and isolated ALIGN asset
provisioning separate: their prerequisites and side effects differ. Review generated
lockfiles and native fixtures through pins, provenance and actual acceptance.
Architecture/tooling rationale also lives in `FLOORPLANNING.md` (data flow/code
organization) and `INSTALLATION_AND_USAGE.md` (runtime maintenance).

## Validation and evidence

Run the checks documented in [FLOORPLANNING.md](FLOORPLANNING.md), including the
unit/Qt suite, native placement/viewer acceptance and installed-wheel check.
Visible desktop/OpenGL acceptance is separate from offscreen CI validation.
Each check prints its receipt location; retain logs with the tested commit SHA
and environment. CI uploads the `runtime-validation` artifact with seven-day
retention. Local temporary paths are not durable evidence for other reviewers.

For committed PR changes, use `git diff --check origin/main...HEAD`; for pending
edits, also use `git diff --check`. The captured native `layers.json` fixture
retains upstream whitespace and is verified against its provenance hashes.
Treat those fixture warnings separately from whitespace introduced in source.
