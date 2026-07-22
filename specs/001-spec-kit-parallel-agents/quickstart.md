# Quickstart: Spec Kit and Parallel Agents

## Install the toolchain

```bash
./setup.sh
specify version
```

Setup installs the pinned official spec-kit CLI only when `specify` is absent.
Override the pin for testing with `SPEC_KIT_VERSION=vX.Y.Z ./setup.sh`.

## Initialize another repository

```bash
cd /path/to/project
specify init --here --force \
  --integration copilot \
  --integration-options="--skills" \
  --script sh
```

Then establish the project constitution and create feature artifacts with:

```text
/speckit-constitution
/speckit-specify
/speckit-clarify
/speckit-plan
/speckit-tasks
/speckit-implement
/speckit-analyze
```

## Coordinate parallel implementation

Before dispatching agents, create the shared claim table:

```sql
CREATE TABLE IF NOT EXISTS todo_claims (
    todo_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL UNIQUE,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    heartbeat_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Each agent uses a unique stable ID, claims exactly one ready todo in a short
transaction, and works only after confirming ownership. If its preferred todo
has an incomplete dependency, it queries for another pending, unclaimed todo
whose dependencies are all done.

## Verify this repository

```bash
bash -n setup.sh
bash tests/test-setup-spec-kit.sh
bash tests/test-todo-claims.sh
.specify/scripts/bash/check-prerequisites.sh --json --require-tasks --include-tasks
python3 -m json.tool templates/mcp-config.json >/dev/null
```
