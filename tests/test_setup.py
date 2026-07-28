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
    monkeypatch.setattr(setup_tools.platform, "system", lambda: "Windows")
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", True)
    assert "psmux" in setup_tools.multiplexer_hint()
    monkeypatch.setattr(setup_tools, "IS_WINDOWS", False)
    monkeypatch.setattr(setup_tools.platform, "system", lambda: "Darwin")
    assert "brew" in setup_tools.multiplexer_hint()
    monkeypatch.setattr(setup_tools.platform, "system", lambda: "Linux")
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
