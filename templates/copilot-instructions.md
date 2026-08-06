# User Workflow Conventions

These conventions apply to all projects I work on. They complement any agent-specific instructions (e.g., Anvil).

---

## Git Worktrees — Always

**This is not optional and not feature-flagged. All work happens in a git worktree.**

Never edit files in the primary checkout. Before your first file change, create a worktree for the
branch you are about to work on.

### Layout

- Worktrees live in `<repoRoot>/.worktrees/<name>`, where `<repoRoot>` is the **primary** checkout.
- `<name>` is the branch name with `/` replaced by `-` (branch `feat/login` → `.worktrees/feat-login`).
- `/.worktrees/` **must** be in the repo's tracked `.gitignore`. Add it if it is missing — worktrees
  are checkouts, not repository content, and every clone needs the rule, so `.git/info/exclude` is
  not good enough.

### Finding the primary repo root

Inside a worktree, `git rev-parse --show-toplevel` returns the *worktree*, not the repo. Never use it
to locate the project. The first record of `git worktree list --porcelain` is always the primary
checkout, from anywhere in the repo:

**bash (Linux/macOS/WSL)**
```bash
repo_root=$(git worktree list --porcelain | head -1 | cut -d' ' -f2-)
```

**PowerShell (Windows)**
```powershell
$repoRoot = (git worktree list --porcelain | Select-Object -First 1) -replace '^worktree '
```

Use that path — never the worktree path — for anything that identifies the *project* rather than the
checkout: the per-project directory, the handoff file, `.specify/` initialization. A worktree is a
second directory for the same project, not a second project. Treating one as its own project mints a
duplicate id and silently splits the project's state in two.

### Working in one

```bash
# Create (run from the primary checkout, or use git -C "$repo_root")
git -C "$repo_root" worktree add .worktrees/feat-login -b feat/login
cd "$repo_root/.worktrees/feat-login"

# ... make changes, commit ...

# Finish: merge into main, then remove the worktree
cd "$repo_root"
git merge --no-ff feat/login
git worktree remove .worktrees/feat-login
git branch -d feat/login
```

The commands are identical on every platform; only the path separators differ.

### Rules

- One worktree per branch. Git refuses to check out the same branch in two worktrees.
- Never create a worktree inside another worktree — resolve the primary root first.
- `cd` out of a worktree before removing it.
- Leave worktrees you did not create alone; another agent may be working in one.
- Worktree branches merge into `main`. There is no separate integration branch.

---

## Scratch Files — Never in the Checkout

Throwaway work goes in a temp directory. Never in a git checkout.

This means every probe script, every reproduction you write to confirm a bug,
every scratch copy of a file you want to diff against, and every fixture you
made to try something out. A script with a relative path writes wherever the
process happens to be, and for an agent that is almost always someone's
checkout.

```bash
scratch=$(mktemp -d)                     # bash
```
```powershell
$scratch = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
```

**Tell your subagents the same thing, by name.** They run their own shell in
your checkout, and you never see the commands they issue — only the result. A
reviewer prompt that says "do not write outside tmp" is worth the one line it
costs.

**The primary checkout is the strongest pull and the worst place to give in
to it.** Filesystem probes especially — a dangling symlink, a read-only file, a
junction — get run there because it is the tree that *is* the project, while a
worktree feels like a copy of it. It is also the one tree every other agent
resolves as the project, so whatever you leave becomes something they have to
stop and reason about. Agents who had already read this section left artifacts
there three times in a single evening. Knowing the rule is not what stops you;
noticing that you are about to create a path relative to the wrong root is.

### Why this is a rule and not a preference

Three agents once spent an evening diagnosing a working-directory bug in a test
suite, on the evidence of directories that kept appearing in a shared checkout.
There was no bug. The directories came from the agents' own review subagents
reproducing defects — nine of them from a single review round, named after a
reviewer's loop variable. Two runs and a grep would have refuted the theory in
four minutes; nobody ran them, because the artifacts *felt* like proof.

The generalisable form is worth more than the rule: **an explanation that fits
the evidence is not the same as the explanation.** When you find evidence of a
problem, reproduce the mechanism before you explain the artifact — a plausible
story will stop you looking.

Two practical consequences:

- **Clean up before you finish, not later.** An artifact discovered afterwards
  has no provenance, and everything fits it equally well. That is what makes
  these expensive, not the disk space.
- **`git status` will not save you.** Git does not track empty directories, so
  an empty stray is invisible to it. A checkout can report perfectly clean with
  artifacts sitting in its root.

If the `checkout-guard` extension is installed it enforces this: it names a
scratch directory at session start, reports new untracked paths the moment they
appear, and refuses a blanket `git add -A` while they are outstanding. Staging
a path by name always works — the point is to stop artifacts being committed
*unnoticed*, not to stop them being committed.

---

## Handing a Worktree to a Subagent

**Commit before you delegate. Staging is not enough.**

A reviewer subagent once ran `git stash` inside another agent's worktree and
destroyed 454 lines of uncommitted work, mentioning it in passing in an
otherwise clean review. `git status` came back empty and `git stash list` was
empty too — the stash had been dropped. It was recovered only because the work
had been `git add`-ed, so the blobs still existed as dangling objects:

```bash
git fsck --unreachable
git cat-file -p <blob>          # grep for a string unique to your change
```

A reviewer that runs `git checkout` or `git reset --hard` instead leaves
nothing to recover at all.

- **Commit first.** A commit is the only state a subagent cannot casually
  destroy. Point reviewers at `git diff main...HEAD`, not `git diff --staged`.
- **Forbid mutating git commands explicitly, by name** — `stash`, `checkout`,
  `reset`, `clean`, `restore`, `rebase`, `commit`, `add`. "Don't write files
  outside tmp" does not cover git plumbing, which writes no new files.
- **Verify the worktree before you read the findings.** If a subagent mentions
  in passing that something of yours was lost, stop and check `git status` and
  `git stash list` before acting on anything else it said.

---

## Project Configuration System

Each project can have a persistent configuration stored outside the repo at `~/.operator/projects/`.

### Catalog

`~/.operator/projects/catalog.csv` maps project root paths to GUIDs. Paths are stored in the **native
form of the platform that created the entry**:

```csv
"C:\Users\dev\repos\my-app",EXAMPLE1-1111-1111-1111-111111111111
"/home/dev/projects/other-project",EXAMPLE2-2222-2222-2222-222222222222
```

Those two GUIDs are invalid on purpose. **Do not copy them, and do not write this block anywhere.**
Generate a fresh GUID, and **append** your one line to `catalog.csv` — never rewrite the file.
It holds the registration of every project on the machine, no tool here writes it, and nothing
here can rebuild it.

When matching, normalize the current project root before comparing. On Windows compare
case-insensitively; on Linux and macOS compare case-sensitively.

### Per-Project Directory

`~/.operator/projects/{guid}/` contains:
- `copilot-instructions.md` — project-specific conventions and feature flags
- `handoff/{instance}.md` — session handoff files, one per operator instance
  (ephemeral, read-once)
- Any other project artifacts that should persist outside the repo

### On Session Start — Project Lookup

1. Determine the current project root. This is the **primary checkout** — if you are in a worktree,
   resolve it with `git worktree list --porcelain` as described under **Git Worktrees**, never with
   `git rev-parse --show-toplevel`. Fall back to the cwd only outside a git repo.
2. Read `~/.operator/projects/catalog.csv` and look for a matching path.
3. **If found**: Read `~/.operator/projects/{guid}/copilot-instructions.md` and follow its conventions. Check for a handoff at `handoff/{instance}.md`.
4. **If not found**: Ask the user:
   - "This project isn't in the catalog yet. Would you like to set it up?"
   - Choices: "Enable all features" / "Select features" / "Skip for now"
   - If enabling: generate a GUID, create the directory, write `copilot-instructions.md` (see
     **What to write in a per-project file** below), and **append** one line to `catalog.csv`.
     Never rewrite that file.
   - **If spec-driven is selected and `.specify/` is missing**, initialize spec-kit using the script
     variant matching your platform — `ps` on Windows, `sh` on Linux/macOS/WSL:

     **PowerShell (Windows)**
     ```powershell
     specify init --here --force --integration copilot --integration-options="--skills" --script ps
     ```

     **bash (Linux/macOS/WSL)**
     ```bash
     specify init --here --force --integration copilot --integration-options="--skills" --script sh
     ```

### Feature Selection

When setting up a new project, the user selects which conventions to enable:

| Feature | Description | Default |
|---------|-------------|---------|
| **Session Handoff** | Per-instance handoff files for cross-session continuity | ON |
| **Session History** | SQL `session_log` table for audit trail | ON |
| **Spec-Driven Development** | Spec as source of truth. Uses GitHub spec-kit. Location: `.specify/` and `specs/`. | ON |
| **Parallel Agents** | SQL-coordinated parallel task execution via `todo_claims`. | ON |
| **Operator Agents** | Peer Copilot sessions via `operator`, and mail between them | ON |
| **Branching Strategy** | Feature branches in worktrees, merged to `main`, conventional commits | ON |
| **Tracked Backlog** | Where open work is recorded | `backlog/` folder |

**Tracked Backlog is a choice, not a toggle.** It takes one of `folder` (a
`backlog/` directory in the repo, one file per item, enforced by tests),
`github-issues`, or `none`. The other rows above are on/off.

The selections are stored in `~/.operator/projects/{guid}/features.json`, and
`project_features.py` is the single owner of what features exist and what
values each may take. Read or change them with:

```
operator projects
```

Do not maintain a second list of features anywhere. A menu that enumerates
features and a document that enumerates features will disagree, and the
disagreement shows up as an option that silently toggles nothing.

### What to write in a per-project file

The per-project `copilot-instructions.md` records what is **true of this project only**. It does
not restate the protocols in this file — an agent reads both, and a second copy of the handoff
or branching rules only creates something to drift. Name the enabled features and stop.

```markdown
# {project} — project conventions

Enabled features: session-handoff, session-history, spec-driven, parallel-agents,
operator-agents, branching-strategy, tracked-backlog.   <!-- list only the ones actually enabled -->

## What this repo is
One paragraph. What it does, and what makes changes here risky.

## Validation
The exact commands that prove a change works, with expected results
(e.g. `python -m pytest -q` — expect 518 passing). What CI runs.

## Gotchas
Things learned the hard way that a fresh agent would otherwise rediscover.

## Standing questions
Open decisions the user wants re-raised each session, if any.
```

Everything in it should be something you verified, not something you assumed. An empty section is
better than an invented one — fill it in as the project teaches you.

---

## Session Handoff Protocol

*Enabled by feature flag: `session-handoff`*

Agents use `~/.operator/projects/{guid}/handoff/{instance}.md` for continuity
across sessions. One file per operator instance: the handoff you find is the one
your own previous session wrote, and a peer working the same checkout has its
own.

### On Session Start
When the user greets you (e.g., "hey", "hello", "hi"), **immediately**:

1. **Check for unmerged work**: Run `git branch --no-merged main` (the ref matters — with no argument git compares against HEAD, which tells you nothing). If any feature branches have unmerged commits, tell the user: *"Found unmerged work on branch X (N commits). Want to continue that, merge it, or start fresh?"*
2. **Read handoff**: Check if `~/.operator/projects/{guid}/handoff/{instance}.md` exists. If it does:
   - Read it and use it as your starting context.
   - Tell the user what was left in progress and what you're picking up.
   - Delete the file after reading it (it's a one-time handoff, not permanent docs).
3. **Log the session**: Insert a row into the `session_log` table in the session database (see Session History below).

### On Session End (automatic)
Use the `handoff` command when any of these are true:
- You've completed a large task and there are known next steps.
- You sense the context window is getting large (long conversation, many tool calls). Don't wait to be asked — proactively write the handoff and tell the user: *"Context is getting heavy. I've written the handoff — starting a new session."*
- The user says they're ending the session.

The `handoff` command takes the same arguments on every platform. Pass each value as a single quoted
argument on one line — do not use shell line continuations, which differ between shells:

```
handoff --instance <operator-instance-name> --status "What was completed (commits, branches, files)" --next "Prioritized next steps" --context "Key decisions, gotchas" --prompt "Ready-to-execute prompt for next session"
```

The `handoff` command atomically writes the handoff file AND triggers the operator restart. **Never write the handoff file manually** — always use the command.

If the `handoff` command is not available (e.g., not on PATH), fall back to writing
`~/.operator/projects/{guid}/handoff/{instance}.md` manually and then creating the restart marker file using
the form for your platform:

**PowerShell (Windows)**
```powershell
New-Item -ItemType File -Force ~/.operator/restart/{instance-name}
```

**bash (Linux/macOS/WSL)**
```bash
touch ~/.operator/restart/{instance-name}
```

### Handoff File Format
```markdown
# Session Handoff

## Status
[What was just completed — be specific about commits, branches, files changed]

## In Progress
[What was actively being worked on when the session ended, if anything]

## Next Steps
[Prioritized list of what the next agent should do]

## Context
[Key decisions made, architectural notes, gotchas discovered — anything the next agent needs to avoid re-deriving from scratch]

## Prompt
[A ready-to-paste prompt the next agent can execute immediately]
```

### An unread handoff

`handoff` does not silently replace one. If a handoff is still sitting at your
instance's path when you write the next one, the old file is moved to
`{instance}.prev.md` first and the tool says so on stderr.

**That cannot happen in the ordinary flow**, because the protocol has the
reader delete the file once it has consumed it. So a `.prev.md` beside your
handoff means a session of *your* instance ended without picking up what the
one before it left. Read it before you decide what you are working on, and say
so in your final message if `handoff` warned you about it — that warning goes
to stderr and dies with the session that saw it.

There is one slot, replaced each time, and that is deliberate: a second
consecutive miss means the read side is broken, and keeping the older of two
undelivered handoffs would not fix it.

### Rules
- The file is ephemeral — read once, then delete. Not documentation.
- Write it proactively. The user should never have to ask for it.
- Never write the handoff file yourself. Use the `handoff` command: writing it
  by hand is what destroys an unread one, and it does not raise the restart
  marker, so the loop never picks the session up.

---

## Session History

*Enabled by feature flag: `session-history`*

Use a `session_log` table in the **session SQL database** to record a persistent history of work across sessions.

```sql
CREATE TABLE IF NOT EXISTS session_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    ended_at DATETIME,
    branch TEXT,
    task_summary TEXT NOT NULL,
    commits TEXT,           -- comma-separated SHAs
    files_changed TEXT,     -- comma-separated paths
    tests_before INTEGER,
    tests_after INTEGER,
    learnings TEXT,         -- key decisions, gotchas, patterns discovered
    status TEXT DEFAULT 'in_progress' CHECK(status IN ('in_progress', 'completed', 'abandoned'))
);
```

**On session start**: `INSERT INTO session_log (branch, task_summary) VALUES ('{branch}', '{what you are working on}');`

**On session end**: `UPDATE session_log SET ended_at = CURRENT_TIMESTAMP, commits = '{shas}', files_changed = '{files}', tests_before = N, tests_after = M, learnings = '{notes}', status = 'completed' WHERE id = {id};`

---

## Tracked Backlog

*Enabled by feature flag: `tracked-backlog`*

**Open work belongs in the repository, not in a handoff.**

Everything above this section is ephemeral by design. A handoff file is
read-once and deleted at session start. `session_log` lives in a per-session
database that does not persist. Both are *handover* mechanisms, and neither is
a record. So closed work is answerable from `git log` and open work has been
answerable from nothing at all — it survives only as a re-summarised sentence
carried from session to session, which is lossy by construction and, worse,
lossy undetectably.

`backlog/` is the durable half. It is tracked, it is reviewed with the code,
and it outlives every session that touched it.

### One file per item

`backlog/0007-short-kebab-slug.md` — four-digit id, kebab slug.

This is the main design constraint, not a style choice. Parallel agents work in
separate worktrees off the same `main`. A single `BACKLOG.md` puts every add
and every close on the same lines of one file, so every concurrent pair
conflicts. One file per item makes an add conflict-free by construction and a
close a small diff.

```
---
id: 7
title: One line, imperative
status: open          # open | closed | rejected
opened: 2026-08-04
closed:               # date, when terminal
commit:               # closing SHA, when closed
spec: specs/003-a-feature/spec.md   # or: none
requirement:          # optional: text that must occur in that spec
---

## Evidence
## Why it matters
## Notes
```

Front matter carries no inline comments — a value is the rest of its line. The
block above is an illustration; real items spell the vocabulary nowhere but
their own `status` field. (YAML's comment rule would also read
`title: Fix issue #42` as `Fix issue`, silently discarding the number.)

### Mapping work to specs

When spec-driven development is enabled, **every item names a spec or says
`none` explicitly**. Writing `none` out makes "this changes no specification" a
decision somebody recorded, rather than a silence that could equally mean
nobody looked.

`requirement` is what makes that mapping load-bearing rather than decorative:
when set, the text must actually occur in the named spec, so renaming or
deleting the requirement turns the suite red and someone has to revisit the
item. An item pointing at a spec section that no longer exists is worse than
one pointing nowhere, because it reads as though it had been checked.

An item is the right place for work that is *not yet* specified. When it grows
into a feature, run `/speckit-specify`, then point the item's `spec` at what
that produced. The backlog is the queue; `specs/` is the contract.

### Closing an item

**Set `status`, `closed` and `commit` in the same commit that does the work,
and update the linked spec in that same commit.** A close landing separately
from its fix is a window in which the backlog is wrong, and the `commit` field
cannot name a SHA that does not exist yet — so commit the work and amend, or
close it in the merge commit.

`rejected` means considered and declined. It takes a `closed` date and no
`commit`, because nothing shipped: demanding a SHA there forces whoever rejects
an item to invent one, and an invented SHA looks exactly like evidence.

### Rules

- **Evidence is required, and it is what was measured** — a reproduction, a
  command and its output, a mutation that ran green. An item with no evidence
  is a rumour, and a backlog of rumours costs every reader the time it takes to
  find that out.
- **Never seed an item you have not verified yourself.** Carried-forward lists
  go stale: two of the four items this convention was first written for turned
  out to be already fixed, and recording them would have created work that did
  not exist.
- **Enforce it with tests, or do not bother.** A backlog nothing reads decays
  like any other prose. At minimum: ids match filenames and are unique, status
  is in the vocabulary, evidence is non-empty, a closed item's SHA resolves in
  this repository, and the directory itself is non-empty — without that last
  one, deleting `backlog/` turns every other rule into a loop over an empty
  list and the suite reports it clean at the moment it stopped existing.
- **Prove each rule can fail.** Violate it in a temp copy and watch the suite
  go red. A guard that cannot fire reads exactly like coverage.

---

## Field Notes (Agent Journal)

Cross-project working journal of insights about building, instructing, and collaborating with AI agents.
Lives in its own repo (not per-project), alongside your other repositories — for example
`C:\Users\dev\repos\agent-field-notes` on Windows or `~/projects/agent-field-notes` on Linux/macOS.

```
journal/    Chronological entries, one conversation per file.
            Format: YYYY-MM-DD-slug.md
essays/     Synthesized principles across multiple journal entries.
            _pending.md tracks topics awaiting more evidence.
```

These notes are **about working with AI**, not about any one project's code. Topics include: model selection, agent orchestration, prompt engineering, failure modes, human-AI workflow philosophy, tooling discoveries.

### When to write a journal entry (proactively, without being asked)

Write a `journal/YYYY-MM-DD-slug.md` entry when something in conversation or work surfaces a transferable insight about how to work with AI:

- Diagnosis of why an agent went sideways, and the principle behind it
- Division-of-labor insight (cheap vs strong models, when to launch subagents)
- Prompt-engineering tells — framings that change model behavior usefully
- Failure modes the user might forget (especially wrong-but-plausible ones)
- A verification gap a reviewer caught that your own checks missed
- A user remark that reframes how the agent should operate

**Conversation-driven, not task-driven.** Write because something was said or noticed that wouldn't be obvious to a future reader who wasn't in the room.

### When NOT to write one

- Routine task summaries → session history, not field notes
- Project-specific code conventions → project `copilot-instructions.md` or `AGENTS.md`
- Single-data-point claims with no story → wait for a second instance, then write the entry that ties them together

### Format

```markdown
# YYYY-MM-DD — {short imperative or question}

**Context**: What conversation/task surfaced this. Be specific.

## What I said (the gist)
The reasoning, expressed crisply.

## What he replied / what we noticed
The human's reaction, especially if it shifted the frame. Quote actual exchanges when they matter.

## What I learned
Numbered transferable principles.

## What we changed (or are about to)
Concrete artifact / instruction change / `_pending.md` entry.

## Quote worth keeping
Optional sentence that captures the principle.
```

### Rules

- **Volunteer them** — don't wait to be asked.
- **Write in the conversation, not after.** Memory rewrites things.
- **Quote actual exchanges.** Don't smooth them.
- **A principle without a story is a slogan.** Always include the story.
- **Don't edit old entries to be right** — write a follow-up. Wrongness is data.
- **Add unsynthesized themes to `essays/_pending.md`** when a conversation hints at a bigger pattern.
- **Commit them.** This is a real repo.

---

## Specification-Driven Development

*Enabled by feature flag: `spec-driven`*

If enabled, the project uses **GitHub spec-kit** as the authoritative workflow. Specifications live in-repo under `.specify/` and `specs/`.

### Workflow
1. **Constitution** (`/speckit-constitution`): Establish project governance.
2. **Specify** (`/speckit-specify`): Define observable behavior in `specs/[id]/spec.md`.
3. **Clarify** (`/speckit-clarify`): Resolve ambiguities before proceeding.
4. **Plan** (`/speckit-plan`): Write the technical approach in `plan.md`.
5. **Tasks** (`/speckit-tasks`): Break execution into `tasks.md`.
6. **Implement** (`/speckit-implement`): Execute tasks and keep artifacts factual.
7. **Analyze** (`/speckit-analyze`): Review deliverables for accuracy and constraints.

### Validation Checklist

When reviewing completed work:
- [ ] Every code change is reflected in the specification artifacts.
- [ ] Every spec claim references actual code (file paths, function names, types)
- [ ] No aspirational claims — spec describes what IS, not what SHOULD BE

### Discovery Workflow
- **Read constitution** (`.specify/memory/constitution.md`) and specs first.
- **Read code only when implementing** — or when the spec has known gaps.

---

## Operator — Peer Agents

*Enabled by feature flag: `operator-agents`*

`operator` runs a **full, first-party Copilot CLI** in its own terminal
session. Starting one gives you a **peer agent, not a sub-agent**: a separate
process with its own context, its own session history and its own git work.
A sub-agent (`task` tool) is a function call that returns to you.

**Delegate to one** when a piece of the work is large, has a **clear
boundary**, and meets the rest of the system through a **defined contract** —
not when you would have to supervise it turn by turn.

**Give it its own folder, ideally its own repo.** Two agents *can* share a
project, but **there is no enforcement**: the only thing keeping a peer in its
lane is the instruction you gave it asking nicely. If the boundary matters, use
separate repos.

```bash
operator --loop --headless --name payments-api --agent anvil
operator send --from <your-instance> --to payments-api "the contract is ..."
operator reply --instance <your-instance> --to payments-api "your answer"
```

**Messages are delivered to you, not polled for.** A message sent while you are
live is typed into your session; one sent while you are between sessions is
printed to you at the start of your next one. Each arrives with its exact reply
command already filled in — use that rather than reconstructing it. **Answer
what you are asked before you write a handoff**: a peer blocked on your reply is
burning sessions doing nothing.

Full reference, including when *not* to spin one up and message etiquette:
the **`peer-agents` skill**.

## Parallel Agents

*Enabled by feature flag: `parallel-agents`*

This is about **sub-agents inside one session** sharing a todo list — not the
separate Copilot CLI processes described under "Operator — Parallel Agents"
above. Operator agents coordinate by mail; these coordinate by database.

When multiple agents collaborate on a feature, coordinate via a shared SQLite database. The protocol relies on atomic `BEGIN IMMEDIATE` transactions to prevent race conditions.

1. **Initialization**: The coordinator must create the tracking table:
   ```sql
   CREATE TABLE IF NOT EXISTS todo_claims (
       todo_id TEXT PRIMARY KEY,
       agent_id TEXT NOT NULL UNIQUE,
       claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
       heartbeat_at TEXT NOT NULL DEFAULT (datetime('now'))
   );
   ```

2. **Identity & Ownership**: Every agent has a unique stable agent ID and claims exactly one todo before changing files.

3. **Ready Work**: Provide exact ready-work SQL excluding claimed or dependency-blocked todos:
   ```sql
   SELECT t.* FROM todos t
   WHERE t.status = 'pending'
     AND NOT EXISTS (SELECT 1 FROM todo_claims c WHERE c.todo_id = t.id)
     AND NOT EXISTS (
         SELECT 1 FROM todo_deps td
         LEFT JOIN todos dep ON td.depends_on = dep.id
         WHERE td.todo_id = t.id
           AND (dep.id IS NULL OR dep.status != 'done')
     )
   ORDER BY t.created_at
   LIMIT 1;
   ```

4. **Atomic Claiming**: Claiming must be atomic in a short `BEGIN IMMEDIATE` transaction and only succeed for a pending, unclaimed todo whose dependencies are all `done`:
   ```sql
   BEGIN IMMEDIATE;
   INSERT OR IGNORE INTO todo_claims (todo_id, agent_id)
     SELECT t.id, '{agent_id}' FROM todos t
     WHERE t.id = '{todo_id}' AND t.status = 'pending'
       AND NOT EXISTS (SELECT 1 FROM todo_claims c WHERE c.todo_id = t.id)
       AND NOT EXISTS (
           SELECT 1 FROM todo_deps td
           LEFT JOIN todos dep ON td.depends_on = dep.id
           WHERE td.todo_id = t.id
             AND (dep.id IS NULL OR dep.status != 'done')
       );
   UPDATE todos
   SET status = 'in_progress', updated_at = datetime('now')
   WHERE id = '{todo_id}' AND status = 'pending'
     AND EXISTS (
         SELECT 1 FROM todo_claims
         WHERE todo_id = '{todo_id}' AND agent_id = '{agent_id}'
     );
   COMMIT;
   ```
   Check `changes()` (or verify status) to ensure the claim succeeded before starting work.

5. **Dependency Awareness**: If preferred work depends on an in-progress item, leave it pending and claim another ready todo. Do not mark dependency waits as blocked.

6. **Releasing**: Completion or a genuine blocker must update status only when the same agent owns the claim, then delete the claim:
   ```sql
   BEGIN IMMEDIATE;
   UPDATE todos
   SET status = '{done_or_blocked}', updated_at = datetime('now')
   WHERE id = '{todo_id}' AND status = 'in_progress'
     AND EXISTS (
         SELECT 1 FROM todo_claims
         WHERE todo_id = '{todo_id}' AND agent_id = '{agent_id}'
     );
   DELETE FROM todo_claims
   WHERE todo_id = '{todo_id}' AND agent_id = '{agent_id}'
     AND EXISTS (
         SELECT 1 FROM todos
         WHERE id = '{todo_id}' AND status = '{done_or_blocked}'
     );
   COMMIT;
   ```
   Substitute either `done` or `blocked` consistently. Dependency waits stay
   `pending` and unclaimed. To return unfinished work, the coordinator sets it
   back to `pending` and deletes the claim in the same transaction. Refresh
   `heartbeat_at` during long-running work.
   Only a coordinator may recover a stale claim after confirming the agent stopped.

7. **Task Granularity**: `[P]` means eligible for parallel execution, not assigned. Same-file work is sequential. Each agent works in its own worktree under `<repoRoot>/.worktrees/` (see **Git Worktrees** above).

8. **Task Artifact Reconciliation**: In parallel mode, worker agents update SQL status and report completion, but ONLY the coordinator serially updates `tasks.md` checkboxes to prevent filesystem conflicts. Single agents update both.

---

## Branching Strategy

*Enabled by feature flag: `branching-strategy`*

- `main` — the integration branch. Feature branches merge here. Don't commit to it directly.
- `feat/xxx`, `fix/xxx`, `docs/xxx` — feature branches off `main`, worked on in a worktree
  (see **Git Worktrees** above).
- There is no `develop` branch. Don't create one and don't assume one exists.
- Use conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`

---

## Common Pitfalls
- Don't write aspirational specs — write factual specs describing what IS
- Don't skip spec verification after implementation
- Don't add features without updating the spec (if spec-driven is enabled)
- Don't hardcode configuration values — use config files or environment variables
- Don't edit files in the primary checkout — create a worktree first
- Don't commit directly on `main` — branch, work in a worktree, then merge back
- Don't write probe scripts, reproductions or scratch copies into a checkout — use a temp directory
- Don't hand a worktree to a subagent with uncommitted work in it — commit first
