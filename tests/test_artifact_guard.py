"""Tests for the autouse guard that pins stray test artifacts to a producer.

The guard exists because three agents once spent a combined hour trying to
attribute untracked files in a shared checkout to whichever test made them.
It failed as an unenforceable convention; these tests keep it a check.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import conftest


def _guard(*entries: tuple[Path, bool]):
    """Point the guard at throwaway directories instead of the real repo."""
    return entries


def test_snapshot_lists_immediate_entries(tmp_path: Path, monkeypatch):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))

    assert conftest._snapshot_guarded() == {
        tmp_path: (True, frozenset({"a.txt", "sub"}))
    }


def test_snapshot_ignores_tooling_churn(tmp_path: Path, monkeypatch):
    for name in conftest._GUARD_IGNORED:
        (tmp_path / name).mkdir()
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))

    assert conftest._snapshot_guarded() == {tmp_path: (True, frozenset())}


def test_recursive_scan_skips_ignored_subtrees(tmp_path: Path, monkeypatch):
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_text("x", encoding="utf-8")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "file.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, True)))

    assert conftest._snapshot_guarded() == {
        tmp_path: (True, frozenset({"real", "real/file.md"}))
    }


def test_missing_directory_is_recorded_as_absent(tmp_path: Path, monkeypatch):
    absent = tmp_path / "not-created-yet"
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((absent, False)))

    assert conftest._snapshot_guarded() == {absent: (False, frozenset())}


def test_creating_a_guarded_directory_is_a_stray_even_when_empty(
    tmp_path: Path, monkeypatch
):
    """An empty new directory and a missing one are not the same thing."""
    absent = tmp_path / "projects"
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((absent, True)))
    before = conftest._snapshot_guarded()

    absent.mkdir()

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == [str(absent)]


def test_creating_a_guarded_directory_with_content_reports_both(
    tmp_path: Path, monkeypatch
):
    absent = tmp_path / "projects"
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((absent, True)))
    before = conftest._snapshot_guarded()

    absent.mkdir()
    (absent / "symlink_test").write_text("planted", encoding="utf-8")

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == sorted(
        [str(absent), str(absent / "symlink_test")]
    )


def test_no_strays_when_nothing_changes(tmp_path: Path, monkeypatch):
    (tmp_path / "pre-existing").mkdir()
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    before = conftest._snapshot_guarded()

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == []


def test_pre_existing_artifact_does_not_blame_an_innocent_test(
    tmp_path: Path, monkeypatch
):
    """A leak already on disk is in the before-set, so it cannot cascade."""
    (tmp_path / ".test_root4").mkdir()
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    before = conftest._snapshot_guarded()

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == []


def test_a_new_file_is_reported(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    before = conftest._snapshot_guarded()

    (tmp_path / "test.txt").write_text("leaked", encoding="utf-8")

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == [
        str(tmp_path / "test.txt")
    ]


def test_strays_in_a_tracked_subdirectory_are_reported(tmp_path: Path, monkeypatch):
    """skills/ is tracked, so `git add -A` would sweep this one up."""
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(
        conftest, "_GUARDED_DIRS", _guard((tmp_path, False), (skills, True))
    )
    before = conftest._snapshot_guarded()

    (skills / "demo").mkdir()

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == [
        str(skills / "demo")
    ]


def test_a_write_inside_an_existing_subdirectory_is_reported(
    tmp_path: Path, monkeypatch
):
    """Top-level-name diffing alone would miss this; skills/ is walked in full."""
    skills = tmp_path / "skills"
    (skills / "code-intelligence").mkdir(parents=True)
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((skills, True)))
    before = conftest._snapshot_guarded()

    (skills / "code-intelligence" / "leak.txt").write_text("x", encoding="utf-8")

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == [
        str(skills / "code-intelligence" / "leak.txt")
    ]


def test_removals_are_not_reported_as_strays(tmp_path: Path, monkeypatch):
    victim = tmp_path / "gone"
    victim.mkdir()
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    before = conftest._snapshot_guarded()

    victim.rmdir()

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == []


def test_multiple_strays_are_all_listed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    before = conftest._snapshot_guarded()

    for name in ("dest_case", "repo_ext", "my_ext"):
        (tmp_path / name).mkdir()

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == sorted(
        str(tmp_path / n) for n in ("dest_case", "repo_ext", "my_ext")
    )


def test_report_names_the_test_and_every_artifact():
    """Attribution is the feature: nodeid and paths must both be present."""
    report = conftest._stray_report(
        "tests/test_setup.py::test_install", ["/repo/skills/demo", "/repo/test.txt"]
    )

    assert "tests/test_setup.py::test_install" in report
    assert "/repo/skills/demo" in report
    assert "/repo/test.txt" in report
    assert "tmp_path" in report


def test_guard_is_registered_as_an_autouse_fixture():
    marker = conftest._no_stray_artifacts._fixture_function_marker
    assert marker.autouse is True
    assert marker.scope == "function"


def test_the_repo_root_and_skills_are_guarded():
    guarded = dict(conftest._GUARDED_DIRS)
    assert guarded[conftest.ROOT] is False
    assert guarded[conftest.ROOT / "skills"] is True


def test_the_real_home_is_not_guarded_by_default():
    """A peer agent writing a handoff must not fail somebody else's test."""
    if os.environ.get("COPILOT_TOOLS_GUARD_HOME") == "1":
        return
    projects = Path.home() / ".copilot" / "projects"
    assert projects not in dict(conftest._GUARDED_DIRS)


def test_guard_survives_an_unreadable_directory(tmp_path: Path, monkeypatch):
    """A scandir failure must not take the whole suite down."""
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))

    def boom(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "scandir", boom)

    with pytest.warns(UserWarning, match="could not read"):
        assert conftest._snapshot_guarded() == {tmp_path: (True, frozenset())}


def test_recursive_walk_errors_are_surfaced_not_swallowed(tmp_path: Path, monkeypatch):
    """os.walk hides traversal errors by default; an unread subtree must warn."""
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, True)))

    def boom(_path, onerror=None, **_kwargs):
        error = PermissionError("denied")
        if onerror is not None:
            onerror(error)
        return iter(())

    monkeypatch.setattr(os, "walk", boom)

    with pytest.warns(UserWarning, match="Strays under it will not be detected"):
        assert conftest._snapshot_guarded() == {tmp_path: (True, frozenset())}
