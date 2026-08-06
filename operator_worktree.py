#!/usr/bin/env python3
"""The worktree side of a work item: ``operator worktree`` (E4).

A worktree is 1:1 with a work item, so the two are created and retired
together or they drift: a checkout nobody holds is indistinguishable from one
whose owner crashed, and a claim naming a directory that is not there sends
the next agent looking for work that was never written.

Three verbs, and the asymmetry between them is the design:

``new``
    Claim the item and create the checkout in one call, so neither can exist
    without the other. The claim is taken *first* and released again if the
    checkout cannot be made, because the two failure directions are not
    symmetric: a claim held for the half-second it takes git to refuse is
    undone by this command itself, while a directory created under a claim
    that was refused is left for a person to identify.

``finish``
    Retire both. It refuses a dirty tree rather than tidying it, and deletes
    the branch only when the integration ref already contains it -- ``git
    branch -d``, never ``-D``, so git's own refusal is the backstop under
    ours. Removing a checkout whose commits are merged loses nothing;
    everything else here is arranged so that is the only case where anything
    is deleted at all.

``recover``
    Reports and preserves; it removes nothing, ever. Its subject is the state
    the other two cannot produce -- a checkout with no claim, a claim with no
    checkout, an owner the liveness cascade calls dead -- which is exactly the
    state a crash leaves behind, and the moment when the wrong verb costs
    somebody a day's uncommitted work.

The mutating git verbs FR-4 forbids (``stash``, ``reset``, ``clean``,
``checkout``, ``restore``, ``rm``, ``mv``) are absent from this module by
construction, and ``tests/test_worktree_cli.py`` asserts their absence over
the parsed source rather than only over the paths its own cases reach.
"""
from __future__ import annotations

import os
import sys
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
import operator_work                                           # noqa: E402
import work_claims                                             # noqa: E402
from operator_work import GitUnavailable                        # noqa: E402
from work_claims import Claim, ClaimRefused                     # noqa: E402

#: Refusal reasons. Separate strings because each names a different next move:
#: a path that already exists is a question for a person, a dirty tree is a
#: commit the owner has not made, and an unmerged branch is a merge nobody has
#: done. Collapsing them into one message would make all three read as "no".
PATH_EXISTS = "path-exists"
PATH_UNREADABLE = "path-unreadable"
NOT_OWNER = "not-owner"
NO_WORKTREE = "no-worktree"
FOREIGN_PLATFORM = "foreign-platform"
WORKTREE_DIRTY = "worktree-dirty"
INSIDE_TARGET = "inside-target"
GIT_FAILED = "git-failed"

#: The prefix a derived branch carries. Deliberately not one of the
#: conventional `feat/` `fix/` `docs/` prefixes: choosing between those from a
#: work-item reference is a guess about intent, and a wrong guess ships in the
#: branch name. ``--branch`` is how the conventional spelling is asked for.
WORK_PREFIX = "work/"

#: Where checkouts live, relative to the primary checkout.
WORKTREES_DIR = ".worktrees"

#: The ref ``finish`` measures "merged" against unless told otherwise.
DEFAULT_INTEGRATION = "main"

#: How a worktree registration is classified by :func:`survey`.
PRIMARY = "primary"
LIVE = "live"
DEAD = "dead"
STALE = "stale"
UNCLAIMED = "unclaimed"
MISSING = "missing"
UNREGISTERED = "unregistered"


def slug(branch: str) -> str:
    """The directory name for ``branch`` — ``feat/login`` → ``feat-login``.

    Both separators are replaced, not just ``/``: a branch name may not
    contain a backslash on either platform, so a backslash reaching here came
    from somewhere other than git and must not be allowed to buy a second path
    segment on the one platform that reads it as one.
    """
    return branch.replace("/", "-").replace("\\", "-") or "worktree"


def derived_branch(item: str) -> str:
    """``work/{item}`` — the branch ``new`` makes when none was named."""
    return f"{WORK_PREFIX}{operator_work._ref_component(item)}"


def default_path(root, branch: str) -> Path:
    """``<primary checkout>/.worktrees/<slug>``, the layout AGENTS.md states."""
    return Path(root) / WORKTREES_DIR / slug(branch)


def _same_path(left, right) -> bool:
    """Whether two path strings from *this* machine name the same place.

    ``os.path`` is the running platform's syntax, which is correct here and
    only here: every caller has already established that the claim was written
    on this kind of system, either by reading the ``platform`` column or by
    having just written the path itself. ``normcase`` is what makes the
    comparison right on Windows, where two spellings differing only in case
    are one directory.
    """
    return (os.path.normcase(os.path.abspath(str(left)))
            == os.path.normcase(os.path.abspath(str(right))))


def _is_inside(child, parent) -> bool:
    """Whether ``child`` is ``parent`` or sits beneath it.

    Compared component-wise after normalisation rather than with
    ``startswith``, which answers True for ``/repo/.worktrees/feat-a2`` under
    ``/repo/.worktrees/feat-a`` and would refuse a legitimate removal, or
    worse, permit one it meant to refuse.
    """
    child_parts = Path(os.path.normcase(os.path.abspath(str(child)))).parts
    parent_parts = Path(os.path.normcase(os.path.abspath(str(parent)))).parts
    return child_parts[:len(parent_parts)] == parent_parts


@dataclass(frozen=True)
class WorktreeResult:
    """What a verb did, and enough of the evidence to argue with it."""

    verb: str
    ok: bool = False
    refused: "str | None" = None
    detail: str = ""
    item: str = ""
    instance: str = ""
    path: "str | None" = None
    branch: "str | None" = None
    claim: "Claim | None" = None
    branch_deleted: bool = False
    notes: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class Registration:
    """One record of ``git worktree list --porcelain``, plus what holds it."""

    path: str
    branch: "str | None" = None
    head: "str | None" = None
    detached: bool = False
    prunable: "str | None" = None
    state: str = UNCLAIMED
    claim: "Claim | None" = None
    liveness: "operator_liveness.Liveness | None" = None
    preserved: "operator_work.Preservation | None" = None
    note: str = ""


def parse_worktree_list(text: str) -> "list[Registration]":
    """The porcelain worktree list, in the order git printed it.

    Order is load-bearing rather than incidental: the first record is always
    the primary checkout, from anywhere in the repository, and that is the
    only reliable way to tell it from a linked worktree. Every other route --
    comparing against ``rev-parse --show-toplevel``, looking for ``.git`` as a
    directory rather than a file -- answers a different question in at least
    one arrangement this repository actually uses.
    """
    records: "list[Registration]" = []
    current: dict = {}

    def flush() -> None:
        if current.get("path"):
            records.append(Registration(
                path=current["path"], branch=current.get("branch"),
                head=current.get("head"), detached=current.get("detached", False),
                prunable=current.get("prunable")))
        current.clear()

    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            flush()
            current["path"] = value.strip()
        elif key == "branch":
            ref = value.strip()
            current["branch"] = (ref[len("refs/heads/"):]
                                 if ref.startswith("refs/heads/") else ref)
        elif key == "HEAD":
            current["head"] = value.strip()
        elif key == "detached":
            current["detached"] = True
        elif key == "prunable":
            current["prunable"] = value.strip() or "prunable"
    flush()
    return records


def registrations(root, runner=None) -> "list[Registration]":
    """Every worktree git knows about, primary checkout first."""
    return parse_worktree_list(
        operator_work._git(["worktree", "list", "--porcelain"], root,
                           runner=runner))


def _claimed_platform_ok(held: Claim) -> bool:
    """Whether ``held``'s recorded worktree can be examined from this machine.

    The recorded ``platform`` is evidence and the path's shape is a fallback,
    in that order, for the reason ``operator_work.reclaim`` gives: the shapes
    overlap, so a guess is wrong in whichever direction the guesser did not
    choose, and one of those directions reports a live checkout as absent.
    """
    if held.platform:
        return held.platform == os.name
    return not operator_work._foreign_path(held.worktree)


def new(db, root, *, item: str, instance: str, subproject: str = "",
        branch: "str | None" = None, path=None, mux_session=None, pid=None,
        probes=None, runner=None) -> WorktreeResult:
    """Claim ``item`` and create its checkout, or leave nothing behind.

    The order is the guarantee. The claim is taken *first*, because it is the
    only step with a compare-and-swap behind it: checking "is this free" and
    then creating a directory before writing the claim lets two agents both
    pass the check and both make a tree. Everything after it -- the target
    path, the branch, git itself -- can only refuse, and every one of those
    refusals releases the claim this call has just taken.

    That compensating release is safe precisely because it is this call's own
    claim, taken microseconds earlier: no agent can have done work under it,
    which is the property that makes releasing somebody else's claim
    unacceptable and this one routine.

    It also settles the refusal an agent is most likely to hit. Two calls for
    the same item derive the same path, so an ordering that probed the
    filesystem first would answer "that directory exists" when the fact worth
    reporting is that somebody else holds the item -- and the two have
    different next moves, `operator work list` and `reclaim` against one and a
    person against the other.
    """
    branch = branch or derived_branch(item)
    target = Path(path) if path else default_path(root, branch)

    try:
        held = operator_work.request(
            db, item=item, instance=instance, subproject=subproject,
            worktree=str(target), branch=branch, mux_session=mux_session,
            pid=pid, probes=probes)
    except ClaimRefused as exc:
        return WorktreeResult(verb="new", refused=exc.reason, item=item,
                              instance=instance, path=str(target),
                              branch=branch, detail=str(exc))

    def undo(refused: str, detail: str, note: str = "") -> WorktreeResult:
        operator_work.release(db, item=item, instance=instance)
        return WorktreeResult(
            verb="new", refused=refused, item=item, instance=instance,
            path=str(target), branch=branch, detail=detail,
            notes=(("the claim taken for this call was released again",)
                   + ((note,) if note else ())))

    present = install_manifest.path_present(target)
    if present is True:
        return undo(PATH_EXISTS, f"{target} already exists")
    if present is None:
        # Not "absent". A path that cannot be examined is a question about the
        # filesystem, and answering it with "go ahead" is how `worktree add`
        # gets pointed at somebody's existing checkout.
        return undo(PATH_UNREADABLE, f"{target} could not be examined")

    try:
        existing = _branch_exists(root, branch, runner=runner)
        args = ["worktree", "add"]
        if existing:
            args += [str(target), branch]
        else:
            args += ["-b", branch, str(target)]
        operator_work._git(args, root, runner=runner)
    except GitUnavailable as exc:
        return undo(GIT_FAILED, str(exc))
    note = ((f"branch {branch} already existed and was checked out",)
            if existing else ())
    return WorktreeResult(verb="new", ok=True, item=item, instance=instance,
                          path=str(target), branch=branch, claim=held,
                          notes=note)


def _branch_exists(root, branch: str, runner=None) -> bool:
    try:
        operator_work._git(["rev-parse", "--verify", "--quiet",
                            f"refs/heads/{branch}"], root, runner=runner)
    except GitUnavailable:
        return False
    return True


def _merged_into(root, branch: str, into: str, runner=None) -> bool:
    """Whether ``into`` already contains every commit on ``branch``.

    A missing ``into`` answers False rather than raising: "I could not
    establish that these commits are safe elsewhere" and "they are not" call
    for the same action, which is to keep the branch.
    """
    try:
        operator_work._git(["merge-base", "--is-ancestor", branch, into],
                           root, runner=runner)
    except GitUnavailable:
        return False
    return True


def finish(db, root, *, item: str, instance: str, into: str = DEFAULT_INTEGRATION,
           cwd=None, runner=None) -> WorktreeResult:
    """Retire a work item's checkout and the claim on it.

    Refuses a dirty tree instead of tidying it. There is no ``--force`` and
    that is the point: the one thing this command must never be is a faster
    way to lose uncommitted work, and every mechanism that would make it one
    is a mutating git verb this module does not contain.

    The branch is deleted only when ``into`` already contains it, with ``git
    branch -d``. Two independent refusals therefore stand between an unmerged
    branch and deletion -- the ancestry check here and git's own under it --
    because this is the only irreversible thing any of these three verbs do.

    The claim is released last. A failure anywhere above leaves it held, which
    is the recoverable direction: an item still claimed by an agent that has
    finished is released by that agent's next call or judged by the liveness
    cascade, while an item released with its checkout still on disk is a tree
    with no owner and no record of who was in it.
    """
    held = work_claims.claim_for_item(db, item)
    if held is None or held.instance != instance:
        return WorktreeResult(verb="finish", refused=NOT_OWNER, item=item,
                              instance=instance,
                              detail=(f"{item!r} is not held by {instance!r}"
                                      if held is None else
                                      f"{item!r} is held by "
                                      f"{held.instance!r}, not {instance!r}"))
    if not held.worktree:
        return WorktreeResult(verb="finish", refused=NO_WORKTREE, item=item,
                              instance=instance, claim=held,
                              detail=f"no worktree is recorded on {item!r}; "
                                     f"use `operator work release`")
    if not _claimed_platform_ok(held):
        return WorktreeResult(verb="finish", refused=FOREIGN_PLATFORM,
                              item=item, instance=instance, claim=held,
                              path=held.worktree,
                              detail=f"{held.worktree} was recorded on a "
                                     f"{held.platform or 'foreign'} system "
                                     f"and cannot be examined from a "
                                     f"{os.name} one")

    here = Path(cwd) if cwd is not None else Path.cwd()
    if _is_inside(here, held.worktree):
        return WorktreeResult(verb="finish", refused=INSIDE_TARGET, item=item,
                              instance=instance, claim=held,
                              path=held.worktree, branch=held.branch,
                              detail=f"the current directory is inside "
                                     f"{held.worktree}; change out of it "
                                     f"first")

    present = install_manifest.dir_present(Path(held.worktree))
    if present is True:
        try:
            status = operator_work._git(["status", "--porcelain"],
                                        held.worktree, runner=runner)
        except GitUnavailable as exc:
            return WorktreeResult(verb="finish", refused=GIT_FAILED, item=item,
                                  instance=instance, claim=held,
                                  path=held.worktree, branch=held.branch,
                                  detail=str(exc))
        if status.strip():
            return WorktreeResult(verb="finish", refused=WORKTREE_DIRTY,
                                  item=item, instance=instance, claim=held,
                                  path=held.worktree, branch=held.branch,
                                  detail=f"{held.worktree} has uncommitted "
                                         f"changes; commit them first")
        try:
            operator_work._git(["worktree", "remove", str(held.worktree)],
                               root, runner=runner)
        except GitUnavailable as exc:
            return WorktreeResult(verb="finish", refused=GIT_FAILED, item=item,
                                  instance=instance, claim=held,
                                  path=held.worktree, branch=held.branch,
                                  detail=str(exc))
        removed_note = ()
    elif present is False:
        # The directory is already gone. Pruning the registration is the
        # remaining half of a removal somebody started, and it deletes no
        # content -- but only on evidence of absence, never on a probe that
        # could not answer.
        try:
            operator_work._git(["worktree", "prune"], root, runner=runner)
        except GitUnavailable as exc:
            return WorktreeResult(verb="finish", refused=GIT_FAILED, item=item,
                                  instance=instance, claim=held,
                                  path=held.worktree, branch=held.branch,
                                  detail=str(exc))
        removed_note = (f"{held.worktree} was already gone; its registration "
                        f"was pruned",)
    else:
        return WorktreeResult(verb="finish", refused=PATH_UNREADABLE,
                              item=item, instance=instance, claim=held,
                              path=held.worktree, branch=held.branch,
                              detail=f"{held.worktree} could not be examined")

    notes = removed_note
    deleted = False
    if held.branch:
        if _merged_into(root, held.branch, into, runner=runner):
            try:
                operator_work._git(["branch", "-d", held.branch], root,
                                   runner=runner)
                deleted = True
            except GitUnavailable as exc:
                notes += (f"branch {held.branch} was kept: {exc}",)
        else:
            notes += (f"branch {held.branch} is not contained in {into} and "
                      f"was kept",)

    operator_work.release(db, item=item, instance=instance)
    return WorktreeResult(verb="finish", ok=True, item=item, instance=instance,
                          claim=held, path=held.worktree, branch=held.branch,
                          branch_deleted=deleted, notes=notes)


def survey(db, root, *, probes=None, now=None,
           stale_after: float = operator_liveness.DEFAULT_STALE_AFTER,
           preserve: bool = False, runner=None) -> "list[Registration]":
    """Every checkout and every claim, each classified and never touched.

    The join is on the path, so the two directions of mismatch both surface: a
    registration no claim names, and a claim naming a place git has no
    registration for. Neither is an error on its own -- the primary checkout
    is unclaimed by construction, and a claim can legitimately record no
    worktree at all -- which is why this reports states rather than problems.

    With ``preserve``, a dirty checkout that is unclaimed or whose owner the
    cascade calls DEAD has its uncommitted work committed to a ``wip/``
    branch. That is the only write this function can make, and it adds a ref:
    nothing is removed, nothing is checked out, and the working tree and index
    are byte-identical afterwards, which is
    :func:`operator_work.preserve`'s existing guarantee rather than a second
    one made here.
    """
    probes = probes or operator_liveness.SystemProbes()
    records = registrations(root, runner=runner)
    held = work_claims.claims(db)
    by_path = {}
    for claim in held:
        if claim.worktree and _claimed_platform_ok(claim):
            by_path[os.path.normcase(os.path.abspath(claim.worktree))] = claim

    seen: set = set()
    out: "list[Registration]" = []
    for index, record in enumerate(records):
        key = os.path.normcase(os.path.abspath(record.path))
        claim = by_path.get(key)
        if claim is not None:
            seen.add(claim.item)
        verdict = None
        if index == 0:
            state = PRIMARY
        elif claim is None:
            state = UNCLAIMED
        else:
            verdict = operator_liveness.assess(claim, probes=probes, now=now,
                                               stale_after=stale_after)
            state = {operator_liveness.LIVE: LIVE,
                     operator_liveness.DEAD: DEAD,
                     operator_liveness.STALE: STALE}[verdict.verdict]
        present = install_manifest.dir_present(Path(record.path))
        note = ""
        if present is False:
            state = MISSING
            note = "the directory is not there"
        elif present is None:
            note = "the directory could not be examined"
        banked = None
        if (preserve and state in (UNCLAIMED, DEAD) and present is True):
            item = claim.item if claim is not None else slug(
                record.branch or Path(record.path).name)
            owner = claim.instance if claim is not None else "unclaimed"
            try:
                banked = operator_work.preserve(record.path, item=item,
                                                instance=owner, runner=runner)
            except GitUnavailable as exc:
                note = (note + "; " if note else "") + f"not preserved: {exc}"
        out.append(Registration(path=record.path, branch=record.branch,
                                head=record.head, detached=record.detached,
                                prunable=record.prunable, state=state,
                                claim=claim, liveness=verdict,
                                preserved=banked, note=note))

    for claim in held:
        if claim.item in seen or not claim.worktree:
            continue
        note = ("recorded on a "
                f"{claim.platform or 'foreign'} system"
                if not _claimed_platform_ok(claim)
                else "git has no worktree registered at this path")
        out.append(Registration(path=claim.worktree, branch=claim.branch,
                                state=UNREGISTERED, claim=claim, note=note))
    return out


__all__ = [
    "DEAD",
    "DEFAULT_INTEGRATION",
    "FOREIGN_PLATFORM",
    "GIT_FAILED",
    "INSIDE_TARGET",
    "LIVE",
    "MISSING",
    "NOT_OWNER",
    "NO_WORKTREE",
    "PATH_EXISTS",
    "PATH_UNREADABLE",
    "PRIMARY",
    "Registration",
    "STALE",
    "UNCLAIMED",
    "UNREGISTERED",
    "WORKTREES_DIR",
    "WORK_PREFIX",
    "WORKTREE_DIRTY",
    "WorktreeResult",
    "default_path",
    "derived_branch",
    "finish",
    "new",
    "parse_worktree_list",
    "registrations",
    "slug",
    "survey",
]
