"""Tests for the tab registry and `operator restore`."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import copilot_operator as op


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
    return tmp_path


# ── registration ────────────────────────────────────────────────
def test_register_tab_noop_without_wt_session(monkeypatch):
    monkeypatch.delenv("WT_SESSION", raising=False)
    inst = op.Instance("proj")
    op.register_tab(inst, False, ["--agent", "anvil:anvil"], Path("/tmp/proj"))
    assert op.load_tabs() == {}


def test_register_tab_records_entry_inside_windows_terminal(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    inst = op.Instance("proj")
    op.register_tab(inst, True, ["--agent", "anvil:anvil"], Path("/tmp/proj"))
    entries = op.load_tabs()
    assert list(entries.keys()) == [inst.id]
    entry = entries[inst.id]
    assert entry["display_name"] == "proj"
    assert entry["argv"] == ["--loop", "--name", "proj", "--agent", "anvil:anvil"]
    assert entry["cwd"] == str(Path("/tmp/proj"))
    assert entry["wsl_distro"] == ""


def test_register_tab_records_wsl_distro(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("/home/me/proj"))
    entry = op.load_tabs()[inst.id]
    assert entry["wsl_distro"] == "Ubuntu"


def test_register_tab_overwrites_previous_entry_for_same_instance(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, False, ["--agent", "a"], Path("/tmp/proj"))
    op.register_tab(inst, True, ["--agent", "b"], Path("/tmp/proj"))
    entries = op.load_tabs()
    assert len(entries) == 1
    assert entries[inst.id]["argv"] == ["--loop", "--name", "proj", "--agent", "b"]


def test_remove_tab_deletes_entry(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("/tmp/proj"))
    op.remove_tab(inst.id)
    assert op.load_tabs() == {}


def test_remove_tab_missing_entry_is_a_noop():
    op.remove_tab("does-not-exist")
    assert op.load_tabs() == {}


def test_load_tabs_survives_corrupt_file(tmp_path):
    op.TABS_FILE.write_text("not json", encoding="utf-8")
    assert op.load_tabs() == {}


def test_load_tabs_rejects_non_dict_json(tmp_path):
    op.TABS_FILE.write_text("[1, 2, 3]", encoding="utf-8")
    assert op.load_tabs() == {}


# ── stop / forget prune the registry ────────────────────────────
def test_stop_named_instance_removes_tab_entry(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("/tmp/proj"))
    inst.save_state(1, "2026-07-27T10:00:00Z")
    assert op.forget_instance("proj") == 0
    assert op.load_tabs() == {}


# ── operator tabs subcommand ─────────────────────────────────────
def test_manage_tabs_list_when_empty(capsys):
    assert op.manage_tabs([]) == 0
    assert "No tracked tabs" in capsys.readouterr().out


def test_manage_tabs_list_shows_entry(monkeypatch, capsys):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, True, ["--agent", "anvil:anvil"], Path("/tmp/proj"))
    out = op.manage_tabs(["list"])
    assert out == 0
    captured = capsys.readouterr().out
    assert "proj" in captured
    assert "operator --loop --name proj --agent anvil:anvil" in captured


def test_manage_tabs_remove(monkeypatch, capsys):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("/tmp/proj"))
    assert op.manage_tabs(["remove", "proj"]) == 0
    assert op.load_tabs() == {}


def test_manage_tabs_remove_unknown_fails(capsys):
    assert op.manage_tabs(["remove", "nope"]) == 1


def test_manage_tabs_clear(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("/tmp/proj"))
    assert op.manage_tabs(["clear"]) == 0
    assert op.load_tabs() == {}


def test_manage_tabs_unknown_subcommand():
    assert op.manage_tabs(["bogus"]) == 1


# ── restore: platform / dependency gating ───────────────────────
def test_restore_requires_windows(monkeypatch):
    monkeypatch.setattr(op, "IS_WINDOWS", False)
    with pytest.raises(SystemExit):
        op.restore_tabs([])


def test_restore_reports_nothing_to_do(monkeypatch, capsys):
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    assert op.restore_tabs([]) == 0
    assert "No tracked tabs to restore" in capsys.readouterr().out


# ── restore: command construction ───────────────────────────────
def test_build_wt_command_native_entry(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("local:a", {
        "display_name": "proj",
        "cwd": "C:\\Users\\me\\proj",
        "argv": ["--loop", "--name", "proj", "--agent", "anvil:anvil"],
        "wsl_distro": "",
    })]
    cmd = op._build_wt_command(entries)
    assert cmd[0] == "wt.exe"
    assert "new-tab" in cmd
    assert "-d" in cmd
    joined = " ".join(cmd)
    assert "operator --loop --name proj --agent anvil:anvil" in joined
    assert "wsl.exe" not in joined


def test_build_wt_command_wsl_entry(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("Ubuntu:a", {
        "display_name": "proj",
        "cwd": "/home/me/proj",
        "argv": ["--name", "proj"],
        "wsl_distro": "Ubuntu",
    })]
    cmd = op._build_wt_command(entries)
    joined = " ".join(cmd)
    assert "wsl.exe" in joined
    assert "Ubuntu" in joined
    assert "/home/me/proj" in joined
    assert "operator --name proj" in joined


def test_build_wt_command_chains_multiple_tabs_with_semicolon(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [
        ("local:a", {"display_name": "a", "cwd": "C:\\a", "argv": [], "wsl_distro": ""}),
        ("local:b", {"display_name": "b", "cwd": "C:\\b", "argv": [], "wsl_distro": ""}),
    ]
    cmd = op._build_wt_command(entries)
    assert cmd.count("new-tab") == 2
    assert ";" in cmd


def test_build_wt_command_dies_without_wt(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        op._build_wt_command([("local:a", {"display_name": "a", "cwd": "", "argv": [],
                                           "wsl_distro": ""})])


# ── restore: dry-run end to end ─────────────────────────────────
def test_restore_dry_run_does_not_launch(monkeypatch, capsys):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, True, ["--agent", "anvil:anvil"], Path("C:\\proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)

    launched = []
    monkeypatch.setattr(op.subprocess, "Popen", lambda *a, **k: launched.append((a, k)))

    assert op.restore_tabs(["--dry-run", "--all"]) == 0
    assert launched == []
    out = capsys.readouterr().out
    assert "not launching" in out
    assert "proj" in out


def test_restore_all_launches_wt(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("C:\\proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)

    launched = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            launched.append(cmd)

    monkeypatch.setattr(op.subprocess, "Popen", _FakePopen)

    assert op.restore_tabs(["--all"]) == 0
    assert len(launched) == 1
    assert launched[0][0] == "wt.exe"


def test_restore_by_name_launches_only_that_entry(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    op.register_tab(op.Instance("proj"), False, [], Path("C:\\proj"))
    op.register_tab(op.Instance("other"), False, [], Path("C:\\other"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)

    launched = []
    monkeypatch.setattr(op.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))

    assert op.restore_tabs(["proj"]) == 0
    assert len(launched) == 1
    assert "proj" in " ".join(launched[0])
    assert "other" not in " ".join(launched[0])


def test_restore_by_unknown_name_fails(monkeypatch, capsys):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    op.register_tab(op.Instance("proj"), False, [], Path("C:\\proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])

    assert op.restore_tabs(["nonexistent"]) == 1
    assert "No tracked tab(s): nonexistent" in capsys.readouterr().err


def test_restore_with_no_args_prompts_and_restores_selection(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    op.register_tab(op.Instance("proj"), False, [], Path("C:\\proj"))
    op.register_tab(op.Instance("other"), False, [], Path("C:\\other"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    monkeypatch.setattr(op, "_prompt_selection", lambda n: "1")

    launched = []
    monkeypatch.setattr(op.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))

    assert op.restore_tabs([]) == 0
    assert len(launched) == 1


def test_restore_with_no_args_prompts_all(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    op.register_tab(op.Instance("proj"), False, [], Path("C:\\proj"))
    op.register_tab(op.Instance("other"), False, [], Path("C:\\other"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    monkeypatch.setattr(op, "_prompt_selection", lambda n: "all")

    cmds = []
    monkeypatch.setattr(op.subprocess, "Popen", lambda cmd, **k: cmds.append(cmd))

    assert op.restore_tabs([]) == 0
    assert len(cmds) == 1
    assert cmds[0].count("new-tab") == 2


def test_restore_blank_prompt_cancels(monkeypatch, capsys):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    op.register_tab(op.Instance("proj"), False, [], Path("C:\\proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op, "_prompt_selection", lambda n: "")

    launched = []
    monkeypatch.setattr(op.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))

    assert op.restore_tabs([]) == 0
    assert launched == []
    assert "Cancelled" in capsys.readouterr().out


def test_parse_selection_variants():
    assert op._parse_selection("1,2", 3) == [0, 1]
    assert op._parse_selection("all", 3) == [0, 1, 2]
    assert op._parse_selection("", 3) == []
    assert op._parse_selection("bogus", 3) == []
    assert op._parse_selection("99", 3) == []


def test_restore_merges_wsl_registry(monkeypatch):
    monkeypatch.setattr(op, "IS_WINDOWS", True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: ["Ubuntu"])
    remote_data = {
        "wsl-inst": {
            "display_name": "wslproj",
            "cwd": "/home/me/wslproj",
            "argv": ["--name", "wslproj"],
            "wsl_distro": "Ubuntu",
        }
    }
    monkeypatch.setattr(op, "_read_remote_tabs", lambda distro: remote_data if distro == "Ubuntu" else {})
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)

    assert op.restore_tabs(["--dry-run", "--all"]) == 0


# ── default instance naming conflict resolution ─────────────────
def test_default_instance_name_no_conflict(tmp_path, monkeypatch):
    proj = tmp_path / "myproj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert op.default_instance_name() == "myproj"


def test_default_instance_name_appends_suffix_on_live_conflict(tmp_path, monkeypatch):
    proj_a = tmp_path / "a" / "backend"
    proj_b = tmp_path / "b" / "backend"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)

    inst = op.Instance("backend")
    inst.spec_file.write_text(json.dumps({"cwd": str(proj_a)}), encoding="utf-8")

    class _FakeMux:
        def available(self_inner):
            return True

        def has_session(self_inner, session_id):
            return session_id == inst.session

    monkeypatch.setattr(op, "MUX", _FakeMux())
    monkeypatch.chdir(proj_b)
    assert op.default_instance_name() == "backend-1"


def test_default_instance_name_reuses_name_for_same_directory(tmp_path, monkeypatch):
    proj = tmp_path / "backend"
    proj.mkdir()

    inst = op.Instance("backend")
    inst.spec_file.write_text(json.dumps({"cwd": str(proj)}), encoding="utf-8")

    class _FakeMux:
        def available(self_inner):
            return True

        def has_session(self_inner, session_id):
            return session_id == inst.session

    monkeypatch.setattr(op, "MUX", _FakeMux())
    monkeypatch.chdir(proj)
    assert op.default_instance_name() == "backend"


def test_default_instance_name_ignores_conflict_when_not_live(tmp_path, monkeypatch):
    proj_a = tmp_path / "a" / "backend"
    proj_b = tmp_path / "b" / "backend"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)

    inst = op.Instance("backend")
    inst.spec_file.write_text(json.dumps({"cwd": str(proj_a)}), encoding="utf-8")

    class _FakeMux:
        def available(self_inner):
            return True

        def has_session(self_inner, session_id):
            return False  # nothing actually running

    monkeypatch.setattr(op, "MUX", _FakeMux())
    monkeypatch.chdir(proj_b)
    assert op.default_instance_name() == "backend"


def test_read_remote_tabs_handles_missing_wsl(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: None)
    assert op._read_remote_tabs("Ubuntu") == {}


def test_read_remote_tabs_handles_nonzero_exit(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wsl.exe")

    class _Result:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _Result())
    assert op._read_remote_tabs("Ubuntu") == {}


def test_read_remote_tabs_parses_json(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wsl.exe")
    payload = json.dumps({"x": {"display_name": "x", "cwd": "/x", "argv": [],
                                "wsl_distro": "Ubuntu"}}).encode("utf-8")

    class _Result:
        returncode = 0
        stdout = payload

    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _Result())
    data = op._read_remote_tabs("Ubuntu")
    assert data["x"]["display_name"] == "x"


def test_wsl_distros_returns_empty_without_wsl(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: None)
    assert op._wsl_distros() == []


def test_wsl_distros_decodes_utf16(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wsl.exe")

    class _Result:
        returncode = 0
        stdout = "Ubuntu\r\n\x00Debian\r\n\x00".encode("utf-16-le")

    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _Result())
    names = op._wsl_distros()
    assert "Ubuntu" in names
    assert "Debian" in names
