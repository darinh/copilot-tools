# Project Name — Project Instructions

> **Project root**: `/path/to/project`
> **GUID**: `{generated-guid}`

---

## Feature Flags

| Feature | Status |
|---------|--------|
| Session Handoff | ✅ ON |
| Session History | ✅ ON |
| Spec-Driven Development | ❌ OFF |
| Branching Strategy | ✅ ON |

---

## Session Handoff

Enabled. See `~/.copilot/copilot-instructions.md` for protocol.

Handoff file: `~/.copilot/projects/{guid}/next-session.md`

### Operator Restart Protocol
When a session is complete or getting heavy (large context window), you MUST:
1. Write `next-session.md` with full handoff context
2. Present your final output to the user (summary of what was delivered, evidence bundles, etc.) — **everything you want recorded in the session log must be output BEFORE step 3**
3. As your **absolute last action**, run the restart touch command from your operator preamble (e.g., `touch ~/.copilot/restart/{instance-name}`) — the operator kills the process immediately after detecting this file, so any output after the touch is lost
4. **DO NOT output anything after the touch.** No summary, no farewell, no tool calls. The touch is the final thing you do.
5. **DO NOT FORGET STEP 3.** The handoff file is useless without the restart trigger.
6. This applies to EVERY session end — whether task-complete or context-heavy.

---

## Session History

Enabled. Log sessions to `session_log` SQL table per protocol.

---

## Branching Strategy

Enabled. Follow convention:
- `main` — stable releases only
- `develop` — integration branch
- `feat/xxx`, `fix/xxx`, `docs/xxx` — feature branches off `develop`
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`

---

## Project-Specific Notes

- **Stack**: (e.g., .NET 9, React + Fluent UI, SQLite, Docker Compose)
- **Build**: (e.g., `dotnet build MySolution.sln`)
- **Test**: (e.g., `dotnet test MySolution.sln`)
