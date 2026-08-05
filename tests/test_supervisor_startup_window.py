"""A supervisor must be visible from the moment it exists, not from the
moment it finishes starting (backlog 0010).

The loop pid file is written near the end of a startup with a measured 105 ms
floor, and it has to stay that way: every reader of the *code record* treats
it as the commit point. So for that whole window `_running_loop_pid` reported
that nothing was running, and the two commands that act destructively on that
answer did the wrong thing silently --

* `operator stop` never set the stop marker, killed the multiplexer session
  itself, and the supervisor -- seeing a session vanish with no marker, which
  is exactly the crash case -- launched a *fresh* one underneath the person
  who had just asked for everything to stop;
* `operator restart-loop` found no supervisor and started a second one, the
  state the code itself calls "relaunching over each other indefinitely".

These tests pin the record that closes the window, the readers that consult
it, and -- just as important -- the two callers that must keep using
`_running_loop_pid`, because they are *confirming a supervisor came up* and
the startup record is written by the spawn itself.
"""
from __future__ import annotations

import os
import time

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


def _dead_pids(monkeypatch, *alive: int) -> None:
    """Make liveness deterministic: only the listed pids are alive.

    Picking a "surely dead" number instead would make the test's meaning
    depend on what else the machine happens to be running.
    """
    monkeypatch.setattr(op, "_pid_alive", lambda pid: pid in alive)


def _age_record(instance: op.Instance, seconds: float) -> None:
    """Backdate the startup record by ``seconds``."""
    path = instance.loop_startup_file
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime - seconds))


class _Clock:
    """A clock that only advances when the code under test sleeps.

    Several paths here poll against a `time.time()` deadline measured in tens
    of seconds. Stubbing `sleep` to do nothing makes those spin for the full
    wall-clock budget; stubbing the clock as well makes them finish at once
    while still taking exactly the number of polls they really would.
    Everything else is delegated, so this stays a clock and not a mock of the
    `time` module.
    """

    def __init__(self) -> None:
        self.now = time.time()

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.001)

    def __getattr__(self, name):
        return getattr(time, name)


def _fast_clock(monkeypatch) -> _Clock:
    """Install the clock above for `copilot_operator` only.

    Patched on the module's own binding rather than on the stdlib module, so
    pytest and everything else keep the real one.
    """
    clock = _Clock()
    monkeypatch.setattr(op, "time", clock)
    return clock


# ── the record ──────────────────────────────────────────────────

def test_no_record_means_no_starting_supervisor():
    """The baseline the rest of the file is measured against: absence has to
    keep meaning absence, or every caller below simply blocks forever."""
    assert op._starting_loop_pid(op.Instance("nobody")) is None
    assert op._supervisor_present(op.Instance("nobody")) is None


def test_a_published_pid_still_answers_first(monkeypatch):
    """The pid file remains the primary signal; the startup record only
    covers the window before it exists."""
    inst = op.Instance("published")
    inst.loop_pid_file.write_text("4242", encoding="utf-8")
    _dead_pids(monkeypatch, 4242)

    assert op._supervisor_present(inst) == 4242


def test_a_starting_supervisor_is_visible_before_it_publishes(monkeypatch):
    """The fix, in one assertion. No pid file exists -- the supervisor has
    not got that far -- and it must still be found."""
    inst = op.Instance("starting")
    op._record_supervisor_starting(inst, 777)
    _dead_pids(monkeypatch, 777)

    assert op._running_loop_pid(inst) is None, \
        "precondition: the narrow reader must still see nothing here"
    assert op._supervisor_present(inst) == 777


def test_a_dead_recorded_pid_is_still_believed_while_the_record_is_young(monkeypatch):
    """On Windows `sys.executable` is usually a launcher shim that re-execs
    the real interpreter and exits, so the pid the parent recorded is dead
    while the supervisor it started is starting normally. Requiring the pid
    to be alive would reopen the window on exactly one platform."""
    inst = op.Instance("shim")
    op._record_supervisor_starting(inst, 31337)
    _dead_pids(monkeypatch)  # nothing is alive

    assert op._supervisor_present(inst) == 31337


def test_an_old_record_naming_a_dead_pid_is_pruned(monkeypatch):
    """The other direction: a supervisor that died during startup must stop
    being believed, or the obvious retry of `operator restart-loop` refuses
    for as long as the grace lasts."""
    inst = op.Instance("crashed")
    op._record_supervisor_starting(inst, 31337)
    _dead_pids(monkeypatch)
    _age_record(inst, op.SUPERVISOR_STARTUP_GRACE + 5)

    assert op._supervisor_present(inst) is None
    assert not inst.loop_startup_file.exists(), \
        "a record nobody will ever honour must not be left for the next reader"


def test_an_old_record_naming_a_live_pid_is_kept(monkeypatch):
    """Age alone must not retire a supervisor that is demonstrably there --
    a startup that shells out to the multiplexer can outlast the grace."""
    inst = op.Instance("slowstart")
    op._record_supervisor_starting(inst, 555)
    _dead_pids(monkeypatch, 555)
    _age_record(inst, op.SUPERVISOR_STARTUP_GRACE + 60)

    assert op._supervisor_present(inst) == 555
    assert inst.loop_startup_file.exists()


def test_an_unparsable_young_record_reports_present_not_absent(monkeypatch):
    """A half-written record is not evidence that nobody is starting. It
    reports 0 -- "present, pid unknown" -- because the alternative is a
    caller concluding absence and destroying a session."""
    inst = op.Instance("torn")
    inst.loop_startup_file.write_text("", encoding="utf-8")
    _dead_pids(monkeypatch)

    assert op._supervisor_present(inst) == 0
    assert op._supervisor_present(inst) is not None


def test_an_unexaminable_record_reports_present_not_absent(monkeypatch):
    """`stat` failing says nothing about whether a supervisor is there, and
    the callers of this all act destructively on "no"."""
    inst = op.Instance("denied")
    op._record_supervisor_starting(inst, 999)
    real_stat = op.Path.stat

    def denied(self, *a, **k):
        if self.name == inst.loop_startup_file.name:
            raise PermissionError(13, "denied")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(op.Path, "stat", denied)

    assert op._supervisor_present(inst) == 0


# ── the record's lifetime ───────────────────────────────────────

def test_spawning_records_the_supervisor_before_returning(monkeypatch):
    """The parent writes it because the child cannot: nothing the child does
    can precede its own interpreter starting and this module importing."""
    inst = op.Instance("spawned")

    class FakeProc:
        pid = 24680

    monkeypatch.setattr(op.subprocess, "Popen", lambda *a, **k: FakeProc())
    _dead_pids(monkeypatch, 24680)

    assert op._spawn_background_loop(inst, [], is_fresh=False) == 24680
    assert op._supervisor_present(inst) == 24680


def test_publishing_the_pid_retires_the_startup_record(monkeypatch):
    """Removed *after* the pid file exists, never before -- a gap between
    them is the whole bug, in miniature."""
    inst = op.Instance("handover")
    op._record_supervisor_starting(inst, 111)
    seen: dict = {}
    real_remove = op.remove_file

    def spy(path):
        if path.name == inst.loop_startup_file.name:
            seen["pid_file_existed"] = inst.loop_pid_file.exists()
        return real_remove(path)

    monkeypatch.setattr(op, "remove_file", spy)
    op._publish_supervisor_records(inst, ["--agent", "x"])

    assert seen["pid_file_existed"] is True, \
        "the startup record was dropped before the pid file replaced it"
    assert not inst.loop_startup_file.exists()
    assert op._supervisor_present(inst) is not None, \
        "the supervisor must stay continuously visible across the handover"


def test_loop_mode_claims_the_record_with_its_own_pid(monkeypatch):
    """The parent's pid may be a shim's. Only the child knows the pid that
    will still be alive a second from now."""
    seen: list[int] = []
    real_record = op._record_supervisor_starting

    def spy(instance, pid):
        seen.append(pid)
        real_record(instance, pid)

    monkeypatch.setattr(op, "_record_supervisor_starting", spy)
    monkeypatch.setattr(op, "start_session",
                        lambda instance, *a, **k: instance.stop_marker.touch())
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    op.run_loop_mode(op.Instance("claimer"), ["--agent", "test:agent"],
                     is_fresh=True)

    assert seen and seen[0] == os.getpid(), \
        f"loop mode must claim the record as its first act, got {seen}"


def test_the_record_is_gone_once_the_supervisor_exits(monkeypatch):
    """Otherwise every caller waits out the grace for a process that has
    already returned."""
    monkeypatch.setattr(op, "start_session",
                        lambda instance, *a, **k: instance.stop_marker.touch())
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    inst = op.Instance("tidy")
    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert not inst.loop_startup_file.exists()
    assert not inst.loop_pid_file.exists()


# ── operator stop ───────────────────────────────────────────────

def test_stop_signals_a_supervisor_that_has_not_published_yet(monkeypatch):
    """Consequence 1 of the item. Without the marker the supervisor reads the
    vanished session as a crash and relaunches a fresh one."""
    inst = op.Instance("stopstarting")
    op._record_supervisor_starting(inst, 4321)
    _dead_pids(monkeypatch, 4321)
    monkeypatch.setattr(op.time, "sleep", lambda s: None)

    op._request_supervisor_stop(inst, timeout=0.0)

    assert inst.stop_marker.exists(), \
        "a starting supervisor was asked to stop by nothing at all"


def test_stop_sets_the_marker_before_looking_for_a_supervisor(monkeypatch):
    """Ordering, not just outcome: a supervisor that becomes visible between
    the two must still find the marker waiting for it."""
    inst = op.Instance("ordering")
    op._record_supervisor_starting(inst, 4321)
    _dead_pids(monkeypatch, 4321)
    monkeypatch.setattr(op.time, "sleep", lambda s: None)
    seen: dict = {}

    real_present = op._supervisor_present

    def spy(instance):
        seen.setdefault("marker_at_check", instance.stop_marker.exists())
        return real_present(instance)

    monkeypatch.setattr(op, "_supervisor_present", spy)
    op._request_supervisor_stop(inst, timeout=0.0)

    assert seen["marker_at_check"] is True


def test_stop_leaves_no_marker_behind_when_nothing_is_running():
    """The cost of setting the marker first, paid back. A marker left in the
    state directory is a trap: the next supervisor started for this name
    consumes it and shuts itself down, and nobody asked it to."""
    inst = op.Instance("nosupervisor")

    op._request_supervisor_stop(inst, timeout=0.0)

    assert not inst.stop_marker.exists()


def test_a_refused_stop_leaves_no_marker_behind(monkeypatch):
    """The concrete way that trap gets armed: `stop_operator` refuses a
    session it does not own and returns *without* running `cleanup_files`,
    so nothing downstream would ever remove it."""
    inst = op.Instance("unownedstop")
    inst.save_state(1, op.utcnow())  # managed, but never claimed
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda session: pytest.fail(
        "must not kill a session this operator does not own"))

    assert op.stop_operator("unownedstop") == 1
    assert not inst.stop_marker.exists()


def test_stop_waits_for_a_starting_supervisor_to_go(monkeypatch):
    """It must not race ahead and kill the session itself while the
    supervisor is still on its way to reading the marker."""
    inst = op.Instance("waitforit")
    op._record_supervisor_starting(inst, 8888)
    polls = {"n": 0}

    def present(instance):
        polls["n"] += 1
        if polls["n"] >= 3:
            op.remove_file(instance.loop_startup_file)
            return None
        return 8888

    monkeypatch.setattr(op, "_supervisor_present", present)
    _fast_clock(monkeypatch)

    op._request_supervisor_stop(inst, timeout=30.0)

    assert polls["n"] >= 3, "stop returned without waiting for the supervisor"


# ── the supervisor's side of a stop that arrives during startup ──

def test_a_stop_that_arrives_during_startup_launches_no_session(monkeypatch):
    """The marker is only half the fix. A supervisor that reads it on its
    first poll has already launched a fresh Copilot session under someone who
    asked for everything to stop -- and an agent that runs for two seconds
    can still commit."""
    launched = {"n": 0}

    def start(instance, *a, **k):
        launched["n"] += 1
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    inst = op.Instance("stopfirst")
    inst.stop_marker.touch()

    assert op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True) == 0
    assert launched["n"] == 0, \
        "a pending stop request must be honoured before anything is launched"
    assert not inst.stop_marker.exists(), "the request must be consumed"


def test_a_detach_that_arrives_during_startup_launches_no_session(monkeypatch):
    """Same for the marker `restart-loop` retires a supervisor with: the
    caller is blocked waiting for this process to go, and making it sit
    through a session launch first is what its timeout budget pays for."""
    launched = {"n": 0}

    def start(instance, *a, **k):
        launched["n"] += 1
        instance.detach_marker.touch()

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    inst = op.Instance("detachfirst")
    inst.detach_marker.touch()

    assert op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True) == 0
    assert launched["n"] == 0
    assert not inst.detach_marker.exists()


def test_a_normal_start_still_launches(monkeypatch):
    """Control for the two above. A pre-launch check that refused
    unconditionally would satisfy both of them and break the product; this is
    the assertion that says the marker is what did it."""
    launched = {"n": 0}

    def start(instance, *a, **k):
        launched["n"] += 1
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", start)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    assert op.run_loop_mode(op.Instance("normalstart"),
                            ["--agent", "test:agent"], is_fresh=True) == 0
    assert launched["n"] == 1


# ── operator restart-loop and the other spawners ────────────────

def test_restart_loop_does_not_start_a_second_supervisor(monkeypatch):
    """Consequence 2 of the item: two supervisors watching one session, which
    the code itself describes as relaunching over each other indefinitely."""
    inst = op.Instance("doubled")
    inst.claim("test-token")
    op._save_loop_args(inst, ["--agent", "anvil:anvil"])
    op._record_supervisor_starting(inst, 5150)
    _dead_pids(monkeypatch, 5150)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    _fast_clock(monkeypatch)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "a supervisor is already starting for this instance"))

    assert op.restart_loop("doubled") == 1


def test_start_and_attach_does_not_start_a_second_supervisor(monkeypatch):
    inst = op.Instance("attachdouble")
    op._record_supervisor_starting(inst, 5151)
    _dead_pids(monkeypatch, 5151)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "a supervisor is already starting for this instance"))
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    attached: list[str] = []
    monkeypatch.setattr(op.MUX, "attach", lambda session: attached.append(session))

    assert op.start_and_attach_loop(inst, [], is_fresh=False) == 0
    assert attached == [inst.session], \
        "it must attach to the starting supervisor's session, not spawn a rival"


def test_start_headless_does_not_start_a_second_supervisor(monkeypatch):
    inst = op.Instance("headlessdouble")
    op._record_supervisor_starting(inst, 5152)
    _dead_pids(monkeypatch, 5152)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: pytest.fail(
        "a supervisor is already starting for this instance"))

    assert op.start_loop_headless(inst, [], is_fresh=False) == 0


def test_restart_loop_still_starts_one_when_nothing_is_there(monkeypatch):
    """Control for the three refusals above: the startup record must be what
    stops them, not a blanket refusal to ever spawn."""
    inst = op.Instance("genuinelyempty")
    inst.claim("test-token")
    op._save_loop_args(inst, ["--agent", "anvil:anvil"])
    spawned = {"n": 0}

    def spawn(instance, copilot_args, is_fresh, adopt=False, cwd=None):
        spawned["n"] += 1
        instance.loop_pid_file.write_text("6060", encoding="utf-8")
        return 6060

    monkeypatch.setattr(op, "_spawn_background_loop", spawn)
    _dead_pids(monkeypatch, 6060)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    _fast_clock(monkeypatch)

    assert op.restart_loop("genuinelyempty") == 0
    assert spawned["n"] == 1


# ── what must NOT change ────────────────────────────────────────

def test_restart_loop_confirmation_is_not_satisfied_by_its_own_spawn(monkeypatch):
    """The one place the wider reader would be actively wrong.

    `_do_restart_loop` waits at the end to confirm the replacement came up.
    That record is written by the spawn itself, so confirming against
    `_supervisor_present` would report success for a supervisor that had not
    executed a single instruction -- turning the check that exists to catch a
    supervisor dying on startup into one that can never fail.
    """
    inst = op.Instance("vacuity")
    inst.claim("test-token")
    op._save_loop_args(inst, ["--agent", "anvil:anvil"])

    def spawn(instance, copilot_args, is_fresh, adopt=False, cwd=None):
        # Exactly what the real one does, and nothing more: the child never
        # gets far enough to publish a pid.
        op._record_supervisor_starting(instance, 7070)
        return 7070

    monkeypatch.setattr(op, "_spawn_background_loop", spawn)
    _dead_pids(monkeypatch, 7070)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    _fast_clock(monkeypatch)

    assert op.restart_loop("vacuity") == 1, \
        "a supervisor that never published a pid was reported as up"


def test_adoption_does_not_refuse_to_start_itself(monkeypatch):
    """A supervisor must not read the record of its own startup as a rival.

    This is a regression test for adoption surviving the new record, not a
    control for which reader the guard uses -- the test below is that, and it
    exists because this one is satisfied by both readers.
    """
    inst = op.Instance("selfrefuse")
    inst.claim("test-token")
    inst.session_file.write_text(
        "11111111-2222-3333-4444-555555555555", encoding="utf-8")
    # The parent's record, naming a shim pid that is not ours and is dead.
    op._record_supervisor_starting(inst, 9091)
    _dead_pids(monkeypatch, os.getpid())
    monkeypatch.setattr(op, "start_session", lambda *a, **k: pytest.fail(
        "adoption must not launch"))
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)
    inst.detach_marker.touch()

    assert op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                            adopt=True) == 0


def test_the_startup_record_names_this_process_before_adoption_checks(monkeypatch):
    """What makes the narrow reader correct in the adoption guard.

    The guard could read either signal today and get the same answer, because
    this process overwrites the record with its own pid as its first act --
    so by the time the guard runs, the record names *us* and a check against
    it can never fire. That equivalence is a fact about the ordering, not
    about the guard, and it is the ordering that has to hold: move the claim
    to after the guard and a supervisor reading the wider signal would find
    the record its own parent wrote for it -- a launcher shim's pid on
    Windows -- and refuse to start itself, on one platform only.
    """
    seen: dict = {}
    inst = op.Instance("guardorder")
    inst.claim("test-token")
    inst.session_file.write_text(
        "11111111-2222-3333-4444-555555555555", encoding="utf-8")
    op._record_supervisor_starting(inst, 9091)  # the parent's record
    _dead_pids(monkeypatch, os.getpid())

    real_running = op._running_loop_pid

    def spy(instance):
        seen.setdefault("record_says", op._starting_loop_pid(instance))
        return real_running(instance)

    monkeypatch.setattr(op, "_running_loop_pid", spy)
    monkeypatch.setattr(op, "start_session", lambda *a, **k: pytest.fail(
        "adoption must not launch"))
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)
    inst.detach_marker.touch()

    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                     adopt=True)

    assert seen.get("record_says") == os.getpid(), (
        "the adoption guard ran while the startup record still named "
        f"{seen.get('record_says')}, not this process")


def test_adoption_still_refuses_a_second_published_supervisor(monkeypatch):
    """Control for the test above: the last line of defence still fires when
    the other supervisor has actually published a pid."""
    inst = op.Instance("realsecond")
    inst.claim("test-token")
    inst.loop_pid_file.write_text("9092", encoding="utf-8")
    _dead_pids(monkeypatch, 9092, os.getpid())
    monkeypatch.setattr(op, "start_session", lambda *a, **k: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda session: False)

    with pytest.raises(SystemExit):
        op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                         adopt=True)


def test_cleanup_removes_the_startup_record():
    """`cleanup_files` is what runs when an instance is torn down; a record
    it did not know about would outlive the instance."""
    inst = op.Instance("teardown")
    op._record_supervisor_starting(inst, 1)
    assert inst.loop_startup_file.exists()

    inst.cleanup_files()

    assert not inst.loop_startup_file.exists()
