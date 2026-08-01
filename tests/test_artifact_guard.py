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


# ── the real project catalog ─────────────────────────────────────
# These guard a file the directory scan above structurally cannot see. It
# compares sets of NAMES, so a file rewritten in place is identical before and
# after. That is not hypothetical: the real ~/.copilot/projects/catalog.csv was
# overwritten by a test run with a single fixture row, six project
# registrations were lost, and the suite stayed green.


def _aim_at(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(conftest, "_REAL_CATALOG", path)


def test_state_distinguishes_absent_from_unreadable(tmp_path: Path, monkeypatch):
    """Absent is a fact about the file; unreadable is the absence of a fact."""
    missing = tmp_path / "gone.csv"
    _aim_at(monkeypatch, missing)
    assert conftest._catalog_state() is None

    present = tmp_path / "catalog.csv"
    present.write_bytes(b"rows\n")
    _aim_at(monkeypatch, present)

    def denied(_self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert conftest._catalog_state() is conftest._UNREADABLE


def test_an_untouched_catalog_is_not_complained_about(tmp_path: Path, monkeypatch):
    catalog = tmp_path / "catalog.csv"
    catalog.write_bytes(b'"C:\\repo",guid\n')
    _aim_at(monkeypatch, catalog)

    assert conftest._catalog_complaint(catalog.read_bytes(), "t") is None


def test_a_rewritten_catalog_is_reported_and_banked(tmp_path: Path, monkeypatch):
    """The run goes red, the old bytes survive, and nothing is overwritten."""
    catalog = tmp_path / "catalog.csv"
    original = b'"C:\\repos\\prism",7dbf3eb0\n"C:\\repos\\copilot-tools",c48add2d\n'
    catalog.write_bytes(original)
    _aim_at(monkeypatch, catalog)
    before = conftest._catalog_state()

    clobbered = b'"/tmp/tmpw067_vjd/repo",the-guid\n'
    catalog.write_bytes(clobbered)

    complaint = conftest._catalog_complaint(before, "tests/test_x.py::test_y")
    assert complaint is not None
    assert "tests/test_x.py::test_y" in complaint
    # The bytes are in the message as well, so the loss is recoverable from a
    # CI log even where the bank could not be written.
    assert "copilot-tools" in complaint

    banked = list(tmp_path.glob("catalog.csv.pre-test-*"))
    assert len(banked) == 1
    assert banked[0].read_bytes() == original
    assert str(banked[0]) in complaint
    # The live file is left exactly as the test left it. Undoing a suspected
    # clobber by overwriting would destroy a concurrent, legitimate
    # registration -- a preserver that destroys.
    assert catalog.read_bytes() == clobbered


def test_banking_never_overwrites_an_existing_bank(tmp_path: Path, monkeypatch):
    """Two clobbers in one second must not have the second eat the first."""
    catalog = tmp_path / "catalog.csv"
    catalog.write_bytes(b"first\n")
    _aim_at(monkeypatch, catalog)
    first = conftest._catalog_state()
    catalog.write_bytes(b"clobber one\n")
    assert conftest._catalog_complaint(first, "t") is not None

    second = conftest._catalog_state()
    catalog.write_bytes(b"clobber two\n")
    assert conftest._catalog_complaint(second, "t") is not None

    banked = sorted(p.read_bytes() for p in tmp_path.glob("catalog.csv.pre-test-*"))
    assert banked == [b"clobber one\n", b"first\n"]


def test_a_catalog_created_by_a_test_is_reported_without_a_bank(tmp_path: Path,
                                                               monkeypatch):
    """There are no prior bytes to bank when the file did not exist."""
    catalog = tmp_path / "catalog.csv"
    _aim_at(monkeypatch, catalog)
    before = conftest._catalog_state()
    assert before is None

    catalog.write_bytes(b"invented\n")

    complaint = conftest._catalog_complaint(before, "t")
    assert complaint is not None
    assert "(did not exist)" in complaint
    assert not list(tmp_path.glob("catalog.csv.pre-test-*"))


def test_an_unreadable_catalog_yields_no_verdict(tmp_path: Path, monkeypatch):
    """Unreadable going in establishes nothing, so nothing may be concluded.

    Treating it as absent would fabricate a "this test created it" accusation
    out of a permissions error -- the same conflation the mail reader and the
    install manifest both reject by name.
    """
    catalog = tmp_path / "catalog.csv"
    catalog.write_bytes(b"real rows\n")
    _aim_at(monkeypatch, catalog)

    assert conftest._catalog_complaint(conftest._UNREADABLE, "t") is None
    assert catalog.read_bytes() == b"real rows\n"
    assert not list(tmp_path.glob("catalog.csv.pre-test-*"))


def test_a_failed_bank_still_reports_the_lost_contents(tmp_path: Path, monkeypatch):
    """If the bank cannot be written the bytes must still reach the reader."""
    catalog = tmp_path / "catalog.csv"
    catalog.write_bytes(b"original rows\n")
    _aim_at(monkeypatch, catalog)
    before = conftest._catalog_state()
    catalog.write_bytes(b"clobbered\n")

    def denied(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(conftest, "open", denied, raising=False)

    complaint = conftest._catalog_complaint(before, "t")
    assert complaint is not None
    assert "banked at" not in complaint
    assert "original rows" in complaint


def test_an_unreadable_catalog_at_teardown_yields_no_verdict(tmp_path: Path,
                                                             monkeypatch):
    """Unreadable coming OUT establishes nothing either.

    The sentinel is not equal to any bytes, so comparing it against the
    pre-test state finds a difference and convicts the running test of a
    change nobody observed. On Windows a peer holding the file open is enough
    to produce that, which would make the guard flaky in precisely the
    multi-agent setting it was written for.
    """
    catalog = tmp_path / "catalog.csv"
    catalog.write_bytes(b"real rows\n")
    _aim_at(monkeypatch, catalog)
    before = conftest._catalog_state()
    assert before == b"real rows\n"

    def denied(_self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_bytes", denied)

    assert conftest._catalog_complaint(before, "t") is None
    assert not list(tmp_path.glob("catalog.csv.pre-test-*"))
