"""Regression tests for adversarial review findings.

Each test names the failure it prevents rather than the function it calls, so
the reason it exists survives future refactoring.
"""
from __future__ import annotations

import sqlite3
import subprocess

import pytest

import copilot_operator as op
import operator_ingest
import operator_mux
from operator_mux import Mux, MuxSessionError, safe_instance_id


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    return tmp_path


# ── ownership ───────────────────────────────────────────────────
def test_stale_state_does_not_authorize_killing_a_foreign_session(monkeypatch, capsys):
    """Continuity state outlives a session. If an unrelated session later takes
    the same name, that leftover file must not authorize destroying it."""
    inst = op.Instance("proj")
    inst.save_state(3, "2026-07-27T10:00:00Z")     # continuity only, no claim
    assert inst.is_managed()

    killed = []
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s) or True)

    rc = op.stop_operator("proj")

    assert rc == 1
    assert killed == [], "a session we do not own must never be killed"
    assert "not started by this operator" in capsys.readouterr().err


def test_stop_all_skips_sessions_without_a_matching_claim(monkeypatch):
    inst = op.Instance("proj")
    inst.save_state(1, "2026-07-27T10:00:00Z")
    killed = []
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id])
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s) or True)

    op.stop_operator()
    assert killed == []


def test_claimed_session_is_stoppable(monkeypatch):
    inst = op.Instance("proj")
    inst.claim("tok")
    killed = []
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s) or True)

    assert op.stop_operator("proj") == 0
    assert killed == [inst.id]


def test_forget_removes_state_without_touching_the_session(monkeypatch, capsys):
    inst = op.Instance("proj")
    inst.save_state(2, "2026-07-27T10:00:00Z")
    killed = []
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s) or True)

    assert op.forget_instance("proj") == 0
    assert killed == []
    assert not inst.state_file.exists()


# ── working directory guard ─────────────────────────────────────
def test_filesystem_root_requires_an_explicit_name(monkeypatch):
    """Bash refuses to run in the filesystem root; silently defaulting would
    point Copilot at an entire drive."""
    class FakeRoot:
        name = ""

        def __str__(self):
            return "C:\\"

    monkeypatch.setattr(op.Path, "cwd", staticmethod(lambda: FakeRoot()))
    with pytest.raises(SystemExit):
        op.default_instance_name()


def test_normal_directory_yields_its_name(monkeypatch, tmp_path):
    d = tmp_path / "my-project"
    d.mkdir()
    monkeypatch.setattr(op.Path, "cwd", staticmethod(lambda: d))
    assert op.default_instance_name() == "my-project"


# ── backend honesty ─────────────────────────────────────────────
def test_kill_session_raises_when_the_session_survives(monkeypatch):
    """Reporting success while a session still runs makes the caller delete
    state for something that is still alive."""
    mux = Mux(binary="tmux")

    def fake_run(cmd, **kwargs):
        verb = cmd[1]
        rc = 1 if verb == "kill-session" else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="denied")

    monkeypatch.setattr(operator_mux.subprocess, "run", fake_run)
    with pytest.raises(MuxSessionError):
        mux.kill_session("stubborn")


# ── instance identity ───────────────────────────────────────────
def test_case_only_variants_do_not_alias_into_shared_state():
    """On Windows and macOS 'Build' and 'build' address the same file and the
    same backend session, so they must be one instance, not two sharing state."""
    a, b = safe_instance_id("Build"), safe_instance_id("build")
    if operator_mux._CASE_INSENSITIVE_FS:
        assert a == b
    else:
        assert a != b


# ── log parsing ─────────────────────────────────────────────────
def test_unmatched_quote_in_prose_does_not_hide_later_events():
    """Copilot logs interleave prose with JSON. A stray quote must not leave
    the scanner stuck and silently drop the shutdown event."""
    text = (
        '2026-07-27T10:00:00.000Z [info] user said "hello\n'
        '2026-07-27T10:30:00.000Z [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {},\n'
        '  "metrics": {"lines_added": 4, "lines_removed": 2}\n'
        '}\n'
    )
    event = operator_ingest.extract_shutdown_event(text)
    assert event is not None
    assert event["metrics"]["lines_added"] == 4


def test_unmatched_brace_in_prose_does_not_hide_later_events():
    text = (
        '2026-07-27T10:00:00.000Z [info] stray { brace in prose\n'
        '2026-07-27T10:30:00.000Z [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {},\n'
        '  "metrics": {"lines_added": 6, "lines_removed": 1}\n'
        '}\n'
    )
    event = operator_ingest.extract_shutdown_event(text)
    assert event is not None
    assert event["metrics"]["lines_added"] == 6


# ── schema migration ────────────────────────────────────────────
def test_concurrent_schema_migration_does_not_fail(tmp_path):
    """Two operators upgrading the same older database can both see the column
    as missing; losing that race must not be an error."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_num INTEGER NOT NULL, log_file TEXT UNIQUE, "
        "no_op INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, "
        "ended_at TEXT NOT NULL);"
        "CREATE TABLE model_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id INTEGER NOT NULL, model_name TEXT NOT NULL);"
    )
    conn.commit()
    conn.close()

    operator_ingest.init_db(db)
    operator_ingest.init_db(db)   # second migration attempt must be a no-op

    with operator_ingest.connect(db) as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    assert "log_file_mtime" in cols
