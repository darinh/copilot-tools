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
- [x] E5 `operator reply`, retiring the inbox-polling semantics
      Delivered as two halves, because polling had two costs and removing
      only one leaves the model intact. Receiving: `operator session start`
      now consumes queued mail and prints it in the same breath, so there is
      no command to remember — an agent that never ran `operator inbox` was
      indistinguishable from one with no mail, and that silence was the
      actual defect. A mailbox that cannot be read is reported as such rather
      than as an empty one, does not stop the session starting, and archives
      nothing, so a jam is re-offered next session; mail already consumed
      before a mid-batch fault is printed, since that is the only time it
      will ever be offered. Answering: `operator reply [--instance NAME]
      [--to NAME] "text"` resolves both addresses — `--to` from the most
      recent correspondent across read *and* unread mail (session start
      archives the inbox, so consulting only unread mail would make replying
      impossible in exactly the session a message was delivered to),
      `--instance` from `$OPERATOR_INSTANCE`. It is sugar over `send_message`
      rather than a second delivery path, and passes the body after a `--` of
      its own so a reply reading `--queue it for later` cannot be re-parsed
      into a flag plus a shorter message. Both lookups refuse rather than
      guess: a reply carries an assertion the recipient acts on, and signing
      it with the directory's name — what `operator inbox` does when given
      none — puts words in another agent's mouth. "Nobody wrote to you" and
      "your mailbox could not be read" are reported separately, because only
      the first means there is no reply to send. `reply_hint` now emits the
      `operator reply` form and keeps naming `--to` explicitly: the default
      is right for one conversation and wrong for a batch from several peers,
      which is exactly when the hints are printed. 47 tests; 26 mutants, one
      per guard, all applied and killed. Three reviewers then found seven
      defects, each fixed with a test and a mutant: the session-start mailbox
      id was sanitized twice (so mail for any name needing sanitization was
      silently undeliverable), the header used box-drawing characters that
      raise `UnicodeEncodeError` on a cp1252 console *after* `consume`
      archived the messages, an inline-empty `--to=` fell through to the
      default recipient instead of being refused, `last_correspondent` skipped
      unreadable files and so could answer with an older sender, the two
      no-recipient failures shared an exit code, and the jam message claimed
      nothing had been marked read while printing messages that had. The
      seventh was a test that could not fail — a `--queue` assertion made
      against a multiplexer with no live sessions.

## Phase F — skills and rationale

- [x] F1 Install `worktrees`, `backlog`, `spec-driven`, `peer-agents`,
      `field-notes`; `peer-agents` replaces `operator-agents` (D7)
      Five skills under `skills/<name>/SKILL.md`; `skills/operator-agents/`
      deleted. The skills were rewritten against this repository's actual
      command surface rather than copied: `operator worktree new/finish/
      recover` with the refusals it really makes, and the backlog's real
      four-status vocabulary (`proposed → open → closed | rejected`) with the
      approval gate `proposed` exists to express — the source draft described
      a three-status backlog with no gate, which is a different product. The
      earned content in the retired skill (mail delivery table, refusal rules,
      etiquette, the worked example) was carried into `peer-agents` and
      corrected for E5: messages are delivered and answered with `operator
      reply`, and `operator inbox` is now described as an audit trail rather
      than the receiving path. The skill's bare `operator reply "<text>"` was
      corrected to name `--instance`/`$OPERATOR_INSTANCE`, since no ambient
      instance name exists. The feature *flag* keeps the slug
      `operator-agents`: it is persisted in every enrolled project's
      `features.json`, and renaming it would read as unset and silently
      re-enable the feature for anyone who had turned it off. References
      updated in `README.md` (3), `docs/skills.md`, `docs/versioning.md`,
      `templates/copilot-instructions.md`, `AGENTS.md`, `tests/test_setup.py`.
- [x] F2 Add `docs/rationale.md` — linked from `AGENTS.md`, not loaded by it
      The war stories and the ETH Zurich measurement behind the word budget,
      linked from `README.md`'s component table. Deliberately not loaded: the
      finding it records is that narrative content in a context file does not
      change behaviour while instructions do, so the stories live here and the
      imperatives live in the managed block.
- [x] F3 Conformance test: every `operator …` command a skill names must exist
      `tests/test_skill_conformance.py`. Every `operator <word>` in any skill,
      the template or `AGENTS.md` must be a real subcommand; every skill's
      front-matter name must match its directory and carry a description; every
      `skills/<name>` path in the docs must resolve. Stated as a path rule
      rather than a ban on the retired name, because the ban needs a prose
      exemption for text that describes the retirement, and a rule with a
      prose exemption stops being checkable. Carries a positive control (the
      scan finds `send`, `reply`, `inbox`, `worktree`, `backlog`), a negative
      control (an invented `operator teleport` is caught), a prose control
      (`operator assigns your worktree` is not a command, while
      `operator restart-loop` still is), and a non-empty control — without
      that last one, deleting `skills/` turns every rule into a loop over an
      empty list and the suite reports clean at the moment they stopped
      existing.

## Phase G+H — enforcement paired with generation (FR-6 … FR-9)

- [x] G1 Establish what the harness can actually enforce before deleting any rule
      that depends on it — `specs/004-operator-session/audit.md`, five mechanisms
      ordered by when they fire, each with a precedent already in this repository.
      Two results change the plan. A permission hook sees a tool call's
      *arguments* before it runs, so it can refuse `task` on a dirty worktree or
      an `edit` resolving outside the assigned tree — both of which FR-6 had
      classified as guardrails on the grounds that a skill cannot cover them,
      which is true and irrelevant, because the third class is *check*, not
      *skill*. And git hooks were considered and rejected for G7: `.git/hooks`
      holds only samples, a hook is per-clone, does not travel with the
      repository, and `--no-verify` removes it.
- [x] G2 Audit table: every managed-block line classified guardrail / procedure /
      checkable, naming the check for each deletion — 13 sections, 4,364 words
      measured. Residue after classification is ~500 words, an ~89% cut, which
      is the measured input G13 needs. Three sections (Session History, Parallel
      Agents, Common Pitfalls) survive at zero words: the first is fully done by
      `operator session start`/`end`, the second becomes one atomic `operator`
      subcommand rather than four pasted SQL statements, the third only restates
      rules above it.
- [x] G3 Block edits outside the assigned worktree (covers scratch-in-checkout) —
      `checkout-guard` gained three denials, not one. A `create`/`edit` whose
      path resolves into another checkout of the same repository is refused
      (writes outside the repository never are — the temp directory is where
      the guard sends everyone). Delegating to a subagent with uncommitted
      changes in *tracked* files is refused, which is the 454-line incident
      and the rule FR-6 had filed as un-checkable. Untracked files are
      deliberately not counted: they survive a `stash` or `reset --hard`, and
      counting them would make every session with one scratch file
      undelegatable. Two real defects were found by the tests before either
      rule shipped — the decision resolved the target but not the candidate
      roots, so an unresolved root never matched; and the first containing
      root won rather than the most specific, so the primary claimed every
      write into a nested worktree and the message named the wrong tree.
- [x] G4 `/.worktrees/` written to tracked `.gitignore` — at `operator
      worktree new`, not "at enroll". Enrollment is not a code path:
      nothing in first-party code writes a row to `catalog.csv`, so there
      was no enroll hook to hang this on. Worktree creation is the honest
      trigger and a better one — it fires the moment the directory it
      protects first exists, and it fires in every project that ever grows
      a worktree rather than only in ones enrolled after the change.
      `worktree_ignore_missing` reads every spelling git accepts
      (`.worktrees`, `/.worktrees`, either with a trailing slash, either
      negated) as present, so a hand-written rule is never doubled. The
      match is exact, so a commented-out rule reads as absent — and there
      is deliberately **no** comment-stripping branch, because mutation
      testing showed no input could tell one apart from its absence: a
      `#` prefix already fails the exact match. Never fails the call: an
      unreadable or unwritable `.gitignore` is reported in `notes` and the
      checkout still happens. Never staged, because a generated line in
      the index is one that gets committed inside somebody else's change.
      11 mutants killed, 0 survived, 0 never ran.
- [x] G5 Refuse to offer enrollment for an already-enrolled project —
      delivered as `tests/test_enrollment_conformance.py`, and the shape
      changed once measured. There is no enrollment to refuse: registering
      a project is exactly two writes — a row in `catalog.csv` and a freshly
      minted id — and **no first-party production module performs either**.
      So instead of a refusal on a path that does not exist, the check pins
      the absence: an AST scan over every production `*.py` reporting any
      write to a catalog-derived path, and any `uuid` minted *into* one.
      That is a stronger backing than the prose had, because it holds even
      for an agent that never reads the line. Reads pass; `tests/` is exempt
      because half the suite writes a fixture catalog into `tmp_path`, and
      `tests/test_artifact_guard.py` already guards the real one — the two
      divide the space. 47 tests, 14 mutants killed, 0 survived, 0 unanchored.
- [x] G6 Subproject path-ownership check on push — `operator ownership check`.
      `operator_ownership.py` decides; it touches neither git nor the
      filesystem beyond reading `.operator/subprojects.json`, so the whole
      rule set is testable as syntax. Paths are repository-relative git
      syntax and containment is segment-wise, never `startswith`, so
      `services/api` does not own `services/api-v2/` — and, as mutation
      showed, does not own `services` either: containment runs one way.
      Comparison is `main...HEAD`, three dots, so an un-rebased branch is
      not blamed for what landed on `main` behind it. Contract paths are
      refused even to a subproject that owns them; `--allow-contracts`
      waives that one rule and does not also grant ownership. Exit 0
      allowed, 1 refused, 2 could not tell — the third deliberately not
      folded into the second, because a hook reading "the declaration would
      not parse" as "this branch is fine" is the failure the check exists
      to prevent. An absent declaration and an empty diff are separate
      passing codes from `owned`, so "the check does not apply" never reads
      as "the check ran and approved". 57 tests, 18 mutants killed, 0
      survived, 0 never ran.
- [x] G7 No-commit-to-`main` hook — a permission hook, not a git hook.
      `.git/hooks` holds only samples, a hook is per-clone, does not travel
      with the repository, and `--no-verify` removes it. `git commit` on
      `main`/`master` is refused unless a merge, cherry-pick or revert is
      waiting to be concluded: merging a feature branch into `main` is how
      work lands here, a conflicted merge is finished with `git commit`, and a
      guard without that clause would block the workflow it exists to protect
      at the least convenient moment available.
- [x] G8 New root and subproject templates.
      The root template is rewritten wholesale: 4,332 rendered words → 674
      on delivery and 694 after adversarial review restored two dropped
      rules,
      an 84% cut, keeping every line that is a guardrail, a procedure or a
      check and deleting the prose that explained *why*. The rationale is not
      lost — it moved to `docs/rationale.md` and the skills, which are read on
      demand rather than on every turn. Three sections went entirely
      (`Session History`'s and `Parallel Agents`' hand-pasted SQL, and
      `Common Pitfalls`); the first two came back as two-line pointers to the
      commands that replaced the SQL, because retiring their feature flags is
      a migration problem and this task is not the place for it.
      The rewrite itself found a defect that no reader had: the draft
      documented `operator work request|list|end`, and there is no `end` work
      verb. `test_documents_only_name_operator_commands_that_exist` now checks
      every documented `(group, verb)` in the template *and* every
      `skills/*/SKILL.md` against the dispatcher's own `SUBCOMMANDS` and
      `*_VERBS` tables — which is why `SESSION_VERBS` and `OWNERSHIP_VERBS`
      exist now, so the document is measured against the code rather than
      against a second copy of the spelling.
      The subproject half is `render_subproject()` (FR-9), placed by
      `_place_subprojects` into every declared, existing owned directory.
      It is **generated rather than templated**, and that is the enforcement:
      there is no prose file for a rule to be written into, so the file can
      only carry resolved facts — name, owned paths, contracts — and a fact
      cannot contradict a rule. Claude Code concatenates parent and child
      while Codex lets the nearer file win, so a rule in both places means two
      things depending on the harness, and an *identical* copy is no safer
      because copies drift. Two tests hold that line with firing controls: no
      directive vocabulary, and no five-word run of prose shared with the root
      block. `templates/subproject-instructions.md` documents the shape and
      ships nothing; a test watches for anyone wiring it into the renderer.
- [x] G9 Marker migration — recognise both spellings, rewrite old→new.
      `MANAGED_BEGIN`/`MANAGED_END` are now `<!-- BEGIN operator:managed -->`
      (D3); the old spelling is still *read*, which is the whole point. A
      writer knowing only the new marker finds no block in a file carrying the
      old one, appends a second block below it, and leaves the repository
      holding two sets of conventions that disagree — invisibly to the
      function meant to keep them in step. Only the new spelling is written,
      so a file migrates on first regeneration and never again (pinned:
      the second run reports `unchanged`). A legacy block *is* ours, so the
      consent question is not re-asked — asking would train the answer and a
      caller answering no would strand the repository on the old spelling.
      Two independent refusals, each catching a file the other does not:
      `spellings_present` refuses whole blocks in both spellings, and
      `_marker_offsets` reads through one pair at a time so a legacy begin
      and a current end cannot delimit a span — pooled, those counts are one
      and one, which is exactly what well-formed looks like. Mutation found
      the second: pooling left every other test green. 6 mutants killed, 0
      survived, 0 never ran; the order of `MARKER_PAIRS` is a provably
      equivalent mutant and the code says so rather than claiming otherwise.
- [x] G10 Move build/test/lint out of the managed block (D11).
      The measured finding first, because it changes what the task was: those
      commands **already never reached a single agent**. They sat under
      `## Project Configuration System`, which `render()` *replaces* wholesale
      with `_configuration_section(...)`. D11 was satisfied by accident, by a
      renderer detail nothing tested and nothing recorded. So the work was
      (a) making the absence enforced rather than incidental, and (b) giving
      the commands somewhere to live: `compose()` now seeds a `## Validation`
      heading **below** the block when it is creating a file from nothing.
      Written once and never again — a project that deletes the section is not
      given it back, and an edited one survives regeneration undoubled. Before
      this, `compose(None, managed)` returned the block alone, so a brand-new
      `AGENTS.md` was told to keep its commands out of the block and given
      nowhere to put them.
- [x] G11 Test that appended project content survives regeneration (FR-7) —
      end-to-end through `retire` → `render` → `compose` → the atomic write,
      not `compose` alone, because every step between is somewhere the
      surrounding bytes could be dropped. The existing idempotence test is a
      weaker claim than it looks: it regenerates the *same* block, so an
      implementation that ignored the existing file and wrote the block alone
      still passes it. These regenerate a block that genuinely **changed** —
      a new version, and a feature turned off so the block *shrinks* — and
      compare everything outside the markers byte for byte. The fixture is
      deliberately ugly (trailing spaces, a doubled blank line, a tab)
      because a "preserving" implementation that round-trips through a line
      list loses exactly those and nothing else, and tidy fixture prose would
      not see it. A project that *documents* the markers in a fenced sample
      keeps its documentation. `merged` is asserted distinct from `written`:
      claiming "we made this file" over someone's prose is the claim the
      preservation contract denies. 9 mutants killed, 0 survived, 0 never
      ran — including both whitespace round-trips.
- [x] G12 Feature flags default off; one platform's commands; emit `CLAUDE.md`
      (FR-8). The six `_FLAG` features now ship `default=OFF`, so an enabled
      section is something somebody chose. `tracked-backlog` deliberately does
      **not** move: `tracked_backlog_backend()` returns its `default` under
      *every* uncertainty — no catalog, an unreadable file, an unregistered
      project — so that value is the enforcing answer three conformance
      guards depend on, and flipping it would retire them on all eight CI
      legs while every leg stayed green.
      Measured before flipping: all eight registered projects on this machine
      have no `features.json`. Resolving an absent configuration to the new
      defaults would therefore strip every optional section from eight
      repositories at once, with the diff attributed to a version bump. So
      `_values_for` **refuses** a project that has never chosen, naming the
      file and `operator projects`; `retire` turns that into a per-project
      `failed` that blocks removal of the global file — conventions in two
      places rather than none.
      One platform's commands: `<!-- operator:platform windows|posix -->` …
      `<!-- operator:endplatform -->` brackets each variant in the template
      and `select_platform` keeps the host's, chosen once per run by
      `host_platform()` from `os.name`. An HTML comment rather than the
      `**PowerShell (Windows)**` label above the fence — the label is prose,
      spelled three ways in the template already, and matching on prose would
      silently keep both variants the first time somebody reworded a heading.
      Markers are hunted outside fences only, for the same reason the
      managed-block finder is: a repository documenting this mechanism writes
      the markers in a fence, and reading the sample as real would delete the
      rest of the section on one platform and nothing at all on the other.
      Unbalanced markers raise rather than being recovered from — that
      asymmetry is invisible to a run on either machine alone. A platform
      name this build does not know is *kept*, the same answer an unknown
      gate slug gets and for the same reason. Removing a block joins the
      blank line above it to the one below, so the collapse of that doubled
      blank is confined to the seam; a blank run the template wrote is left
      exactly as written (both halves pinned).
      `render` takes `platform` with **no default**: a default would make
      every test that forgot it agree with the machine it ran on, so the
      Windows legs and the POSIX legs would each prove only their own half.
      `CLAUDE.md` carries `@AGENTS.md` and none of the conventions —
      duplicating them would put two texts that can disagree in front of one
      agent in one turn, with the newer one right and neither file saying
      which that is. It is written after `AGENTS.md`, never before. A file
      already there *with* a managed block is regenerated; one *without* is
      left alone and is not a blocker, because its whole content is an import
      line and a second consent prompt per project would spend the operator's
      attention on the file carrying no conventions. Both files are staged
      and committed together, from a pathspec built out of what is on disk —
      `git add` and `git commit` both treat a pathspec matching nothing as
      fatal, so a project that declined its `AGENTS.md` would otherwise have
      its staging reported as a git failure for naming a file that was
      correctly never written.
      Mutation: 3/0/0 on the defaults change, 11/0/0 on the platform and
      `CLAUDE.md` work. The one survivor of the first pass — collapsing blank
      runs unconditionally — was a real gap: the shipped template contains no
      doubled blank line today, so the confined rule and the unconditional
      one agree on it and stop agreeing the moment somebody writes one.
      `test_a_blank_run_away_from_the_seam_is_left_exactly_as_written` is
      what tells them apart.
- [x] G13 Set the budget from measured residue and make generation error above it
      `WORD_BUDGET = 700`, `block_words()` and a raise at the end of
      `render()`. It **errors**; it does not warn, and there is deliberately
      no override flag — a warning is a line of output nobody is obliged to
      act on, and the block that produced it still ships, which is how the
      predecessor reached 4,332 words with every one of them added for a
      reason. The count includes the markers and the fences, so prose cannot
      be hidden from it by moving it into a code block; over-counting binds
      slightly early, which is the safe direction. The message names the
      count, the budget, the overage and the three places a line can go
      instead (a skill, `docs/rationale.md`, or the tool), because a refusal
      that does not say what to do next is answered by deleting the check.
      It fired on its first real run at 756 words and 82 words came out
      without a guardrail going with them.
      Subprojects get their own, an order of magnitude smaller:
      `SUBPROJECT_WORD_BUDGET = 120`, delivered at 63. That file is read *in
      addition to* the root one in the same turn, so the two are cumulative
      against one reader's attention.

## Delivered — G8, G10, G13 (the held three)

The human lifted the hold on 2026-08-06 and accepted all three
recommendations, the 700-word figure, and deleting the emptied sections.
Measured after: the block renders **674 words of 700** with every flag on,
against 4,332 before — an **84% cut**. The subproject block renders 63 of
120. Three adversarial reviewers then found eight defects, including a path
traversal all three caught independently; the fixes restored two rules the
cut had dropped and the block now renders **694 of 700**. Full account in
`audit.md`.

## Still open for the human

FR-8 makes `_values_for` **refuse** a project that never chose its features.
Every one of the eight registered projects on this machine is unconfigured,
so every one of them fails regeneration until somebody opens
`operator projects` — where **Record these as chosen** now answers it in one
keystroke without changing a single value. That refusal is what makes "an
enabled section is a live requirement" true rather than decorative, and it is
deliberate, but it is a behaviour change that touches every project at once
and it should be somebody's decision rather than an agent's.

## Verification

- [x] V1 Full suite green (baseline 3383 passed, 10 skipped)
      Measured 2026-08-06 on `feat/operator-session` at `036626c`:
      **4158 passed, 9 skipped** in 406.77s
      (`python -m pytest -q --no-header -p no:randomly`).
      The skip count fell by one against the baseline and the pass count rose
      by 775; no test that ran before is skipped now.
- [x] V2 Mutation-test every new guard: break it, watch it go red, restore
      Done per commit rather than in one pass at the end, which is the only
      way it stays honest -- a guard mutated a fortnight after it was written
      is scored against a suite that has moved. Every feature commit on this
      branch records its own mutation result in its message; `git log
      959c450..HEAD --grep=mutant -i` lists them, and `a5a6d1a` is
      representative: *"Mutation: 3/0/0 on the staging rule, 4 killed and 1
      provably equivalent on the record option."*
      Surviving mutants are recorded where they survived, with the argument
      for equivalence next to the code, not tallied here.
