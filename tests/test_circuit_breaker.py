"""The progress circuit breaker: stop a loop that is not changing anything.

An unattended loop restarts a session every time the agent writes a handoff.
Nothing in that mechanism asks whether the session accomplished anything, so
an agent that has run out of real work will keep handing off to itself
indefinitely, burning credits to produce handoffs about handoffs.

The breaker counts consecutive sessions that left the repository's git state
untouched and stops the supervisor at MAX_NOCHANGE_SESSIONS.

Two properties matter more than the counting, and most of this file is about
them:

* "Could not tell" must never be recorded as "changed nothing". A probe that
  failed has established nothing about the session, and folding it into the
  counter would eventually stop a loop that was working.
* The measurement covers the whole repository, not the current directory.
  Work here happens on a branch in a linked worktree, so a session can land a
  whole feature without the primary checkout's HEAD or `git status` moving.
"""
from __future__ import annotations

import subprocess

import pytest

import copilot_operator as op
from conftest import denied


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
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)
    monkeypatch.setattr(op, "SESSION_ID_WAIT", 1)
    return tmp_path


# ── helpers ─────────────────────────────────────────────────────
def _git(cwd, *args) -> str:
    """Run git for real, failing the test loudly if it did not work."""
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
         *args],
        cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def make_repo(path):
    """A real git repository with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")
    return path


# ── the decision function ───────────────────────────────────────
def test_an_identical_fingerprint_advances_the_counter():
    assert op.evaluate_progress(0, "abc", "abc") == (1, "unchanged")
    assert op.evaluate_progress(2, "abc", "abc") == (3, "unchanged")


def test_a_different_fingerprint_clears_the_counter():
    """One productive session must wipe out the whole streak, not decrement
    it: three no-change sessions either side of real work are not evidence
    that the loop has stalled."""
    assert op.evaluate_progress(2, "abc", "xyz") == (0, "changed")


@pytest.mark.parametrize("before,after", [
    (None, "abc"),      # could not measure the start
    ("abc", None),      # could not measure the end
    (None, None),       # could not measure either
])
def test_an_unmeasurable_session_leaves_the_counter_untouched(before, after):
    """The failure this guards against is a loop that stops because git was
    briefly unavailable. `unknown` is not `unchanged`."""
    assert op.evaluate_progress(2, before, after) == (2, "unknown")


def test_an_unreadable_counter_cannot_be_advanced():
    """If the count itself is unknown, no verdict can be reached about it --
    reading it as 0 would silently disarm the breaker, reading it as high
    would stop a healthy loop."""
    assert op.evaluate_progress(None, "abc", "abc") == (None, "unknown")
    # Same call shape with a readable count does move, so the None above is
    # the count's doing and not an inert function.
    assert op.evaluate_progress(0, "abc", "abc") == (1, "unchanged")


# ── the fingerprint ─────────────────────────────────────────────
def test_a_quiet_repository_fingerprints_the_same_twice(tmp_path):
    repo = make_repo(tmp_path / "repo")
    first = op.workspace_fingerprint(repo)
    assert first is not None
    assert op.workspace_fingerprint(repo) == first


def test_a_commit_changes_the_fingerprint(tmp_path):
    repo = make_repo(tmp_path / "repo")
    before = op.workspace_fingerprint(repo)
    (repo / "new.txt").write_text("work\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "did something")
    after = op.workspace_fingerprint(repo)
    assert after is not None
    assert after != before


def test_an_uncommitted_edit_changes_the_fingerprint(tmp_path):
    """Most sessions end with work in the tree, not yet committed. Requiring
    a commit would call that session idle."""
    repo = make_repo(tmp_path / "repo")
    before = op.workspace_fingerprint(repo)
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    assert op.workspace_fingerprint(repo) != before


def test_an_untracked_file_changes_the_fingerprint(tmp_path):
    """A brand new file is untracked, and a new file is the most common shape
    of a productive session."""
    repo = make_repo(tmp_path / "repo")
    before = op.workspace_fingerprint(repo)
    (repo / "brand_new.py").write_text("print(1)\n", encoding="utf-8")
    assert op.workspace_fingerprint(repo) != before


def test_a_commit_in_a_linked_worktree_changes_the_fingerprint(tmp_path):
    """The case this repository actually runs into.

    All work happens on a branch in a worktree under `.worktrees/`. Neither
    the primary checkout's HEAD nor its `git status` moves when that branch
    gets a commit, so a fingerprint taken only of the current directory would
    report a session that landed an entire feature as having changed nothing
    -- and three of those in a row would stop a loop that was working.
    """
    repo = make_repo(tmp_path / "repo")
    before = op.workspace_fingerprint(repo)
    _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(repo / "wt"))
    # The worktree itself moved the ref list; the interesting part is the
    # commit made inside it afterwards.
    after_add = op.workspace_fingerprint(repo)
    (repo / "wt" / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo / "wt", "add", "-A")
    _git(repo / "wt", "commit", "-q", "-m", "feature work")

    after_commit = op.workspace_fingerprint(repo)
    assert after_commit is not None
    assert after_commit != after_add, \
        "a commit on a branch in a linked worktree must count as progress"
    assert after_commit != before


def test_uncommitted_work_in_a_linked_worktree_changes_the_fingerprint(tmp_path):
    repo = make_repo(tmp_path / "repo")
    _git(repo, "worktree", "add", "-q", "-b", "feat/y", str(repo / "wt"))
    before = op.workspace_fingerprint(repo)
    (repo / "wt" / "scratch.py").write_text("y = 2\n", encoding="utf-8")
    assert op.workspace_fingerprint(repo) != before


def test_a_directory_that_is_not_a_repository_has_no_fingerprint(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert op.workspace_fingerprint(plain) is None
    # The same call answers for a real repository, so None is a property of
    # this directory rather than of the function.
    assert op.workspace_fingerprint(make_repo(tmp_path / "repo")) is not None


def test_git_being_unavailable_is_unknown_not_unchanged(tmp_path, monkeypatch):
    """If `git` cannot be run at all, every session would fingerprint
    identically -- as None. Returning a constant instead would make the
    breaker stop the loop after three sessions on a machine with no git."""
    repo = make_repo(tmp_path / "repo")
    assert op.workspace_fingerprint(repo) is not None

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(op.subprocess, "run", no_git)
    assert op.workspace_fingerprint(repo) is None


def test_a_git_command_that_fails_is_unknown(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")

    def failing(*args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")

    monkeypatch.setattr(op.subprocess, "run", failing)
    assert op.workspace_fingerprint(repo) is None


def test_a_timed_out_probe_is_unknown(tmp_path, monkeypatch):
    """A busy repository can hold the index lock. Waiting forever would hang
    the supervisor; guessing would corrupt the verdict."""
    repo = make_repo(tmp_path / "repo")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(op.subprocess, "run", slow)
    assert op.workspace_fingerprint(repo) is None


def test_an_unreadable_worktree_makes_the_whole_verdict_unknown(tmp_path,
                                                               monkeypatch):
    """The worktree that cannot be examined is exactly the one that might
    hold the change, so the repository has no verdict."""
    repo = make_repo(tmp_path / "repo")
    _git(repo, "worktree", "add", "-q", "-b", "feat/z", str(repo / "wt"))
    assert op.workspace_fingerprint(repo) is not None

    real_run = op.subprocess.run

    def refuse_status_in_worktree(args, **kwargs):
        if "status" in args and str(kwargs.get("cwd", "")).endswith("wt"):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="nope")
        return real_run(args, **kwargs)

    monkeypatch.setattr(op.subprocess, "run", refuse_status_in_worktree)
    assert op.workspace_fingerprint(repo) is None


def test_a_worktree_directory_that_is_gone_does_not_disable_the_breaker(
    tmp_path, monkeypatch
):
    """git keeps listing a worktree whose directory was deleted. Treating
    that as unreadable would leave the breaker permanently unable to reach a
    verdict, which is indistinguishable from having no breaker at all."""
    import shutil

    repo = make_repo(tmp_path / "repo")
    _git(repo, "worktree", "add", "-q", "-b", "feat/gone", str(repo / "wt"))
    shutil.rmtree(repo / "wt")

    fingerprint = op.workspace_fingerprint(repo)
    assert fingerprint is not None, \
        "a pruned worktree must not make the repository unmeasurable"
    # And the surviving checkout is still being measured, so the fingerprint
    # is not merely a constant produced by giving up on everything.
    (repo / "later.txt").write_text("more\n", encoding="utf-8")
    assert op.workspace_fingerprint(repo) != fingerprint


# ── the persisted counter ───────────────────────────────────────
def test_a_missing_counter_reads_as_zero():
    inst = op.Instance("counter-new")
    assert inst.read_nochange_count() == 0


def test_the_counter_round_trips():
    inst = op.Instance("counter-rt")
    inst.save_nochange_count(2)
    assert inst.read_nochange_count() == 2


def test_a_corrupt_counter_reads_as_unknown():
    """Hand-edited or half-written state must not silently become zero."""
    inst = op.Instance("counter-bad")
    inst.nochange_file.write_text("not a number\n", encoding="utf-8")
    assert inst.read_nochange_count() is None
    # The same reader returns a number for a good file, so None is the file's
    # doing.
    inst.save_nochange_count(1)
    assert inst.read_nochange_count() == 1


def test_a_negative_counter_reads_as_unknown():
    inst = op.Instance("counter-neg")
    inst.nochange_file.write_text("-4\n", encoding="utf-8")
    assert inst.read_nochange_count() is None


def test_an_unexaminable_counter_reads_as_unknown(monkeypatch):
    """A revoked directory raises rather than reporting absence; that must
    not be read as 'no stalling recorded yet'."""
    inst = op.Instance("counter-denied")
    inst.save_nochange_count(2)
    with denied(monkeypatch, inst.nochange_file):
        assert inst.read_nochange_count() is None


def test_cleanup_removes_the_counter():
    """A stopped instance starts its next run fresh -- otherwise a tripped
    breaker would trip again on the first session of the next run."""
    inst = op.Instance("counter-clean")
    inst.save_nochange_count(3)
    inst.cleanup_files()
    assert inst.read_nochange_count() == 0


# ── the loop ────────────────────────────────────────────────────
# Everything above tests the parts. These drive `run_loop_mode` itself,
# because the counting, the fingerprinting and the persistence can all be
# correct while the block that wires them into the loop is not.
@pytest.fixture
def loop_in_repo(tmp_path, monkeypatch):
    """A supervisor whose working directory is a real repository.

    `run_loop_mode` reads `Path.cwd()` once, at startup, so the chdir has to
    happen before it is called.
    """
    repo = make_repo(tmp_path / "project")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)
    return repo


def _sessions_that_die(sessions: list[int], on_start=None):
    """A `start_session` double that records each launch and dies at once."""
    def start(instance, args, session_num, remain_on_exit=False, preamble=""):
        sessions.append(session_num)
        if on_start is not None:
            on_start(session_num)
        instance.exit_file.write_text("0", encoding="utf-8")
    return start


def _age_past_healthy(monkeypatch):
    """Make every session look like it stayed up past the healthy threshold.

    That resets `crash_failures` on each death, so MAX_LAUNCH_FAILURES can
    never be reached and the breaker is the only thing that can end the loop.
    Without this, a test that needs more than MAX_LAUNCH_FAILURES sessions is
    stopped by the crash counter and never reaches the behaviour it is about.
    """
    clock = {"t": 1_000.0}
    monkeypatch.setattr(op.time, "time", lambda: clock["t"])
    really_running = op.is_copilot_running

    def aged(instance):
        clock["t"] += op.HEALTHY_SESSION_SECONDS + 1
        return really_running(instance)

    monkeypatch.setattr(op, "is_copilot_running", aged)


def test_the_loop_stops_after_three_sessions_that_change_nothing(
        loop_in_repo, monkeypatch, capsys):
    launched: list[int] = []
    monkeypatch.setattr(op, "start_session", _sessions_that_die(launched))

    inst = op.Instance("stalled")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == op.EXIT_NO_PROGRESS
    assert launched == [1, 2, 3], (
        "the breaker must stop the loop instead of starting a fourth session")
    # The exit code alone would be satisfied by any path that returns 3; the
    # reason has to be the one claimed.
    assert "Progress breaker tripped" in capsys.readouterr().err


def test_a_session_that_changes_the_repository_clears_the_streak(
        loop_in_repo, monkeypatch):
    """Two idle sessions either side of a productive one are not a stall.

    Without the reset the loop would stop at the third session regardless of
    what the second one achieved.
    """
    _age_past_healthy(monkeypatch)
    launched: list[int] = []

    def work_on_the_third(session_num):
        if session_num == 3:
            (loop_in_repo / f"session{session_num}.txt").write_text(
                "work\n", encoding="utf-8")

    monkeypatch.setattr(op, "start_session",
                        _sessions_that_die(launched, work_on_the_third))

    inst = op.Instance("productive")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == op.EXIT_NO_PROGRESS
    assert launched == [1, 2, 3, 4, 5, 6], (
        "session 3 changed the repository, so the streak must restart there "
        "and the loop must survive three more sessions")


def test_the_breaker_bounds_the_healthy_uptime_path(
        loop_in_repo, monkeypatch, capsys):
    """The regression this breaker exists for.

    A session that stays up past HEALTHY_SESSION_SECONDS and then dies resets
    `crash_failures`, so MAX_LAUNCH_FAILURES can never be reached and the
    relaunching is unbounded -- see `test_loop_resilience.py::
    test_a_session_that_ran_for_minutes_does_not_count_toward_the_give_up_
    limit`, which asserts exactly that and has to raise KeyboardInterrupt to
    terminate. This machine's operator.log shows the real thing: fifteen
    sessions in seventy-eight minutes, each up for minutes, none of them
    changing anything.

    Nothing in the crash counter can stop that. The breaker is the only bound
    on it, so it has to hold with the counter being reset underneath it.
    """
    _age_past_healthy(monkeypatch)
    launched: list[int] = []
    monkeypatch.setattr(op, "start_session", _sessions_that_die(launched))

    inst = op.Instance("healthy-but-idle")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == op.EXIT_NO_PROGRESS
    assert len(launched) == op.MAX_NOCHANGE_SESSIONS
    err = capsys.readouterr().err
    assert "Progress breaker tripped" in err
    # The crash counter really was being reset, so the stop cannot be
    # credited to it: this is the negative control for the claim above.
    assert "not a crash loop, resetting the exit count" in err


def test_a_workspace_with_no_git_state_leaves_the_loop_unbounded(
        tmp_path, monkeypatch, capsys):
    """The breaker is off where it cannot measure, and says so.

    This is the isolation `test_loop_resilience.py` depends on. If it ever
    stopped holding, every counter test in that file would silently start
    measuring the breaker instead.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda instance: None)

    launched: list[int] = []
    monkeypatch.setattr(op, "start_session", _sessions_that_die(launched))

    inst = op.Instance("unmeasurable")
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc != op.EXIT_NO_PROGRESS, "nothing was measurable to stop on"
    assert len(launched) == op.MAX_LAUNCH_FAILURES, (
        "the crash counter, not the breaker, must be what ends this loop")
    assert "Progress breaker: inactive" in capsys.readouterr().err


def test_the_streak_survives_a_supervisor_swap(loop_in_repo, monkeypatch):
    """The counter is on disk so `operator restart-loop` cannot reset it.

    A breaker that lived in the supervisor's memory would forget its count
    every time the supervisor was replaced, and a loop that is restarted
    more often than MAX_NOCHANGE_SESSIONS could never trip it.
    """
    inst = op.Instance("swapped")
    inst.save_nochange_count(op.MAX_NOCHANGE_SESSIONS - 1)

    launched: list[int] = []
    monkeypatch.setattr(op, "start_session", _sessions_that_die(launched))

    # is_fresh=False: a replacement supervisor continues the run rather than
    # starting one, which is the case the on-disk counter exists for.
    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False)

    assert rc == op.EXIT_NO_PROGRESS
    assert launched == [1], (
        "the inherited streak should be one session short of the limit")


def test_a_fresh_run_does_not_inherit_a_stalled_streak(
        loop_in_repo, monkeypatch):
    """`--fresh` means forget the previous run, counter included."""
    inst = op.Instance("refreshed")
    inst.save_nochange_count(op.MAX_NOCHANGE_SESSIONS - 1)

    launched: list[int] = []
    monkeypatch.setattr(op, "start_session", _sessions_that_die(launched))

    rc = op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=True)

    assert rc == op.EXIT_NO_PROGRESS
    assert len(launched) == op.MAX_NOCHANGE_SESSIONS, (
        "a fresh run is owed the full allowance, not the one session left "
        "over from the previous run")

