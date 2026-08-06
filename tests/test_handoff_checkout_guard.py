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
from pathlib import Path

import pytest

import handoff_tool as ho


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=str(root), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


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


def test_the_git_directory_is_never_reported(repo):
    assert not any(p.startswith(".git") for p in ho.scan_checkout(repo))


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


def test_when_check_ignore_cannot_answer_the_empty_dir_half_is_dropped(
        repo, monkeypatch):
    """Not reported unfiltered -- dropped.

    This is a regression pin, not a hypothetical. The first spelling here
    passed ``-z`` without ``--stdin``, which git rejects outright ("fatal:
    -z only makes sense with --stdin"), so check-ignore never once answered
    and the fallback reported *everything* -- while the docstring beside it
    said the half was dropped. Prose and behaviour disagreed and only the
    behaviour ran.
    """
    (repo / "node_modules").mkdir()
    (repo / "scratch").mkdir()
    real = ho._git

    def selective(root, *args, **kw):
        if args and args[0] == "check-ignore":
            return False, ""
        return real(root, *args, **kw)

    monkeypatch.setattr(ho, "_git", selective)
    assert ho.scan_checkout(repo) == [], (
        "an unanswerable filter must cost findings, never invent them")


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
            "handoff": home / ".operator" / "projects" / "guid-cg"
                       / "next-session.md"}


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
