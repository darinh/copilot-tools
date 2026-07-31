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
checkout: the `catalog.csv` lookup, the per-project directory, the handoff file, `.specify/`
initialization. A worktree is a second directory for the same project, not a second project. Cataloging
one mints a duplicate GUID and silently splits the project's state in two.

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

## Project Configuration System

Each project can have a persistent configuration stored outside the repo at `~/.copilot/projects/`.

### Catalog

`~/.copilot/projects/catalog.csv` maps project root paths to GUIDs. Paths are stored in the **native
form of the platform that created the entry**:

```csv
"C:\Users\dev\repos\my-app",a1b2c3d4-e5f6-7890-abcd-ef1234567890
"/home/dev/projects/other-project",f9e8d7c6-b5a4-3210-fedc-ba0987654321
```

When matching, normalize the current project root before comparing. On Windows compare
case-insensitively; on Linux and macOS compare case-sensitively.

### Per-Project Directory

`~/.copilot/projects/{guid}/` contains:
- `copilot-instructions.md` — project-specific conventions and feature flags
- `next-session.md` — session handoff file (ephemeral, read-once)
- Any other project artifacts that should persist outside the repo

### On Session Start — Project Lookup

1. Determine the current project root. This is the **primary checkout** — if you are in a worktree,
   resolve it with `git worktree list --porcelain` as described under **Git Worktrees**, never with
   `git rev-parse --show-toplevel`. Fall back to the cwd only outside a git repo.
2. Read `~/.copilot/projects/catalog.csv` and look for a matching path.
3. **If found**: Read `~/.copilot/projects/{guid}/copilot-instructions.md` and follow its conventions. Check for `next-session.md` handoff.
4. **If not found**: Ask the user:
   - "This project isn't in the catalog yet. Would you like to set it up?"
   - Choices: "Enable all features" / "Select features" / "Skip for now"
   - If enabling: generate a GUID, create the directory, write `copilot-instructions.md` with selected features, add entry to `catalog.csv`.
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
| **Session Handoff** | `next-session.md` for cross-session continuity | ON |
| **Session History** | SQL `session_log` table for audit trail | ON |
| **Spec-Driven Development** | Spec as source of truth. Uses GitHub spec-kit. Location: `.specify/` and `specs/`. | ON |
| **Parallel Agents** | SQL-coordinated parallel task execution via `todo_claims`. | ON |
| **Branching Strategy** | Feature branches in worktrees, merged to `main`, conventional commits | ON |

The generated `copilot-instructions.md` includes only the enabled sections.

---

## Session Handoff Protocol

*Enabled by feature flag: `session-handoff`*

Agents use `~/.copilot/projects/{guid}/next-session.md` for continuity across sessions.

### On Session Start
When the user greets you (e.g., "hey", "hello", "hi"), **immediately**:

1. **Check for unmerged work**: Run `git branch --no-merged main` (the ref matters — with no argument git compares against HEAD, which tells you nothing). If any feature branches have unmerged commits, tell the user: *"Found unmerged work on branch X (N commits). Want to continue that, merge it, or start fresh?"*
2. **Read handoff**: Check if `~/.copilot/projects/{guid}/next-session.md` exists. If it does:
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
`~/.copilot/projects/{guid}/next-session.md` manually and then creating the restart marker file using
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

### Rules
- The file is ephemeral — read once, then delete. Not documentation.
- Write it proactively. The user should never have to ask for it.

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

## Operator — Parallel Agents

`operator` runs a **full, first-party Copilot CLI** in its own terminal
session. Starting one gives you a **peer agent, not a sub-agent**: a separate
process with its own context, its own session history and its own git work.
A sub-agent (`task` tool) is a function call that returns to you. An operator
agent is a colleague that keeps working after you stop watching.

**Delegate to one** when a piece of the work is large, has a **clear
boundary**, and meets the rest of the system through a **defined contract** —
not when you would have to supervise it turn by turn.

**Give it its own folder, ideally its own repo.** Instance names, handoff files
and git state are all keyed to the directory, and two loops in one working tree
fight over the index and each other's uncommitted changes. Two agents *can*
share a project, but understand what that is: **there is no enforcement.** The
only thing keeping a parallel agent in its lane is the instruction you gave it
asking nicely — a vibe-wish, not a sandbox. If the boundary matters, use
separate repos.

```bash
# Start a peer without your terminal being taken over by its TUI:
operator --loop --headless --name payments-api --agent anvil

# Talk to it. --from and --to are required so it knows who to answer:
operator send --from <your-instance> --to payments-api "the contract is ..."
operator inbox          # read your own messages
```

Your instance name is in your session preamble ("Operator instance: ...").
Messages reach a running agent immediately and a sleeping one at the start of
its next session. **Check `operator inbox` when you start work and before you
write a handoff**, and answer what you are asked — a peer blocked on your reply
is burning sessions doing nothing.

Full reference, including when *not* to spin one up and message etiquette:
the **`operator-agents` skill**.

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
