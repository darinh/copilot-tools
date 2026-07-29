"""Tests for the operator CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import copilot_operator as op


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point all module-level state at a temp directory."""
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    return tmp_path


# ── instance identity ───────────────────────────────────────────
def test_instance_uses_safe_id_for_files():
    inst = op.Instance("my:proj")
    assert ":" not in inst.id
    assert inst.session == inst.id
    assert inst.display_name == "my:proj"


def test_distinct_names_get_distinct_state_files():
    a, b, c = op.Instance("a.b"), op.Instance("a:b"), op.Instance("a-b")
    paths = {a.state_file, b.state_file, c.state_file}
    assert len(paths) == 3


def test_simple_name_is_unchanged():
    assert op.Instance("frontend").id == "frontend"


# ── persisted state ─────────────────────────────────────────────
def test_state_roundtrip():
    inst = op.Instance("proj")
    inst.save_state(4, "2026-07-27T10:00:00Z", "3f2a9c1e-1111-2222-3333-444455556666")
    state = inst.load_state()
    assert state["SESSION_NUM"] == "4"
    assert state["RUN_STARTED"] == "2026-07-27T10:00:00Z"
    assert state["COPILOT_SESSION_ID"] == "3f2a9c1e-1111-2222-3333-444455556666"


def test_state_omits_blank_session_id():
    inst = op.Instance("proj")
    inst.save_state(1, "2026-07-27T10:00:00Z")
    assert "COPILOT_SESSION_ID" not in inst.state_file.read_text(encoding="utf-8")


def test_load_state_absent_returns_none():
    assert op.Instance("nothing").load_state() is None


def test_read_session_id_rejects_non_uuid():
    inst = op.Instance("proj")
    inst.session_file.write_text("not-a-uuid", encoding="utf-8")
    assert inst.read_session_id() == ""


def test_read_session_id_accepts_uuid():
    inst = op.Instance("proj")
    inst.session_file.write_text(
        "3f2a9c1e-1111-2222-3333-444455556666", encoding="utf-8")
    assert inst.read_session_id() == "3f2a9c1e-1111-2222-3333-444455556666"


# ── ownership ───────────────────────────────────────────────────
def test_claim_records_token_and_display_name():
    """An empty marker cannot prove which process owns a session."""
    inst = op.Instance("my.proj")
    inst.claim("tok123")
    owner = inst.ownership()
    assert owner["token"] == "tok123"
    assert owner["display_name"] == "my.proj"
    assert owner["session"] == inst.id


def test_ownership_none_when_unclaimed():
    assert op.Instance("unowned").ownership() is None


def test_managed_instances_lists_claimed(isolated_state):
    inst = op.Instance("alpha")
    inst.claim("t")
    found = op.managed_instances()
    assert inst.id in found
    assert found[inst.id]["display_name"] == "alpha"


def test_cleanup_removes_transient_files_but_keeps_state():
    inst = op.Instance("beta")
    inst.claim("t")
    inst.save_state(2, "2026-07-27T10:00:00Z")
    inst.restart_marker.touch()
    inst.pid_file.write_text("1", encoding="utf-8")
    inst.cleanup_files()
    assert not inst.managed_file.exists()
    assert not inst.restart_marker.exists()
    assert not inst.pid_file.exists()
    # State survives so a named loop can auto-continue after a restart.
    assert inst.state_file.exists()


# ── argument handling ───────────────────────────────────────────
@pytest.mark.parametrize("args,expected", [
    (["--agent", "anvil:anvil"], "anvil:anvil"),
    (["--agent=custom:x"], "custom:x"),
    (["--yolo"], "anvil:anvil"),
])
def test_extract_agent_from_args(args, expected):
    assert op.extract_agent_from_args(args) == expected


@pytest.mark.parametrize("args,expected", [
    (["--resume=abc"], True),
    (["--resume"], True),
    (["--continue"], True),
    (["--connect=x"], True),
    (["--yolo"], False),
    (["--resumearg"], False),
])
def test_args_have_explicit_session(args, expected):
    assert op.args_have_explicit_session(args) is expected


@pytest.mark.parametrize("args,expected", [
    (["--agent", "x"], True), (["--agent=x"], True), (["--model", "y"], False),
])
def test_has_agent_flag(args, expected):
    assert op.has_agent_flag(args) is expected


# ── preamble ────────────────────────────────────────────────────
def test_preamble_is_platform_neutral():
    """The preamble is read by an agent that may be on Windows, so it must not
    prescribe a POSIX-only command such as `touch`."""
    text = op.build_preamble("anvil:anvil", op.Instance("proj"))
    assert "touch " not in text
    assert "handoff --instance proj" in text


def test_preamble_uses_display_name_not_internal_id():
    text = op.build_preamble("a:b", op.Instance("my.proj"))
    assert "my.proj" in text


# ── launch spec ─────────────────────────────────────────────────
def test_launch_spec_roundtrip(tmp_path):
    inst = op.Instance("proj")
    argv = ["copilot", "--yolo", "-i", "a preamble with 'quotes' and \"more\""]
    path = op.write_launch_spec(inst, argv, tmp_path, 3)
    spec = json.loads(path.read_text(encoding="utf-8"))
    assert spec["argv"] == argv
    assert spec["session_num"] == 3
    assert spec["instance"] == inst.id


def test_runner_argv_is_a_list_not_a_shell_string(tmp_path):
    argv = op.runner_argv(tmp_path / "spec.json")
    assert isinstance(argv, list)
    assert argv[0] == op.sys.executable
    assert argv[1].endswith("operator_runner.py")


# ── reports ─────────────────────────────────────────────────────
def test_report_without_database_is_actionable(capsys):
    assert op.report_metrics("summary") == 1
    assert "No metrics database" in capsys.readouterr().out


def test_unknown_report_type_lists_valid_ones(capsys, isolated_state):
    op.METRICS_DB.write_bytes(b"")
    assert op.report_metrics("bogus") == 1
    out = capsys.readouterr().out
    for kind in ("summary", "sessions", "models", "projects", "costs"):
        assert kind in out


def test_table_renders_headers_and_rows():
    rendered = op._table([("a", 1), ("bb", 22)], ["name", "count"])
    assert "name" in rendered and "count" in rendered
    assert "bb" in rendered


def test_table_handles_no_rows():
    assert op._table([], ["a"]) == "(no data)"


# ── dispatch ────────────────────────────────────────────────────
def test_help_exits_zero(capsys):
    assert op.main(["help"]) == 0
    assert "operator" in capsys.readouterr().out


def test_version(capsys):
    assert op.main(["version"]) == 0
    assert op.__version__ in capsys.readouterr().out


def test_reserved_words_are_not_instance_names():
    for word in ("stop", "list", "report", "ingest", "help", "join", "reload"):
        assert word in op.RESERVED_WORDS


def test_stop_unknown_instance_reports_error(monkeypatch, capsys):
    monkeypatch.setattr(op.MUX, "available", lambda: False)
    assert op.stop_operator("ghost") == 1


def test_stop_with_nothing_running_is_success(monkeypatch, capsys):
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [])
    assert op.stop_operator() == 0
    assert "No running operator instances" in capsys.readouterr().out


def test_list_with_nothing_running(monkeypatch, capsys):
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [])
    assert op.list_instances() == 0
    assert "(none)" in capsys.readouterr().out


def test_list_excludes_foreign_sessions(monkeypatch, capsys):
    """A session the operator did not create must never be listed."""
    inst = op.Instance("mine")
    inst.claim("t")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id, "someone-elses"])
    op.list_instances()
    out = capsys.readouterr().out
    assert "mine" in out
    assert "someone-elses" not in out


def test_stop_all_ignores_foreign_sessions(monkeypatch):
    inst = op.Instance("mine")
    inst.claim("t")
    killed = []
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id, "foreign"])
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s))
    op.stop_operator()
    assert killed == [inst.id]


def test_is_copilot_running_treats_dead_pane_as_stopped(monkeypatch):
    """Loop mode sets remain-on-exit, so has_session stays true after the
    program exits. Ignoring pane_dead lets the loop poll forever."""
    inst = op.Instance("proj")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda s: True)
    assert op.is_copilot_running(inst) is False


def test_is_copilot_running_true_while_alive(monkeypatch):
    inst = op.Instance("proj")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda s: False)
    assert op.is_copilot_running(inst) is True


def test_is_copilot_running_false_when_exit_marker_present(monkeypatch):
    inst = op.Instance("proj")
    inst.exit_file.write_text("0", encoding="utf-8")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda s: False)
    assert op.is_copilot_running(inst) is False


def test_reload_without_name_errors(capsys):
    assert op.reload_instance(None) == 1


def test_reload_rebuilds_preamble(tmp_path, isolated_state):
    inst = op.Instance("proj")
    op.write_launch_spec(
        inst, ["copilot", "--agent", "anvil:anvil", "-i", "old preamble"], tmp_path, 1)
    assert op.reload_instance("proj") == 0
    spec = json.loads(inst.spec_file.read_text(encoding="utf-8"))
    assert spec["argv"][-2] == "-i"
    assert "blanket human approval" in spec["argv"][-1]
    assert "--effort" in spec["argv"]
