"""`operator restart-loop --all`, and the notices that send people to it.

An operator change makes every supervisor on the machine stale at the same
instant -- they each imported their code once, at startup. So the per-instance
restart is the exception and the sweep is the normal case, and for a while it
was the other way round: `operator list` named eight stale supervisors and
printed eight commands to type. A remedy applied by hand once per instance is
a remedy applied to some of them.
"""
from __future__ import annotations

import pytest

import copilot_operator as op


@pytest.fixture(autouse=True)
def operator_home(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    return tmp_path


@pytest.fixture
def three_looping(monkeypatch):
    """Three managed instances, each with a live supervisor."""
    names = ["alpha", "beta", "gamma"]
    for name in names:
        op.Instance(name).claim("tok")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions",
                        lambda: [op.Instance(n).id for n in names])
    monkeypatch.setattr(op, "_running_loop_pid", lambda inst: 4242)
    monkeypatch.setattr(op, "_own_instance_id", lambda: None)
    return names


def test_the_sweep_restarts_every_running_supervisor(three_looping, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(op, "restart_loop",
                        lambda name: seen.append(name) or 0)
    assert op.restart_all_loops() == 0
    assert sorted(seen) == ["alpha", "beta", "gamma"]


def test_one_refusal_does_not_abandon_the_rest(three_looping, monkeypatch, capsys):
    """The whole point of a sweep is that it is not a shell loop with set -e.

    An instance with no recorded loop arguments, or a session someone else
    owns, refuses -- and must not decide the fate of the others.
    """
    seen: list[str] = []

    def one_fails(name):
        seen.append(name)
        return 1 if name == "beta" else 0

    monkeypatch.setattr(op, "restart_loop", one_fails)
    assert op.restart_all_loops() == 1, "a refusal must be reported in the status"
    assert sorted(seen) == ["alpha", "beta", "gamma"], \
        "the sweep stopped at the instance that refused"
    out = capsys.readouterr().out
    assert "Restarted 2/3" in out
    assert "Not restarted: beta" in out, \
        "a count without names leaves the reader to work out which ones failed"


def test_a_dying_restart_does_not_kill_the_sweep(three_looping, monkeypatch):
    """`die` raises SystemExit, and it is reachable from the restart path."""
    seen: list[str] = []

    def one_dies(name):
        seen.append(name)
        if name == "beta":
            raise SystemExit(2)
        return 0

    monkeypatch.setattr(op, "restart_loop", one_dies)
    assert op.restart_all_loops() == 1
    assert sorted(seen) == ["alpha", "beta", "gamma"], \
        "SystemExit from one instance ended the sweep"


def test_the_callers_own_supervisor_goes_last(three_looping, monkeypatch):
    """An agent restarting its own supervisor is the one least able to report.

    Whatever it can still speak for, it should do first.
    """
    monkeypatch.setattr(op, "_own_instance_id", lambda: op.Instance("alpha").id)
    seen: list[str] = []
    monkeypatch.setattr(op, "restart_loop", lambda name: seen.append(name) or 0)
    assert op.restart_all_loops() == 0
    assert seen[-1] == "alpha", f"expected alpha last, got {seen}"
    assert sorted(seen) == ["alpha", "beta", "gamma"], "self was dropped, not deferred"


def test_the_sweep_skips_instances_with_no_supervisor(monkeypatch):
    """A session whose loop was stopped has nothing to replace."""
    op.Instance("solo").claim("tok")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [op.Instance("solo").id])
    monkeypatch.setattr(op, "_running_loop_pid", lambda inst: None)
    monkeypatch.setattr(op, "_own_instance_id", lambda: None)
    called: list[str] = []
    monkeypatch.setattr(op, "restart_loop", lambda name: called.append(name) or 0)
    assert op.restart_all_loops() == 0
    assert called == [], "restarted a supervisor that was not running"


def test_nothing_running_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(op, "active_instances", lambda: [])
    monkeypatch.setattr(op, "_own_instance_id", lambda: None)
    assert op.restart_all_loops() == 0


# ── self-detection must not be fooled by a recycled pid ─────────
def _ancestry(monkeypatch, chain):
    monkeypatch.setattr(op.operator_trace, "ancestry", lambda: chain)


def test_self_detection_matches_a_copilot_ancestor(monkeypatch):
    inst = op.Instance("alpha")
    inst.claim("tok")
    monkeypatch.setattr(op.Instance, "copilot_pid", lambda self: 4242)
    _ancestry(monkeypatch, [{"pid": 999, "name": "pwsh.exe"},
                            {"pid": 4242, "name": "copilot.EXE"}])
    assert op._own_instance_id() == inst.id


def test_a_recycled_pid_on_an_unrelated_ancestor_is_not_self(monkeypatch):
    """The cost of a wrong positive is worse than a wrong None.

    Every ancestry holds long-lived shells and multiplexers, and a dead
    session's recorded pid can be reissued to one of them. A pid-only test let
    that collision decide which row is "this session's own" -- which defers the
    wrong instance and leaves the real one near the front, where a failing
    restart can take this process down before it reports on the rest. It also
    prints "this session's own supervisor" against somebody else's name.
    """
    inst = op.Instance("alpha")
    inst.claim("tok")
    monkeypatch.setattr(op.Instance, "copilot_pid", lambda self: 8400)
    _ancestry(monkeypatch, [{"pid": 8400, "name": "tmux.exe"},
                            {"pid": 777, "name": "pwsh.exe"}])
    assert op._own_instance_id() is None, \
        "a recycled pid on a multiplexer was accepted as this agent's session"


def test_self_detection_answers_none_when_the_process_table_is_unreadable(
    monkeypatch,
):
    monkeypatch.setattr(op.operator_trace, "ancestry", lambda: None)
    assert op._own_instance_id() is None


def test_the_cli_routes_all_to_the_sweep(monkeypatch):
    for flag in ("--all", "-a"):
        called = {"n": 0}
        monkeypatch.setattr(op, "restart_all_loops",
                            lambda: called.__setitem__("n", called["n"] + 1) or 0)
        monkeypatch.setattr(op, "restart_loop",
                            lambda name: pytest.fail(
                                f"{flag} was treated as an instance name"))
        assert op.main(["restart-loop", flag]) == 0
        assert called["n"] == 1


def test_restart_loop_with_no_target_advertises_the_sweep(capsys):
    assert op.restart_loop(None) == 1
    err = capsys.readouterr().err
    assert "--all" in err, \
        "the usage line does not mention the form that is normally wanted"


# ── the notices must not go back to one command per instance ────
def _stale_snap(name, verdict):
    return {"name": name, "loop_pid": 99, "loop_code": verdict}


@pytest.mark.parametrize("verdict", [op.CODE_STALE, op.CODE_UNRECORDED,
                                     op.CODE_MISMATCH])
def test_a_supervisor_notice_offers_the_sweep_not_a_command_per_instance(
    verdict, monkeypatch, capsys
):
    """Every one of these fires for all instances at once, by construction.

    Parametrised over the verdicts rather than written out for one, because
    the defect was that a rule was applied to the group somebody happened to
    be looking at. A fourth verdict added without a remedy line fails the
    coverage test below instead of slipping through here.
    """
    snaps = [_stale_snap(n, verdict) for n in ("alpha", "beta", "gamma")]
    byname = {s["name"]: s for s in snaps}
    monkeypatch.setattr(op, "active_instances",
                        lambda: [op.Instance(n) for n in byname])
    monkeypatch.setattr(op, "instance_snapshot",
                        lambda inst: byname[inst.display_name])
    monkeypatch.setattr(op, "_instance_summary", lambda snap: snap["name"])
    op.list_instances()
    out = capsys.readouterr().out
    assert "operator restart-loop --all" in out, (
        f"the {verdict} notice does not offer the sweep"
    )
    per_instance = [f"operator restart-loop {n}" for n in ("alpha", "beta", "gamma")]
    offered = [cmd for cmd in per_instance if cmd in out]
    assert offered == [], (
        f"the {verdict} notice still prints a command per instance: {offered}. "
        f"Three supervisors go stale together; three commands to type is how "
        f"one of them gets missed."
    )


def test_every_staleness_verdict_with_a_remedy_is_covered_here():
    """The parametrised list above must not fall behind the verdicts.

    A hand-written list is a list a newly added verdict is simply absent from,
    which is the shape of a guard that keeps passing over a set that no longer
    describes the code.
    """
    remediable = {op.CODE_STALE, op.CODE_UNRECORDED, op.CODE_MISMATCH}
    declared = {name: value for name, value in vars(op).items()
                if name.startswith("CODE_") and isinstance(value, str)}
    unhandled = set(declared.values()) - remediable - {op.CODE_CURRENT,
                                                      op.CODE_UNKNOWN}
    assert unhandled == set(), (
        f"these staleness verdicts have no remedy coverage: {sorted(unhandled)}. "
        f"Add them to the parametrised test above, or to the exempt set with a "
        f"reason."
    )
