"""Tests for the tab registry and `operator restore`."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
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


# ── is_wsl() detection ───────────────────────────────────────────
def test_is_wsl_true_from_env_var(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert op.is_wsl() is True


def test_is_wsl_true_from_proc_version(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    def fake_read_text(self, **k):
        if self.as_posix() == "/proc/version":
            return "Linux version ... Microsoft ..."
        raise OSError("no such file")

    monkeypatch.setattr(op.Path, "read_text", fake_read_text)
    assert op.is_wsl() is True


def test_is_wsl_false_on_plain_linux(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)

    def fake_read_text(self, **k):
        raise OSError("no such file")

    monkeypatch.setattr(op.Path, "read_text", fake_read_text)
    assert op.is_wsl() is False


def test_is_wsl_false_when_proc_version_lacks_microsoft(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(op.Path, "read_text", lambda self, **k: "Linux version 6.8.0-generic")
    assert op.is_wsl() is False


# ── restore: platform / dependency gating ───────────────────────
def test_restore_dies_on_plain_linux_without_wsl(monkeypatch):
    monkeypatch.setattr(op, "IS_WINDOWS", False)
    monkeypatch.setattr(op, "is_wsl", lambda: False)
    with pytest.raises(SystemExit):
        op.restore_tabs([])


def test_restore_dies_from_wsl_without_wt_reachable(monkeypatch):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    op.register_tab(op.Instance("proj"), False, [], Path("/home/me/proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", False)
    monkeypatch.setattr(op, "is_wsl", lambda: True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: [])
    monkeypatch.setattr(op.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        op.restore_tabs(["--all"])


def test_restore_works_from_wsl_when_wt_reachable(monkeypatch, capsys):
    monkeypatch.setenv("WT_SESSION", "some-guid")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    inst = op.Instance("proj")
    expected_cwd = str(Path("/home/me/proj"))
    op.register_tab(inst, False, [], Path("/home/me/proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", False)
    monkeypatch.setattr(op, "is_wsl", lambda: True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: ["Ubuntu"])
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)

    launched = []
    monkeypatch.setattr(op.subprocess, "Popen", lambda cmd, **k: launched.append(cmd))

    assert op.restore_tabs(["--all"]) == 0
    assert len(launched) == 1
    joined = " ".join(launched[0])
    assert "Ubuntu" in joined
    assert expected_cwd in joined
    out = capsys.readouterr().out
    assert "Running inside WSL" in out


def test_restore_wsl_does_not_double_count_current_distro(monkeypatch):
    """`_wsl_distros()` enumerates every installed distro, including the one
    this process is itself running inside. Its entries are already covered by
    `load_tabs()` (the local registry), so re-reading it via `wsl.exe -d
    <this-distro>` must not duplicate every tracked tab."""
    monkeypatch.setenv("WT_SESSION", "some-guid")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    inst = op.Instance("proj")
    op.register_tab(inst, False, [], Path("/home/me/proj"))
    monkeypatch.setattr(op, "IS_WINDOWS", False)
    monkeypatch.setattr(op, "is_wsl", lambda: True)
    monkeypatch.setattr(op, "_wsl_distros", lambda: ["Ubuntu", "Debian"])

    remote_calls = []

    def fake_read_remote(distro):
        remote_calls.append(distro)
        return {}

    monkeypatch.setattr(op, "_read_remote_tabs", fake_read_remote)

    entries = op._collect_tab_entries()
    assert remote_calls == ["Debian"], "the current distro must not be re-queried remotely"
    assert len(entries) == 1
    assert entries[0][0] == f"local:{inst.id}"


def test_restore_native_windows_still_merges_all_wsl_distros(monkeypatch):
    """Sanity check that the current-distro skip is a no-op on native
    Windows, where WSL_DISTRO_NAME is never set."""
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(op, "_wsl_distros", lambda: ["Ubuntu", "Debian"])
    monkeypatch.setattr(op, "_read_remote_tabs", lambda distro: {
        f"{distro}-inst": {"display_name": distro, "cwd": f"/home/me/{distro}",
                           "argv": [], "wsl_distro": distro}
    })
    entries = op._collect_tab_entries()
    idents = [ident for ident, _ in entries]
    assert idents == ["Ubuntu:Ubuntu-inst", "Debian:Debian-inst"]


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


def test_build_wt_command_preserves_argument_boundaries_native(monkeypatch):
    """A tracked argument containing spaces must survive the round trip."""
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("local:a", {
        "display_name": "my project",
        "cwd": "C:\\Users\\me\\my proj",
        "argv": ["--loop", "--name", "my project", "--allow-tool", "shell(git status)"],
        "wsl_distro": "",
    })]
    inner = op._build_wt_command(entries)[-1]
    assert inner == ("operator --loop --name 'my project' "
                     "--allow-tool 'shell(git status)'")


def test_build_wt_command_preserves_argument_boundaries_wsl(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("Ubuntu:a", {
        "display_name": "my project",
        "cwd": "/home/me/my proj",
        "argv": ["--name", "my project", "--banner", "it's here"],
        "wsl_distro": "Ubuntu",
    })]
    inner = op._build_wt_command(entries)[-1]
    assert shlex.split(inner) == ["operator", "--name", "my project",
                                  "--banner", "it's here"]


@pytest.mark.parametrize("argv", [
    [],
    ["--loop", "--name", "proj"],
    ["--name", "my project", "--banner", "it's here", "--path", "C:\\tmp\\a b"],
    ["--empty", "", "--quote", 'say "hi"', "--dollar", "$HOME", "--back", "a`b"],
])
def test_relaunch_command_round_trips_through_a_posix_parser(argv):
    """shlex is the reference POSIX parser `bash -lic` will use."""
    assert shlex.split(op._relaunch_command(argv, shlex.quote)) == ["operator", *argv]


def test_ps_quote_leaves_ordinary_arguments_bare():
    for arg in ["--loop", "proj", "anvil:anvil", "C:\\Users\\me\\proj", "a.b-c_d"]:
        assert op._ps_quote(arg) == arg


@pytest.mark.parametrize("arg,expected", [
    ("my project", "'my project'"),
    ("it's here", "'it''s here'"),
    ("", "''"),
    ("shell(git status)", "'shell(git status)'"),
    ('say "hi"', "'say \"hi\"'"),
    ("$HOME", "'$HOME'"),
    ("#comment", "'#comment'"),
    ("@args", "'@args'"),
    ("a,b", "'a,b'"),
])
def test_ps_quote_wraps_arguments_powershell_would_reparse(arg, expected):
    assert op._ps_quote(arg) == expected


def test_build_wt_command_survives_a_non_list_argv(monkeypatch):
    """A hand-edited registry must not relaunch one character per argument."""
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("local:a", {"display_name": "a", "cwd": "C:\\a",
                            "argv": "--loop --name a", "wsl_distro": ""})]
    assert op._build_wt_command(entries)[-1] == "operator"


_PS = shutil.which("powershell") or shutil.which("pwsh")

_PS_PARSE = """
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $env:OPERATOR_PS_INPUT, [ref]$null, [ref]$null)
$cmds = @($ast.FindAll({
    param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true))
if ($cmds.Count -ne 1) { exit 2 }
ConvertTo-Json -Compress -InputObject @(
    $cmds[0].CommandElements | ForEach-Object { $_.Value })
"""


@pytest.mark.skipif(_PS is None, reason="no PowerShell available")
def test_ps_quote_round_trips_through_the_real_powershell_parser(monkeypatch):
    """The only authority on PowerShell quoting is PowerShell's own parser."""
    argv = ["--loop", "--name", "my project", "--allow-tool", "shell(git status)",
            "--banner", "it's here", "--path", "C:\\tmp\\a b", "--dollar", "$HOME",
            "--empty", "", "--quote", 'say "hi"', "--back", "a`b",
            "--hash", "#comment", "--splat", "@args", "--comma", "a,b",
            "--pct", "%", "--tilde", "~", "--bang", "!", "--caret", "^",
            "--star", "*", "--question", "?", "--brace", "{x}",
            "--amp", "&", "--pipe", "|", "--lt", "<", "--gt", ">"]
    env = {**os.environ,
           "OPERATOR_PS_INPUT": op._relaunch_command(argv, op._ps_quote)}
    proc = subprocess.run([_PS, "-NoProfile", "-NonInteractive", "-Command", _PS_PARSE],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["operator", *argv]


_ARGDUMP = "import json, sys; print(json.dumps(sys.argv[1:]))"


@pytest.mark.skipif(_PS is None, reason="no PowerShell available")
def test_relaunch_command_delivers_arguments_to_a_native_process(tmp_path):
    """End to end: what PowerShell actually hands the `operator` executable.

    Arguments that are empty or contain a literal double quote are excluded on
    purpose -- Windows PowerShell drops or mangles those on the way into any
    native process, which `_ps_quote` cannot repair (see its docstring).
    """
    dump = tmp_path / "argdump.py"
    dump.write_text(_ARGDUMP, encoding="utf-8")
    argv = ["--loop", "--name", "my project", "--allow-tool", "shell(git status)",
            "--banner", "it's here", "--path", "C:\\tmp\\a b", "--dollar", "$HOME",
            "--back", "a`b", "--hash", "#comment",
            "--splat", "@args", "--comma", "a,b", "--brace", "{x}",
            "--amp", "&", "--pipe", "|", "--gt", ">", "--pct", "%"]
    inner = op._relaunch_command(argv, op._ps_quote)
    script = (f"& {op._ps_quote(sys.executable)} {op._ps_quote(str(dump))} "
              + inner[len("operator "):])
    proc = subprocess.run([_PS, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == argv


@pytest.mark.parametrize("raw,expected", [
    ("$HOME", "\\$HOME"),
    ("`echo x`", "\\`echo x\\`"),
    ("$(echo x)", "\\$(echo x)"),
    ("a\\b", "a\\\\b"),
    ("plain", "plain"),
    ("\\$HOME", "\\\\\\$HOME"),
])
def test_wsl_escape_neutralises_the_expansion_pass(raw, expected):
    assert op._wsl_escape(raw) == expected


def test_build_wt_command_escapes_expansion_on_the_wsl_path(monkeypatch):
    """A tracked `$(...)` argument must not run during restore."""
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("Ubuntu:a", {
        "display_name": "a", "cwd": "/home/me", "wsl_distro": "Ubuntu",
        "argv": ["--name", "$(rm -rf ~)"],
    })]
    inner = op._build_wt_command(entries)[-1]
    assert "\\$(rm -rf ~)" in inner
    assert "$(rm" not in inner.replace("\\$(rm", "")


def test_build_wt_command_coerces_non_string_metadata(monkeypatch):
    """Hand-edited JSON must not reach subprocess as a dict."""
    monkeypatch.setattr(op.shutil, "which", lambda name: "wt.exe" if "wt" in name else None)
    entries = [("local:a", {"display_name": {"a": 1}, "cwd": ["x"],
                            "wsl_distro": 7, "argv": ["--loop"]})]
    cmd = op._build_wt_command(entries)
    assert all(isinstance(part, str) for part in cmd)
    assert "operator --loop" in cmd


def _wsl_works() -> bool:
    exe = shutil.which("wsl.exe")
    if not exe:
        return False
    try:
        proc = subprocess.run([exe, "--", "bash", "-lic", "echo ok"],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "ok" in proc.stdout


_WSL_OK = _wsl_works()


@pytest.mark.skipif(not _WSL_OK, reason="no working WSL distro")
def test_wsl_transport_delivers_arguments_without_expanding_them():
    """End to end: a hostile tracked argument reaches the process unexecuted."""
    argv = ["--sub", "$(echo PWNED)", "--tick", "`echo PWNED`", "--home", "$HOME",
            "--name", "my project", "--quote", "it's here", "--star", "*",
            "--semi", "a;b", "--back", "a\\b", "--tail"]
    inner = op._wsl_escape(op._relaunch_command(argv, shlex.quote))
    dump = inner.replace("operator", "printf '%s\\n'", 1)
    proc = subprocess.run([shutil.which("wsl.exe"), "--", "bash", "-lic", dump],
                          capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode("utf-8", "replace").splitlines() == argv


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


# ── background loop + auto-attach ────────────────────────────────
def test_start_and_attach_loop_spawns_when_no_supervisor_running(monkeypatch):
    inst = op.Instance("fresh-loop")
    spawned = {}

    def fake_spawn(instance, copilot_args, is_fresh):
        spawned["called"] = (instance.display_name, copilot_args, is_fresh)
        return 4242

    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: None)
    monkeypatch.setattr(op, "_spawn_background_loop", fake_spawn)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    attached = []
    monkeypatch.setattr(op.MUX, "attach", lambda session: attached.append(session))

    rc = op.start_and_attach_loop(inst, ["--agent", "a:b"], is_fresh=True)

    assert rc == 0
    assert spawned["called"] == ("fresh-loop", ["--agent", "a:b"], True)
    assert attached == [inst.session]


def test_start_and_attach_loop_reuses_existing_supervisor(monkeypatch):
    inst = op.Instance("already-running")
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: 1234)

    spawn_calls = []
    monkeypatch.setattr(op, "_spawn_background_loop",
                        lambda *a, **k: spawn_calls.append(1))
    monkeypatch.setattr(op.MUX, "has_session", lambda session: True)
    attached = []
    monkeypatch.setattr(op.MUX, "attach", lambda session: attached.append(session))

    rc = op.start_and_attach_loop(inst, [], is_fresh=False)

    assert rc == 0
    assert spawn_calls == [], "an already-running supervisor must not be duplicated"
    assert attached == [inst.session]


def test_start_and_attach_loop_times_out_if_session_never_appears(monkeypatch):
    inst = op.Instance("never-starts")
    monkeypatch.setattr(op, "_running_loop_pid", lambda instance: None)
    monkeypatch.setattr(op, "_spawn_background_loop", lambda *a, **k: 1)
    monkeypatch.setattr(op, "SESSION_ID_WAIT", 0)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)

    rc = op.start_and_attach_loop(inst, [], is_fresh=False)

    assert rc == 1


def test_spawn_background_loop_builds_supervise_command(monkeypatch, tmp_path):
    inst = op.Instance("cmdcheck")
    captured = {}

    class FakeProc:
        pid = 5555

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(op.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)

    pid = op._spawn_background_loop(inst, ["--agent", "x:y"], is_fresh=True)

    assert pid == 5555
    cmd = captured["cmd"]
    assert "--_supervise" in cmd
    assert "--loop" in cmd
    assert "--name" in cmd and "cmdcheck" in cmd
    assert "--fresh" in cmd
    assert "--agent" in cmd and "x:y" in cmd
    assert captured["kwargs"]["stdin"] == op.subprocess.DEVNULL


def test_spawn_background_loop_uses_no_window_not_detached(monkeypatch, tmp_path):
    """Regression: a stray console window popped up on `operator --loop`.

    DETACHED_PROCESS leaves the supervisor with no console at all, so when
    sys.executable is a venv/Store *shim* it re-execs the real python.exe and
    Windows hands that child a brand new **visible** console. CREATE_NO_WINDOW
    gives the supervisor an invisible console that all descendants inherit.
    """
    if not op.IS_WINDOWS:
        pytest.skip("creationflags are Windows-only")

    inst = op.Instance("flagcheck")
    captured = {}

    class FakeProc:
        pid = 1234

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(op.subprocess, "Popen", fake_popen)
    monkeypatch.chdir(tmp_path)
    op._spawn_background_loop(inst, [], is_fresh=False)

    flags = captured["kwargs"]["creationflags"]
    assert flags & op.subprocess.CREATE_NO_WINDOW
    assert flags & op.subprocess.CREATE_NEW_PROCESS_GROUP
    # DETACHED_PROCESS is what caused the visible window; it must be gone.
    assert not flags & 0x00000008


def test_wsl_probes_suppress_console_window(monkeypatch):
    """The wsl.exe probes run from the same console-less contexts.

    Injects a sentinel rather than asserting the constant's own value, so the
    wiring is verified on every platform instead of restating the
    implementation's platform branch.
    """
    monkeypatch.setattr(op, "NO_WINDOW_KWARGS", {"creationflags": 0xABCD})
    monkeypatch.setattr(op.shutil, "which", lambda name: "wsl.exe")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return op.subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._wsl_distros()
    assert seen["creationflags"] == 0xABCD
    # CREATE_NO_WINDOW rebinds std handles, so capture must stay explicit or
    # the probe's output would vanish into the hidden console.
    assert seen["capture_output"] is True


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


class _FakeStdout:
    """Captures what the operator writes to the terminal."""

    def __init__(self, tty=True):
        self.text = ""
        self._tty = tty

    def isatty(self):
        return self._tty

    def write(self, data):
        self.text += data

    def flush(self):
        pass


def test_loop_mode_shows_the_animated_tab_ring(monkeypatch):
    """State 3 is the indeterminate ring Windows Terminal animates."""
    out = _FakeStdout()
    monkeypatch.setattr(op.sys, "stdout", out)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("OPERATOR_NO_TAB_PROGRESS", raising=False)
    op.set_tab_progress(op.TAB_LOOPING)
    assert out.text == "\033]9;4;3;100\007"


def test_single_session_ring_differs_from_the_loop_ring(monkeypatch):
    out = _FakeStdout()
    monkeypatch.setattr(op.sys, "stdout", out)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("OPERATOR_NO_TAB_PROGRESS", raising=False)
    op.set_tab_progress(op.TAB_STEADY)
    assert out.text == "\033]9;4;1;100\007"
    assert op.TAB_STEADY != op.TAB_LOOPING


def test_tab_ring_is_wrapped_for_tmux(monkeypatch):
    """tmux discards sequences it does not implement unless they are wrapped
    in its DCS passthrough, with every ESC doubled."""
    out = _FakeStdout()
    monkeypatch.setattr(op.sys, "stdout", out)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.delenv("OPERATOR_NO_TAB_PROGRESS", raising=False)
    op.set_tab_progress(op.TAB_LOOPING)
    assert out.text == "\033Ptmux;\033\033]9;4;3;100\007\033\\"


def test_tab_ring_is_silent_when_not_a_terminal(monkeypatch):
    out = _FakeStdout(tty=False)
    monkeypatch.setattr(op.sys, "stdout", out)
    monkeypatch.delenv("OPERATOR_NO_TAB_PROGRESS", raising=False)
    op.set_tab_progress(op.TAB_LOOPING)
    assert out.text == ""


def test_tab_ring_can_be_switched_off(monkeypatch):
    out = _FakeStdout()
    monkeypatch.setattr(op.sys, "stdout", out)
    monkeypatch.setenv("OPERATOR_NO_TAB_PROGRESS", "1")
    op.set_tab_progress(op.TAB_LOOPING)
    assert out.text == ""


def test_clearing_the_ring_uses_the_hide_state(monkeypatch):
    out = _FakeStdout()
    monkeypatch.setattr(op.sys, "stdout", out)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("OPERATOR_NO_TAB_PROGRESS", raising=False)
    op.clear_tab_progress()
    assert out.text == "\033]9;4;0;0\007"
