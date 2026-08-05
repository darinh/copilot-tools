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
    nobody reviews."""
    path = file_one(repo)
    before = path.read_text(encoding="utf-8").splitlines()
    backlog_tool.approve_item(backlog_dir(repo), 1)
    after = path.read_text(encoding="utf-8").splitlines()
    differing = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(before) == len(after)
    assert differing == [(f"status: {PROPOSED_STATUS}",
                          f"status: {OPEN_STATUS}")], differing


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
# The watermark
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A relocated home, so no test can reach the real project catalog."""
    fake = tmp_path / "home"
    (fake / ".copilot" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


def catalogue(home: Path, root: Path, guid: str = "test-guid") -> Path:
    catalog = home / ".copilot" / "projects" / "catalog.csv"
    catalog.write_text(f'"{root.resolve()}",{guid}\n', encoding="utf-8")
    (home / ".copilot" / "projects" / guid).mkdir(parents=True, exist_ok=True)
    return catalog


def test_the_watermark_lives_in_the_per_project_directory(repo, home):
    catalogue(home, repo)
    path = backlog_tool.watermark_path(repo)
    assert path.parent == home / ".copilot" / "projects" / "test-guid"
    assert path.name == backlog_tool.WATERMARK_NAME


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
    (home / ".copilot" / "projects" / "catalog.csv").write_text(
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
    """
    file_one(repo)
    commit_all(repo, "file an item")
    report = backlog_tool.scrum_report(repo, {"commit": "0" * 40,
                                              "checked_at": "2026-08-05T00:00:00Z"})
    assert not report.since_resolves
    assert any("does not resolve" in note for note in report.notes)
    assert "does not resolve" in backlog_tool.format_scrum(report)


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
    failing step has to leave a note behind.
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
    assert any("could not list commits" in note for note in report.notes)
    assert any("could not list backlog changes" in note
               for note in report.notes)
    assert "Caveats" in backlog_tool.format_scrum(report)


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
