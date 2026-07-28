"""Tests for the in-pane session supervisor."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import operator_runner
from conftest import make_log


# ── log attribution ─────────────────────────────────────────────
def test_find_log_matches_exact_pid(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    mine = logs / "process-1700000000000-4242.log"
    mine.write_text("x", encoding="utf-8")
    (logs / "process-1700000000000-9999.log").write_text("x", encoding="utf-8")
    assert operator_runner._find_log(logs, 4242, 1700000000000) == mine


def test_find_log_never_falls_back_to_newest(tmp_path):
    """The bash version grabbed the newest log on a PID miss, which let one
    instance record another's usage. A miss must return None."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "process-1700000000000-1111.log").write_text("x", encoding="utf-8")
    assert operator_runner._find_log(logs, 4242, 1700000000000) is None


def test_find_log_ignores_older_launches(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "process-1000-4242.log").write_text("old", encoding="utf-8")
    current = logs / "process-1700000000000-4242.log"
    current.write_text("new", encoding="utf-8")
    assert operator_runner._find_log(logs, 4242, 1699999999999) == current


def test_find_log_ignores_logs_far_after_launch(tmp_path):
    """PID reuse: a much later Copilot run that happened to get the same PID
    must not be attributed to this session."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "process-1800000000000-4242.log").write_text("much later", encoding="utf-8")
    assert operator_runner._find_log(logs, {4242}, 1700000000000) is None


def test_find_log_prefers_the_launch_closest_in_time(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    near = logs / "process-1700000001000-4242.log"
    near.write_text("near", encoding="utf-8")
    (logs / "process-1700000500000-4242.log").write_text("far", encoding="utf-8")
    assert operator_runner._find_log(logs, {4242}, 1700000000000) == near


def test_find_log_handles_missing_directory(tmp_path):
    assert operator_runner._find_log(tmp_path / "nope", 1, 0) is None


# ── session id extraction ───────────────────────────────────────
def test_extract_session_id_from_json_field(tmp_path):
    log = tmp_path / "a.log"
    log.write_text(
        'noise\n{"session_id": "3f2a9c1e-1111-2222-3333-444455556666"}\n',
        encoding="utf-8",
    )
    assert operator_runner._extract_session_id(log) == \
        "3f2a9c1e-1111-2222-3333-444455556666"


def test_extract_session_id_from_workspace_line(tmp_path):
    log = tmp_path / "b.log"
    log.write_text(
        "Workspace initialized: aaaabbbb-cccc-dddd-eeee-ffff00001111\n",
        encoding="utf-8",
    )
    assert operator_runner._extract_session_id(log) == \
        "aaaabbbb-cccc-dddd-eeee-ffff00001111"


def test_extract_session_id_absent(tmp_path):
    log = tmp_path / "c.log"
    log.write_text("nothing to see\n", encoding="utf-8")
    assert operator_runner._extract_session_id(log) is None


# ── end-to-end supervision ──────────────────────────────────────
def test_runner_records_pid_and_exit_code(tmp_path, state_dir, db_path, launch_spec):
    spec = launch_spec([sys.executable, "-c", "import sys; sys.exit(7)"])
    rc = operator_runner.run(spec)
    assert rc == 7
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "7"
    # The pid file is transient and removed once the child exits.
    assert not (state_dir / "testinst.pid").exists()


def test_runner_clears_stale_exit_marker(tmp_path, state_dir, db_path, launch_spec):
    (state_dir / "testinst.exit").write_text("99", encoding="utf-8")
    spec = launch_spec([sys.executable, "-c", "pass"])
    operator_runner.run(spec)
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "0"


def test_runner_reports_missing_executable(tmp_path, state_dir, launch_spec):
    spec = launch_spec(["definitely-not-a-real-binary-xyz"])
    assert operator_runner.run(spec) == 127
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "127"


def test_runner_captures_metrics_for_its_own_pid(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """The whole point of the runner: metrics are attributed to the exact
    process it launched, and captured even though the operator has gone."""
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "pass"], session_num=5, log_dir=logs)

    real_popen = operator_runner.subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            make_log(logs / f"process-{int(time.time() * 1000)}-{self.pid}.log")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)

    assert operator_runner.run(spec) == 0

    import operator_ingest
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT session_num, no_op FROM sessions").fetchone()
    assert row is not None, "runner must record metrics after the child exits"
    assert row["session_num"] == 5
    assert row["no_op"] == 0


def test_runner_writes_no_metrics_when_log_absent(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """No guessing: absent log means no record, never another instance's."""
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "pass"], log_dir=logs)
    make_log(logs / "process-1700000000000-999999.log")
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)
    operator_runner.run(spec)

    import operator_ingest
    operator_ingest.init_db(db_path)
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
