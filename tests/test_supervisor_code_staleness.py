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
import json
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


def _unreadable(monkeypatch, target: Path):
    """Make `open()` raise EACCES for one path, as a revoked file does.

    `conftest.denied` patches `os.stat`/`os.lstat`, which a digest that opens
    the file never reaches -- so it would deny nothing here and the test would
    pass without exercising the branch it names.
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
def test_no_record_at_all_is_unknown():
    """A supervisor started before this existed has recorded nothing. That
    must not read as either running current code or being stale."""
    inst = op.Instance("norecord")
    assert op.loop_code_state(inst) == (op.CODE_UNKNOWN, [])


def test_an_unparseable_record_is_unknown():
    inst = op.Instance("garbage")
    inst.loop_code_file.write_text("{not json", encoding="utf-8")
    assert op.loop_code_state(inst)[0] == op.CODE_UNKNOWN


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
    """Two different unreadable files must not produce equal fingerprints by
    both collapsing to the same placeholder digest."""
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_bytes(b"aaa\n")
    b.write_bytes(b"bbb\n")

    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [a])
    first = op.running_code_fingerprint()["digest"]
    monkeypatch.setattr(op, "_RUNNING_CODE", None)
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda: [b])
    second = op.running_code_fingerprint()["digest"]

    assert first != second


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


# ── what an operator sees ───────────────────────────────────────
def _snap(**over):
    snap = {"name": "proj", "loop_pid": 123, "session_live": True,
            "owned": True, "session_num": 3, "run_started": "", "cwd": "",
            "loop_code": op.CODE_CURRENT}
    snap.update(over)
    return snap


def test_a_stale_supervisor_is_called_out():
    assert "older code" in op._instance_summary(_snap(loop_code=op.CODE_STALE))


def test_a_current_supervisor_is_not_called_out():
    assert "older code" not in op._instance_summary(_snap())


def test_an_unknown_verdict_is_not_reported_as_stale():
    """`unknown` is most often a supervisor that predates this feature.
    Telling those users their code is stale would make the notice noise."""
    assert "older code" not in op._instance_summary(
        _snap(loop_code=op.CODE_UNKNOWN))


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


def test_the_listing_says_nothing_when_every_supervisor_is_current(
        monkeypatch, capsys):
    """The negative control for the notice: a message that cannot be absent
    carries no information when it appears."""
    monkeypatch.setattr(op, "active_instances", lambda: [op.Instance("alpha")])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: _snap(name="alpha"))

    op.list_instances()

    assert "restart-loop" not in capsys.readouterr().out


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
