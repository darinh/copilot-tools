#!/usr/bin/env bash
set -euo pipefail

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 is required" >&2
    exit 1
fi

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT
db="${tmp_dir}/todos.db"

sqlite3 "$db" <<'SQL'
PRAGMA journal_mode = WAL;

CREATE TABLE todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE todo_deps (
    todo_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (todo_id, depends_on)
);

CREATE TABLE todo_claims (
    todo_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL UNIQUE,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now')),
    heartbeat_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO todos (id, title, status) VALUES
    ('foundation', 'Foundation', 'in_progress'),
    ('dependent', 'Dependent work', 'pending'),
    ('ready', 'Ready work', 'pending'),
    ('also-ready', 'Other ready work', 'pending');

INSERT INTO todo_deps (todo_id, depends_on)
VALUES ('dependent', 'foundation');
SQL

claim_todo() {
    local todo_id="$1"
    local agent_id="$2"

    sqlite3 -cmd '.timeout 5000' "$db" <<SQL
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO todo_claims (todo_id, agent_id)
SELECT t.id, '${agent_id}'
FROM todos AS t
WHERE t.id = '${todo_id}'
  AND t.status = 'pending'
  AND NOT EXISTS (
      SELECT 1
      FROM todo_claims AS claim
      WHERE claim.todo_id = t.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM todo_deps AS dependency
      JOIN todos AS prerequisite
        ON prerequisite.id = dependency.depends_on
      WHERE dependency.todo_id = t.id
        AND prerequisite.status != 'done'
  );

UPDATE todos
SET status = 'in_progress',
    updated_at = datetime('now')
WHERE id = '${todo_id}'
  AND status = 'pending'
  AND EXISTS (
      SELECT 1
      FROM todo_claims AS claim
      WHERE claim.todo_id = todos.id
        AND claim.agent_id = '${agent_id}'
  );
COMMIT;
SQL
}

complete_todo() {
    local todo_id="$1"
    local agent_id="$2"

    sqlite3 -cmd '.timeout 5000' "$db" <<SQL
BEGIN IMMEDIATE;
UPDATE todos
SET status = 'done',
    updated_at = datetime('now')
WHERE id = '${todo_id}'
  AND status = 'in_progress'
  AND EXISTS (
      SELECT 1
      FROM todo_claims AS claim
      WHERE claim.todo_id = todos.id
        AND claim.agent_id = '${agent_id}'
  );

DELETE FROM todo_claims
WHERE todo_id = '${todo_id}'
  AND agent_id = '${agent_id}'
  AND EXISTS (
      SELECT 1
      FROM todos
      WHERE id = '${todo_id}'
        AND status = 'done'
  );
COMMIT;
SQL
}

ready_todos() {
    sqlite3 "$db" <<'SQL'
SELECT t.id
FROM todos AS t
WHERE t.status = 'pending'
  AND NOT EXISTS (
      SELECT 1
      FROM todo_claims AS claim
      WHERE claim.todo_id = t.id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM todo_deps AS dependency
      JOIN todos AS prerequisite
        ON prerequisite.id = dependency.depends_on
      WHERE dependency.todo_id = t.id
        AND prerequisite.status != 'done'
  )
ORDER BY t.id;
SQL
}

claim_todo ready agent-a &
pid_a=$!
claim_todo ready agent-b &
pid_b=$!
wait "$pid_a"
wait "$pid_b"

claim_count=$(sqlite3 "$db" "SELECT COUNT(*) FROM todo_claims WHERE todo_id = 'ready';")
[[ "$claim_count" == "1" ]] || {
    echo "expected exactly one claim for a raced todo, got ${claim_count}" >&2
    exit 1
}

winner=$(sqlite3 "$db" "SELECT agent_id FROM todo_claims WHERE todo_id = 'ready';")
loser="agent-a"
[[ "$winner" == "agent-a" ]] && loser="agent-b"

claim_todo ready "$loser"
actual_owner=$(sqlite3 "$db" "SELECT agent_id FROM todo_claims WHERE todo_id = 'ready';")
[[ "$actual_owner" == "$winner" ]] || {
    echo "a losing agent replaced the active owner" >&2
    exit 1
}

ready_before=$(ready_todos)
[[ "$ready_before" == "also-ready" ]] || {
    echo "dependency-blocked todo was returned as ready: ${ready_before}" >&2
    exit 1
}

claim_todo also-ready "$winner"
winner_claims=$(sqlite3 "$db" "SELECT COUNT(*) FROM todo_claims WHERE agent_id = '${winner}';")
[[ "$winner_claims" == "1" ]] || {
    echo "one agent acquired more than one active todo" >&2
    exit 1
}

complete_todo ready "$winner"
remaining_claims=$(sqlite3 "$db" "SELECT COUNT(*) FROM todo_claims WHERE todo_id = 'ready';")
[[ "$remaining_claims" == "0" ]] || {
    echo "completion did not release the active claim" >&2
    exit 1
}

sqlite3 "$db" \
    "UPDATE todos SET status = 'done', updated_at = datetime('now') WHERE id = 'foundation';"

ready_after=$(ready_todos)
expected=$'also-ready\ndependent'
[[ "$ready_after" == "$expected" ]] || {
    echo "ready work did not update after dependency completion: ${ready_after}" >&2
    exit 1
}

claim_todo dependent "$winner"
dependent_owner=$(sqlite3 "$db" "SELECT agent_id FROM todo_claims WHERE todo_id = 'dependent';")
[[ "$dependent_owner" == "$winner" ]] || {
    echo "released agent could not claim newly unblocked work" >&2
    exit 1
}

echo "todo claim coordination tests passed"
