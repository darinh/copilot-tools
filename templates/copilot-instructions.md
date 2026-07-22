# User Workflow Conventions

These conventions apply to all projects I work on. They complement any agent-specific instructions (e.g., Anvil).

---

## Project Configuration System

Each project can have a persistent configuration stored outside the repo at `~/.copilot/projects/`.

### Catalog

`~/.copilot/projects/catalog.csv` maps project root paths to GUIDs:

```csv
"/home/user/projects/my-app",a1b2c3d4-e5f6-7890-abcd-ef1234567890
"/home/user/projects/other-project",f9e8d7c6-b5a4-3210-fedc-ba0987654321
```

### Per-Project Directory

`~/.copilot/projects/{guid}/` contains:
- `copilot-instructions.md` — project-specific conventions and feature flags
- `next-session.md` — session handoff file (ephemeral, read-once)
- `specs/` — living specifications (if spec-driven development is enabled)
- Any other project artifacts that should persist outside the repo

### On Session Start — Project Lookup

1. Determine the current project root (git root or cwd).
2. Read `~/.copilot/projects/catalog.csv` and look for a matching path.
3. **If found**: Read `~/.copilot/projects/{guid}/copilot-instructions.md` and follow its conventions. Check for `next-session.md` handoff.
4. **If not found**: Ask the user:
   - "This project isn't in the catalog yet. Would you like to set it up?"
   - Choices: "Enable all features" / "Select features" / "Skip for now"
   - If enabling: generate a GUID, create the directory, write `copilot-instructions.md` with selected features, add entry to `catalog.csv`.
   - **If spec-driven is selected and `.specify/` is missing**, instruct the agent to initialize with `specify init --here --force --integration copilot --integration-options="--skills" --script sh`.

### Feature Selection

When setting up a new project, the user selects which conventions to enable:

| Feature | Description | Default |
|---------|-------------|---------|
| **Session Handoff** | `next-session.md` for cross-session continuity | ON |
| **Session History** | SQL `session_log` table for audit trail | ON |
| **Spec-Driven Development** | Spec as source of truth. Uses GitHub spec-kit. Location: `.specify/` and `specs/`. | ON |
| **Parallel Agents** | SQL-coordinated parallel task execution via `todo_claims`. | ON |
| **Branching Strategy** | develop → feature branches, conventional commits | ON |

The generated `copilot-instructions.md` includes only the enabled sections.

---

## Session Handoff Protocol

*Enabled by feature flag: `session-handoff`*

Agents use `~/.copilot/projects/{guid}/next-session.md` for continuity across sessions.

### On Session Start
When the user greets you (e.g., "hey", "hello", "hi"), **immediately**:

1. **Check for unmerged work**: Run `git branch --no-merged` against the integration branch (usually `develop` or `main`). If any feature branches have unmerged commits, tell the user: *"Found unmerged work on branch X (N commits). Want to continue that, merge it, or start fresh?"*
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

```bash
handoff --instance <operator-instance-name> \
  --status "What was completed (commits, branches, files)" \
  --next "Prioritized next steps" \
  --context "Key decisions, gotchas" \
  --prompt "Ready-to-execute prompt for next session"
```

The `handoff` command atomically writes the handoff file AND triggers the operator restart. **Never write the handoff file manually** — always use the command.

If the `handoff` command is not available (e.g., not on PATH), fall back to writing `~/.copilot/projects/{guid}/next-session.md` manually and then running `touch ~/.operator/restart/{instance-name}`.

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

Cross-project working journal of insights about building, instructing, and collaborating with AI agents. Lives in its own repo (not per-project): `~/projects/agent-field-notes/`.

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
1. **Specify**: Define observable behavior in `specs/[id]/spec.md`.
2. **Clarify**: Resolve ambiguities in `spec.md` before proceeding.
3. **Plan**: Write the technical approach in `plan.md`.
4. **Tasks**: Break down execution into `tasks.md`.
5. **Implement**: Execute tasks, keeping specs factual. Update `spec.md`, `plan.md`, and `tasks.md` with delivered behavior.
6. **Analyze**: Review deliverables for accuracy and constraints.

### Validation Checklist

When reviewing completed work:
- [ ] Every code change is reflected in the specification artifacts.
- [ ] Every spec claim references actual code (file paths, function names, types)
- [ ] The changelog has an entry for the change
- [ ] No aspirational claims — spec describes what IS, not what SHOULD BE

### Discovery Workflow
- **Read constitution** (`.specify/memory/constitution.md`) and specs first.
- **Read code only when implementing** — or when the spec has known gaps.

---

## Parallel Agents

*Enabled by feature flag: `parallel-agents`*

When multiple agents collaborate on a feature, coordinate via a shared SQLite database:

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
3. **Atomic Claiming**: Claiming must be atomic in a short `BEGIN IMMEDIATE` transaction and only succeed for a pending, unclaimed todo whose dependencies are all `done`.
4. **Ready Work**: Provide exact ready-work SQL excluding claimed or dependency-blocked todos.
5. **Dependency Awareness**: If preferred work depends on an in-progress item, leave it pending and claim another ready todo. Do not mark dependency waits as blocked.
6. **Releasing**: Completion, real blockers, or releasing must update status and claim coherently. Only a coordinator may recover a stale claim after confirming the agent stopped.
7. **Task Granularity**: `[P]` means eligible for parallel execution, not assigned. Same-file work is sequential. Work in isolated worktrees.

---

## Branching Strategy

*Enabled by feature flag: `branching-strategy`*

- `main` — stable releases only. Never push directly.
- `develop` — integration branch. PRs from feature branches merge here.
- `feat/xxx`, `fix/xxx`, `docs/xxx` — feature branches off `develop`.
- All work happens on feature branches. PRs go to `develop`.
- Use conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`

---

## Common Pitfalls
- Don't write aspirational specs — write factual specs describing what IS
- Don't skip spec verification after implementation
- Don't add features without updating the spec (if spec-driven is enabled)
- Don't hardcode configuration values — use config files or environment variables
- Don't push directly to `main` — always use a feature branch and PR
