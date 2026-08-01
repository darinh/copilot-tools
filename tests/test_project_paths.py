"""Worktree-aware project identity.

All work happens in worktrees under ``<repoRoot>/.worktrees/``, so the
question "which project am I in?" cannot be answered with the working
directory. These tests use real git worktrees rather than stubs, because the
failure being guarded against is git's own behaviour: inside a linked worktree
``rev-parse --show-toplevel`` returns the worktree, and a project resolved that
way looks unregistered and mints a duplicate catalog entry.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import denied

import copilot_operator as op
import handoff_tool as ho
import project_paths
from project_paths import guid_is_usable, primary_repo_root


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo_with_worktree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    _git("worktree", "add", ".worktrees/feat-x", "-b", "feat/x", cwd=root)
    return root, root / ".worktrees" / "feat-x"


def _catalog(tmp_path, monkeypatch, root: Path, guid: str) -> Path:
    home = tmp_path / "home"
    (home / ".copilot" / "projects").mkdir(parents=True)
    catalog = home / ".copilot" / "projects" / "catalog.csv"
    catalog.write_text(f'"{root.resolve()}",{guid}\n', encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return catalog


# ── root resolution ─────────────────────────────────────────────
def test_primary_root_from_the_primary_checkout(repo_with_worktree):
    root, _ = repo_with_worktree
    assert primary_repo_root(root).resolve() == root.resolve()


def test_primary_root_from_inside_a_worktree(repo_with_worktree):
    """A worktree is another checkout of one project, not a second project."""
    _root, wt = repo_with_worktree
    assert primary_repo_root(wt).resolve() == _root.resolve()


def test_primary_root_outside_a_repository_is_unchanged(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert primary_repo_root(plain) == plain


def test_primary_root_of_a_missing_path_is_unchanged(tmp_path):
    gone = tmp_path / "nope"
    assert primary_repo_root(gone) == gone


def test_primary_root_of_a_worktree_it_cannot_examine_is_still_the_root(
        repo_with_worktree, monkeypatch):
    """A stat that fails is not a path outside a repository.

    ``is_dir()`` raises on EACCES, and the enclosing handler returned ``start``
    unchanged -- the documented answer for "not in a repo", handed back for a
    checkout that is very much in one. In a worktree that answer is the
    worktree, which looks like an unregistered project and mints the duplicate
    catalog entry this module exists to prevent.

    Scope, stated honestly: a directory the process genuinely cannot search
    will fail git's ``chdir`` too, and that failure is caught below and still
    returns ``base``. This fix does not rescue that case and does not claim
    to. What it rescues is the larger class where the *stat* fails and the
    directory is still usable -- a Windows sharing violation from a scanner
    holding a handle, EIO on a network home, a transient denial between two
    polls. So the assertion that matters is not only the answer but that git
    was consulted at all, which is the behaviour that changed.
    """
    root, wt = repo_with_worktree
    calls: list[str | None] = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        calls.append(kwargs.get("cwd"))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(project_paths.subprocess, "run", spy)
    with denied(monkeypatch, wt) as seen:
        found = primary_repo_root(wt)
    assert seen["n"], "the denial never fired; the test proves nothing"
    assert calls == [str(wt)], \
        f"the failed probe short-circuited the git call: {calls!r}"
    assert found.resolve() == root.resolve(), \
        "an unexaminable worktree was mistaken for a project of its own"


# ── catalog lookups ─────────────────────────────────────────────
def test_handoff_resolves_the_catalog_from_a_worktree(
        repo_with_worktree, tmp_path, monkeypatch):
    root, wt = repo_with_worktree
    catalog = _catalog(tmp_path, monkeypatch, root, "guid-wt")
    restart = tmp_path / "operator" / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(ho, "CATALOG", catalog)
    monkeypatch.setattr(ho, "state_dir", lambda: restart)
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)

    rc = ho.main([
        "--instance", "proj",
        "--status", "done", "--next", "next",
        "--project-root", str(wt),
    ])

    assert rc == 0
    handoff = (Path.home() / ".copilot" / "projects" / "guid-wt"
               / "next-session.md")
    assert handoff.is_file()


def test_operator_finds_the_handoff_file_from_a_worktree(
        repo_with_worktree, tmp_path, monkeypatch):
    root, wt = repo_with_worktree
    catalog = _catalog(tmp_path, monkeypatch, root, "guid-op")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    found = op.project_handoff_file(wt)

    assert found is not None
    assert found.parent.name == "guid-op"


def test_an_uncatalogued_project_is_still_reported_as_such(
        repo_with_worktree, tmp_path, monkeypatch):
    """Resolving the primary root must not invent a match for a project that
    genuinely has no catalog entry."""
    _root, wt = repo_with_worktree
    catalog = _catalog(tmp_path, monkeypatch, tmp_path / "elsewhere", "other")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    assert op.project_handoff_file(wt) is None


# -- one definition of a valid project id ------------------------
def test_the_writer_and_the_reader_share_one_guid_predicate():
    """Reader and writer must not hold separate ideas of a valid id.

    `handoff_tool` creates ~/.copilot/projects/<guid>/next-session.md and
    `copilot_operator` resolves it again to report on it. When those two
    disagreed, an id the writer refused to create was still one the reader
    would happily resolve -- and `../../elsewhere` resolved outside the
    projects root entirely. Sharing the object, not the source, is what keeps
    them from drifting apart again.
    """
    assert ho.guid_is_usable is project_paths.guid_is_usable
    assert op.guid_is_usable is project_paths.guid_is_usable


@pytest.mark.parametrize("guid", [
    "", ".", "..", "../../elsewhere", "a/b", "a\\b", "victim.", "victim ",
    "bad:stream", "CON", "NUL", "a\x00b", "...",
])
def test_an_id_that_is_not_one_plain_directory_name_is_refused(guid):
    assert not guid_is_usable(guid)


@pytest.mark.parametrize("guid", [
    "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "abc-123", "..dots",
])
def test_an_ordinary_project_id_is_accepted(guid):
    assert guid_is_usable(guid)
