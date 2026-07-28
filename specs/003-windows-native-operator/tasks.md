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
      silent-failure detection, argv preservation (25 tests)
- [x] **T012** `tests/test_ingest.py` — parsing, premium summing, idempotency, UTF-8,
      SQL binding, concurrency settings (17 tests)
- [x] **T013** `tests/test_runner.py` — log attribution, no-fallback guarantee, session-id
      extraction, end-to-end supervision (13 tests)
- [x] **T014** `tests/test_operator.py` — identity, state, ownership, args, preamble,
      reports, dispatch, foreign-session isolation (42 tests)
- [x] **T015** `tests/test_handoff.py` — rendering, catalog, path matching, warnings (19 tests)
- [x] **T016** `tests/test_integration.py` — real multiplexer: create/query/kill, persistence,
      spaces in paths, unsafe-name rejection, runner supervision, detach, concurrency (8 tests)

## Phase 5: Documentation and governance

- [x] **T017** `README.md` — platform matrix, cross-platform quick start, repo structure
- [x] **T018** `docs/operator.md` — platform support, prerequisites, architecture, files,
      environment variables, troubleshooting
- [x] **T019** Constitution amendment — Python as primary cross-platform language,
      platform verification requirement, version 1.1.0
- [x] **T020** Spec artifacts updated to describe delivered behavior

## Verification

- [x] 124 tests pass, including 8 against a real psmux session on Windows
- [x] `bash -n` clean on all shell scripts
- [x] `git diff main -- operator.sh handoff.sh operator-ingest.py` empty (no Linux regression)
- [x] Manual Windows 11: loop mode, restart, resume after killed operator, reports,
      `list`/`stop`, foreign-session isolation, `handoff`
