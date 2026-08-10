"""The liveness cascade, and the "I cannot tell" that keeps it honest.

Two properties are load-bearing and everything here exists to pin one of them:

* **Nothing but the three conclusive probes may return DEAD.** An old
  heartbeat is STALE -- reported to a person, never acted on -- because the
  alternative is guessing, and a wrong guess puts a second agent into a tree
  the first is still writing to.
* **A probe that could not look must say so.** ``False`` means *absent*;
  ``None`` means *unknown*. Collapsing the two is the defect that turns a
  permissions failure or a mid-shutdown multiplexer into a death certificate.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operator_liveness as ol  # noqa: E402
import work_claims as wc  # noqa: E402
from operator_mux import Mux, MuxNotFoundError  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


class FakeProbes:
    """Every probe answers whatever the test says, including ``None``."""

    def __init__(self, boot=None, session=None, running=None, start=None):
        self._boot = boot
        self._session = session
        self._running = running
        self._start = start
        self.asked: list = []

    def boot_identity(self):
        self.asked.append("boot")
        return self._boot

    def session_present(self, session):
        self.asked.append("mux")
        return self._session

    def process_present(self, pid):
        self.asked.append("pid")
        return self._running

    def process_start_token(self, pid):
        self.asked.append("start")
        return self._start


def make_claim(*, age_seconds=0, boot_id="uuid:same", mux_session="alpha",
               pid=4242, pid_start="linux:99"):
    stamp = (NOW - timedelta(seconds=age_seconds)).strftime(wc.TS_FORMAT)
    return wc.Claim(item="0007", instance="alpha", boot_id=boot_id,
                    mux_session=mux_session, pid=pid, pid_start=pid_start,
                    claimed_at=stamp, heartbeat_at=stamp)


# ── the cascade: the three conclusive signals ───────────────────
def test_a_different_boot_id_is_dead_with_no_timeout() -> None:
    """The unplanned-reboot case. Nothing from the previous boot is running,
    so there is nothing to wait for -- even a heartbeat from one second ago."""
    verdict = ol.assess(make_claim(age_seconds=1),
                        probes=FakeProbes(boot="uuid:other"), now=NOW)
    assert verdict.verdict == ol.DEAD
    assert "reboot" in verdict.reason
    assert verdict.reclaimable is True


def test_an_absent_mux_session_is_dead() -> None:
    verdict = ol.assess(make_claim(age_seconds=1),
                        probes=FakeProbes(boot="uuid:same", session=False),
                        now=NOW)
    assert verdict.verdict == ol.DEAD
    assert "mux session" in verdict.reason


def test_an_absent_pid_is_dead() -> None:
    verdict = ol.assess(
        make_claim(age_seconds=1),
        probes=FakeProbes(boot="uuid:same", session=True, running=False),
        now=NOW)
    assert verdict.verdict == ol.DEAD
    assert "not running" in verdict.reason


def test_a_reused_pid_is_dead_even_though_the_pid_is_running() -> None:
    """The start-time comparison is the whole reason the pid probe is safe.
    Without it a recycled pid reads as its dead predecessor still working."""
    verdict = ol.assess(
        make_claim(age_seconds=1),
        probes=FakeProbes(boot="uuid:same", session=True, running=True,
                          start="linux:100000"),
        now=NOW)
    assert verdict.verdict == ol.DEAD
    assert "reused" in verdict.reason


def test_a_matching_start_time_is_not_death() -> None:
    verdict = ol.assess(
        make_claim(age_seconds=1),
        probes=FakeProbes(boot="uuid:same", session=True, running=True,
                          start="linux:99"),
        now=NOW)
    assert verdict.verdict == ol.LIVE


# ── the cascade: the fourth signal is never conclusive ───────────
@pytest.mark.parametrize("probes", [
    FakeProbes(),                                    # nothing could be read
    FakeProbes(boot=None, session=None, running=None),
    FakeProbes(boot="uuid:same", session=None, running=None),
    FakeProbes(boot="uuid:same", session=True, running=True, start=None),
    FakeProbes(boot="uuid:same", session=True, running=True, start="linux:99"),
])
def test_an_old_heartbeat_alone_is_stale_and_never_reclaimable(probes) -> None:
    """Spec FR-3 step 4: report, never auto-steal. A hung process and a clock
    that moved look identical from here, and one of them is still writing."""
    verdict = ol.assess(make_claim(age_seconds=4000), probes=probes, now=NOW)
    assert verdict.verdict == ol.STALE
    assert verdict.reclaimable is False


def test_nothing_readable_and_a_fresh_heartbeat_is_live() -> None:
    """"Could not tell" is not evidence of death, at any step."""
    verdict = ol.assess(make_claim(age_seconds=5), probes=FakeProbes(), now=NOW)
    assert verdict.verdict == ol.LIVE


def test_an_unreadable_heartbeat_is_stale_not_live_and_not_dead() -> None:
    claim = wc.Claim(item="0007", instance="alpha", boot_id="uuid:same",
                     mux_session=None, pid=None, heartbeat_at="not a time")
    verdict = ol.assess(claim, probes=FakeProbes(boot="uuid:same"), now=NOW)
    assert verdict.verdict == ol.STALE
    assert verdict.reclaimable is False


def test_the_stale_boundary_is_the_configured_limit() -> None:
    fresh = ol.assess(make_claim(age_seconds=100), probes=FakeProbes(),
                      now=NOW, stale_after=100)
    stale = ol.assess(make_claim(age_seconds=101), probes=FakeProbes(),
                      now=NOW, stale_after=100)
    assert (fresh.verdict, stale.verdict) == (ol.LIVE, ol.STALE)


def test_the_default_limit_is_thirty_minutes() -> None:
    """Spec D4."""
    assert ol.DEFAULT_STALE_AFTER == 30 * 60


def test_a_heartbeat_from_the_future_is_not_stale() -> None:
    verdict = ol.assess(make_claim(age_seconds=-600), probes=FakeProbes(),
                        now=NOW)
    assert verdict.verdict == ol.LIVE


# ── ordering: cheapest first, and short-circuiting ──────────────
def test_the_boot_probe_short_circuits_the_expensive_ones() -> None:
    probes = FakeProbes(boot="uuid:other", session=True, running=True)
    ol.assess(make_claim(), probes=probes, now=NOW)
    assert probes.asked == ["boot"], (
        "a conclusive reboot must not cost a multiplexer call")


def test_the_pid_probe_short_circuits_the_mux_probe() -> None:
    """A syscall is asked before a subprocess spawn. Both can only conclude
    DEAD, so the order cannot change a verdict -- only what establishing a
    dead owner costs, on every claim of every sweep."""
    probes = FakeProbes(boot="uuid:same", session=True, running=False)
    ol.assess(make_claim(), probes=probes, now=NOW)
    assert probes.asked == ["boot", "pid"]


def test_the_mux_probe_is_still_asked_when_the_pid_is_inconclusive() -> None:
    """Cheapest first is an ordering, not a filter: a pid that cannot be
    judged must not swallow the question the mux session can answer."""
    probes = FakeProbes(boot="uuid:same", session=False, running=None)
    verdict = ol.assess(make_claim(), probes=probes, now=NOW)
    assert probes.asked == ["boot", "pid", "mux"]
    assert verdict.verdict == ol.DEAD


def test_the_start_token_is_only_asked_for_when_the_pid_is_there() -> None:
    probes = FakeProbes(boot="uuid:same", session=True, running=None)
    ol.assess(make_claim(), probes=probes, now=NOW)
    assert "start" not in probes.asked


def test_a_claim_with_no_recorded_session_or_pid_skips_those_probes() -> None:
    probes = FakeProbes(boot="uuid:same")
    verdict = ol.assess(make_claim(mux_session=None, pid=None), probes=probes,
                        now=NOW)
    assert probes.asked == ["boot"]
    assert verdict.verdict == ol.LIVE


def test_every_signal_is_reported_not_just_the_deciding_one() -> None:
    """A STALE claim is only actionable by a person if it says what could not
    be established."""
    verdict = ol.assess(make_claim(age_seconds=4000),
                        probes=FakeProbes(boot="uuid:same", session=True,
                                          running=True, start="linux:99"),
                        now=NOW)
    assert verdict.signals["boot"] is True
    assert verdict.signals["mux"] is True
    assert verdict.signals["pid"] is True
    assert verdict.signals["start"] == "linux:99"
    assert verdict.signals["heartbeat_age"] == pytest.approx(4000)


def test_assess_never_writes_to_the_store(tmp_path: Path) -> None:
    """Read-only, always: a verdict is a statement about the world, not an
    instruction that anything has been taken."""
    path = wc.db_path(tmp_path)
    wc.init_db(path)
    wc.claim(path, item="0007", instance="alpha", boot_id="uuid:same",
             pid=4242, now=(NOW - timedelta(hours=4)).strftime(wc.TS_FORMAT))
    before = wc.claim_for_item(path, "0007")
    digest = path.read_bytes()
    verdict = ol.assess(before, probes=FakeProbes(boot="uuid:other"), now=NOW)
    assert verdict.verdict == ol.DEAD
    assert wc.claim_for_item(path, "0007") == before
    assert path.read_bytes() == digest


# ── boot identity ───────────────────────────────────────────────
@pytest.mark.parametrize("recorded,current,expected", [
    ("uuid:abc", "uuid:abc", True),
    ("uuid:abc", "uuid:def", False),
    ("instant:1000", "instant:1000", True),
    ("instant:1000", "instant:1000000", False),
    # Kinds are never compared across: a claim written where an exact source
    # was available and read where it was not must be unknown, not different.
    ("uuid:abc", "instant:1000", None),
    ("instant:1000", "uuid:abc", None),
    (None, "uuid:abc", None),
    ("uuid:abc", None, None),
    ("", "uuid:abc", None),
    ("uuid:", "uuid:abc", None),
    ("uuid:abc", "uuid:", None),
    ("nonsense", "uuid:abc", None),
    ("instant:abc", "instant:1000", None),
])
def test_same_boot_matrix(recorded, current, expected) -> None:
    assert ol.same_boot(recorded, current) is expected


def test_computed_boot_instants_tolerate_a_nudged_clock() -> None:
    """Windows and macOS report an instant, and an instant moves -- the kernel
    adjusts it when the wall clock is corrected. Erring wide costs a slower
    answer; erring narrow reports a live agent as dead."""
    base = 1_700_000_000
    within = ol.BOOT_INSTANT_TOLERANCE - 1
    beyond = ol.BOOT_INSTANT_TOLERANCE + 1
    assert ol.same_boot(f"instant:{base}", f"instant:{base + within}") is True
    assert ol.same_boot(f"instant:{base}", f"instant:{base - within}") is True
    assert ol.same_boot(f"instant:{base}", f"instant:{base + beyond}") is False


def test_the_tolerance_does_not_swallow_a_real_reboot() -> None:
    assert ol.BOOT_INSTANT_TOLERANCE < 600


def test_this_machine_reports_a_tagged_boot_identity() -> None:
    token = ol.boot_identity()
    assert token is not None, "no boot identity could be read on this platform"
    assert token.split(":", 1)[0] in ("uuid", "instant")
    assert token.split(":", 1)[1]


def test_this_machines_boot_identity_is_stable_across_calls() -> None:
    """A boot id that changes between two reads makes every claim look like it
    came from a previous boot -- the one false DEAD that costs a worktree."""
    first = ol.boot_identity()
    time.sleep(0.05)
    assert ol.same_boot(first, ol.boot_identity()) is True


# ── process probes, against this machine ────────────────────────
def test_our_own_process_is_present() -> None:
    assert ol.process_present(os.getpid()) is True


def test_our_own_start_token_is_tagged_and_stable() -> None:
    first = ol.process_start_token(os.getpid())
    assert first is not None and ":" in first
    assert ol.process_start_token(os.getpid()) == first


def test_a_process_that_exited_is_absent_and_has_no_token() -> None:
    """The pid probe has to answer False -- not None -- for a process that
    really is gone, or nothing would ever be conclusively DEAD."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    deadline = time.time() + 10
    while time.time() < deadline and ol.process_present(proc.pid) is not False:
        time.sleep(0.1)
    assert ol.process_present(proc.pid) is False
    assert ol.process_start_token(proc.pid) is None


def test_a_child_that_is_still_running_reports_a_different_token(
        tmp_path: Path) -> None:
    """Two different processes must not share a start token, or pid reuse
    would be undetectable."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(30)"])
    try:
        deadline = time.time() + 10
        token = None
        while time.time() < deadline and token is None:
            token = ol.process_start_token(proc.pid)
            if token is None:
                time.sleep(0.1)
        assert ol.process_present(proc.pid) is True
        assert token is not None
        assert token != ol.process_start_token(os.getpid())
    finally:
        proc.kill()
        proc.wait(timeout=60)


@pytest.mark.parametrize("pid", [None, 0, -1, "", "abc", 1.5, True, "-1",
                                 " 12 3", object()])
def test_a_malformed_pid_is_unknown_rather_than_absent(pid) -> None:
    """``os.kill(0, 0)`` probes our own process *group*, which would answer
    True about something nobody asked; a negative pid is a group too. Neither
    is a process, and the answer to a malformed question is "cannot tell"."""
    assert ol.process_present(pid) is None
    assert ol.process_start_token(pid) is None


def test_the_ps_probe_pins_its_rendering(monkeypatch) -> None:
    """``ps -o lstart=`` prints a wall-clock date through ``LC_TIME`` and
    ``TZ``, so an inherited environment makes the token a property of the
    *caller*. `copilot_operator._loop_pid_reused` spends a token mismatch on
    deleting a supervisor's pid file and `assess` spends one on declaring a
    claim's owner DEAD, so two shells with different settings could disown
    each other's live process. Found by adversarial review."""
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "Sat Aug  9 17:25:00 2026"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(ol.subprocess, "run", fake_run)

    # `psc`, not `ps`: the pin changes the string for a process that has not
    # moved, so the tag is what stops a pre-pin record comparing unequal to
    # its own live owner. See `same_start_token`.
    assert ol._ps_start_token(4242) == "psc:Sat Aug  9 17:25:00 2026"
    assert seen["cmd"][:2] == ["ps", "-p"]
    assert seen["env"] is not None, "an inherited environment carries a locale"
    assert seen["env"]["LC_ALL"] == "C"
    assert seen["env"]["LC_TIME"] == "C"
    assert seen["env"]["TZ"] == "UTC"
    # The rest of the environment still has to reach `ps`, or the probe stops
    # finding it on a machine whose PATH is not the default.
    assert "PATH" in seen["env"] or "PATH" not in os.environ


def test_a_float_pid_is_refused_rather_than_truncated() -> None:
    """``int(1.5)`` is ``1``, so truncating does not refuse the question -- it
    answers a different one, about a real process, and sounds certain."""
    assert ol.process_present(1.5) is None
    assert ol.process_present(float(os.getpid())) is None
    assert ol.process_present(os.getpid()) is True


def test_a_numeric_string_pid_is_accepted() -> None:
    assert ol.process_present(str(os.getpid())) is True
    assert ol.process_start_token(str(os.getpid())) == \
        ol.process_start_token(os.getpid())


def test_an_absurd_pid_is_absent() -> None:
    """In range, so it is a question the OS can answer -- and it answers no."""
    assert ol.process_present(0x7FFFFFFE) is False


@pytest.mark.parametrize("pid", [
    1 << 32,
    (1 << 32) + 4,
    1 << 64,
    str((1 << 32) + 4),
    10 ** 30,
])
def test_a_pid_too_wide_for_the_platform_is_refused(pid) -> None:
    """A pid is 32 bits on both platforms and ``ctypes`` truncates rather than
    complains: ``OpenProcess`` handed ``(1 << 32) + os.getpid()`` opens *this*
    process and reports it running. Truncating is the float-pid bug at a
    different width -- a confident answer about a different process."""
    assert ol._coerce_pid(pid) is None
    assert ol.process_present(pid) is None
    assert ol.process_start_token(pid) is None


def test_the_widest_representable_pid_is_still_a_question() -> None:
    """The refusal is a range check, not a cap that swallows real pids."""
    assert ol._coerce_pid(ol._PID_MAX) == ol._PID_MAX
    assert ol._coerce_pid(ol._PID_MAX + 1) is None


def test_our_own_pid_is_not_reachable_by_wrapping() -> None:
    """The concrete alias the range check exists to stop, on this machine."""
    mine = os.getpid()
    assert ol.process_present(mine) is True
    assert ol.process_present((1 << 32) + mine) is None
    assert ol.process_start_token((1 << 32) + mine) is None


def test_the_real_probes_judge_our_own_process_live() -> None:
    """End to end against this machine, with no fakes: the runtime identity
    this module records is the one it can read back."""
    claim = wc.Claim(
        item="0007", instance="alpha", boot_id=ol.boot_identity(),
        mux_session=None, pid=os.getpid(),
        pid_start=ol.process_start_token(os.getpid()),
        claimed_at=wc.utcnow(), heartbeat_at=wc.utcnow())
    assert ol.assess(claim, probes=ol.SystemProbes()).verdict == ol.LIVE


# ── the Windows open-failure branches ───────────────────────────
@pytest.mark.parametrize("err,expected", [
    (5, True),        # ERROR_ACCESS_DENIED: refused, therefore it is there
    (87, False),      # ERROR_INVALID_PARAMETER: no such pid
    (6, None),        # ERROR_INVALID_HANDLE: no idea, and it must say so
    (0, None),
])
def test_a_failed_open_is_read_from_the_error_code(err, expected) -> None:
    """An access-denied process cannot be conjured on demand, so the branch
    that reads "refused" as "present" is reached with a scripted opener. Read
    the other way round it reports a running agent dead, which is the one
    error this cascade is built to avoid."""
    calls = []

    def opener(pid):
        calls.append(pid)
        return (object(), 0)

    assert ol._win_process_present(
        4321, opener=opener, last_error=lambda: err) is expected
    assert calls == [4321]


def test_an_open_that_raises_is_unknown() -> None:
    def opener(pid):
        raise OSError("kernel32 is not here")

    assert ol._win_process_present(
        4321, opener=opener, last_error=lambda: 5) is None


# ── the multiplexer probe is tri-state ──────────────────────────
class FakeRun:
    """A ``Mux`` whose one shell-out is scripted."""

    def __init__(self, out="", err="", rc=0, raises=None):
        self.result = (out, err, rc)
        self.raises = raises
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.mark.parametrize("out,rc,expected", [
    ("alpha\nbeta\n", 0, True),
    ("beta\ngamma\n", 0, False),
    ("", 0, False),
    ("  alpha  \n", 0, True),
])
def test_a_listed_session_is_present_and_an_unlisted_one_is_not(
        monkeypatch, out, rc, expected) -> None:
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run", FakeRun(out=out, rc=rc))
    assert mux.session_present("alpha") is expected


def test_no_server_running_is_conclusively_absent(monkeypatch) -> None:
    """No server means no sessions. This is the one failure signature that
    answers the question rather than failing to reach something that could."""
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run",
                        FakeRun(err="no server running on /tmp/tmux-1000/default",
                                rc=1))
    assert mux.session_present("alpha") is False


@pytest.mark.parametrize("err", [
    "server exited unexpectedly",
    "lost server",
    "error connecting to /tmp/tmux-1000/default (Permission denied)",
    "",
    "something nobody has seen before",
])
def test_a_call_that_never_reached_a_server_is_unknown(monkeypatch, err) -> None:
    """These say the question was not answered. Reading them as "absent" is
    what hands a live agent's worktree to somebody else."""
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run", FakeRun(err=err, rc=1))
    assert mux.session_present("alpha") is None


def test_a_missing_multiplexer_is_unknown_not_absent(monkeypatch) -> None:
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run",
                        FakeRun(raises=MuxNotFoundError("install tmux")))
    assert mux.session_present("alpha") is None


def test_an_os_error_from_the_multiplexer_is_unknown(monkeypatch) -> None:
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run", FakeRun(raises=OSError("boom")))
    assert mux.session_present("alpha") is None


def test_has_session_still_answers_two_valued(monkeypatch) -> None:
    """The tri-state probe is an addition, not a replacement: the create path
    is right to treat every failure as "not there" and try again."""
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run", FakeRun(rc=1))
    assert mux.has_session("alpha") is False


def test_system_probes_route_the_session_question_to_the_mux(monkeypatch) -> None:
    mux = Mux(binary="tmux")
    monkeypatch.setattr(mux, "_run", FakeRun(out="alpha\n", rc=0))
    probes = ol.SystemProbes(mux=mux)
    assert probes.session_present("alpha") is True
    assert probes.session_present("beta") is False


# ── the verdict object ──────────────────────────────────────────
def test_only_dead_is_reclaimable() -> None:
    assert ol.Liveness(ol.DEAD, "", {}).reclaimable is True
    assert ol.Liveness(ol.STALE, "", {}).reclaimable is False
    assert ol.Liveness(ol.LIVE, "", {}).reclaimable is False


def test_the_three_verdicts_are_distinct() -> None:
    assert len({ol.LIVE, ol.DEAD, ol.STALE}) == 3


def test_heartbeat_age_reads_a_naive_now_as_utc() -> None:
    claim = make_claim(age_seconds=60)
    assert ol.heartbeat_age(claim, now=NOW.replace(tzinfo=None)) == \
        pytest.approx(60)
