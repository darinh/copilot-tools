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

### Feature Selection

When setting up a new project, the user selects which conventions to enable:

| Feature | Description | Default |
|---------|-------------|---------|
| **Session Handoff** | `next-session.md` for cross-session continuity | ON |
| **Session History** | SQL `session_log` table for audit trail | ON |
| **Spec-Driven Development** | `specs/` directory as source of truth, mandatory spec change proposals | OFF |
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

If the `handoff` command is not available (e.g., not on PATH), fall back to writing `~/.copilot/projects/{guid}/next-session.md` manually and then running `touch ~/.copilot/restart/{instance-name}`.

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

## Specification-Driven Development

*Enabled by feature flag: `spec-driven`*

If enabled, the project's `specs/` directory (at `~/.copilot/projects/{guid}/specs/` or in-repo — whichever is configured) is the single source of truth.

### Plans ARE Spec Change Proposals

Every implementation plan must include a **Spec Change Proposal** section:

```
## Spec Change Proposal
- **Sections affected**: [spec section numbers and names]
- **Change type**: NEW_CAPABILITY | MODIFICATION | BUG_FIX_CODE | BUG_FIX_SPEC
- **Proposed changes**: [what the spec will say after implementation]
- **Verification**: [how to confirm spec accuracy against delivered code]
```

### Validation Checklist

When reviewing completed work:
- [ ] Every code change has a corresponding spec update
- [ ] Every spec claim references actual code (file paths, function names, types)
- [ ] The changelog has an entry for the change
- [ ] No aspirational claims — spec describes what IS, not what SHOULD BE

### Discovery Workflow
- **Read specs first** — specs have complete API contracts, DB schemas, behavior rules.
- **Read code only when implementing** — or when the spec says "Planned" or has known gaps.

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
