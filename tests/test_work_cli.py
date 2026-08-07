"""``operator work``: the policy over the claim store, and the reclaim (FR-3, FR-4).

Two properties carry this file, and both are asserted rather than argued.

**A claim never records a signal that is already false.** The liveness cascade
treats each recorded identity field as evidence, and three of the four can
conclude DEAD alone. So a claim written with the pid of the ``operator``
process that wrote it -- gone a second later -- is not merely imprecise: it is
manufactured proof that the owner is dead, and the next sweep hands a live
agent's worktree to somebody else.

**A reclaim preserves before it reassigns, and issues no mutating git verb.**
The incident behind FR-4 is a ``git stash`` that destroyed 454 lines and was
recoverable only because the work happened to have been staged. So the tests
here check the working tree, the index and ``HEAD`` are byte-identical after a
preservation, and they check the *command stream* -- every git invocation the
reclaim made -- rather than only its outcome, because an outcome test passes
for a reclaim that destroyed something and then rebuilt it.
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
import operator_liveness as live
import operator_session as osess
import operator_work as ow
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
        self.asked_pids: list = []
        self.asked_sessions: list = []

    def boot_identity(self):
        return self.boot

    def process_present(self, pid):
        self.asked_pids.append(pid)
        return self.pids.get(pid, True)

    def process_start_token(self, pid):
        return self.token

    def session_present(self, session):
        self.asked_sessions.append(session)
        return self.sessions.get(session, True)


class RecordingGit:
    """A git runner that records every invocation and delegates to the real one.

    The recording is the point. FR-4 is a statement about which commands are
    *issued*, and a test that only inspects the tree afterwards cannot tell a
    reclaim that never touched the worktree from one that clobbered it and
    restored it from the commit it had just made.
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


# ── git fixtures ────────────────────────────────────────────────
def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one commit and a configured identity."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "agent@example.invalid")
    _git(root, "config", "user.name", "Agent")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "--quiet", "-m", "first")
    return root


def _dirty(repo: Path) -> None:
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("brand new\n", encoding="utf-8")


def _snapshot(repo: Path) -> dict:
    """Everything a preservation promises not to change."""
    index = repo / ".git" / "index"
    return {
        "status": _git(repo, "status", "--porcelain"),
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "files": {p.name: p.read_bytes()
                  for p in sorted(repo.glob("*.txt"))},
        "index": index.read_bytes() if index.exists() else None,
    }


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = osess.db_path(tmp_path)
    osess.init_db(path)
    return path


def _claim(db: Path, item="0007", instance="alpha", *, worktree=None,
           pid=1000, when=None, subproject="", branch=None):
    return wc.claim(db, item=item, instance=instance, subproject=subproject,
                    worktree=None if worktree is None else str(worktree),
                    branch=branch, boot_id="boot-1", mux_session=instance,
                    pid=pid, pid_start="start-token", now=when)


# ── branch naming ───────────────────────────────────────────────
NASTY = [
    ("0007-slug", "copilot-tools"),
    ("specs/004-operator-session", "peer/one"),
    ("has space", "has space"),
    ("..", ".."),
    (".leading", "trailing."),
    ("a~b^c:d?e*f[g", "back\\slash"),
    ("item.lock", "inst.lock"),
    ("item.lock.lock", "inst.lock.lock.lock"),
    ("@", "@{"),
    ("", ""),
    ("\x01\x7f", "control\tchars"),
    ("—unicode—", "naïve"),
    ("/", "//"),
]


@pytest.mark.parametrize("item,instance", NASTY)
def test_the_preservation_branch_name_is_always_a_legal_ref(item, instance) -> None:
    """Checked against git itself, not against our reading of its rules.

    The failure this prevents is the worst-timed one available: the branch is
    created last, so a name git refuses loses the reclaim *after* the commit
    holding the departed agent's work exists with nothing pointing at it.
    """
    name = ow.wip_branch(item, instance)
    proc = subprocess.run(["git", "check-ref-format", "--branch", name],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == 0, f"git refused {name!r}: {proc.stderr}"


@pytest.mark.parametrize("item,instance", NASTY)
def test_the_preservation_branch_stays_under_the_wip_prefix(item, instance) -> None:
    """A `/` in the item must not buy it a second path segment."""
    name = ow.wip_branch(item, instance)
    assert name.startswith(ow.WIP_PREFIX)
    assert name.count("/") == 1


def test_the_branch_name_carries_both_the_item_and_the_dead_instance() -> None:
    assert ow.wip_branch("0007", "alpha") == "wip/0007-alpha"


# ── identity: never record a signal that is already false ───────
def test_a_dead_pid_is_not_recorded() -> None:
    ident = ow.agent_identity(pid=4242, probes=FakeProbes(pids={4242: False}))
    assert ident["pid"] is None and ident["pid_start"] is None


def test_an_absent_mux_session_is_not_recorded() -> None:
    ident = ow.agent_identity(mux_session="gone",
                              probes=FakeProbes(sessions={"gone": False}))
    assert ident["mux_session"] is None


def test_an_unanswerable_probe_is_not_recorded_either() -> None:
    """``None`` is "could not look", and a signal nobody could confirm is not
    evidence. Recording it anyway would make the cascade act on a guess."""
    ident = ow.agent_identity(pid=7, mux_session="s",
                              probes=FakeProbes(pids={7: None},
                                                sessions={"s": None}))
    assert (ident["pid"], ident["pid_start"], ident["mux_session"]) \
        == (None, None, None)


def test_confirmed_signals_are_recorded() -> None:
    ident = ow.agent_identity(pid=11, mux_session="alpha",
                              probes=FakeProbes())
    assert ident == {"boot_id": "boot-1", "mux_session": "alpha", "pid": 11,
                     "pid_start": "start-token"}


def test_a_claim_taken_with_an_unconfirmable_identity_does_not_read_dead(
        db: Path) -> None:
    """The whole reason the probes are asked before the fields are written.

    An agent with no supervisor and no mux session must still be able to hold
    a claim. Written naively, that claim carries a pid that is gone and a
    session that never existed, and the cascade -- correctly, on the evidence
    it was given -- calls its owner dead the moment the command returns.
    """
    probes = FakeProbes(pids={999: False}, sessions={"nowhere": False})
    ow.request(db, item="0007", instance="alpha", pid=999,
               mux_session="nowhere", probes=probes)
    held = wc.claim_for_item(db, "0007")
    assert live.assess(held, probes=probes).verdict == live.LIVE


def test_agent_pid_reports_the_agent_or_nothing_never_this_process(
        monkeypatch) -> None:
    """`_agent_pid` answers about the agent, not about the command.

    ``operator work request`` exits within a second of writing the claim, so
    its own pid is the one value guaranteed to be wrong -- and it is exactly
    what `operator_session.runtime_identity` substitutes when handed ``None``,
    which is why this path does not reuse it. With nothing to point at the
    answer is ``None``, and a claim written from it carries no pid at all.
    """
    instance = op.Instance("alpha")
    monkeypatch.setattr(op.Instance, "copilot_pid", lambda self: None)
    monkeypatch.setattr(op, "_running_loop_pid", lambda inst: None)
    assert op._agent_pid(instance) is None
    monkeypatch.setattr(op.Instance, "copilot_pid", lambda self: 4242)
    monkeypatch.setattr(op, "_pid_alive", lambda pid: True)
    assert op._agent_pid(instance) == 4242


def test_a_dead_copilot_pid_falls_back_to_the_supervisor(monkeypatch) -> None:
    instance = op.Instance("alpha")
    monkeypatch.setattr(op.Instance, "copilot_pid", lambda self: 4242)
    monkeypatch.setattr(op, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(op, "_running_loop_pid", lambda inst: 77)
    assert op._agent_pid(instance) == 77


# ── preservation ────────────────────────────────────────────────
def test_a_clean_worktree_preserves_nothing_and_says_so(repo: Path) -> None:
    result = ow.preserve(repo, item="0007", instance="alpha")
    assert result.dirty is False and result.branch is None
    assert result.notes
    assert "wip/0007-alpha" not in _git(repo, "branch", "--list")


def test_uncommitted_work_reaches_the_wip_branch(repo: Path) -> None:
    _dirty(repo)
    result = ow.preserve(repo, item="0007", instance="alpha")
    assert result.dirty is True
    assert result.branch == "wip/0007-alpha"
    listed = _git(repo, "ls-tree", "-r", "--name-only", result.branch).split()
    assert set(listed) == {"tracked.txt", "untracked.txt"}
    assert _git(repo, "show", f"{result.branch}:tracked.txt") == "modified\n"
    assert _git(repo, "show", f"{result.branch}:untracked.txt") == "brand new\n"


def test_preservation_changes_nothing_in_the_worktree(repo: Path) -> None:
    """Files, index, HEAD and the checked-out branch, all byte-identical.

    The index is in the snapshot because ``git add -A`` is the one command
    here that would ordinarily write to it, and an owner who comes back to a
    silently restaged tree has lost information they had.
    """
    _dirty(repo)
    before = _snapshot(repo)
    ow.preserve(repo, item="0007", instance="alpha")
    assert _snapshot(repo) == before


def test_preservation_issues_no_mutating_verb(repo: Path) -> None:
    _dirty(repo)
    git = RecordingGit()
    ow.preserve(repo, item="0007", instance="alpha", runner=git)
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    assert not forbidden.intersection(git.verbs), git.calls


def test_the_preserved_commit_descends_from_head(repo: Path) -> None:
    _dirty(repo)
    head = _git(repo, "rev-parse", "HEAD").strip()
    result = ow.preserve(repo, item="0007", instance="alpha")
    parents = _git(repo, "rev-list", "--parents", "-n", "1",
                   result.commit).split()
    assert parents[1:] == [head]


def test_an_existing_wip_branch_is_never_moved(repo: Path) -> None:
    """A second crash on the same item is exactly when one already exists."""
    _dirty(repo)
    first = ow.preserve(repo, item="0007", instance="alpha")
    before = _git(repo, "rev-parse", first.branch).strip()
    (repo / "tracked.txt").write_text("modified twice\n", encoding="utf-8")
    second = ow.preserve(repo, item="0007", instance="alpha")
    assert second.branch == "wip/0007-alpha-2"
    assert _git(repo, "rev-parse", first.branch).strip() == before
    assert _git(repo, "show", f"{second.branch}:tracked.txt") \
        == "modified twice\n"


def test_a_third_crash_gets_a_third_name(repo: Path) -> None:
    _dirty(repo)
    names = []
    for text in ("one", "two", "three"):
        (repo / "tracked.txt").write_text(text, encoding="utf-8")
        names.append(ow.preserve(repo, item="0007", instance="alpha").branch)
    assert names == ["wip/0007-alpha", "wip/0007-alpha-2", "wip/0007-alpha-3"]


def test_an_unborn_branch_still_preserves_the_work(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "agent@example.invalid")
    _git(root, "config", "user.name", "Agent")
    (root / "only.txt").write_text("work\n", encoding="utf-8")
    result = ow.preserve(root, item="0007", instance="alpha")
    assert result.dirty is True and result.notes
    assert _git(root, "show", f"{result.branch}:only.txt") == "work\n"
    assert _git(root, "rev-list", "--count", result.branch).strip() == "1"


def test_a_commit_git_refuses_for_want_of_an_identity_is_retried_with_one(
        repo: Path) -> None:
    """A crashed agent's checkout may have no ``user.email``, and git then
    refuses to write a commit at all. Losing an agent's work over a config
    file is not an acceptable outcome, so the second attempt supplies one.

    Driven through a stub rather than an unconfigured repository because the
    ambient case is not reproducible: git invents ``user@hostname`` on most
    machines and succeeds, so a test built on a real repo would pass while
    never reaching the retry.
    """
    seen: list = []

    def runner(args, path, env_extra):
        seen.append((list(args), dict(env_extra or {})))
        if args[0] == "commit-tree":
            if not (env_extra or {}).get("GIT_COMMITTER_EMAIL"):
                raise ow.GitUnavailable(
                    "*** Please tell me who you are. no email was given")
            return "cafe1234\n"
        if args[0] == "branch":
            return ""
        return _real_runner(args, path, env_extra)

    _dirty(repo)
    result = ow.preserve(repo, item="0007", instance="alpha", runner=runner)
    attempts = [env for args, env in seen if args[0] == "commit-tree"]
    assert len(attempts) == 2, "the ambient identity must be tried first"
    assert attempts[0] == {}
    assert attempts[1]["GIT_COMMITTER_EMAIL"] == "operator@localhost"
    assert result.commit == "cafe1234"


def test_a_directory_that_is_not_a_repository_is_an_error(tmp_path: Path) -> None:
    """Not "clean". Reading an unreadable tree as empty is how a reclaim steps
    over the work it exists to protect."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ow.GitUnavailable):
        ow.preserve(plain, item="0007", instance="alpha")


def test_a_missing_worktree_preserves_nothing_without_erroring(
        tmp_path: Path) -> None:
    result = ow.preserve(tmp_path / "gone", item="0007", instance="alpha")
    assert result.dirty is False and result.branch is None and result.notes


def test_ignored_files_are_not_swept_into_the_preservation(repo: Path) -> None:
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "--quiet", "-m", "ignore")
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("junk", encoding="utf-8")
    (repo / "real.txt").write_text("work\n", encoding="utf-8")
    result = ow.preserve(repo, item="0007", instance="alpha")
    listed = _git(repo, "ls-tree", "-r", "--name-only", result.branch).split()
    assert "real.txt" in listed
    assert not [name for name in listed if name.startswith("build/")]


# ── reclaim: the refusals ───────────────────────────────────────
def test_a_live_owner_is_never_reclaimed(db: Path, repo: Path) -> None:
    _claim(db, worktree=repo)
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes())
    assert result.ok is False and result.refused == ow.OWNER_LIVE
    assert wc.claim_for_item(db, "0007").instance == "alpha"
    assert "wip/" not in _git(repo, "branch", "--list")


def test_a_stale_owner_is_reported_and_never_taken(db: Path, repo: Path) -> None:
    """STALE means the cascade could not establish anything. Guessing here is
    how two agents end up in one tree."""
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=3)).strftime(
        wc.TS_FORMAT)
    _claim(db, worktree=repo, when=old)
    probes = FakeProbes(pids={1000: None}, sessions={"alpha": None})
    result = ow.reclaim(db, item="0007", to_instance="beta", probes=probes)
    assert result.refused == ow.OWNER_STALE
    assert result.liveness.verdict == live.STALE
    assert wc.claim_for_item(db, "0007").instance == "alpha"


def test_an_unclaimed_item_is_refused(db: Path) -> None:
    result = ow.reclaim(db, item="nobody", to_instance="beta",
                        probes=FakeProbes())
    assert result.refused == ow.NO_SUCH_CLAIM and result.ok is False


def test_reclaiming_your_own_item_is_refused_as_already_yours(db: Path) -> None:
    _claim(db, instance="beta")
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes())
    assert result.refused == ow.ALREADY_MINE


def test_an_instance_holding_another_item_is_refused(db: Path) -> None:
    """Spec D6: one work item per agent."""
    _claim(db, item="0007", instance="alpha")
    _claim(db, item="0008", instance="beta")
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.refused == ow.INSTANCE_BUSY
    assert wc.claim_for_item(db, "0007").instance == "alpha"


def test_a_worktree_that_cannot_be_read_refuses_the_reclaim(
        db: Path, tmp_path: Path) -> None:
    """The claim stays where it was. Handing somebody a tree whose state
    nobody could establish is how one of the forbidden verbs gets typed."""
    plain = tmp_path / "notarepo"
    plain.mkdir()
    _claim(db, worktree=plain)
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.refused == ow.PRESERVE_FAILED
    assert result.ok is False
    assert result.reassigned_without_preserving is False
    assert wc.claim_for_item(db, "0007").instance == "alpha"


# ── reclaim: the success path ───────────────────────────────────
def test_a_dead_owners_item_moves_and_their_work_is_preserved(
        db: Path, repo: Path) -> None:
    _dirty(repo)
    _claim(db, worktree=repo, branch="feat/x")
    before = _snapshot(repo)
    git = RecordingGit()
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}), runner=git)
    assert result.ok is True
    assert result.previous.instance == "alpha"
    assert result.preservation.branch == "wip/0007-alpha"
    assert result.reassigned_without_preserving is False
    moved = wc.claim_for_item(db, "0007")
    assert moved.instance == "beta"
    assert (moved.worktree, moved.branch) == (str(repo), "feat/x")
    assert _snapshot(repo) == before
    forbidden = {"stash", "reset", "clean", "checkout", "restore"}
    assert not forbidden.intersection(git.verbs), git.calls


def test_a_clean_dead_worktree_is_reassigned_as_is(db: Path, repo: Path) -> None:
    _claim(db, worktree=repo)
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.ok is True
    assert result.preservation.dirty is False
    assert result.preservation.branch is None
    assert wc.claim_for_item(db, "0007").instance == "beta"


def test_a_reclaimed_claim_records_the_new_owners_identity(
        db: Path, repo: Path) -> None:
    _claim(db, worktree=repo)
    ow.reclaim(db, item="0007", to_instance="beta", pid=55,
               mux_session="beta", probes=FakeProbes(pids={1000: False}))
    moved = wc.claim_for_item(db, "0007")
    assert (moved.pid, moved.mux_session) == (55, "beta")
    assert moved.instance == "beta"


def test_a_claim_with_no_worktree_is_reassigned_with_a_note(db: Path) -> None:
    _claim(db, worktree=None)
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.ok is True and result.preservation.notes
    assert result.reassigned_without_preserving is False


def test_the_dead_owner_is_judged_before_anything_is_written(
        db: Path, repo: Path) -> None:
    """Ordering, from the outside: a live owner leaves no git trace at all."""
    _claim(db, worktree=repo)
    _dirty(repo)
    git = RecordingGit()
    ow.reclaim(db, item="0007", to_instance="beta", probes=FakeProbes(),
               runner=git)
    assert git.calls == []


def test_an_owner_that_stirs_between_the_verdict_and_the_write_keeps_its_item(
        db: Path, repo: Path) -> None:
    """The verdict is computed from a row read some milliseconds earlier, and
    the owner's *name* does not change when it comes back. So a compare-and-
    swap on the name alone still fires at an owner that has, in the meantime,
    published fresh evidence of being alive -- which is the one outcome the
    whole cascade exists to prevent.

    The refresh is injected during preservation because that is the widest
    part of the window: it runs several git commands in a worktree that may
    be large.
    """
    _claim(db, worktree=repo)
    _dirty(repo)
    import operator_work as module
    real = module.preserve

    def stirring_preserve(*args, **kwargs):
        result = real(*args, **kwargs)
        wc.claim(db, item="0007", instance="alpha", worktree=str(repo),
                 pid=1000, boot_id="boot-2")
        return result

    module.preserve = stirring_preserve
    try:
        result = ow.reclaim(db, item="0007", to_instance="beta",
                            probes=FakeProbes(pids={1000: False}))
    finally:
        module.preserve = real
    assert result.ok is False
    assert result.refused == ow.RACED
    assert wc.claim_for_item(db, "0007").instance == "alpha"
    assert result.preservation is not None and result.preservation.dirty


def test_a_same_second_heartbeat_that_changes_no_value_still_refuses(
        db: Path, repo: Path) -> None:
    """The row-comparison CAS on its own is not enough, and this is the case
    that proves it: a heartbeat written inside the same whole second as the
    stored one leaves every column byte-identical, because `TS_FORMAT` has no
    sub-second field. A comparison of values reads "nothing happened" at the
    exact moment the owner published that it is alive. `Claim.revision` is
    what makes that write visible.
    """
    _claim(db, worktree=repo)
    _dirty(repo)
    stored = wc.claim_for_item(db, "0007")
    import operator_work as module
    real = module.preserve

    def stirring_preserve(*args, **kwargs):
        result = real(*args, **kwargs)
        assert wc.heartbeat(db, item="0007", instance="alpha",
                            now=stored.heartbeat_at) is True
        return result

    module.preserve = stirring_preserve
    try:
        result = ow.reclaim(db, item="0007", to_instance="beta",
                            probes=FakeProbes(pids={1000: False}))
    finally:
        module.preserve = real
    refreshed = wc.claim_for_item(db, "0007")
    assert refreshed.heartbeat_at == stored.heartbeat_at, (
        "the injected refresh must leave the timestamp identical, or this "
        "test proves nothing the previous one did not")
    assert refreshed.revision == stored.revision + 1
    assert result.ok is False and result.refused == ow.RACED
    assert refreshed.instance == "alpha"


def test_the_preserved_branch_survives_a_refused_reassign(
        db: Path, repo: Path) -> None:
    """A race loses the reclaim, not the work. The branch written before the
    refusal is left in place: the owner it was taken from is the one who gets
    it back, and deleting it would be the only destructive act in the file."""
    _claim(db, worktree=repo)
    _dirty(repo)
    import operator_work as module
    real = module.preserve

    def stirring_preserve(*args, **kwargs):
        result = real(*args, **kwargs)
        wc.claim(db, item="0007", instance="alpha", worktree=str(repo),
                 pid=1000, boot_id="boot-2")
        return result

    module.preserve = stirring_preserve
    try:
        result = ow.reclaim(db, item="0007", to_instance="beta",
                            probes=FakeProbes(pids={1000: False}))
    finally:
        module.preserve = real
    assert result.refused == ow.RACED
    assert "wip/0007-alpha" in _git(repo, "branch", "--list")


def test_a_worktree_recorded_in_another_platforms_syntax_refuses(
        db: Path) -> None:
    """`Path(r"C:\\repos\\app")` on POSIX is a *relative* path, so a presence
    probe reports it absent, preservation concludes there is nothing to save,
    and the reclaim reassigns a worktree it never looked at. The refusal is
    the only safe answer: nobody here can read that tree.
    """
    foreign = ("C:\\repos\\app" if os.name != "nt" else "/home/dev/app")
    with pytest.raises(ow.GitUnavailable):
        ow.preserve(foreign, item="0007", instance="alpha")
    _claim(db, worktree=foreign)
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.ok is False
    assert result.refused == ow.PRESERVE_FAILED
    assert wc.claim_for_item(db, "0007").instance == "alpha"


def test_a_native_path_is_not_mistaken_for_a_foreign_one(repo: Path) -> None:
    """The negative control. A refusal that fires on every path would pass the
    test above while making reclaim useless."""
    assert ow._foreign_path(str(repo)) is False
    assert ow._foreign_path(repo) is False


@pytest.mark.parametrize("windows,path,foreign", [
    (False, "C:\\repos\\app", True),
    (False, "\\\\server\\share\\app", True),
    (False, "/home/dev/app", False),
    (False, "relative/dir", False),
    (True, "/home/dev/app", True),
    (True, "//home/dev/app", True),
    (True, "C:\\repos\\app", False),
    (True, "C:/repos/app", False),
    (True, "\\\\server\\share\\app", False),
    (True, "relative\\dir", False),
])
def test_foreign_paths_are_judged_in_both_syntaxes_on_any_platform(
        windows, path, foreign) -> None:
    """Both branches, on every CI leg.

    Left to read ``os.name``, each leg exercises only its own half, and the
    other half's verdict is decided by whichever platform happens to run the
    suite -- with the POSIX half mattering most, since that is where a Windows
    path reads as an ordinary relative name.

    ``//home/dev/app`` is in the table because ``ntpath.splitdrive`` reads it
    as the UNC share ``//home``: a drive test alone calls that POSIX path
    native and sends Windows looking for ``\\\\home\\dev``.
    """
    assert ow._foreign_path(path, windows=windows) is foreign


def test_a_worktree_from_another_kind_of_system_refuses_before_any_guessing(
        db: Path) -> None:
    """The syntax test is a fallback for claims older than the column. What
    settles it is the claim saying which kind of system wrote the path --
    evidence rather than inference, and the shapes overlap enough that
    inference is wrong in one direction or the other.

    The path here is a legal spelling on both kinds of system, so the syntax
    test alone would call it native and reassign a worktree it never read.
    """
    other = "nt" if os.name != "nt" else "posix"
    native_looking = "C:\\srv\\app" if os.name == "nt" else "/srv/agents/app"
    _claim(db, worktree=native_looking)
    with wc.connect(db) as conn:
        conn.execute("UPDATE work_claims SET platform = ? WHERE item = ?",
                     (other, "0007"))
    held = wc.claim_for_item(db, "0007")
    assert ow._foreign_path(held.worktree) is False, (
        "the path must be one the syntax test calls native, or this proves "
        "nothing the previous test did not")
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.ok is False and result.refused == ow.PRESERVE_FAILED
    assert wc.claim_for_item(db, "0007").instance == "alpha"


def test_a_worktree_from_this_kind_of_system_is_reclaimed_normally(
        db: Path, repo: Path) -> None:
    """The negative control. A platform gate that refused every claim would
    pass the test above and make reclaim useless."""
    _claim(db, worktree=repo)
    assert wc.claim_for_item(db, "0007").platform == os.name
    result = ow.reclaim(db, item="0007", to_instance="beta",
                        probes=FakeProbes(pids={1000: False}))
    assert result.ok is True


# ── the module issues no mutating verb, statically ──────────────
def test_no_mutating_git_verb_appears_in_the_module_at_all() -> None:
    """A source scan beside the behavioural tests, because the behavioural
    ones can only cover the paths a test reached. FR-4's promise is about
    every path, including the ones added tomorrow.

    Over the parsed tree rather than the raw text: a text scan has to pick a
    quote style, and the one thing a forbidden verb would not do is arrive in
    the spelling the scan happened to choose. Every string constant in the
    module is checked, wherever it sits, so `'stash'`, a name in a list, and a
    default argument are all caught.
    """
    tree = ast.parse(Path(ow.__file__).read_text(encoding="utf-8"))
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    offenders = sorted({
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in forbidden})
    assert offenders == [], (
        f"{offenders} appears as a string constant in operator_work.py; FR-4 "
        f"forbids these verbs in a departed owner's worktree")


def test_the_verb_scan_would_notice_a_single_quoted_offender() -> None:
    """The positive control. A scan that matches nothing reports a clean tree,
    which reads exactly like success -- and the previous spelling of this test
    matched only double-quoted tokens, so `'stash'` walked straight past it."""
    tree = ast.parse("def f():\n    return _git(['stash', 'list'], root)\n")
    forbidden = {"stash", "reset", "clean", "checkout", "restore", "rm", "mv"}
    found = sorted({
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in forbidden})
    assert found == ["stash"]


# ── listing ─────────────────────────────────────────────────────
def test_listing_pairs_every_claim_with_a_verdict(db: Path) -> None:
    _claim(db, item="0007", instance="alpha")
    _claim(db, item="0008", instance="beta", pid=2000)
    rows = ow.listing(db, probes=FakeProbes(pids={2000: False}))
    verdicts = {held.item: verdict.verdict for held, verdict in rows}
    assert verdicts == {"0007": live.LIVE, "0008": live.DEAD}


def test_listing_filters_by_subproject(db: Path) -> None:
    _claim(db, item="0007", instance="alpha", subproject="api")
    _claim(db, item="0008", instance="beta", subproject="web")
    rows = ow.listing(db, subproject="api", probes=FakeProbes())
    assert [held.item for held, _ in rows] == ["0007"]


# ── request / release / heartbeat ───────────────────────────────
def test_request_refuses_an_item_somebody_else_holds(db: Path) -> None:
    _claim(db, item="0007", instance="alpha")
    with pytest.raises(wc.ClaimRefused) as caught:
        ow.request(db, item="0007", instance="beta", probes=FakeProbes())
    assert caught.value.reason == wc.ITEM_HELD


def test_release_and_heartbeat_are_owner_guarded(db: Path) -> None:
    _claim(db, item="0007", instance="alpha")
    assert ow.heartbeat(db, item="0007", instance="beta") is False
    assert ow.release(db, item="0007", instance="beta") is False
    assert wc.claim_for_item(db, "0007") is not None
    assert ow.release(db, item="0007", instance="alpha") is True
    assert wc.claim_for_item(db, "0007") is None


def test_current_branch_reports_none_for_a_detached_head(repo: Path) -> None:
    assert ow.current_branch(repo) in ("main", "master")
    _git(repo, "checkout", "--quiet", "--detach", "HEAD")
    assert ow.current_branch(repo) is None


def test_current_branch_reports_none_outside_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ow.current_branch(plain) is None


# ── the CLI seam ────────────────────────────────────────────────
@pytest.fixture
def cli(db: Path, monkeypatch):
    """`operator work ...` wired to a temp database and a silent probe set.

    The probes are replaced on :mod:`operator_liveness` itself, which is the
    module object ``operator_work`` resolves ``SystemProbes`` from at call
    time. Without it the real mux probe runs, and the suite's multiplexer
    guard refuses the spawn.
    """
    monkeypatch.setattr(op, "_session_db", lambda cwd: db)
    monkeypatch.setattr(op, "_agent_pid", lambda instance: None)
    monkeypatch.setattr(live, "SystemProbes", lambda *a, **kw: FakeProbes())
    return db


def _run(*args) -> int:
    return op.manage_work(list(args))


def test_bare_work_prints_usage_and_fails(capsys) -> None:
    assert _run() == 1
    assert "operator work request" in capsys.readouterr().err


def test_work_help_prints_usage_and_succeeds(capsys) -> None:
    assert _run("--help") == 0
    assert "operator work reclaim" in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["requst", "RECLAIM", "start", "steal"])
def test_an_unknown_work_verb_is_refused(verb, capsys) -> None:
    assert _run(verb) == 1
    assert "Unknown subcommand" in capsys.readouterr().err


def test_an_unknown_option_is_refused_before_the_database(monkeypatch,
                                                          capsys) -> None:
    def explode(cwd):
        raise AssertionError("the database was reached after a bad option")
    monkeypatch.setattr(op, "_session_db", explode)
    assert _run("list", "--projct", "api") == 1
    assert "Unknown option" in capsys.readouterr().err


@pytest.mark.parametrize("verb", ["request", "release", "heartbeat", "reclaim"])
def test_every_verb_but_list_requires_an_instance(verb, capsys) -> None:
    assert _run(verb, "--item", "0007") == 1
    assert "Missing required: --instance" in capsys.readouterr().err


def test_list_needs_no_instance(cli, capsys) -> None:
    assert _run("list") == 0
    assert "No work items" in capsys.readouterr().out


def test_request_records_the_worktree_it_was_run_in(cli, monkeypatch, repo,
                                                    capsys) -> None:
    monkeypatch.chdir(repo)
    assert _run("request", "--instance", "alpha", "--item", "0007") == 0
    held = wc.claim_for_item(cli, "0007")
    assert Path(held.worktree) == repo
    assert held.branch in ("main", "master")
    assert "0007 claimed by alpha" in capsys.readouterr().out


def test_request_without_an_item_is_refused(cli, capsys) -> None:
    assert _run("request", "--instance", "alpha") == 1
    assert "Missing required: --item" in capsys.readouterr().err


def test_request_reports_a_refusal_as_a_failure(cli, capsys) -> None:
    _claim(cli, item="0007", instance="alpha")
    assert _run("request", "--instance", "beta", "--item", "0007") == 1
    err = capsys.readouterr().err
    assert "Refused" in err and "reclaim" in err


def test_release_defaults_to_whatever_this_instance_holds(cli, capsys) -> None:
    """FR-2's point is that the agent need not know its item's name."""
    _claim(cli, item="0007", instance="alpha")
    assert _run("release", "--instance", "alpha") == 0
    assert wc.claim_for_item(cli, "0007") is None


def test_releasing_with_no_claim_fails_rather_than_reporting_success(
        cli, capsys) -> None:
    assert _run("release", "--instance", "alpha") == 1
    assert "holds no work item" in capsys.readouterr().err


def test_heartbeat_refreshes_only_the_owners_claim(cli, capsys) -> None:
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).strftime(
        wc.TS_FORMAT)
    _claim(cli, item="0007", instance="alpha", when=old)
    assert _run("heartbeat", "--instance", "beta", "--item", "0007") == 1
    assert wc.claim_for_item(cli, "0007").heartbeat_at == old
    assert _run("heartbeat", "--instance", "alpha") == 0
    assert wc.claim_for_item(cli, "0007").heartbeat_at != old


def test_list_json_carries_the_verdict_and_whether_it_is_reclaimable(
        cli, capsys) -> None:
    _claim(cli, item="0007", instance="alpha")
    assert _run("list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["item"] == "0007"
    assert rows[0]["verdict"] == live.LIVE
    assert rows[0]["reclaimable"] is False


def test_reclaim_through_the_cli_moves_the_claim_and_names_the_branch(
        cli, monkeypatch, repo, capsys) -> None:
    _dirty(repo)
    _claim(cli, item="0007", instance="alpha", worktree=repo)
    monkeypatch.setattr(live, "SystemProbes",
                        lambda *a, **kw: FakeProbes(pids={1000: False}))
    assert _run("reclaim", "--instance", "beta", "--item", "0007") == 0
    out = capsys.readouterr().out
    assert "alpha -> beta" in out
    assert "wip/0007-alpha" in out
    assert wc.claim_for_item(cli, "0007").instance == "beta"


def test_reclaim_of_a_live_owner_fails_through_the_cli(cli, repo,
                                                       capsys) -> None:
    _claim(cli, item="0007", instance="alpha", worktree=repo)
    assert _run("reclaim", "--instance", "beta", "--item", "0007") == 1
    assert "Refused" in capsys.readouterr().err
    assert wc.claim_for_item(cli, "0007").instance == "alpha"


def test_reclaim_json_reports_the_refusal_with_a_nonzero_exit(cli, repo,
                                                              capsys) -> None:
    _claim(cli, item="0007", instance="alpha", worktree=repo)
    assert _run("reclaim", "--instance", "beta", "--item", "0007",
                "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["refused"] == ow.OWNER_LIVE
    assert payload["verdict"] == live.LIVE


def test_reclaim_without_an_item_is_refused(cli, capsys) -> None:
    assert _run("reclaim", "--instance", "beta") == 1
    assert "Missing required: --item" in capsys.readouterr().err


def test_work_is_a_reserved_word_and_dispatches(monkeypatch) -> None:
    """Without this the shortcut path reads `work` as an instance name and
    starts a session for it."""
    assert "work" in op.RESERVED_WORDS
    seen = {}

    def stub(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(op, "manage_work", stub)
    assert op._dispatch_command(["work", "list"]) == 0
    assert seen["args"] == ["list"]
