# Tasks: Windows-Native Operator

**Feature**: `003-windows-native-operator` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Phase 1: Foundations

- [x] **T001** Session-backend abstraction `operator_mux.py` — probe order, argv-after-`--`
      launching, post-create verification, safe naming with collision-free ids
- [x] **T002** In-pane supervisor `operator_runner.py` — spawn, process-tree discovery,
      log pinning, session-id extraction, exit recording, metrics capture
- [x] **T003** Pure-Python parser `operator_ingest.py` — stdlib sqlite3, WAL + busy timeout,
      parameter binding, explicit UTF-8, idempotent upsert
- [x] **T004** UTF-8 console output `operator_console.py`

## Phase 2: CLI

- [x] **T005** Operator CLI `copilot_operator.py` — instance lifecycle, ownership records,
      state persistence with resume-once, loop mode, graceful shutdown, all subcommands
- [x] **T006** Report rendering without the `sqlite3` binary (summary, sessions, models,
      projects, costs) and the aggregate run summary
- [x] **T007** Handoff port `handoff_tool.py` — case-insensitive catalog matching on Windows,
      path-aware instance inference, UTF-8 output, no legacy dual-write

## Phase 3: Packaging and setup

- [x] **T008** `pyproject.toml` — console scripts `operator`, `handoff`, `operator-ingest`,
      `operator-runner`; `requires-python >= 3.10`
- [x] **T009** `setup_tools.py` — per-platform prerequisites, package install with PATH
      guidance, junction-based extension linking, consent-based template install
- [x] **T010** CI matrix — Ubuntu/Windows/macOS x Python 3.10/3.12 with a real multiplexer
      installed on each, plus a bash syntax job

## Phase 4: Tests

- [x] **T011** `tests/test_mux.py` — naming, collisions, reserved names, probe order,
      silent-failure detection, argv preservation
- [x] **T012** `tests/test_ingest.py` — parsing, premium summing, idempotency, UTF-8,
      SQL binding, concurrency settings, string-literal braces, field order
- [x] **T013** `tests/test_runner.py` — log attribution, no-fallback guarantee, PID-reuse
      bounds, session-id extraction, end-to-end supervision
- [x] **T014** `tests/test_operator.py` — identity, state, ownership, args, preamble,
      reports, dispatch, foreign-session isolation, `pane_dead` liveness
- [x] **T015** `tests/test_handoff.py` — rendering, catalog, path matching, warnings
- [x] **T016** `tests/test_integration.py` — real multiplexer: create/query/kill, persistence,
      spaces in paths, unsafe-name rejection, runner supervision, detach, concurrency
- [x] **T017** `tests/test_loop_resilience.py` — launch retry, bounded give-up, resume-id
      preservation across a failed launch
- [x] **T018** `tests/test_setup.py` — extension preservation without consent, consent-gated
      replacement, identical-tree no-op, prerequisite hints

## Phase 5: Documentation and governance

- [x] **T019** `README.md` — platform matrix, cross-platform quick start, repo structure
- [x] **T020** `docs/operator.md` — platform support, prerequisites, architecture, files,
      environment variables, troubleshooting
- [x] **T021** Constitution amendment — Python as primary cross-platform language,
      platform verification requirement, version 1.1.0
- [x] **T022** Spec artifacts updated to describe delivered behavior

## Phase 6: Adversarial review

- [x] **T023** Three review rounds across four models; every finding fixed with a
      regression test. See the review table in [spec.md](./spec.md).

## Verification

- [x] 186 tests pass, including 8 against a real psmux session on Windows
- [x] `verify_cross_platform.py` passes 36/36 on Windows and on Linux
- [x] `bash -n` clean on all shell scripts
- [x] `git diff main -- operator.sh handoff.sh operator-ingest.py` empty (no Linux regression)
- [x] Manual Windows 11: loop mode, restart, resume after killed operator, reports,
      `list`/`stop`, foreign-session isolation, `handoff`

- [x] **T024** Third review round: ownership semantics, backend honesty, parser resynchronization,
      case-only aliasing, schema-migration race, filesystem-root guard, retry checkpointing.
      Adds `operator forget` for stale state. 11 new regression tests.

## Phase 7: AI credit billing

- [x] **T025** Establish the new billing mechanics from a live Copilot session; record
      `total_nano_aiu`, the 1 credit = $0.01 conversion, and the token-type breakdown
- [x] **T026** `extract_ai_credit_usage` sums credits and tokens per session and per model
- [x] **T027** Schema records credits and token counts; existing databases migrate in place
- [x] **T028** Reports switch to AI credits, with legacy premium-request fallback; add `report tokens`
- [x] **T029** Append `--log-level debug` at launch so usage data exists at all
- [x] **T030** `tests/test_billing.py` — conversion against the real captured value, multi-call
      summing, per-model attribution, migration preserving history, launch-flag behaviour

- [x] **T031** `operator logs` inspects and prunes Copilot's process logs, since forcing debug
      logging makes them grow and Copilot does not rotate them. Pruning only removes logs
      already ingested, so usage is never lost.
