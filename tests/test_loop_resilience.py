"""Loop-mode resilience: a launch failure must not kill an unattended loop."""
from __future__ import annotations

import pytest

import copilot_operator as op
from operator_mux import MuxSessionError


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "LAUNCH_BACKOFF_BASE", 0)
    return tmp_path


def test_launch_failure_retries_then_succeeds(monkeypatch):
    """A transient backend failure must be retried, not fatal."""
    attempts = {"n": 0}

    def flaky(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise MuxSessionError("simulated silent failure")
        instance.exit_file.write_text("0", encoding="utf-8")

    monkeypatch.setattr(op, "start_session", flaky)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("retry")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == 0
    assert attempts["n"] == 2, "launch should have been retried after failure"


def test_persistent_launch_failure_eventually_gives_up(monkeypatch):
    """Retrying forever would spin silently; there must be a bound."""
    attempts = {"n": 0}

    def always_fail(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        raise MuxSessionError("always fails")

    monkeypatch.setattr(op, "start_session", always_fail)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("giveup")
    with pytest.raises(MuxSessionError):
        op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)
    assert attempts["n"] == op.MAX_LAUNCH_FAILURES


def test_resume_id_is_restored_when_launch_fails(monkeypatch):
    """A failed launch must not consume the saved resume id."""
    seen = []

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen.append(list(args))
        if len(seen) == 1:
            raise MuxSessionError("boom")
        instance.exit_file.write_text("0", encoding="utf-8")

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("resume")
    sid = "3f2a9c1e-1111-2222-3333-444455556666"
    inst.save_state(2, "2026-07-27T10:00:00Z", sid)

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False)

    assert any(f"--resume={sid}" in a for a in seen[0])
    assert any(f"--resume={sid}" in a for a in seen[1]), \
        "resume id must survive a failed launch"
