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
When a session is complete or getting heavy (large context window), call the
`handoff` command. It takes the handoff text as arguments, writes
`next-session.md`, and raises the restart marker in one step — so the restart
can never be forgotten and a half-written handoff can never trigger one.

```
handoff --instance {instance-name} --status "What was completed" --next "Prioritized next steps" --context "Key decisions, gotchas" --prompt "Ready-to-execute prompt for next session"
```

`--status` and `--next` are required; the command fails rather than writing a
useless handoff. It takes the same arguments on every platform.

1. Present your final output first — everything you want recorded must be
   emitted **before** you call `handoff`.
2. Call `handoff` as your **absolute last action**. The operator kills the
   process as soon as it sees the marker, so anything after it is lost.
3. **Never write `next-session.md` by hand and never touch the marker
   yourself.** Doing it in two steps is exactly what `handoff` exists to
   prevent.
4. This applies to EVERY session end — task-complete or context-heavy.

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
