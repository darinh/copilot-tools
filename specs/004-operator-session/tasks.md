# Tasks: Operator Session, Assignment, and Liveness

Status is tracked in SQL during execution; this file is the reconciled record.

## Phase A — spec

- [x] A1 Write `specs/004-operator-session/{spec,plan,tasks}.md`
- [x] A2 File the harness-agnostic rename as a `proposed` backlog item (D2)

## Phase B — handoff keyed by instance (FR-1)

- [x] B1 Re-key the handoff path to `projects/{guid}/handoff/{instance}.md`
- [x] B2 Delete the `superseded/` archive, the lock and the warning banners now
      unreachable. The author stamp is **kept**: the migration routes on it, and
      a file copied out of its directory keeps its bytes but loses its name.
      What went was the rule telling agents how to interpret it.
- [x] B3 Migrate existing `next-session.md` and `superseded/*` into per-instance
      files — move, never delete; unknown provenance parks under a reserved name
- [x] B4 Update `handoff.sh` (bash 3.2 clean), `backlog_tool.py`,
      `project_features.py` and every test pinning the old layout

## Phase C — liveness (FR-3, FR-4)

- [x] C1 `work_claims` schema and store
- [x] C2 `boot_id` probe — Linux `/proc/sys/kernel/random/boot_id`, Windows
      `LastBootUpTime`
- [x] C3 Mux-session and PID+start-time probes
- [x] C4 The four-step cascade returning LIVE / DEAD / STALE, never auto-stealing
      on STALE

## Phase D — session lifecycle (FR-2, FR-5)

- [ ] D1 `operator session start --instance <n> [--project <sub>]`
- [ ] D2 Feed `instanceName` / `worktreePath` / `workItemRef` / `branchName` into
      the preamble and the template substitutions
- [ ] D3 `operator session end` — atomic handoff + claim release + log close
- [ ] D4 Wire the supervisor loop to `session start` / `end`; the loop calls
      `work heartbeat`, not the agent by its own judgement

## Phase E — commands

- [ ] E1 `operator work request` / `release` / `list` / `heartbeat`
- [ ] E2 `operator work reclaim` — refuses a live owner; commits uncommitted
      changes to `wip/{item}-{deadInstance}` before reassigning (FR-4)
- [ ] E3 `operator backlog ready` / `close` — preserving the `proposed` gate
- [ ] E4 `operator worktree new` / `finish` / `recover`
- [ ] E5 `operator reply`, retiring the inbox-polling semantics

## Phase F — skills and rationale

- [ ] F1 Install `worktrees`, `backlog`, `spec-driven`, `peer-agents`,
      `field-notes`; `peer-agents` replaces `operator-agents` (D7)
- [ ] F2 Add `docs/rationale.md` — linked from `AGENTS.md`, not loaded by it
- [ ] F3 Conformance test: every `operator …` command a skill names must exist

## Phase G+H — enforcement paired with generation (FR-6 … FR-9)

- [ ] G1 Establish what the harness can actually enforce before deleting any rule
      that depends on it
- [ ] G2 Audit table: every managed-block line classified guardrail / procedure /
      checkable, naming the check for each deletion
- [ ] G3 Block edits outside the assigned worktree (covers scratch-in-checkout)
- [ ] G4 `/.worktrees/` written to tracked `.gitignore` at enroll
- [ ] G5 Refuse to offer enrollment for an already-enrolled project
- [ ] G6 Subproject path-ownership check on push
- [ ] G7 No-commit-to-`main` hook
- [ ] G8 New root and subproject templates
- [ ] G9 Marker migration — recognise both spellings, rewrite old→new
- [ ] G10 Move build/test/lint out of the managed block (D11)
- [ ] G11 Test that appended project content survives regeneration (FR-7)
- [ ] G12 Feature flags default off; one platform's commands; emit `CLAUDE.md`
- [ ] G13 Set the budget from measured residue and make generation error above it

## Verification

- [ ] V1 Full suite green (baseline 3383 passed, 10 skipped)
- [ ] V2 Mutation-test every new guard: break it, watch it go red, restore
