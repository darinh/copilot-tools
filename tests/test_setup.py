"""Tests for cross-platform setup."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import install_manifest
import setup_tools


def _make_dir_link(target: Path, link: Path) -> None:
    """Create a directory link at ``link`` -> ``target``, or skip the test.

    Tries a real symlink first and falls back to a junction, because Windows
    refuses ``os.symlink`` without Developer Mode or elevation while ``mklink
    /J`` needs neither. A platform that will do neither cannot exercise the
    behaviour under test at all, so skipping is honest where xfail would not be.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode == 0:
            return
    pytest.skip("this platform will not create directory links")


def _link_destination(link: Path) -> str | None:
    """Where ``link`` points, or None when it is not a link.

    Deliberately a local copy rather than an import of the production helper:
    these tests must be able to fail against the *unfixed* module, and a test
    that errors because a new symbol does not exist yet proves nothing about
    behaviour.
    """
    try:
        return os.readlink(str(link))
    except OSError:
        return None


def test_dirs_match_identical(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "sub").mkdir(parents=True)
        (d / "f.txt").write_text("same", encoding="utf-8")
        (d / "sub" / "g.txt").write_text("also", encoding="utf-8")
    assert setup_tools._dirs_match(a, b) is True


def test_dirs_match_detects_content_difference(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("one", encoding="utf-8")
    (b / "f.txt").write_text("two", encoding="utf-8")
    assert setup_tools._dirs_match(a, b) is False


def test_dirs_match_detects_extra_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f.txt").write_text("x", encoding="utf-8")
    (b / "f.txt").write_text("x", encoding="utf-8")
    (b / "extra.txt").write_text("y", encoding="utf-8")
    assert setup_tools._dirs_match(a, b) is False


def test_link_directory_preserves_user_edits_without_consent(tmp_path, monkeypatch):
    """A real directory may hold edits the user made in place. Setup must not
    delete it without asking."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "extension.mjs").write_text("repo version", encoding="utf-8")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "extension.mjs").write_text("MY LOCAL EDITS", encoding="utf-8")

    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: False)
    result = setup_tools._link_directory(src, dest)

    assert "skipped" in result
    assert (dest / "extension.mjs").read_text(encoding="utf-8") == "MY LOCAL EDITS"


def test_link_directory_replaces_when_consented(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "extension.mjs").write_text("repo version", encoding="utf-8")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "extension.mjs").write_text("old", encoding="utf-8")

    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: True)
    result = setup_tools._link_directory(src, dest)

    assert "replaced" in result
    assert (dest / "extension.mjs").read_text(encoding="utf-8") == "repo version"


def test_link_directory_noop_when_already_identical(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.mjs").write_text("same", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "f.mjs").write_text("same", encoding="utf-8")

    def refuse(*a, **k):
        raise AssertionError("must not prompt when contents already match")

    monkeypatch.setattr(setup_tools, "ask", refuse)
    assert setup_tools._link_directory(src, dest) == "already up to date"


def test_link_directory_creates_link_when_absent(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.mjs").write_text("x", encoding="utf-8")
    dest = tmp_path / "dest"

    result = setup_tools._link_directory(src, dest)
    assert result in ("junction created", "symlink created",
                      "copied (junction unavailable)")
    assert (dest / "f.mjs").read_text(encoding="utf-8") == "x"


def test_link_directory_keeps_a_user_link_without_consent(tmp_path, monkeypatch):
    """A link the user pointed somewhere else is theirs, exactly as much as a
    directory of edits is. Refusing must leave the link *and its target* alone,
    not merely return the right string after already unlinking."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "extension.mjs").write_text("repo version", encoding="utf-8")

    mine = tmp_path / "my-working-copy"
    mine.mkdir()
    (mine / "extension.mjs").write_text("MY LOCAL EDITS", encoding="utf-8")

    dest = tmp_path / "dest"
    _make_dir_link(mine, dest)

    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: False)
    result = setup_tools._link_directory(src, dest)

    assert "skipped" in result
    assert _link_destination(dest) is not None, "the user's link was destroyed"
    assert Path(os.path.realpath(dest)) == mine.resolve(), \
        "the link survived but no longer points where the user pointed it"
    assert (dest / "extension.mjs").read_text(encoding="utf-8") == "MY LOCAL EDITS"
    assert (mine / "extension.mjs").read_text(encoding="utf-8") == "MY LOCAL EDITS"


def test_link_directory_replacing_a_link_does_not_delete_through_it(tmp_path, monkeypatch):
    """With consent the link is replaced — but the directory it pointed at is
    the user's data and must survive. Removing a link by walking it would take
    the target's contents with it."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "extension.mjs").write_text("repo version", encoding="utf-8")

    mine = tmp_path / "my-working-copy"
    mine.mkdir()
    (mine / "extension.mjs").write_text("MY LOCAL EDITS", encoding="utf-8")
    (mine / "notes.txt").write_text("keep me", encoding="utf-8")

    dest = tmp_path / "dest"
    _make_dir_link(mine, dest)

    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: True)
    setup_tools._link_directory(src, dest)

    assert (dest / "extension.mjs").read_text(encoding="utf-8") == "repo version"
    assert (mine / "extension.mjs").read_text(encoding="utf-8") == "MY LOCAL EDITS"
    assert (mine / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_link_directory_replaces_a_user_link_when_the_manifest_permits(tmp_path, monkeypatch):
    """``may_replace`` is the manifest saying it already wrote this. No prompt."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "extension.mjs").write_text("repo version", encoding="utf-8")
    mine = tmp_path / "elsewhere"
    mine.mkdir()
    dest = tmp_path / "dest"
    _make_dir_link(mine, dest)

    def refuse(*a, **k):
        raise AssertionError("must not prompt when the manifest permits replacement")

    monkeypatch.setattr(setup_tools, "ask", refuse)
    setup_tools._link_directory(src, dest, may_replace=True)

    assert (dest / "extension.mjs").read_text(encoding="utf-8") == "repo version"
    assert mine.is_dir()


def test_link_directory_does_not_prompt_when_the_link_is_already_ours(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.mjs").write_text("x", encoding="utf-8")
    dest = tmp_path / "dest"
    _make_dir_link(src, dest)

    def refuse(*a, **k):
        raise AssertionError("must not prompt for a link that already points at src")

    monkeypatch.setattr(setup_tools, "ask", refuse)
    assert setup_tools._link_directory(src, dest) == "already linked"


def test_link_directory_keeps_a_broken_user_link_without_consent(tmp_path, monkeypatch):
    """A link whose target is gone is still the user's link. Treating it as
    absent would silently overwrite the one thing it records."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.mjs").write_text("x", encoding="utf-8")

    gone = tmp_path / "gone"
    gone.mkdir()
    dest = tmp_path / "dest"
    _make_dir_link(gone, dest)
    gone.rmdir()

    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: False)
    result = setup_tools._link_directory(src, dest)

    assert "skipped" in result
    assert _link_destination(dest) is not None, \
        "the user's broken link was destroyed"


def test_link_directory_replaces_a_plain_file_at_the_destination(tmp_path, monkeypatch):
    """A regular file where a directory belongs must still be replaceable —
    ``rmtree`` cannot remove one, so the copy that follows would collide."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.mjs").write_text("x", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.write_text("i am a file", encoding="utf-8")

    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: True)
    result = setup_tools._link_directory(src, dest)

    assert "replaced" in result
    assert (dest / "f.mjs").read_text(encoding="utf-8") == "x"


def test_multiplexer_hint_is_platform_specific(monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    assert "psmux" in setup_tools.multiplexer_hint()
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", True)
    assert "brew" in setup_tools.multiplexer_hint()
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    assert "tmux" in setup_tools.multiplexer_hint()


def test_sqlite3_binary_is_not_a_prerequisite(monkeypatch, capsys):
    """The toolkit uses Python's stdlib sqlite3, so the binary must not be
    demanded on any platform."""
    monkeypatch.setattr(setup_tools.shutil, "which",
                        lambda n: None if n == "sqlite3" else f"/usr/bin/{n}")
    missing = setup_tools.check_prerequisites()
    out = capsys.readouterr().out
    assert missing == 0
    assert "sqlite3" not in out


# ── provisioning ────────────────────────────────────────────────
class FakeMachine:
    """A stand-in PATH: tools appear only once 'installed'."""

    def __init__(self, present=(), installable=(), output=""):
        self.present = set(present)
        self.installable = set(installable)
        self.output = output
        self.commands: list[list[str]] = []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.present else None

    def run(self, cmd, **kwargs):
        self.commands.append(cmd)
        # An install command names the package(s) after the executable, so the
        # executable's own path must not be mistaken for a package name.
        arguments = [str(part) for part in cmd[1:]]
        matched = [tool for tool in sorted(self.installable)
                   if any(tool in arg for arg in arguments)]
        self.present.update(matched)
        return bool(matched)

    def capture(self, cmd):
        self.commands.append(cmd)
        return True, self.output


@pytest.fixture
def machine(monkeypatch):
    def _make(present=(), installable=(), output=""):
        m = FakeMachine(present, installable, output)
        monkeypatch.setattr(setup_tools, "which", m.which)
        monkeypatch.setattr(setup_tools, "run", m.run)
        monkeypatch.setattr(setup_tools, "capture", m.capture)
        monkeypatch.setattr(setup_tools, "refresh_path", lambda: None)
        monkeypatch.setattr(setup_tools, "persist_user_path", lambda d: None)
        monkeypatch.setattr(setup_tools, "_prepend_process_path", lambda d: None)
        monkeypatch.setattr(setup_tools, "_sudo_usable", lambda sudo: True)
        return m
    return _make


def test_missing_multiplexer_is_installed_not_just_reported(machine, monkeypatch):
    """Setup provisions the machine; it must not hand back a homework list."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    m = machine(present=["apt-get", "sudo"], installable=["tmux"])
    assert setup_tools.ensure_multiplexer() is True
    assert any("tmux" in " ".join(c) for c in m.commands)


def test_missing_git_is_installed(machine, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", True)
    m = machine(present=["brew"], installable=["git"])
    assert setup_tools.ensure_git() is True
    assert ["/usr/bin/brew", "install", "git"] in m.commands


def test_missing_copilot_is_installed_via_npm(machine, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    m = machine(present=["npm", "apt-get", "sudo"], installable=["copilot"])
    assert setup_tools.ensure_copilot() is True
    assert any("@github/copilot" in " ".join(c) for c in m.commands)


def test_copilot_install_pulls_in_node_when_npm_absent(machine, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    m = machine(present=["apt-get", "sudo"], installable=["nodejs", "npm", "copilot"])
    assert setup_tools.ensure_copilot() is True
    joined = [" ".join(c) for c in m.commands]
    assert any("nodejs" in c for c in joined)
    assert any("@github/copilot" in c for c in joined)


def test_unfixable_prerequisite_reports_manual_instructions(machine, monkeypatch, capsys):
    """When automation genuinely cannot help, the user still gets told how."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    machine(present=[], installable=[])
    assert setup_tools.ensure_git() is False
    assert "git-scm.com" in capsys.readouterr().err


def test_ensure_prerequisites_counts_only_failures(machine, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    machine(present=["tmux", "git", "copilot"])
    assert setup_tools.ensure_prerequisites() == 0


def test_specify_installed_with_uv(machine, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    m = machine(present=["uv"], installable=["specify"])
    assert setup_tools.ensure_specify() is True
    joined = [" ".join(c) for c in m.commands]
    assert any("tool install" in c and "specify-cli" in c for c in joined)
    assert any(setup_tools.SPEC_KIT_VERSION in c for c in joined)


def test_specify_skipped_cleanly_when_uv_unavailable(machine, monkeypatch, capsys):
    """An optional tool that cannot be installed must not fail the setup."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    machine(present=[], installable=[])
    assert setup_tools.ensure_specify() is False
    assert "uv" in capsys.readouterr().out


def test_anvil_uses_the_current_plugin_cli(machine):
    """`copilot extensions list` / `copilot install <repo>` are rejected by the
    CLI; their failure used to be swallowed, so Anvil never installed."""
    m = machine(present=["copilot"], installable=["anvil"])
    setup_tools.ensure_anvil()
    joined = [" ".join(str(p) for p in c) for c in m.commands]
    assert any("plugin install burkeholland/anvil" in c for c in joined)
    assert not any("extensions" in c for c in joined)


def test_anvil_not_reinstalled_when_present(machine):
    m = machine(present=["copilot"], output="anvil  burkeholland/anvil")
    setup_tools.ensure_anvil()
    assert not any("install" in " ".join(str(p) for p in c) for c in m.commands)


def test_uv_never_pipes_a_download_into_a_shell(machine, monkeypatch):
    """`irm ... | iex` dies on machines whose PowerShell cannot load its own
    modules, which is exactly where setup has to keep working."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    m = machine(present=["winget", "pwsh"], installable=[])
    setup_tools.ensure_uv()
    joined = [" ".join(str(p) for p in c) for c in m.commands]
    assert not any("iex" in c for c in joined)


def test_uv_falls_back_to_pip_when_the_package_manager_fails(machine, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    pip_args: list[list[str]] = []
    monkeypatch.setattr(setup_tools, "pip_install",
                        lambda args: pip_args.append(args) or False)
    m = machine(present=["winget"], installable=[])
    setup_tools.ensure_uv()
    joined = [" ".join(str(p) for p in c) for c in m.commands]
    assert any("astral-sh.uv" in c for c in joined)
    assert pip_args == [["--upgrade", "uv"]]


def test_missing_pip_is_installed_not_just_reported(monkeypatch):
    """Debian ships python3 without pip. Setup installs it; it does not
    hand the user a homework assignment and quit."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    state = {"pip": False}
    monkeypatch.setattr(setup_tools, "have_pip", lambda: state["pip"])
    monkeypatch.setattr(setup_tools, "run", lambda cmd, **kw: False)
    installed: list[str] = []

    def fake_install(logical):
        installed.append(logical)
        state["pip"] = True
        return True

    monkeypatch.setattr(setup_tools, "install_system_package", fake_install)
    assert setup_tools.ensure_pip() is True
    assert "pip" in installed


def test_pip_bootstrap_is_downloaded_not_piped(monkeypatch):
    """When no package manager has pip, get-pip.py is fetched to a file and
    run by path, never streamed into an interpreter."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    monkeypatch.setattr(setup_tools, "have_pip", lambda: False)
    monkeypatch.setattr(setup_tools, "run", lambda cmd, **kw: False)
    monkeypatch.setattr(setup_tools, "install_system_package", lambda logical: False)
    called = {"bootstrap": False}

    def fake_bootstrap():
        called["bootstrap"] = True
        return False

    monkeypatch.setattr(setup_tools, "_install_pip_from_bootstrap", fake_bootstrap)
    assert setup_tools.ensure_pip() is False
    assert called["bootstrap"] is True


def test_pip_install_retries_when_the_interpreter_is_externally_managed(monkeypatch):
    """PEP 668 distro Pythons refuse a plain install; --user is allowed."""
    monkeypatch.setattr(setup_tools, "ensure_pip", lambda: True)
    monkeypatch.setattr(setup_tools, "in_virtualenv", lambda: False)
    monkeypatch.setattr(setup_tools, "persist_user_path", lambda d: None)
    monkeypatch.setattr(setup_tools, "_prepend_process_path", lambda d: None)
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "" if returncode == 0 else \
                "error: externally-managed-environment"

    def fake_run(cmd, **kwargs):
        calls.append([str(p) for p in cmd])
        return Result(0 if "--user" in cmd else 1)

    monkeypatch.setattr(setup_tools.subprocess, "run", fake_run)
    assert setup_tools.pip_install(["-e", "."]) is True
    assert len(calls) == 2
    assert "--user" in calls[1]


def test_pip_install_does_not_retry_a_genuine_failure(monkeypatch):
    monkeypatch.setattr(setup_tools, "ensure_pip", lambda: True)
    monkeypatch.setattr(setup_tools, "in_virtualenv", lambda: False)
    calls: list[list[str]] = []

    class Result:
        returncode = 1
        stdout = ""
        stderr = "ERROR: No matching distribution found"

    def fake_run(cmd, **kwargs):
        calls.append([str(p) for p in cmd])
        return Result()

    monkeypatch.setattr(setup_tools.subprocess, "run", fake_run)
    assert setup_tools.pip_install(["nope"]) is False
    assert len(calls) == 1


def test_install_package_goes_through_the_resilient_pip_path(monkeypatch):
    """install_package must inherit the pip bootstrap and PEP 668 retries."""
    monkeypatch.setattr(setup_tools.shutil, "which", lambda name: None)
    seen: list[list[str]] = []
    monkeypatch.setattr(setup_tools, "pip_install",
                        lambda args: seen.append(args) or True)
    assert setup_tools.install_package(assume_yes=True) is True
    assert seen and seen[0][0] == "-e"


def test_spawned_shells_do_not_inherit_a_broken_psmodulepath(monkeypatch):
    """A pwsh-set PSModulePath makes powershell.exe unable to load even
    built-in modules, which is what broke the uv install."""
    monkeypatch.setenv("PSModulePath", "C:\\only\\pwsh\\modules")
    assert "PSModulePath" not in setup_tools._clean_env()


def test_prepend_process_path_is_idempotent(monkeypatch):
    monkeypatch.setenv("PATH", "/a/b")
    setup_tools._prepend_process_path(Path("/new/dir"))
    setup_tools._prepend_process_path(Path("/new/dir"))
    assert os.environ["PATH"].split(os.pathsep).count(str(Path("/new/dir"))) == 1


def test_refresh_path_is_a_noop_off_windows(monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", "/only/this")
    setup_tools.refresh_path()
    assert os.environ["PATH"] == "/only/this"


def test_persist_user_path_writes_the_shell_profile_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("PATH", "/existing")
    target = Path("/opt/tools/bin")
    setup_tools.persist_user_path(target)
    assert str(target) in (tmp_path / ".bashrc").read_text(encoding="utf-8")


def test_package_install_does_not_block_on_a_sudo_password_prompt(monkeypatch):
    """A non-interactive run must fail fast, not sit through sudo's retries."""
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    monkeypatch.setattr(setup_tools, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(setup_tools, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup_tools, "refresh_path", lambda: None)
    monkeypatch.setattr(setup_tools.os, "geteuid", lambda: 1000, raising=False)

    class _NoTTY:
        @staticmethod
        def isatty():
            return False

    monkeypatch.setattr(setup_tools.sys, "stdin", _NoTTY)
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append([str(p) for p in cmd])
        return False  # sudo -n fails: a password is required

    monkeypatch.setattr(setup_tools, "run", fake_run)
    assert setup_tools.install_system_package("pip") is False
    assert commands == [["/usr/bin/sudo", "-n", "true"]]


def test_package_install_proceeds_when_sudo_is_passwordless(monkeypatch):
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools, "IS_MACOS", False)
    monkeypatch.setattr(setup_tools, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(setup_tools, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(setup_tools, "refresh_path", lambda: None)
    monkeypatch.setattr(setup_tools.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(setup_tools, "_APT_UPDATED", True)
    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append([str(p) for p in cmd])
        return True

    monkeypatch.setattr(setup_tools, "run", fake_run)
    assert setup_tools.install_system_package("pip") is True
    assert any("python3-pip" in c for c in commands[-1])


# ── skill installation ──────────────────────────────────────────
#
# Skills install for the *user* (~/.copilot/skills), not into one project:
# the operator skill is about work that spans projects, so a copy under a
# single repo's .github/skills would be invisible where it is needed.
@pytest.fixture
def skill_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("v1", encoding="utf-8")
    home = tmp_path / "copilot"
    monkeypatch.setattr(setup_tools, "REPO_ROOT", repo)
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", home)
    return repo, home


def test_skills_install_at_user_level(skill_repo):
    _, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v1"


def test_skill_reinstall_is_a_noop_when_identical(skill_repo, capsys):
    _, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    capsys.readouterr()
    setup_tools.install_skills(assume_yes=True)
    assert "already up to date" in capsys.readouterr().out


def test_modified_skill_is_kept_without_consent(skill_repo, monkeypatch):
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("mine", encoding="utf-8")
    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")
    monkeypatch.setattr(setup_tools, "ask", lambda *_a, **_k: False)
    setup_tools.install_skills()
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_modified_skill_is_replaced_when_consented(skill_repo, monkeypatch):
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("mine", encoding="utf-8")
    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")
    monkeypatch.setattr(setup_tools, "ask", lambda *_a, **_k: True)
    setup_tools.install_skills()
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v2"


def test_directory_without_a_skill_file_is_not_installed(skill_repo):
    repo, home = skill_repo
    (repo / "skills" / "notaskill").mkdir()
    setup_tools.install_skills(assume_yes=True)
    assert not (home / "skills" / "notaskill").exists()


def test_skill_subdirectories_are_installed(skill_repo):
    """A skill that bundles references or scripts is one artifact. Half of it
    deployed is not a working skill."""
    repo, home = skill_repo
    (repo / "skills" / "demo" / "reference").mkdir()
    (repo / "skills" / "demo" / "reference" / "guide.md").write_text(
        "the details", encoding="utf-8")
    (repo / "skills" / "demo" / "scripts" / "deep").mkdir(parents=True)
    (repo / "skills" / "demo" / "scripts" / "deep" / "run.py").write_text(
        "print('hi')", encoding="utf-8")

    setup_tools.install_skills(assume_yes=True)

    assert (home / "skills" / "demo" / "reference" / "guide.md").read_text(
        encoding="utf-8") == "the details"
    assert (home / "skills" / "demo" / "scripts" / "deep" / "run.py").read_text(
        encoding="utf-8") == "print('hi')"


def test_skill_with_subdirectories_converges(skill_repo, capsys):
    """Reinstalling must reach a fixed point. A copy that omits subdirectories
    can never match the digest setup compares against, so setup would rewrite
    the skill on every run for the life of the machine and never say why."""
    repo, home = skill_repo
    (repo / "skills" / "demo" / "reference").mkdir()
    (repo / "skills" / "demo" / "reference" / "guide.md").write_text(
        "the details", encoding="utf-8")

    manifest = install_manifest.empty_manifest()
    setup_tools.install_skills(assume_yes=True, manifest=manifest)
    capsys.readouterr()
    setup_tools.install_skills(assume_yes=True, manifest=manifest)

    assert "already up to date" in capsys.readouterr().out
    assert install_manifest.classify(
        manifest, "skills/demo", home / "skills" / "demo",
        install_manifest.tree_digest(repo / "skills" / "demo"),
    ) == install_manifest.CURRENT


def test_skill_file_deleted_upstream_is_removed_on_reinstall(skill_repo):
    """Stale files left behind are the same convergence failure wearing the
    other hat: the deployed tree keeps content the repository no longer has."""
    repo, home = skill_repo
    (repo / "skills" / "demo" / "old.md").write_text("obsolete", encoding="utf-8")
    setup_tools.install_skills(assume_yes=True)
    assert (home / "skills" / "demo" / "old.md").exists()

    (repo / "skills" / "demo" / "old.md").unlink()
    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")
    setup_tools.install_skills(assume_yes=True)

    assert not (home / "skills" / "demo" / "old.md").exists()
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v2"


def test_user_link_at_a_skill_destination_is_not_replaced_without_consent(
        skill_repo, tmp_path, monkeypatch):
    """A digest taken through a link describes the target, not the destination.
    It can never prove setup wrote what is there, so STALE must not license
    deleting a link setup never created."""
    repo, home = skill_repo
    manifest = install_manifest.empty_manifest()
    setup_tools.install_skills(assume_yes=True, manifest=manifest)

    mine = tmp_path / "my-skill"
    mine.mkdir()
    (mine / "SKILL.md").write_text("v1", encoding="utf-8")
    dest = home / "skills" / "demo"
    shutil.rmtree(dest)
    _make_dir_link(mine, dest)

    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")
    monkeypatch.setattr(setup_tools, "ask", lambda *a, **k: False)
    setup_tools.install_skills(manifest=manifest)

    assert _link_destination(dest) is not None, "the user's link was destroyed"
    assert Path(os.path.realpath(dest)) == mine.resolve()
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "v1"


def test_failed_skill_copy_leaves_the_installed_skill_intact(skill_repo, monkeypatch):
    """Staging exists for this: a copy that dies part-way must not take the
    working skill with it."""
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v1"

    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")

    def explode(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(setup_tools.shutil, "copytree", explode)
    setup_tools.install_skills(assume_yes=True)

    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v1", \
        "a failed copy destroyed the skill that was already installed"


def test_failed_skill_copy_leaves_no_staging_directory_behind(skill_repo, monkeypatch):
    """The staging tree must be cleaned up after it has actually been created.
    A copy that dies before writing anything proves nothing about cleanup, and
    the scratch name holds a SKILL.md, so litter is a skill the CLI may load."""
    repo, home = skill_repo

    def half_written(_source, target, *_a, **_k):
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "SKILL.md").write_text("half written", encoding="utf-8")
        raise OSError("no space left on device")

    monkeypatch.setattr(setup_tools.shutil, "copytree", half_written)
    setup_tools.install_skills(assume_yes=True)

    assert list((home / "skills").iterdir()) == [], \
        "staging scratch was left in the skills directory"


def test_a_double_rename_failure_still_puts_the_skill_back(skill_repo, monkeypatch):
    """Both the swap and its rollback can fail, which leaves the only copy
    under the aside name. The run must still end with the skill where it
    belongs rather than with a warning and an empty destination."""
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")

    real_replace = setup_tools.os.replace
    calls = {"n": 0}

    def fail_the_swap_and_its_rollback(source, target, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise OSError("the swap and the rollback both failed")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(setup_tools.os, "replace", fail_the_swap_and_its_rollback)
    setup_tools.install_skills(assume_yes=True)

    assert calls["n"] >= 3, "the double failure was never reached"
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v1"
    assert [p.name for p in (home / "skills").iterdir()] == ["demo"]


def test_a_swap_interrupted_by_a_killed_process_is_repaired_next_run(skill_repo, monkeypatch):
    """No exception handler runs when the process is killed outright, so the
    repair has to happen on the way in, not on the way out."""
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    dest = home / "skills" / "demo"
    os.replace(dest, dest.with_name(".demo.previous"))
    assert not dest.exists()

    def explode(*_a, **_k):
        raise OSError("and the next run cannot copy either")

    monkeypatch.setattr(setup_tools.shutil, "copytree", explode)
    setup_tools.install_skills(assume_yes=True)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "v1", \
        "the copy left under the aside name was not restored"
    assert [p.name for p in (home / "skills").iterdir()] == ["demo"]


def test_undeletable_destination_does_not_abort_the_whole_install(skill_repo, monkeypatch):
    """A locked file under the destination is the real Windows failure. The
    installed skill must survive it whole — a partial delete followed by a
    warning is still the user's data gone."""
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    (repo / "skills" / "demo" / "reference").mkdir()
    (repo / "skills" / "demo" / "reference" / "guide.md").write_text("new", encoding="utf-8")
    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")

    real_replace = setup_tools.os.replace

    def locked(source, target, *args, **kwargs):
        if Path(source).name == "demo":
            raise OSError("the directory is in use by another process")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(setup_tools.os, "replace", locked)
    setup_tools.install_skills(assume_yes=True)

    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v1"


def test_a_failed_swap_puts_the_original_skill_back(skill_repo, monkeypatch):
    """The old copy is renamed aside, not deleted, so the window in which the
    user has nothing must close again even when the swap itself fails."""
    repo, home = skill_repo
    setup_tools.install_skills(assume_yes=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")

    real_replace = setup_tools.os.replace
    calls = {"n": 0}

    def fail_on_the_second_move(source, target, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("interrupted between the two renames")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(setup_tools.os, "replace", fail_on_the_second_move)
    setup_tools.install_skills(assume_yes=True)

    assert calls["n"] >= 2, "the swap never reached the second rename"
    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v1"
    assert [p.name for p in (home / "skills").iterdir()] == ["demo"], \
        "scratch was left behind after the rollback"


def test_missing_skills_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_tools, "REPO_ROOT", tmp_path / "empty")
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", tmp_path / "copilot")
    setup_tools.install_skills(assume_yes=True)


def test_the_operator_skill_is_shipped():
    """The skill the instructions point agents at must actually exist."""
    root = Path(__file__).resolve().parent.parent
    skill = root / "skills" / "operator-agents" / "SKILL.md"
    assert skill.is_file()
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: operator-agents" in text
    assert "operator send" in text and "--headless" in text


# ── install manifest integration ─────────────────────────────────
@pytest.fixture()
def install_env(tmp_path, monkeypatch):
    """A miniature repo and a home to deploy it into."""
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "templates" / "copilot-instructions.md").write_text("v1", encoding="utf-8")
    (repo / "templates" / "mcp-config.json").write_text("{}", encoding="utf-8")
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("v1", encoding="utf-8")
    home = tmp_path / "copilot"
    operator_home = tmp_path / "operator"
    monkeypatch.setattr(setup_tools, "REPO_ROOT", repo)
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", home)
    monkeypatch.setattr(setup_tools, "OPERATOR_HOME", operator_home)
    return repo, home, operator_home


def _forbid_ask(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("setup asked about a file the user never touched")
    monkeypatch.setattr(setup_tools, "ask", explode)


def test_unmodified_template_updates_without_asking(install_env, monkeypatch):
    """The whole point of the manifest: if the deployed bytes are the bytes we
    wrote, the user has nothing invested in them and there is no question to
    ask. Before the manifest this prompted on every upgrade."""
    repo, home, _ = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)

    (repo / "templates" / "copilot-instructions.md").write_text("v2", encoding="utf-8")
    _forbid_ask(monkeypatch)
    setup_tools.install_templates(manifest=manifest)

    assert (home / "copilot-instructions.md").read_text(encoding="utf-8") == "v2"


def test_unmodified_skill_updates_without_asking(install_env, monkeypatch):
    repo, home, _ = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_skills(assume_yes=True, manifest=manifest)

    (repo / "skills" / "demo" / "SKILL.md").write_text("v2", encoding="utf-8")
    _forbid_ask(monkeypatch)
    setup_tools.install_skills(manifest=manifest)

    assert (home / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8") == "v2"


def test_locally_edited_template_still_asks(install_env, monkeypatch):
    repo, home, _ = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)

    (home / "copilot-instructions.md").write_text("MY EDITS", encoding="utf-8")
    (repo / "templates" / "copilot-instructions.md").write_text("v2", encoding="utf-8")
    asked = []
    monkeypatch.setattr(setup_tools, "ask",
                        lambda q, *_a, **_k: asked.append(q) or False)
    setup_tools.install_templates(manifest=manifest)

    assert asked, "a locally edited file must not be silently overwritten"
    assert (home / "copilot-instructions.md").read_text(encoding="utf-8") == "MY EDITS"


def test_edited_template_is_replaced_when_consented(install_env, monkeypatch):
    repo, home, _ = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    (home / "copilot-instructions.md").write_text("MY EDITS", encoding="utf-8")
    (repo / "templates" / "copilot-instructions.md").write_text("v2", encoding="utf-8")
    monkeypatch.setattr(setup_tools, "ask", lambda *_a, **_k: True)
    setup_tools.install_templates(manifest=manifest)
    assert (home / "copilot-instructions.md").read_text(encoding="utf-8") == "v2"


def test_without_a_manifest_setup_falls_back_to_asking(install_env, monkeypatch):
    """A machine that predates manifests, or one whose manifest was lost, keeps
    the old conservative behaviour."""
    repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    (home / "copilot-instructions.md").write_text("from an older setup",
                                                  encoding="utf-8")
    (repo / "templates" / "copilot-instructions.md").write_text("v2", encoding="utf-8")
    asked = []
    monkeypatch.setattr(setup_tools, "ask",
                        lambda q, *_a, **_k: asked.append(q) or False)
    setup_tools.install_templates(manifest=install_manifest.empty_manifest())
    assert asked
    assert (home / "copilot-instructions.md").read_text(
        encoding="utf-8") == "from an older setup"


def test_install_records_what_it_wrote(install_env):
    _repo, home, _ = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    setup_tools.install_skills(assume_yes=True, manifest=manifest)

    entry = manifest["artifacts"]["templates/copilot-instructions.md"]
    assert entry["version"] == setup_tools.TOOLKIT_VERSION
    assert entry["sha256"] == install_manifest.file_digest(
        home / "copilot-instructions.md")
    assert "skills/demo" in manifest["artifacts"]


def test_an_already_current_file_becomes_tracked(install_env):
    """A file that happens to match the repo is recorded too, so the very next
    upgrade can be applied silently."""
    _repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    (home / "copilot-instructions.md").write_text("v1", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    assert "templates/copilot-instructions.md" in manifest["artifacts"]


def test_declining_leaves_the_artifact_untracked(install_env, monkeypatch):
    """Refusing an overwrite must not record the file as ours, or the next run
    would overwrite it without asking."""
    repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    (home / "copilot-instructions.md").write_text("mine", encoding="utf-8")
    (repo / "templates" / "copilot-instructions.md").write_text("v2", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    monkeypatch.setattr(setup_tools, "ask", lambda *_a, **_k: False)
    setup_tools.install_templates(manifest=manifest)
    assert "templates/copilot-instructions.md" not in manifest["artifacts"]


def test_deployed_artifacts_covers_templates_and_skills(install_env):
    keys = {key for key, _kind, _src, _dest in setup_tools.deployed_artifacts()}
    assert "templates/copilot-instructions.md" in keys
    assert "templates/mcp-config.json" in keys
    assert "skills/demo" in keys


def test_status_reports_stale_and_exits_nonzero(install_env, capsys):
    repo, _home, operator_home = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    setup_tools.install_skills(assume_yes=True, manifest=manifest)
    manifest["package_version"] = setup_tools.TOOLKIT_VERSION
    install_manifest.save(operator_home, manifest)

    (repo / "templates" / "copilot-instructions.md").write_text("v2", encoding="utf-8")
    code = setup_tools.report_status()
    out = capsys.readouterr().out
    assert code == 1
    assert "safe to update" in out


def test_status_is_clean_when_everything_is_current(install_env, capsys):
    _repo, _home, operator_home = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    setup_tools.install_skills(assume_yes=True, manifest=manifest)
    manifest["package_version"] = setup_tools.TOOLKIT_VERSION
    install_manifest.save(operator_home, manifest)

    assert setup_tools.report_status() == 0
    assert "up to date" in capsys.readouterr().out


def test_status_flags_a_machine_that_never_ran_setup(install_env, capsys):
    setup_tools.report_status()
    assert "No install manifest" in capsys.readouterr().out


def test_upgrades_are_skipped_on_a_fresh_machine(install_env, monkeypatch, capsys):
    """There is no old state to migrate before the first install, so running
    historical upgrades would be noise."""
    called = []
    monkeypatch.setattr(install_manifest, "pending_migrations",
                        lambda *_a, **_k: [("1.0.0", "1.1.0",
                                            lambda ctx: called.append(1))])
    setup_tools.apply_upgrades(install_manifest.empty_manifest())
    assert called == []


def test_upgrades_run_when_files_are_already_deployed(install_env, monkeypatch):
    _repo, home, _ = install_env
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    called = []
    monkeypatch.setattr(install_manifest, "pending_migrations",
                        lambda *_a, **_k: [("1.0.0", "1.1.0",
                                            lambda ctx: called.append(ctx))])
    setup_tools.apply_upgrades(manifest)
    assert len(called) == 1
    assert called[0].copilot_dir == home


def test_the_version_is_a_single_source_of_truth():
    """pyproject reads the version from the module, so the two cannot drift."""
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'attr = "copilot_tools_version.__version__"' in text
    assert 'dynamic = ["version"]' in text


def _declared_py_modules():
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("py-modules = [", 1)[1].split("]", 1)[0]
    return {line.strip().strip('",') for line in block.splitlines() if line.strip()}


def test_every_imported_local_module_is_packaged():
    """A top-level module that another packaged module imports must itself be
    declared, or the installed package raises ModuleNotFoundError at runtime
    while the repo checkout keeps working."""
    import ast

    root = Path(__file__).resolve().parent.parent
    declared = _declared_py_modules()
    local = {p.stem for p in root.glob("*.py")}

    missing = {}
    for name in sorted(declared):
        source = root / f"{name}.py"
        if not source.is_file():
            missing[name] = "declared but no such file"
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported = {node.module.split(".")[0]}
            else:
                continue
            for dep in imported & local:
                if dep not in declared:
                    missing[dep] = f"imported by {name}.py"
    assert not missing, f"add these to pyproject py-modules: {missing}"


# ── an unreadable destination is not an empty one ────────────────
def _deny_lstat(monkeypatch, target: Path) -> None:
    """Make ``target`` look present-but-unexaminable, as a permission denial,
    an exclusive lock or a dropped network mount does.

    Staged by patching the syscalls the presence probe makes, because the real
    condition cannot be produced portably: POSIX permission bits do not
    restrain root, which is how CI containers run, and Windows needs ACL edits
    a runner will not reliably grant. Both ``lstat`` and ``stat`` are denied,
    as a real denial denies both. Every other path passes straight through.
    """
    real_lstat, real_stat = os.lstat, os.stat
    resolved = str(target)

    def denied(real):
        def fake(path, *args, **kwargs):
            if str(path) == resolved:
                raise PermissionError(13, "Permission denied")
            return real(path, *args, **kwargs)
        return fake

    monkeypatch.setattr(os, "lstat", denied(real_lstat))
    monkeypatch.setattr(os, "stat", denied(real_stat))


def test_unreadable_template_is_not_overwritten_even_with_yes(install_env,
                                                              monkeypatch):
    """The failure this guards against: a transient denial makes the
    destination read as absent, absent is the state that needs no consent, and
    the user's instructions file is replaced by the repository's.

    ``assume_yes`` does not license it. ``--yes`` answers questions about
    contents somebody could look at; nobody could look at these.
    """
    repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    dest = home / "copilot-instructions.md"
    dest.write_text("MY CAREFULLY EDITED INSTRUCTIONS", encoding="utf-8")
    (repo / "templates" / "copilot-instructions.md").write_text("v2",
                                                                encoding="utf-8")
    _deny_lstat(monkeypatch, dest)

    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)

    assert dest.read_text(encoding="utf-8") == "MY CAREFULLY EDITED INSTRUCTIONS"
    assert "templates/copilot-instructions.md" not in manifest["artifacts"], \
        "an artifact nobody could examine must not be recorded as installed"


def test_unreadable_template_is_reported_not_silently_skipped(install_env,
                                                              monkeypatch,
                                                              capsys):
    """Leaving it alone is only right if the user is told, or a machine quietly
    never receives an update and nothing ever says why."""
    _repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    dest = home / "copilot-instructions.md"
    dest.write_text("mine", encoding="utf-8")
    _deny_lstat(monkeypatch, dest)

    setup_tools.install_templates(assume_yes=True,
                                  manifest=install_manifest.empty_manifest())

    assert "could not be examined" in capsys.readouterr().out


def test_unreadable_skill_destination_is_left_alone(install_env, monkeypatch):
    _repo, home, _ = install_env
    dest = home / "skills" / "demo"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("the user's copy", encoding="utf-8")
    _deny_lstat(monkeypatch, dest)

    manifest = install_manifest.empty_manifest()
    setup_tools.install_skills(assume_yes=True, manifest=manifest)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "the user's copy"
    assert "skills/demo" not in manifest["artifacts"]
    assert not (home / "skills" / ".demo.previous").exists()
    assert not (home / "skills" / ".demo.installing").exists()


def test_unreadable_extension_destination_is_left_alone(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "extensions" / "guard").mkdir(parents=True)
    (repo / "extensions" / "guard" / "index.js").write_text("v2", encoding="utf-8")
    home = tmp_path / "copilot"
    dest = home / "extensions" / "guard"
    dest.mkdir(parents=True)
    (dest / "index.js").write_text("the user's copy", encoding="utf-8")
    monkeypatch.setattr(setup_tools, "REPO_ROOT", repo)
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", home)
    _deny_lstat(monkeypatch, dest)

    manifest = install_manifest.empty_manifest()
    setup_tools.install_extensions(assume_yes=True, manifest=manifest)

    assert (dest / "index.js").read_text(encoding="utf-8") == "the user's copy"
    assert "extensions/guard" not in manifest["artifacts"]


def test_a_machine_with_an_unreadable_artifact_is_not_treated_as_fresh(
        install_env, monkeypatch):
    """``apply_upgrades`` skips migrations on a fresh machine, on the grounds
    that there is no old state to migrate. A machine whose deployed artifact
    could not be examined is not fresh — it is a machine carrying exactly the
    old state migrations exist to fix, and skipping them there is silent."""
    _repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    dest = home / "copilot-instructions.md"
    dest.write_text("mine", encoding="utf-8")
    _deny_lstat(monkeypatch, dest)

    ran = []
    monkeypatch.setattr(install_manifest, "pending_migrations",
                        lambda *_a, **_k: [("1.0.0", "1.1.0", lambda ctx: None)])
    monkeypatch.setattr(install_manifest, "run_migrations",
                        lambda ctx, *a, **k: ran.append(ctx) or [])
    setup_tools.apply_upgrades(install_manifest.empty_manifest(), assume_yes=True)

    assert ran, "migrations were skipped on a machine that is not fresh"


def test_reconcile_leaves_both_copies_when_the_destination_is_unknown(
        tmp_path, monkeypatch):
    """The aside copy may be the user's only one. Restoring it over a
    destination that might be a finished install, or discarding it when the
    destination might be missing, each destroy a copy on a guess."""
    dest = tmp_path / "demo"
    previous = tmp_path / ".demo.previous"
    previous.mkdir()
    (previous / "SKILL.md").write_text("the only copy", encoding="utf-8")
    _deny_lstat(monkeypatch, dest)

    setup_tools._reconcile_scratch(dest)

    assert (previous / "SKILL.md").read_text(encoding="utf-8") == "the only copy"


def test_is_link_does_not_raise_on_a_path_it_cannot_examine(tmp_path,
                                                            monkeypatch):
    """``Path.is_symlink`` re-raises a permission denial on every interpreter
    in the CI matrix (verified on 3.11 and 3.12), so unguarded this aborts
    setup with a traceback rather than skipping one artifact."""
    dest = tmp_path / "skill"
    dest.mkdir()
    _deny_lstat(monkeypatch, dest)
    assert setup_tools._is_link(dest) is False


def test_a_dangling_link_at_a_destination_is_not_written_through(install_env,
                                                                 monkeypatch):
    """Real condition, no mocking. ``Path.exists`` follows the link, finds
    nothing and reports the destination absent — the one state that needs no
    consent — and ``shutil.copyfile`` then follows the same link and lands the
    repository's copy wherever the user pointed it, silently and somewhere
    they never named. The name is occupied, so setup must ask."""
    repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    elsewhere = home.parent / "elsewhere.md"
    dest = home / "copilot-instructions.md"
    try:
        os.symlink(elsewhere, dest)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this platform will not create symlinks")
    (repo / "templates" / "copilot-instructions.md").write_text("v2",
                                                                encoding="utf-8")

    assert dest.exists() is False, "precondition: the primitive says absent"
    asked = []
    monkeypatch.setattr(setup_tools, "ask",
                        lambda q, *_a, **_k: asked.append(q) or False)
    setup_tools.install_templates(manifest=install_manifest.empty_manifest())

    assert asked, "setup wrote through a link into a path the user never named"
    assert not elsewhere.exists()


def test_yes_does_not_write_through_a_dangling_link(install_env):
    """``--yes`` consents to overwriting the destination, not to writing
    somewhere else. ``shutil.copyfile`` follows the link and lands the
    repository's copy in the target's location, outside ~/.copilot entirely,
    where nothing will ever look for it and setup's own manifest will describe
    a file that is not where it says."""
    repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    elsewhere = home.parent / "elsewhere.md"
    dest = home / "copilot-instructions.md"
    try:
        os.symlink(elsewhere, dest)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this platform will not create symlinks")
    (repo / "templates" / "copilot-instructions.md").write_text("v2",
                                                                encoding="utf-8")

    setup_tools.install_templates(assume_yes=True,
                                  manifest=install_manifest.empty_manifest())

    assert not elsewhere.exists(), "setup wrote outside the directory it owns"
    assert dest.read_text(encoding="utf-8") == "v2"
    assert not dest.is_symlink()


def test_yes_does_not_write_through_a_link_to_a_real_file(install_env):
    """The same defect with something to lose: a user who points their
    instructions at a dotfiles repo gets that file rewritten by a setup run
    they believed only touched ~/.copilot."""
    repo, home, _ = install_env
    home.mkdir(parents=True, exist_ok=True)
    dotfiles = home.parent / "dotfiles.md"
    dotfiles.write_text("THE USER'S DOTFILES COPY", encoding="utf-8")
    dest = home / "copilot-instructions.md"
    try:
        os.symlink(dotfiles, dest)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this platform will not create symlinks")
    (repo / "templates" / "copilot-instructions.md").write_text("v2",
                                                                encoding="utf-8")

    setup_tools.install_templates(assume_yes=True,
                                  manifest=install_manifest.empty_manifest())

    assert dotfiles.read_text(encoding="utf-8") == "THE USER'S DOTFILES COPY"
    assert dest.read_text(encoding="utf-8") == "v2"


def test_reconcile_keeps_the_aside_copy_while_a_staged_copy_survives(tmp_path):
    """The mark of a finished swap is that no staged copy is left: _replace_tree
    renames it *onto* the destination. So a staged copy that is still there
    says the swap did not finish, and then whatever occupies the destination
    is not the install this scratch belongs to -- the CLI has been observed
    recreating directories under ~/.copilot -- which leaves the aside copy the
    user's only one. Discarding it because 'something is at the destination'
    loses both."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "SKILL.md").write_text("recreated by somebody else", encoding="utf-8")
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the copy that never landed", encoding="utf-8")
    previous = tmp_path / ".demo.previous"
    previous.mkdir()
    (previous / "SKILL.md").write_text("THE USER'S ONLY COPY", encoding="utf-8")

    setup_tools._reconcile_scratch(dest)

    assert previous.is_dir(), "the user's only copy was discarded on a guess"
    assert (previous / "SKILL.md").read_text(encoding="utf-8") == \
        "THE USER'S ONLY COPY"


def test_reconcile_discards_the_aside_copy_once_the_swap_has_landed(tmp_path):
    """The counterpart, so the guard above cannot be satisfied by never
    discarding anything: no staged copy plus an occupied destination is a swap
    that completed and died before its own cleanup. The aside copy really is
    obsolete then, and leaving a directory holding a SKILL.md beside the
    skills directory invites the CLI to load it as a skill of its own."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "SKILL.md").write_text("the installed copy", encoding="utf-8")
    previous = tmp_path / ".demo.previous"
    previous.mkdir()
    (previous / "SKILL.md").write_text("superseded", encoding="utf-8")

    setup_tools._reconcile_scratch(dest)

    assert not previous.exists()


def test_reconcile_restores_the_aside_copy_when_the_destination_is_gone(tmp_path):
    """The original reason this function exists: a process killed between the
    two renames leaves the user's copy under the aside name and nothing at the
    destination."""
    dest = tmp_path / "demo"
    previous = tmp_path / ".demo.previous"
    previous.mkdir()
    (previous / "SKILL.md").write_text("restore me", encoding="utf-8")
    staged = tmp_path / ".demo.installing"
    staged.mkdir()

    setup_tools._reconcile_scratch(dest)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "restore me"
    assert not staged.exists()


def test_replace_refuses_a_destination_that_appeared_after_it_was_found_absent(
        tmp_path):
    """Consent is scoped to the state it was given for. Nothing was there when
    setup classified, so nobody was asked; if something has appeared since,
    replacing it spends an authorisation that was never granted -- and the
    aside copy is discarded on success, so the thing that appeared is gone."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "SKILL.md").write_text("APPEARED MID-RUN", encoding="utf-8")
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the new copy", encoding="utf-8")

    with pytest.raises(OSError):
        setup_tools._replace_tree(staged, dest, expect_absent=True)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "APPEARED MID-RUN"


def test_replace_still_swaps_when_the_destination_was_expected(tmp_path):
    """The counterpart: the refusal above must not be reachable on the path the
    user did consent to, or every overwrite becomes an error."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "SKILL.md").write_text("old", encoding="utf-8")
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("new", encoding="utf-8")

    setup_tools._replace_tree(staged, dest, expect_absent=False)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / ".demo.previous").exists()


def _locking_replace(monkeypatch, *, fail_times, winerror=5, only_staged=True):
    """Stand in for ``os.replace`` while a scanner holds the source open.

    Selective on the source by default: the real lock this models is on the
    tree ``shutil.copytree`` has just written, and a double that failed every
    rename could not tell a retried swap apart from a retried rollback.

    ``time.sleep`` is patched on the ``time`` module rather than on
    ``setup_tools`` so that this file still runs against a revision that never
    imported it. Reaching through the module under test would raise
    ``AttributeError`` during setup there, and six identical errors thrown
    before a single assertion runs is a control that cannot fail for the
    reason it claims to test.
    """
    real = os.replace
    calls = []

    def fake(src, dst):
        calls.append((str(src), str(dst)))
        staged_src = ".installing" in os.path.basename(str(src))
        if (staged_src or not only_staged) and len(calls) <= fail_times:
            exc = OSError(13, "Access is denied")
            exc.winerror = winerror
            raise exc
        return real(src, dst)

    slept = []
    monkeypatch.setattr(setup_tools.os, "replace", fake)
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", True)
    return calls, slept


def test_a_transient_lock_on_the_swap_is_waited_out(tmp_path, monkeypatch):
    """An antivirus scanner opening the tree setup just copied makes the very
    next rename fail with ERROR_ACCESS_DENIED. Nothing is wrong and nothing is
    the user's -- the destination name is free -- so refusing here loses a
    skill to a lock that was over before the warning was printed."""
    dest = tmp_path / "demo"
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the new copy", encoding="utf-8")
    calls, slept = _locking_replace(monkeypatch, fail_times=4)

    setup_tools._replace_tree(staged, dest, expect_absent=True)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "the new copy"
    assert len(calls) == 5, "it should have kept trying until the lock lifted"
    assert len(slept) == 4, "one wait between each attempt"
    assert all(s > 0 for s in slept), "a retry with no wait is just a louder failure"


def test_a_lock_that_outlasts_the_retries_still_stops(tmp_path, monkeypatch):
    """The negative control for the test above, and the property the retry must
    not cost: waiting is bounded, so a handle that is genuinely still open ends
    as it did before -- raising, with the user's copy put back."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "SKILL.md").write_text("the user's copy", encoding="utf-8")
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the new copy", encoding="utf-8")
    calls, _ = _locking_replace(monkeypatch, fail_times=99)

    with pytest.raises(OSError):
        setup_tools._replace_tree(staged, dest, expect_absent=False)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "the user's copy"
    swaps = [c for c in calls if ".installing" in os.path.basename(c[0])]
    assert len(swaps) > 1, "a lock worth waiting out must have been waited out"
    assert len(swaps) < 20, "and the waiting must be bounded, not a spin"


def test_moving_the_users_copy_aside_is_never_retried(tmp_path, monkeypatch):
    """A handle on the *user's* tree is the one case where the original rule
    stands unchanged: it is their file being held open, and waiting for it to
    close so we can move it is the opposite of what the lock is telling us."""
    dest = tmp_path / "demo"
    dest.mkdir()
    (dest / "SKILL.md").write_text("the user's copy", encoding="utf-8")
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the new copy", encoding="utf-8")
    calls, slept = _locking_replace(monkeypatch, fail_times=99, only_staged=False)

    with pytest.raises(OSError):
        setup_tools._replace_tree(staged, dest, expect_absent=False)

    assert len(calls) == 1, "the aside rename must fail on its first refusal"
    assert slept == []


def test_a_refusal_that_is_not_a_lock_is_not_waited_out(tmp_path, monkeypatch):
    """Only the two errors that mean "open right now" are worth waiting on. Any
    other is a real refusal, and retrying it would turn a clear error into a
    slow one while telling the user nothing new."""
    dest = tmp_path / "demo"
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the new copy", encoding="utf-8")
    calls, slept = _locking_replace(monkeypatch, fail_times=99, winerror=87)

    with pytest.raises(OSError):
        setup_tools._replace_tree(staged, dest, expect_absent=True)

    assert len(calls) == 1
    assert slept == []


def test_posix_does_not_wait_out_a_permission_error(tmp_path, monkeypatch):
    """The same errno on POSIX means the permissions forbid it, which waiting
    does not change. Retrying there would be a slow refusal, so the platform
    check is asserted rather than assumed."""
    dest = tmp_path / "demo"
    staged = tmp_path / ".demo.installing"
    staged.mkdir()
    (staged / "SKILL.md").write_text("the new copy", encoding="utf-8")
    calls, slept = _locking_replace(monkeypatch, fail_times=99)
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)

    with pytest.raises(OSError):
        setup_tools._replace_tree(staged, dest, expect_absent=True)

    assert len(calls) == 1
    assert slept == []


def test_reconcile_waits_out_a_lock_restoring_the_only_copy(tmp_path, monkeypatch):
    """The aside copy is the user's only one here, so this rename is the most
    expensive one in the module to give up on: losing it to a transient lock
    leaves the skill under a dotted name until somebody notices."""
    dest = tmp_path / "demo"
    previous = tmp_path / ".demo.previous"
    previous.mkdir()
    (previous / "SKILL.md").write_text("the only copy", encoding="utf-8")
    calls, _ = _locking_replace(monkeypatch, fail_times=4, only_staged=False)

    setup_tools._reconcile_scratch(dest)

    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "the only copy"
    assert len(calls) == 5



# ── The shell entrypoints' query-flag list must mean the same thing here ──


@pytest.mark.parametrize("flag", ["--status", "--check-only", "--yes",
                                  "--skip-package", "--skip-optional",
                                  "--no-install-prereqs"])
def test_exact_flags_are_still_accepted(flag, capsys):
    """The spellings setup.sh and setup.ps1 match, and README documents.

    A sentinel flag forces argparse to bail before ``main`` acts on anything.
    Asserting only that the sentinel is named would pass vacuously if ``flag``
    were rejected too -- argparse reports every unrecognized argument in one
    message -- so the assertion that carries the weight is that ``flag`` is
    ABSENT from the complaint.
    """
    with pytest.raises(SystemExit) as exc:
        setup_tools.main([flag, "--nonexistent-sentinel"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    complaint = err.split("error:")[-1]
    assert "--nonexistent-sentinel" in complaint
    assert flag not in complaint, f"{flag} was itself rejected: {complaint}"


@pytest.mark.parametrize("abbreviation", ["--stat", "--sta", "--statu",
                                          "--check", "--check-onl"])
def test_abbreviations_are_rejected(abbreviation, capsys):
    """`--stat` must not quietly mean `--status`.

    setup.sh and setup.ps1 decide whether an invocation is a question
    (``--status``/``--check-only``/``--help``, which install nothing) or an
    install, and they match exact spellings. While argparse accepted
    unambiguous prefixes, ``./setup.sh --stat`` read as an install there and
    as ``--status`` here -- and the install path moves the user's
    ``~/.local/bin/{operator,handoff}`` aside on the strength of that
    disagreement. Asserting the exit code alone would not do: `2` is also what
    a genuinely unknown flag returns, which is the outcome we want, so the
    message is asserted too.
    """
    with pytest.raises(SystemExit) as exc:
        setup_tools.main([abbreviation])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err or "invalid choice" in err, err
    assert abbreviation in err
