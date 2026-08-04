"""The invocation trace, and the distinctions it is not allowed to lose.

The trace exists because ``operator.log`` could not say who asked. Every test
here that matters is about a *distinction*: could-not-tell against nothing-
found, a filter that matched nothing against a file that recorded nothing, a
command that refused against one that crashed. Those are the pairs that, when
collapsed, produce a log which reads as healthy while the machine is not --
which is the failure this module was written after.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import copilot_operator as op  # noqa: E402
import operator_trace  # noqa: E402


def chain(*entries: tuple) -> list[dict]:
    """A process chain, nearest parent first, from (name, path) pairs."""
    return [{"pid": 1000 + i, "name": name, "path": path}
            for i, (name, path) in enumerate(entries)]


# ── the tri-state that the whole module turns on ────────────────


def test_an_unreadable_process_tree_is_unknown_and_never_user():
    """The distinction the module exists for.

    `unknown` and `user` are both "no evidence of an agent", and a classifier
    that merged them would report every machine it failed to examine as a
    person at a keyboard -- confidently, and in the ordinary words.
    """
    verdict = operator_trace.classify(None, ["list"], tty=True)
    assert verdict["kind"] == "unknown", (
        "an unreadable process tree was classified as "
        f"{verdict['kind']!r}; a failure to look must not become a finding")
    assert "could not be read" in verdict["why"]


def test_a_tty_does_not_upgrade_an_unreadable_tree():
    """A tty is the strongest hint toward `user`, and it is still not enough.

    Negative control for the test above: if `classify` consulted the tty
    before the tree, the previous test would pass for the wrong reason on any
    non-interactive runner.
    """
    for tty in (True, False, None):
        assert operator_trace.classify(None, [], tty=tty)["kind"] == "unknown"


def test_an_empty_chain_is_not_an_unreadable_one():
    """`[]` means "looked, found no ancestors"; `None` means "could not look".

    They take different branches and must not produce the same verdict. The
    first draft of `classify` returned `unknown` for both, which is the
    module's own defect class reached through its own classifier.
    """
    empty = operator_trace.classify([], ["list"], tty=False)
    unreadable = operator_trace.classify(None, ["list"], tty=False)
    assert empty["kind"] != unreadable["kind"], (
        "an empty ancestry and an unreadable one produced the same verdict "
        f"({empty['kind']!r}); the two states are not the same finding")
    assert empty["kind"] == "orphan"
    assert unreadable["kind"] == "unknown"


def test_a_tty_decides_only_once_the_tree_has_been_read():
    """Why the asymmetry above is principled rather than convenient.

    With an empty-but-readable tree we know there is no Copilot session above
    us, so a tty really does indicate a person. With an unreadable tree we
    know nothing -- an agent's shell has a tty too -- so the same tty must
    not produce the same answer.
    """
    assert operator_trace.classify([], [], tty=True)["kind"] == "user"
    assert operator_trace.classify(None, [], tty=True)["kind"] == "unknown"


def test_ancestry_returns_none_when_the_table_cannot_be_read(monkeypatch):
    """Not `[]`. An empty list is a claim that this process has no parents."""
    monkeypatch.setattr(operator_trace, "IS_WINDOWS", True)
    monkeypatch.setattr(operator_trace, "_win_process_table", lambda: None)
    assert operator_trace.ancestry(1234) is None


def test_ancestry_of_the_live_process_finds_something(monkeypatch):
    """Positive control.

    Without this, every ancestry assertion above would keep passing if the
    walk were broken outright and always returned None.
    """
    got = operator_trace.ancestry()
    assert got is not None, "the real process tree could not be read at all"
    assert got, "the walk produced no ancestors for a process that has some"
    assert all("pid" in entry for entry in got)


# ── attribution ─────────────────────────────────────────────────


def test_a_copilot_ancestor_is_an_agent():
    tree = chain(("pwsh.exe", r"C:\pwsh.exe"),
                 ("copilot.exe", r"C:\copilot.exe"))
    verdict = operator_trace.classify(tree, ["send"], tty=True)
    assert verdict["kind"] == "agent"


def test_an_agent_records_the_session_pid_so_the_log_can_be_joined():
    """`operator.log` already prints "Session #N running (copilot pid=X)".

    Recording X is what turns "an agent did this" into "*that* instance did
    this" without inventing any new state to keep in sync.
    """
    tree = chain(("copilot.exe", r"C:\copilot.exe"))
    verdict = operator_trace.classify(tree, ["send"], tty=False)
    assert verdict["session_pid"] == tree[0]["pid"]


def test_a_copilot_ancestor_wins_over_a_tty():
    """An agent's shell has a tty too, so the tty must not decide first."""
    tree = chain(("pwsh.exe", r"C:\pwsh.exe"),
                 ("copilot.exe", r"C:\copilot.exe"))
    assert operator_trace.classify(tree, [], tty=True)["kind"] == "agent"


def test_the_supervisor_is_recognised_from_its_own_flag():
    tree = chain(("python.exe", r"C:\python.exe"))
    verdict = operator_trace.classify(tree, ["--_supervise", "--loop"],
                                      tty=False)
    assert verdict["kind"] == "supervisor"


def test_a_shell_with_a_tty_is_a_user():
    tree = chain(("bash", "/bin/bash"))
    assert operator_trace.classify(tree, ["list"], tty=True)["kind"] == "user"


def test_a_non_interactive_unknown_launcher_is_external_and_is_named():
    """The launcher that mattered was one no allow-list would have held.

    So `external` names the executable rather than judging it -- the verdict
    a human needs is "something you have not thought about started this",
    plus its path.
    """
    tree = chain(("python.exe",
                  r"C:\Users\x\AppData\Local\hermes\venv\python.exe"))
    verdict = operator_trace.classify(tree, ["--loop"], tty=False)
    assert verdict["kind"] == "external"
    assert "hermes" in verdict["launcher"]


def test_executable_stems_ignore_extension_and_case():
    for spelling in ("COPILOT.EXE", "copilot", "copilot.exe"):
        tree = chain((spelling, None))
        assert operator_trace.classify(tree, [], tty=False)["kind"] == "agent", (
            f"{spelling!r} was not recognised as a copilot session")


def test_a_path_names_the_session_when_the_bare_name_does_not():
    """On Windows the table gives a bare name; the path is what distinguishes.

    Two different launchers were both `python.exe` on the machine this was
    written for, so classification has to be willing to read the path.
    """
    tree = [{"pid": 7, "name": "wrapper", "path": r"C:\x\copilot.exe"}]
    assert operator_trace.classify(tree, [], tty=False)["kind"] == "agent"


# ── what gets written ───────────────────────────────────────────


def test_a_long_message_body_is_replaced_by_its_length(tmp_path):
    body = "s" * 300
    op_home = tmp_path / "home"
    ctx = operator_trace.record_invocation(
        op_home, ["send", "--from", "a", "--to", "b", body])
    assert ctx is not None
    written = operator_trace.trace_path(op_home).read_text(encoding="utf-8")
    assert body not in written, "a message body was written to the trace"
    assert "<redacted:300 chars>" in written


def test_flags_around_a_redacted_body_survive(tmp_path):
    """Redaction must not eat the part that makes the record useful."""
    op_home = tmp_path / "home"
    operator_trace.record_invocation(
        op_home, ["send", "--from", "alpha", "--to", "beta", "x" * 90])
    rec = json.loads(operator_trace.trace_path(op_home)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert rec["argv"][:5] == ["send", "--from", "alpha", "--to", "beta"]


def test_a_short_argument_to_send_is_redacted_but_its_flags_are_not(tmp_path):
    """Negative control for redaction: it must not swallow the flag values
    that make the record useful, while still redacting a short body.

    The original of this test asserted that a short body survived into the
    trace -- it was written to stop redaction eating everything, and instead
    pinned the leak in place. A test can enforce the bug it was meant to
    prevent.
    """
    op_home = tmp_path / "home"
    operator_trace.record_invocation(op_home, ["send", "--to", "beta", "ok"])
    rec = json.loads(operator_trace.trace_path(op_home)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert rec["argv"][:3] == ["send", "--to", "beta"]
    assert "ok" not in rec["argv"]
    assert rec["argv"][-1] == "<redacted:2 chars>"


def test_other_subcommands_are_not_redacted(tmp_path):
    op_home = tmp_path / "home"
    operator_trace.record_invocation(op_home, ["join", "y" * 60])
    rec = json.loads(operator_trace.trace_path(op_home)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert "redacted" not in json.dumps(rec["argv"])


def test_invoke_and_exit_share_a_trace_id_and_record_the_code(tmp_path):
    op_home = tmp_path / "home"
    ctx = operator_trace.record_invocation(op_home, ["list"])
    operator_trace.record_exit(ctx, 3)
    lines = [json.loads(l) for l in operator_trace.trace_path(op_home)
             .read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == ["invoke", "exit"]
    assert lines[0]["trace_id"] == lines[1]["trace_id"]
    assert lines[1]["rc"] == 3


# ── tracing must never break the operator ───────────────────────


def test_a_trace_that_cannot_be_written_returns_none_rather_than_raising(
        tmp_path, monkeypatch):
    """This runs at the top of every invocation, including the supervisor.

    A tracer that raised would take down the unattended loops it exists to
    explain, which would be a strictly worse outcome than no trace at all.
    """
    def refuse(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(operator_trace, "_append", refuse)
    assert operator_trace.record_invocation(tmp_path, ["list"]) is None


def test_a_broken_ancestry_walk_does_not_stop_the_record(tmp_path,
                                                         monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("process table on fire")

    monkeypatch.setattr(operator_trace, "ancestry", explode)
    # The record is lost, but the caller gets None instead of an exception.
    assert operator_trace.record_invocation(tmp_path, ["list"]) is None


def test_record_exit_without_a_context_is_a_no_op():
    operator_trace.record_exit(None, 0)


def test_record_exit_survives_a_broken_context():
    operator_trace.record_exit({"home": None, "trace_id": "x"}, 0)


# ── reading it back ─────────────────────────────────────────────


def test_an_absent_trace_reads_as_empty_but_an_unreadable_one_does_not(
        tmp_path, monkeypatch):
    assert operator_trace.read_records(tmp_path / "nothing-here") == []

    op_home = tmp_path / "home"
    operator_trace.record_invocation(op_home, ["list"])

    def denied(*args, **kwargs):
        raise PermissionError("blocked")

    monkeypatch.setattr(Path, "read_text", denied)
    assert operator_trace.read_records(op_home) is None, (
        "an unreadable trace reported the same empty list as an absent one")


def test_a_torn_line_does_not_end_the_read(tmp_path):
    """Appends race with reads, so the last line can be half-written."""
    op_home = tmp_path / "home"
    operator_trace.record_invocation(op_home, ["list"])
    path = operator_trace.trace_path(op_home)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event": "invoke", "argv"\n')
    records = operator_trace.read_records(op_home)
    assert records is not None and len(records) == 1


def test_rotation_keeps_the_previous_file(tmp_path, monkeypatch):
    monkeypatch.setattr(operator_trace, "_MAX_BYTES", 200)
    op_home = tmp_path / "home"
    for _ in range(12):
        operator_trace.record_invocation(op_home, ["list"])
    assert operator_trace.trace_path(op_home).exists()
    assert Path(str(operator_trace.trace_path(op_home)) + ".1").exists()


# ── the CLI ─────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "operator-home"
    d.mkdir()
    monkeypatch.setattr(op, "OPERATOR_HOME", d)
    return d


def test_main_traces_a_command_and_its_exit_code(home, capsys):
    rc = op.main(["version"])
    capsys.readouterr()
    assert rc == 0
    lines = [json.loads(l) for l in operator_trace.trace_path(home)
             .read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == ["invoke", "exit"]
    assert lines[0]["argv"] == ["version"]
    assert lines[1]["rc"] == 0


def test_a_command_that_leaves_through_die_still_records_its_exit(home,
                                                                 capsys):
    """Most error paths in the operator are `die()`, i.e. SystemExit.

    If the bracket only recorded ordinary returns, the trace would hold an
    `invoke` with no `exit` for exactly the invocations worth investigating,
    and those are indistinguishable from a command still running.
    """
    with pytest.raises(SystemExit):
        op.main(["trace", "--limit"])
    capsys.readouterr()
    lines = [json.loads(l) for l in operator_trace.trace_path(home)
             .read_text(encoding="utf-8").splitlines()]
    assert [l["event"] for l in lines] == ["invoke", "exit"]
    assert lines[1]["rc"] == 1


def test_a_crash_is_distinguishable_from_a_refusal(home, monkeypatch, capsys):
    def explode(_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(op, "show_trace", explode)
    with pytest.raises(RuntimeError):
        op.main(["trace"])
    capsys.readouterr()
    lines = [json.loads(l) for l in operator_trace.trace_path(home)
             .read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["rc"] == -1, (
        "a command that crashed recorded the same code as one that refused")


def test_trace_reports_an_unreadable_file_rather_than_an_empty_one(
        home, monkeypatch, capsys):
    monkeypatch.setattr(operator_trace, "read_records",
                        lambda *a, **k: None)
    rc = op.show_trace([])
    out = capsys.readouterr()
    assert rc == 1
    assert "could not read" in out.err
    assert "No operator invocations" not in out.out


def test_a_filter_matching_nothing_is_not_reported_as_an_empty_trace(
        home, capsys):
    """The same substitution one level up.

    A filter that matched nothing and a file that recorded nothing are
    different findings, and a reader told the wrong one stops looking.
    """
    op.main(["version"])
    capsys.readouterr()
    op.show_trace(["--kind", "external"])
    out = capsys.readouterr().out
    assert "No operator invocations have been traced yet" not in out
    assert "No invocation matched" in out
    assert "kinds present" in out


def test_an_empty_trace_says_so(home, capsys):
    """Negative control for the test above."""
    op.show_trace([])
    assert "No operator invocations have been traced yet" in capsys.readouterr().out


def test_trace_json_output_is_parseable(home, capsys):
    op.main(["version"])
    capsys.readouterr()
    op.show_trace(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["invocations"][0]["argv"] == ["version"]
    # Present even when empty: a reader that has to guess whether the key is
    # missing or the list is empty is back to the substitution this file is
    # about.
    assert payload["session_exits"] == []


def test_trace_is_a_reserved_word_so_it_cannot_be_an_instance_name():
    """`operator foo` attaches to instance foo, so `trace` must be excluded
    or the subcommand would be shadowed by any instance named `trace`."""
    assert "trace" in op.RESERVED_WORDS


# --- The tri-state, tested where it is produced -------------------------
#
# The tests above prove `classify` treats None and [] differently. They cannot
# prove `ancestry` ever produces None, and for a while it did not: on Linux a
# PermissionError reading /proc came back as None from `_posix_parent`, the
# walk broke, and an unreadable tree was returned as an empty chain -- which
# `classify` reads as "no ancestors", one tty away from "a human ran this".
# A guarantee is only protected where the value is made.


class _DeniedPath:
    def __init__(self, *_a, **_k):
        pass

    def read_text(self, *_a, **_k):
        raise PermissionError(13, "Permission denied")

    def read_bytes(self, *_a, **_k):
        raise PermissionError(13, "Permission denied")


class _GonePath:
    def __init__(self, *_a, **_k):
        pass

    def read_text(self, *_a, **_k):
        raise FileNotFoundError(2, "No such file")

    def read_bytes(self, *_a, **_k):
        raise FileNotFoundError(2, "No such file")


def test_posix_parent_separates_denied_from_gone(monkeypatch):
    """Two OSErrors, two opposite meanings. Collapsing them is the bug."""
    monkeypatch.setattr(operator_trace, "Path", _GonePath)
    assert operator_trace._posix_parent(4242) is None

    monkeypatch.setattr(operator_trace, "Path", _DeniedPath)
    with pytest.raises(operator_trace._TreeUnreadable):
        operator_trace._posix_parent(4242)


def _force_procfs(monkeypatch):
    monkeypatch.setattr(operator_trace, "IS_WINDOWS", False)
    monkeypatch.setattr(operator_trace, "_procfs_available", lambda: True)


def test_ancestry_returns_none_when_proc_cannot_be_read(monkeypatch):
    """An unreadable tree must not arrive as an empty one."""
    _force_procfs(monkeypatch)
    monkeypatch.setattr(operator_trace, "Path", _DeniedPath)
    assert operator_trace.ancestry(pid=4242) is None


def test_ancestry_returns_empty_chain_when_the_parent_is_gone(monkeypatch):
    """...and a genuine dead end must not arrive as a failure to look."""
    _force_procfs(monkeypatch)
    monkeypatch.setattr(operator_trace, "Path", _GonePath)
    assert operator_trace.ancestry(pid=4242) == []


def test_ancestry_of_a_denied_tree_never_classifies_as_user(monkeypatch):
    """The end-to-end shape of the bug: denied /proc plus a tty read as a
    human sitting at a terminal, which is exactly the attribution an
    incident needs to be right about."""
    _force_procfs(monkeypatch)
    monkeypatch.setattr(operator_trace, "Path", _DeniedPath)
    chain = operator_trace.ancestry(pid=4242)
    assert operator_trace.classify(chain, [], tty=True)["kind"] == "unknown"


def test_a_readable_but_empty_process_table_is_not_an_unreadable_one(
        monkeypatch):
    """`return table or None` made an empty snapshot indistinguishable from a
    snapshot that could not be taken."""
    monkeypatch.setattr(operator_trace, "IS_WINDOWS", True)
    monkeypatch.setattr(operator_trace, "_win_process_table", lambda: {})
    assert operator_trace.ancestry(pid=4242) == []

    monkeypatch.setattr(operator_trace, "_win_process_table", lambda: None)
    assert operator_trace.ancestry(pid=4242) is None


# --- Redaction is not a length heuristic --------------------------------


def test_a_short_message_body_is_still_redacted():
    """The first version redacted only bodies longer than 40 characters, so
    `operator send ... hunter2` went into the trace verbatim. A secret is not
    less of one for being short."""
    out = operator_trace._safe_argv(
        ["send", "--from", "a", "--to", "b", "hunter2"])
    assert "hunter2" not in out
    assert out[-1] == "<redacted:7 chars>"


def test_flag_values_survive_redaction_because_they_are_the_attribution():
    out = operator_trace._safe_argv(
        ["send", "--from", "copilot-tools", "--to", "prism", "hi"])
    assert "copilot-tools" in out and "prism" in out


def test_a_body_that_looks_like_a_flag_is_not_waved_through():
    """`not text.startswith("-")` let a body beginning with a dash skip
    redaction and fall through to plain truncation."""
    out = operator_trace._safe_argv(["send", "--to", "b", "-----BEGIN KEY-----"])
    assert not any("BEGIN KEY" in part for part in out)


def test_non_redacted_subcommands_are_left_alone():
    assert operator_trace._safe_argv(["version"]) == ["version"]
    assert operator_trace._safe_argv(["join", "prism"]) == ["join", "prism"]


# --- Controls that drive the real reader, not a stand-in ----------------
#
# An earlier version of the three tests below monkeypatched `_win_process_table`
# and `_ps_process_table` themselves, so the `return table or None` they were
# written to catch was never executed. Mutation testing found them: the bug was
# reintroduced and the suite stayed green. A test that replaces the code it is
# meant to guard is scenery.


def test_ps_table_distinguishes_an_empty_listing_from_a_failed_one(
        monkeypatch):
    import subprocess as _sp

    class _Result:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Result(0, ""))
    assert operator_trace._ps_process_table() == {}, (
        "ps exited 0 with no rows: that is an answer, not a failure to look")

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Result(1, ""))
    assert operator_trace._ps_process_table() is None


class _FakeFn:
    """A stand-in for a kernel32 export that still accepts the restype and
    argtypes assignments the module makes before calling it."""

    def __init__(self, result):
        self._result = result
        self.restype = None
        self.argtypes = None

    def __call__(self, *_a, **_k):
        return self._result


class _FakeKernel32:
    def __init__(self, snapshot=1234, first=0):
        self.CreateToolhelp32Snapshot = _FakeFn(snapshot)
        self.Process32FirstW = _FakeFn(first)
        self.Process32NextW = _FakeFn(0)
        self.CloseHandle = _FakeFn(1)


@pytest.mark.skipif(not operator_trace.IS_WINDOWS,
                    reason="ctypes.wintypes is Windows-only")
def test_win_table_distinguishes_an_empty_snapshot_from_a_failed_one(
        monkeypatch):
    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda *a, **k: _FakeKernel32(snapshot=1234, first=0))
    assert operator_trace._win_process_table() == {}, (
        "the snapshot was taken and held no rows: still an answer")

    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda *a, **k: _FakeKernel32(snapshot=0))
    assert operator_trace._win_process_table() is None


@pytest.mark.skipif(not operator_trace.IS_WINDOWS,
                    reason="ctypes.wintypes is Windows-only")
def test_an_invalid_handle_is_recognised_as_failure(monkeypatch):
    """CreateToolhelp32Snapshot reports failure as INVALID_HANDLE_VALUE. With
    ctypes' default 32-bit restype that arrived as -1 and was compared against
    c_void_p(-1).value (2**64-1), so a failed snapshot was walked as a good
    one. The declared restype is what makes this comparison meaningful."""
    import ctypes

    invalid = ctypes.c_void_p(-1).value
    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda *a, **k: _FakeKernel32(snapshot=invalid))
    assert operator_trace._win_process_table() is None


def test_stem_does_not_depend_on_the_hosts_path_separator(monkeypatch):
    r"""`os.path.basename` leaves `C:\x\copilot.exe` whole on POSIX, so the
    ancestry of a Windows-written trace would stop being recognised the moment
    it was read anywhere else. Forcing the POSIX implementation reproduces
    that here, on any host."""
    import posixpath

    monkeypatch.setattr(operator_trace.os.path, "basename",
                        posixpath.basename)
    assert operator_trace._stem(r"C:\x\copilot.exe") == "copilot"
    assert operator_trace._stem("/usr/local/bin/copilot") == "copilot"


# --- Sessions found gone ------------------------------------------------
#
# The event the trace was actually built for. When seven loops died together
# no operator command ran at all, so an invocation log had nothing to show.


def test_a_session_exit_records_the_evidence_not_a_verdict(tmp_path):
    op_home = tmp_path / "home"
    operator_trace.record_session_exit(
        op_home, instance="prism", session=51, pid=34732,
        markers={"stop": False, "detach": False, "restart": False,
                 "exit_code": 0},
        consecutive=5, limit=5)
    rec = json.loads(operator_trace.trace_path(op_home)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert rec["event"] == "session_exit"
    assert rec["instance"] == "prism" and rec["session"] == 51
    assert rec["session_pid"] == 34732
    assert rec["markers"]["exit_code"] == 0
    assert rec["giving_up"] is True


def test_an_unreadable_marker_survives_into_the_record(tmp_path):
    """None is why the supervisor waits instead of relaunching. Flattening it
    to False here would erase the reason from the only place it is kept."""
    op_home = tmp_path / "home"
    operator_trace.record_session_exit(
        op_home, instance="prism", session=1, pid=None,
        markers={"stop": None, "detach": False, "restart": False,
                 "exit_code": None},
        consecutive=1, limit=5)
    rec = json.loads(operator_trace.trace_path(op_home)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert rec["markers"]["stop"] is None
    assert rec["markers"]["exit_code"] is None
    assert rec["session_pid"] is None


def test_recording_a_session_exit_never_raises(tmp_path):
    """This runs inside the supervisor's poll loop. Raising here would take
    down the thing that keeps unattended sessions alive."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    operator_trace.record_session_exit(
        blocked / "nested", instance="x", session=1, pid=1,
        markers={"stop": False}, consecutive=1, limit=5)

    class _Unserialisable:
        pass

    operator_trace.record_session_exit(
        tmp_path / "home2", instance="x", session=1, pid=1,
        markers={"stop": _Unserialisable()}, consecutive=1, limit=5)


def test_a_clean_exit_is_reported_as_clean_not_as_a_crash(home, capsys):
    """`operator.log` can only say "exited unexpectedly", which reads as a
    crash. The runner records the real code, and rc=0 means copilot shut down
    in an orderly way and simply nobody set a marker to say so -- the
    difference between a machine that is failing and a loop that is ending."""
    operator_trace.record_session_exit(
        op.OPERATOR_HOME, instance="prism", session=51, pid=34732,
        markers={"stop": False, "detach": False, "restart": False,
                 "exit_code": 0},
        consecutive=5, limit=5)
    op.show_trace([])
    out = capsys.readouterr().out
    assert "Supervised sessions found gone" in out
    assert "clean exit (rc=0)" in out
    assert "GIVING UP" in out


def test_session_exits_show_even_when_nothing_was_invoked(home, capsys):
    """The whole point: a mass die-off invokes no operator command, so a view
    that only listed invocations would render the incident as an empty file."""
    operator_trace.record_session_exit(
        op.OPERATOR_HOME, instance="prism", session=1, pid=2,
        markers={"stop": False, "exit_code": 1}, consecutive=1, limit=5)
    op.show_trace([])
    out = capsys.readouterr().out
    assert "No operator invocations have been traced yet." not in out
    assert "prism" in out
