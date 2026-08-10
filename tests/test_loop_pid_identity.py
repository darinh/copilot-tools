"""A pid is not an identity, and `looping` must not be a recycled pid.

Backlog 0029. `_running_loop_pid` decided a supervisor was alive by asking
whether *some* process held the pid in `{instance}.loop.pid`. Windows recycles
pids aggressively, so once a supervisor died, any unrelated process later
handed its pid made the file read as live -- and `operator list` printed that
instance as `looping`, with a session number and an age, byte-identical to a
healthy row. That is backlog 0001's failure shape: the instrument reports the
machine as fine at exactly the moment it is not.

It also gated everything downstream. `_instance_summary` and `list_instances`
only say anything about staleness, an unrecorded record, a mismatched one or a
supervisor restart when `snap["loop_pid"]` is truthy, so a false positive kept
four notices switched on for a supervisor that could not be described, and a
false negative would switch all four off at once -- which is why every test
below that pins a *fallback* matters as much as the ones that pin a refusal.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

import copilot_operator as op
import operator_liveness as ol

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "_RUNNING_CODE", None)
    return tmp_path


def _write(instance: op.Instance, *lines: str) -> None:
    instance.loop_pid_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _token_probe(monkeypatch, value):
    """Patch the live start-token probe and count its calls.

    The count is half the assertion everywhere it appears. Every fallback in
    `_loop_pid_reused` short-circuits *before* probing, so in those tests a
    patched probe that is never reached looks exactly like one that ran and
    agreed -- and the test would pass against an implementation that ignored
    the stamp entirely.
    """
    calls = {"n": 0}

    def probe(pid):
        calls["n"] += 1
        return value

    monkeypatch.setattr(op.operator_liveness, "process_start_token", probe)
    return calls


def _boot_probe(monkeypatch, value):
    """Patch this machine's boot identity and count the calls."""
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return value

    monkeypatch.setattr(op.operator_liveness, "boot_identity", probe)
    return calls


def _alive(monkeypatch, *pids: int) -> None:
    monkeypatch.setattr(op, "_pid_alive", lambda pid: pid in pids)


# ── the format ──────────────────────────────────────────────────

def test_the_first_line_is_the_bare_pid(monkeypatch):
    """Anything that only wants the pid keeps working, including the pid
    files every earlier version wrote, which are exactly this first line."""
    _token_probe(monkeypatch, "win:1234")
    _boot_probe(monkeypatch, "instant:900")

    text = op._loop_pid_stamp(4242)

    assert text.splitlines()[0] == "4242"
    assert int(text.splitlines()[0]) == 4242


def test_the_stamp_carries_the_writers_identity(monkeypatch):
    _token_probe(monkeypatch, "win:1234")
    _boot_probe(monkeypatch, "instant:900")

    lines = op._loop_pid_stamp(4242).splitlines()

    assert lines[1:] == ["pid_start=win:1234", "boot=instant:900"]


def test_an_unanswerable_probe_writes_the_pid_alone(monkeypatch):
    """`process_start_token` and `boot_identity` both return ``None`` where
    they cannot answer. The file then says only what is known, which is the
    pre-stamp shape and is read as such."""
    _token_probe(monkeypatch, None)
    _boot_probe(monkeypatch, None)

    assert op._loop_pid_stamp(4242) == "4242\n"


def test_a_token_containing_spaces_survives_the_round_trip(monkeypatch):
    """macOS and BSD keep ``ps -o lstart=`` verbatim, which is a date with
    spaces in it -- `ps:Sat Aug  9 17:25:00 2026`. A space-separated file
    format would truncate that and make a live supervisor compare unequal to
    itself, on the two platforms nobody here tests interactively."""
    token = "ps:Sat Aug  9 17:25:00 2026"
    inst = op.Instance("spacey")
    _token_probe(monkeypatch, token)
    _boot_probe(monkeypatch, None)
    inst.loop_pid_file.write_text(op._loop_pid_stamp(4242), encoding="utf-8")

    pid, stamps = op._read_loop_pid_stamp(inst)

    assert (pid, stamps) == (4242, {"pid_start": token})


def test_an_absent_file_is_no_supervisor():
    assert op._read_loop_pid_stamp(op.Instance("nobody")) is None


def test_a_stamp_value_is_kept_verbatim():
    """Not stripped. The tokens are already stripped where they are produced,
    and re-stripping here would silently rewrite any future token that ended
    in a space -- turning "the same process" into "a different one", which is
    the direction that costs a live supervisor."""
    inst = op.Instance("padded")
    _write(inst, "4242", "pid_start=win:1234 ")

    assert op._read_loop_pid_stamp(inst) == (4242, {"pid_start": "win:1234 "})


def test_a_file_naming_no_process_is_no_supervisor():
    """A first line that is not an integer names nothing, so there is no
    question to ask about it."""
    inst = op.Instance("garbled")
    _write(inst, "not-a-pid", "pid_start=win:1234")

    assert op._read_loop_pid_stamp(inst) is None
    assert op._running_loop_pid(inst) is None


def test_stamp_lines_without_a_key_are_ignored():
    inst = op.Instance("noisy")
    _write(inst, "4242", "a stray line", "=orphan", "pid_start=win:1234")

    assert op._read_loop_pid_stamp(inst) == (4242, {"pid_start": "win:1234"})


# ── the refusal ─────────────────────────────────────────────────

def test_a_recycled_pid_is_not_a_running_supervisor(monkeypatch):
    """The bug, in one assertion. The pid is held by a live process and that
    process is not the supervisor that wrote the file."""
    inst = op.Instance("recycled")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "started-at-9am")

    assert op._running_loop_pid(inst) is None
    assert calls["n"] == 1, "the refusal is only reachable by probing"


def test_a_refuted_pid_file_is_pruned(monkeypatch):
    """Left in place it keeps answering, and the process holding that pid may
    outlive anybody's patience. Pruned for the same reason the dead-pid
    branch prunes."""
    inst = op.Instance("pruned")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "started-at-9am")

    op._running_loop_pid(inst)

    assert not inst.loop_pid_file.exists()


def test_a_matching_token_is_the_running_supervisor(monkeypatch):
    """Positive control for the refusal above: same file, same pid, and the
    only thing allowed to decide it agrees."""
    inst = op.Instance("genuine")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "started-at-8am")

    assert op._running_loop_pid(inst) == 4242
    assert inst.loop_pid_file.exists()
    assert calls["n"] == 1


def test_a_recycled_pid_stops_being_a_looping_instance(monkeypatch):
    """What the fix is *for*. `active_instances` and every supervisor notice
    in the listing are gated on this predicate, so a refusal has to reach
    them rather than stopping at the function under test."""
    inst = op.Instance("listed")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "started-at-9am")
    monkeypatch.setattr(op, "managed_instances",
                        lambda: {inst.id: {"display_name": "listed"}})
    monkeypatch.setattr(op.MUX, "available", lambda: False)

    assert op.active_instances() == []


def test_a_live_supervisor_stays_a_looping_instance(monkeypatch):
    """Negative control for the case above: the same wiring, with the token
    agreeing, must still list the instance."""
    inst = op.Instance("listed")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "started-at-8am")
    monkeypatch.setattr(op, "managed_instances",
                        lambda: {inst.id: {"display_name": "listed"}})
    monkeypatch.setattr(op.MUX, "available", lambda: False)

    assert [i.id for i in op.active_instances()] == [inst.id]


# ── what must never be refused ──────────────────────────────────
#
# Every one of these leaves the answer exactly as it was before the stamp
# existed. Turning any of them into "stopped" would drop the instance from
# `active_instances`, silence all four supervisor notices at once, and let
# `restart-loop` start a second supervisor on top of a live one -- so the
# blindness this item is about is the cheaper of the two errors here.

def test_a_pid_file_predating_the_stamp_is_still_believed(monkeypatch):
    """Every supervisor running when this landed wrote a bare pid."""
    inst = op.Instance("legacy")
    inst.loop_pid_file.write_text("4242", encoding="utf-8")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "started-at-9am")

    assert op._running_loop_pid(inst) == 4242
    assert calls["n"] == 0, "no recorded token, so nothing to probe against"


def test_an_unreadable_live_token_leaves_the_pid_believed(monkeypatch):
    """`process_start_token` returns ``None`` for a pid it cannot inspect.
    That is an absence of evidence, and it must not be spent refuting a
    supervisor that is running."""
    inst = op.Instance("opaque")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, None)

    assert op._running_loop_pid(inst) == 4242
    assert inst.loop_pid_file.exists()
    assert calls["n"] == 1


@pytest.mark.parametrize("line", ["pid_start=", "pid_start"])
def test_a_damaged_stamp_leaves_the_pid_believed(monkeypatch, line):
    """Deliberately *not* how `_record_describes` treats damage in the code
    record. There a malformed field costs a staleness verdict and buys a
    printed caveat; here it would cost the session its supervisor."""
    inst = op.Instance("damaged")
    _write(inst, "4242", line)
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "started-at-9am")

    assert op._running_loop_pid(inst) == 4242
    assert calls["n"] == 0


def test_a_dead_pid_is_pruned_without_probing(monkeypatch):
    """Unchanged behaviour, and the ordering is worth pinning: a dead pid is
    already an answer, and probing a pid nothing holds would fork `ps` on
    macOS for a question that is settled."""
    inst = op.Instance("dead")
    _write(inst, "4242", "pid_start=started-at-8am")
    _alive(monkeypatch)
    calls = _token_probe(monkeypatch, "started-at-9am")

    assert op._running_loop_pid(inst) is None
    assert not inst.loop_pid_file.exists()
    assert calls["n"] == 0


# ── across a reboot ─────────────────────────────────────────────
#
# `operator_liveness._linux_start_token` is clock ticks *since boot*, so two
# processes from different boots can carry the same token. The boot identity
# is what refutes that, and it is consulted only where it can discriminate:
# `win:` and `ps:` tokens are absolute instants, and asking anyway costs a
# `sysctl` fork per call on macOS.

def test_a_boot_relative_token_from_another_boot_is_refuted(monkeypatch):
    inst = op.Instance("rebooted")
    _write(inst, "4242", "pid_start=linux:900", "boot=uuid:aaa")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "linux:900")
    calls = _boot_probe(monkeypatch, "uuid:bbb")

    assert op._running_loop_pid(inst) is None
    assert calls["n"] == 1


def test_a_boot_relative_token_from_this_boot_is_believed(monkeypatch):
    """Positive control: the same collision within one boot is the supervisor
    itself, and the ticks are then a real identity."""
    inst = op.Instance("sameboot")
    _write(inst, "4242", "pid_start=linux:900", "boot=uuid:aaa")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "linux:900")
    _boot_probe(monkeypatch, "uuid:aaa")

    assert op._running_loop_pid(inst) == 4242


def test_an_unknowable_boot_does_not_refute(monkeypatch):
    """`same_boot` returns ``None`` across kinds -- a record written on
    another platform, or a machine whose exact source stopped answering --
    and only ``False`` may refute."""
    inst = op.Instance("unknowable")
    _write(inst, "4242", "pid_start=linux:900", "boot=uuid:aaa")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "linux:900")
    _boot_probe(monkeypatch, "instant:900")

    assert op._running_loop_pid(inst) == 4242


def test_a_missing_boot_stamp_does_not_refute(monkeypatch):
    inst = op.Instance("bootless")
    _write(inst, "4242", "pid_start=linux:900")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "linux:900")
    calls = _boot_probe(monkeypatch, "uuid:bbb")

    assert op._running_loop_pid(inst) == 4242
    assert calls["n"] == 0


def test_an_absolute_token_never_asks_for_the_boot(monkeypatch):
    """A `win:` token is a FILETIME and a `ps:` token a wall-clock date;
    neither can collide across a reboot, so the probe would be a subprocess
    per call -- on `operator list`'s per-instance path and `restart-loop`'s
    twice-a-second poll -- for a question already answered."""
    inst = op.Instance("absolute")
    _write(inst, "4242", "pid_start=win:1234", "boot=uuid:aaa")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "win:1234")
    calls = _boot_probe(monkeypatch, "uuid:bbb")

    assert op._running_loop_pid(inst) == 4242
    assert calls["n"] == 0, "an absolute token settles it on its own"


# ── end to end, against this machine ────────────────────────────

def test_a_supervisor_that_just_published_reads_as_running():
    """No probes patched: publish with the real machine's own token and boot,
    then ask the question `operator list` asks. Anything but this pid means a
    healthy supervisor has just been declared stopped -- and every test above
    that patches a probe would be blind to a stamp that never agreed with the
    live process on any real platform."""
    inst = op.Instance("realsupervisor")
    op._publish_supervisor_records(inst, [])

    assert op._running_loop_pid(inst) == os.getpid()
    # And it stamped what this machine actually reports, rather than reaching
    # the assertion above by writing a bare pid and falling back.
    assert op._read_loop_pid_stamp(inst)[1].get("pid_start") == \
        ol.process_start_token(os.getpid())


def test_a_published_stamp_refuses_a_different_process(monkeypatch):
    """The other half of the end-to-end pair: the same real file, read while
    the pid belongs to something that started at a different moment."""
    inst = op.Instance("realrecycled")
    op._publish_supervisor_records(inst, [])
    real_token = op._read_loop_pid_stamp(inst)[1].get("pid_start")
    if not real_token:
        pytest.skip("this platform records no start token to compare")
    monkeypatch.setattr(op.operator_liveness, "process_start_token",
                        lambda pid: real_token + "-different")

    assert op._running_loop_pid(inst) is None


def test_the_e2e_harness_reads_a_stamped_pid_file(monkeypatch):
    """`e2e_restart_loop.read_pid` polls this file to decide the supervisor
    came up. It read the whole file as one integer, which a stamped file is
    not, so it would have reported every restart as a failure to start."""
    spec = importlib.util.spec_from_file_location(
        "e2e_restart_loop_for_pid_stamp", ROOT / "e2e_restart_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inst = op.Instance("harness")
    _token_probe(monkeypatch, "win:1234")
    _boot_probe(monkeypatch, "instant:900")
    inst.loop_pid_file.write_text(op._loop_pid_stamp(4242), encoding="utf-8")

    assert module.read_pid(inst.loop_pid_file) == 4242
    assert module.read_pid(inst.loop_pid_file.with_name("absent.pid")) is None


# ── the boot-relativity predicate ───────────────────────────────

@pytest.mark.parametrize("token,expected", [
    ("linux:12345", True),
    ("win:134308020110986193", False),
    ("ps:Sat Aug  9 17:25:00 2026", False),
    ("", False),
    (None, False),
    (17, False),
])
def test_only_the_linux_token_is_boot_relative(token, expected):
    assert ol.start_token_is_boot_relative(token) is expected


def test_this_machines_own_token_is_classified():
    """A control against the table above drifting from the producers: every
    shape `process_start_token` can return is one of the three, so whatever
    this machine produces must be answerable without an exception."""
    token = ol.process_start_token(os.getpid())

    assert isinstance(ol.start_token_is_boot_relative(token), bool)
