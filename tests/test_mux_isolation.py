"""The unit suite must never drive the developer's real terminal multiplexer.

`copilot_operator.MUX` is built at import time, so a test that calls into
copilot_operator without replacing it talks to whatever tmux/psmux server the
machine is running. That was measured, not theorised: 30 unit tests and 134
`tmux has-session` subprocess calls per suite run, which is what made
test_loop_resilience.py::test_unexpected_exit_without_marker_is_relaunched fail
in a full run and pass three times out of three on its own.

conftest's `_no_real_multiplexer` closes that boundary. These tests exist
because a guard nobody checks is indistinguishable from a guard nobody has:
delete the fixture and the suite goes right back to passing, quietly, on a
machine-dependent answer.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import copilot_operator as op
import operator_mux
from conftest import FakeMux, _MUX_BINARIES, _is_a_multiplexer_spawn
from operator_mux import MuxSessionError


@pytest.fixture
def no_subprocess(monkeypatch):
    """Make any process spawn from operator_mux an immediate, loud failure.

    Patching `operator_mux.subprocess.run` reaches the module object shared by
    every importer, so this catches a spawn from anywhere under the call, not
    just from `Mux._run`.
    """
    def poisoned(*args, **kwargs):
        raise AssertionError(f"a real process was spawned: {args[0]!r}")

    monkeypatch.setattr(operator_mux.subprocess, "run", poisoned)


@pytest.fixture
def only_git_subprocess(monkeypatch):
    """Refuse every spawn except git, which is answered without spawning.

    `no_subprocess` above poisons *all* spawning, and for the FakeMux tests
    that is exactly right: they must reach no process at all. The supervisor
    is a different case now. While this branch sat unmerged, `main` grew the
    no-change progress breaker, which fingerprints the repository by running
    read-only git probes from inside `run_loop_mode` -- so "the supervisor
    spawns nothing" stopped being true by design rather than by regression.
    The merge was textually clean and semantically broken, which is the shape
    the backlog item warned about: zero divergence is perishable.

    Blanket poisoning would now fail the test for a legitimate reason, and the
    tempting repair -- delegating anything that is not a multiplexer -- is
    worse than it looks. It would run the real git against the developer's own
    checkout, so a unit test written to end this suite's dependence on machine
    state would acquire a fresh one, and a slow one: a `status
    --untracked-files=all` per worktree. Instead git is answered here with a
    canned non-zero result and no process. `_git_output` maps that to `None`,
    the documented "could not be answered" reading, so the breaker takes its
    unknown path and the loop's behaviour under test is unchanged.

    What the test actually claims survives intact: nothing reached a real
    multiplexer. Anything that is not one of the probes named below still
    raises, so a future spawn cannot be added silently.

    `ps` and `sysctl` joined git for the same reason and by the same
    measurement, and they are macOS's alone. Writing the pid file calls
    `operator_liveness.process_start_token` -- ``ps -p <pid> -o lstart=`` --
    and then `boot_identity`, which falls through to
    ``sysctl -n kern.boottime`` once ``/proc`` has failed to answer. Linux
    reads ``/proc`` for both and Windows asks ctypes, so macOS is the only leg
    that spawns anything here. Nobody works on macOS interactively, so the
    omission was invisible until CI said so, and it failed *both* macOS legs
    on a supervisor behaving exactly as designed. The canned non-zero result
    is right for both: each reads a non-zero return as "the probe could not
    answer", and `_loop_pid_stamp` documents a pid file missing either stamp
    as a supported shape.

    Matched on the whole argument vector rather than the program name.
    ``ps`` and ``sysctl`` are general-purpose tools, and an allowance spelled
    as "any ps" would let an unrelated future spawn through the guard this
    file exists to be -- which is the same mistake as the blanket delegation
    rejected above, one command narrower.
    """
    def answered_without_spawning(parts: "list[str]") -> bool:
        program = os.path.basename(parts[0]).lower() if parts else ""
        if program.endswith(".exe"):
            program = program[:-4]
        if program == "git":
            # The progress breaker's probes, whose arguments vary by design.
            return True
        if program == "ps":
            return parts[1:2] == ["-p"] and parts[3:5] == ["-o", "lstart="]
        if program == "sysctl":
            return parts[1:] == ["-n", "kern.boottime"]
        return False

    spawned: list[list[str]] = []

    def guarded(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        parts = ([str(a) for a in argv] if isinstance(argv, (list, tuple))
                 else [str(argv)])
        spawned.append(parts)
        if not answered_without_spawning(parts):
            raise AssertionError(
                f"a real process was spawned: {parts!r}. Only git, "
                "`ps -p <pid> -o lstart=` and `sysctl -n kern.boottime` are "
                "expected from the supervisor (the progress breaker's "
                "read-only probes, and the macOS start-token and boot-identity "
                "probes); a multiplexer spawn here is the flake this file "
                "exists to prevent."
            )
        return subprocess.CompletedProcess(parts, 1, "", "")

    monkeypatch.setattr(operator_mux.subprocess, "run", guarded)
    return spawned


# ── the guard is installed ──────────────────────────────────────
def test_the_module_level_mux_is_not_a_real_one():
    """If this fails, every test in the suite is talking to a live server."""
    assert isinstance(op.MUX, FakeMux), (
        "copilot_operator.MUX is not the in-memory double -- conftest's "
        "_no_real_multiplexer fixture is missing or was overridden"
    )


def test_the_double_starts_empty():
    """The leaking tests were relying on 'no session of mine exists'. That has
    to keep being the answer, or their assertions change meaning."""
    assert op.MUX.list_sessions() == []
    assert op.MUX.has_session("relaunch-me") is False


def test_the_supervisor_does_not_spawn_a_multiplexer(only_git_subprocess,
                                                     tmp_path, monkeypatch):
    """The regression test for the flake itself.

    The loop crash-relaunches twice and then stops, exactly as
    test_unexpected_exit_without_marker_is_relaunched drives it. Before the
    fix this made five `tmux has-session` calls; with spawning intercepted,
    any one of them fails the test instead of silently depending on the
    machine.

    Git is permitted because `main`'s progress breaker fingerprints the
    repository from inside the loop -- see `only_git_subprocess`. The claim
    asserted at the end is the one that matters and the one that was ever
    true: no multiplexer was reached.
    """
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)

    attempts = {"n": 0}

    def flaky_session(instance, args, session_num, remain_on_exit=False,
                      preamble=""):
        attempts["n"] += 1
        instance.exit_file.write_text("0", encoding="utf-8")
        if attempts["n"] >= 3:
            instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", flaky_session)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)

    rc = op.run_loop_mode(op.Instance("relaunch-me"), ["--agent", "test:agent"],
                          is_fresh=True)

    assert rc == 0
    assert attempts["n"] == 3

    # The claim in the test's name, asserted rather than assumed. Anything
    # that was not git already raised inside the fixture; this catches the
    # remaining way to be wrong -- a multiplexer invoked *through* git -- and,
    # more usefully, it keeps failing if someone relaxes the fixture later.
    def _program(part: str) -> str:
        name = os.path.basename(str(part)).lower()
        return name[:-4] if name.endswith(".exe") else name

    reached = [argv for argv in only_git_subprocess
               if any(_program(part) in _MUX_BINARIES for part in argv)]
    assert reached == [], f"the supervisor reached a multiplexer: {reached!r}"


# ── the double is faithful ──────────────────────────────────────
def test_an_unmodelled_verb_raises_rather_than_inventing_an_answer(no_subprocess):
    """A double that answers a question it does not understand is worse than
    no double: the caller cannot tell the difference from success."""
    with pytest.raises(AssertionError, match="does not model the 'wait-for' verb"):
        FakeMux()._run("wait-for", "-S", "ready")


def test_an_unmodelled_display_format_raises(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    with pytest.raises(AssertionError, match="display format"):
        mux._run("display-message", "-t", "s", "-p", "#{window_name}")


def test_create_query_and_kill_round_trips(no_subprocess, tmp_path):
    """The real `Mux` verbs run unmodified on top of the fake backend."""
    mux = FakeMux()
    mux.new_session("s1", str(tmp_path), ["python", "-c", "pass"])

    assert mux.has_session("s1") is True
    assert mux.list_sessions() == ["s1"]
    assert mux.kill_session("s1") is True
    assert mux.has_session("s1") is False
    assert mux.kill_session("s1") is False, "killing an absent session is not an error"


def test_a_duplicate_session_name_is_refused(no_subprocess, tmp_path):
    """`Mux.new_session` raises on a name already in use. The double must let
    that real check reach its real conclusion."""
    mux = FakeMux()
    mux.new_session("dup", str(tmp_path), ["python"])
    with pytest.raises(MuxSessionError, match="already exists"):
        mux.new_session("dup", str(tmp_path), ["python"])


def test_sending_keys_to_a_missing_session_raises(no_subprocess):
    """The mail delivery path depends on this raising -- a swallowed failure
    files an undelivered message as read. See Mux.send_keys."""
    with pytest.raises(MuxSessionError, match="Failed to send keys"):
        FakeMux().send_keys("nobody", "hello")


def test_a_test_that_builds_its_own_mux_still_cannot_reach_a_real_one():
    """The hole that substituting `op.MUX` alone leaves open.

    test_integration.py builds its own `Mux()` on purpose, so the pattern is
    already in the file anyone copies from. A test that does the same by
    accident would sail straight past the substitution -- and nothing would say
    so, because a real `has-session` for a session that does not exist returns
    the same answer the fake would have given.

    The binary is named explicitly rather than probed: CI runs on machines with
    no multiplexer installed, where `Mux().binary` raises MuxNotFoundError and
    this test would pass for a reason that has nothing to do with the guard.
    """
    with pytest.raises(AssertionError, match="real terminal multiplexer"):
        operator_mux.Mux(binary="tmux").has_session("nobody")


def test_the_refusal_names_the_test_and_the_argv():
    """A refusal that does not say who did it or what they ran is a puzzle
    rather than a repair instruction."""
    with pytest.raises(AssertionError) as caught:
        operator_mux.Mux(binary="tmux")._run("kill-server")
    message = str(caught.value)
    assert "test_the_refusal_names_the_test_and_the_argv" in message
    assert "kill-server" in message


@pytest.mark.parametrize("binary", ["tmux", "psmux", "pmux", "tmux.exe", "PSMUX.EXE",
                                    r"C:\tools\tmux.exe", "/usr/bin/tmux"])
def test_every_multiplexer_spelling_is_refused(binary):
    """The guard matches on the program name, so it has to survive a full path
    and a .exe suffix. A detector with a too-narrow match reports a clean tree
    and a clean tree the same way."""
    with pytest.raises(AssertionError, match="real terminal multiplexer"):
        subprocess.run([binary, "-V"], capture_output=True)


def test_the_spawn_predicate_depends_on_its_argument():
    """The predicate must actually discriminate, not return a constant.

    It shipped as ``return name in _MUX_BINARIES and False``: a debug stub
    left in by a session that was killed before the verification its own
    commit message promised. Pinned to ``False`` the guard is not weakened but
    absent, and the three refusal tests above go red -- while every other test
    in the suite quietly regains access to the real server, which is the half
    nobody would have read from a failure list.

    The end-to-end refusals above would catch it too. This names it directly,
    because the two halves of a predicate that is wired to a constant are one
    edit apart and the diagnosis should not require reading a traceback about
    tmux.
    """
    assert _is_a_multiplexer_spawn(["tmux", "kill-server"]) is True
    assert _is_a_multiplexer_spawn([sys.executable, "-c", "pass"]) is False


def test_the_predicate_reads_both_separators_on_every_platform():
    """The program name must be extracted the same way whichever platform's
    path syntax the argv uses, on whichever platform is running.

    ``os.path`` is the *running* platform's path syntax, and this guard is
    asked about argv naming the other one. ``os.path.basename`` on POSIX
    returns ``C:\\tools\\tmux.exe`` unchanged -- backslash is an ordinary
    filename character there -- so the membership test saw ``c:\\tools\\tmux``,
    missed, and the guard delegated a tmux invocation to the real binary.

    ``C:tmux.exe`` is here because the first fix for that was a hand-rolled
    split on both separators, which read a drive-*relative* path (legal on
    Windows: it resolves against the current directory of drive C:) as the name
    ``c:tmux`` and delegated it. ``os.path.basename`` had handled that spelling
    correctly on Windows all along, so the hand-rolled fix closed the POSIX
    hole by opening a Windows one, and only an adversarial review caught it.
    A guard fix that moves the hole is worth a case of its own.

    Be honest about what the Windows-path rows can do: they cannot fail on
    Windows, where ``basename`` already splits both separators and the original
    bug is unreachable. Only the four POSIX legs can falsify those. That is the
    whole lesson of the defect -- a green local Windows suite was not evidence
    about the other platforms, and the parametrised full-path case above passed
    here while spawning a real tmux on every Linux and macOS runner. The
    ``C:tmux.exe`` row is the exception, and fails everywhere when the drive
    prefix is mishandled.
    """
    refused = [
        "tmux", "tmux.exe",                     # bare names
        "./tmux", ".\\tmux.exe",                # relative, either syntax
        "/usr/bin/tmux", "C:\\tools\\tmux.exe",  # absolute, either syntax
        "C:/tools/tmux.exe",                    # Windows drive, POSIX separator
        "C:tmux.exe",                           # drive-relative
        "\\\\server\\share\\tmux.exe",           # UNC
        "PSMUX.EXE", "/opt/bin/pmux",
    ]
    for argv0 in refused:
        assert _is_a_multiplexer_spawn([argv0]) is True, \
            f"the guard would delegate {argv0!r} to a real multiplexer"

    # Negative controls on the identical spellings. Without these, a reading
    # that answered True for everything would satisfy every line above -- and
    # a guard that refuses the whole suite proves nothing by refusing tmux.
    delegated = [
        "python", "python.exe", "./python", "/usr/bin/python",
        "C:\\tools\\python.exe", "C:python.exe", "\\\\server\\share\\python.exe",
        "tmuxinator",  # a real program whose name merely starts with one of ours
    ]
    for argv0 in delegated:
        assert _is_a_multiplexer_spawn([argv0]) is False, \
            f"the guard would refuse {argv0!r}, which is not a multiplexer"


def test_a_non_multiplexer_subprocess_is_delegated_untouched():
    """Control. The guard must be a filter, not a blanket ban: the suite runs
    real Python child processes and several tests depend on it. If this ever
    fails the same way the ones above pass, the guard is refusing everything
    and its refusals prove nothing."""
    proc = subprocess.run([sys.executable, "-c", "print('ok')"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"


def test_the_guard_does_not_nest_across_tests():
    """A leaked guard is invisible from inside the test that leaked it, but not
    from the next one: the fixture would wrap the PREVIOUS test's wrapper
    instead of the real `subprocess.run`. So the honest thing to inspect is
    what this test's guard captured, not whether one is installed."""
    guard = operator_mux.subprocess.run
    assert guard.__qualname__.endswith("guarded_run"), \
        "the multiplexer guard is not installed for this test"
    captured = dict(zip(guard.__code__.co_freevars,
                        (cell.cell_contents for cell in guard.__closure__)))
    delegate = captured["real_run"]
    assert not str(getattr(delegate, "__qualname__", "")).endswith("guarded_run"), \
        "an earlier test's guard was never removed -- the fixture's finally leaked"


def test_sending_keys_records_the_text_and_the_enter(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.send_keys("s", "hello")
    assert mux.keys == [("s", "hello"), ("s", "Enter")]


def test_a_non_literal_send_records_the_text_and_not_just_the_enter(no_subprocess,
                                                                    tmp_path):
    """`send_keys(literal=False, enter=True)` puts the text and `Enter` in ONE
    backend call. Reading only the last argument would record the submit and
    silently drop what was typed -- and the caller could not tell, because a
    send that delivered nothing looks exactly like one that delivered."""
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.send_keys("s", "C-c", literal=False)
    assert mux.keys == [("s", "C-c"), ("s", "Enter")]


def test_a_non_literal_send_without_enter_records_only_the_key(no_subprocess,
                                                               tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.send_keys("s", "C-c", literal=False, enter=False)
    assert mux.keys == [("s", "C-c")]


def test_a_literal_send_without_enter_does_not_submit(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.send_keys("s", "half a line", enter=False)
    assert mux.keys == [("s", "half a line")]


def test_literal_text_that_looks_like_a_key_name_is_recorded_as_text(no_subprocess,
                                                                     tmp_path):
    """The reason `-l` exists: without it the backend reads `Enter` inside a
    message as a submit. The double must keep the two distinguishable."""
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.send_keys("s", "press Enter to continue")
    assert mux.keys == [("s", "press Enter to continue"), ("s", "Enter")]


def test_pane_dead_reflects_the_modelled_state(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    assert mux.pane_dead("s") is False
    mux.sessions["s"]["dead"] = True
    assert mux.pane_dead("s") is True


def test_remain_on_exit_is_recorded(no_subprocess, tmp_path):
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), ["python"])
    mux.set_remain_on_exit("s", True)
    assert mux.sessions["s"]["remain_on_exit"] is True


def test_the_session_argv_and_cwd_survive_the_verb_encoding(no_subprocess, tmp_path):
    """`new_session` passes argv after `--`; the fake has to slice it back out
    at the same offset or every assertion about launch arguments is fiction."""
    argv = ["python", "-c", "print('a b')", "--flag=with space"]
    mux = FakeMux()
    mux.new_session("s", str(tmp_path), argv)
    assert mux.sessions["s"]["argv"] == argv
    assert mux.sessions["s"]["cwd"] == str(tmp_path)


def test_preseeded_sessions_are_visible(no_subprocess):
    mux = FakeMux(sessions=("a", "b"))
    assert sorted(mux.list_sessions()) == ["a", "b"]
    assert mux.has_session("a") is True


def test_the_double_never_looks_for_a_binary(monkeypatch):
    """`Mux.binary` probes PATH with shutil.which and raises when nothing is
    installed. The double must not depend on the machine for that either."""
    def poisoned(_name):
        raise AssertionError("the double probed PATH for a multiplexer")

    monkeypatch.setattr(operator_mux.shutil, "which", poisoned)
    assert FakeMux().binary == "fakemux"
    assert FakeMux().available() is True


def test_subprocess_is_still_reachable_outside_the_poison_fixture():
    """Control: the poison above is a fixture, not a permanent change. Without
    it a spawn works, so the tests that pass are passing on the fake rather
    than on a globally broken subprocess module."""
    # An identity check here -- `subprocess.run is operator_mux.subprocess.run`
    # -- would assert nothing: they are two names for one module attribute, so
    # it holds however broken the state is, including with the poison still
    # installed. What distinguishes this moment is which of the two layers is
    # active, so assert that instead: conftest's guard still refuses a
    # multiplexer, while the per-test poison that refused *everything* is gone.
    with pytest.raises(AssertionError, match="real terminal multiplexer"):
        subprocess.run(["tmux", "-V"], capture_output=True)
    proc = subprocess.run([sys.executable, "-c", "print('ok')"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"
