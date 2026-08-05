"""Tests for the autouse guard that pins stray test artifacts to a producer.

The guard exists because three agents once spent a combined hour trying to
attribute untracked files in a shared checkout to whichever test made them.
It failed as an unenforceable convention; these tests keep it a check.
"""
from __future__ import annotations

import os
import re
import shutil
import time
import warnings
from pathlib import Path

import pytest

import conftest


def _guard(*entries: tuple[Path, bool]):
    """Point the guard at throwaway directories instead of the real repo.

    Entries are given as (directory, recursive) and default to fatal, which is
    the behaviour the pre-existing tests were written against.
    """
    return tuple((d, recursive, True) for d, recursive in entries)


def _advisory(*entries: tuple[Path, bool]):
    """The same, but non-fatal -- the repo root's severity."""
    return tuple((d, recursive, False) for d, recursive in entries)


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
    guarded = {d: recursive for d, recursive, _fatal in conftest._GUARDED_DIRS}
    assert guarded[conftest.ROOT] is False
    assert guarded[conftest.ROOT / "skills"] is True


def test_the_shared_repo_root_warns_and_skills_fails():
    """Severity follows who else writes there, not how bad the artifact is."""
    severity = {d: fatal for d, _recursive, fatal in conftest._GUARDED_DIRS}
    assert severity[conftest.ROOT] is False
    assert severity[conftest.ROOT / "skills"] is True


def test_the_real_home_is_not_guarded_by_default():
    """A peer agent writing a handoff must not fail somebody else's test."""
    if os.environ.get("COPILOT_TOOLS_GUARD_HOME") == "1":
        return
    projects = Path.home() / ".operator" / "projects"
    assert projects not in {d for d, _recursive, _fatal in conftest._GUARDED_DIRS}


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


# ── severity and attribution ─────────────────────────────────────
# What a hit DOES is a separate question from what the guard SEES. The repo
# root has writers other than the running test -- peer agents, reviewer
# subagents, anything not run in a worktree -- so failing on it accuses the
# innocent intermittently, which reads as a flaky test and gets the guard
# switched off. These grade the split and the mtime annotation that goes
# with it.


class _FakeNode:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


class _FakeRequest:
    """Enough of a FixtureRequest for the guard: it only reads node.nodeid."""

    def __init__(self, nodeid: str = "tests/test_thing.py::test_case") -> None:
        self.node = _FakeNode(nodeid)


def _start_guard(nodeid: str = "tests/test_thing.py::test_case"):
    """Drive the real autouse fixture by hand, up to its yield.

    Exercising the fixture rather than the helpers is deliberate: the change
    under test is which branch the fixture takes, and a test that only called
    _find_strays would pass whatever the fixture then did with the result.
    """
    generator = conftest._no_stray_artifacts.__wrapped__(_FakeRequest(nodeid))
    next(generator)
    return generator


def _finish_guard(generator) -> None:
    with pytest.raises(StopIteration):
        next(generator)


def test_a_stray_in_the_shared_root_warns_instead_of_failing(
    tmp_path: Path, monkeypatch
):
    """The repo root is shared, so a hit there is news and not an accusation."""
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _advisory((tmp_path, False)))
    generator = _start_guard()

    (tmp_path / "test_cwd.js").write_text("a peer agent's probe", encoding="utf-8")

    with pytest.warns(UserWarning, match="appeared in a guarded shared directory"):
        _finish_guard(generator)


def test_a_stray_in_an_unshared_directory_still_fails(tmp_path: Path, monkeypatch):
    """Downgrading the root must not downgrade skills/ with it."""
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    generator = _start_guard()

    (tmp_path / "leak.txt").write_text("x", encoding="utf-8")

    with pytest.raises(AssertionError, match="left files outside tmp_path"):
        next(generator)


def test_a_clean_test_neither_warns_nor_fails(tmp_path: Path, monkeypatch):
    """The control: without it, a guard that never fires would pass every test."""
    monkeypatch.setattr(
        conftest, "_GUARDED_DIRS", _advisory((tmp_path, False)) + _guard((tmp_path, True))
    )
    generator = _start_guard()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _finish_guard(generator)

    assert [w for w in caught if "appeared in a guarded" in str(w.message)] == []


def test_an_advisory_stray_with_an_old_mtime_is_still_reported(
    tmp_path: Path, monkeypatch
):
    """mtime annotates; it never suppresses.

    The before/after name diff is what establishes that the path appeared
    during this test. st_mtime is not a creation time, so filtering on it
    could only ever subtract true positives -- shutil.copy2, os.utime, archive
    extraction and NTFS timestamp tunnelling all produce a genuinely new
    directory entry carrying an old timestamp.
    """
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _advisory((tmp_path, False)))
    generator = _start_guard()

    stale = tmp_path / "copied_with_copy2.txt"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (0, 0))

    with pytest.warns(UserWarning, match="copied_with_copy2.txt"):
        _finish_guard(generator)


def test_a_copy2_preserving_an_old_mtime_is_still_reported(
    tmp_path: Path, monkeypatch
):
    """The reviewer's reproduction, kept as a test."""
    source = tmp_path / "source"
    source.mkdir()
    original = source / "fixture.txt"
    original.write_text("x", encoding="utf-8")
    os.utime(original, (0, 0))

    guarded = tmp_path / "guarded"
    guarded.mkdir()
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _advisory((guarded, False)))
    generator = _start_guard()

    shutil.copy2(original, guarded / "fixture.txt")

    with pytest.warns(UserWarning, match="fixture.txt"):
        _finish_guard(generator)


def test_an_advisory_stray_with_an_unreadable_mtime_is_still_reported(
    tmp_path: Path, monkeypatch
):
    """Cannot-tell must not be filed as stale.

    This is the whole reason _appeared_since is three-valued. Collapsing an
    unreadable mtime into "old" would drop the stray silently, and silent is
    the one failure mode a detector may not have.
    """
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _advisory((tmp_path, False)))
    generator = _start_guard()

    victim = tmp_path / "unreadable.txt"
    victim.write_text("x", encoding="utf-8")
    real_lstat = os.lstat

    def denied(path, *args, **kwargs):
        if str(path) == str(victim):
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)

    with pytest.warns(UserWarning, match="mtime unreadable"):
        _finish_guard(generator)


def test_an_old_mtime_does_not_excuse_a_stray_in_an_unshared_directory(
    tmp_path: Path, monkeypatch
):
    """The asymmetry, stated as a test.

    mtime narrows an accusation; it does not establish authorship. Using it to
    suppress a FATAL hit would trade real detection for a heuristic -- a test
    that unpacks a fixture with preserved timestamps writes into skills/ and
    would never be reported. So it filters the advisory line only.
    """
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _guard((tmp_path, False)))
    generator = _start_guard()

    stale = tmp_path / "unpacked_with_old_timestamps.txt"
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (0, 0))

    with pytest.raises(AssertionError, match="unpacked_with_old_timestamps"):
        next(generator)


def test_appeared_since_reports_unknown_rather_than_stale(tmp_path: Path, monkeypatch):
    absent = tmp_path / "gone.txt"

    assert conftest._appeared_since(str(absent), time.time()) is None


def test_appeared_since_never_lets_an_exception_escape_teardown():
    """os.lstat raises ValueError, not OSError, for an embedded NUL.

    Unreachable from an os.scandir name, which cannot contain one, but an
    exception escaping the fixture would fail an unrelated test -- the exact
    misattribution this module was changed to stop producing.
    """
    assert conftest._appeared_since("a\x00b", time.time()) is None


def test_appeared_since_uses_lstat_so_a_dangling_symlink_has_an_mtime(
    tmp_path: Path, monkeypatch
):
    """stat follows links and would call a dangling one unreadable.

    Simulated rather than skipped: creating a real symlink needs privileges
    this suite cannot assume on Windows, and a test that opts out on one
    platform grades nothing there. Patching os.stat while leaving os.lstat
    alone reproduces exactly the differential a dangling link produces.
    """
    link = tmp_path / "broken_sym"
    link.write_text("x", encoding="utf-8")
    real_stat = os.stat

    def follows_into_nothing(path, *args, **kwargs):
        if str(path) == str(link):
            raise FileNotFoundError("dangling")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", follows_into_nothing)

    assert conftest._appeared_since(str(link), 0.0) is True


def test_the_advisory_notice_states_what_was_observed_not_who_did_it():
    """A report that overstates its evidence is one the reader learns to skip."""
    notice = conftest._stray_notice(
        "tests/test_integration.py::test_runner",
        ["/repo/test_cwd.js (mtime within this test)"],
    )

    assert "tests/test_integration.py::test_runner" in notice
    assert "/repo/test_cwd.js" in notice
    assert "appeared" in notice
    assert "does not establish" in notice


def test_the_advisory_notice_distinguishes_the_three_mtime_verdicts():
    assert conftest._describe("p", True).endswith("(mtime within this test)")
    assert conftest._describe("p", False).endswith("(mtime before this test)")
    assert conftest._describe("p", None).endswith("(mtime unreadable)")


def test_a_duplicate_guarded_directory_cannot_narrow_the_scan(
    tmp_path: Path, monkeypatch
):
    """Last-wins on a repeated path would hide everything nested under it.

    The snapshot is keyed by directory, so listing one twice -- once recursive
    and once not -- used to leave the shallower scan in place and report a
    nested artifact as nothing at all. A misconfiguration may only ever fail
    toward MORE visibility.
    """
    monkeypatch.setattr(
        conftest,
        "_GUARDED_DIRS",
        ((tmp_path, True, True), (tmp_path, False, False)),
    )
    (tmp_path / "sub").mkdir()
    before = conftest._snapshot_guarded()

    (tmp_path / "sub" / "new.txt").write_text("x", encoding="utf-8")

    assert conftest._find_strays(before, conftest._snapshot_guarded()) == [
        str(tmp_path / "sub" / "new.txt")
    ]


def test_a_duplicate_guarded_directory_cannot_downgrade_severity(
    tmp_path: Path, monkeypatch
):
    """The same rule for how loudly it objects, not just what it sees."""
    monkeypatch.setattr(
        conftest,
        "_GUARDED_DIRS",
        ((tmp_path, False, True), (tmp_path, False, False)),
    )
    generator = _start_guard()

    (tmp_path / "leak.txt").write_text("x", encoding="utf-8")

    with pytest.raises(AssertionError, match="leak.txt"):
        next(generator)


def test_merging_duplicates_leaves_a_normal_configuration_alone():
    """The control: the merge must not quietly rewrite the real settings."""
    assert conftest._guarded_dirs() == tuple(conftest._GUARDED_DIRS)


def test_a_peer_file_is_reported_once_and_not_by_every_later_test(
    tmp_path: Path, monkeypatch
):
    """Removing the mtime filter must not turn one peer file into a storm.

    Nothing suppresses an advisory hit any more, so the only thing keeping a
    long suite from warning once per test is that the snapshot is taken per
    test: a file that appeared during test N is in the BEFORE set of test
    N+1. That is load-bearing now, so it is asserted rather than assumed.
    """
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _advisory((tmp_path, False)))

    first = _start_guard("tests/t.py::test_one")
    (tmp_path / "peer_wrote_this.txt").write_text("x", encoding="utf-8")
    with pytest.warns(UserWarning, match="peer_wrote_this.txt"):
        _finish_guard(first)

    for nodeid in ("tests/t.py::test_two", "tests/t.py::test_three"):
        later = _start_guard(nodeid)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _finish_guard(later)
        assert [w for w in caught if "appeared in a guarded" in str(w.message)] == []


def test_a_peer_file_deleted_and_recreated_is_reported_each_time(
    tmp_path: Path, monkeypatch
):
    """The other half: reported once per appearance, not once ever."""
    monkeypatch.setattr(conftest, "_GUARDED_DIRS", _advisory((tmp_path, False)))
    victim = tmp_path / "churn.txt"

    for round_number in range(2):
        generator = _start_guard(f"tests/t.py::test_{round_number}")
        victim.write_text("x", encoding="utf-8")
        with pytest.warns(UserWarning, match="churn.txt"):
            _finish_guard(generator)
        victim.unlink()


# ── the real project catalog ─────────────────────────────────────
# These guard a file the directory scan above structurally cannot see. It
# compares sets of NAMES, so a file rewritten in place is identical before and
# after. That is not hypothetical: the real ~/.operator/projects/catalog.csv was
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


def test_the_documented_bank_name_is_the_one_the_guard_writes(
        tmp_path: Path, monkeypatch):
    """The name in docs/operator.md is the name that actually appears on disk.

    A reader who finds one of these beside their catalog has one way to learn
    what it is: search for the name. That only works while the documented name
    and the produced name are the same string, and nothing else checks it --
    the guard is silent in every passing run, so a rename here would stay
    invisible until the day it mattered, which is the day somebody has just
    lost their registrations.

    The prefix is read out of the documentation rather than written down a
    second time here, so changing the suffix in ``_bank`` without touching the
    docs fails this test rather than passing it.

    Every occurrence is checked, not the first one. A search-and-check that
    stops at the first hit passes while the Files table -- the place a reader
    actually looks something up -- carries a stale name, because the prose two
    paragraphs below still carries the right one. The population is all of
    them; taking the first is the same narrowing that let the original loss
    through.
    """
    docs = (Path(__file__).resolve().parent.parent
            / "docs" / "operator.md").read_text(encoding="utf-8")
    documented = re.findall(r"catalog\.csv\.pre-test-[^\s`|]*", docs)
    assert documented, "docs/operator.md does not name the banked copy at all"

    # Named in the Files table specifically, since that is the index rather
    # than a mention in passing, and checked as a literal so that renaming the
    # row cannot be absorbed by a correct mention elsewhere in the file.
    rows = [line for line in docs.splitlines()
            if line.startswith("| `~/.operator/projects/catalog.csv.pre-test-")]
    assert rows, "the banked copy is not listed in the Files table"
    table_name = re.search(r"catalog\.csv\.pre-test-[^\s`|]*", rows[0]).group(0)
    assert "<" in table_name, (
        f"the Files table names no placeholder for the varying part: {rows[0]!r}")
    # The one form of the name the docs are entitled to use without a
    # placeholder, taken from the table rather than written down here so that
    # it cannot drift from the row this test just validated.
    stem = table_name.split("<")[0]

    catalog = tmp_path / "catalog.csv"
    catalog.write_bytes(b"rows\n")
    _aim_at(monkeypatch, catalog)

    banked = conftest._bank(b"rows\n")

    assert banked is not None
    for occurrence in documented:
        # A mention that ends a sentence picks up the full stop, which is
        # prose and not part of the name. Ambiguous in principle; in practice
        # this guard does not write a name ending in punctuation.
        occurrence = occurrence.rstrip(".,;:)")
        parts = [p for p in re.split(r"(<[^>]*>)", occurrence) if p]
        if any(p.startswith("<") for p in parts):
            # A template names the whole shape, so the whole shape is the
            # claim. Checking only the stem would let a documented
            # ``...<timestamp>.bak`` pass on the strength of its prefix while
            # naming a file that never appears.
            shape = "".join(".+" if p.startswith("<") else re.escape(p)
                            for p in parts)
            matches = re.fullmatch(shape, banked.name) is not None
        else:
            # A bare mention is allowed to be the stem and nothing else.
            # Accepting any prefix would accept ``...pre-test-2``, which is a
            # prefix of a real name only by the accident of what today's
            # timestamp starts with, and is not a name anything ever writes.
            matches = occurrence == stem
        assert matches, (
            f"the guard writes {banked.name!r} but docs/operator.md documents "
            f"{occurrence!r}: a reader searching for the documented name finds "
            "nothing")
