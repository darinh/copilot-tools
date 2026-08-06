"""The backlog *workflow*: filing, approving, and checking in.

``tests/test_backlog_conformance.py`` covers the rules ``check()`` applies to
items at rest. This module covers the three commands that put work into the
queue and take it out -- ``backlog new``, ``backlog approve`` and
``backlog scrum`` -- and the durable watermark the last of them measures
against.

The shape of the controls is the same as next door and for the same reason: a
positive control that proves each refusal fires, and a negative control that
proves the correct spelling still gets through. A tool that refused everything
would satisfy every positive control in this file, and a queue that is
permanently empty reads as a quiet backlog rather than a broken one.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

import backlog_tool
from backlog_tool import (
    BACKLOG_DIRNAME,
    ITEM_FILENAME,
    OPEN_STATUS,
    PROPOSED_STATUS,
    STATUSES,
    TERMINAL_STATUSES,
    WatermarkError,
)

GIT_ENV = ["-c", "user.email=test@example.invalid", "-c", "user.name=test"]


def git(root, *args, check=True) -> str:
    proc = subprocess.run(["git", *GIT_ENV, *args], cwd=str(root),
                          capture_output=True, encoding="utf-8",
                          errors="replace")
    if check:
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A throwaway repository with one commit.

    A plain temp directory would make every git call fail, so the report would
    look right for the wrong reason and nothing about the resolved path would
    ever be exercised.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "commit", "-q", "--allow-empty", "-m", "seed")
    return root


def backlog_dir(root: Path) -> Path:
    return root / BACKLOG_DIRNAME


def file_one(root: Path, **kwargs) -> Path:
    kwargs.setdefault("title", "A filed request")
    kwargs.setdefault("evidence", "Observed 2026-08-05, and reproducible.")
    return backlog_tool.create_item(backlog_dir(root), **kwargs)


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------

def test_a_filed_item_conforms_and_parses_back(repo):
    """The negative control for every refusal below.

    The tool must produce a document this project's own checker accepts. A
    writer and a reader of one format that disagree is the failure mode the
    hand-rolled parser exists to keep visible, and it would surface here as an
    item nobody can validate rather than as an error.
    """
    path = file_one(repo)
    assert ITEM_FILENAME.match(path.name), path.name
    assert backlog_tool.check(repo) == []
    item = backlog_tool.parse_item(path)
    assert item.status == PROPOSED_STATUS, "filing is not approving"
    assert item.section("## Evidence")


def test_filing_defaults_to_awaiting_approval(repo):
    """The default is the whole gate. An agent that files an item as ``open``
    has approved its own work."""
    item = backlog_tool.parse_item(file_one(repo))
    assert item.status == PROPOSED_STATUS
    assert backlog_tool.workable(backlog_tool.load(backlog_dir(repo))[0]) == []


def test_an_item_with_no_evidence_is_refused(repo):
    """R6 says an item with no evidence is a rumour. A tool that emitted them
    faster than a human can weed them is worse than no tool."""
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        file_one(repo, evidence="   \n\n  ")
    assert "rumour" in str(exc.value)
    assert not backlog_dir(repo).exists() or not list(
        backlog_tool.item_paths(backlog_dir(repo)))


def test_a_multi_line_title_is_refused(repo):
    """Its second line would land in the front-matter block and parse as a
    malformed field -- a file this module wrote that its own parser rejects."""
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        file_one(repo, title="A title\nand its runaway second line")
    assert "one line" in str(exc.value)


def test_an_empty_title_is_refused(repo):
    with pytest.raises(backlog_tool.BacklogFormatError):
        file_one(repo, title="   ")


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_filing_an_item_that_is_already_finished_is_refused(repo, status):
    """A new item cannot be born closed: it would need a closing date and, for
    a close, the SHA of work that has not happened."""
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        file_one(repo, status=status)
    assert status in str(exc.value)


def test_an_unknown_status_is_refused(repo):
    invented = "wontfix"
    assert invented not in STATUSES
    with pytest.raises(backlog_tool.BacklogFormatError):
        file_one(repo, status=invented)


def test_ids_are_allocated_one_past_the_highest(repo):
    first = file_one(repo, title="First")
    second = file_one(repo, title="Second")
    assert first.name.startswith("0001-")
    assert second.name.startswith("0002-")
    assert backlog_tool.next_id(backlog_dir(repo)) == 3


def test_allocation_refuses_a_directory_it_cannot_list(repo, monkeypatch):
    """The failure where "I could not see the backlog" is spent as "the
    backlog is empty".

    Allocating id 1 against an unreadable directory would write over whatever
    already holds it, so this must raise rather than answer.
    """
    file_one(repo)

    def denied(self):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(backlog_tool.BacklogFormatError):
        backlog_tool.next_id(backlog_dir(repo))


def test_two_agents_racing_for_one_id_do_not_overwrite_each_other(repo,
                                                                 monkeypatch):
    """Id allocation is a read then a write, and nothing locks it.

    The loser of a race must be told, not silently merged over the winner's
    evidence -- which is unrecoverable, unlike a duplicate id that R3 reports
    at merge.
    """
    first = file_one(repo, title="First", slug="first")
    monkeypatch.setattr(backlog_tool, "next_id", lambda directory: 1)
    with pytest.raises(OSError):
        file_one(repo, title="Racing", slug="first")
    assert "First" in first.read_text(encoding="utf-8")


@pytest.mark.parametrize("title,expected", [
    ("Fix issue #42 in the parser", "fix-issue-42-in-the-parser"),
    ("  Leading and trailing  ", "leading-and-trailing"),
    ("Multiple   ---   separators", "multiple-separators"),
])
def test_slugs_are_derived_from_the_title(title, expected):
    assert backlog_tool.slug_for(title) == expected


@pytest.mark.parametrize("title", ["#42", "...", "\u4f60\u597d"])
def test_a_title_with_nothing_sluggable_still_yields_a_valid_filename(title):
    """An empty slug would produce ``0008-.md``, which the discovery pattern
    does not match -- so the file would sit in the directory being validated
    by nothing at all."""
    slug = backlog_tool.slug_for(title)
    assert slug
    assert ITEM_FILENAME.match(f"0008-{slug}.md")


def test_a_very_long_title_is_truncated_to_a_valid_slug():
    slug = backlog_tool.slug_for("word " * 60)
    assert len(slug) <= backlog_tool.SLUG_MAX
    assert ITEM_FILENAME.match(f"0008-{slug}.md"), slug


def test_the_title_keeps_what_the_slug_drops(repo):
    """The front matter carries no comment rule, so ``#42`` survives there
    even though the filename cannot hold it."""
    path = file_one(repo, title="Fix issue #42 in the parser")
    assert backlog_tool.parse_item(path).title == "Fix issue #42 in the parser"
    assert "42" in path.name


def test_a_blocking_item_is_filed_with_its_reference(repo):
    approved = file_one(repo, title="Approved work")
    backlog_tool.approve_item(backlog_dir(repo), 1)
    del approved
    path = file_one(repo, title="A defect found mid-task", blocks=1)
    assert backlog_tool.parse_item(path).front["blocks"] == "1"
    assert backlog_tool.check(repo) == []
    assert [i.filename_id for i in
            backlog_tool.workable(backlog_tool.load(backlog_dir(repo))[0])] \
        == [1, 2]


# ---------------------------------------------------------------------------
# Approving
# ---------------------------------------------------------------------------

def test_approval_changes_exactly_one_line(repo):
    """A one-line decision that renders as a whole-file diff is a decision
    nobody reviews.

    Compared as bytes. An earlier draft compared ``splitlines()`` of each
    side, which is the same lossy call the implementation was making -- so
    every separator the rewrite destroyed was destroyed in the expectation
    too, and the control agreed with the bug.
    """
    path = file_one(repo)
    before = path.read_bytes()
    backlog_tool.approve_item(backlog_dir(repo), 1)
    after = path.read_bytes()
    expected = before.replace(f"status: {PROPOSED_STATUS}".encode("utf-8"),
                              f"status: {OPEN_STATUS}".encode("utf-8"), 1)
    assert after == expected
    assert after != before


@pytest.mark.parametrize("separator", ["\f", "\v", "\x85", "\u2028", "\u2029"])
def test_approval_preserves_separators_python_calls_line_boundaries(repo,
                                                                    separator):
    """``str.splitlines`` splits on all of these; rejoining turns each into an
    ordinary newline.

    That is silent corruption of somebody's evidence, performed by the one
    function whose entire claim is that it changed a single line -- and it
    would never show up as an error, only as prose that quietly reflowed.
    """
    path = file_one(repo, evidence=f"Observed{separator}mid-sentence, 2026.")
    before = path.read_bytes()
    assert separator.encode("utf-8") in before
    backlog_tool.approve_item(backlog_dir(repo), 1)
    assert separator.encode("utf-8") in path.read_bytes()


def test_approval_preserves_trailing_blank_lines(repo):
    """``splitlines`` drops them, and rejoining cannot put them back."""
    path = file_one(repo)
    path.write_bytes(path.read_bytes() + b"\n\n")
    backlog_tool.approve_item(backlog_dir(repo), 1)
    assert path.read_bytes().endswith(b"\n\n\n")


def test_approval_preserves_a_missing_final_newline(repo):
    path = file_one(repo)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    backlog_tool.approve_item(backlog_dir(repo), 1)
    assert not path.read_bytes().endswith(b"\n")


def test_approval_handles_the_line_endings_the_parser_handles(repo):
    """A CR-only file, which ``parse_item`` reads without complaint.

    Splitting on fewer endings than the parser accepts makes an item that
    lists, shows and validates cleanly refuse to approve -- with an error
    blaming a missing ``status:`` line that is plainly in the file. The two
    have to agree about what a line is.
    """
    path = file_one(repo)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r"))
    assert backlog_tool.parse_item(path).status == PROPOSED_STATUS
    backlog_tool.approve_item(backlog_dir(repo), 1)
    raw = path.read_bytes()
    assert b"\n" not in raw, "a CR-only file came back with LF endings"
    assert f"status: {OPEN_STATUS}".encode("utf-8") in raw
    assert backlog_tool.parse_item(path).status == OPEN_STATUS


def test_approval_makes_the_item_workable(repo):
    file_one(repo)
    assert backlog_tool.workable(backlog_tool.load(backlog_dir(repo))[0]) == []
    backlog_tool.approve_item(backlog_dir(repo), 1)
    ready = backlog_tool.workable(backlog_tool.load(backlog_dir(repo))[0])
    assert [i.filename_id for i in ready] == [1]


def test_approval_leaves_crlf_files_as_it_found_them(repo):
    """Writing with the platform default would flip every line of a file
    checked out with CRLF, which is this repository's most-paid-for platform
    trap."""
    path = file_one(repo)
    text = path.read_text(encoding="utf-8")
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    backlog_tool.approve_item(backlog_dir(repo), 1)
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert raw.replace(b"\r\n", b"\n").count(b"\n") == raw.count(b"\r\n")


def test_approval_does_not_touch_a_status_line_in_the_body(repo):
    """Only the front matter is front matter.

    An item whose prose discusses ``status: open`` must come back with that
    prose intact; rewriting it would corrupt the evidence while reporting a
    successful approval.
    """
    path = file_one(repo, evidence="The file said\nstatus: rejected\nbefore.")
    backlog_tool.approve_item(backlog_dir(repo), 1)
    body = backlog_tool.parse_item(path).body
    assert "status: rejected" in body


def test_approving_an_already_approved_item_is_refused(repo):
    """A silent no-op here reads exactly like an approval having happened."""
    file_one(repo)
    backlog_tool.approve_item(backlog_dir(repo), 1)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.approve_item(backlog_dir(repo), 1)
    assert "already" in str(exc.value)


def test_approving_a_finished_item_is_refused(repo):
    path = file_one(repo)
    text = path.read_text(encoding="utf-8").replace(
        f"status: {PROPOSED_STATUS}", "status: rejected")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.approve_item(backlog_dir(repo), 1)
    assert "rejected" in str(exc.value)


def test_approving_an_item_that_does_not_exist_is_refused(repo):
    file_one(repo)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.approve_item(backlog_dir(repo), 99)
    assert "99" in str(exc.value)


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

def approved_one(root: Path, **kwargs) -> Path:
    """One item, filed and approved -- the ordinary subject of a close."""
    path = file_one(root, **kwargs)
    return backlog_tool.approve_item(backlog_dir(root),
                                     int(ITEM_FILENAME.match(path.name)
                                         .group(1)))


def test_a_close_records_the_commit_the_date_and_conforms(repo):
    """The negative control for every refusal below.

    A close must produce a document this project's own checker accepts. R7
    wants a closing date, R8 wants a commit that resolves here, and a tool
    that wrote one without the other would hand the caller a backlog its own
    suite fails -- with nothing pointing back at the command that did it.
    """
    path = approved_one(repo)
    head = git(repo, "rev-parse", "HEAD")
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    item = backlog_tool.parse_item(path)
    assert item.status == backlog_tool.CLOSED_STATUS
    assert item.front["commit"] == head
    assert item.front["closed"] == backlog_tool._today()
    assert backlog_tool.check(repo) == []


def test_a_close_writes_the_sha_not_the_revision_it_was_handed(repo):
    """``HEAD`` names a different commit the moment anybody commits again.

    Storing the word would leave a record that still passes every check and
    points somewhere else tomorrow, which is worse than a broken reference:
    a dangling SHA is reported by R8, and a drifting one never is.
    """
    approved_one(repo)
    first = git(repo, "rev-parse", "HEAD")
    path = backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                                   repo_root=repo)
    git(repo, "commit", "-q", "--allow-empty", "-m", "later work")
    assert git(repo, "rev-parse", "HEAD") != first
    assert backlog_tool.parse_item(path).front["commit"] == first


def test_a_close_touches_only_the_front_matter(repo):
    """The same promise approval makes, over three fields instead of one."""
    path = approved_one(repo, evidence="The file said\nstatus: open\nbefore.")
    before = path.read_bytes()
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    item = backlog_tool.parse_item(path)
    assert "status: open" in item.body
    assert item.body == backlog_tool.split_front_matter(
        before.decode("utf-8"))[1]
    assert item.front["title"] == backlog_tool.split_front_matter(
        before.decode("utf-8"))[0]["title"]


def test_the_closing_fields_land_beside_the_opening_one(repo):
    """Dates read as a pair. Appended at the end of the block instead, they
    land after ``spec`` and read as though somebody had added them by hand."""
    path = approved_one(repo)
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    keys = list(backlog_tool.parse_item(path).front)
    assert keys == ["id", "title", "status", "opened", "closed", "commit",
                    "spec"]


def test_a_closing_field_already_present_is_replaced_not_duplicated(repo):
    """The template in ``AGENTS.md`` shows ``closed:`` blank, and a blank
    value is legal at rest -- R7 only objects to one that is set. Inserting a
    second line would produce a duplicate key, which the parser refuses, so
    the item would close and then stop loading at all."""
    path = approved_one(repo)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\nspec:", "\nclosed:\nspec:"),
                    encoding="utf-8")
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    item = backlog_tool.parse_item(path)
    assert item.front["closed"] == backlog_tool._today()
    assert backlog_tool.check(repo) == []


@pytest.mark.parametrize("ending", ["\r\n", "\r"])
def test_a_close_leaves_the_files_line_endings_as_it_found_them(repo, ending):
    """Both the replaced lines and the inserted ones.

    An inserted line is the new risk here: approval only ever rewrote a line
    that already had an ending to copy, so a hard-coded ``\\n`` would have
    shown up nowhere until the first close of a CRLF file.
    """
    path = approved_one(repo)
    path.write_bytes(path.read_bytes().replace(b"\n", ending.encode("utf-8")))
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    raw = path.read_bytes()
    if ending == "\r":
        assert b"\n" not in raw
    else:
        assert raw.replace(b"\r\n", b"").count(b"\n") == 0
    assert backlog_tool.parse_item(path).status == backlog_tool.CLOSED_STATUS


def test_a_close_preserves_a_missing_final_newline(repo):
    path = approved_one(repo)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    assert not path.read_bytes().endswith(b"\n")


# -- the gate ---------------------------------------------------------------

def test_an_unapproved_item_cannot_be_closed(repo):
    """The gate, enforced where it can actually be bypassed.

    ``ready`` is a queue an agent can simply not consult. If ``close`` asked
    nothing, an agent could file its own item and mark it shipped, and the
    approval gate would hold only on the path nobody is obliged to take.
    """
    file_one(repo)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                                repo_root=repo)
    assert "awaiting approval" in str(exc.value)
    assert backlog_tool.parse_item(
        backlog_tool.item_paths(backlog_dir(repo))[0]).status \
        == PROPOSED_STATUS


def test_an_item_working_under_the_blocks_exception_can_be_closed(repo):
    """The positive control for the refusal above, and the reason it is
    written against ``why_not_workable`` rather than against the status.

    An agent that finds a defect while working an approved item may file it
    and carry on -- that is the recorded exception the gate depends on for its
    own enforceability. An item lawfully worked that cannot then be lawfully
    closed sends the same agent to edit the status field by hand.
    """
    approved_one(repo, title="Approved work")
    file_one(repo, title="A defect found mid-task", blocks=1)
    path = backlog_tool.close_item(backlog_dir(repo), 2, commit="HEAD",
                                   repo_root=repo)
    assert backlog_tool.parse_item(path).status == backlog_tool.CLOSED_STATUS
    assert backlog_tool.check(repo) == []


def test_an_unapproved_item_can_still_be_rejected(repo):
    """Rejection is not gated on approval, and the asymmetry is the point: the
    ordinary thing to decline is an unapproved proposal, so requiring approval
    first would mean approving something in order to turn it down."""
    file_one(repo)
    path = backlog_tool.close_item(backlog_dir(repo), 1, reject=True,
                                   repo_root=repo)
    item = backlog_tool.parse_item(path)
    assert item.status == backlog_tool.REJECTED_STATUS
    assert item.front["closed"] == backlog_tool._today()
    assert "commit" not in item.front
    assert backlog_tool.check(repo) == []


def test_a_rejection_refuses_a_commit(repo):
    """Nothing shipped. A SHA recorded against a rejection reads as though
    something had, and that is exactly the kind of wrong that looks like
    evidence."""
    file_one(repo)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 1, reject=True,
                                commit="HEAD", repo_root=repo)
    assert "nothing shipped" in str(exc.value)


def test_a_close_with_no_commit_is_refused(repo):
    approved_one(repo)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 1, repo_root=repo)
    assert "--commit" in str(exc.value)


def test_a_close_naming_a_commit_that_does_not_resolve_is_refused(repo):
    approved_one(repo)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 1, commit="0" * 40,
                                repo_root=repo)
    assert "does not resolve" in str(exc.value)


def test_a_close_naming_an_object_that_is_not_a_commit_is_refused(repo):
    """``git rev-parse --verify`` alone resolves a blob or a tree.

    Without ``^{commit}`` any object in the repository would certify a close,
    and the item would name something that has no history, no author and no
    date -- while looking exactly like a SHA that does.
    """
    approved_one(repo)
    (repo / "note.txt").write_text("a blob", encoding="utf-8")
    blob = git(repo, "hash-object", "-w", "note.txt")
    assert len(blob) == 40
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 1, commit=blob,
                                repo_root=repo)
    assert "does not resolve" in str(exc.value)


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_closing_an_item_whose_life_already_ended_is_refused(repo, status):
    """A silent second close overwrites the date and the SHA of the first,
    which is the one pair of fields nothing else records."""
    path = approved_one(repo)
    path.write_text(path.read_text(encoding="utf-8").replace(
        f"status: {OPEN_STATUS}", f"status: {status}"), encoding="utf-8")
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                                repo_root=repo)
    assert "already" in str(exc.value)


def test_closing_an_item_that_does_not_exist_is_refused(repo):
    approved_one(repo)
    with pytest.raises(backlog_tool.BacklogFormatError) as exc:
        backlog_tool.close_item(backlog_dir(repo), 99, commit="HEAD",
                                repo_root=repo)
    assert "99" in str(exc.value)


def test_a_closed_item_leaves_the_queue(repo):
    approved_one(repo)
    assert [i.filename_id for i in backlog_tool.workable(
        backlog_tool.load(backlog_dir(repo))[0])] == [1]
    backlog_tool.close_item(backlog_dir(repo), 1, commit="HEAD",
                            repo_root=repo)
    assert backlog_tool.workable(backlog_tool.load(backlog_dir(repo))[0]) == []


# ---------------------------------------------------------------------------
# The watermark
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A relocated home, so no test can reach the real project catalog."""
    fake = tmp_path / "home"
    (fake / ".operator" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


def catalogue(home: Path, root: Path, guid: str = "test-guid") -> Path:
    catalog = home / ".operator" / "projects" / "catalog.csv"
    catalog.write_text(f'"{root.resolve()}",{guid}\n', encoding="utf-8")
    (home / ".operator" / "projects" / guid).mkdir(parents=True, exist_ok=True)
    return catalog


def test_the_watermark_lives_in_the_per_project_directory(repo, home):
    catalogue(home, repo)
    path = backlog_tool.watermark_path(repo)
    assert path.parent == home / ".operator" / "projects" / "test-guid"
    # The literal, not the module's own constant. Asserting
    # ``path.name == backlog_tool.WATERMARK_NAME`` compares the code's answer
    # against the code's input, and holds for any name at all -- including a
    # rename that would orphan every watermark already on disk.
    assert path.name == "backlog-scrum.json"


def test_every_worktree_of_a_project_shares_one_watermark(repo, home, tmp_path):
    """A worktree is a second directory for the same project.

    Keying the watermark on the checkout instead would give each worktree its
    own, and each would then report the other's work as new -- the duplicate
    identity ``primary_repo_root`` exists to prevent, in a new costume.
    """
    catalogue(home, repo)
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    assert backlog_tool.watermark_path(linked) == \
        backlog_tool.watermark_path(repo)


def test_an_uncatalogued_project_is_told_what_to_add(repo, home):
    """Refusing is right; refusing without the fix is not."""
    (home / ".operator" / "projects" / "catalog.csv").write_text(
        '"/somewhere/else",other\n', encoding="utf-8")
    with pytest.raises(WatermarkError) as exc:
        backlog_tool.watermark_path(repo)
    assert str(repo.resolve()) in str(exc.value)


def test_a_missing_catalog_is_distinguished_from_a_missing_entry(repo, home):
    with pytest.raises(WatermarkError) as exc:
        backlog_tool.watermark_path(repo)
    assert "No project catalog" in str(exc.value)


def test_a_watermark_that_was_never_written_reads_as_never(tmp_path):
    assert backlog_tool.read_watermark(tmp_path / "absent.json") is None


def test_a_watermark_that_cannot_be_parsed_refuses_rather_than_resetting(
        tmp_path):
    """The bug class this repository has paid for four times.

    An unreadable watermark read as "never checked in" reports the entire
    history as new, which looks exactly like a first run -- so the boundary is
    discarded silently, on a report whose only value is that it says what
    changed.
    """
    path = tmp_path / "backlog-scrum.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WatermarkError):
        backlog_tool.read_watermark(path)


def test_a_watermark_holding_the_wrong_shape_refuses(tmp_path):
    path = tmp_path / "backlog-scrum.json"
    path.write_text('["a list"]', encoding="utf-8")
    with pytest.raises(WatermarkError):
        backlog_tool.read_watermark(path)


@pytest.mark.parametrize("field", ["commit", "checked_at"])
def test_a_watermark_field_of_the_wrong_type_refuses(tmp_path, field):
    """A JSON object is not a schema.

    ``{"commit": 123}`` parses, passes an ``isinstance(dict)`` check, and dies
    later at ``since[:12]`` with a TypeError -- a traceback in place of the
    actionable refusal every other watermark failure gets.
    """
    path = tmp_path / "backlog-scrum.json"
    payload = {"commit": "a" * 40, "checked_at": "2026-08-05T00:00:00Z"}
    payload[field] = 123
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(WatermarkError) as exc:
        backlog_tool.read_watermark(path)
    assert field in str(exc.value)


def test_a_watermark_that_cannot_be_read_refuses(tmp_path):
    """A directory where the file should be. ``read_text`` raises rather than
    answering, and answering ``None`` would spend the failure as a first run."""
    path = tmp_path / "backlog-scrum.json"
    path.mkdir()
    with pytest.raises(WatermarkError):
        backlog_tool.read_watermark(path)


def test_a_written_watermark_reads_back(tmp_path):
    """Negative control for the four refusals above."""
    path = tmp_path / "backlog-scrum.json"
    written = backlog_tool.write_watermark(path, "a" * 40)
    read = backlog_tool.read_watermark(path)
    assert read == written
    assert read["commit"] == "a" * 40
    assert read["checked_at"].endswith("Z")


def test_a_watermark_that_cannot_be_written_says_so(tmp_path):
    """Silence here repeats the whole period at the next check-in, and a
    repeated report looks exactly like a week in which nothing happened."""
    with pytest.raises(WatermarkError):
        backlog_tool.write_watermark(tmp_path, "a" * 40)


# ---------------------------------------------------------------------------
# The check-in report
# ---------------------------------------------------------------------------

def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def test_an_unreadable_head_is_unknown_not_none(repo, monkeypatch):
    """The branch one step out from the one the flags were added for.

    ``rev-parse`` failing means nothing was listed, so nothing is known -- but
    the listing block is skipped rather than failing, so the flags stay true
    unless something says otherwise, and the report renders "Commits: none."
    over a broken git. Two reviewers read this branch and called it sound.
    """
    file_one(repo)
    head = commit_all(repo, "file an item")
    real = backlog_tool._git

    def no_head(args, cwd):
        if args and args[0] == "rev-parse":
            return 128, ""
        return real(args, cwd)

    monkeypatch.setattr(backlog_tool, "_git", no_head)
    report = backlog_tool.scrum_report(
        repo, {"commit": head, "checked_at": "2026-08-05T00:00:00Z"})
    assert report.head == ""
    assert not report.commits_known and not report.changes_known
    text = backlog_tool.format_scrum(report)
    assert "Commits: none." not in text
    assert "could not be listed" in text


def test_a_first_check_in_claims_neither_none_nor_unknown(repo):
    """A first run has no boundary, so both readings would be false; the
    opening prose already says the history is left out."""
    file_one(repo)
    commit_all(repo, "file an item")
    text = backlog_tool.format_scrum(backlog_tool.scrum_report(repo, None))
    assert "Commits: none." not in text
    assert "could not be listed" not in text
    assert "First check-in" in text


def test_a_first_check_in_says_so_and_reports_current_state(repo):
    file_one(repo)
    commit_all(repo, "file an item")
    report = backlog_tool.scrum_report(repo, None)
    assert report.first_run
    assert dict(report.counts)[PROPOSED_STATUS] == 1
    assert report.awaiting and not report.ready


def test_a_later_check_in_reports_only_what_changed(repo):
    file_one(repo, title="Filed before the check-in")
    first = commit_all(repo, "before")
    file_one(repo, title="Filed after the check-in")
    commit_all(repo, "after")

    report = backlog_tool.scrum_report(repo, {"commit": first,
                                              "checked_at": "2026-08-05T00:00:00Z"})
    assert not report.first_run
    assert report.since_resolves
    assert [line.split(" ", 1)[1] for line in report.commits] == ["after"]
    assert [name for _, name in report.item_changes] == \
        ["0002-filed-after-the-check-in.md"]


def test_a_check_in_with_nothing_new_reports_nothing_new(repo):
    """Negative control: the report must be capable of being empty, or
    "nothing happened" and "the diff is broken" are the same output."""
    file_one(repo)
    head = commit_all(repo, "file an item")
    report = backlog_tool.scrum_report(repo, {"commit": head,
                                              "checked_at": "2026-08-05T00:00:00Z"})
    assert report.commits == ()
    assert report.item_changes == ()
    assert dict(report.counts)[PROPOSED_STATUS] == 1


def test_a_watermark_commit_that_no_longer_exists_is_reported_not_ignored(repo):
    """A rewritten history or a fresh clone.

    Reporting nothing would be the dangerous answer: it is what a quiet week
    looks like. This repository has rewritten its own history once already, so
    the case is live rather than hypothetical.

    The caveat is checked *and so is the content*. An earlier draft asserted
    only that the note appeared, and the note says the report covers
    everything -- so the one thing the assertion did not look at was the one
    thing the note promised, and the section was in fact being skipped.
    """
    file_one(repo, title="Filed before the boundary vanished")
    commit_all(repo, "first")
    file_one(repo, title="Filed after")
    commit_all(repo, "second")
    report = backlog_tool.scrum_report(repo, {"commit": "0" * 40,
                                              "checked_at": "2026-08-05T00:00:00Z"})
    assert not report.since_resolves
    assert any("does not resolve" in note for note in report.notes)
    subjects = [line.split(" ", 1)[1] for line in report.commits]
    assert subjects == ["second", "first", "seed"], subjects
    assert sorted(name for _, name in report.item_changes) == [
        "0001-filed-before-the-boundary-vanished.md", "0002-filed-after.md"]
    text = backlog_tool.format_scrum(report)
    assert "does not resolve" in text
    assert "second" in text and "first" in text


def test_a_check_in_with_no_commits_says_none_rather_than_omitting_the_section(
        repo):
    """"Commits: none." and a missing commits section read differently: one is
    an answer, the other is a rendering slip the reader has to guess about."""
    file_one(repo)
    head = commit_all(repo, "file an item")
    report = backlog_tool.scrum_report(repo, {"commit": head,
                                              "checked_at": "2026-08-05T00:00:00Z"})
    assert "Commits: none." in backlog_tool.format_scrum(report)


def test_a_checkout_git_cannot_answer_for_is_a_caveat_not_a_quiet_week(tmp_path):
    """No repository at all. The report must say it could not date anything
    rather than render an empty commit list."""
    (tmp_path / BACKLOG_DIRNAME).mkdir()
    report = backlog_tool.scrum_report(tmp_path, None)
    assert report.head == ""
    assert any("no commits yet" in note for note in report.notes)
    assert "Caveats" in backlog_tool.format_scrum(report)


def test_a_git_that_will_not_run_reports_failure_not_empty_output(tmp_path):
    """The contract the whole report rests on.

    Empty output is what "nothing changed" looks like, so a git that cannot be
    launched must come back as a failure. Collapsing the two makes the report
    say "no news" on the day it stopped working -- and no caller could tell.
    """
    rc, out = backlog_tool._git(["rev-parse", "HEAD"], tmp_path / "absent")
    assert rc != 0, "a git that could not run reported success"
    assert out == ""


def test_a_git_command_that_fails_mid_report_becomes_a_caveat(repo, monkeypatch):
    """The half-assembled report.

    ``rev-parse`` works, so the report looks healthy, and then ``log`` fails.
    Rendering that as "no commits" is the same lie as a quiet week, so each
    failing step has to leave a note behind *and* the rendered text must not
    claim a number it does not have.
    """
    file_one(repo)
    head = commit_all(repo, "file an item")
    real = backlog_tool._git

    def flaky(args, cwd):
        if args and args[0] in ("log", "diff"):
            return 1, ""
        return real(args, cwd)

    monkeypatch.setattr(backlog_tool, "_git", flaky)
    report = backlog_tool.scrum_report(
        repo, {"commit": head, "checked_at": "2026-08-05T00:00:00Z"})
    assert report.commits == ()
    assert not report.commits_known and not report.changes_known
    assert any("could not list commits" in note for note in report.notes)
    assert any("could not list backlog changes" in note
               for note in report.notes)
    text = backlog_tool.format_scrum(report)
    assert "Caveats" in text
    assert "Commits: none." not in text, (
        "a failed listing rendered as an empty one; 'unknown' and 'none' read "
        "identically and only one of them is true")
    assert "could not be listed" in text


def test_the_report_separates_what_is_ready_from_what_is_waiting(repo):
    file_one(repo, title="Approved work")
    backlog_tool.approve_item(backlog_dir(repo), 1)
    file_one(repo, title="Waiting on the owner")
    commit_all(repo, "two items")
    report = backlog_tool.scrum_report(repo, None)
    assert [i for i, _ in report.ready] == [1]
    assert [i for i, _ in report.awaiting] == [2]
    text = backlog_tool.format_scrum(report)
    assert "Ready to work (1)" in text
    assert "Awaiting your approval (1)" in text


def test_the_report_carries_conformance_problems(repo):
    """A check-in that renders a broken backlog as a healthy one is worse than
    no check-in."""
    path = file_one(repo)
    path.write_text(path.read_text(encoding="utf-8").replace("id: 1", "id: 9"),
                    encoding="utf-8")
    report = backlog_tool.scrum_report(repo, None)
    assert report.problems
    assert "Conformance problems" in backlog_tool.format_scrum(report)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def run(root: Path, *args) -> int:
    return backlog_tool.main(["-C", str(root), *args])


def test_the_new_command_files_an_item(repo, capsys):
    rc = run(repo, "new", "--title", "From the command line",
             "--evidence", "Observed 2026-08-05.")
    out = capsys.readouterr().out
    assert rc == 0
    assert "0001-from-the-command-line.md" in out
    assert backlog_tool.check(repo) == []


def test_the_new_command_offers_no_way_to_file_an_approved_item(repo, capsys):
    """The bypass a reviewer found: ``backlog new --status open``.

    An agent needs no cunning to find a documented option, so the command must
    not carry one. It is still possible to write the file by hand -- the gate
    is a speed bump with an audit trail, not a sandbox -- but there is a
    difference between a boundary somebody stepped over and one the tool
    published in its own ``--help``.
    """
    with pytest.raises(SystemExit) as exc:
        run(repo, "new", "--title", "Self-approved", "--evidence", "Observed.",
            "--status", OPEN_STATUS)
    assert exc.value.code != 0
    assert "--status" in capsys.readouterr().err
    assert backlog_tool.item_paths(backlog_dir(repo)) == []


def test_every_item_the_new_command_files_awaits_approval(repo):
    """The positive control for the refusal above: whatever the caller passes,
    what lands is ``proposed``."""
    assert run(repo, "new", "--title", "Filed", "--evidence", "Observed.") == 0
    item = backlog_tool.parse_item(backlog_tool.item_paths(backlog_dir(repo))[0])
    assert item.status == PROPOSED_STATUS


def test_the_new_command_refuses_an_item_with_no_evidence(repo, capsys):
    rc = run(repo, "new", "--title", "No evidence at all")
    assert rc == 1
    assert "rumour" in capsys.readouterr().err
    assert backlog_tool.item_paths(backlog_dir(repo)) == []


def test_the_new_command_reads_evidence_from_a_file(repo, tmp_path):
    source = tmp_path / "evidence.txt"
    source.write_text("Observed 2026-08-05 in the log.", encoding="utf-8")
    assert run(repo, "new", "--title", "From a file",
               "--evidence-file", str(source)) == 0
    item = backlog_tool.parse_item(backlog_tool.item_paths(backlog_dir(repo))[0])
    assert "Observed 2026-08-05 in the log." in item.section("## Evidence")


def test_the_new_command_reports_a_missing_evidence_file(repo, tmp_path, capsys):
    rc = run(repo, "new", "--title", "From a file",
             "--evidence-file", str(tmp_path / "absent.txt"))
    assert rc == 1
    assert "cannot read" in capsys.readouterr().err


def test_the_ready_command_prints_the_queue_and_its_reasons(repo, capsys):
    file_one(repo, title="Approved work")
    backlog_tool.approve_item(backlog_dir(repo), 1)
    file_one(repo, title="Waiting on the owner")
    assert run(repo, "ready", "--explain") == 0
    captured = capsys.readouterr()
    assert "Approved work" in captured.out
    assert "Waiting on the owner" not in captured.out
    assert "awaiting approval" in captured.err


def test_the_approve_command_reports_the_transition(repo, capsys):
    file_one(repo)
    assert run(repo, "approve", "1") == 0
    assert f"{PROPOSED_STATUS} -> {OPEN_STATUS}" in capsys.readouterr().out


def test_the_approve_command_reports_a_refusal(repo, capsys):
    file_one(repo)
    run(repo, "approve", "1")
    assert run(repo, "approve", "1") == 1
    assert "already" in capsys.readouterr().err


def test_the_close_command_reports_the_transition(repo, capsys):
    file_one(repo)
    run(repo, "approve", "1")
    assert run(repo, "close", "1") == 0
    out = capsys.readouterr().out
    assert backlog_tool.CLOSED_STATUS in out
    assert git(repo, "rev-parse", "HEAD")[:12] in out


def test_the_close_command_defaults_to_head(repo):
    """The natural thing to pass straight after committing the work, and the
    default exists so that not passing it is not a reason to hand-edit."""
    file_one(repo)
    run(repo, "approve", "1")
    assert run(repo, "close", "1") == 0
    item = backlog_tool.parse_item(backlog_tool.item_paths(backlog_dir(repo))[0])
    assert item.front["commit"] == git(repo, "rev-parse", "HEAD")


def test_the_close_command_refuses_an_unapproved_item(repo, capsys):
    """The gate, from the command line. The item must be left where it was:
    a refusal that half-wrote the file is a worse outcome than the close."""
    file_one(repo)
    assert run(repo, "close", "1") == 1
    assert "awaiting approval" in capsys.readouterr().err
    item = backlog_tool.parse_item(backlog_tool.item_paths(backlog_dir(repo))[0])
    assert item.status == PROPOSED_STATUS
    assert backlog_tool.check(repo) == []


def test_the_close_command_rejects_without_a_commit(repo, capsys):
    file_one(repo)
    assert run(repo, "close", "1", "--reject") == 0
    assert backlog_tool.REJECTED_STATUS in capsys.readouterr().out
    item = backlog_tool.parse_item(backlog_tool.item_paths(backlog_dir(repo))[0])
    assert "commit" not in item.front


def test_the_close_command_refuses_a_commit_against_a_rejection(repo, capsys):
    """``--commit`` defaults to HEAD, so the default and an explicit value
    have to stay distinguishable. Defaulting in the parser instead makes them
    the same string, and this refusal silently never fires."""
    file_one(repo)
    assert run(repo, "close", "1", "--reject", "--commit", "HEAD") == 1
    assert "nothing shipped" in capsys.readouterr().err
    item = backlog_tool.parse_item(backlog_tool.item_paths(backlog_dir(repo))[0])
    assert item.status == PROPOSED_STATUS


def test_the_close_command_offers_no_way_past_the_gate(repo):
    """The shape of the bypass ``new --status open`` was: a documented option
    that repeals the gate. ``close`` must not grow one either -- an agent
    needs no cunning to find a flag that is in ``--help``."""
    parser_flags = _close_flags()
    assert parser_flags == {"-h", "--help", "--commit", "--reject"}, (
        f"`backlog close` grew a flag: {sorted(parser_flags)}. If it can "
        "close an item the approval gate would have refused, it is the "
        "bypass in the tool's own --help.")


def _close_flags() -> set:
    """Every option string ``backlog close`` accepts."""
    parser = backlog_tool.build_parser()
    sub = [a for a in parser._actions
           if isinstance(a, argparse._SubParsersAction)]
    assert sub, "the parser no longer has subcommands, so this asserts nothing"
    close = sub[0].choices["close"]
    return {opt for action in close._actions for opt in action.option_strings}


def test_the_operator_subcommand_is_this_tool(repo, capsys):
    """``operator backlog`` delegates rather than reimplementing.

    A second argument parser over there would be a second copy of the
    vocabulary, the gate and every rule ``check`` enforces -- and the copy is
    what drifts. The delegation is asserted by running a verb through it and
    seeing this tool's own output.
    """
    import copilot_operator

    file_one(repo, title="Approved work")
    backlog_tool.approve_item(backlog_dir(repo), 1)
    rc = copilot_operator.manage_backlog(["-C", str(repo), "ready"])
    assert rc == 0
    assert "Approved work" in capsys.readouterr().out


def test_the_operator_subcommand_does_not_take_the_process_down(repo, capsys):
    """``argparse`` exits rather than returning, and ``operator`` is a long
    program with state of its own. A ``--help`` that raised out of a
    subcommand would leave through a path that has nothing to do with it."""
    import copilot_operator

    assert copilot_operator.manage_backlog(["--help"]) == 0
    assert "operator backlog" in capsys.readouterr().out
    assert copilot_operator.manage_backlog(["-C", str(repo), "wat"]) != 0


def test_the_operator_subcommand_is_dispatched_and_reserved():
    """A word dispatched but missing from ``SUBCOMMANDS`` is a word the
    positional shortcut will try to join as an instance name."""
    import copilot_operator

    assert "backlog" in copilot_operator.SUBCOMMANDS
    assert "backlog" in copilot_operator.RESERVED_WORDS


def test_the_scrum_command_advances_the_watermark(repo, home, capsys):
    catalogue(home, repo)
    file_one(repo)
    head = commit_all(repo, "file an item")
    assert run(repo, "scrum") == 0
    assert "First check-in" in capsys.readouterr().out
    recorded = json.loads(
        backlog_tool.watermark_path(repo).read_text(encoding="utf-8"))
    assert recorded["commit"] == head


def test_peeking_leaves_the_watermark_alone(repo, home, capsys):
    """Reading a period and marking it read are separable, deliberately.

    An agent drafting a handoff reports a period the human has not seen; if
    that consumed it, the next check-in would skip it and a week of work would
    go unmentioned.
    """
    catalogue(home, repo)
    file_one(repo)
    commit_all(repo, "file an item")
    assert run(repo, "scrum", "--peek") == 0
    assert "--peek" in capsys.readouterr().out
    assert backlog_tool.read_watermark(backlog_tool.watermark_path(repo)) is None


def test_the_scrum_command_refuses_without_a_catalogued_project(repo, home,
                                                                capsys):
    assert run(repo, "scrum") == 1
    assert "catalog" in capsys.readouterr().err.lower()


def test_a_second_check_in_measures_from_the_first(repo, home, capsys):
    catalogue(home, repo)
    file_one(repo, title="Before")
    commit_all(repo, "before")
    run(repo, "scrum")
    capsys.readouterr()
    file_one(repo, title="After")
    commit_all(repo, "after")
    assert run(repo, "scrum") == 0
    out = capsys.readouterr().out
    assert "Since the check-in of" in out
    assert "after" in out
    assert "before" not in out


# ---------------------------------------------------------------------------
# The rendered view
# ---------------------------------------------------------------------------

def test_every_status_has_a_style_in_the_rendered_page(repo):
    """A status with no rule renders as unstyled text, which reads as a
    rendering glitch rather than as a status the page has never heard of."""
    file_one(repo)
    page = backlog_tool.render_html(repo, resolve_commits=False)
    for status in STATUSES:
        assert f".{status} " in page or f".{status}{{" in page, status


def test_the_page_says_why_an_item_is_not_workable(repo):
    file_one(repo)
    page = backlog_tool.render_html(repo, resolve_commits=False)
    assert "blocked_because" in page
    assert "awaiting approval" in page
