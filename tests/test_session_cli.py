"""Tests for the `operator session start|end` CLI seam.

The lifecycle itself is covered in test_session_lifecycle.py. What is covered
here is everything between the argv and that module: option parsing, the
session-number fallback, and the conversion of the handoff tool's exit
convention into a reported failure.

Argument parsing gets the most attention because its failure mode is silence.
A caller who wrote `--next --done`, was told the session ended, and finds the
claim still held has been lied to by a command whose whole job is to be the
last thing a session does.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import copilot_operator as op
import operator_session as osess
import work_claims as wc


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = osess.db_path(tmp_path)
    osess.init_db(path)
    return path


def _parse(*args):
    return op._parse_session_args(list(args))


# ── option parsing: values ──────────────────────────────────────
def test_separate_and_inline_values_agree() -> None:
    separate = _parse("--instance", "alpha", "--status", "done")
    inline = _parse("--instance=alpha", "--status=done")
    assert separate is not None and inline is not None
    assert separate["instance"] == inline["instance"] == "alpha"
    assert separate["status"] == inline["status"] == "done"


def test_an_inline_empty_value_is_taken_as_empty() -> None:
    opts = _parse("--instance", "alpha", "--context=")
    assert opts is not None and opts["context"] == ""


@pytest.mark.parametrize("flag", ["--project", "--status", "--next",
                                  "--context", "--prompt", "--in-progress",
                                  "--instance", "--session"])
def test_a_flag_at_the_end_of_argv_is_refused(flag) -> None:
    assert _parse("--instance", "alpha", flag) is None


@pytest.mark.parametrize("flag,follower", [
    ("--next", "--done"),
    ("--status", "--json"),
    ("--project", "--json"),
    ("--session", "--done"),
    ("--context", "--next"),
])
def test_a_value_flag_never_eats_the_option_after_it(flag, follower) -> None:
    """The regression this parser exists to prevent, in its quietest form.

    Binding the follower as a value leaves the flag it was meant to set
    unset, and the command then reports success for an effect that never
    happened -- `--done` swallowed by `--next` means the claim is retained
    while the caller is told the session ended.
    """
    assert _parse("--instance", "alpha", flag, follower) is None


def test_a_dash_prefixed_value_is_still_expressible_inline() -> None:
    opts = _parse("--instance", "alpha", "--status=--done")
    assert opts is not None and opts["status"] == "--done"
    assert opts["done"] is False


# ── option parsing: --session ───────────────────────────────────
def test_session_accepts_a_number_either_way() -> None:
    assert _parse("--instance", "a", "--session", "7")["session"] == 7
    assert _parse("--instance", "a", "--session=7")["session"] == 7


@pytest.mark.parametrize("raw", ["abc", "", "7.5", "-3"])
def test_session_refuses_anything_that_is_not_a_session_number(raw) -> None:
    assert _parse("--instance", "a", f"--session={raw}") is None


def test_session_defaults_to_none_so_the_supervisor_state_is_consulted() -> None:
    assert _parse("--instance", "a")["session"] is None


# ── option parsing: flags and refusals ──────────────────────────
@pytest.mark.parametrize("flag", ["--done", "--release"])
def test_both_spellings_of_release_are_accepted(flag) -> None:
    assert _parse("--instance", "a", flag)["done"] is True


def test_done_is_off_unless_asked_for() -> None:
    assert _parse("--instance", "a")["done"] is False


@pytest.mark.parametrize("arg", ["--relase", "--json=true", "--done=1",
                                 "start", "-x"])
def test_an_unrecognised_argument_is_refused_not_ignored(arg) -> None:
    assert _parse("--instance", "alpha", arg) is None


def test_instance_is_required() -> None:
    assert _parse("--status", "x") is None
    assert _parse("--instance=") is None


# ── the session number fallback ─────────────────────────────────
def test_session_number_comes_from_the_supervisor_state(monkeypatch) -> None:
    class State:
        def __init__(self, _id):
            pass

        def load_state(self):
            return {"SESSION_NUM": "12"}

    monkeypatch.setattr(op, "Instance", State)
    assert op._read_session_number("alpha") == 12


@pytest.mark.parametrize("state", [
    {},                        # no supervisor has run for this instance
    {"SESSION_NUM": None},     # present but unset
    {"SESSION_NUM": "abc"},    # unparseable
    "not a dict",              # state file of an unexpected shape
])
def test_an_unusable_state_reads_as_session_zero(monkeypatch, state) -> None:
    class State:
        def __init__(self, _id):
            pass

        def load_state(self):
            return state

    monkeypatch.setattr(op, "Instance", State)
    assert op._read_session_number("alpha") == 0


def test_a_state_that_cannot_be_read_reads_as_session_zero(monkeypatch) -> None:
    class State:
        def __init__(self, _id):
            pass

        def load_state(self):
            raise OSError("state file is gone")

    monkeypatch.setattr(op, "Instance", State)
    assert op._read_session_number("alpha") == 0


# ── session end: the handoff's exit convention ──────────────────
def _end_opts(**kw):
    opts = {"instance": "alpha", "session": 1, "project": None, "json": True,
            "done": False, "status": "did the thing", "next": "do the next",
            "context": "", "prompt": "", "in_progress": ""}
    opts.update(kw)
    return opts


def test_end_refuses_a_handoff_with_no_status_or_next(db, capsys) -> None:
    assert op._session_end(_end_opts(status=""), db) == 1
    assert op._session_end(_end_opts(next=""), db) == 1
    assert osess.sessions(db) == []


def test_a_handoff_that_exits_nonzero_leaves_the_claim_alone(db, monkeypatch,
                                                             capsys) -> None:
    """`handoff_tool.die` calls sys.exit, and SystemExit is not an Exception.

    Left unconverted it would escape `end_session` entirely -- the claim would
    still be safe, because nothing after the handoff runs, but the caller
    would be told nothing at all and the command's own exit code would come
    from the handoff tool rather than from the session.
    """
    wc.claim(db, item="0007", instance="alpha", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="alpha", pid=1000, pid_start="tok")
    osess.start_session(db, instance="alpha", session=1)

    def explode(_argv):
        raise SystemExit(1)

    monkeypatch.setattr(op.handoff_tool, "main", explode)
    rc = op._session_end(_end_opts(done=True), db)

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["handoff_written"] is False
    assert payload["claim_released"] is False
    assert "handoff exited 1" in payload["failure"]
    # The whole point: the claim outlives a failed handoff.
    assert [c.item for c in wc.claims(db)] == ["0007"]
    assert osess.sessions(db)[0]["ended_at"] is None


def test_a_handoff_that_succeeds_closes_the_session(db, monkeypatch,
                                                    capsys) -> None:
    wc.claim(db, item="0007", instance="alpha", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="alpha", pid=1000, pid_start="tok")
    osess.start_session(db, instance="alpha", session=1)
    seen: list = []
    monkeypatch.setattr(op.handoff_tool, "main",
                        lambda argv: seen.append(argv) or 0)

    assert op._session_end(_end_opts(done=True), db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["handoff_written"] is True
    assert payload["claim_released"] is True
    assert wc.claims(db) == []
    assert osess.sessions(db)[0]["ended_at"] is not None
    # The raw display name is what reaches the handoff tool, which applies
    # `safe_instance_id` itself -- sanitizing here too would produce a
    # different id than every other consumer resolves.
    assert "alpha" in seen[0]


def test_the_claim_is_kept_when_done_is_not_asked_for(db, monkeypatch,
                                                      capsys) -> None:
    wc.claim(db, item="0007", instance="alpha", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="alpha", pid=1000, pid_start="tok")
    osess.start_session(db, instance="alpha", session=1)
    monkeypatch.setattr(op.handoff_tool, "main", lambda argv: 0)

    assert op._session_end(_end_opts(), db) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claim_retained"] is True
    assert payload["claim_released"] is False
    assert [c.item for c in wc.claims(db)] == ["0007"]


# ── the subcommand itself ───────────────────────────────────────
@pytest.mark.parametrize("argv", [["stop"], ["strat"], ["END"]])
def test_an_unknown_subcommand_is_refused(argv, capsys) -> None:
    assert op.manage_session(argv) == 1
    assert "Unknown subcommand" in capsys.readouterr().err


def test_bare_session_prints_usage_and_fails(capsys) -> None:
    assert op.manage_session([]) == 1
    assert "operator session start" in capsys.readouterr().err


def test_help_prints_usage_and_succeeds(capsys) -> None:
    assert op.manage_session(["--help"]) == 0
    assert "operator session start" in capsys.readouterr().out


def test_a_bad_option_stops_before_any_database_is_touched(monkeypatch,
                                                           capsys) -> None:
    """Parsing is refused first, so a typo cannot resolve a project at all."""
    def fail(_cwd):
        raise AssertionError("the database was resolved despite a bad option")

    monkeypatch.setattr(op, "_session_db", fail)
    assert op.manage_session(["end", "--instance", "a", "--relase"]) == 1
    assert "Unknown option" in capsys.readouterr().err


# -- the loop wiring (D4) ----------------------------------------
def test_the_preamble_carries_the_assignment(db) -> None:
    """FR-2: the agent's first token already knows what it is working on."""
    wc.claim(db, item="0007", instance="alpha", subproject="",
             worktree="/w/feat-x", branch="feat/x", boot_id="boot-1",
             mux_session="alpha", pid=1000, pid_start="tok")
    assignment = osess.start_session(db, instance="alpha", session=1)
    instance = op.Instance("alpha")

    without = op.build_preamble("anvil", instance)
    with_it = op.build_preamble("anvil", instance, assignment=assignment)

    assert "0007" not in without
    assert "0007" in with_it
    assert with_it.startswith(without)


def test_an_unassigned_session_says_nothing_extra(db) -> None:
    """Silence, not a line reading "you have no assignment".

    An always-present line is paid for on every token of every session that
    has none, which is the cost this whole feature exists to remove.
    """
    assignment = osess.start_session(db, instance="alpha", session=1)
    instance = op.Instance("alpha")
    assert (op.build_preamble("anvil", instance, assignment=assignment)
            == op.build_preamble("anvil", instance))


def test_the_loop_opens_a_session_row_and_resolves_the_claim(db) -> None:
    wc.claim(db, item="0007", instance="alpha", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="alpha", pid=1000, pid_start="tok")
    got = op._loop_start_session(db, op.Instance("alpha"), 4)
    assert got is not None and got.kind == osess.RESUME and got.item == "0007"
    rows = osess.sessions(db)
    assert [(r["instance"], r["session"]) for r in rows] == [("alpha", 4)]


def test_an_unresolvable_database_costs_a_hint_not_a_session(tmp_path,
                                                             monkeypatch) -> None:
    """Every failure in the loop path is a None, never an exception.

    The loop is unattended: a project that is not registered, or a database
    that cannot be opened, must still get its agent launched.
    """
    lines: list = []
    monkeypatch.setattr(op, "log", lines.append)
    assert op._loop_start_session(None, op.Instance("alpha"), 1) is None

    def boom(_root):
        raise OSError("no catalog")

    monkeypatch.setattr(op, "catalog_guid", boom)
    assert op._loop_work_db(tmp_path) is None
    assert any("work database" in line for line in lines)

    # A database path that cannot be a database at all.
    assert op._loop_start_session(tmp_path, op.Instance("alpha"), 1) is None
    assert any("assignment" in line for line in lines)


def test_the_supervisor_heartbeats_the_claim_it_finds_now(db, monkeypatch) -> None:
    """Re-read, not remembered: a claim taken mid-session must be refreshed."""
    monkeypatch.setattr(op, "log", lambda *_: None)
    # Nothing held yet -- the loop's first ticks are silent and harmless.
    op._loop_heartbeat(db, "alpha")
    assert wc.claims(db) == []

    wc.claim(db, item="0007", instance="alpha", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="alpha", pid=1000, pid_start="tok")
    before = wc.claim_for_instance(db, "alpha").heartbeat_at
    op._loop_heartbeat(db, "alpha")
    after = wc.claim_for_instance(db, "alpha").heartbeat_at
    assert after >= before

    # Another instance's claim is never touched: heartbeat is owner-guarded
    # and this looks up only what "alpha" holds.
    op._loop_heartbeat(db, "beta")
    assert wc.claim_for_instance(db, "alpha") is not None


def test_a_broken_heartbeat_never_reaches_the_loop(tmp_path, monkeypatch) -> None:
    lines: list = []
    monkeypatch.setattr(op, "log", lines.append)
    op._loop_heartbeat(tmp_path / "nope" / "work.db", "alpha")
    assert any("work claim" in line for line in lines)


def test_the_heartbeat_interval_is_longer_than_the_poll_interval() -> None:
    """One write per poll would buy no evidence the cascade can use."""
    assert op.HEARTBEAT_INTERVAL > op.POLL_INTERVAL


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    """Enough isolation to run one real supervisor cycle, as the loop tests do."""
    home = tmp_path / "home"
    restart = home / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", home)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", home / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", home / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", home / "logs")
    monkeypatch.setattr(op, "TABS_FILE", home / "tabs.json")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "LAUNCH_BACKOFF_BASE", 0)
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return home


def test_a_real_loop_cycle_opens_the_session_and_carries_the_assignment(
        db, loop_env, monkeypatch) -> None:
    """The end-to-end wiring, driven through `run_loop_mode` itself.

    Everything else here tests the pieces in isolation, which is exactly how
    this feature could be complete, fully covered, and still dead: nothing
    forces the loop to call any of it. One real cycle is what says otherwise.
    """
    wc.claim(db, item="0007", instance="looper", subproject="",
             worktree="/w/feat-x", branch="feat/x", boot_id="boot-1",
             mux_session="looper", pid=1000, pid_start="tok")
    monkeypatch.setattr(op, "_loop_work_db", lambda _workdir: db)
    seen: list = []

    def launch(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", launch)

    assert op.run_loop_mode(op.Instance("looper"),
                            ["--agent", "test:agent"], is_fresh=True) == 0

    assert seen, "the loop never launched a session"
    assert "0007" in seen[0], seen[0]
    rows = osess.sessions(db)
    assert [(r["instance"], r["session"]) for r in rows] == [("looper", 1)]


def test_a_loop_cycle_survives_a_project_with_no_work_database(
        loop_env, monkeypatch) -> None:
    """The unattended case: an unregistered project still gets its agent."""
    seen: list = []

    def launch(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", launch)
    assert op.run_loop_mode(op.Instance("orphan"),
                            ["--agent", "test:agent"], is_fresh=True) == 0
    assert seen and "Operator instance: orphan" in seen[0]


def test_a_running_session_gets_its_claim_heartbeaten_by_the_supervisor(
        db, loop_env, monkeypatch) -> None:
    """The loop must actually call it -- the D4 half an agent cannot do itself.

    Reaching this line needs a session that is *still up* on a poll, which is
    the state every other loop test here skips past. Without it the heartbeat
    is a function that exists, is covered, and is never invoked: a claim that
    goes stale under a working agent and is then reclaimed away from it.
    """
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    wc.claim(db, item="0007", instance="beater", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="beater", pid=1000, pid_start="tok", now=old)
    before = wc.claim_for_instance(db, "beater").heartbeat_at
    monkeypatch.setattr(op, "_loop_work_db", lambda _workdir: db)
    real_heartbeat = op._loop_heartbeat
    calls: list = []
    polls = {"n": 0}

    def still_running(_inst):
        # True until the heartbeat has been seen -- but bounded, so a build in
        # which the loop never heartbeats fails this test instead of hanging
        # it. A guard whose only failure mode is an infinite loop cannot be
        # mutation-tested, and one that cannot be mutation-tested is a guess.
        polls["n"] += 1
        return not calls and polls["n"] < 20

    monkeypatch.setattr(op, "is_copilot_running", still_running)
    # No runner is writing a session id here, and with copilot forced alive
    # the discovery wait would otherwise sleep out its whole budget.
    monkeypatch.setattr(op, "SESSION_ID_WAIT", 0)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda _inst: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda _s: False)

    def watched(work_db, instance_id):
        # Delegates to the real thing, so the claim below is genuinely
        # refreshed; the marker ends the loop on the next iteration.
        calls.append(instance_id)
        real_heartbeat(work_db, instance_id)
        op.Instance("beater").stop_marker.touch()

    monkeypatch.setattr(op, "_loop_heartbeat", watched)

    def launch(instance, args, session_num, remain_on_exit=False, preamble=""):
        return None

    monkeypatch.setattr(op, "start_session", launch)
    op.run_loop_mode(op.Instance("beater"), ["--agent", "test:agent"],
                     is_fresh=True)

    assert calls == ["beater"], "the loop never heartbeat the running session"
    after = wc.claim_for_instance(db, "beater").heartbeat_at
    assert after > before, (before, after)


def test_an_adopted_session_is_logged_and_heartbeaten_too(db, loop_env,
                                                          monkeypatch) -> None:
    """Adoption skips the launch, and skipped the session lifecycle with it.

    The first version of this wiring lived entirely in the launch branch, so
    `operator --loop --adopt` reached the poll with no work database resolved
    and crashed on the first heartbeat. An adopted session is a session: it
    gets a log row and a heartbeat, and only the preamble is missing, because
    nothing is being launched to read one.
    """
    wc.claim(db, item="0007", instance="adopted", subproject="",
             worktree=None, branch=None, boot_id="boot-1",
             mux_session="adopted", pid=1000, pid_start="tok",
             now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    before = wc.claim_for_instance(db, "adopted").heartbeat_at
    monkeypatch.setattr(op, "_loop_work_db", lambda _workdir: db)
    monkeypatch.setattr(op, "SESSION_ID_WAIT", 0)
    monkeypatch.setattr(op, "stop_session_gracefully", lambda _inst: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda _s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda _s: False)
    monkeypatch.setattr(op.MUX, "kill_session", lambda _s: None)

    real_heartbeat = op._loop_heartbeat
    calls: list = []
    polls = {"n": 0}

    def still_running(_inst):
        polls["n"] += 1
        return not calls and polls["n"] < 20

    monkeypatch.setattr(op, "is_copilot_running", still_running)

    def watched(work_db, instance_id):
        calls.append(instance_id)
        real_heartbeat(work_db, instance_id)
        op.Instance("adopted").stop_marker.touch()

    monkeypatch.setattr(op, "_loop_heartbeat", watched)

    def launch(instance, args, session_num, remain_on_exit=False, preamble=""):
        raise AssertionError("an adopted session must not be launched")

    monkeypatch.setattr(op, "start_session", launch)
    inst = op.Instance("adopted")
    inst.claim("test-token")
    inst.save_state(2, "2026-07-27T10:00:00Z", "")
    op.run_loop_mode(inst, ["--agent", "test:agent"], is_fresh=False,
                     adopt=True)

    assert calls == ["adopted"]
    assert wc.claim_for_instance(db, "adopted").heartbeat_at > before
    assert [(r["instance"], r["session"]) for r in osess.sessions(db)] \
        == [("adopted", 2)]
