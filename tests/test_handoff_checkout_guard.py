"""The handoff refuses to leave a mess for the next session.

Prose asking agents to clean up has been measured failing in this very
repository: agents who had read the rule left artifacts in a shared checkout
three times in one evening, and a review round left nine at once. This is the
same rule expressed where it can actually be enforced -- the tool every agent
runs on the way out.

Every guard here has a *positive* control (it fires on the thing it is for)
and a *negative* control (it stays quiet on the portable, correct spelling).
A guard that matches nothing reports the whole tree clean, which reads exactly
like success.
"""
from __future__ import annotations

import subprocess
import stat
import sys
from pathlib import Path

import pytest

import handoff_tool as ho


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=str(root), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _can_symlink(root: Path) -> bool:
    """Whether this platform will make one *here*.

    Windows refuses ``os.symlink`` without Developer Mode or elevation, and
    the answer is a property of the running box, not of the code under test.
    """
    probe = root / "_symlink_probe"
    try:
        probe.symlink_to(root / "_nothing_at_all")
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


def _junction(link: Path, target: Path) -> None:
    subprocess.run(("cmd", "/c", "mklink", "/J", str(link), str(target)),
                   capture_output=True, check=True)


def _can_junction(root: Path) -> bool:
    """Windows only, and it needs no elevation -- unlike ``os.symlink``."""
    if sys.platform != "win32":
        return False
    probe, target = root / "_junc_probe", root / "_junc_target"
    target.mkdir()
    try:
        _junction(probe, target)
    except (OSError, subprocess.CalledProcessError):
        target.rmdir()
        return False
    probe.rmdir()
    target.rmdir()
    return True


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "kept.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "kept.txt")
    _git(root, "commit", "-m", "initial")
    return root


# ── the clean case: the guard must be silent ─────────────────────


def test_a_clean_checkout_reports_nothing(repo):
    """The negative control. Without it, a guard that always fires reads the
    same as a guard that works."""
    assert ho.scan_checkout(repo) == []
    assert ho.checkout_complaints(repo) == ([], "measured")


# ── uncommitted and untracked: what git does report ──────────────


def test_an_uncommitted_edit_to_a_tracked_file_is_reported(repo):
    (repo / "kept.txt").write_text("edited\n", encoding="utf-8")
    assert "kept.txt" in ho.scan_checkout(repo)


def test_a_staged_but_uncommitted_change_is_reported(repo):
    """Staged is not committed. The reviewer incident that cost 454 lines
    turned on exactly this distinction."""
    (repo / "new.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "new.txt")
    assert "new.txt" in ho.scan_checkout(repo)


def test_an_untracked_scratch_file_is_reported(repo):
    (repo / "probe.py").write_text("print(1)\n", encoding="utf-8")
    assert "probe.py" in ho.scan_checkout(repo)


def test_every_file_in_an_untracked_directory_is_named_not_just_the_directory(
        repo):
    """`-uall`, not the default.

    The default collapses an untracked directory to its own name, so fifty
    scratch files report as one entry -- indistinguishable from a typo, and
    the reader cannot tell how much is there.
    """
    scratch = repo / "scratch"
    scratch.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (scratch / name).write_text("x\n", encoding="utf-8")

    found = ho.scan_checkout(repo)

    assert "scratch/a.txt" in found
    assert "scratch/b.txt" in found
    assert "scratch/c.txt" in found


def test_an_ignored_file_is_not_reported(repo):
    """Ignored build output is not litter, and reporting it would train the
    override flag into a reflex."""
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore build")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("x\n", encoding="utf-8")

    assert ho.scan_checkout(repo) == []


# ── the blind spot: empty directories git never reports ──────────


def test_an_empty_untracked_directory_is_reported_though_git_says_clean(repo):
    """The failure that prose cannot close.

    Git has no blob for an empty directory, so there is nothing for it to
    report: `git status` says clean while the artifact sits in the root. This
    is the exact shape of the nine reviewer directories that cost three agents
    an evening.
    """
    (repo / "ntf_review_mutabc").mkdir()

    assert _git(repo, "status", "--porcelain") == "", (
        "precondition: git itself must consider this tree clean, or the test "
        "is not exercising the blind spot it claims to")
    assert "ntf_review_mutabc/" in ho.scan_checkout(repo)


def test_a_nest_of_empty_directories_is_reported(repo):
    (repo / "stray" / "deep" / "deeper").mkdir(parents=True)
    assert "stray/" in ho.scan_checkout(repo)


def test_a_directory_holding_a_file_is_reported_by_its_file_not_as_empty(repo):
    """The two halves must not double-count the same artifact."""
    (repo / "stray").mkdir()
    (repo / "stray" / "f.txt").write_text("x\n", encoding="utf-8")

    found = ho.scan_checkout(repo)

    assert "stray/f.txt" in found
    assert "stray/" not in found


def test_an_ignored_empty_directory_is_not_reported(repo):
    """Otherwise every ignored build directory in the project is handed to an
    agent as litter to delete."""
    (repo / ".gitignore").write_text("cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore cache")
    (repo / "cache").mkdir()

    assert ho.scan_checkout(repo) == []


def test_the_git_directory_is_never_reported(repo, monkeypatch):
    """With ``holds_no_files`` forced True, so the exclusion is what decides.

    Against a clean repo this assertion holds no matter what the code does --
    the list is empty either way -- and two reviewers said so independently.
    `.git` is rejected twice over in reality (it holds files, and in a linked
    worktree it is a file), which is why the naive version could not fail.
    Stubbing the predicate removes one of those two, leaving the exclusion
    itself as the only thing standing between `.git` and a refusal message
    telling an agent to go and delete its repository.
    """
    monkeypatch.setattr(ho, "holds_no_files", lambda *_a, **_k: True)
    assert not any(p.startswith(".git") for p in ho.scan_checkout(repo))


def test_an_empty_directory_nested_under_a_tracked_one_is_reported(repo):
    """The shape that made the top-level-only scan misleading.

    A reviewer subagent that reproduces a defect under `tests/` leaves
    exactly this: git reports nothing (no blob), and a scan that looks only
    at the checkout root reports nothing either -- so `handoff` would issue a
    clean bill of health for a tree that is not clean. Being silent about a
    case it looks like it covers is worse than being loud about a limit.
    """
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "add src")
    (repo / "src" / "ntf_review_scratch").mkdir()

    assert ho.scan_checkout(repo) == ["src/ntf_review_scratch/"]


def test_a_nested_ignored_directory_is_still_not_reported(repo):
    """Walking deeper must not mean walking into `node_modules`.

    The prune set comes from git itself -- `!! node_modules/` in a single
    `--ignored=matching` call -- rather than from one `check-ignore` per
    candidate, which is what confined the old pass to the top level.
    """
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore build")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "add src")
    (repo / "build").mkdir()
    (repo / "src" / "build").mkdir()

    assert ho.scan_checkout(repo) == []


def test_only_the_outermost_empty_directory_is_named(repo):
    """One mistake described once.

    `a/`, `a/b/` and `a/b/c/` are one stray. Listing all three pads the
    refusal with two entries that vanish the moment the first is removed,
    and pushes real findings past the truncation limit.
    """
    (repo / "a" / "b" / "c").mkdir(parents=True)
    assert ho.scan_checkout(repo) == ["a/"]


def test_a_symlinked_directory_is_not_walked_or_reported(repo):
    """A link is not litter, and following one leaves the checkout.

    Two consequences if the walk descends it. A link pointing at an empty
    directory elsewhere is reported as a stray, so an agent is told to remove
    something that is not its artifact; and a link pointing at an ancestor is
    a cycle, which the budget then spends on itself, costing real findings.
    """
    if not _can_symlink(repo):
        pytest.skip("this platform will not create a symlink here")
    (repo / "elsewhere").mkdir()
    (repo / "link").symlink_to(repo / "elsewhere", target_is_directory=True)

    found = ho.scan_checkout(repo)
    assert "link/" not in found, "a link must never be named as a stray"
    assert sorted(found) == ["elsewhere/", "link"], (
        "the link is still an untracked path git reports by name -- what "
        "must not happen is the walk treating it as a directory of ours")


def test_a_dangling_junction_is_reported_though_git_is_silent_about_it(repo):
    """The case where git and the walk agreed, and neither had looked.

    A junction whose target is gone makes ``git status`` print nothing on
    stdout -- the ``could not open directory`` warning goes to *stderr*,
    which this tool does not read -- and the walk then hits ``OSError`` and
    drops it under "unreadable is not empty". Both halves say clean. Git
    cannot store a junction at all, so one inside a checkout is always
    something a process left behind.
    """
    if not _can_junction(repo):
        pytest.skip("this platform does not have directory junctions")
    (repo / "target").mkdir()
    _junction(repo / "junc", repo / "target")
    (repo / "target").rmdir()

    assert _git(repo, "status", "--porcelain", "-uall") == "", (
        "premise: git itself is silent about a dangling junction")
    assert ho.scan_checkout(repo) == ["junc"]


def test_a_live_junction_is_named_alongside_what_git_found_through_it(repo):
    """Git descends a junction itself, so its contents arrive either way.

    What this pins is that the *link* is named too. Measured, not assumed:
    the first draft of this test asserted no `junc/...` path appeared and
    failed, because those paths come from git's own `-uall` listing and not
    from the walk. Asserting the exact list is what caught that.
    """
    if not _can_junction(repo):
        pytest.skip("this platform does not have directory junctions")
    (repo / "target").mkdir()
    (repo / "target" / "deep.txt").write_text("z\n", encoding="utf-8")
    _junction(repo / "junc", repo / "target")

    assert sorted(ho.scan_checkout(repo)) == [
        "junc", "junc/deep.txt", "target/deep.txt"]


def test_a_junction_to_an_empty_directory_is_a_link_not_an_empty_stray(repo):
    """The case where the walk alone decides.

    Git is silent -- there is no blob for an empty directory, on either side
    of the link -- so whatever the walk says is the whole answer. It must not
    say `junc/`: that reads as "an empty directory you left", and the fix for
    it would be to delete a link into somebody else's tree.
    """
    if not _can_junction(repo):
        pytest.skip("this platform does not have directory junctions")
    (repo / "target").mkdir()
    _junction(repo / "junc", repo / "target")

    found = ho.scan_checkout(repo)
    assert "junc/" not in found, "a link is never an empty directory of ours"
    assert sorted(found) == ["junc", "target/"]


def test_an_ignored_junction_is_left_alone(repo):
    """The legitimate case: `node_modules` pointed at a shared cache."""
    if not _can_junction(repo):
        pytest.skip("this platform does not have directory junctions")
    (repo / ".gitignore").write_text("cache\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore cache")
    (repo / "target").mkdir()
    _junction(repo / "cache", repo / "target")

    assert ho.scan_checkout(repo) == ["target/"], (
        "an ignored junction is infrastructure, not litter")


@pytest.mark.parametrize("tag, expected", [
    (getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003), True),
    (getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C), False),
    (0xA000001F, False),  # IO_REPARSE_TAG_APPEXECLINK
    (0, False),
])
def test_only_the_mount_point_tag_counts_as_a_junction(tag, expected):
    """Runs on every platform, because the real junction tests cannot.

    A POSIX leg has no junctions to make, so without this the comparison
    itself is exercised nowhere but Windows -- and a rule enforced on one leg
    is that leg's history, not a rule. Narrow to the mount-point tag on
    purpose: a directory can be a reparse point for reasons that are not a
    junction (a cloud-storage placeholder), and refusing to walk those would
    cost findings in the ordinary case.
    """
    class _Entry:
        def stat(self, follow_symlinks=True):
            return type("S", (), {"st_reparse_tag": tag})()

    assert ho._is_junction(_Entry()) is expected


def test_a_platform_without_reparse_tags_answers_not_a_junction():
    """``st_reparse_tag`` is Windows-only, so ``AttributeError`` is the POSIX
    answer rather than a failure -- and it must not fail towards "junction",
    which would stop the walk entering any directory at all."""
    class _Entry:
        def stat(self, follow_symlinks=True):
            return type("S", (), {})()

    assert ho._is_junction(_Entry()) is False


def test_an_unwalkable_subtree_costs_a_finding_rather_than_inventing_one(
        repo, monkeypatch):
    """The walk budget bounds *enumeration*, not the emptiness verdict.

    Exhausting it means deeper candidates are never offered, so a stray goes
    unreported -- a miss. It can never turn a tracked tree into litter,
    because whether a candidate is empty is decided by ``holds_no_files``,
    which carries its own budget and fails towards "not empty".
    """
    monkeypatch.setattr(ho, "WALK_BUDGET", 1)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "add src")
    (repo / "src" / "deep_scratch").mkdir()
    (repo / "top_scratch").mkdir()

    assert ho.scan_checkout(repo) == ["top_scratch/"], (
        "the root pass must still report, and the unreached subtree must "
        "cost a finding rather than produce a wrong one")


def test_a_git_too_old_for_the_ignored_option_keeps_the_rest_of_the_guard(
        repo, monkeypatch):
    """Losing the prune set costs the empty-directory half, not the guard.

    `--ignored=matching` is git 2.16. A git that rejects it answers nothing,
    and the difference between "the tree is clean" and "I could not ask"
    is the whole point of this module -- so the plain form is tried after
    it and uncommitted work is still caught.
    """
    (repo / "scratch").mkdir()
    (repo / "probe.py").write_text("x\n", encoding="utf-8")
    real = ho._git

    def old_git(root, *args, **kw):
        if "--ignored=matching" in args:
            return False, ""
        return real(root, *args, **kw)

    monkeypatch.setattr(ho, "_git", old_git)
    assert ho.scan_checkout(repo) == ["probe.py"], (
        "the untracked file must still be found, and the empty directory "
        "must be dropped rather than reported without a prune set")


# ── holds_no_files, the ported predicate ─────────────────────────


def test_holds_no_files_is_true_for_a_tree_of_only_directories(tmp_path):
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    assert ho.holds_no_files(tmp_path / "a") is True


def test_holds_no_files_is_false_when_a_file_hides_deep_inside(tmp_path):
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "f.txt").write_text("x", encoding="utf-8")
    assert ho.holds_no_files(tmp_path / "a") is False


def test_holds_no_files_gives_up_rather_than_walking_forever(tmp_path):
    """A budget, and exceeding it answers 'not empty'.

    Answering 'empty' on exhaustion would make a deep tree look like litter
    to delete, which is the one direction this predicate must never fail in.
    """
    here = tmp_path / "deep"
    here.mkdir()
    for i in range(12):
        here = here / f"d{i}"
        here.mkdir()

    assert ho.holds_no_files(tmp_path / "deep", budget=3) is False


def test_holds_no_files_treats_an_unreadable_directory_as_not_empty(tmp_path,
                                                                    monkeypatch):
    """Unreadable is not empty; guessing either way is a claim the filesystem
    declined to support."""
    target = tmp_path / "locked"
    target.mkdir()

    def boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(ho.os, "scandir", boom)
    assert ho.holds_no_files(target) is False


# ── git could not answer ─────────────────────────────────────────


def test_a_directory_that_is_not_a_repository_reports_unknown_not_clean(
        tmp_path):
    """"No information" and "nothing to report" are the same empty list to a
    caller that only checks length, and they are opposite facts."""
    plain = tmp_path / "notarepo"
    plain.mkdir()

    assert ho.scan_checkout(plain) is None
    assert ho.checkout_complaints(plain) == ([], "unknown")


def test_a_git_that_cannot_run_at_all_reports_unknown(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("git is not installed")

    monkeypatch.setattr(ho.subprocess, "run", boom)
    assert ho.scan_checkout(tmp_path) is None


def test_a_renamed_file_is_reported_once_under_its_new_name(repo):
    """A rename record is *two* NUL-separated fields, and the second is bare.

    `git status --porcelain -z` emits ``R  new\\0old\\0`` -- the old path
    arrives with no status prefix. A reader that treats every field as a
    record slices three characters off it and reports ``name.txt`` for
    ``oldname.txt``: a path that never existed, offered to an agent as
    something to go and clean up. Found by mutation; nothing else covers it.
    """
    _git(repo, "mv", "kept.txt", "renamed.txt")
    assert ho.scan_checkout(repo) == ["renamed.txt"], (
        "the old name must not be reported, and must not be mis-sliced")


@pytest.mark.parametrize("letter, name", [("R", "renamed"), ("C", "copied")])
def test_the_second_field_of_a_two_field_record_is_never_a_finding(
        repo, monkeypatch, letter, name):
    """Fed straight to the parser, because git will not produce both on demand.

    An unstaged rename is not reported as one at all (measured: ` D old` plus
    `?? new`), and `C` needs ``status.renames=copies`` in somebody's config
    plus content similar enough for detection. Both are documented porcelain
    v1 records, so the parser has to survive them whether or not this machine
    can be persuaded to emit one.

    Asserted as an *exact* list. The first spelling here asserted that the
    mis-sliced value was absent -- and named the wrong slice, so the `C`
    mutant walked straight through it. An assertion that guesses at the
    wrong answer only holds when you guessed the wrong answer right.
    """
    stream = f"{letter}  new.txt\0old.txt\0?? probe.py\0"
    monkeypatch.setattr(
        ho, "_git",
        lambda root, *a, **k: (True, stream) if a[0] == "status" else (True, ""))

    assert sorted(ho.scan_checkout(repo)) == ["new.txt", "probe.py"], (
        f"a {name} record must neither swallow the record after it nor "
        f"contribute its own bare second field")


def test_a_status_letter_in_the_worktree_column_does_not_skip(repo, monkeypatch):
    """The index column decides, and widening it would be a silent miss.

    Reading ``code[1]`` too looks like defence in depth. It is the opposite:
    git never emits `R` or `C` there (an unstaged rename is ` D` plus `??`),
    so the branch can only fire on something that is *not* a two-field
    record -- and firing consumes the next real record. A false positive on
    the guard becomes a finding that silently vanishes.
    """
    monkeypatch.setattr(
        ho, "_git",
        lambda root, *a, **k: (True, " R kept.txt\0?? probe.py\0")
        if a[0] == "status" else (True, ""))

    assert "probe.py" in ho.scan_checkout(repo)


def test_a_record_too_short_to_hold_a_path_is_dropped(repo, monkeypatch):
    """Malformed input costs a finding, never a phantom one.

    ``record[3:]`` of a two-character record is the empty string, and an
    empty path in a refusal message names nothing while still refusing.
    """
    monkeypatch.setattr(
        ho, "_git",
        lambda root, *a, **k: (True, "??\0?? probe.py\0")
        if a[0] == "status" else (True, ""))

    assert ho.scan_checkout(repo) == ["probe.py"]


def test_a_tracked_directory_of_tracked_files_is_not_a_stray(repo):
    """The one shape `git status` says nothing about for a *good* reason.

    A committed subdirectory is invisible to `git status --porcelain`, so it
    reaches the empty-directory pass as a candidate exactly like a scratch
    directory does. Only ``holds_no_files`` separates them. Found by
    mutation: stubbing that predicate to True left every test green while
    turning `tests/` and `specs/` into reported litter in this project's own
    checkout -- a guard that refuses every handoff is worse than no guard,
    because it gets switched off rather than fixed.
    """
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "add src")

    assert ho.scan_checkout(repo) == []





# ── end to end: the refusal, and the way past it ─────────────────


@pytest.fixture
def wired(repo, tmp_path, monkeypatch):
    """A registered project whose root is a real git checkout."""
    home = tmp_path / "home"
    (home / ".operator" / "projects").mkdir(parents=True)
    restart = tmp_path / "operator" / "restart"
    restart.mkdir(parents=True)
    catalog = home / ".operator" / "projects" / "catalog.csv"
    catalog.write_text(f'"{repo.resolve()}",guid-cg\n', encoding="utf-8")
    monkeypatch.setattr(ho, "CATALOG", catalog)
    monkeypatch.setattr(ho, "state_dir", lambda: restart)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    return {"repo": repo, "restart": restart,
            # Keyed by instance, not by project: a handoff is written in the
            # first person, so `handoff/{instance}.md` is where this branch
            # publishes it. The guard suite arrived on `main` while that was
            # still `next-session.md`, and pointing it at the old path would
            # make every assertion below read a file the tool never writes --
            # which is a suite that passes by finding nothing.
            "handoff": home / ".operator" / "projects" / "guid-cg"
                       / "handoff" / "cg.md"}


def _run(wired, *extra):
    return ho.main(["--instance", "cg", "--status", "s", "--next", "n",
                    "--project-root", str(wired["repo"]), *extra])


def test_a_clean_checkout_hands_off_normally(wired):
    """The negative control for the whole feature. Without it, a guard that
    refuses everything would look identical to one that works."""
    assert _run(wired) == 0
    assert wired["handoff"].exists()


def test_a_dirty_checkout_refuses_and_writes_nothing(wired, capsys):
    (wired["repo"] / "scratch.py").write_text("probe\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(wired)

    err = capsys.readouterr().err
    assert "scratch.py" in err, "the refusal must name what is in the way"
    assert not wired["handoff"].exists(), (
        "refusing after writing the handoff would be worse than not "
        "refusing: the successor is started against a tree nobody cleaned")
    assert list(wired["restart"].iterdir()) == [], (
        "and it must not trigger the restart either")


def test_the_refusal_names_the_way_past_it(wired, capsys):
    """A guard an agent cannot satisfy is a guard an agent routes around."""
    (wired["repo"] / "scratch.py").write_text("probe\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(wired)

    assert ho.CLEAN_OVERRIDE in capsys.readouterr().err


def test_an_empty_stray_directory_alone_is_enough_to_refuse(wired):
    """The whole point. `git status` calls this tree clean."""
    (wired["repo"] / "ntf_review_mutabc").mkdir()

    with pytest.raises(SystemExit):
        _run(wired)

    assert not wired["handoff"].exists()


def test_the_override_hands_off_and_records_what_was_left(wired):
    (wired["repo"] / "scratch.py").write_text("probe\n", encoding="utf-8")

    assert _run(wired, ho.CLEAN_OVERRIDE) == 0

    written = wired["handoff"].read_text(encoding="utf-8")
    assert "scratch.py" in written, (
        "the successor must be told what was left behind; an override that "
        "only silences the check moves the surprise rather than removing it")
    assert ho.CLEAN_OVERRIDE in written


def test_the_leftovers_notice_sits_above_the_status(wired):
    """A reader must meet the warning before the content it qualifies."""
    (wired["repo"] / "scratch.py").write_text("probe\n", encoding="utf-8")
    _run(wired, ho.CLEAN_OVERRIDE)

    written = wired["handoff"].read_text(encoding="utf-8")
    assert written.index("scratch.py") < written.index("## Status")


def test_the_override_still_stamps_the_author(wired):
    """The two guarantees are independent and must not trade off."""
    (wired["repo"] / "scratch.py").write_text("probe\n", encoding="utf-8")
    _run(wired, ho.CLEAN_OVERRIDE)

    assert ho.authoring_instance(
        wired["handoff"].read_text(encoding="utf-8")) == "cg"


def test_a_long_list_says_how_much_it_is_not_showing(wired, capsys):
    """Both the refusal and the recorded notice count what they truncate.

    A successor handed twenty of sixty paths, with nothing saying so, reads
    the list as complete -- and a partial cleanup that looks finished is the
    one outcome worse than no cleanup, because nobody comes back to it.
    """
    for i in range(ho.LEFTOVER_LIMIT + 7):
        (wired["repo"] / f"probe{i:02d}.py").write_text("x\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        _run(wired)
    assert "and 7 more" in capsys.readouterr().err

    _run(wired, ho.CLEAN_OVERRIDE)
    assert "and 7 more" in wired["handoff"].read_text(encoding="utf-8")


@pytest.mark.parametrize("raw, expected", [
    ("a\nb", "a\\x0ab"),
    ("a\rb", "a\\x0db"),
    ("a\tb", "a\\x09b"),
    ("a\x7fb", "a\\x7fb"),
    ("plain/path.py", "plain/path.py"),
    ("caf\u00e9/na\u00efve.py", "caf\u00e9/na\u00efve.py"),
])
def test_display_path_escapes_control_characters_and_nothing_else(raw, expected):
    """POSIX permits every byte but `/` and NUL in a filename.

    `-z` hands the path over literally, which is correct and necessary -- a
    line-based reader would turn one such name into two paths that do not
    exist. The consequence is that the name reaches a markdown blockquote and
    a stderr list unaltered, and every structure both use is line-initial:
    one newline forges an extra bullet, or escapes the blockquote into text
    that reads like the tool's own words.

    The last two cases are the negative controls, and they are the reason
    this is not spelled ``path.encode("unicode_escape")``: that passes every
    positive case here while turning an ordinary accented filename into
    mojibake, and a successor cannot go and look for `caf\\xe9`.
    """
    assert ho.display_path(raw) == expected


def test_a_forged_line_in_a_filename_reaches_neither_message(
        wired, capsys, monkeypatch):
    """End to end, because escaping applied at only one of the two sites
    would look correct in every unit test of the escaper."""
    forged = "probe.py\n> - `SAFE TO DELETE EVERYTHING`"
    monkeypatch.setattr(ho, "scan_checkout", lambda _root: [forged])

    with pytest.raises(SystemExit):
        _run(wired)
    assert "\n> - `SAFE" not in capsys.readouterr().err

    _run(wired, ho.CLEAN_OVERRIDE)
    written = wired["handoff"].read_text(encoding="utf-8")
    assert "\n> - `SAFE" not in written
    assert "\\x0a" in written, "the name must still be recognisable"


def test_a_handoff_with_no_instance_and_no_way_to_infer_one_refuses(
        wired, capsys):
    """Identity is not optional.

    A handoff is written throughout in the first person, and the mailbox is
    per-project while the restart marker is per-instance -- so an unsigned
    handoff read by a peer has that peer acting on somebody else's branch
    claims and, destructively, somebody else's `operator inbox`.
    """
    with pytest.raises(SystemExit):
        ho.main(["--status", "s", "--next", "n",
                 "--project-root", str(wired["repo"])])

    assert "--instance" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The worktrees directory is another checkout, not this one's content.


def test_an_empty_worktrees_directory_does_not_refuse_the_handoff(repo):
    """The regression this pair of tests exists for.

    `git worktree remove` deletes the tree and never its parent, so
    `.worktrees/` outlives the checkouts it held. Git cannot see an empty
    directory, so in any repository whose `.gitignore` has not got the rule
    yet it is invisible to `git status` and visible only to the walk -- which
    reported it, refused the handoff, and told the agent to delete "what was
    scratch". The toolchain creates this directory itself, so the refusal was
    self-inflicted and permanent: nothing the agent could clean would clear
    it.
    """
    (repo / ".worktrees").mkdir()

    assert ho.scan_checkout(repo) == []


def test_a_peer_worktree_is_never_reported_as_this_checkout_litter(repo):
    """The worse half, and the reason the walk does not descend either.

    A live worktree here belongs to another agent. Reporting its contents
    hands one agent another's tree as litter to remove, against the one rule
    this project states about worktrees -- and it arrives inside a refusal
    whose remedy is deletion. Both halves are checked because they came from
    different places: `git status` reports the nested checkout itself, and the
    walk reaches the scratch directory inside it.
    """
    _git(repo, "worktree", "add", ".worktrees/feat-x", "-b", "feat/x")
    (repo / ".worktrees" / "feat-x" / "tests" / "scratch").mkdir(parents=True)

    assert ho.scan_checkout(repo) == []


def test_a_worktrees_directory_below_the_root_is_still_reported(repo):
    """Negative control: the exemption is anchored, and must stay that way.

    `operator worktree new` writes `/.worktrees/` with a leading slash for
    exactly this reason -- it names the directory at the repository root and
    nothing else. An unanchored exemption would silence a `docs/.worktrees/`
    that somebody meant to track, and an exemption wider than its reason is
    the kind that gets noticed only when something is already lost.

    `docs/` holds a file so that it is not itself the finding: the walk
    reports only the outermost empty directory of a nest, so an empty `docs/`
    would be named instead and this control would pass without ever reaching
    the directory it is about.
    """
    (repo / "docs" / ".worktrees").mkdir(parents=True)
    (repo / "docs" / "note.md").write_text("kept\n", encoding="utf-8")

    assert sorted(ho.scan_checkout(repo)) == ["docs/.worktrees/",
                                              "docs/note.md"]


def test_a_directory_merely_starting_with_the_name_is_still_reported(repo):
    """Negative control for the prefix test.

    `.worktrees-old/` is not `.worktrees/`, and a `startswith` without the
    separator would swallow it. That is the shape that turns one exemption
    into a family of them.
    """
    (repo / ".worktrees-old").mkdir()

    assert ho.scan_checkout(repo) == [".worktrees-old/"]


@pytest.mark.parametrize("rel, inside", [
    (".worktrees", True),
    (".worktrees/", True),
    (".worktrees/feat-x", True),
    (".worktrees/feat-x/tests/", True),
    ("docs/.worktrees", False),
    ("docs/.worktrees/feat-x", False),
    (".worktrees-old", False),
    (".worktreesx", False),
    ("worktrees", False),
    (".worktrees\\x", False),
])
def test_in_worktrees_names_the_root_directory_and_nothing_adjacent(rel, inside):
    """The last case is the one worth stating.

    A backslash is an ordinary character in a POSIX filename, so
    ``.worktrees\\x`` there is one file at the checkout root and not a path
    inside anything. Both producers feeding this predicate emit `/` -- `git
    status -z` always does, and the walk builds its own paths with it -- so
    translating separators could only ever suppress a real finding. This
    project has already shipped one bug of exactly that family by reaching for
    `os.path` where the string named the other platform's syntax.
    """
    assert ho._in_worktrees(rel) is inside


def test_the_guard_and_the_worktree_command_agree_on_the_directory_name():
    """`handoff_tool` copies the constant rather than importing it.

    That is deliberate -- see the comment there; the handoff must not acquire
    the work database as an import-time dependency. A copy with nothing
    binding it to its original is how two spellings drift apart silently, and
    the failure would be a guard that quietly stops covering the directory it
    was written for.
    """
    import operator_worktree

    assert ho.WORKTREES_DIR == operator_worktree.WORKTREES_DIR
