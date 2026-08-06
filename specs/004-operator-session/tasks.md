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

- [x] D1 `operator session start --instance <n> [--project <sub>]`
- [x] D2 Feed `instanceName` / `worktreePath` / `workItemRef` / `branchName` into
      the preamble (`build_preamble(..., assignment=)`) and expose them as a
      substitution table (`operator_session.assignment_values`). The template
      *consumer* lands with G8, which is where the templates that read it are
      written — there is no substitution mechanism in `project_instructions`
      today to wire it into.
- [x] D3 `operator session end` — handoff, then log close and claim disposal in
      one transaction; the claim is kept unless `--done` (FR-5, amended)
- [x] D4 Wire the supervisor loop to `session start`; the loop calls the work
      heartbeat, not the agent by its own judgement — `_loop_work_db`,
      `_loop_start_session` and `_loop_heartbeat` in `copilot_operator.py`.
      The assignment is resolved and the session log opened before the
      preamble is built, so FR-2's answer is in the agent's first token. The
      claim is re-read on each heartbeat rather than remembered from the
      assignment, so an item claimed mid-session is refreshed too. Every
      failure on this path is a log line and a `None`: an unattended loop must
      launch its agent whether or not the project is registered. `session end`
      is not called by the loop — it is the agent's own last act, and the loop
      learns of it through the restart marker it already watches.

## Phase E — commands

- [x] E1 `operator work request` / `release` / `list` / `heartbeat` —
      `manage_work` in `copilot_operator.py` over a new `operator_work.py`,
      which is where the claim store and the liveness cascade meet.
      `operator_work.agent_identity` probes every signal before recording it:
      an unconfirmed pid or mux session is written `NULL`, because each field
      is conclusive-for-DEAD in the cascade, so recording the short-lived
      `operator` process's own pid would manufacture proof that the owner is
      gone. `--item` is optional on `release`/`heartbeat` — an agent handed an
      assignment need not know the item's name, so the claim is looked up by
      instance. `list` is the one verb that does not require `--instance`.
- [x] E2 `operator work reclaim` — refuses a live owner; commits uncommitted
      changes to `wip/{item}-{deadInstance}` before reassigning (FR-4).
      `operator_work.reclaim` orders its refusals so that no git work happens
      for a reclaim that was going to be refused anyway: no-such-claim,
      already-mine, instance-busy, then the cascade — only `DEAD` proceeds,
      `STALE` is refused rather than auto-stolen. Preservation writes refs
      only: the index is copied to a temp file and `GIT_INDEX_FILE` points
      `git add` at the copy, so the branch is built with `write-tree` /
      `commit-tree` / `branch` and the owner's working tree, `.git/index` and
      `HEAD` are left byte-identical. `stash`, `reset`, `clean`, `checkout`,
      `restore`, `rm` and `mv` are absent from the module by construction and
      a source scan in `tests/test_work_cli.py` asserts it. A preservation
      that fails refuses the reclaim (`preserve-failed`) rather than handing
      on a tree nobody could read, and an existing `wip/` branch is never
      moved — the second crash on one item is exactly when one exists. The
      final swap compares the whole claim row the verdict was computed from
      (`work_claims.reassign(expect_claim=…)`), not just the owner's name,
      which does not change when a dead-judged owner comes back. A monotonic
      `revision` column makes that comparison see a write that changed no
      visible value, which a same-second heartbeat otherwise would not, and a
      `platform` column recording the writer's `os.name` lets a reclaim refuse
      a worktree from the other kind of system on recorded evidence instead of
      guessing from the path's shape.
- [x] E3 `operator backlog ready` / `close` — preserving the `proposed` gate.
      `operator backlog` delegates to `backlog_tool.main`, which already owns
      the vocabulary, the gate and every rule `check` enforces; a second
      parser in `copilot_operator.py` would be a second copy of all three.
      `backlog close` is the new verb: `close_item` refuses an item
      `why_not_workable` would have kept out of the queue, so filing an item
      and marking it shipped is not a two-command bypass of the approval that
      `ready` enforces on a path nobody has to take. It admits the recorded
      `blocks` exception for the same reason the gate does — an item lawfully
      worked that cannot be lawfully closed sends the agent to the status
      field by hand. `--reject` is deliberately outside that check, because
      the ordinary thing to decline is a proposal nobody approved, and it
      refuses a `--commit` rather than ignoring one. `--commit` is resolved
      through `git rev-parse ...^{commit}` and stored as the full SHA, so a
      revision that resolves to nothing, or to a blob or tree, is refused, and
      `HEAD` cannot be recorded as a word that names something else tomorrow.
      The rewrite is the same byte-preserving one approval uses, generalised
      to insert `closed:` and `commit:` beside `opened:` carrying that line's
      own ending — approval only ever rewrote a line that had one to copy.
      Adversarial review found the remaining hole: `commit` was illegal only
      under a *live* status, so a rejection laundered one — hand-edit a SHA
      onto an open item, reject it, and the value stops being reported at the
      moment nobody looks at the item again. `--reject` now clears a set
      `commit`, and R8 objects to one under any status that does not require
      one rather than only under a live one.
- [x] E4 `operator worktree new` / `finish` / `recover` — `operator_worktree.py`
      plus the CLI in `copilot_operator.py`. `new` takes the claim *before* it
      probes the path, because the claim is the only step with a
      compare-and-swap behind it and "the directory is absent" is not a
      reservation two agents cannot both observe; every refusal after the
      claim compensates by releasing the claim it just took. The branch
      defaults to `work/{item}` rather than guessing among `feat/`, `fix/` and
      `docs/`, and the directory is resolved from the *primary* root, since
      `--show-toplevel` inside a worktree would nest one inside another.
      `finish` refuses on not-owner, no recorded worktree, a foreign-platform
      claim, a cwd inside the target, a dirty tree, an unreadable directory
      and any git failure; it releases the claim last so a failure leaves it
      held, removes with `git worktree remove` and never `--force`, prunes
      only on evidence of absence rather than an unknown probe, and deletes
      the branch only when `merge-base --is-ancestor` proves it merged, with
      `-d` and never `-D`. `recover` reports and removes nothing; `--preserve`
      reuses `operator_work.preserve` and fires only for UNCLAIMED or provably
      DEAD owners — STALE is reported as itself, as `reclaim` already does.
      The mutating verbs are absent from the module by construction, asserted
      by an AST scan with a positive control.
      Three defects found by adversarial review, all fixed here. `undo`
      released unconditionally, but `work_claims.claim` *resumes* rather than
      refuses for the same instance, so a `new` that found its own live claim
      had not created it and the compensating release orphaned the agent's
      existing checkout; `new` now reads the claim first, refuses
      `already-yours` when it can confirm a recorded checkout, and releases
      only a claim this call took. A relative `--path` was recorded verbatim,
      but git resolves it against the repository root while a presence probe
      resolves it against the process cwd — so `finish` run from elsewhere
      probed the wrong place, called a live tree gone, pruned and released;
      paths are now anchored to the root on the way in and legacy relative
      claims are anchored on the way out. And `status --porcelain` does not
      list ignored files, so a tree holding only ignored content read as clean
      and `worktree remove` took it silently: gating on `--ignored` would
      refuse on any tree that had run a build and would be answered with a
      force flag, so the boundary is stated instead — `finish` names the
      ignored paths it is about to remove, with a positive control asserting
      it stays quiet when there are none.
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
