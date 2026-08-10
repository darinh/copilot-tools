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
import json
import os
from pathlib import Path

import pytest

import copilot_operator as op
import operator_liveness as ol

ROOT = Path(__file__).resolve().parent.parent


def _raise_oserror(*args, **kwargs):
    raise OSError("rename refused")


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


def test_a_file_that_is_not_utf8_is_no_supervisor():
    """`read_text` raises `UnicodeDecodeError` -- a `ValueError`, not an
    `OSError` -- for a file damaged into invalid UTF-8. Letting that escape
    would take `operator list`, `stop` and `restart-loop` down for every
    instance over one corrupt file belonging to one. Caught by adversarial
    review: splitting the old single `except (OSError, ValueError)` into a
    read and a parse dropped exactly this case."""
    inst = op.Instance("mojibake")
    inst.loop_pid_file.write_bytes(b"\xff\xfe4242\n")

    assert op._read_loop_pid_stamp(inst) is None
    assert op._running_loop_pid(inst) is None


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
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "win:9am")

    assert op._running_loop_pid(inst) is None
    assert calls["n"] == 1, "the refusal is only reachable by probing"


def test_a_refuted_pid_file_is_pruned(monkeypatch):
    """Left in place it keeps answering, and the process holding that pid may
    outlive anybody's patience. Pruned for the same reason the dead-pid
    branch prunes."""
    inst = op.Instance("pruned")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "win:9am")

    op._running_loop_pid(inst)

    assert not inst.loop_pid_file.exists()


def test_a_matching_token_is_the_running_supervisor(monkeypatch):
    """Positive control for the refusal above: same file, same pid, and the
    only thing allowed to decide it agrees."""
    inst = op.Instance("genuine")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "win:8am")

    assert op._running_loop_pid(inst) == 4242
    assert inst.loop_pid_file.exists()
    assert calls["n"] == 1


def test_a_recycled_pid_stops_being_a_looping_instance(monkeypatch):
    """What the fix is *for*. `active_instances` and every supervisor notice
    in the listing are gated on this predicate, so a refusal has to reach
    them rather than stopping at the function under test."""
    inst = op.Instance("listed")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "win:9am")
    monkeypatch.setattr(op, "managed_instances",
                        lambda: {inst.id: {"display_name": "listed"}})
    monkeypatch.setattr(op.MUX, "available", lambda: False)

    assert op.active_instances() == []


def test_a_live_supervisor_stays_a_looping_instance(monkeypatch):
    """Negative control for the case above: the same wiring, with the token
    agreeing, must still list the instance."""
    inst = op.Instance("listed")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "win:8am")
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
    calls = _token_probe(monkeypatch, "win:9am")

    assert op._running_loop_pid(inst) == 4242
    assert calls["n"] == 0, "no recorded token, so nothing to probe against"


def test_an_unreadable_live_token_leaves_the_pid_believed(monkeypatch):
    """`process_start_token` returns ``None`` for a pid it cannot inspect.
    That is an absence of evidence, and it must not be spent refuting a
    supervisor that is running."""
    inst = op.Instance("opaque")
    _write(inst, "4242", "pid_start=win:8am")
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
    calls = _token_probe(monkeypatch, "win:9am")

    assert op._running_loop_pid(inst) == 4242
    assert calls["n"] == 0


def test_a_dead_pid_is_pruned_without_probing(monkeypatch):
    """Unchanged behaviour, and the ordering is worth pinning: a dead pid is
    already an answer, and probing a pid nothing holds would fork `ps` on
    macOS for a question that is settled."""
    inst = op.Instance("dead")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch)
    calls = _token_probe(monkeypatch, "win:9am")

    assert op._running_loop_pid(inst) is None
    assert not inst.loop_pid_file.exists()
    assert calls["n"] == 0


@pytest.mark.parametrize("recorded", ["pid_start=garbage",
                                      "pid_start=win:",
                                      "pid_start=:1234",
                                      "pid_start=8am"])
def test_a_stamp_no_probe_could_have_written_is_not_evidence(monkeypatch, recorded):
    """A value outside `operator_liveness.START_TOKEN_KINDS` was written by no
    version of this code, so it is damage rather than a different process --
    and damage must not be spent deleting a live supervisor's pid file."""
    inst = op.Instance("nonsense")
    _write(inst, "4242", recorded)
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "win:9am")

    assert op._running_loop_pid(inst) == 4242
    assert inst.loop_pid_file.exists()


def test_a_live_token_of_an_unknown_shape_is_not_evidence(monkeypatch):
    """The other side of the same rule. If the probe starts returning
    something this module does not recognise, the honest reading is that it
    stopped answering -- not that every supervisor on the machine is a
    stranger."""
    inst = op.Instance("futuretoken")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    _token_probe(monkeypatch, "quantum-9am")

    assert op._running_loop_pid(inst) == 4242
    assert inst.loop_pid_file.exists()


# ── publishing, and the window a reader must not widen ──────────

def test_the_pid_file_is_published_by_rename(monkeypatch):
    """`write_text` truncates first, so a concurrent `operator list` can read
    a file that stops in the middle of the start token -- and a truncated
    token is a *well-formed* one that differs from the live process's, which
    is the one input that would delete a running supervisor's pid file. The
    rename makes a reader see the old file or the new one."""
    inst = op.Instance("atomic")
    written: list[str] = []
    real_write = op.Path.write_text

    def spy(self, *args, **kwargs):
        written.append(str(self))
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(op.Path, "write_text", spy)
    op._write_loop_pid_file(inst, 4242)

    assert str(inst.loop_pid_file) not in written, \
        "the live path must never be the one being written into"
    assert op._read_loop_pid_stamp(inst)[0] == 4242
    assert not list(op.RESTART_DIR.glob("*.tmp")), "the temporary is renamed away"


def test_publishing_falls_back_when_the_rename_fails(monkeypatch):
    """An unwritten pid file costs the session its `stop`, its `restart-loop`
    and its row in the listing. That is worse than the narrow window the
    rename closes, so the fallback is deliberate."""
    inst = op.Instance("fallback")
    monkeypatch.setattr(op.os, "replace", _raise_oserror)

    op._write_loop_pid_file(inst, 4242)

    assert op._read_loop_pid_stamp(inst)[0] == 4242


def test_a_stale_reader_does_not_delete_a_replacements_pid_file(monkeypatch):
    """Deciding costs a probe, and on macOS that probe can take ten seconds.
    A replacement supervisor can publish inside that window, and an
    unconditional unlink would then delete a *live* supervisor's file on the
    strength of a verdict about its predecessor. Found by adversarial
    review."""
    inst = op.Instance("replaced")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242, 5353)

    def probe_then_replace(pid):
        # The replacement lands while the OS is being asked about the old pid.
        _write(inst, "5353", "pid_start=win:10am")
        return "win:9am"

    monkeypatch.setattr(op.operator_liveness, "process_start_token",
                        probe_then_replace)

    assert op._running_loop_pid(inst) is None, \
        "the verdict about the pid it read is still the right one"
    assert inst.loop_pid_file.exists(), \
        "but it is not a verdict about the file that is there now"
    assert op._read_loop_pid_stamp(inst) == (5353, {"pid_start": "win:10am"})


def test_a_stale_reader_does_not_delete_a_replacement_after_a_dead_pid(monkeypatch):
    """The same guard on the other prune. `_pid_alive` is cheap, but it is
    not instantaneous, and the file it read may already have been replaced."""
    inst = op.Instance("deadreplaced")
    _write(inst, "4242", "pid_start=win:8am")

    def alive_then_replace(pid):
        _write(inst, "5353", "pid_start=win:10am")
        return False

    monkeypatch.setattr(op, "_pid_alive", alive_then_replace)

    assert op._running_loop_pid(inst) is None
    assert inst.loop_pid_file.exists()


# ── one probe per instance ──────────────────────────────────────

def test_the_listing_asks_who_holds_a_pid_once(monkeypatch):
    """`instance_snapshot` asks `_running_loop_identity` and then asks the
    record reader a related question about the same pid. Probing twice is one
    `ps` fork per instance too many on macOS, which is the cost complaint
    `loop_record_facts` already exists to answer."""
    inst = op.Instance("snapped")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "win:8am")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 4242, "pid_start": "win:8am", "files": []}),
        encoding="utf-8")
    monkeypatch.setattr(op.MUX, "available", lambda: False)

    snap = op.instance_snapshot(inst)

    assert snap["loop_pid"] == 4242
    assert snap["loop_code"] != op.CODE_MISMATCH, \
        "the record is the supervisor's own, so the handed-over token agreed"
    assert calls["n"] == 1


def test_an_unprobed_pid_still_gets_the_record_reader_to_look(monkeypatch):
    """The sentinel exists so "nobody looked" and "looked, and the OS would
    not say" stay apart. An unstamped pid file leaves the first, and the
    record reader must still probe -- passing ``None`` instead would silently
    retire the record's own pid-reuse check."""
    inst = op.Instance("unprobed")
    inst.loop_pid_file.write_text("4242\n", encoding="utf-8")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, "win:9am")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 4242, "pid_start": "win:8am", "files": []}),
        encoding="utf-8")
    monkeypatch.setattr(op.MUX, "available", lambda: False)

    snap = op.instance_snapshot(inst)

    assert snap["loop_pid"] == 4242
    assert snap["loop_code"] == op.CODE_MISMATCH
    assert calls["n"] == 1, "the record reader had to ask, because nobody had"


def test_a_probe_that_could_not_answer_is_not_asked_twice(monkeypatch):
    """``None`` is an answer -- "the OS would not say" -- and asking again
    costs a second fork for a result already known to be unavailable."""
    inst = op.Instance("silentos")
    _write(inst, "4242", "pid_start=win:8am")
    _alive(monkeypatch, 4242)
    calls = _token_probe(monkeypatch, None)
    inst.loop_code_file.write_text(
        json.dumps({"pid": 4242, "pid_start": "win:8am", "files": []}),
        encoding="utf-8")
    monkeypatch.setattr(op.MUX, "available", lambda: False)

    snap = op.instance_snapshot(inst)

    assert snap["loop_pid"] == 4242
    assert calls["n"] == 1


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
    the pid belongs to something that started at a different moment.

    The token is asserted rather than skipped on. Every platform this runs on
    has a `process_start_token` implementation -- ``win:``, ``linux:`` or
    ``ps:`` -- so ``None`` here does not mean "not applicable", it means the
    probe broke and the recycled-pid protection is off on that platform. A
    skip would retire the guarantee while staying green, which is how the
    silent all-clear gets back in.
    """
    inst = op.Instance("realrecycled")
    op._publish_supervisor_records(inst, [])
    real_token = op._read_loop_pid_stamp(inst)[1].get("pid_start")

    assert real_token, \
        "no start token on this platform: pid reuse is undetectable here"

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
    inst.loop_pid_file.write_bytes(b"\xff\xfe4242\n")
    assert module.read_pid(inst.loop_pid_file) is None, \
        "a file damaged into invalid UTF-8 must not abort the harness"


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
    """A control against the table above drifting from the producers.

    Asserting the *shape* rather than that the answer is a bool: the
    predicate returns a bool for every input including ``None``, so an
    isinstance check passes against a classifier that is simply wrong, and
    against a machine whose probe has stopped answering at all. Both of those
    switch the reboot half of `_loop_pid_reused` off silently.
    """
    token = ol.process_start_token(os.getpid())

    assert token, "no start token here: pid reuse is undetectable"
    kind = token.split(":", 1)[0]
    assert kind in {"win", "linux", "ps"}, f"unclassified token shape {token!r}"
    assert ol.start_token_is_boot_relative(token) is (kind == "linux")
