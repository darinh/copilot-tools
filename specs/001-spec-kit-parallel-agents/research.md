# Research: Spec Kit and Parallel Agents

## Spec-kit distribution and integration

**Decision**: Pin the official GitHub source release `v0.13.4` and initialize
GitHub Copilot with `--integration-options="--skills"`.

**Rationale**: The installed local CLI reported that legacy Copilot prompt mode
is deprecated. Skills mode is the supported forward path and avoids committing
both agent and prompt wrappers.

**Alternatives considered**:

- Unpinned `uv tool install specify-cli`: simpler, but setup output could change
  without a repository update.
- Legacy Copilot prompt mode: rejected because spec-kit warns it will stop being
  the default.

## Missing `uv`

**Decision**: Bootstrap `uv` with Astral's documented installer only when both
`specify` and `uv` are absent, then install spec-kit from the pinned GitHub tag.

**Rationale**: Official spec-kit documentation recommends `uv`; fresh setup must
not stop at manual instructions when the requested tool is absent.

**Alternatives considered**:

- Make `uv` a hard prerequisite: rejected because setup is responsible for
  installing spec-kit.
- Install through system `pip`: rejected because externally managed Python
  environments and user-site path behavior are less predictable.

## Todo ownership model

**Decision**: Use a separate `todo_claims` table instead of altering the built-in
`todos` table.

**Rationale**: `CREATE TABLE IF NOT EXISTS` is safe to initialize before
parallel dispatch, while conditional `ALTER TABLE ADD COLUMN` is not portable
across SQLite versions and is prone to initialization races.

**Alternatives considered**:

- Store only `status='in_progress'`: rejected because it does not identify the
  owner.
- Add `assigned_to` directly to `todos`: rejected because schema migration is
  harder to make idempotent and race-free.
- Markdown-only ownership in `tasks.md`: rejected because concurrent writes are
  not atomic.

## Ready-work selection

**Decision**: A todo is ready only when it is pending, unclaimed, and every
dependency is `done`.

**Rationale**: This directly implements the requirement to avoid work whose
prerequisite is in progress and lets agents select other useful work.

## MCP cleanup

**Decision**: Retain Roslyn for C# structural analysis and direct all other
languages to built-in code search and language-aware tools.

**Rationale**: This removes the low-value integration without weakening the
supported C# path.
