"""Tests for the in-pane session supervisor."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

import operator_runner
from conftest import make_log


# ── log attribution ─────────────────────────────────────────────
def test_find_log_matches_exact_pid(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    mine = logs / "process-1700000000000-4242.log"
    mine.write_text("x", encoding="utf-8")
    (logs / "process-1700000000000-9999.log").write_text("x", encoding="utf-8")
    assert operator_runner._find_log(logs, 4242, 1700000000000) == mine


def test_find_log_never_falls_back_to_newest(tmp_path):
    """The bash version grabbed the newest log on a PID miss, which let one
    instance record another's usage. A miss must return None."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "process-1700000000000-1111.log").write_text("x", encoding="utf-8")
    assert operator_runner._find_log(logs, 4242, 1700000000000) is None


def test_find_log_ignores_older_launches(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "process-1000-4242.log").write_text("old", encoding="utf-8")
    current = logs / "process-1700000000000-4242.log"
    current.write_text("new", encoding="utf-8")
    assert operator_runner._find_log(logs, 4242, 1699999999999) == current


def test_find_log_ignores_logs_far_after_launch(tmp_path):
    """PID reuse: a much later Copilot run that happened to get the same PID
    must not be attributed to this session."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "process-1800000000000-4242.log").write_text("much later", encoding="utf-8")
    assert operator_runner._find_log(logs, {4242}, 1700000000000) is None


def test_find_log_prefers_the_launch_closest_in_time(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    near = logs / "process-1700000001000-4242.log"
    near.write_text("near", encoding="utf-8")
    (logs / "process-1700000500000-4242.log").write_text("far", encoding="utf-8")
    assert operator_runner._find_log(logs, {4242}, 1700000000000) == near


def test_find_log_handles_missing_directory(tmp_path):
    assert operator_runner._find_log(tmp_path / "nope", 1, 0) is None


# ── session id extraction ───────────────────────────────────────
def test_extract_session_id_from_json_field(tmp_path):
    log = tmp_path / "a.log"
    log.write_text(
        'noise\n{"session_id": "3f2a9c1e-1111-2222-3333-444455556666"}\n',
        encoding="utf-8",
    )
    assert operator_runner._extract_session_id(log) == \
        "3f2a9c1e-1111-2222-3333-444455556666"


def test_extract_session_id_from_workspace_line(tmp_path):
    log = tmp_path / "b.log"
    log.write_text(
        "Workspace initialized: aaaabbbb-cccc-dddd-eeee-ffff00001111\n",
        encoding="utf-8",
    )
    assert operator_runner._extract_session_id(log) == \
        "aaaabbbb-cccc-dddd-eeee-ffff00001111"


def test_extract_session_id_absent(tmp_path):
    log = tmp_path / "c.log"
    log.write_text("nothing to see\n", encoding="utf-8")
    assert operator_runner._extract_session_id(log) is None


# ── end-to-end supervision ──────────────────────────────────────
def test_runner_records_pid_and_exit_code(tmp_path, state_dir, db_path, launch_spec):
    spec = launch_spec([sys.executable, "-c", "import sys; sys.exit(7)"])
    rc = operator_runner.run(spec)
    assert rc == 7
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "7"
    # The pid file is transient and removed once the child exits.
    assert not (state_dir / "testinst.pid").exists()


def test_runner_clears_stale_exit_marker(tmp_path, state_dir, db_path, launch_spec):
    (state_dir / "testinst.exit").write_text("99", encoding="utf-8")
    spec = launch_spec([sys.executable, "-c", "pass"])
    operator_runner.run(spec)
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "0"


def test_runner_reports_missing_executable(tmp_path, state_dir, launch_spec):
    spec = launch_spec(["definitely-not-a-real-binary-xyz"])
    assert operator_runner.run(spec) == 127
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "127"


def test_runner_captures_metrics_for_its_own_pid(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """The whole point of the runner: metrics are attributed to the exact
    process it launched, and captured even though the operator has gone."""
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "pass"], session_num=5, log_dir=logs)

    real_popen = operator_runner.subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            make_log(logs / f"process-{int(time.time() * 1000)}-{self.pid}.log")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)

    assert operator_runner.run(spec) == 0

    import operator_ingest
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT session_num, no_op FROM sessions").fetchone()
    assert row is not None, "runner must record metrics after the child exits"
    assert row["session_num"] == 5
    assert row["no_op"] == 0


def test_runner_writes_no_metrics_when_log_absent(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """No guessing: absent log means no record, never another instance's."""
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "pass"], log_dir=logs)
    make_log(logs / "process-1700000000000-999999.log")
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)
    operator_runner.run(spec)

    import operator_ingest
    operator_ingest.init_db(db_path)
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


# ── the exit marker is published before metrics capture ─────────
#
# The marker is both the signal that ends the supervisor's poll and the only
# durable record of *how* Copilot ended. It used to be written after the
# metrics capture, which measured 7s to 13.3 hours on this machine, so a dead
# session went unrelaunched for an average of 95 minutes and anything that
# killed the runner in that window destroyed the exit code -- 3 of 1042
# recorded endings ever carried one. These pin the ordering rather than the
# wall-clock, because the wall-clock is a property of the log being parsed.
def test_the_exit_marker_is_on_disk_before_metrics_capture_starts(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """The supervisor must be able to relaunch without waiting for a log parse.

    Asserted at the moment `ingest_file` is entered, which is the earliest
    observable point inside the capture. Restoring the old ordering leaves no
    file to read here at all.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "import sys; sys.exit(3)"],
                       log_dir=logs)

    real_popen = operator_runner.subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            make_log(logs / f"process-{int(time.time() * 1000)}-{self.pid}.log")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)

    import operator_ingest
    seen: dict[str, object] = {}
    real_ingest = operator_ingest.ingest_file

    def watching_ingest(logfile, db, **kwargs):
        marker = state_dir / "testinst.exit"
        seen["existed"] = marker.exists()
        seen["contents"] = (marker.read_text(encoding="utf-8").strip()
                            if marker.exists() else None)
        return real_ingest(logfile, db, **kwargs)

    monkeypatch.setattr(operator_ingest, "ingest_file", watching_ingest)

    assert operator_runner.run(spec) == 3
    assert seen.get("existed") is True, (
        "metrics capture began while the exit marker was still unwritten: the "
        "supervisor cannot see the session has ended until the parse finishes"
    )
    assert seen["contents"] == "3", (
        "the marker was present during capture but did not carry the code "
        "copilot actually exited with"
    )


def test_the_exit_code_survives_a_runner_killed_during_metrics_capture(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """A runner destroyed mid-capture must still leave the code behind.

    This is the case the whole change exists for: the exit code separates
    "copilot crashed on its own" from "something took the whole pane", and it
    is unrecoverable once the process is gone. `BaseException` rather than
    `Exception` deliberately -- `run` catches the latter around the capture,
    so an `Exception` would prove nothing about what a *kill* leaves behind.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "import sys; sys.exit(5)"],
                       log_dir=logs)

    real_popen = operator_runner.subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            make_log(logs / f"process-{int(time.time() * 1000)}-{self.pid}.log")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)

    import operator_ingest

    def killed(*a, **k):
        raise KeyboardInterrupt("pane destroyed mid-capture")

    monkeypatch.setattr(operator_ingest, "ingest_file", killed)

    with pytest.raises(KeyboardInterrupt):
        operator_runner.run(spec)

    marker = state_dir / "testinst.exit"
    assert marker.exists(), (
        "the runner was killed during metrics capture and left no exit code, "
        "so a crash and an external kill are indistinguishable afterwards"
    )
    assert marker.read_text(encoding="utf-8").strip() == "5"


def test_metrics_are_still_captured_when_nothing_interrupts_the_runner(
    tmp_path, state_dir, db_path, launch_spec, monkeypatch
):
    """Publishing the marker early must not skip the capture altogether.

    Without this, the two tests above are satisfied by a runner that never
    ingests anything at all.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    spec = launch_spec([sys.executable, "-c", "pass"], session_num=11,
                       log_dir=logs)

    real_popen = operator_runner.subprocess.Popen

    class SpyPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            make_log(logs / f"process-{int(time.time() * 1000)}-{self.pid}.log")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", SpyPopen)
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)

    assert operator_runner.run(spec) == 0

    import operator_ingest
    operator_ingest.init_db(db_path)
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT session_num FROM sessions").fetchone()
    assert row is not None and row["session_num"] == 11, (
        "the exit marker moved ahead of the capture and took the capture with it"
    )


# ── launch spec validation ──────────────────────────────────────
def _write_spec(path: Path, spec) -> Path:
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _valid_spec(tmp_path: Path, state_dir: Path, db_path: Path) -> dict:
    return {
        "instance": "testinst",
        "argv": [sys.executable, "-c", "pass"],
        "cwd": str(tmp_path),
        "session_num": 1,
        "state_dir": str(state_dir),
        "metrics_db": str(db_path),
        "copilot_log_dir": str(tmp_path / "logs"),
    }


def test_bad_spec_exit_code_is_distinct_from_a_session_failure():
    """The parent must be able to tell 'launched wrong' from 'session failed'.

    0, 126 and 127 already mean something else in this module.
    """
    assert operator_runner.EXIT_BAD_SPEC not in (0, 126, 127)


def test_missing_spec_file_is_reported_not_raised(tmp_path, capsys):
    """A vanished spec used to exit with a bare FileNotFoundError traceback."""
    spec = tmp_path / "testinst.launch.json"
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    err = capsys.readouterr().err
    assert str(spec) in err


def test_unreadable_spec_still_writes_an_exit_marker(tmp_path):
    """The supervisor dying silently is the worst case: the loop polls for
    `{id}.exit` and would otherwise never learn why the pane went away. The
    spec path alone names both the state dir and the instance."""
    spec = tmp_path / "testinst.launch.json"
    operator_runner.run(spec)
    marker = tmp_path / "testinst.exit"
    assert marker.read_text(encoding="utf-8").strip() == str(operator_runner.EXIT_BAD_SPEC)


def test_truncated_spec_is_reported(tmp_path, capsys):
    """A spec caught mid-write by a crash is the realistic corruption."""
    spec = tmp_path / "testinst.launch.json"
    spec.write_text('{"instance": "testinst", "argv": [', encoding="utf-8")
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (tmp_path / "testinst.exit").exists()
    assert "not valid JSON" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [[], "a string", 12, None])
def test_spec_must_be_a_json_object(tmp_path, payload):
    spec = _write_spec(tmp_path / "testinst.launch.json", payload)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC


@pytest.mark.parametrize("key", [
    "instance", "argv", "cwd", "state_dir", "copilot_log_dir", "metrics_db",
])
def test_missing_required_key_names_the_key(tmp_path, state_dir, db_path, key, capsys):
    body = _valid_spec(tmp_path, state_dir, db_path)
    del body[key]
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    err = capsys.readouterr().err
    assert key in err, f"diagnostic must name the offending key, got: {err}"
    assert str(spec) in err, "diagnostic must name the offending file"


def test_string_argv_is_rejected_rather_than_split_per_character(
    tmp_path, state_dir, db_path, monkeypatch
):
    """`list("--loop")` is a valid expression and a catastrophic launch: it
    would spawn a process named '-' with each remaining character as its own
    argument. Nothing may be spawned."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["argv"] = "--loop --name a"
    spec = _write_spec(tmp_path / "testinst.launch.json", body)

    def explode(*args, **kwargs):
        raise AssertionError("a malformed spec must never spawn a process")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", explode)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC


def test_empty_argv_is_rejected(tmp_path, state_dir, db_path):
    """Popen([]) raises IndexError, which the FileNotFoundError/OSError
    handlers around the spawn do not catch."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["argv"] = []
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC


@pytest.mark.parametrize("argv", [[None], ["ok", 5], [["nested"]]])
def test_argv_entries_must_be_strings(tmp_path, state_dir, db_path, argv):
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["argv"] = argv
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC


@pytest.mark.parametrize("value", ["", "   ", None, 5, []])
def test_string_keys_must_be_non_empty_strings(tmp_path, state_dir, db_path, value):
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["instance"] = value
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC


@pytest.mark.parametrize("value", ["3", 1.5, None, True])
def test_session_num_must_be_an_integer(tmp_path, state_dir, db_path, value):
    """`int(spec['session_num'])` raised ValueError/TypeError before any
    marker was written. `True` is rejected too: bool would silently become 1."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["session_num"] = value
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC


def test_marker_lands_where_the_parent_is_watching(tmp_path, state_dir, db_path):
    """The operator writes the spec to `{state_dir}/{instance}.launch.json`
    and polls `{state_dir}/{instance}.exit`, so the spec's own path is the one
    location known to be watched. A spec that already failed validation must
    not be able to redirect the report somewhere nobody looks."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    del body["argv"]
    diverted = tmp_path / "diverted"
    body["state_dir"] = str(diverted)
    spec = _write_spec(state_dir / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() \
        == str(operator_runner.EXIT_BAD_SPEC)
    assert not diverted.exists(), "a failed spec must not steer the marker"


def test_bad_spec_is_recorded_in_the_runner_log(tmp_path, state_dir, db_path):
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["argv"] = []
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    operator_runner.run(spec)
    log = (tmp_path / "testinst.runner.log").read_text(encoding="utf-8")
    assert "invalid launch spec" in log
    assert str(spec) in log


@pytest.mark.parametrize("key", [
    "instance", "cwd", "state_dir", "copilot_log_dir", "metrics_db",
])
def test_embedded_nul_in_a_string_key_is_rejected(
    tmp_path, state_dir, db_path, key, monkeypatch
):
    """A NUL survives JSON but makes filesystem calls and `Popen` raise
    ValueError, which is not an OSError -- so it escaped every guard and
    crashed after validation had already "passed". Found by adversarial
    review."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    body[key] = f"bad\x00{key}"

    def explode(*args, **kwargs):
        raise AssertionError("a spec with a NUL must never spawn a process")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", explode)
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (tmp_path / "testinst.exit").exists()


def test_embedded_nul_in_argv_is_rejected(tmp_path, state_dir, db_path, monkeypatch):
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["argv"] = ["python", "arg\x00two"]

    def explode(*args, **kwargs):
        raise AssertionError("a spec with a NUL must never spawn a process")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", explode)
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (tmp_path / "testinst.exit").exists()


def test_reporter_never_raises_even_when_it_cannot_write(tmp_path, monkeypatch, capsys):
    """`_report_bad_spec` is the last-resort reporter. If it throws, it
    restores the exact bare-traceback failure it exists to prevent."""
    spec = tmp_path / "testinst.launch.json"
    spec.write_text("{", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise ValueError("embedded null character in path")

    monkeypatch.setattr(operator_runner.Path, "mkdir", refuse)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert "invalid launch spec" in capsys.readouterr().err


def test_spawn_value_error_is_reported_with_a_marker(
    tmp_path, state_dir, db_path, monkeypatch
):
    """Defense in depth: ValueError is not caught by the FileNotFoundError or
    OSError handlers around the spawn."""
    def boom(*args, **kwargs):
        raise ValueError("embedded null character")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", boom)
    spec = _write_spec(tmp_path / "testinst.launch.json",
                       _valid_spec(tmp_path, state_dir, db_path))
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() \
        == str(operator_runner.EXIT_BAD_SPEC)


def test_bad_spec_creates_a_missing_state_dir(tmp_path, db_path):
    """The failure has to be observable even when the directory does not
    exist yet."""
    missing = tmp_path / "no-such-dir"
    spec = missing / "testinst.launch.json"
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (missing / "testinst.exit").exists()


def test_main_propagates_the_bad_spec_code(tmp_path):
    """The CLI entry point is what the mux actually runs."""
    spec = tmp_path / "testinst.launch.json"
    assert operator_runner.main([str(spec)]) == operator_runner.EXIT_BAD_SPEC


def test_valid_spec_still_runs(tmp_path, state_dir, db_path, monkeypatch):
    """Validation must not reject what the operator actually writes."""
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)
    spec = _write_spec(tmp_path / "testinst.launch.json",
                       _valid_spec(tmp_path, state_dir, db_path))
    assert operator_runner.run(spec) == 0
    assert (state_dir / "testinst.exit").read_text(encoding="utf-8").strip() == "0"


def test_binary_spec_is_reported_not_raised(tmp_path):
    """UnicodeDecodeError is not an OSError, so reading binary garbage escaped
    the read guard and produced exactly the bare traceback this fix exists to
    remove. Found by adversarial review."""
    spec = tmp_path / "testinst.launch.json"
    spec.write_bytes(b"\xff\xfe\xff")
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (tmp_path / "testinst.exit").read_text(encoding="utf-8").strip() \
        == str(operator_runner.EXIT_BAD_SPEC)


@pytest.mark.parametrize("instance", [
    "..", ".", "../escaped", "..\\escaped", "sub/inst", "/abs", "a\x00b",
])
def test_instance_may_not_escape_the_state_directory(
    tmp_path, state_dir, db_path, instance, monkeypatch
):
    """`instance` is interpolated into `{instance}.exit` / `.pid` / `.log`, so
    a traversal value would write outside the directory the parent watches.
    Found by adversarial review."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["instance"] = instance

    def explode(*args, **kwargs):
        raise AssertionError("an unsafe instance name must never spawn a process")

    monkeypatch.setattr(operator_runner.subprocess, "Popen", explode)
    spec = _write_spec(tmp_path / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    escaped = list(tmp_path.parent.glob("escaped.*"))
    assert not escaped, f"wrote outside the state dir: {escaped}"


def test_unsafe_instance_in_a_bad_spec_does_not_steer_the_marker(
    tmp_path, state_dir, db_path
):
    """The reporting path runs on a spec that failed validation, so nothing
    it says about its own identity is trusted."""
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["instance"] = "..\\escaped"
    del body["argv"]
    spec = _write_spec(state_dir / "testinst.launch.json", body)
    assert operator_runner.run(spec) == operator_runner.EXIT_BAD_SPEC
    assert (state_dir / "testinst.exit").exists(), \
        "must fall back to the path-derived instance name"
    assert not list(state_dir.parent.glob("escaped.*"))


@pytest.mark.parametrize("instance", ["a,b", "copilot-tools", "a.b", "a-b_c"])
def test_real_instance_names_are_accepted(tmp_path, state_dir, db_path,
                                          instance, monkeypatch):
    """Guard against over-tightening: these are live instance ids on this
    machine, and `a,b` in particular is a real one."""
    monkeypatch.setattr(operator_runner.time, "sleep", lambda s: None)
    body = _valid_spec(tmp_path, state_dir, db_path)
    body["instance"] = instance
    spec = _write_spec(tmp_path / f"{instance}.launch.json", body)
    assert operator_runner.run(spec) == 0
    assert (state_dir / f"{instance}.exit").exists()
