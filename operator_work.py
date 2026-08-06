#!/usr/bin/env python3
"""The policy over the claim store: ``operator work`` (FR-3, FR-4).

:mod:`work_claims` is the table and its atomic writes; it judges nothing.
:mod:`operator_liveness` judges one claim's owner and changes nothing. This
module is where the two meet, and it exists so that the decision to move a
work item away from the instance that holds it is made in exactly one place.

Two things here are load-bearing rather than convenient.

**A claim records only signals that are true at the moment it is written.**
The four identity fields are read by the liveness cascade as evidence, and
each of them can conclude DEAD on its own. So recording a mux session that is
not running, or the pid of the short-lived ``operator`` process that is about
to exit, does not merely lose information -- it manufactures proof that the
owner is gone, and the next sweep hands a live agent's worktree to somebody
else. :func:`agent_identity` therefore probes each signal before it records
it, and a signal that cannot be confirmed is written as ``NULL``, where the
cascade treats it as "no evidence" instead of "evidence of death".

**Reclaim preserves before it reassigns, and it never issues a mutating git
verb.** ``stash``, ``reset``, ``clean``, ``checkout`` and ``restore`` are
absent from this file by construction and a test asserts their absence: the
incident behind FR-4 is a reviewer's ``git stash`` destroying 454 lines that
survived only as dangling objects. Preservation here writes a commit and a
branch and nothing else -- the index is copied to a temp file first, so even
``.git/index`` inside the dead owner's worktree is left byte-for-byte as it
was, and the working tree is never read for anything but ``git add``.
"""
from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# An editable install freezes the module list into its import finder, so a
# module added to this directory after the last `pip install -e .` is invisible
# to the installed entry points even though the file sits right here.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import install_manifest                                        # noqa: E402
import operator_liveness                                       # noqa: E402
import work_claims                                             # noqa: E402
from work_claims import Claim, ClaimRefused                    # noqa: E402

#: Refusal reasons. Each is a different next move for the caller, which is why
#: they are not one string: an owner that is LIVE will never become
#: reclaimable by waiting, a STALE one might, and an unreadable worktree is a
#: question about the filesystem rather than about the agent.
NO_SUCH_CLAIM = "no-such-claim"
OWNER_LIVE = "owner-live"
OWNER_STALE = "owner-stale"
ALREADY_MINE = "already-mine"
INSTANCE_BUSY = "instance-busy"
PRESERVE_FAILED = "preserve-failed"
RACED = "raced"

#: The prefix every preservation branch carries.
WIP_PREFIX = "wip/"

#: Characters a branch name may contain here. A whitelist rather than a
#: blacklist of what ``git check-ref-format`` rejects: the reject list has
#: positional rules (no leading dot, no trailing ``.lock``, no ``..``) that a
#: character filter cannot express, and a name that git refuses turns a
#: preservation into an error at the last step, after the commit exists and
#: with nothing pointing at it.
_REF_SAFE = set("abcdefghijklmnopqrstuvwxyz"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789-_.")


class GitUnavailable(Exception):
    """git could not be run, or refused the question.

    Every git failure collapses to this, and the caller turns it into a
    refusal. The one thing it must never become is "the worktree is clean":
    that reads as "nothing to preserve" and is how a reclaim would step over
    the work it exists to protect.
    """


def _git(args: "list[str]", repo, *, env_extra: "dict | None" = None,
         runner=None) -> str:
    """Run one git command in ``repo`` and return stdout.

    The encoding is named rather than inherited, for the reason
    :func:`git_identity._git` gives at length: ``text=True`` alone decodes
    with the locale's preferred codec, which on Windows is cp1252, and git
    stores commit data as bytes. An undecodable byte there raised inside
    subprocess's reader thread and surfaced as an ``AttributeError`` on a
    ``None`` stdout.
    """
    if runner is not None:
        return runner(args, repo, env_extra)
    env = None
    if env_extra:
        env = dict(os.environ)
        env.update(env_extra)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=120, env=env,
        )
    except FileNotFoundError as exc:
        raise GitUnavailable("git is not installed or not on PATH") from exc
    except OSError as exc:
        raise GitUnavailable(f"git could not be run: {exc}") from exc
    except subprocess.SubprocessError as exc:      # includes TimeoutExpired
        raise GitUnavailable(f"git did not complete: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else f"exit {proc.returncode}"
        raise GitUnavailable(f"`git {' '.join(args)}` failed: {first}")
    if proc.stdout is None:
        raise GitUnavailable(
            f"`git {' '.join(args)}` produced no readable output")
    return proc.stdout


def _foreign_path(raw, *, windows: "bool | None" = None) -> bool:
    """Whether ``raw`` names a path in the *other* platform's syntax.

    A claim records the worktree the way the machine that took it spelled it,
    and nothing converts it. Read the other way round the string is not
    invalid, which is the whole problem: ``Path("C:\\\\repos\\\\app")`` on POSIX
    is a *relative* path -- a backslash is an ordinary filename character
    there -- so a presence probe answers "not there", preservation concludes
    there is nothing to save, and the reclaim reassigns a worktree whose
    uncommitted work it never looked at. That is the exact failure FR-4
    exists to prevent, arrived at through a string.

    ``ntpath`` rather than ``os.path`` for the drive test, because ``os.path``
    *is* the running platform's syntax and so cannot answer a question about
    the other one. A false positive costs a refused reclaim; a false negative
    costs somebody's uncommitted work.

    ``windows`` is a parameter rather than a read of ``os.name`` at the point
    of use so that both branches can be tested on every CI leg. Patching
    ``os.name`` would do it too, and takes ``pathlib`` with it: ``Path()``
    consults the same attribute and raises ``NotImplementedError`` for the
    flavour it cannot instantiate.
    """
    text = str(raw)
    drive = ntpath.splitdrive(text)[0]
    if windows is None:
        windows = os.name == "nt"
    if windows:
        # Rooted with no drive and no UNC share is POSIX syntax. Windows would
        # silently resolve it against whichever drive happens to be current.
        return not drive and text[:1] in ("/", "\\")
    return bool(drive) or "\\" in text


def _ref_component(text: str) -> str:
    """One path segment of a branch name, safe for ``git check-ref-format``.

    Everything outside :data:`_REF_SAFE` becomes ``-`` -- including ``/``, so
    a work item named like a path cannot smuggle a second segment into the
    branch and land the preservation somewhere other than under ``wip/``.
    """
    cleaned = "".join(ch if ch in _REF_SAFE else "-" for ch in text)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip(".-")
    # A loop, not an `if`: stripping one suffix from `foo.lock.lock` leaves a
    # name that still ends in `.lock`, which git refuses at `git branch` --
    # the last step of a preservation, after the commit exists and with
    # nothing pointing at it.
    while cleaned.endswith(".lock"):
        cleaned = cleaned[:-len(".lock")].rstrip(".-")
    if cleaned == "@":                            # git refuses a lone `@`
        cleaned = ""
    return cleaned or "unknown"


def wip_branch(item: str, instance: str) -> str:
    """``wip/{item}-{deadInstance}`` — the name FR-4 gives the preserved work.

    The dead owner's name is in it because the branch is *their* work, and the
    reclaiming agent is the one reading the name: "somebody else's, from
    before" is the whole message it has to carry.
    """
    return f"{WIP_PREFIX}{_ref_component(item)}-{_ref_component(instance)}"


def agent_identity(*, mux_session: "str | None" = None,
                   pid: "int | None" = None, probes=None) -> dict:
    """The four identity fields, with every unconfirmed signal left ``NULL``.

    A recorded signal is evidence the cascade will act on, so an unconfirmed
    one is not a harmless blank: a mux session name written for a session that
    is not running makes step 3 conclude DEAD, and the pid of the ``operator``
    process that wrote the claim is gone before the command returns, which
    makes step 2 conclude DEAD. Either would make the claim reclaimable the
    instant it was taken.

    A claim with neither is judged on boot id and heartbeat alone, which the
    cascade reports as STALE rather than acting on -- the correct answer when
    nothing about the owner could be established.
    """
    probes = probes or operator_liveness.SystemProbes()
    confirmed_pid = None
    pid_start = None
    if pid:
        if probes.process_present(pid):
            confirmed_pid = pid
            pid_start = probes.process_start_token(pid)
    confirmed_session = None
    if mux_session:
        if probes.session_present(mux_session):
            confirmed_session = mux_session
    return {
        "boot_id": probes.boot_identity(),
        "mux_session": confirmed_session,
        "pid": confirmed_pid,
        "pid_start": pid_start,
    }


@dataclass(frozen=True)
class Preservation:
    """What became of a departed owner's uncommitted work.

    ``dirty`` and ``branch`` are separate because "there was nothing to
    preserve" and "it was preserved" are both successes and must not read
    alike in a report somebody will use to go looking for their work.
    """

    dirty: bool = False
    branch: "str | None" = None
    commit: "str | None" = None
    notes: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class ReclaimResult:
    """The outcome of a reclaim, and enough of the evidence to argue with it."""

    item: str
    to_instance: str
    ok: bool = False
    refused: "str | None" = None
    detail: str = ""
    previous: "Claim | None" = None
    claim: "Claim | None" = None
    liveness: "operator_liveness.Liveness | None" = None
    preservation: "Preservation | None" = None

    @property
    def reassigned_without_preserving(self) -> bool:
        """The combination FR-4 exists to make unreachable.

        A claim moved to a new owner while that owner's uncommitted changes
        were neither committed nor established to be absent. Expressed as a
        property so the tests can assert it at every point the call can fail,
        rather than reasoning about the ordering in prose.
        """
        return self.ok and self.preservation is None


def _head_commit(worktree, runner=None) -> "str | None":
    """The worktree's HEAD, or ``None`` on an unborn branch."""
    try:
        return _git(["rev-parse", "--verify", "HEAD"], worktree,
                    runner=runner).strip() or None
    except GitUnavailable:
        return None


def _free_branch_name(worktree, wanted: str, runner=None) -> str:
    """``wanted``, or the first ``-2``, ``-3``… that no ref already uses.

    Never reuses an existing name. A second crash of the same instance on the
    same item is exactly when a preservation branch already exists, and
    updating it in place would drop the first crash's work -- the failure this
    whole requirement is about, arrived at from the other direction.
    """
    candidate = wanted
    suffix = 1
    while True:
        try:
            _git(["rev-parse", "--verify", "--quiet",
                  f"refs/heads/{candidate}"], worktree, runner=runner)
        except GitUnavailable:
            return candidate
        suffix += 1
        candidate = f"{wanted}-{suffix}"


def _commit_tree(worktree, tree: str, parent: "str | None", message: str,
                 runner=None) -> str:
    """``git commit-tree``, falling back to a synthetic identity.

    A crashed agent's checkout may have no ``user.email`` configured, and git
    refuses to write a commit without one. Retrying with an explicit identity
    is right in that case and wrong as a default: the ambient identity is
    whoever was working there, and overwriting it would make the preservation
    look like the tool's own work.
    """
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    try:
        return _git(args, worktree, runner=runner).strip()
    except GitUnavailable:
        env = {"GIT_AUTHOR_NAME": "operator", "GIT_COMMITTER_NAME": "operator",
               "GIT_AUTHOR_EMAIL": "operator@localhost",
               "GIT_COMMITTER_EMAIL": "operator@localhost"}
        return _git(args, worktree, env_extra=env, runner=runner).strip()


def preserve(worktree, *, item: str, instance: str, runner=None) -> Preservation:
    """Commit a departed owner's uncommitted work to ``wip/{item}-{instance}``.

    Raises :class:`GitUnavailable` when it cannot establish what is there. A
    reclaim refuses on that exception rather than proceeding, because the two
    unknowns are not symmetric: reassigning a tree whose state could not be
    read hands somebody an unexplained diff, and the next thing they will
    reach for is one of the verbs this module never issues.

    The index is copied to a temp file and ``GIT_INDEX_FILE`` points ``git
    add`` at the copy, so the owner's own staged/unstaged split survives
    exactly as they left it. Nothing here writes to the working tree, and
    ``HEAD`` does not move: the branch is created with ``git branch`` at a
    commit built by ``commit-tree``, which is the whole reason for going the
    long way round instead of committing normally.
    """
    if _foreign_path(worktree):
        raise GitUnavailable(
            f"worktree {worktree!r} was recorded in another platform's path "
            f"syntax and cannot be examined from here")
    root = Path(worktree)
    # `dir_present` rather than `is_dir`, which answers False both for a path
    # that is not there and for one that could not be examined. Only the first
    # means there is nothing to preserve; the second is a question about the
    # filesystem, and answering it with a no-op is how a reclaim would step
    # over the work it exists to protect. So None falls through to git, which
    # fails and refuses the reclaim.
    if install_manifest.dir_present(root) is False:
        return Preservation(notes=(f"worktree {worktree} is not a directory; "
                                   f"nothing to preserve",))
    status = _git(["status", "--porcelain"], root, runner=runner)
    if not status.strip():
        return Preservation(notes=("worktree is clean",))

    index = _git(["rev-parse", "--git-path", "index"], root,
                 runner=runner).strip()
    index_path = Path(index)
    if not index_path.is_absolute():
        index_path = root / index_path

    handle, temp_index = tempfile.mkstemp(prefix="operator-index-")
    os.close(handle)
    try:
        try:
            shutil.copyfile(index_path, temp_index)
        except (FileNotFoundError, NotADirectoryError):
            # A repository with no index yet. An empty file is not a valid
            # index, so the copy is skipped and git writes a fresh one.
            os.unlink(temp_index)
        except OSError as exc:
            # The index is there but unreadable. Proceeding would build a tree
            # from an empty index and preserve a deletion of everything the
            # owner had staged, which is worse than refusing.
            raise GitUnavailable(
                f"could not read the index at {index_path}: {exc}") from exc
        env = {"GIT_INDEX_FILE": temp_index}
        _git(["add", "-A"], root, env_extra=env, runner=runner)
        tree = _git(["write-tree"], root, env_extra=env, runner=runner).strip()
    finally:
        try:
            os.unlink(temp_index)
        except OSError:
            pass

    parent = _head_commit(root, runner=runner)
    message = (f"wip: preserve {instance}'s uncommitted work on {item}\n\n"
               f"Committed by `operator work reclaim` before the item was "
               f"reassigned. The working tree was not modified.\n")
    commit = _commit_tree(root, tree, parent, message, runner=runner)
    branch = _free_branch_name(root, wip_branch(item, instance), runner=runner)
    _git(["branch", branch, commit], root, runner=runner)
    notes = () if parent else ("preserved onto an unborn branch: the commit "
                               "has no parent",)
    return Preservation(dirty=True, branch=branch, commit=commit, notes=notes)


def current_branch(path, runner=None) -> "str | None":
    """The branch checked out at ``path``, or ``None``.

    A detached HEAD answers ``None`` rather than the literal ``"HEAD"`` git
    prints for it: the field records *which branch this work is on*, and a
    claim saying ``HEAD`` would send whoever reads it looking for a branch by
    that name.
    """
    try:
        name = _git(["rev-parse", "--abbrev-ref", "HEAD"], path,
                    runner=runner).strip()
    except GitUnavailable:
        return None
    return None if not name or name == "HEAD" else name


def request(path, *, item: str, instance: str, subproject: str = "",
            worktree=None, branch=None, mux_session=None, pid=None,
            probes=None, now=None) -> Claim:
    """Take ``item`` for ``instance``, recording confirmed identity only.

    Raises :class:`work_claims.ClaimRefused` when the item is held by somebody
    else or this instance already holds another. Neither is overridden here:
    the first is what ``reclaim`` is for, and the second is spec D6.
    """
    identity = agent_identity(mux_session=mux_session, pid=pid, probes=probes)
    return work_claims.claim(path, item=item, instance=instance,
                             subproject=subproject, worktree=worktree,
                             branch=branch, now=now, **identity)


def release(path, *, item: str, instance: str) -> bool:
    """Give an item up. False when ``instance`` is not its owner."""
    return work_claims.release(path, item=item, instance=instance)


def heartbeat(path, *, item: str, instance: str, now=None) -> bool:
    """Refresh a claim's heartbeat. False when ``instance`` is not its owner."""
    return work_claims.heartbeat(path, item=item, instance=instance, now=now)


def listing(path, *, subproject: "str | None" = None, probes=None, now=None,
            stale_after: float = operator_liveness.DEFAULT_STALE_AFTER,
            ) -> "list[tuple[Claim, operator_liveness.Liveness]]":
    """Every claim with its owner's verdict, oldest first.

    The verdict is computed here rather than left to the caller because a
    claim listed without one invites the reader to judge it by its heartbeat,
    which is the single signal the cascade refuses to act on alone.
    """
    probes = probes or operator_liveness.SystemProbes()
    rows = []
    for held in work_claims.claims(path):
        if subproject is not None and held.subproject != subproject:
            continue
        rows.append((held, operator_liveness.assess(
            held, probes=probes, now=now, stale_after=stale_after)))
    return rows


def reclaim(path, *, item: str, to_instance: str, probes=None, now=None,
            stale_after: float = operator_liveness.DEFAULT_STALE_AFTER,
            mux_session=None, pid=None, runner=None) -> ReclaimResult:
    """Take an item from an owner that is provably gone (FR-3, FR-4).

    The order is the guarantee, and it is one-way:

    1. the owner is judged, and only :data:`operator_liveness.DEAD` proceeds --
       LIVE never becomes reclaimable by waiting and STALE means the cascade
       could not establish anything, which is a report for a person;
    2. their uncommitted work is committed to a ``wip/`` branch, or the tree
       is established to be clean;
    3. only then is the claim moved, as a compare-and-swap against the owner
       that was judged, so a claim that changed hands in between refuses
       rather than overwrites.

    A failure at any step leaves the claim where it was. There is no ordering
    in which the item is reassigned and the previous owner's changes were
    neither committed nor shown to be absent.
    """
    held = work_claims.claim_for_item(path, item)
    if held is None:
        return ReclaimResult(item=item, to_instance=to_instance,
                             refused=NO_SUCH_CLAIM,
                             detail=f"no claim on {item!r}")
    if held.instance == to_instance:
        return ReclaimResult(item=item, to_instance=to_instance, previous=held,
                             refused=ALREADY_MINE,
                             detail=f"{item!r} is already held by "
                                    f"{to_instance!r}")
    busy = work_claims.claim_for_instance(path, to_instance)
    if busy is not None and busy.item != item:
        return ReclaimResult(item=item, to_instance=to_instance, previous=held,
                             refused=INSTANCE_BUSY,
                             detail=f"{to_instance!r} already holds "
                                    f"{busy.item!r}; release it first")

    verdict = operator_liveness.assess(held, probes=probes, now=now,
                                       stale_after=stale_after)
    if verdict.verdict != operator_liveness.DEAD:
        refusal = (OWNER_STALE if verdict.verdict == operator_liveness.STALE
                   else OWNER_LIVE)
        return ReclaimResult(item=item, to_instance=to_instance, previous=held,
                             liveness=verdict, refused=refusal,
                             detail=f"{held.instance!r} is {verdict.verdict}: "
                                    f"{verdict.reason}")

    if held.worktree:
        try:
            preserved = preserve(held.worktree, item=item,
                                 instance=held.instance, runner=runner)
        except GitUnavailable as exc:
            return ReclaimResult(item=item, to_instance=to_instance,
                                 previous=held, liveness=verdict,
                                 refused=PRESERVE_FAILED,
                                 detail=f"{held.worktree}: {exc}")
    else:
        preserved = Preservation(notes=("no worktree recorded on the claim",))

    identity = agent_identity(mux_session=mux_session, pid=pid, probes=probes)
    try:
        moved = work_claims.reassign(path, item=item,
                                     expect_owner=held.instance,
                                     expect_claim=held,
                                     to_instance=to_instance,
                                     worktree=held.worktree,
                                     branch=held.branch, now=None, **identity)
    except ClaimRefused as exc:
        return ReclaimResult(item=item, to_instance=to_instance, previous=held,
                             liveness=verdict, preservation=preserved,
                             refused=RACED, detail=str(exc))
    return ReclaimResult(item=item, to_instance=to_instance, ok=True,
                         previous=held, claim=moved, liveness=verdict,
                         preservation=preserved)


__all__ = [
    "ALREADY_MINE",
    "GitUnavailable",
    "INSTANCE_BUSY",
    "NO_SUCH_CLAIM",
    "OWNER_LIVE",
    "OWNER_STALE",
    "PRESERVE_FAILED",
    "Preservation",
    "RACED",
    "ReclaimResult",
    "WIP_PREFIX",
    "agent_identity",
    "current_branch",
    "heartbeat",
    "listing",
    "preserve",
    "reclaim",
    "release",
    "request",
    "wip_branch",
]
