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
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "LAUNCH_BACKOFF_BASE", 0)
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)
    return tmp_path


def test_launch_failure_retries_then_succeeds(monkeypatch):
    """A transient backend failure must be retried, not fatal."""
    attempts = {"n": 0}

    def flaky(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise MuxSessionError("simulated silent failure")
        instance.exit_file.write_text("0", encoding="utf-8")
        # A launch that succeeds and then exits with no restart marker is now
        # treated as an unexpected crash and relaunched; stop the loop here so
        # this test only exercises the launch-failure retry, not that.
        instance.stop_marker.touch()

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
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("resume")
    sid = "3f2a9c1e-1111-2222-3333-444455556666"
    inst.save_state(2, "2026-07-27T10:00:00Z", sid)

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False)

    assert any(f"--resume={sid}" in a for a in seen[0])
    assert any(f"--resume={sid}" in a for a in seen[1]), \
        "resume id must survive a failed launch"


def test_resume_without_handoff_file_gets_crash_note(monkeypatch, tmp_path):
    """Resuming with no handoff file for the project is treated as crash
    recovery and gets a note added to the preamble."""
    seen_preambles = []

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen_preambles.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op, "project_handoff_file", lambda cwd: None)

    inst = op.Instance("crashy")
    sid = "3f2a9c1e-1111-2222-3333-444455556666"
    inst.save_state(1, "2026-07-27T10:00:00Z", sid)

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False)

    assert "crash" in seen_preambles[0].lower()


def test_resume_with_handoff_file_present_has_no_crash_note(monkeypatch, tmp_path):
    """When the project's handoff file exists, resuming is a normal
    continuation, not crash recovery — no note should be added."""
    seen_preambles = []

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen_preambles.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    handoff = tmp_path / "next-session.md"
    handoff.write_text("# handoff", encoding="utf-8")

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op, "project_handoff_file", lambda cwd: handoff)

    inst = op.Instance("tidy")
    sid = "3f2a9c1e-1111-2222-3333-444455556666"
    inst.save_state(1, "2026-07-27T10:00:00Z", sid)

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False)

    assert "crash" not in seen_preambles[0].lower()


def test_fresh_run_has_no_crash_note(monkeypatch):
    """A --fresh loop has no resume id at all, so there is nothing to be a
    crash recovery of."""
    seen_preambles = []

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen_preambles.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("fresh-run")
    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert "crash" not in seen_preambles[0].lower()


def test_unexpected_exit_without_marker_is_relaunched(monkeypatch):
    """An unexpected session death (crash, or `operator stop-session`) with no
    restart marker and no stop/detach marker must be relaunched automatically
    rather than ending the loop — that's the whole point of "loop" mode."""
    attempts = {"n": 0}

    def flaky_session(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        if attempts["n"] < 3:
            instance.exit_file.write_text("0", encoding="utf-8")
        else:
            instance.exit_file.write_text("0", encoding="utf-8")
            instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", flaky_session)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("relaunch-me")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == 0
    assert attempts["n"] == 3, "each unexpected exit should trigger a fresh launch"


def test_repeated_unexpected_exits_eventually_give_up(monkeypatch):
    """Unbounded crash-relaunching would spin forever; there must be a cap
    distinct from (but the same size as) the launch-failure cap."""
    attempts = {"n": 0}

    def always_crashes(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        instance.exit_file.write_text("0", encoding="utf-8")

    monkeypatch.setattr(op, "start_session", always_crashes)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("doomed")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == 1
    assert attempts["n"] == op.MAX_LAUNCH_FAILURES


def test_a_session_that_ran_for_minutes_does_not_count_toward_the_give_up_limit(
        monkeypatch):
    """Five deaths hours apart must not retire a loop the way five in a minute do.

    The consecutive-exit limit is there to stop a hot relaunch spin, but it
    counted exits and never their spacing. This machine's operator.log shows
    what that costs: on four separate occasions every instance died within
    seconds of every other, independent of when each was launched, each having
    run for minutes. Five such waves and every supervisor retired itself, so
    the user came back to nothing running.

    A session that stayed up past the healthy threshold restarts the count, so
    only genuinely rapid failures can still exhaust it. The negative control is
    `test_repeated_unexpected_exits_eventually_give_up`, whose sessions die
    instantly and must still give up at the cap.
    """
    clock = {"t": 1_000.0}
    monkeypatch.setattr(op.time, "time", lambda: clock["t"])

    attempts = {"n": 0}
    keep_going = op.MAX_LAUNCH_FAILURES * 3

    def dies(instance, args, session_num, remain_on_exit=False, preamble=""):
        attempts["n"] += 1
        if attempts["n"] > keep_going:
            # Unbounded by construction now, so the test must end it.
            raise KeyboardInterrupt
        instance.exit_file.write_text("0", encoding="utf-8")

    really_running = op.is_copilot_running

    def aged(instance):
        # Age the session past the healthy threshold before its death is
        # noticed, which is the only way the supervisor can tell a session
        # that ran from one that never started.
        clock["t"] += op.HEALTHY_SESSION_SECONDS + 1
        return really_running(instance)

    monkeypatch.setattr(op, "start_session", dies)
    monkeypatch.setattr(op, "is_copilot_running", aged)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)

    inst = op.Instance("long-lived")
    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert attempts["n"] > op.MAX_LAUNCH_FAILURES, (
        "the supervisor gave up at the cap even though every session had been "
        "up for longer than HEALTHY_SESSION_SECONDS before it died")
    assert attempts["n"] == keep_going + 1


def test_detach_marker_leaves_session_running(monkeypatch):
    """`operator stop-loop NAME` (a touched detach marker) must stop the
    supervisor without touching the session or calling stop_session_gracefully."""
    calls = {"start": 0, "stop_gracefully": 0}
    session_live = {"v": False}

    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        calls["start"] += 1
        instance.session_file.write_text(
            "11111111-2222-3333-4444-555555555555", encoding="utf-8")
        instance.detach_marker.touch()
        session_live["v"] = True

    def fake_stop_gracefully(instance):
        calls["stop_gracefully"] += 1

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", fake_stop_gracefully)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: session_live["v"])
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    inst = op.Instance("detach-me")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == 0
    assert calls["stop_gracefully"] == 0, "detach must not stop the session"
    assert not inst.detach_marker.exists()
    assert not inst.loop_pid_file.exists()


def test_stop_marker_stops_session_and_supervisor(monkeypatch):
    """`operator stop NAME` (a touched stop marker) must stop both the
    supervisor and the session."""
    calls = {"stop_gracefully": 0, "kill_session": 0}
    session_live = {"v": False}

    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        instance.session_file.write_text(
            "11111111-2222-3333-4444-555555555555", encoding="utf-8")
        instance.stop_marker.touch()
        session_live["v"] = True

    def fake_stop_gracefully(instance):
        calls["stop_gracefully"] += 1

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", fake_stop_gracefully)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: session_live["v"])
    monkeypatch.setattr(op.MUX, "kill_session", lambda session: calls.__setitem__(
        "kill_session", calls["kill_session"] + 1) or True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    inst = op.Instance("stop-me")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == 0
    assert calls["stop_gracefully"] == 1
    assert calls["kill_session"] == 1
    assert not inst.stop_marker.exists()
    assert not inst.loop_pid_file.exists()


def test_an_unexplained_exit_is_traced_with_its_real_exit_code(monkeypatch):
    """Reproduces the 2026-08-03 die-off: copilot shuts down cleanly, no
    marker explains it, and the loop counts a crash.

    `operator.log` can only say "exited unexpectedly", which reads as a crash
    and is why seven loops looked like a machine-wide fault. The runner has
    written the real code to the exit file all along; the trace now records
    it, so rc=0 -- an orderly shutdown nobody asked us to expect -- is
    distinguishable from a session that actually died.

    This is also the event no invocation log can see: not one operator command
    is run during it.
    """
    import json

    import operator_trace

    def clean_exit_no_marker(instance, args, session_num,
                             remain_on_exit=False, preamble=""):
        instance.exit_file.write_text("0", encoding="utf-8")

    monkeypatch.setattr(op, "start_session", clean_exit_no_marker)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("tracer")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)
    assert rc == 1, "five unexplained exits should end the loop"

    lines = operator_trace.trace_path(op.OPERATOR_HOME).read_text(
        encoding="utf-8").splitlines()
    exits = [json.loads(x) for x in lines
             if json.loads(x).get("event") == "session_exit"]
    assert len(exits) == op.MAX_LAUNCH_FAILURES, (
        "every unexplained exit should be traced, not just the last")
    assert [e["consecutive"] for e in exits] == list(
        range(1, op.MAX_LAUNCH_FAILURES + 1))
    assert exits[-1]["giving_up"] is True
    assert exits[0]["giving_up"] is False
    assert all(e["instance"] == "tracer" for e in exits)
    assert exits[-1]["markers"]["exit_code"] == 0, (
        "the exit code the runner recorded is the whole point")
    assert exits[-1]["markers"]["restart"] is False
