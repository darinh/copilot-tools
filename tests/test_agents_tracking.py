"""What `operator projects retire` tells git about the AGENTS.md it writes.

The defect these cover: the command wrote a repository's own conventions file
and said nothing to git, leaving it untracked. Untracked is the one state that
`git clean -fd` deletes, that never reaches a clone, and that a checkout guard
cannot tell apart from scratch. Across eight real projects the outcome was
three tracked and five not -- and the three only because somebody happened to
commit them later.

These tests drive real git repositories rather than a mock, because every
property being claimed here is a property of git's own behaviour: that staging
survives `git clean`, that a pathspec commit ignores an unrelated index, and
that a half-finished merge is visible through `rev-parse --git-path`. A fake
git would be asserting that the author understood git, which is the thing in
doubt.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import copilot_operator as op
import project_instructions as pi


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repository with one commit, on a branch, ready to receive work."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-m", "seed")
    return root


def _write_agents(root: Path, text: str = "# conventions\n") -> Path:
    target = root / pi.AGENTS_NAME
    target.write_text(text, encoding="utf-8")
    return target


def _outcome(root: Path, label: str = "proj") -> pi.ProjectOutcome:
    return pi.ProjectOutcome(guid="guid", path=str(root), label=label,
                             state=pi.WRITTEN,
                             agents_path=root / pi.AGENTS_NAME)


def _is_tracked(root: Path, name: str = pi.AGENTS_NAME) -> bool:
    proc = subprocess.run(["git", "ls-files", "--error-unmatch", "--", name],
                          cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode == 0


# ── staging ──────────────────────────────────────────────────────
def test_an_untracked_agents_file_is_staged(repo):
    _write_agents(repo)
    assert not _is_tracked(repo)

    tracking = op.stage_agents_files([_outcome(repo)])

    assert [t.state for t in tracking] == [op.AGENTS_STAGED]
    assert _is_tracked(repo), "staging must put the file in the index"


def test_a_staged_agents_file_survives_git_clean(repo):
    """The property the whole change exists for.

    `git clean -fd` is routine checkout hygiene and agents here are instructed
    to run it. Untracked, the repository's conventions file is deleted by it;
    staged, it is not. This asserts the difference rather than describing it.
    """
    _write_agents(repo)
    op.stage_agents_files([_outcome(repo)])

    _git(repo, "clean", "-fd")

    assert (repo / pi.AGENTS_NAME).exists()


def test_an_untracked_agents_file_is_destroyed_by_git_clean(repo):
    """The control for the test above -- it must not pass vacuously.

    If `git clean -fd` never removed anything in this fixture, the previous
    test would be green with the staging removed entirely.
    """
    _write_agents(repo)

    _git(repo, "clean", "-fd")

    assert not (repo / pi.AGENTS_NAME).exists()


def test_a_file_already_tracked_and_unchanged_is_left_alone(repo):
    _write_agents(repo)
    _git(repo, "add", pi.AGENTS_NAME)
    _git(repo, "commit", "-m", "conventions")

    tracking = op.stage_agents_files([_outcome(repo)])

    assert [t.state for t in tracking] == [op.AGENTS_TRACKED]


def test_a_tracked_file_whose_contents_changed_is_staged_again(repo):
    _write_agents(repo, "# old\n")
    _git(repo, "add", pi.AGENTS_NAME)
    _git(repo, "commit", "-m", "conventions")
    _write_agents(repo, "# new\n")

    tracking = op.stage_agents_files([_outcome(repo)])

    assert [t.state for t in tracking] == [op.AGENTS_STAGED]
    assert "# new" in _git(repo, "show", ":" + pi.AGENTS_NAME)


def test_a_directory_that_is_not_a_repository_is_reported_not_guessed(tmp_path):
    root = tmp_path / "loose"
    root.mkdir()
    _write_agents(root)

    tracking = op.stage_agents_files([_outcome(root)])

    assert [t.state for t in tracking] == [op.AGENTS_NO_REPO]


def test_an_ignored_agents_file_reports_the_failure_rather_than_success(repo):
    """A .gitignore that swallows AGENTS.md must not read as 'already tracked'.

    `git status --porcelain` says nothing about an ignored file, exactly as it
    says nothing about a tracked and unmodified one. Telling the user the file
    is fine when git has refused it is the one wrong answer available here.
    """
    (repo / ".gitignore").write_text(pi.AGENTS_NAME + "\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore conventions")
    _write_agents(repo)

    tracking = op.stage_agents_files([_outcome(repo)])

    assert [t.state for t in tracking] == [op.AGENTS_FAILED]
    assert tracking[0].detail, "a failure must say why"


def test_an_outcome_with_no_agents_path_is_skipped(repo):
    outcome = pi.ProjectOutcome(guid="g", path=str(repo), label="proj",
                                state=pi.WRITTEN, agents_path=None)

    assert op.stage_agents_files([outcome]) == []


# ── committing ───────────────────────────────────────────────────
def test_accepting_the_prompt_commits_the_file(repo):
    _write_agents(repo)
    tracking = op.stage_agents_files([_outcome(repo)])

    settled = op.commit_agents_files(tracking, "a template")

    assert [t.state for t in settled] == [op.AGENTS_COMMITTED]
    assert pi.AGENTS_NAME in _git(repo, "show", "--name-only", "--format=", "HEAD")


def test_a_commit_does_not_sweep_up_unrelated_staged_work(repo):
    """The reason this uses `git commit -- <path>` and not a bare commit.

    A repository may already have somebody's work in its index. Committing
    that alongside the conventions file would put changes into a commit the
    user did not write and did not see, in eight repositories at once.
    """
    (repo / "someones_work.txt").write_text("in progress\n", encoding="utf-8")
    _git(repo, "add", "someones_work.txt")
    _write_agents(repo)
    tracking = op.stage_agents_files([_outcome(repo)])

    op.commit_agents_files(tracking, "a template")

    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == [pi.AGENTS_NAME]
    assert "someones_work.txt" in _git(repo, "diff", "--cached", "--name-only")


def test_a_detached_head_is_staged_but_never_committed(repo):
    _write_agents(repo)
    _git(repo, "checkout", "--detach")
    tracking = op.stage_agents_files([_outcome(repo)])
    head_before = _git(repo, "rev-parse", "HEAD").strip()

    settled = op.commit_agents_files(tracking, "a template")

    assert [t.state for t in settled] == [op.AGENTS_STAGED]
    assert "detached" in settled[0].detail
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before


def test_an_interrupted_merge_is_staged_but_never_committed(repo):
    _write_agents(repo)
    tracking = op.stage_agents_files([_outcome(repo)])
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    (repo / ".git" / "MERGE_HEAD").write_text(head_before + "\n",
                                              encoding="utf-8")

    settled = op.commit_agents_files(tracking, "a template")

    assert [t.state for t in settled] == [op.AGENTS_STAGED]
    assert "merge" in settled[0].detail
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before


def test_a_clean_repository_is_not_refused(repo):
    """The control for the two refusals above.

    Both assert a commit did not happen. If `_commit_would_be_unsafe` returned
    a reason unconditionally they would both still pass, and the feature would
    never commit anything.
    """
    assert op._commit_would_be_unsafe(repo) is None


def test_entries_that_were_never_staged_pass_through_untouched(repo):
    entry = op.AgentsTracking("elsewhere", repo, op.AGENTS_NO_REPO)

    assert op.commit_agents_files([entry], "a template") == [entry]


# ── the prompt ───────────────────────────────────────────────────
def _wire(monkeypatch, answers, projects):
    """Drive `retire_user_instructions` past everything but the decision."""
    asked: list[str] = []
    committed: list[list] = []
    placed = [_outcome(Path(p["path"]), p["label"]) for p in projects]

    def fake_prompt(prompt):
        asked.append(prompt)
        return answers.pop(0) if answers else ""

    monkeypatch.setattr(op, "_prompt_line", fake_prompt)
    monkeypatch.setattr(op, "user_instructions_present", lambda: True)
    monkeypatch.setattr(op, "catalog_projects", lambda: (projects, []))
    monkeypatch.setattr(op, "_catalog_fingerprint", lambda: "same")
    monkeypatch.setattr(op.install_manifest, "load", lambda home: {})
    monkeypatch.setattr(op.project_instructions, "resolve_source",
                        lambda *a, **k: ("body", "a template"))
    result = pi.RetirementResult(source_origin="a template")
    result.outcomes = placed
    result.removed = True
    monkeypatch.setattr(op.project_instructions, "retire",
                        lambda *a, **k: result)
    monkeypatch.setattr(op, "stage_agents_files",
                        lambda outcomes: [op.AgentsTracking(
                            o.label, Path(o.path), op.AGENTS_STAGED)
                            for o in outcomes])

    def fake_commit(tracked, origin):
        committed.append(list(tracked))
        return [op.AgentsTracking(t.label, t.root, op.AGENTS_COMMITTED)
                for t in tracked]

    monkeypatch.setattr(op, "commit_agents_files", fake_commit)
    return asked, committed


def test_declining_the_commit_prompt_leaves_the_file_staged(monkeypatch, repo,
                                                            capsys):
    projects = [{"guid": "g", "path": str(repo), "label": "proj"}]
    asked, committed = _wire(monkeypatch, ["y", "n"], projects)

    assert op.retire_user_instructions() == 0

    assert committed == [], "declining must not commit"
    assert any("Commit" in prompt for prompt in asked)
    assert op.AGENTS_STAGED in capsys.readouterr().out


def test_accepting_the_commit_prompt_commits(monkeypatch, repo, capsys):
    projects = [{"guid": "g", "path": str(repo), "label": "proj"}]
    asked, committed = _wire(monkeypatch, ["y", "y"], projects)

    assert op.retire_user_instructions() == 0

    assert len(committed) == 1
    assert op.AGENTS_COMMITTED in capsys.readouterr().out


def test_assume_yes_stages_but_never_asks_and_never_commits(monkeypatch, repo,
                                                            capsys):
    """`--yes` consents to writing files, which is not consent to commit.

    Unattended is exactly when nobody is watching where a commit lands, so the
    conservative half of the behaviour is the half that runs.
    """
    projects = [{"guid": "g", "path": str(repo), "label": "proj"}]
    asked, committed = _wire(monkeypatch, [], projects)

    assert op.retire_user_instructions(assume_yes=True) == 0

    assert committed == []
    assert not any("Commit" in prompt for prompt in asked)
    assert op.AGENTS_STAGED in capsys.readouterr().out


# ── the whole command, with nothing about git faked ──────────────
def test_retiring_for_real_leaves_every_file_committed(monkeypatch, tmp_path,
                                                       repo, capsys):
    """End to end: real `retire()`, real git, two real repositories.

    Every test above this line either drives the git helpers directly or
    replaces them. Neither shape can see the seam between them -- a wrong
    attribute where the outcomes are handed over would leave both halves
    passing and the actual command raising on the first real run, after it had
    already written into every registered repository. This is the only test
    that crosses that seam.
    """
    second = tmp_path / "second"
    second.mkdir()
    _git(second, "init", "-b", "main")
    _git(second, "config", "user.email", "test@example.invalid")
    _git(second, "config", "user.name", "Test")
    _git(second, "config", "commit.gpgsign", "false")
    (second / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(second, "add", "seed.txt")
    _git(second, "commit", "-m", "seed")

    global_path = tmp_path / "copilot-instructions.md"
    global_path.write_text("# conventions\n\nBe careful.\n", encoding="utf-8")
    projects = [{"guid": "g1", "path": str(repo), "label": "first"},
                {"guid": "g2", "path": str(second), "label": "second"}]

    monkeypatch.setattr(op, "global_instructions_path", lambda: global_path)
    monkeypatch.setattr(op, "user_instructions_present", lambda: True)
    monkeypatch.setattr(op, "catalog_projects", lambda: (projects, []))
    monkeypatch.setattr(op, "_catalog_fingerprint", lambda: "same")
    monkeypatch.setattr(op, "instructions_archive_dir",
                        lambda: tmp_path / "archive")
    monkeypatch.setattr(op, "projects_root", lambda: tmp_path / "projects")
    monkeypatch.setattr(op.install_manifest, "load", lambda home: {})
    monkeypatch.setattr(op.project_instructions, "resolve_source",
                        lambda *a, **k: (global_path.read_text(encoding="utf-8"),
                                         str(global_path)))
    answers = ["y", "y"]
    monkeypatch.setattr(op, "_prompt_line",
                        lambda prompt: answers.pop(0) if answers else "")

    assert op.retire_user_instructions() == 0

    for root in (repo, second):
        assert (root / pi.AGENTS_NAME).exists()
        assert _is_tracked(root), f"{root} left its AGENTS.md untracked"
        head = _git(root, "show", "--name-only", "--format=", "HEAD").split()
        assert head == [pi.AGENTS_NAME]
    out = capsys.readouterr().out
    assert out.count(op.AGENTS_COMMITTED) >= 2
