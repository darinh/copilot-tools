"""``operator worktree``: the checkout of a work item (E4).

Three properties carry this file.

**A claim and its checkout are created together or neither is.** The failure
that matters is the half-done one: a directory made under a claim that was
refused has no owner and no record of who made it, and a claim held for a
checkout that was never created sends the next agent looking for work that
does not exist. So the tests here assert on both sides after every refusal,
not only on the return value.

**Nothing here is a faster way to lose uncommitted work.** ``finish`` refuses
a dirty tree rather than tidying it, and ``recover`` removes nothing at all.
Both are checked against the *command stream* -- every git invocation the call
made -- as well as against the outcome, because an outcome test passes for a
call that destroyed something and rebuilt it from a commit it had just made.

**A branch is deleted only when its commits are somewhere else.** That is the
one irreversible act any of these verbs perform, and it is guarded twice: by
the ancestry check in :func:`operator_worktree._merged_into` and by ``git
branch -d`` underneath it.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import copilot_operator as op
import install_manifest
import operator_liveness as live
import operator_session as osess
import operator_work as ow
import operator_worktree as owt
import work_claims as wc


# ── fakes ───────────────────────────────────────────────────────
class FakeProbes:
    """Probes whose every answer is stated by the test that built them."""

    def __init__(self, *, boot="boot-1", pids=None, sessions=None,
                 token="start-token"):
        self.boot = boot
        self.pids = {} if pids is None else dict(pids)
        self.sessions = {} if sessions is None else dict(sessions)
        self.token = token

    def boot_identity(self):
        return self.boot

    def process_present(self, pid):
        return self.pids.get(pid, True)

    def process_start_token(self, pid):
        return self.token

    def session_present(self, session):
        return self.sessions.get(session, True)


class RecordingGit:
    """A git runner that records every invocation and delegates to the real one.

    The recording is the point: FR-4 is a statement about which commands are
    *issued*, and a tree inspected afterwards cannot distinguish a call that
    never touched it from one that clobbered it and put it back.
    """

    def __init__(self):
        self.calls: list = []

    def __call__(self, args, repo, env_extra):
        self.calls.append(list(args))
        return _real_runner(args, repo, env_extra)

    @property
    def verbs(self) -> list:
        return [call[0] for call in self.calls]


def _real_runner(args, repo, env_extra):
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120, env=env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise ow.GitUnavailable(detail[0] if detail else "git failed")
    return proc.stdout


# ── fixtures ────────────────────────────────────────────────────
def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A primary checkout on ``main`` with one commit."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", "agent@example.invalid")
    _git(root, "config", "user.name", "Agent")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "first")
    return root


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = osess.db_path(tmp_path)
    osess.init_db(path)
    return path


def _new(db: Path, repo: Path, *, item="0007", instance="alpha", pid=None,
         probes=None, **kw):
    return owt.new(db, repo, item=item, instance=instance, pid=pid,
                   probes=probes or FakeProbes(), **kw)


def _snapshot(tree: Path) -> dict:
    """Everything a preservation promises not to change."""
    index = Path(_git(tree, "rev-parse", "--path-format=absolute",
                      "--git-path", "index").strip())
    return {
        "status": _git(tree, "status", "--porcelain"),
        "head": _git(tree, "rev-parse", "HEAD"),
        "branch": _git(tree, "rev-parse", "--abbrev-ref", "HEAD"),
        "files": {p.name: p.read_bytes() for p in sorted(tree.glob("*.txt"))},
        "index": index.read_bytes() if index.exists() else None,
    }


# ── naming ──────────────────────────────────────────────────────
@pytest.mark.parametrize("branch,expected", [
    ("feat/login", "feat-login"),
    ("work/0007", "work-0007"),
    ("plain", "plain"),
    ("a/b/c", "a-b-c"),
    ("back\\slash", "back-slash"),
    ("", "worktree"),
])
def test_the_directory_name_is_the_branch_with_no_separator_left(
        branch, expected) -> None:
    """A separator surviving here buys the checkout a second path segment,
    which puts it somewhere other than under `.worktrees/`."""
    assert owt.slug(branch) == expected


@pytest.mark.parametrize("item", [
    "0007", "0007-a-slug", "specs/004-operator-session", "has space", "..",
    ".leading", "trailing.", "a~b^c:d?e*f[g", "item.lock", "item.lock.lock",
    "@", "", "\x01\x7f", "—unicode—", "/",
])
def test_a_derived_branch_is_always_a_legal_ref(item) -> None:
    """Checked against git itself rather than against our reading of its
    rules: a name git refuses fails `worktree add` after the claim is taken."""
    name = owt.derived_branch(item)
    proc = subprocess.run(["git", "check-ref-format", "--branch", name],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == 0, f"git refused {name!r}: {proc.stderr}"
    assert name.startswith(owt.WORK_PREFIX)
    assert name.count("/") == 1


def test_the_default_path_is_the_layout_agents_md_states(repo: Path) -> None:
    assert owt.default_path(repo, "feat/login") == (
        repo / ".worktrees" / "feat-login")


# ── the prefix trap ─────────────────────────────────────────────
def test_a_sibling_sharing_a_name_prefix_is_not_inside(tmp_path: Path) -> None:
    """`startswith` answers True for `.worktrees/feat-a2` under
    `.worktrees/feat-a`, which would refuse a legitimate `finish` -- and the
    same mistake in the other direction permits one it meant to refuse."""
    assert owt._is_inside(tmp_path / "feat-a" / "src", tmp_path / "feat-a")
    assert owt._is_inside(tmp_path / "feat-a", tmp_path / "feat-a")
    assert not owt._is_inside(tmp_path / "feat-a2", tmp_path / "feat-a")


# ── new ─────────────────────────────────────────────────────────
def test_new_creates_the_checkout_and_the_claim_together(db: Path,
                                                         repo: Path) -> None:
    result = _new(db, repo)
    assert result.ok is True
    tree = Path(result.path)
    assert tree == repo / ".worktrees" / "work-0007"
    assert install_manifest.dir_present(tree) is True
    held = wc.claim_for_item(db, "0007")
    assert held is not None
    assert held.instance == "alpha"
    assert owt._same_path(held.worktree, tree)
    assert held.branch == "work/0007"
    assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "work/0007"


def test_new_records_the_platform_that_wrote_the_path(db: Path,
                                                      repo: Path) -> None:
    """The path is in this machine's syntax and nothing converts it. Without
    the recorded platform a reader on the other kind of system sees a path
    that is not invalid, merely wrong."""
    _new(db, repo)
    assert wc.claim_for_item(db, "0007").platform == os.name


def test_new_refuses_an_existing_path_and_leaves_no_claim(db: Path,
                                                          repo: Path) -> None:
    """The claim is taken first and released again here. What must not survive
    a refusal is the claim: a retry would otherwise report `instance-busy`
    against the agent's own name."""
    (repo / ".worktrees" / "work-0007").mkdir(parents=True)
    result = _new(db, repo)
    assert result.ok is False
    assert result.refused == owt.PATH_EXISTS
    assert wc.claim_for_item(db, "0007") is None
    assert wc.claim_for_instance(db, "alpha") is None


def test_new_refuses_a_path_it_could_not_examine(db: Path, repo: Path,
                                                 monkeypatch) -> None:
    """`path_present` answers None for a path that could not be examined, and
    that is not "absent": pointing `worktree add` at somebody's existing
    checkout is the failure this direction produces."""
    monkeypatch.setattr(install_manifest, "path_present", lambda path: None)
    result = _new(db, repo)
    assert result.refused == owt.PATH_UNREADABLE
    assert wc.claim_for_item(db, "0007") is None


def test_new_releases_its_own_claim_when_git_refuses(db: Path,
                                                     repo: Path) -> None:
    """The compensating release, and the reason it is safe: the claim was
    taken by this call microseconds earlier, so no agent can have worked
    under it."""
    def refuse(args, repo_arg, env_extra):
        if args[:2] == ["worktree", "add"]:
            raise ow.GitUnavailable("worktree add refused")
        return _real_runner(args, repo_arg, env_extra)

    result = _new(db, repo, runner=refuse)
    assert result.ok is False
    assert result.refused == owt.GIT_FAILED
    assert wc.claim_for_item(db, "0007") is None
    assert wc.claim_for_instance(db, "alpha") is None
    assert install_manifest.path_present(repo / ".worktrees" / "work-0007") is False


def test_new_checks_out_a_branch_that_already_exists(db: Path,
                                                     repo: Path) -> None:
    """`worktree add -b` fails on an existing branch, and the natural reading
    of `--branch` is "put this branch in a tree", not "make a new one"."""
    _git(repo, "branch", "feat/login")
    result = _new(db, repo, branch="feat/login")
    assert result.ok is True
    assert any("already existed" in note for note in result.notes)
    assert _git(Path(result.path), "rev-parse", "--abbrev-ref",
                "HEAD").strip() == "feat/login"


def test_new_refuses_an_item_somebody_else_holds(db: Path, repo: Path) -> None:
    """The refusal names the claim, not the directory. Both facts are true at
    this point and only one has a next move: `operator work list` and
    `reclaim` answer a held item, while a person has to answer a directory."""
    _new(db, repo, item="0007", instance="alpha")
    result = _new(db, repo, item="0007", instance="beta")
    assert result.refused == wc.ITEM_HELD
    assert wc.claim_for_item(db, "0007").instance == "alpha"
    assert wc.claim_for_instance(db, "beta") is None


def test_new_refuses_a_second_item_for_one_instance(db: Path,
                                                    repo: Path) -> None:
    """Spec D6: one work item per agent."""
    _new(db, repo, item="0007", instance="alpha")
    result = _new(db, repo, item="0008", instance="alpha")
    assert result.refused == wc.INSTANCE_BUSY
    assert wc.claim_for_item(db, "0008") is None
    assert install_manifest.path_present(
        repo / ".worktrees" / "work-0008") is False


def test_new_issues_no_mutating_verb(db: Path, repo: Path) -> None:
    git = RecordingGit()
    _new(db, repo, runner=git)
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    assert not forbidden.intersection(git.verbs), git.calls


# ── finish ──────────────────────────────────────────────────────
def _finished(db: Path, repo: Path, **kw):
    return owt.finish(db, repo, item="0007", instance="alpha", cwd=repo, **kw)


def test_finish_removes_the_tree_and_releases_the_claim(db: Path,
                                                        repo: Path) -> None:
    tree = Path(_new(db, repo).path)
    (tree / "work.txt").write_text("done\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "--quiet", "-m", "work")
    _git(repo, "merge", "--quiet", "--no-ff", "-m", "merge", "work/0007")

    result = _finished(db, repo)
    assert result.ok is True
    assert install_manifest.dir_present(tree) is False
    assert wc.claim_for_item(db, "0007") is None
    assert result.branch_deleted is True
    assert "work/0007" not in _git(repo, "branch", "--list", "work/0007")


def test_finish_keeps_a_branch_the_integration_ref_does_not_contain(
        db: Path, repo: Path) -> None:
    """Removing the checkout loses nothing while the branch survives.
    Deleting the branch is the one irreversible act here, so it happens only
    when the commits are demonstrably somewhere else."""
    tree = Path(_new(db, repo).path)
    (tree / "work.txt").write_text("unmerged\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "--quiet", "-m", "unmerged work")

    result = _finished(db, repo)
    assert result.ok is True
    assert result.branch_deleted is False
    assert any("not contained in main" in note for note in result.notes)
    assert _git(repo, "branch", "--list", "work/0007").strip() != ""


def test_finish_refuses_a_dirty_tree_and_removes_nothing(db: Path,
                                                         repo: Path) -> None:
    tree = Path(_new(db, repo).path)
    (tree / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (tree / "untracked.txt").write_text("brand new\n", encoding="utf-8")
    before = _snapshot(tree)

    git = RecordingGit()
    result = _finished(db, repo, runner=git)
    assert result.ok is False
    assert result.refused == owt.WORKTREE_DIRTY
    assert _snapshot(tree) == before
    assert wc.claim_for_item(db, "0007") is not None
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    assert not forbidden.intersection(git.verbs), git.calls
    assert "remove" not in [call[1] for call in git.calls
                            if call[0] == "worktree" and len(call) > 1]


def test_finish_refuses_from_inside_the_tree_it_would_remove(
        db: Path, repo: Path) -> None:
    """git's own refusal covers the exact directory; this covers being
    anywhere beneath it, which is where an agent actually stands."""
    tree = Path(_new(db, repo).path)
    result = owt.finish(db, repo, item="0007", instance="alpha",
                        cwd=tree / "deep" / "deeper")
    assert result.refused == owt.INSIDE_TARGET
    assert install_manifest.dir_present(tree) is True
    assert wc.claim_for_item(db, "0007") is not None


def test_finish_refuses_an_item_this_instance_does_not_hold(db: Path,
                                                            repo: Path) -> None:
    _new(db, repo, item="0007", instance="alpha")
    result = owt.finish(db, repo, item="0007", instance="beta", cwd=repo)
    assert result.refused == owt.NOT_OWNER
    assert wc.claim_for_item(db, "0007").instance == "alpha"


def test_finish_refuses_an_unclaimed_item(db: Path, repo: Path) -> None:
    result = owt.finish(db, repo, item="0009", instance="alpha", cwd=repo)
    assert result.refused == owt.NOT_OWNER


def test_finish_refuses_a_claim_with_no_worktree(db: Path, repo: Path) -> None:
    wc.claim(db, item="0007", instance="alpha", worktree=None, branch=None)
    result = _finished(db, repo)
    assert result.refused == owt.NO_WORKTREE
    assert wc.claim_for_item(db, "0007") is not None


def test_finish_refuses_a_worktree_recorded_on_another_platform(
        db: Path, repo: Path) -> None:
    """A path in the other syntax is not invalid here, merely wrong: the
    probe reports the tree absent and the prune branch would then run against
    a live checkout somebody else is standing in.

    The recorded path is deliberately one the *syntax* test calls native, so
    this proves the platform column is consulted rather than the shape."""
    other = "posix" if os.name == "nt" else "nt"
    _new(db, repo)
    with wc.connect(db) as conn:
        conn.execute("UPDATE work_claims SET platform = ? WHERE item = ?",
                     (other, "0007"))
    held = wc.claim_for_item(db, "0007")
    assert ow._foreign_path(held.worktree) is False, (
        "the path must be one the syntax test calls native, or this proves "
        "nothing about the platform column")
    result = _finished(db, repo)
    assert result.refused == owt.FOREIGN_PLATFORM
    assert wc.claim_for_item(db, "0007") is not None
    assert install_manifest.dir_present(Path(held.worktree)) is True


def test_finish_prunes_a_registration_whose_directory_is_gone(
        db: Path, repo: Path) -> None:
    """The remaining half of a removal somebody started by hand. It deletes
    no content, and it runs only on evidence of absence."""
    tree = Path(_new(db, repo).path)
    _git(repo, "worktree", "remove", str(tree))
    assert install_manifest.dir_present(tree) is False

    result = _finished(db, repo)
    assert result.ok is True
    assert any("already gone" in note for note in result.notes)
    assert wc.claim_for_item(db, "0007") is None


def test_finish_refuses_a_directory_it_could_not_examine(
        db: Path, repo: Path, monkeypatch) -> None:
    _new(db, repo)
    monkeypatch.setattr(install_manifest, "dir_present", lambda path: None)
    result = _finished(db, repo)
    assert result.refused == owt.PATH_UNREADABLE
    assert wc.claim_for_item(db, "0007") is not None


def test_finish_keeps_the_claim_when_the_removal_fails(db: Path,
                                                       repo: Path) -> None:
    """The recoverable direction. An item still claimed by an agent that has
    finished is judged by the liveness cascade; an item released with its
    checkout still on disk is a tree with no owner."""
    _new(db, repo)

    def refuse(args, repo_arg, env_extra):
        if args[:2] == ["worktree", "remove"]:
            raise ow.GitUnavailable("removal refused")
        return _real_runner(args, repo_arg, env_extra)

    result = _finished(db, repo, runner=refuse)
    assert result.ok is False
    assert result.refused == owt.GIT_FAILED
    assert wc.claim_for_item(db, "0007") is not None


# ── recover ─────────────────────────────────────────────────────
def test_survey_calls_the_first_record_the_primary_checkout(db: Path,
                                                            repo: Path) -> None:
    """The only reliable way to tell it from a linked worktree, from anywhere
    in the repository."""
    _new(db, repo)
    rows = owt.survey(db, repo, probes=FakeProbes())
    assert rows[0].state == owt.PRIMARY
    assert owt._same_path(rows[0].path, repo)


def _dead(db: Path, repo: Path, **kw):
    """A checkout whose owner the cascade will call DEAD.

    The pid is the signal, not the boot id: `same_boot` answers None for two
    strings that are not in the recorded `uuid:`/`instant:` form, and a test
    that leaned on it would be asserting against STALE while reading DEAD.
    """
    return _new(db, repo, pid=1000, probes=FakeProbes(pids={1000: True}), **kw)


DEAD_PROBES = dict(pids={1000: False})


def test_survey_reports_a_dead_owner_without_taking_anything(db: Path,
                                                             repo: Path) -> None:
    _dead(db, repo)
    rows = owt.survey(db, repo, probes=FakeProbes(**DEAD_PROBES))
    dead = [row for row in rows if row.state == owt.DEAD]
    assert len(dead) == 1
    assert dead[0].claim.instance == "alpha"
    assert dead[0].liveness.verdict == live.DEAD
    assert wc.claim_for_item(db, "0007").instance == "alpha"


def test_survey_reports_a_stale_owner_as_its_own_state(db: Path,
                                                       repo: Path) -> None:
    """STALE is not DEAD. Collapsing them is how two agents end up in one
    tree, and it is the reason `recover` preserves against DEAD only."""
    result = _dead(db, repo)
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=3)).strftime(
        wc.TS_FORMAT)
    with wc.connect(db) as conn:
        conn.execute("UPDATE work_claims SET heartbeat_at = ? WHERE item = ?",
                     (old, "0007"))
    rows = owt.survey(db, repo, probes=FakeProbes(pids={1000: None}))
    assert result.ok
    stale = [row for row in rows if row.state == owt.STALE]
    assert len(stale) == 1
    assert stale[0].liveness.verdict == live.STALE


def test_survey_reports_a_checkout_no_claim_names(db: Path,
                                                  repo: Path) -> None:
    _git(repo, "worktree", "add", "--quiet", "-b", "orphan",
         str(repo / ".worktrees" / "orphan"))
    rows = owt.survey(db, repo, probes=FakeProbes())
    states = {row.state for row in rows}
    assert owt.UNCLAIMED in states


def test_survey_reports_a_claim_git_has_no_registration_for(db: Path,
                                                            repo: Path) -> None:
    """The other direction of the same mismatch, and the one a path-keyed
    join drops if it only walks the registrations."""
    wc.claim(db, item="0007", instance="alpha",
             worktree=str(repo / ".worktrees" / "never-made"),
             branch="work/0007")
    rows = owt.survey(db, repo, probes=FakeProbes())
    unregistered = [row for row in rows if row.state == owt.UNREGISTERED]
    assert len(unregistered) == 1
    assert unregistered[0].claim.item == "0007"


def test_survey_reports_a_registration_whose_directory_is_gone(
        db: Path, repo: Path) -> None:
    tree = Path(_new(db, repo).path)
    for path in sorted(tree.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        else:
            path.rmdir()
    tree.rmdir()
    rows = owt.survey(db, repo, probes=FakeProbes())
    assert owt.MISSING in {row.state for row in rows}


def test_survey_removes_nothing_and_issues_no_mutating_verb(
        db: Path, repo: Path) -> None:
    tree = Path(_dead(db, repo).path)
    (tree / "tracked.txt").write_text("modified\n", encoding="utf-8")
    before = _snapshot(tree)
    git = RecordingGit()
    owt.survey(db, repo, probes=FakeProbes(**DEAD_PROBES), preserve=True,
               runner=git)
    assert _snapshot(tree) == before
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    assert not forbidden.intersection(git.verbs), git.calls
    assert "remove" not in [call[1] for call in git.calls
                            if call[0] == "worktree" and len(call) > 1]
    assert "prune" not in [call[1] for call in git.calls
                           if call[0] == "worktree" and len(call) > 1]


def test_preserve_banks_the_work_of_a_tree_whose_owner_is_gone(
        db: Path, repo: Path) -> None:
    tree = Path(_dead(db, repo).path)
    (tree / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (tree / "untracked.txt").write_text("brand new\n", encoding="utf-8")
    rows = owt.survey(db, repo, probes=FakeProbes(**DEAD_PROBES), preserve=True)
    dead = [row for row in rows if row.state == owt.DEAD]
    assert len(dead) == 1
    banked = dead[0].preserved
    assert banked is not None and banked.dirty is True
    assert banked.branch.startswith(ow.WIP_PREFIX)
    listed = _git(tree, "show", "--name-only", "--format=", banked.commit)
    assert "untracked.txt" in listed


def test_preserve_leaves_a_live_owners_tree_alone(db: Path,
                                                  repo: Path) -> None:
    """The whole cascade exists so that "I could not confirm it is alive" and
    "I confirmed it is dead" are different answers. Preserving a live agent's
    tree would write a branch nobody asked for into the tree they are in."""
    tree = Path(_new(db, repo).path)
    (tree / "tracked.txt").write_text("modified\n", encoding="utf-8")
    rows = owt.survey(db, repo, probes=FakeProbes(), preserve=True)
    assert [row.preserved for row in rows] == [None] * len(rows)
    assert _git(tree, "branch", "--list", "wip/*").strip() == ""


def test_preserve_reports_a_failure_rather_than_dropping_it(
        db: Path, repo: Path) -> None:
    """A tree that could not be read is a question about the filesystem, and
    answering it with silence is how somebody's work goes unlooked-for."""
    _dead(db, repo)

    def refuse(args, repo_arg, env_extra):
        if args[0] == "status":
            raise ow.GitUnavailable("status refused")
        return _real_runner(args, repo_arg, env_extra)

    rows = owt.survey(db, repo, probes=FakeProbes(**DEAD_PROBES),
                      preserve=True, runner=refuse)
    dead = [row for row in rows if row.state == owt.DEAD]
    assert len(dead) == 1
    assert "not preserved" in dead[0].note


# ── the porcelain reader ────────────────────────────────────────
PORCELAIN = (
    "worktree /repo\n"
    "HEAD 1111111111111111111111111111111111111111\n"
    "branch refs/heads/main\n"
    "\n"
    "worktree /repo/.worktrees/feat-a\n"
    "HEAD 2222222222222222222222222222222222222222\n"
    "branch refs/heads/feat/a\n"
    "\n"
    "worktree /repo/.worktrees/detached\n"
    "HEAD 3333333333333333333333333333333333333333\n"
    "detached\n"
    "\n"
    "worktree /repo/.worktrees/gone\n"
    "HEAD 4444444444444444444444444444444444444444\n"
    "branch refs/heads/gone\n"
    "prunable gitdir file points to non-existent location\n"
)


def test_the_porcelain_reader_keeps_gits_order_and_strips_the_ref_prefix() -> None:
    records = owt.parse_worktree_list(PORCELAIN)
    assert [r.path for r in records] == [
        "/repo", "/repo/.worktrees/feat-a", "/repo/.worktrees/detached",
        "/repo/.worktrees/gone"]
    assert records[1].branch == "feat/a"
    assert records[2].detached is True and records[2].branch is None
    assert records[3].prunable.startswith("gitdir file")


def test_the_porcelain_reader_survives_a_missing_trailing_blank_line() -> None:
    """git terminates the last record with a blank line, and a reader that
    depends on one drops whichever worktree is listed last -- the newest, in
    practice, which is the one an agent has just made."""
    records = owt.parse_worktree_list("worktree /repo\nbranch refs/heads/main")
    assert [r.path for r in records] == ["/repo"]
    assert records[0].branch == "main"


def test_the_porcelain_reader_treats_the_blank_line_as_the_record_boundary(
) -> None:
    """The blank line is the separator git documents, and `worktree` merely
    happens to be the key that follows it today. A reader that closed records
    only on `worktree` would attribute anything after a blank line to the
    previous record -- so an attribute git adds later, or a line this reader
    does not recognise, would silently rewrite the record above it rather than
    being ignored."""
    records = owt.parse_worktree_list(
        "worktree /repo\n"
        "branch refs/heads/main\n"
        "\n"
        "branch refs/heads/ghost\n"
        "prunable stray\n")
    assert [r.path for r in records] == ["/repo"]
    assert records[0].branch == "main"
    assert records[0].prunable is None


def test_the_porcelain_reader_reads_the_real_thing(repo: Path) -> None:
    """Against git's actual output, because every assertion above is against
    a string this file wrote."""
    _git(repo, "worktree", "add", "--quiet", "-b", "feat/a",
         str(repo / ".worktrees" / "feat-a"))
    records = owt.registrations(repo)
    assert len(records) == 2
    assert owt._same_path(records[0].path, repo)
    assert records[1].branch == "feat/a"


# ── the module issues no mutating verb, statically ──────────────
def test_no_mutating_git_verb_appears_in_the_module_at_all() -> None:
    """A source scan beside the behavioural tests, because the behavioural
    ones can only cover the paths a test reached. The promise is about every
    path, including the ones added tomorrow.

    Over the parsed tree rather than the raw text: a text scan has to pick a
    quote style, and the one thing a forbidden verb would not do is arrive in
    the spelling the scan happened to choose.
    """
    tree = ast.parse(Path(owt.__file__).read_text(encoding="utf-8"))
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    offenders = sorted({
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in forbidden})
    assert offenders == [], (
        f"{offenders} appears as a string constant in operator_worktree.py")


def test_the_verb_scan_would_notice_a_single_quoted_offender() -> None:
    """The positive control. A scan that matches nothing reports a clean tree,
    which reads exactly like success."""
    tree = ast.parse("def f():\n    return _git(['stash', 'list'], root)\n")
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    found = sorted({
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in forbidden})
    assert found == ["stash"]


def test_the_module_never_forces_a_branch_deletion() -> None:
    """`git branch -d` refuses an unmerged branch and `-D` does not. The
    ancestry check above it is the first guard; git's own is the second, and
    it only exists while the flag stays `-d`."""
    tree = ast.parse(Path(owt.__file__).read_text(encoding="utf-8"))
    assert not [node for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and node.value == "-D"]
    assert not [node for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and node.value in ("--force", "-f")]


# ── the CLI ─────────────────────────────────────────────────────
def _wire(monkeypatch, db: Path, repo: Path) -> None:
    """`operator worktree ...` against a temp project and silent probes.

    The probes are replaced on :mod:`operator_liveness` itself, which is the
    module object ``operator_work`` resolves ``SystemProbes`` from at call
    time. Without it the real mux probe runs and the suite's multiplexer guard
    refuses the spawn -- correctly, since a unit test's answers must not
    depend on what happens to be running on this machine.
    """
    monkeypatch.setattr(op, "_session_db", lambda cwd: db)
    monkeypatch.setattr(op, "primary_repo_root", lambda cwd: repo)
    monkeypatch.setattr(op, "_agent_pid", lambda instance: None)
    monkeypatch.setattr(live, "SystemProbes", lambda *a, **kw: FakeProbes())


def test_worktree_is_a_reserved_word_of_the_operator() -> None:
    """Without this, `operator worktree` is read as an instance name and
    starts a copilot session called `worktree`."""
    assert "worktree" in op.SUBCOMMANDS
    assert "worktree" in op.RESERVED_WORDS


def test_an_unknown_worktree_verb_is_refused_rather_than_run(capsys) -> None:
    assert op.manage_worktree(["destroy"]) == 1
    assert "Unknown subcommand" in capsys.readouterr().err


def test_worktree_with_no_arguments_prints_usage_and_fails(capsys) -> None:
    assert op.manage_worktree([]) == 1
    assert "operator worktree new" in capsys.readouterr().err


def test_worktree_help_prints_usage_and_succeeds(capsys) -> None:
    assert op.manage_worktree(["--help"]) == 0
    assert "operator worktree recover" in capsys.readouterr().out


def test_new_and_finish_require_an_instance(capsys) -> None:
    assert op.manage_worktree(["new", "--item", "0007"]) == 1
    assert "--instance" in capsys.readouterr().err


def test_an_unknown_option_is_refused_rather_than_ignored(capsys) -> None:
    """A caller who typed `--brnch` and saw a success message believes an
    effect happened that did not."""
    assert op.manage_worktree(["new", "--instance", "alpha", "--brnch",
                               "x"]) == 1
    assert "--brnch" in capsys.readouterr().err


def test_the_cli_reports_a_refusal_as_json_and_a_nonzero_exit(
        db: Path, repo: Path, monkeypatch, capsys) -> None:
    """The JSON path is what an agent reads, and a refusal that exits 0 there
    is a refusal nothing notices."""
    _wire(monkeypatch, db, repo)
    (repo / ".worktrees" / "work-0007").mkdir(parents=True)
    rc = op.manage_worktree(["new", "--instance", "alpha", "--item", "0007",
                             "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["refused"] == owt.PATH_EXISTS


def test_the_cli_creates_a_checkout_and_finishes_it(db: Path, repo: Path,
                                                    monkeypatch, capsys) -> None:
    """End to end through the argument parser, because everything above calls
    the functions directly and an option bound to the wrong key is invisible
    from there."""
    _wire(monkeypatch, db, repo)
    monkeypatch.chdir(repo)

    assert op.manage_worktree(["new", "--instance", "alpha", "--item", "0007",
                               "--branch", "feat/login", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["branch"] == "feat/login"
    tree = Path(created["path"])
    assert tree.name == "feat-login"
    assert install_manifest.dir_present(tree) is True

    assert op.manage_worktree(["finish", "--instance", "alpha", "--json"]) == 0
    finished = json.loads(capsys.readouterr().out)
    assert finished["ok"] is True
    assert install_manifest.dir_present(tree) is False
    assert wc.claim_for_item(db, "0007") is None


def test_the_cli_recovers_without_an_instance(db: Path, repo: Path,
                                              monkeypatch, capsys) -> None:
    """`recover` is a question about the project, not about one agent, and
    demanding a name to ask it would make the natural reading unavailable."""
    _wire(monkeypatch, db, repo)
    assert op.manage_worktree(["recover", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["state"] for row in rows] == [owt.PRIMARY]
