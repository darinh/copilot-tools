# Repository Agent Instructions

## Shell scripts must run on bash 3.2

`/bin/bash` on macOS is 3.2 — Apple froze it at the last GPLv2 release and is
never going to move it — so on macOS it is the default interpreter,
permanently, not a legacy configuration a user could upgrade out of. Every
`*.sh` file in this repo runs under `set -u`, so two bash 4 features are
runtime aborts there rather than warnings:

- **Associative arrays.** `local -A x` is `declare: -A: invalid option`, and
  under `set -e` that ends the run. Use an indexed array with the `in_list`
  helper (`operator.sh`, `handoff.sh`) instead.
- **Expanding an empty array.** `"${a[@]}"` under `set -u` is an
  unbound-variable abort on every bash before 4.4. Write
  `${a[@]+"${a[@]}"}` instead — **uniformly**, including where the array
  looks provably non-empty. Which arrays can be reached while empty is a fact
  about today's callers, and callers change. `${#a[@]}` is safe.

`tests/test_shell_bash32_conformance.py` enforces this over every first-party
shell script, along with the other bash 4 constructs that would break there
(namerefs, `mapfile`, `coproc`, `;;&`, `[ -v x ]`, `exec {fd}<`, `${v@Q}`,
`$'\uXXXX'` and more). Every detector has a positive control asserting it
fires and a negative control asserting the portable spelling still passes — a
detector that matches nothing reports the whole tree clean, which reads
exactly like success. Add both when you add a detector.

It is deliberately a static scan: these are the exact tokens bash 3.2 rejects,
and CI executes these scripts under a 3.2 interpreter on one leg only, so an
execution test cannot object on the other seven.

Do not narrow that scan to a single file. It replaced a tripwire that read
only `operator.sh`, which is how `handoff.sh` kept an associative array
through the change that removed operator.sh's — a rule enforced against one
file is not a rule, it is that file's history.

## Spec-kit workflow

- Read `.specify/memory/constitution.md` and the active feature under `specs/`
  before implementing a non-trivial change.
- Use the `speckit-*` skills for specification, planning, tasks,
  implementation, and analysis.
- Keep specs factual and update `spec.md`, `plan.md`, and `tasks.md` with
  delivered behavior.

## Parallel todo ownership

- The coordinator must create the shared `todo_claims` table before launching
  parallel agents:
  `CREATE TABLE IF NOT EXISTS todo_claims (todo_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL UNIQUE, claimed_at TEXT NOT NULL DEFAULT (datetime('now')), heartbeat_at TEXT NOT NULL DEFAULT (datetime('now')));`
- Use a unique stable agent ID and atomically claim one ready todo before
  changing files. Claiming must be atomic in a short `BEGIN IMMEDIATE` transaction using `INSERT OR IGNORE INTO todo_claims SELECT ...` with full condition checks, followed by a guarded `UPDATE todos SET status = 'in_progress'` that verifies the claim succeeded.
- Never work on a todo claimed by another agent and never steal a claim without
  coordinator confirmation that its owner has stopped.
- A todo is ready only when it is pending, unclaimed, and every dependency is `done`. Provide exact ready-work SQL excluding claimed or dependency-blocked todos:
  `SELECT t.* FROM todos t WHERE t.status = 'pending' AND NOT EXISTS (SELECT 1 FROM todo_claims c WHERE c.todo_id = t.id) AND NOT EXISTS (SELECT 1 FROM todo_deps td LEFT JOIN todos dep ON td.depends_on = dep.id WHERE td.todo_id = t.id AND (dep.id IS NULL OR dep.status != 'done'));`
- If preferred work depends on an in-progress todo, leave it pending and select
  another ready todo instead of waiting. Do not mark dependency waits as blocked.
- Completion/real blocker/release must update status only when the same agent owns the claim, then delete the claim coherently within a transaction.
- Refresh `heartbeat_at` during long-running work; only the coordinator may
  recover a stale claim after confirming its owner stopped.
- Work in an isolated git worktree. Tasks that modify the same file are
  sequential even when they are otherwise marked parallel (`[P]` means eligible, not assigned).
- In parallel mode, worker agents update SQL status and report completion, but ONLY the coordinator serially reconciles `tasks.md` checkboxes. Single agents update both SQL and `tasks.md` directly.
