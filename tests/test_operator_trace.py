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


def test_a_short_argument_to_send_is_not_redacted(tmp_path):
    """Negative control for redaction: it must not swallow ordinary values.

    Without this, a redactor that replaced every argument would pass the two
    tests above.
    """
    op_home = tmp_path / "home"
    operator_trace.record_invocation(op_home, ["send", "--to", "beta", "ok"])
    rec = json.loads(operator_trace.trace_path(op_home)
                     .read_text(encoding="utf-8").splitlines()[0])
    assert "ok" in rec["argv"]


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
    assert payload and payload[0]["argv"] == ["version"]


def test_trace_is_a_reserved_word_so_it_cannot_be_an_instance_name():
    """`operator foo` attaches to instance foo, so `trace` must be excluded
    or the subcommand would be shadowed by any instance named `trace`."""
    assert "trace" in op.RESERVED_WORDS
