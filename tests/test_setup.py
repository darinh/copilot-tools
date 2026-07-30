"""Tests for cross-platform setup."""
from __future__ import annotations

from pathlib import Path

import pytest

import setup_tools


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
