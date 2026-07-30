# Project Name — Project Instructions

> **Project root**: `C:\Users\dev\repos\my-project` (Windows) or `/home/dev/projects/my-project` (Linux/macOS)
> **GUID**: `{generated-guid}`

---

## Feature Flags

| Feature | Status |
|---------|--------|
| Session Handoff | ✅ ON |
| Session History | ✅ ON |
| Spec-Driven Development | ✅ ON |
| Parallel Agents | ✅ ON |
| Branching Strategy | ✅ ON |

---

## Session Handoff

Enabled. See `~/.copilot/copilot-instructions.md` for protocol.

Handoff file: `~/.copilot/projects/{guid}/next-session.md`

### Operator Restart Protocol
When a session is complete or getting heavy (large context window), you MUST:
1. Write `next-session.md` with full handoff context
2. Present your final output to the user (summary of what was delivered, evidence bundles, etc.) — **everything you want recorded in the session log must be output BEFORE step 3**
3. As your **absolute last action**, create the restart marker from your operator preamble using the
   form for your platform — the operator kills the process immediately after detecting this file, so any
   output after this point is lost:

   **PowerShell (Windows)**
   ```powershell
   New-Item -ItemType File -Force ~/.operator/restart/{instance-name}
   ```

   **bash (Linux/macOS/WSL)**
   ```bash
   touch ~/.operator/restart/{instance-name}
   ```
4. **DO NOT output anything after creating the marker.** No summary, no farewell, no tool calls. It is the final thing you do.
5. **DO NOT FORGET STEP 3.** The handoff file is useless without the restart trigger.
6. This applies to EVERY session end — whether task-complete or context-heavy.

---

## Session History

Enabled. Log sessions to `session_log` SQL table per protocol.

---

## Branching Strategy

Enabled. Follow convention:
- `main` — the integration branch; feature branches merge here
- `feat/xxx`, `fix/xxx`, `docs/xxx` — feature branches off `main`, worked on in a worktree under
  `<repoRoot>/.worktrees/` (see `~/.copilot/copilot-instructions.md`)
- No `develop` branch
- Conventional commits: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`

---

## Project-Specific Notes

- **Stack**: (e.g., .NET 9, React + Fluent UI, SQLite, Docker Compose)
- **Build**: (e.g., `dotnet build MySolution.sln`)
- **Test**: (e.g., `dotnet test MySolution.sln`)
