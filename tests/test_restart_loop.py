"""`operator restart-loop`: replace a supervisor, keep the session.

The loop supervisor is a long-lived process that imported the operator's code
when it started, so a running instance only picks up new code when its
supervisor is replaced. `operator stop` does that but takes the Copilot
session down with it. These tests pin the behaviour that makes replacing the
supervisor safe: the session survives untouched, and adoption refuses to run
against a session this operator does not own.
"""
from __future__ import annotations

import json

import pytest

import copilot_operator as op


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


def _claim(instance: op.Instance) -> None:
    """Mark instance as owning a live session, the way a real launch does."""
    instance.claim("test-token")


def _live_session(monkeypatch, live: bool = True, only: str | None = None):
    """Point MUX at a session that is (or is not) running.

    ``only`` restricts liveness to a single session name, so a test can run an
    adopting instance against a live session and a control instance against
    nothing in the same process.
    """
    state = {"v": live}
    monkeypatch.setattr(
        op.MUX, "has_session",
        lambda session: state["v"] and (only is None or session == only))
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)
    return state


def _fake_supervisor(monkeypatch, instance, pid: int = 4242):
    """A supervisor that behaves like the real one during a handoff.

    It reports ``pid`` until the detach marker appears, then does what a real
    supervisor does — *consumes* the marker and exits. Tests that skip the
    consume step exercise the "something else stopped it" path instead.
    """
    state = {"pid": pid}

    def running(inst):
        if state["pid"] is not None and instance.detach_marker.exists():
            instance.detach_marker.unlink(missing_ok=True)
            state["pid"] = None
        return state["pid"]

    monkeypatch.setattr(op, "_running_loop_pid", running)
    return state


# ── loop-args round trip ────────────────────────────────────────

def test_loop_args_round_trip():
    """A supervisor must be reproducible from what was recorded about it."""
    inst = op.Instance("argsave")
    op._save_loop_args(inst, ["--agent", "anvil:anvil", "--model", "x"])

    args, cwd = op._load_loop_args(inst)
    assert args == ["--agent", "anvil:anvil", "--model", "x"]
    assert cwd is not None


def test_loop_args_missing_is_reported_not_guessed():
    """Nothing recorded must not silently become an empty argument list: a
    supervisor restarted with no args is a different supervisor."""
    inst = op.Instance("noargs")
    assert op._load_loop_args(inst) == ([], None)


def test_loop_args_survive_a_corrupt_file():
    """A truncated write must not take the caller down with it."""
    inst = op.Instance("corrupt")
    inst.loop_args_file.write_text("{not json", encoding="utf-8")
    assert op._load_loop_args(inst) == ([], None)


def test_loop_args_reject_non_string_args():
    """Hand-edited or version-skewed payloads must not reach subprocess."""
    inst = op.Instance("badtypes")
    inst.loop_args_file.write_text(
        json.dumps({"user_args": ["--agent", 7], "cwd": "/tmp"}), encoding="utf-8")
    assert op._load_loop_args(inst) == ([], None)


def test_loop_args_are_recorded_by_loop_mode(monkeypatch):
    """restart-loop can only reproduce a supervisor if loop mode wrote the
    args down in the first place."""
    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    _live_session(monkeypatch, live=False)

    inst = op.Instance("records")
    saved: dict = {}
    real_save = op._save_loop_args

    def spy(instance, user_args):
        saved["args"] = list(user_args)
        real_save(instance, user_args)

    monkeypatch.setattr(op, "_save_loop_args", spy)
    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert saved["args"] == ["--agent", "test:agent"]


# ── adoption ────────────────────────────────────────────────────

def test_adopt_takes_over_without_launching_a_session(monkeypatch):
    """The whole point: the running session must not be relaunched."""
    calls = {"start": 0, "stop_gracefully": 0}

    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        calls["start"] += 1

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully",
                        lambda instance: calls.__setitem__(
                            "stop_gracefully", calls["stop_gracefully"] + 1))
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    _live_session(monkeypatch)

    inst = op.Instance("adoptme")
    _claim(inst)
    inst.session_file.write_text(
        "11111111-2222-3333-4444-555555555555", encoding="utf-8")
    # Ask the adopted loop to detach immediately so the test terminates.
    inst.detach_marker.touch()

    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                          adopt=True)

    assert rc == 0
    assert calls["start"] == 0, "adoption must not launch a new session"
    assert calls["stop_gracefully"] == 0, "adoption must not stop the session"


def test_adopt_keeps_the_running_sessions_number(monkeypatch):
    """A launch moves to the next session; adoption joins the current one.

    Incrementing here would make session numbering — and the metrics keyed to
    it — count a session that never happened.
    """
    seen = {}

    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen["num"] = session_num
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    inst = op.Instance("numbering")
    # Only the adopting instance has a live session; the control launches into
    # an empty slot, which is what makes it a fair comparison.
    _live_session(monkeypatch, only=inst.session)
    _claim(inst)
    inst.save_state(4, op.utcnow())
    inst.session_file.write_text(
        "11111111-2222-3333-4444-555555555555", encoding="utf-8")
    inst.detach_marker.touch()

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False, adopt=True)

    state = inst.load_state()
    assert state["SESSION_NUM"] == "4", "adoption must not consume a session number"

    # Control: the same state without adoption advances to #5.
    inst2 = op.Instance("numbering2")
    _claim(inst2)
    inst2.save_state(4, op.utcnow())
    op.run_loop_mode(inst2, ["--agent", "test:agent"], is_fresh=False)
    assert seen["num"] == 5


def test_adopt_refuses_when_no_session_is_running(monkeypatch):
    """Adopting nothing would leave a supervisor polling a session that will
    never appear, so it must fail loudly instead."""
    monkeypatch.setattr(op, "start_session", lambda *a, **k: None)
    _live_session(monkeypatch, live=False)

    inst = op.Instance("ghost")
    _claim(inst)
    with pytest.raises(SystemExit):
        op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                         adopt=True)


def test_adopt_refuses_a_session_it_does_not_own(monkeypatch):
    """A session with the right name but no ownership claim belongs to
    somebody else; supervising it would fight its real owner.

    handle_existing_session is stubbed out because it *also* exits (on the
    prompt's EOF), which would let this pass even with the guard removed.
    """
    monkeypatch.setattr(op, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(op, "handle_existing_session", lambda instance: None)
    _live_session(monkeypatch)

    inst = op.Instance("notmine")  # no claim() call
    with pytest.raises(SystemExit):
        op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                         adopt=True)


def test_adopt_does_not_prompt_about_the_existing_session(monkeypatch):
    """handle_existing_session prompts, and a headless supervisor cannot
    answer — that EOF is the bug this feature exists to remove."""
    def boom(instance):
        raise AssertionError("adoption must not go through handle_existing_session")

    monkeypatch.setattr(op, "handle_existing_session", boom)
    monkeypatch.setattr(op, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    _live_session(monkeypatch)

    inst = op.Instance("noprompt")
    _claim(inst)
    inst.detach_marker.touch()

    assert op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                            adopt=True) == 0


def test_only_the_first_session_is_adopted(monkeypatch):
    """Adoption applies to the session already running. Once it ends, the
    supervisor must go back to launching normally."""
    launches = []

    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        launches.append(session_num)
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    alive = {"v": True}
    monkeypatch.setattr(op.MUX, "has_session", lambda session: alive["v"])
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)
    monkeypatch.setattr(op.MUX, "kill_session", lambda session: True)

    inst = op.Instance("oneshot")
    _claim(inst)
    inst.save_state(2, op.utcnow())
    inst.session_file.write_text(
        "11111111-2222-3333-4444-555555555555", encoding="utf-8")
    # Adopted session #2 is asked to restart, so #3 must be a real launch.
    inst.restart_marker.touch()

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False, adopt=True)

    assert launches == [3], f"expected one real launch of session #3, got {launches}"


# ── the restart-loop command ────────────────────────────────────

def test_restart_loop_replaces_supervisor_and_keeps_session(monkeypatch):
    spawned: dict = {}

    inst = op.Instance("swapme")
    _claim(inst)
    op._save_loop_args(inst, ["--agent", "anvil:anvil"])
    state = _fake_supervisor(monkeypatch, inst)

    def fake_spawn(instance, copilot_args, is_fresh, adopt=False, cwd=None):
        spawned["args"] = list(copilot_args)
        spawned["adopt"] = adopt
        spawned["is_fresh"] = is_fresh
        state["pid"] = 9999
        return 9999

    monkeypatch.setattr(op, "_spawn_background_loop", fake_spawn)
    killed = {"n": 0}
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op.MUX, "kill_session",
                        lambda session: killed.__setitem__("n", killed["n"] + 1))

    rc = op.restart_loop("swapme")

    assert rc == 0
    assert spawned["adopt"] is True, "replacement must adopt, not relaunch"
    assert spawned["is_fresh"] is False
    assert spawned["args"] == ["--agent", "anvil:anvil"], \
        "replacement must reuse the original arguments"
    assert killed["n"] == 0, "the session must never be killed"


def test_restart_loop_asks_the_old_supervisor_to_detach(monkeypatch):
    """It must use the detach marker (session survives), never the stop
    marker (session dies)."""
    touched: list[str] = []

    inst = op.Instance("detachnotstop")
    _claim(inst)
    op._save_loop_args(inst, [])
    state = _fake_supervisor(monkeypatch, inst)

    monkeypatch.setattr(op, "_spawn_background_loop",
                        lambda *a, **k: state.__setitem__("pid", 1) or 1)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)

    real_touch = op.Path.touch

    def spy_touch(self, *a, **k):
        touched.append(self.name)
        real_touch(self, *a, **k)

    monkeypatch.setattr(op.Path, "touch", spy_touch)

    assert op.restart_loop("detachnotstop") == 0
    assert inst.detach_marker.name in touched
    assert inst.stop_marker.name not in touched, \
        "stop marker would take the session down with the supervisor"


def test_restart_loop_refuses_when_no_session_is_running(monkeypatch):
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "must not spawn a supervisor with no session to adopt"))

    inst = op.Instance("nosession")
    _claim(inst)
    op._save_loop_args(inst, [])
    assert op.restart_loop("nosession") == 1


def test_restart_loop_refuses_an_unowned_session(monkeypatch):
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "must not supervise a session this operator does not own"))

    op._save_loop_args(op.Instance("unowned"), [])
    assert op.restart_loop("unowned") == 1


def test_restart_loop_refuses_when_args_were_never_recorded(monkeypatch):
    """Silently restarting with no arguments would produce a supervisor that
    differs from the one it replaced — wrong agent, wrong model."""
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: 1234)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "must not spawn a supervisor with unknown arguments"))

    inst = op.Instance("legacy")
    _claim(inst)
    assert op.restart_loop("legacy") == 1


def test_restart_loop_starts_one_when_none_is_running(monkeypatch):
    """`stop-loop` then `restart-loop` must work: a session with no
    supervisor is exactly what this should be able to fix."""
    spawned = {"n": 0}
    pid_state = {"v": None}

    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: pid_state["v"])

    def fake_spawn(instance, copilot_args, is_fresh, adopt=False, cwd=None):
        spawned["n"] += 1
        spawned["adopt"] = adopt
        pid_state["v"] = 777
        return 777

    monkeypatch.setattr(op, "_spawn_background_loop", fake_spawn)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)

    inst = op.Instance("orphan")
    _claim(inst)
    op._save_loop_args(inst, ["--agent", "test:agent"])

    assert op.restart_loop("orphan") == 0
    assert spawned == {"n": 1, "adopt": True}


def test_restart_loop_requires_a_name():
    assert op.restart_loop(None) == 1
    assert op.restart_loop("") == 1


# ── handoff hazards found in adversarial review ─────────────────

def test_concurrent_restarts_do_not_spawn_two_supervisors(monkeypatch):
    """Two supervisors watching one session relaunch over each other's work
    forever. Only one handoff may be in flight at a time."""
    spawned = {"n": 0}
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: None)

    inst = op.Instance("concurrent")
    _claim(inst)
    op._save_loop_args(inst, [])

    def reentrant_spawn(instance, copilot_args, is_fresh, adopt=False, cwd=None):
        spawned["n"] += 1
        if spawned["n"] == 1:
            # A second restart arriving mid-handoff must be turned away.
            assert op.restart_loop("concurrent") == 1
        return 1

    monkeypatch.setattr(op, "_spawn_background_loop", reentrant_spawn)
    monkeypatch.setattr(op, "_running_loop_pid",
                        lambda instance: 55 if spawned["n"] else None)

    assert op.restart_loop("concurrent") == 0
    assert spawned["n"] == 1, "the second restart must not have spawned anything"


def test_restart_lock_is_released_so_a_later_restart_works(monkeypatch):
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)

    inst = op.Instance("lockrelease")
    _claim(inst)
    op._save_loop_args(inst, [])
    state = _fake_supervisor(monkeypatch, inst, pid=55)
    monkeypatch.setattr(op, "_spawn_background_loop",
                        lambda *a, **k: state.__setitem__("pid", 56) or 56)

    assert op.restart_loop("lockrelease") == 0
    assert not inst.restart_lock_file.exists(), "lock must not outlive the handoff"
    assert op.restart_loop("lockrelease") == 0


def test_a_stale_restart_lock_is_reclaimed(monkeypatch):
    """A crash mid-handoff must not wedge the instance forever."""
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)

    inst = op.Instance("stalelock")
    _claim(inst)
    op._save_loop_args(inst, [])
    state = _fake_supervisor(monkeypatch, inst, pid=55)
    monkeypatch.setattr(op, "_spawn_background_loop",
                        lambda *a, **k: state.__setitem__("pid", 56) or 56)
    # A pid that is definitely not running.
    inst.restart_lock_file.write_text("999999999", encoding="utf-8")

    assert op.restart_loop("stalelock") == 0


def test_restart_loop_does_not_resurrect_a_session_that_stop_killed(monkeypatch):
    """If `operator stop` wins the race, the supervisor exits on the *stop*
    marker and never consumes our detach request. Spawning an adopting
    supervisor then would bring back a session the user just stopped."""
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "must not replace a supervisor that something else stopped"))

    inst = op.Instance("stoprace")
    _claim(inst)
    op._save_loop_args(inst, [])

    # Supervisor dies, but the detach marker is left behind untouched.
    pids = iter([4242, None, None, None])
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: next(pids, None))

    assert op.restart_loop("stoprace") == 1
    assert not inst.detach_marker.exists(), "the unconsumed marker must be cleaned up"


def test_restart_loop_aborts_if_the_session_dies_during_handoff(monkeypatch):
    """The session is re-checked after the handoff, not trusted from before."""
    alive = {"v": True}
    monkeypatch.setattr(op.MUX, "has_session", lambda session: alive["v"])
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "must not adopt a session that no longer exists"))

    inst = op.Instance("vanished")
    _claim(inst)
    op._save_loop_args(inst, [])

    def dying(instance):
        # Consume the detach request, then the session disappears.
        instance.detach_marker.unlink(missing_ok=True)
        alive["v"] = False
        return None

    monkeypatch.setattr(op, "_running_loop_pid", dying)
    assert op.restart_loop("vanished") == 1


def test_restart_loop_refuses_a_missing_working_directory(monkeypatch, tmp_path):
    """Spawning from the caller's cwd instead would point the instance at a
    different project entirely."""
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: pytest.fail(
        "must reject before touching the running supervisor"))
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "must not spawn into the wrong directory"))

    inst = op.Instance("movedproject")
    _claim(inst)
    inst.loop_args_file.write_text(
        json.dumps({"user_args": [], "cwd": str(tmp_path / "gone")}),
        encoding="utf-8")

    assert op.restart_loop("movedproject") == 1


def test_restart_loop_spawns_into_the_recorded_directory(monkeypatch, tmp_path):
    """The replacement must run where the original did, without mutating this
    process's working directory to get there."""
    project = tmp_path / "project"
    project.mkdir()
    captured: dict = {}
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: 7)

    inst = op.Instance("intodir")
    _claim(inst)
    inst.loop_args_file.write_text(
        json.dumps({"user_args": [], "cwd": str(project)}), encoding="utf-8")
    state = _fake_supervisor(monkeypatch, inst, pid=7)

    def fake_spawn(instance, copilot_args, is_fresh, adopt=False, cwd=None):
        captured["cwd"] = cwd
        captured["process_cwd"] = str(op.Path.cwd())
        state["pid"] = 8
        return 8

    monkeypatch.setattr(op, "_spawn_background_loop", fake_spawn)

    before = str(op.Path.cwd())
    assert op.restart_loop("intodir") == 0
    assert captured["cwd"] == str(project)
    assert captured["process_cwd"] == before, \
        "restart-loop must not chdir the operator process"
    assert str(op.Path.cwd()) == before


def test_restart_loop_survives_a_failed_spawn(monkeypatch):
    """Crashing here would leave a live session unsupervised with a traceback
    instead of an explanation."""
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: None)

    def boom(*a, **k):
        raise OSError("no more processes")

    monkeypatch.setattr(op, "_spawn_background_loop", boom)

    inst = op.Instance("spawnfail")
    _claim(inst)
    op._save_loop_args(inst, [])

    assert op.restart_loop("spawnfail") == 1
    assert not inst.restart_lock_file.exists()


def test_adopt_refuses_when_another_supervisor_is_alive(monkeypatch):
    """Belt and braces behind the handoff lock: a supervisor that finds
    another one already recorded must not start."""
    monkeypatch.setattr(op, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(op, "handle_existing_session", lambda instance: None)
    _live_session(monkeypatch)

    inst = op.Instance("twosupervisors")
    _claim(inst)
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: 4242)

    with pytest.raises(SystemExit):
        op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                         adopt=True)


# ── plumbing ────────────────────────────────────────────────────

def test_spawn_passes_adopt_flag_to_the_supervisor(monkeypatch):
    """--adopt is what tells the re-exec'd process to take over rather than
    launch; losing it in transit would kill the session it was meant to save."""
    captured: dict = {}

    class FakeProc:
        pid = 4321

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(op.subprocess, "Popen", fake_popen)

    inst = op.Instance("spawncheck")
    op._spawn_background_loop(inst, ["--agent", "test:agent"], is_fresh=False,
                              adopt=True)
    assert "--adopt" in captured["cmd"]
    assert "--fresh" not in captured["cmd"]

    op._spawn_background_loop(inst, ["--agent", "test:agent"], is_fresh=False)
    assert "--adopt" not in captured["cmd"], "adoption must be opt-in"


def test_adopt_flag_is_parsed_and_threaded_through(monkeypatch):
    """The supervisor is re-exec'd as a fresh process, so --adopt only works
    if the argument parser routes it into run_loop_mode."""
    seen: dict = {}

    def fake_loop(instance, user_args, is_fresh, adopt=False):
        seen["adopt"] = adopt
        seen["args"] = list(user_args)
        return 0

    monkeypatch.setattr(op, "run_loop_mode", fake_loop)
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op, "register_tab", lambda *a, **k: None)

    op.run_dispatch(["--_supervise", "--loop", "--name", "x", "--adopt",
                     "--agent", "test:agent"])
    assert seen["adopt"] is True
    assert seen["args"] == ["--agent", "test:agent"], \
        "--adopt must not leak through to the Copilot CLI"

    op.run_dispatch(["--_supervise", "--loop", "--name", "x",
                     "--agent", "test:agent"])
    assert seen["adopt"] is False


def test_restart_loop_is_a_reserved_word():
    """Otherwise `operator restart-loop` would be read as an instance name."""
    assert "restart-loop" in op.RESERVED_WORDS


def test_restart_loop_is_dispatched(monkeypatch):
    called: dict = {}
    monkeypatch.setattr(op, "restart_loop",
                        lambda target: called.__setitem__("target", target) or 0)
    monkeypatch.setattr(op, "migrate_legacy_state", lambda: None)

    assert op.main(["restart-loop", "someinst"]) == 0
    assert called["target"] == "someinst"


def test_loop_args_file_is_cleaned_up_with_the_rest():
    """A stale args file would let a later restart-loop resurrect a
    supervisor for an instance that was deliberately stopped."""
    inst = op.Instance("cleanme")
    op._save_loop_args(inst, ["--agent", "test:agent"])
    assert inst.loop_args_file.exists()

    inst.cleanup_files()
    assert not inst.loop_args_file.exists()
