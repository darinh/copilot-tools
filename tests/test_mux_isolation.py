"""The unit suite must never drive the developer's real terminal multiplexer.

`copilot_operator.MUX` is built at import time, so a test that calls into
copilot_operator without replacing it talks to whatever tmux/psmux server the
machine is running. That was measured, not theorised: 30 unit tests and 134
`tmux has-session` subprocess calls per suite run, which is what made
test_loop_resilience.py::test_unexpected_exit_without_marker_is_relaunched fail
in a full run and pass three times out of three on its own.

conftest's `_no_real_multiplexer` closes that boundary. These tests exist
because a guard nobody checks is indistinguishable from a guard nobody has:
delete the fixture and the suite goes right back to passing, quietly, on a
machine-dependent answer.
"""
from __future__ import annotations

import subprocess

import pytest

import copilot_operator as op
import operator_mux
from conftest import FakeMux
from operator_mux import MuxSessionError


@pytest.fixture
def no_subprocess(monkeypatch):
    """Make any process spawn from operator_mux an immediate, loud failure.

    Patching `operator_mux.subprocess.run` reaches the module object shared by
    every importer, so this catches a spawn from anywhere under the call, not
    just from `Mux._run`.
    """
    def poisoned(*args, **kwargs):
        raise AssertionError(f"a real process was spawned: {args[0]!r}")

    monkeypatch.setattr(operator_mux.subprocess, "run", poisoned)


# ── the guard is installed ──────────────────────────────────────
def test_the_module_level_mux_is_not_a_real_one():
    """If this fails, every test in the suite is talking to a live server."""
    assert isinstance(op.MUX, FakeMux), (
        "copilot_operator.MUX is not the in-memory double -- conftest's "
        "_no_real_multiplexer fixture is missing or was overridden"
    )


def test_the_double_starts_empty():
    """The leaking tests were relying on 'no session of mine exists'. That has
    to keep being the answer, or their assertions change meaning."""
    assert op.MUX.list_sessions() == []
    assert op.MUX.has_session("relaunch-me") is False


def test_the_supervisor_does_not_spawn_a_process(no_subprocess, tmp_path,
                                                 monkeypatch):
    """The regression test for the flake itself.

    The loop crash-relaunches twice and then stops, exactly as
    test_unexpected_exit_without_marker_is_relaunched drives it. Before the
    fix this made five `tmux has-session` calls; with spawning poisoned, any
    one of them fails the test instead of silently depending on the machine.
    """
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)

    attempts = {"n": 0}

    def flaky_session(instance, args, session_num, remain_on_exit=False,
                      preamble=""):
        attempts["n"] += 1
        instance.exit_file.write_text("0", encoding="utf-8")
        if attempts["n"] >= 3:
            instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", flaky_session)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    rc = op.run_loop_mode(op.Instance("relaunch-me"), ["--agent", "test:agent"],
                          is_fresh=True)

    assert rc == 0
    assert attempts["n"] == 3


# ── the double is faithful ──────────────────────────────────────
def test_an_unmodelled_verb_raises_rather_than_inventing_an_answer(no_subprocess):
    """A double that answers a question it does not understand is worse than
    no double: the caller cannot tell the difference from success."""
    with pytest.raises(AssertionError, match="does not model the 'wait-for' verb"):
        FakeMux()._run("wait-for", "-S", "ready")


def test_an_unmodelled_display_format_raises(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    with pytest.raises(AssertionError, match="display format"):
        mux._run("display-message", "-t", "s", "-p", "#{window_name}")


def test_create_query_and_kill_round_trips(no_subprocess, tmp_path):
    """The real `Mux` verbs run unmodified on top of the fake backend."""
    mux = FakeMux()
    mux.new_session("s1", str(tmp_path), ["python", "-c", "pass"])

    assert mux.has_session("s1") is True
    assert mux.list_sessions() == ["s1"]
    assert mux.kill_session("s1") is True
    assert mux.has_session("s1") is False
    assert mux.kill_session("s1") is False, "killing an absent session is not an error"


def test_a_duplicate_session_name_is_refused(no_subprocess, tmp_path):
    """`Mux.new_session` raises on a name already in use. The double must let
    that real check reach its real conclusion."""
    mux = FakeMux()
    mux.new_session("dup", str(tmp_path), ["python"])
    with pytest.raises(MuxSessionError, match="already exists"):
        mux.new_session("dup", str(tmp_path), ["python"])


def test_sending_keys_to_a_missing_session_raises(no_subprocess):
    """The mail delivery path depends on this raising -- a swallowed failure
    files an undelivered message as read. See Mux.send_keys."""
    with pytest.raises(MuxSessionError, match="Failed to send keys"):
        FakeMux().send_keys("nobody", "hello")


def test_sending_keys_records_the_text_and_the_enter(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.send_keys("s", "hello")
    assert mux.keys == [("s", "hello"), ("s", "Enter")]


def test_pane_dead_reflects_the_modelled_state(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    assert mux.pane_dead("s") is False
    mux.sessions["s"]["dead"] = True
    assert mux.pane_dead("s") is True


def test_remain_on_exit_is_recorded(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.set_remain_on_exit("s", True)
    assert mux.sessions["s"]["remain_on_exit"] is True


def test_the_session_argv_and_cwd_survive_the_verb_encoding(no_subprocess, tmp_path):
    """`new_session` passes argv after `--`; the fake has to slice it back out
    at the same offset or every assertion about launch arguments is fiction."""
    argv = ["python", "-c", "print('a b')", "--flag=with space"]
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), argv)
    assert mux.sessions["s"]["argv"] == argv
    assert mux.sessions["s"]["cwd"] == str(tmp_path)


def test_preseeded_sessions_are_visible(no_subprocess):
    mux = FakeMux(sessions=("a", "b"))
    assert sorted(mux.list_sessions()) == ["a", "b"]
    assert mux.has_session("a") is True


def test_the_double_never_looks_for_a_binary(monkeypatch):
    """`Mux.binary` probes PATH with shutil.which and raises when nothing is
    installed. The double must not depend on the machine for that either."""
    def poisoned(_name):
        raise AssertionError("the double probed PATH for a multiplexer")

    monkeypatch.setattr(operator_mux.shutil, "which", poisoned)
    assert FakeMux().binary == "fakemux"
    assert FakeMux().available() is True


def test_subprocess_is_still_reachable_outside_the_poison_fixture():
    """Control: the poison above is a fixture, not a permanent change. Without
    it a spawn works, so the tests that pass are passing on the fake rather
    than on a globally broken subprocess module."""
    assert subprocess.run is operator_mux.subprocess.run
    proc = subprocess.run([__import__("sys").executable, "-c", "print('ok')"],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"
