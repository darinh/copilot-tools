"""A supervisor's code is fixed at startup; the trace must be able to say so.

Backlog 0001's re-measurement instruction ("scope to records at or after
2026-08-05") was false the moment it was written. The fix it refers to landed
at 19:36 on 2026-08-04 and every supervisor on the machine had started at
13:28, so records dated after the fix were still produced by instruments
without it. Nothing in a record said which code wrote it, and nothing
anywhere said a running supervisor had fallen behind the disk.
"""
from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

import copilot_operator as op
import operator_trace


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    # The fingerprint is cached for the life of the process on purpose, so
    # every test that computes one has to start from a known-empty cache or
    # it would grade the previous test's answer.
    monkeypatch.setattr(op, "_RUNNING_CODE", None)
    return tmp_path


def _record(instance, entries):
    instance.loop_code_file.write_text(
        json.dumps({"version": "1.4.0", "digest": "x", "files": entries}),
        encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _detail_snap(**over):
    """A snapshot complete enough for `_print_instance_detail`, which reads
    more keys than the one-line summary does."""
    snap = {"name": "proj", "id": "op-proj", "loop_pid": 123,
            "session_live": True, "owned": True, "session_num": 3,
            "run_started": "", "loop_started": "", "loop_adopted": None, "loop_began_run": None,
            "cwd": "", "loop_code": op.CODE_CURRENT, "copilot_pid": None,
            "copilot_session_id": "", "argv": []}
    snap.update(over)
    return snap


def _unreadable(monkeypatch, target: Path):
    """Make reads of one path raise EACCES, as a revoked file does.

    `conftest.denied` patches `os.stat`/`os.lstat`, which a digest that opens
    the file never reaches -- so it would deny nothing here and the test would
    pass without exercising the branch it names.

    Both `builtins.open` and `io.open` are patched, and the second is not
    redundant. They are the *same function object* but two separate names, so
    rebinding one leaves the other pointing at the original -- and
    `Path.read_text` goes through `io.open`, not through the builtins global.
    Patching only builtins denied `_digest_file`, which calls `open()`
    directly, while silently failing to deny every reader that went through
    `pathlib`. `test_an_unreadable_record_is_unknown_not_unrecorded` was one
    of those: measured with a probe, its `read_text` succeeded, and the record
    it wrote (`{}`) has no `files` key, so `CODE_UNKNOWN` came back for a
    reason that had nothing to do with a denied read. It asserted the right
    answer and proved nothing, which is why that test now writes a record
    that would read as `CODE_CURRENT` if this helper ever stopped biting.
    """
    real_open = builtins.open

    def guard(file, *args, **kwargs):
        try:
            same = Path(file) == target
        except TypeError:
            same = False
        if same:
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.setattr(io, "open", guard)


class _RaisingRecord:
    """An instance whose code record raises a chosen error when *read*.

    `loop_code_state` touches nothing else on an instance, so this is the
    whole surface it needs -- and it lets the errno be chosen exactly, which
    building the situation on disk cannot do portably.

    The error comes out of `read_text`, not out of the attribute lookup, so
    an implementation that resolved the path outside its `try` would still
    fail these tests rather than pass them by accident.
    """

    class _Path:
        def __init__(self, error: BaseException):
            self._error = error

        def read_text(self, *args, **kwargs):
            raise self._error

    def __init__(self, error: BaseException):
        self.loop_code_file = self._Path(error)


def _raising(error: BaseException):
    return _RaisingRecord(error)


# ── the three answers _digest_file has to keep apart ─────────────
def test_digest_reads_a_file(tmp_path):
    """Checked against an independently computed digest, not against the
    implementation's own output -- an assertion that only says "a string came
    back" holds for any implementation, including a broken one."""
    path = tmp_path / "m.py"
    path.write_bytes(b"print(1)\n")
    assert op._digest_file(path) == hashlib.sha256(b"print(1)\n").hexdigest()


def test_digest_of_the_same_bytes_is_the_same(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_bytes(b"same\n")
    b.write_bytes(b"same\n")
    assert op._digest_file(a) == op._digest_file(b)


def test_digest_changes_with_the_bytes(tmp_path):
    path = tmp_path / "m.py"
    path.write_bytes(b"before\n")
    first = op._digest_file(path)
    path.write_bytes(b"after\n")
    assert op._digest_file(path) != first


def test_a_missing_file_is_absent_not_unknown(tmp_path):
    """Absence is a definite answer and must not be confused with a failure
    to look: a module the supervisor loaded that is gone *has* changed."""
    assert op._digest_file(tmp_path / "nope.py") is op.FILE_ABSENT


def test_an_unreadable_file_is_unknown_not_absent(tmp_path, monkeypatch):
    path = tmp_path / "m.py"
    path.write_bytes(b"x\n")
    _unreadable(monkeypatch, path)
    assert op._digest_file(path) is None


def test_absent_and_unreadable_do_not_compare_equal(tmp_path, monkeypatch):
    """The one collapse that would break the verdict: if the two answers were
    the same value, a file nobody could read would be reported as changed."""
    gone = tmp_path / "gone.py"
    denied = tmp_path / "denied.py"
    denied.write_bytes(b"x\n")
    _unreadable(monkeypatch, denied)
    assert op._digest_file(gone) is not op._digest_file(denied)


# ── the verdict ─────────────────────────────────────────────────
def test_no_record_at_all_is_unrecorded_not_unknown():
    """A supervisor that is running and has left no record started before the
    record existed, or could not write one. That is an observation, not a
    failure to look, and it must reach the operator -- collapsing it into
    "cannot tell" is what silenced this check for every supervisor on the
    machine. It still must not read as current or stale."""
    inst = op.Instance("norecord")
    assert op.loop_code_state(inst) == (op.CODE_UNRECORDED, [])


def test_unrecorded_is_not_current_and_not_stale():
    """The invariant the old `unknown` verdict protected, kept explicitly so
    a later widening of `unrecorded` cannot quietly take it away."""
    verdict = op.loop_code_state(op.Instance("norecord2"))[0]
    assert verdict not in (op.CODE_CURRENT, op.CODE_STALE)


def test_a_record_under_a_file_shaped_parent_is_unrecorded():
    """`NotADirectoryError`, not `FileNotFoundError`: nothing can exist below
    a path component that is a regular file, so this is as definite as
    absence and must answer the same way. Raised through a stub rather than
    built on disk because which errno Windows and POSIX produce for that
    shape differs, and a test that only holds on some legs is the failure
    mode this repository keeps paying for.
    """
    assert op.loop_code_state(_raising(NotADirectoryError(20, "Not a directory"))) \
        == (op.CODE_UNRECORDED, [])


def test_a_record_that_could_not_be_looked_at_is_unknown():
    """The counterpart control: the same call site, a different errno, the
    other answer. Without this the branch above would pass against an
    implementation that returned `unrecorded` for every OSError."""
    assert op.loop_code_state(_raising(PermissionError(13, "Denied")))[0] \
        == op.CODE_UNKNOWN


def test_an_unreadable_record_is_unknown_not_unrecorded(tmp_path, monkeypatch):
    """The distinction the whole change rests on, through the real file this
    time. A record behind a denied read exists; saying "unrecorded" about it
    would claim the supervisor never wrote one, which is a different and
    stronger statement.

    The record written here agrees with disk, so an implementation -- or a
    denial helper -- that let the read through would answer `CODE_CURRENT`
    and fail. Previously it wrote `{}`, which has no `files` key and so
    yielded `CODE_UNKNOWN` whether or not the read was ever denied.
    """
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = op.Instance("denied")
    _record(inst, [{"path": str(src), "sha256": _sha(src)}])
    assert op.loop_code_state(inst) == (op.CODE_CURRENT, [])

    _unreadable(monkeypatch, inst.loop_code_file)

    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


def test_a_record_that_is_not_utf8_is_unknown():
    """Bytes are there and could not be decoded -- `UnicodeDecodeError` is a
    `ValueError`, so this would fall through to the JSON branch if the read
    did not catch it, and the answer must still be "cannot tell"."""
    inst = op.Instance("notutf8")
    inst.loop_code_file.write_bytes(b"\xff\xfe\x00garbage")
    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


def test_an_unparseable_record_is_unknown():
    inst = op.Instance("garbage")
    inst.loop_code_file.write_text("{not json", encoding="utf-8")
    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


@pytest.mark.parametrize("text", ["null", "[]", '"a string"', "17", "true"])
def test_valid_json_that_is_not_a_record_is_unknown(text):
    """`json.loads` accepts far more than the object this writes.

    A truncation or a stray `echo` leaves a file that parses cleanly and has
    no `.get`, so the ValueError guard lets it through and the AttributeError
    comes out of `operator ls` -- one instance's damaged file taking down the
    status listing for all of them. Unreadable and unparseable already answer
    "cannot tell"; so must this.
    """
    inst = op.Instance("notanobject")
    inst.loop_code_file.write_text(text, encoding="utf-8")
    assert op.loop_code_state(inst) == (op.CODE_UNKNOWN, [])


def test_a_record_with_no_files_is_unknown():
    inst = op.Instance("empty")
    _record(inst, [])
    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


def test_unchanged_files_are_current(tmp_path):
    src = tmp_path / "m.py"
    src.write_bytes(b"stable\n")
    inst = op.Instance("current")
    _record(inst, [{"path": str(src), "sha256": op._digest_file(src)}])
    assert op.loop_code_state(inst) == (op.CODE_CURRENT, [])


def test_a_changed_file_is_stale_and_is_named(tmp_path):
    src = tmp_path / "m.py"
    src.write_bytes(b"before\n")
    inst = op.Instance("stale")
    _record(inst, [{"path": str(src), "sha256": op._digest_file(src)}])
    src.write_bytes(b"after\n")

    verdict, changed = op.loop_code_state(inst)

    assert verdict == op.CODE_STALE
    assert changed == [str(src)]


def test_a_deleted_file_is_stale(tmp_path):
    """The supervisor is running a module that is no longer on disk. That is
    a definite difference, not an inability to look."""
    src = tmp_path / "m.py"
    src.write_bytes(b"here\n")
    inst = op.Instance("deleted")
    _record(inst, [{"path": str(src), "sha256": op._digest_file(src)}])
    src.unlink()

    verdict, changed = op.loop_code_state(inst)

    assert verdict == op.CODE_STALE
    assert changed == [str(src)]


def test_an_unreadable_file_is_unknown_never_current(tmp_path, monkeypatch):
    """Currency is a claim about every file. One nobody could examine
    withdraws it -- reporting `current` here would tell a reader the
    supervisor has a fix that may not be in it."""
    src = tmp_path / "m.py"
    src.write_bytes(b"x\n")
    inst = op.Instance("denied")
    _record(inst, [{"path": str(src), "sha256": op._digest_file(src)}])
    _unreadable(monkeypatch, src)

    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


def test_a_file_whose_digest_was_never_known_is_unknown(tmp_path):
    """The supervisor could not hash it at startup either, so there is
    nothing to compare against now."""
    src = tmp_path / "m.py"
    src.write_bytes(b"x\n")
    inst = op.Instance("nodigest")
    _record(inst, [{"path": str(src), "sha256": None}])
    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


def test_one_observed_change_outweighs_a_file_that_could_not_be_read(
        tmp_path, monkeypatch):
    """Staleness is established by a single changed file. Currency is not,
    which is why the two states resolve in opposite directions here."""
    changed_src = tmp_path / "changed.py"
    denied_src = tmp_path / "denied.py"
    changed_src.write_bytes(b"before\n")
    denied_src.write_bytes(b"x\n")
    inst = op.Instance("mixed")
    _record(inst, [
        {"path": str(changed_src), "sha256": op._digest_file(changed_src)},
        {"path": str(denied_src), "sha256": op._digest_file(denied_src)},
    ])
    changed_src.write_bytes(b"after\n")
    _unreadable(monkeypatch, denied_src)

    verdict, changed = op.loop_code_state(inst)

    assert verdict == op.CODE_STALE
    assert changed == [str(changed_src)]


def test_a_malformed_entry_does_not_pass_as_agreement(tmp_path):
    """A row that cannot be compared is undecided, not a match. Skipping it
    silently would let a truncated record read as `current`."""
    inst = op.Instance("malformed")
    _record(inst, [{"path": None, "sha256": "abc"}])
    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


# ── what the supervisor records about itself ────────────────────
def test_the_fingerprint_covers_the_operator_own_source():
    fingerprint = op.running_code_fingerprint()
    paths = [entry["path"] for entry in fingerprint["files"]]
    assert any(Path(p).name == "copilot_operator.py" for p in paths), paths
    assert any(Path(p).name == "operator_trace.py" for p in paths), paths


def test_the_fingerprint_excludes_source_outside_the_operator_directory():
    """Hashing the whole interpreter's imports would make every supervisor
    stale whenever anything on the machine changed."""
    here = Path(op.__file__).resolve().parent
    for entry in op.running_code_fingerprint()["files"]:
        assert here in Path(entry["path"]).parents, entry["path"]


def test_the_fingerprint_does_not_glob_the_directory():
    """A file that sits beside the operator but was never imported is not
    part of the code this process is running. Including it would fire the
    notice every time an unrelated tool in the checkout was edited.

    The module table is supplied rather than probed, so the negative is
    asserted about a file that demonstrably *exists* and demonstrably was not
    imported. An earlier version named a file that does not exist at all,
    which no implementation can return -- so it passed against a globbing one
    too, and a mutation proved it.
    """
    here = Path(op.__file__).resolve().parent
    neighbours = sorted(here.glob("*.py"))
    assert len(neighbours) >= 2, "expected several modules beside the operator"
    imported, not_imported = neighbours[0], neighbours[1]

    class OneModule:
        __file__ = str(imported)

    sources = op._loaded_operator_sources({"only": OneModule})

    assert imported in sources
    assert not_imported not in sources, (
        f"{not_imported.name} was never imported but is in the fingerprint "
        f"-- the implementation is enumerating the directory")


def test_the_fingerprint_ignores_modules_from_elsewhere(tmp_path):
    """Hashing every module the interpreter has loaded would make a
    supervisor stale whenever anything on the machine changed."""
    outsider = tmp_path / "outsider.py"
    outsider.write_text("x = 1\n", encoding="utf-8")

    class Outsider:
        __file__ = str(outsider)

    assert op._loaded_operator_sources({"out": Outsider}) == []


def test_the_fingerprint_skips_modules_with_no_file():
    """Builtin and namespace modules have no __file__; reaching for one
    would raise inside the supervisor's startup path."""

    class Builtin:
        __file__ = None

    assert op._loaded_operator_sources({"b": Builtin}) == []


def test_the_digest_moves_when_source_changes_though_the_version_does_not(
        tmp_path, monkeypatch):
    """The incident this exists for, in one test.

    The fix that made `session_exit` record handoff endings changed
    `copilot_operator.py` and bumped no version -- so a staleness check
    comparing version strings would have called the stale supervisor and the
    fixed one identical, which is exactly the report that was missing.
    """
    src = tmp_path / "m.py"
    src.write_bytes(b"before\n")
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [src])

    first = op.running_code_fingerprint()
    first_digest, first_version = first["digest"], first["version"]

    src.write_bytes(b"after\n")
    monkeypatch.setattr(op, "_RUNNING_CODE", None)
    second = op.running_code_fingerprint()

    assert second["version"] == first_version, "the version did not move"
    assert second["digest"] != first_digest, (
        "a version-only check could not have told these apart")


def test_the_fingerprint_keeps_answering_for_the_code_it_loaded(
        tmp_path, monkeypatch):
    """Recomputing on demand would hash whatever is on disk *now* and report
    the supervisor as running code it has never executed -- restating the
    confusion this exists to end."""
    src = tmp_path / "m.py"
    src.write_bytes(b"before\n")
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [src])

    first = op.running_code_fingerprint()["digest"]
    src.write_bytes(b"after\n")

    assert op.running_code_fingerprint()["digest"] == first


def test_an_unreadable_source_does_not_hash_as_a_constant(
        tmp_path, monkeypatch):
    """An unreadable source must be recorded as unknown, not as empty.

    The tempting shortcut is to let a failed read fall through to the digest
    of no bytes. That reads as a perfectly ordinary answer, so the file joins
    the fingerprint as a value the checker will happily compare -- and every
    unreadable file then agrees with every other one, which is a verdict of
    *current* built entirely out of files nobody could examine.

    Its predecessor made neither file unreadable: it hashed two ordinary
    files with different contents and asserted the digests differed, which is
    true of an implementation that folds unreadable files into a constant and
    true of one that crashes on them. It restated
    `test_digest_changes_with_the_bytes` under a name that promised the
    branch was covered.
    """
    src, empty = tmp_path / "src.py", tmp_path / "empty.py"
    src.write_bytes(b"contents that cannot be read\n")
    empty.write_bytes(b"")

    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [src])
    _unreadable(monkeypatch, src)
    recorded = op.running_code_fingerprint()["files"][0]["sha256"]
    assert recorded is None, (
        f"an unreadable source was recorded as {recorded!r}, a value the "
        "checker will compare as though the file had been examined")

    monkeypatch.undo()
    monkeypatch.setattr(op, "_RUNNING_CODE", None)
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [empty])
    assert op.running_code_fingerprint()["files"][0]["sha256"] is not None, (
        "a readable empty file is a known quantity and must be recorded as "
        "one -- otherwise this test passes against an implementation that "
        "records everything as unknown")


def test_saving_and_reading_back_reports_current(tmp_path, monkeypatch):
    """End to end: what the supervisor writes is what the checker reads."""
    src = tmp_path / "m.py"
    src.write_bytes(b"stable\n")
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [src])
    inst = op.Instance("roundtrip")

    op._save_loop_code(inst)

    assert op.loop_code_state(inst) == (op.CODE_CURRENT, [])
    src.write_bytes(b"edited\n")
    assert op.loop_code_state(inst) == (op.CODE_STALE, [str(src)])


def test_a_failed_write_does_not_take_the_supervisor_down(
        tmp_path, monkeypatch):
    """Losing this costs a verdict, never a running session."""
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [])

    def refuse(self, *args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", refuse)
    op._save_loop_code(op.Instance("unwritable"))  # must not raise


def test_cleanup_removes_the_code_record():
    inst = op.Instance("cleanup")
    _record(inst, [{"path": "x", "sha256": "y"}])
    assert inst.loop_code_file.exists()
    inst.cleanup_files()
    assert not inst.loop_code_file.exists()


# ── what a reader of the trace sees ─────────────────────────────
def test_supervisor_start_is_traced_with_its_code(tmp_path):
    operator_trace.record_supervisor_start(
        tmp_path, instance="proj", session=4,
        code={"digest": "abc123", "version": "1.4.0"})

    records = [json.loads(line) for line
               in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    starts = [r for r in records if r.get("event") == "supervisor_start"]
    assert len(starts) == 1
    assert starts[0]["instance"] == "proj"
    assert starts[0]["code"] == "abc123"
    assert starts[0]["toolkit_version"] == "1.4.0"


def test_session_exit_carries_the_code_that_wrote_it(tmp_path):
    """Without this a reader can only scope by date, and dates cannot see a
    supervisor that started before the fix and kept running."""
    operator_trace.record_session_exit(
        tmp_path, instance="proj", session=4, pid=1,
        markers={"restart": False}, consecutive=1, limit=5, code="abc123")

    record = json.loads(
        (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["code"] == "abc123"


def test_tracing_never_raises_on_a_bad_code_argument(tmp_path):
    """The trace is instrumentation; it must never be able to end a run."""
    operator_trace.record_supervisor_start(
        tmp_path, instance="proj", session=1, code="not-a-dict")
    operator_trace.record_supervisor_start(
        tmp_path, instance="proj", session=1, code=None)


# ── the startup window ──────────────────────────────────────────
def test_the_code_record_exists_by_the_time_the_pid_file_does(monkeypatch):
    """The invariant that keeps the notice honest during startup.

    Every consumer treats the loop pid file as "a supervisor is running", so
    if it appears first there is a window in which a healthy supervisor
    running the newest code reads as having recorded nothing -- and
    `operator ls` tells it to restart. Observed as a real ordering bug: the
    pid file used to be written three lines before the record.
    """
    inst = op.Instance("ordering")
    seen = {}
    real_write = op.Path.write_text

    def spy(self, *args, **kwargs):
        if self == inst.loop_pid_file:
            seen["code_present_at_pid_write"] = inst.loop_code_file.exists()
            seen["args_present_at_pid_write"] = inst.loop_args_file.exists()
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(op.Path, "write_text", spy)
    op._publish_supervisor_records(inst, ["--agent", "x"])

    assert seen["code_present_at_pid_write"] is True
    assert seen["args_present_at_pid_write"] is True


def test_publishing_writes_all_three_records(monkeypatch):
    """The companion to the ordering test above: it asserts the writes happen
    at all.

    The ordering test cannot be satisfied by an implementation that never
    writes the pid file -- its spy would not fire and the assertion would
    raise `KeyError` rather than pass -- so this is not a vacuity guard. It
    is here because a `KeyError` names the dictionary, not the defect, and
    the next person to see it should have a second failure that says which
    file went missing.
    """
    inst = op.Instance("published")
    op._publish_supervisor_records(inst, [])

    assert inst.loop_pid_file.exists()
    assert inst.loop_code_file.exists()
    assert inst.loop_args_file.exists()
    assert inst.loop_pid_file.read_text(encoding="utf-8").strip() == str(
        os.getpid())


def test_a_supervisor_that_just_published_reads_as_current():
    """End to end through the real files: publish, then ask the question
    `operator ls` asks. Anything other than `current` here means a freshly
    started supervisor would be told to restart itself."""
    inst = op.Instance("freshstart")
    op._publish_supervisor_records(inst, [])

    assert op.loop_code_state(inst) == (op.CODE_CURRENT, [])


# ── what an operator sees ───────────────────────────────────────
def _snap(**over):
    snap = {"name": "proj", "loop_pid": 123, "session_live": True,
            "owned": True, "session_num": 3, "run_started": "", "cwd": "",
            "loop_started": "", "loop_adopted": None, "loop_began_run": None,
            "loop_code": op.CODE_CURRENT}
    snap.update(over)
    return snap


def test_a_stale_supervisor_is_called_out():
    assert "older code" in op._instance_summary(_snap(loop_code=op.CODE_STALE))


def test_a_current_supervisor_is_not_called_out():
    assert "older code" not in op._instance_summary(_snap())


def test_an_unknown_verdict_is_not_reported_as_stale():
    """`unknown` means the record could not be read, not that the code is
    behind. Telling those users their code is stale would make the notice
    noise."""
    assert "older code" not in op._instance_summary(
        _snap(loop_code=op.CODE_UNKNOWN))


def test_an_unrecorded_supervisor_is_called_out():
    """The case this check exists for and could not report: measured
    2026-08-05T11:35Z, all six running supervisors predated the record, so
    every one read `unknown` and `operator ls` printed nothing at all.

    Asserts the label rather than the bare verdict string. `CODE_UNRECORDED`
    *is* the word "unrecorded", so a summary that merely dumped the raw
    verdict into the row would satisfy the looser assertion without the
    notice ever having been written.
    """
    summary = op._instance_summary(_snap(loop_code=op.CODE_UNRECORDED))
    assert "[supervisor code unrecorded]" in summary


def test_an_unrecorded_supervisor_is_not_called_stale():
    """It is not known to be behind -- that is the entire point. The notice
    must report the missing record, not invent a verdict from it."""
    assert "older code" not in op._instance_summary(
        _snap(loop_code=op.CODE_UNRECORDED))


def test_a_current_supervisor_is_not_called_unrecorded():
    """Negative control for the label above: a message that cannot be absent
    carries no information when it appears."""
    assert "unrecorded" not in op._instance_summary(_snap())


def test_an_unrecorded_supervisor_that_is_not_running_is_not_called_out():
    """No supervisor, no loaded code to be unaccounted for, and
    `restart-loop` has nothing to restart."""
    assert "unrecorded" not in op._instance_summary(
        _snap(loop_pid=None, loop_code=op.CODE_UNRECORDED))


def test_a_stopped_instance_is_not_called_out():
    """With no supervisor running there is no loaded code to be behind, and
    the remedy (`restart-loop`) does not apply."""
    assert "older code" not in op._instance_summary(
        _snap(loop_pid=None, loop_code=op.CODE_STALE))


def test_the_listing_names_the_remedy_for_each_stale_instance(
        monkeypatch, capsys):
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha"),
                                                         op.Instance("beta")])
    snaps = {"alpha": _snap(name="alpha", loop_code=op.CODE_STALE),
             "beta": _snap(name="beta", loop_code=op.CODE_CURRENT)}
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: snaps[inst.display_name])

    op.list_instances()
    out = capsys.readouterr().out

    assert "operator restart-loop alpha" in out
    assert "operator restart-loop beta" not in out


def test_the_listing_names_the_remedy_for_each_unrecorded_instance(
        monkeypatch, capsys):
    """The whole point of the change, at the level the operator actually
    reads. Before it, this listing was byte-identical to the all-current
    one."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha"),
                                                         op.Instance("beta")])
    snaps = {"alpha": _snap(name="alpha", loop_code=op.CODE_UNRECORDED),
             "beta": _snap(name="beta", loop_code=op.CODE_CURRENT)}
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: snaps[inst.display_name])

    op.list_instances()
    out = capsys.readouterr().out

    assert "operator restart-loop alpha" in out
    assert "operator restart-loop beta" not in out


def test_an_unreadable_record_does_not_produce_the_notice(monkeypatch, capsys):
    """`unknown` stays silent. The notice claims the supervisor left no
    record; a record nobody could read is not that, and a remedy offered on a
    guess is how a notice becomes noise."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha",
                                           loop_code=op.CODE_UNKNOWN))

    op.list_instances()

    assert "restart-loop" not in capsys.readouterr().out


def test_stale_and_unrecorded_are_reported_as_separate_reasons(
        monkeypatch, capsys):
    """Both need the same restart for different reasons, and one sentence
    covering both would say something false about one of them.

    Asserts that each instance is listed under *its own* reason, not that one
    group is printed before the other. Pinning the group order would fail on
    a refactor that swapped two correct blocks, which is a fact about today's
    print statements rather than about the behaviour.
    """
    monkeypatch.setattr(op, "active_instances",
                        lambda: [op.Instance("alpha"), op.Instance("beta")])
    snaps = {"alpha": _snap(name="alpha", loop_code=op.CODE_STALE),
             "beta": _snap(name="beta", loop_code=op.CODE_UNRECORDED)}
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: snaps[inst.display_name])

    op.list_instances()
    out = capsys.readouterr().out

    assert "changed on disk" in out
    assert "did not record" in out
    assert out.index("operator restart-loop alpha") > out.index("changed on disk")
    assert out.index("operator restart-loop beta") > out.index("did not record")


def test_the_listing_says_nothing_when_every_supervisor_is_current(
        monkeypatch, capsys):
    """The negative control for the notice: a message that cannot be absent
    carries no information when it appears."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha"))

    op.list_instances()

    assert "restart-loop" not in capsys.readouterr().out


# ── a supervisor that is not the one that began the run ─────────
#
# Measured 2026-08-09T17:27 local: every supervisor on the machine had been
# killed with the logon session and relaunched ninety seconds earlier, and
# `operator list` reported all eight "up 10d 13h" -- output byte-identical to
# a machine nothing had touched. `RUN_STARTED` is carried across the restart
# on purpose, so the one field the row showed was the one that could not
# change. The supervisor's own start instant was already on disk the whole
# time, stamped as `recorded` by `_save_loop_code`; nothing read it.
def test_a_supervisor_younger_than_its_run_is_a_restart():
    assert op.supervisor_restarted_after("2026-07-30T10:36:35Z",
                                         "2026-08-10T00:26:37Z")


def test_a_supervisor_that_began_its_own_run_is_not_a_restart():
    """The negative control. A supervisor publishes its records before the
    first session is launched, so on a fresh run it is a few seconds *older*
    than `RUN_STARTED` -- the margin must not read that as a restart."""
    assert not op.supervisor_restarted_after("2026-08-09T12:00:05Z",
                                             "2026-08-09T12:00:00Z")


def test_a_restart_inside_the_margin_is_not_claimed():
    """Bounds the detector from the other side: within the margin the answer
    is "no claim", and the margin exists to protect the negative control
    above rather than to be as small as possible."""
    assert not op.supervisor_restarted_after("2026-08-09T12:00:00Z",
                                             "2026-08-09T12:04:00Z")


def test_a_restart_just_past_the_margin_is_claimed():
    """The positive control for the boundary itself. Without this the margin
    could be widened to infinity and every test above would still pass."""
    assert op.supervisor_restarted_after("2026-08-09T12:00:00Z",
                                         "2026-08-09T12:05:01Z")


@pytest.mark.parametrize("run_started, loop_started", [
    ("2026-07-30T10:36:35Z", None),      # record absent or unreadable
    ("2026-07-30T10:36:35Z", ""),        # snapshot's empty-string default
    ("2026-07-30T10:36:35Z", "garbage"),
    ("", "2026-08-10T00:26:37Z"),        # no run start to compare against
    ("garbage", "2026-08-10T00:26:37Z"),
])
def test_nothing_to_compare_makes_no_claim(run_started, loop_started):
    """"Cannot tell" must not be reported as "did not restart" -- but here it
    collapses to `False` because `False` is *silence*, not an assertion. The
    missing record is reported in its own right by `loop_code_state`, so the
    reader is never left with nothing.
    """
    assert not op.supervisor_restarted_after(run_started, loop_started)


def test_a_restarted_supervisor_is_called_out_in_its_row():
    summary = op._instance_summary(_snap(run_started="2026-07-30T10:36:35Z",
                                         loop_started="2026-08-10T00:26:37Z"))
    assert "[supervisor restarted" in summary


def test_a_supervisor_that_began_its_run_is_not_called_out_in_its_row():
    assert "restarted" not in op._instance_summary(
        _snap(run_started="2026-08-09T12:00:05Z",
              loop_started="2026-08-09T12:00:00Z"))


def test_the_row_still_shows_the_run_age_next_to_the_restart():
    """The two are different facts and both are wanted: `up` dates the run,
    the notice dates the process. Dropping either is how one of them came to
    stand for the other."""
    summary = op._instance_summary(_snap(run_started="2026-07-30T10:36:35Z",
                                         loop_started="2026-08-10T00:26:37Z"))
    assert "up " in summary
    assert "[supervisor restarted" in summary


def test_a_restarted_supervisor_that_is_not_running_is_not_called_out():
    """No live supervisor, nothing whose start instant describes anything
    now -- the record belongs to a process that has since gone."""
    assert "restarted" not in op._instance_summary(
        _snap(loop_pid=None, run_started="2026-07-30T10:36:35Z",
              loop_started="2026-08-10T00:26:37Z"))


def test_the_listing_explains_what_a_restart_means(monkeypatch, capsys):
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha",
                                           run_started="2026-07-30T10:36:35Z",
                                           loop_started="2026-08-10T00:26:37Z"))

    op.list_instances()
    out = capsys.readouterr().out

    assert "alpha" in out
    assert "the process that began it" in out


def test_the_listing_does_not_claim_a_session_was_lost(monkeypatch, capsys):
    """The record establishes that the supervisor was replaced. It does not
    establish that a session died without a handoff -- `restart_loop` hands
    its session over intact, and whether a handoff was written is a separate
    question with its own instrument. An earlier draft asserted the cost, and
    adversarial review caught it."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha",
                                           run_started="2026-07-30T10:36:35Z",
                                           loop_started="2026-08-10T00:26:37Z"))

    op.list_instances()

    assert "without a handoff" not in capsys.readouterr().out


def test_the_listing_says_nothing_about_restarts_when_there_were_none(
        monkeypatch, capsys):
    """Negative control for the group: a paragraph that is always printed
    tells the reader nothing when it appears."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha",
                                           run_started="2026-08-09T12:00:05Z",
                                           loop_started="2026-08-09T12:00:00Z"))

    op.list_instances()

    assert "the process that began it" not in capsys.readouterr().out


# ── a deliberate handover is not a takeover ─────────────────────
#
# `operator restart-loop` retires one supervisor and hands the live session to
# a new one on purpose, and leaves the identical trace on disk: a supervisor
# younger than the run it is running. Reporting it would fire the notice on a
# routine action and claim a loss that did not happen -- caught by adversarial
# review before this shipped.
def _adopted(**over):
    snap = {"run_started": "2026-07-30T10:36:35Z",
            "loop_started": "2026-08-10T00:26:37Z", "loop_adopted": True}
    snap.update(over)
    return _snap(**snap)


def test_a_supervisor_that_adopted_its_session_is_not_a_takeover():
    assert not op.supervisor_took_over(_adopted())


def test_a_supervisor_that_started_its_own_session_is_a_takeover():
    """The positive control against the case above: same timestamps, and only
    the adoption flag differs."""
    assert op.supervisor_took_over(_adopted(loop_adopted=False))


def test_an_unrecorded_adoption_is_still_reported():
    """`None` is not `False`, but here it must not buy silence either. Every
    supervisor started before the flag existed records `None`, and a notice
    that skipped them would be the same all-clear-nobody-checked this whole
    item is about."""
    assert op.supervisor_took_over(_adopted(loop_adopted=None))


def test_an_adopted_handover_is_not_called_out_in_its_row():
    assert "restarted" not in op._instance_summary(_adopted())


def test_an_adopted_handover_is_absent_from_the_listing(monkeypatch, capsys):
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _adopted(name="alpha"))

    op.list_instances()

    assert "the process that began it" not in capsys.readouterr().out


def test_the_adoption_flag_is_recorded_by_a_real_publish():
    """End to end: a unit test asserting on `loop_adopted` proves nothing if
    the supervisor never writes the field."""
    adopted_inst = op.Instance("adopted")
    op._publish_supervisor_records(adopted_inst, [], adopted=True)
    own_inst = op.Instance("ownsession")
    op._publish_supervisor_records(own_inst, [], adopted=False)

    assert op.loop_adopted(adopted_inst) is True
    assert op.loop_adopted(own_inst) is False


def test_a_record_without_the_flag_reads_as_unknown_not_false():
    inst = op.Instance("preflag")
    _record(inst, [])

    assert op.loop_adopted(inst) is None


# ── a leftover record must not describe the live supervisor ─────
#
# `_save_loop_code` tolerates a failed write by design, so a supervisor whose
# record could not be replaced keeps its predecessor's -- which would date a
# live process by a dead one and hide the very restart this reports.
def test_a_record_belonging_to_another_process_dates_nothing():
    inst = op.Instance("mismatch")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 111, "recorded": "2026-08-10T00:26:37Z",
                    "adopted": True, "files": []}), encoding="utf-8")

    assert op.loop_started_at(inst, 222) is None
    assert op.loop_adopted(inst, 222) is None


def test_a_record_matching_the_running_supervisor_is_used():
    """The positive control: the same record, read with the pid it names."""
    inst = op.Instance("match")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 222, "recorded": "2026-08-10T00:26:37Z",
                    "adopted": True, "files": []}), encoding="utf-8")

    assert op.loop_started_at(inst, 222) == "2026-08-10T00:26:37Z"
    assert op.loop_adopted(inst, 222) is True


def test_a_caller_that_names_no_pid_still_gets_the_record():
    """Asking without a pid is asking about whatever record is there.
    Inventing a mismatch that was never checked would be its own false
    answer."""
    inst = op.Instance("nopid")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 111, "recorded": "2026-08-10T00:26:37Z",
                    "files": []}), encoding="utf-8")

    assert op.loop_started_at(inst) == "2026-08-10T00:26:37Z"


def test_a_record_without_a_pid_is_a_mismatch():
    """A record with no pid cannot establish that it describes the live
    process, and there is no pre-pid schema to be lenient towards: the pid
    field landed in the same commit as the record itself (7b5b58d, verified
    with `git log -S`). So a pid-less record is damaged, not old, and damage
    must not read as agreement -- that is the leftover-record hole. An earlier
    draft accepted it on the strength of a history that never existed, and
    adversarial review caught the invented data."""
    inst = op.Instance("nopidfield")
    inst.loop_code_file.write_text(
        json.dumps({"recorded": "2026-08-10T00:26:37Z", "files": []}),
        encoding="utf-8")

    assert op.loop_started_at(inst, 222) is None


def test_a_record_with_a_non_integer_pid_is_a_mismatch():
    inst = op.Instance("junkpid")
    inst.loop_code_file.write_text(
        json.dumps({"pid": "222", "recorded": "2026-08-10T00:26:37Z",
                    "files": []}), encoding="utf-8")

    assert op.loop_started_at(inst, 222) is None


# ── the code verdict must answer about the live supervisor too ──
def test_a_leftover_record_does_not_decide_the_code_verdict(tmp_path):
    """The sibling hole to the one above. A predecessor's record compared
    against disk answers about a process that has gone -- it can call a
    current supervisor stale, or clear one that is genuinely behind.

    The verdict is `CODE_MISMATCH` and not `CODE_UNKNOWN` because
    `list_instances` prints nothing for `unknown`: answering that would swap a
    wrong verdict for no verdict, which is this item's signature failure. An
    earlier draft did exactly that and adversarial review caught it.
    """
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = op.Instance("codemismatch")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 111, "recorded": "2026-08-10T00:26:37Z",
                    "files": [{"path": str(src), "sha256": _sha(src)}]}),
        encoding="utf-8")

    assert op.loop_code_state(inst, 111) == (op.CODE_CURRENT, [])
    assert op.loop_code_state(inst, 222) == (op.CODE_MISMATCH, [])


def test_the_code_verdict_is_unchanged_when_no_pid_is_supplied(tmp_path):
    """Negative control: every existing caller passes no pid and must keep
    its answer."""
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = op.Instance("codenopid")
    inst.loop_code_file.write_text(
        json.dumps({"pid": 111, "recorded": "2026-08-10T00:26:37Z",
                    "files": [{"path": str(src), "sha256": _sha(src)}]}),
        encoding="utf-8")

    assert op.loop_code_state(inst) == (op.CODE_CURRENT, [])


def _live_supervisor(monkeypatch, name):
    """An instance whose supervisor pid file names *this* process.

    Uses the running interpreter's own pid rather than a fabricated one so
    `_running_loop_pid` finds a genuinely live process and returns it. A made
    up pid is pruned as dead, `loop_pid` comes back ``None``, and every
    record reader then takes its "caller named no pid" path -- which is the
    one path these tests must not exercise.
    """
    inst = op.Instance(name)
    inst.claim("tok")
    inst.save_state(3, "2026-07-30T09:00:00Z")
    inst.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(op.MUX, "available", lambda: False)
    return inst


def _snapshot_record(inst, pid, src):
    inst.loop_code_file.write_text(
        json.dumps({"pid": pid, "recorded": "2026-08-10T00:26:37Z",
                    "adopted": False, "began_run": True,
                    "files": [{"path": str(src), "sha256": _sha(src)}]}),
        encoding="utf-8")


def test_the_snapshot_asks_the_record_readers_about_the_live_supervisor(
        tmp_path, monkeypatch):
    """The wiring, not the predicate. Every reader above is exercised
    directly, so all of them keep answering correctly even if
    `instance_snapshot` stops handing them the running pid -- and that one
    argument is the whole reason the fix reaches `operator list`. Mutation
    testing found this gap: dropping the pid from the `loop_code_state` call
    left the suite green.
    """
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = _live_supervisor(monkeypatch, "snapmismatch")
    _snapshot_record(inst, os.getpid() + 1, src)

    snap = op.instance_snapshot(inst)

    assert snap["loop_pid"] == os.getpid()
    assert snap["loop_code"] == op.CODE_MISMATCH
    assert snap["loop_started"] == ""
    assert snap["loop_adopted"] is None
    assert snap["loop_began_run"] is None


def test_the_snapshot_reads_a_matching_record_in_full(tmp_path, monkeypatch):
    """Positive control for the test above. Identical record but for the pid,
    so a snapshot that reported ``unknown`` for everything -- which would pass
    the mismatch test on its own -- fails here.
    """
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = _live_supervisor(monkeypatch, "snapmatch")
    _snapshot_record(inst, os.getpid(), src)

    snap = op.instance_snapshot(inst)

    assert snap["loop_code"] == op.CODE_CURRENT
    assert snap["loop_started"] == "2026-08-10T00:26:37Z"
    assert snap["loop_adopted"] is False
    assert snap["loop_began_run"] is True


# ── a mismatched record must be reported, not merely withheld ───
#
# `list_instances` prints a group for `stale` and one for `unrecorded` and
# nothing at all for `unknown`. So answering "unknown" on a mismatch replaces
# a wrong verdict with an absent one, and the row goes back to being
# byte-identical to a healthy machine -- which is the failure this whole item
# is about, reproduced inside its own remedy. Caught by adversarial review.
def test_a_mismatched_record_is_named_in_the_listing(monkeypatch, capsys):
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha",
                                           loop_code=op.CODE_MISMATCH))

    op.list_instances()
    out = capsys.readouterr().out

    assert "belongs to a different" in out
    assert "operator restart-loop alpha" in out


def test_the_listing_says_nothing_about_mismatches_when_there_are_none(
        monkeypatch, capsys):
    """Negative control: a paragraph that is always printed says nothing."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot", lambda inst: _snap(name="alpha"))

    op.list_instances()

    assert "belongs to a different" not in capsys.readouterr().out


def test_a_mismatched_record_is_named_on_the_row(monkeypatch):
    assert "record is not its own" in op._instance_summary(
        _snap(loop_code=op.CODE_MISMATCH))


def test_a_current_supervisor_is_not_named_on_the_row():
    """Negative control for the row marker."""
    assert "record is not its own" not in op._instance_summary(_snap())


def test_a_stopped_instance_is_never_called_mismatched():
    """No live supervisor means no record to be anybody's: the marker must
    not attach to a row it cannot act on, which is how the sibling markers
    behave."""
    assert "record is not its own" not in op._instance_summary(
        _snap(loop_pid=None, loop_code=op.CODE_MISMATCH))


# ── a pid is not an identity ────────────────────────────────────
#
# Windows recycles pids aggressively. `_save_loop_code` tolerates a failed
# write, so a supervisor that could not replace its predecessor's record can
# be handed that predecessor's pid by the OS -- and a pid-only check reads the
# dead process's record as its own. `process_start_token` is the discrimination
# the repository already uses for exactly this, in `operator_session` and
# `operator_work`.
def test_a_recycled_pid_does_not_make_a_predecessor_record_its_own(monkeypatch):
    monkeypatch.setattr(op.operator_liveness, "process_start_token",
                        lambda pid: "started-at-9am")

    assert not op._record_describes(
        {"pid": 123, "pid_start": "started-at-8am"}, 123)


def test_a_matching_start_token_is_the_supervisors_own_record(monkeypatch):
    """Positive control against the case above: same pid, same everything but
    the token, which is the only thing that may decide it."""
    monkeypatch.setattr(op.operator_liveness, "process_start_token",
                        lambda pid: "started-at-8am")

    assert op._record_describes(
        {"pid": 123, "pid_start": "started-at-8am"}, 123)


def _token_probe(monkeypatch, value="started-at-9am"):
    """Patch the live-token probe and count its calls.

    Counting matters as much as the value. Adversarial review pointed out
    that the fallback branches short-circuit *before* probing, so a plain
    monkeypatch in those tests is dead code that asserts nothing -- and a
    patch that is never reached looks identical to one that is. Asserting
    the count pins which branch actually ran.
    """
    calls = {"n": 0}

    def probe(pid):
        calls["n"] += 1
        return value

    monkeypatch.setattr(op.operator_liveness, "process_start_token", probe)
    return calls


def test_a_record_predating_the_token_falls_back_to_the_pid(monkeypatch):
    """Unlike `pid`, `pid_start` has a real pre-stamp history: every
    supervisor running when it landed wrote a record without one. Refusing
    those would have reported every supervisor on the machine as a leftover
    on day one."""
    calls = _token_probe(monkeypatch)

    assert op._record_describes({"pid": 123}, 123)
    assert not op._record_describes({"pid": 999}, 123)
    assert calls["n"] == 0, "no token to compare, so nothing to probe for"


def test_a_null_token_falls_back_to_the_pid(monkeypatch):
    """`_save_loop_code` writes whatever `process_start_token` returned, and
    that is ``None`` on a machine where the probe cannot answer. A record
    saying "I could not tell either" is the absent case, not damage."""
    calls = _token_probe(monkeypatch)

    assert op._record_describes({"pid": 123, "pid_start": None}, 123)
    assert calls["n"] == 0


def test_an_unreadable_live_token_falls_back_to_the_pid(monkeypatch):
    """`process_start_token` returns None for a pid it cannot inspect. That
    is an absence of evidence, and it must not be spent refuting a record."""
    calls = _token_probe(monkeypatch, value=None)

    assert op._record_describes(
        {"pid": 123, "pid_start": "started-at-8am"}, 123)
    assert calls["n"] == 1, "this branch is only reachable by probing"


@pytest.mark.parametrize("junk", [17, "", [], {}, True])
def test_a_malformed_token_is_a_mismatch(monkeypatch, junk):
    """Present but impossible. `_save_loop_code` writes a non-empty string or
    ``None``, so no version of it produces these -- they are damage, and the
    leniency above is owed to a real earlier schema, not to corruption. The
    first draft accepted `17` and `""`; adversarial review caught it."""
    calls = _token_probe(monkeypatch)

    assert not op._record_describes({"pid": 123, "pid_start": junk}, 123)
    assert calls["n"] == 0, "refused on the record alone, without probing"


# ── the token is boot-relative on Linux ─────────────────────────
#
# `_linux_start_token` is field 22 of /proc/<pid>/stat: clock ticks *since
# boot*. Across a reboot a replacement can therefore collide with its
# predecessor on both pid and token, which is the one case the token alone
# cannot refute. `same_boot` compares the tagged forms correctly and answers
# None for anything it cannot compare, so only a definite False refutes.
def _tokens(monkeypatch, live_boot):
    monkeypatch.setattr(op.operator_liveness, "process_start_token",
                        lambda pid: "linux:900")
    monkeypatch.setattr(op.operator_liveness, "boot_identity",
                        lambda: live_boot)


def test_a_record_from_another_boot_is_a_mismatch(monkeypatch):
    _tokens(monkeypatch, "uuid:bbb")

    assert not op._record_describes(
        {"pid": 123, "pid_start": "linux:900", "boot": "uuid:aaa"}, 123)


def test_a_record_from_this_boot_is_its_own(monkeypatch):
    """Positive control: identical but for the boot id, which is the only
    thing that may decide it."""
    _tokens(monkeypatch, "uuid:aaa")

    assert op._record_describes(
        {"pid": 123, "pid_start": "linux:900", "boot": "uuid:aaa"}, 123)


def test_a_boot_identity_that_cannot_be_compared_does_not_refute(monkeypatch):
    """`same_boot` answers None across tag kinds -- a record written on
    another platform, or a machine whose exact source stopped answering. An
    absence of evidence must not be spent as a refutation."""
    _tokens(monkeypatch, "instant:1000")

    assert op._record_describes(
        {"pid": 123, "pid_start": "linux:900", "boot": "uuid:aaa"}, 123)


def test_a_record_predating_the_boot_stamp_falls_back(monkeypatch):
    """Same pre-stamp history argument as `pid_start`: records without the
    key exist right now."""
    _tokens(monkeypatch, "uuid:bbb")

    assert op._record_describes({"pid": 123, "pid_start": "linux:900"}, 123)


def test_a_null_boot_identity_in_the_record_falls_back(monkeypatch):
    _tokens(monkeypatch, "uuid:bbb")

    assert op._record_describes(
        {"pid": 123, "pid_start": "linux:900", "boot": None}, 123)


@pytest.mark.parametrize("junk", [17, "", []])
def test_a_malformed_boot_identity_is_a_mismatch(monkeypatch, junk):
    _tokens(monkeypatch, "uuid:aaa")

    assert not op._record_describes(
        {"pid": 123, "pid_start": "linux:900", "boot": junk}, 123)


def test_the_boot_identity_is_stamped_by_a_real_publish():
    inst = op.Instance("bootstamp")
    op._publish_supervisor_records(inst, [])

    payload = json.loads(inst.loop_code_file.read_text(encoding="utf-8"))

    assert payload["boot"] == op.operator_liveness.boot_identity()


# ── the record is read once per snapshot ────────────────────────
#
# Each of the four readers opens the file and runs `_record_describes`, which
# probes process identity. That is free on Windows (0.021 ms measured) and is
# not on macOS/BSD, where `operator_liveness._ps_start_token` spawns `ps` with
# a ten-second timeout -- four subprocesses per instance per `operator list`.
# Found by adversarial review; the same "measured on Windows, therefore free"
# shape the repository's `os.path` note warns about.
def test_the_snapshot_reads_the_record_once(tmp_path, monkeypatch):
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = _live_supervisor(monkeypatch, "readonce")
    _snapshot_record(inst, os.getpid(), src)

    reads = {"n": 0}
    real = op._read_loop_record
    monkeypatch.setattr(op, "_read_loop_record",
                        lambda i: (reads.__setitem__("n", reads["n"] + 1),
                                   real(i))[1])
    probes = {"n": 0}
    real_token = op.operator_liveness.process_start_token
    monkeypatch.setattr(op.operator_liveness, "process_start_token",
                        lambda pid: (probes.__setitem__("n", probes["n"] + 1),
                                     real_token(pid))[1])

    op.instance_snapshot(inst)

    assert reads["n"] == 1
    assert probes["n"] <= 1


def test_reading_once_gives_the_same_answers_as_reading_four_times(
        tmp_path, monkeypatch):
    """The refactor's negative control. Cutting four reads to one is only
    safe if it changed no answer, so this asserts the batch reader agrees
    with each individual reader rather than merely that it returns
    something."""
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = _live_supervisor(monkeypatch, "agreeonce")
    _snapshot_record(inst, os.getpid(), src)
    pid = os.getpid()

    facts = op.loop_record_facts(inst, pid)

    assert facts["code"] == op.loop_code_state(inst, pid)[0]
    assert facts["changed"] == op.loop_code_state(inst, pid)[1]
    assert facts["started"] == op.loop_started_at(inst, pid)
    assert facts["adopted"] == op.loop_adopted(inst, pid)
    assert facts["began_run"] == op.loop_began_run(inst, pid)


@pytest.mark.parametrize("pid_used", ["match", "mismatch"])
def test_the_batch_reader_agrees_on_a_mismatch_too(tmp_path, monkeypatch,
                                                   pid_used):
    src = tmp_path / "mod.py"
    src.write_text("x = 1\n", encoding="utf-8")
    inst = _live_supervisor(monkeypatch, f"agree{pid_used}")
    _snapshot_record(inst, os.getpid(), src)
    pid = os.getpid() if pid_used == "match" else os.getpid() + 1

    facts = op.loop_record_facts(inst, pid)

    assert facts["code"] == op.loop_code_state(inst, pid)[0]
    assert facts["started"] == op.loop_started_at(inst, pid)
    assert facts["adopted"] == op.loop_adopted(inst, pid)
    assert facts["began_run"] == op.loop_began_run(inst, pid)


def test_the_batch_reader_reports_an_absent_record_as_unrecorded():
    """The verdict from `_read_loop_record` must survive the batch path --
    `unrecorded` and `unknown` are kept apart everywhere else."""
    assert op.loop_record_facts(op.Instance("nobatchrecord"))["code"] == \
        op.CODE_UNRECORDED


def test_the_start_token_is_stamped_by_a_real_publish():
    """End to end: the refutation above is worth nothing if no supervisor
    ever writes the field. Asserts against this process's own live token
    rather than a constant, so it fails if the value written is fabricated."""
    inst = op.Instance("tokenstamp")
    op._publish_supervisor_records(inst, [])

    payload = json.loads(inst.loop_code_file.read_text(encoding="utf-8"))

    assert payload["pid"] == os.getpid()
    assert payload["pid_start"] == \
        op.operator_liveness.process_start_token(os.getpid())


def test_a_real_published_record_is_its_own_writers(monkeypatch):
    """The whole chain, unmocked: a record this process wrote must read as
    describing this process, token and all."""
    inst = op.Instance("tokenroundtrip")
    op._publish_supervisor_records(inst, [])

    payload = json.loads(inst.loop_code_file.read_text(encoding="utf-8"))

    assert op._record_describes(payload, os.getpid())


# ── whether it began the run, recorded rather than estimated ────
def test_a_supervisor_that_recorded_beginning_the_run_is_not_a_takeover():
    """The recorded answer outranks the timestamps. These timestamps are the
    ones that would otherwise read as a restart, so this fails if the record
    is ignored."""
    assert not op.supervisor_took_over(
        _snap(run_started="2026-07-30T10:36:35Z",
              loop_started="2026-08-10T00:26:37Z", loop_began_run=True))


def test_a_supervisor_that_recorded_inheriting_the_run_is_a_takeover():
    """The case the margin could not see: a supervisor destroyed and
    relaunched seconds into a run. The timestamps are inside
    `SUPERVISOR_RESTART_MARGIN`, so this fails if the record is ignored."""
    assert op.supervisor_took_over(
        _snap(run_started="2026-08-09T12:00:00Z",
              loop_started="2026-08-09T12:00:30Z", loop_began_run=False))


def test_an_adopted_handover_is_not_a_takeover_even_when_it_inherited():
    """`restart-loop` inherits the run by definition; the adoption flag is
    what keeps it quiet."""
    assert not op.supervisor_took_over(
        _snap(run_started="2026-08-09T12:00:00Z",
              loop_started="2026-08-09T12:00:30Z",
              loop_began_run=False, loop_adopted=True))


def test_without_the_record_the_timestamps_still_decide():
    """The fallback for supervisors predating the stamp, which is the whole
    population on this machine today."""
    assert op.supervisor_took_over(
        _snap(run_started="2026-07-30T10:36:35Z",
              loop_started="2026-08-10T00:26:37Z", loop_began_run=None))


def test_the_began_run_flag_is_recorded_by_a_real_publish():
    fresh = op.Instance("freshrun")
    op._publish_supervisor_records(fresh, [], began_run=True)
    inherited = op.Instance("inheritedrun")
    op._publish_supervisor_records(inherited, [], began_run=False)

    assert op.loop_began_run(fresh) is True
    assert op.loop_began_run(inherited) is False


def test_a_record_without_the_began_run_flag_reads_as_unknown():
    inst = op.Instance("prebeganflag")
    _record(inst, [])

    assert op.loop_began_run(inst) is None


def test_the_start_instant_is_read_back_from_a_real_published_record():
    """End to end through the files a supervisor actually writes. Every test
    above hands `supervisor_restarted_after` a string; this is the one that
    checks a real record carries a stamp that `_parse_utc` accepts, so a
    change to the record's format cannot leave the detector silently unable
    to read it while every unit test stays green."""
    inst = op.Instance("stamped")
    op._publish_supervisor_records(inst, [])

    stamp = op.loop_started_at(inst)

    assert stamp
    assert op._parse_utc(stamp) is not None


def test_no_record_means_the_start_instant_is_unknown_not_absent():
    assert op.loop_started_at(op.Instance("neverpublished")) is None


def test_an_unreadable_record_does_not_report_a_start_instant(monkeypatch):
    inst = op.Instance("denied")
    inst.loop_code_file.write_text('{"recorded": "2026-08-10T00:26:37Z"}',
                                   encoding="utf-8")
    _unreadable(monkeypatch, inst.loop_code_file)

    assert op.loop_started_at(inst) is None


def test_the_detail_view_dates_the_supervisor_as_well_as_the_run(capsys):
    """`operator stats` shows "Running for", which is kept across a restart.
    On its own it reports a healthy ten-day run on a machine where every
    session died ninety seconds ago."""
    op._print_instance_detail(_detail_snap(
        run_started="2026-07-30T10:36:35Z",
        loop_started="2026-08-10T00:26:37Z"))
    out = capsys.readouterr().out

    assert "Running for" in out
    assert "Supervisor started" in out


def test_the_detail_view_says_nothing_when_the_supervisor_began_the_run(capsys):
    """Negative control: a row that is always printed carries no information
    when it appears."""
    op._print_instance_detail(_detail_snap(
        run_started="2026-08-09T12:00:05Z",
        loop_started="2026-08-09T12:00:00Z"))

    assert "Supervisor started" not in capsys.readouterr().out


# ── what `operator trace` shows ─────────────────────────────────
def _trace(tmp_path, *records):
    path = tmp_path / "trace.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records),
                    encoding="utf-8")
    return path


def _exit_record(**over):
    rec = {"ts": "2026-08-05T01:02:52Z", "event": "session_exit",
           "instance": "proj", "session": 7, "session_pid": 99,
           "markers": {"stop": False, "detach": False, "restart": False,
                       "exit_code": None, "uptime_s": 340},
           "consecutive": 1, "limit": 5, "giving_up": False}
    rec.update(over)
    return rec


def test_a_session_ending_shows_which_code_recorded_it(tmp_path, capsys):
    _trace(tmp_path, _exit_record(code="deadbeefcafe0001"))
    op.show_trace([])
    assert "code=deadbeefcafe0001" in capsys.readouterr().out


def test_an_unstamped_ending_says_so_rather_than_looking_stamped(
        tmp_path, capsys):
    """The pre-fix records are the ones no conclusion should rest on, so they
    have to be visibly distinguishable rather than merely blank."""
    _trace(tmp_path, _exit_record())
    op.show_trace([])
    assert "code=unrecorded" in capsys.readouterr().out


def test_supervisor_starts_are_shown(tmp_path, capsys):
    _trace(tmp_path, {"ts": "2026-08-05T06:00:00Z", "event": "supervisor_start",
                      "instance": "proj", "session": 4,
                      "code": "abc0000000000001", "toolkit_version": "1.4.0"})
    op.show_trace([])
    out = capsys.readouterr().out
    assert "Supervisor starts" in out
    assert "abc0000000000001" in out


def test_a_trace_of_only_supervisor_starts_is_not_reported_as_empty(
        tmp_path, capsys):
    """"Nothing has been traced" and "nothing of the kind you rendered" are
    different findings, and this file already carries one bug of that shape."""
    _trace(tmp_path, {"ts": "2026-08-05T06:00:00Z", "event": "supervisor_start",
                      "instance": "proj", "session": 1, "code": "abc"})
    op.show_trace([])
    assert "No operator invocations have been traced yet" not in \
        capsys.readouterr().out


def test_supervisor_starts_reach_the_json_output(tmp_path, capsys):
    _trace(tmp_path, {"ts": "2026-08-05T06:00:00Z", "event": "supervisor_start",
                      "instance": "proj", "session": 1, "code": "abc"})
    op.show_trace(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["supervisor_starts"][0]["code"] == "abc"
