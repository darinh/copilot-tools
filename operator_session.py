#!/usr/bin/env python3
"""The session lifecycle: who works what, decided before the agent's first token.

Two things live here, and they are the two ends of one session.

:func:`resolve_assignment` answers *what am I working on* (FR-2). The agent
never works it out for itself, because both routes it would take are silently
wrong: ``git rev-parse --show-toplevel`` inside a worktree returns the
worktree, and "walk up to the nearest ``AGENTS.md``" finds a subproject's file
or the worktree's own tracked copy. Both resolve correctly in code, identically
every time, which is the whole argument for moving the question out of the
instruction file.

:func:`end_session` is the other end (FR-5). Ending a session is three effects
-- the handoff, the session-log close, and whatever happens to the work claim
-- and three separate commands is three chances to do one of them. The
combination that must never exist is a **released claim with no handoff**:
that loses the context *and* hands the worktree to somebody else. So the
handoff is written first and the two database effects share one
``BEGIN IMMEDIATE`` transaction. The only seam left is between a file and a
database, and it is oriented so the forbidden pair is unreachable.

**The claim is retained by default, not released.** FR-5 as first written said
``session end`` releases the claim, which cannot be right: the ordinary reason
a session ends is a handoff mid-item, and the resume path in FR-2 exists for
exactly that. Releasing on every end would empty the claim before anything
could resume it. The default is also the safe direction of the two errors --
a claim retained when it should have been released is recovered by the
liveness cascade once its owner is provably gone, while a claim released when
it should have been kept hands a live agent's worktree to somebody else, and
nothing recovers that.

Nothing here steals anything. An offer is a *report* that a claim's owner is
provably gone; taking it is ``operator work reclaim``, which has to preserve
the dead owner's uncommitted work first (FR-4).
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

import operator_liveness                                       # noqa: E402
import work_claims                                             # noqa: E402
from operator_ingest import connect                            # noqa: E402
from work_claims import Claim, utcnow                          # noqa: E402

#: This instance already holds an item; it resumes rather than being offered
#: anything. The restart case, and the common one.
RESUME = "resume"
#: This instance holds nothing, and at least one claim's owner is provably
#: gone. The claims are reported oldest first; none of them has been taken.
OFFER = "offer"
#: Nothing to resume and nothing reclaimable.
NONE = "none"

#: A session row closed by :func:`end_session`.
ENDED = "ended"
#: A session row that was still open when a later session of the same instance
#: started. Not an error and not a guess: the previous session ended without
#: reaching ``session end``, which is what a crash looks like from here.
ABANDONED = "abandoned"

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    instance    TEXT NOT NULL,
    session     INTEGER NOT NULL,
    item        TEXT,
    subproject  TEXT NOT NULL DEFAULT '',
    worktree    TEXT,
    branch      TEXT,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    outcome     TEXT
);
CREATE INDEX IF NOT EXISTS session_log_instance
    ON session_log (instance, session);
"""


def db_path(project_dir) -> Path:
    """The session log shares the claim database.

    One file, because :func:`end_session` closes a log row and releases a
    claim in a single transaction, and a transaction cannot span two sqlite
    databases without attaching one to the other -- which is a lock ordering
    problem bought to keep two files apart for no reason anybody has named.
    """
    return work_claims.db_path(project_dir)


def init_db(path) -> None:
    work_claims.init_db(path)
    with connect(path) as conn:
        conn.executescript(SESSION_SCHEMA)


@dataclass(frozen=True)
class Offer:
    """A claim whose owner is provably gone, and the evidence for that.

    The verdict travels with the claim because "this is reclaimable" is not
    reportable on its own: whoever is asked to confirm an agent is gone needs
    to see *which* probe said so.
    """

    claim: Claim
    liveness: "operator_liveness.Liveness"

    @property
    def item(self) -> str:
        return self.claim.item

    @property
    def reason(self) -> str:
        return self.liveness.reason


@dataclass(frozen=True)
class Assignment:
    """What this instance is to work on, and how that was decided."""

    kind: str
    instance: str
    claim: "Claim | None" = None
    offers: "tuple[Offer, ...]" = ()
    stale: "tuple[Offer, ...]" = ()
    subproject: "str | None" = None

    @property
    def item(self) -> "str | None":
        return None if self.claim is None else self.claim.item

    @property
    def worktree(self) -> "str | None":
        return None if self.claim is None else self.claim.worktree

    @property
    def branch(self) -> "str | None":
        return None if self.claim is None else self.claim.branch


def _sort_key(claim: Claim) -> tuple:
    """Oldest first, and deterministic when two claims share a timestamp.

    ``claimed_at`` has one-second resolution, so a tie is ordinary rather than
    exotic, and an unstable order would make the offer list differ between two
    reads of an unchanged database.
    """
    return (claim.claimed_at or "", claim.item)


def resolve_assignment(path, *, instance: str, subproject: "str | None" = None,
                       probes=None, now=None,
                       stale_after: float = operator_liveness.DEFAULT_STALE_AFTER,
                       ) -> Assignment:
    """Resolve, in order: resume, then offer, then nothing (FR-2).

    **An instance's own claim is never liveness-checked.** After a restart the
    recorded pid is the previous process of this same instance, so the cascade
    would return DEAD by construction -- and treating that as "not mine" would
    put the agent's own item on the offer list for somebody else to take.
    Ownership is by instance name; liveness answers a question about *other*
    instances.

    ``subproject`` filters what may be offered. It does not filter the resume:
    an instance that already holds an item resumes it whatever subproject it
    belongs to, because the alternative is stranding a live claim whose owner
    was told to look somewhere else.
    """
    probes = probes or operator_liveness.SystemProbes()
    mine = work_claims.claim_for_instance(path, instance)
    if mine is not None:
        return Assignment(RESUME, instance, claim=mine, subproject=subproject)

    offers: list[Offer] = []
    stale: list[Offer] = []
    for other in work_claims.claims(path):
        if other.instance == instance:  # pragma: no cover - excluded above
            continue
        if subproject is not None and other.subproject != subproject:
            continue
        verdict = operator_liveness.assess(other, probes=probes, now=now,
                                           stale_after=stale_after)
        if verdict.verdict == operator_liveness.DEAD:
            offers.append(Offer(other, verdict))
        elif verdict.verdict == operator_liveness.STALE:
            stale.append(Offer(other, verdict))

    offers.sort(key=lambda o: _sort_key(o.claim))
    stale.sort(key=lambda o: _sort_key(o.claim))
    kind = OFFER if offers else NONE
    return Assignment(kind, instance, offers=tuple(offers),
                      stale=tuple(stale), subproject=subproject)


def runtime_identity(*, mux_session: "str | None" = None, pid: "int | None" = None,
                     probes=None) -> dict:
    """The four fields a claim records so its owner can later be judged.

    Gathered here rather than at each call site because a claim written
    without them can only ever be judged by its heartbeat, which is the one
    signal :mod:`operator_liveness` refuses to act on alone.
    """
    probes = probes or operator_liveness.SystemProbes()
    pid = os.getpid() if pid is None else pid
    return {
        "boot_id": probes.boot_identity(),
        "mux_session": mux_session,
        "pid": pid,
        "pid_start": probes.process_start_token(pid),
    }


def _close_open_rows(conn, instance: str, stamp: str, outcome: str,
                     session: "int | None" = None) -> int:
    sql = ("UPDATE session_log SET ended_at = ?, outcome = ?"
           " WHERE instance = ? AND ended_at IS NULL")
    params: list = [stamp, outcome, instance]
    if session is not None:
        sql += " AND session = ?"
        params.append(session)
    return conn.execute(sql, params).rowcount


def start_session(path, *, instance: str, session: int,
                  subproject: "str | None" = None, probes=None, now=None,
                  stamp: "str | None" = None,
                  stale_after: float = operator_liveness.DEFAULT_STALE_AFTER,
                  ) -> Assignment:
    """Open a session-log row and return this instance's assignment.

    Any row of this instance still open is closed as :data:`ABANDONED` first.
    That is a recorded fact rather than a guess: a row is only left open by a
    session that never reached ``session end``, and the log is append-only, so
    the alternative -- reusing the row -- would overwrite the evidence that it
    happened.

    ``now`` is the *datetime* the liveness cascade measures heartbeat age
    against; ``stamp`` is the *string* written to the log. They are separate
    arguments because they are separate types with separate readers, and one
    parameter carrying either would be a coin toss decided by ``isinstance``.
    """
    assignment = resolve_assignment(path, instance=instance,
                                    subproject=subproject, probes=probes,
                                    now=now, stale_after=stale_after)
    stamp = stamp or utcnow()
    claim = assignment.claim
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _close_open_rows(conn, instance, stamp, ABANDONED)
        conn.execute(
            "INSERT INTO session_log (instance, session, item, subproject,"
            " worktree, branch, started_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instance, session,
             None if claim is None else claim.item,
             (claim.subproject if claim is not None
              else (subproject or "")),
             None if claim is None else claim.worktree,
             None if claim is None else claim.branch,
             stamp))
    return assignment


def open_session(path, *, instance: str) -> "dict | None":
    """The instance's currently open session row, or ``None``."""
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM session_log WHERE instance = ? AND ended_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (instance,)).fetchone()
    return None if row is None else dict(row)


def sessions(path, *, instance: "str | None" = None) -> "list[dict]":
    sql = "SELECT * FROM session_log"
    params: tuple = ()
    if instance is not None:
        sql += " WHERE instance = ?"
        params = (instance,)
    sql += " ORDER BY id"
    with connect(path) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


@dataclass(frozen=True)
class EndResult:
    """What ``session end`` actually managed to do.

    Every effect is reported separately because the caller's next move differs
    by which one failed, and a bare exception cannot say "the handoff is
    safely on disk, the claim is still yours".
    """

    instance: str
    session: int
    handoff_written: bool = False
    handoff_path: "str | None" = None
    log_closed: bool = False
    claim_released: bool = False
    claim_retained: bool = False
    item: "str | None" = None
    failure: "str | None" = None
    notes: tuple = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def released_without_handoff(self) -> bool:
        """The combination FR-5 exists to make unreachable.

        Asserted by the tests at every failure point rather than argued for in
        prose: a property that is only ever reasoned about is a property
        nothing checks.
        """
        return self.claim_released and not self.handoff_written


def end_session(path, *, instance: str, session: int, write_handoff,
                release_claim: bool = False, stamp: "str | None" = None,
                ) -> EndResult:
    """End a session: handoff, then log close and claim disposal together.

    ``write_handoff`` is a zero-argument callable returning the path it wrote.
    It is injected rather than imported so that this function owns the
    *ordering* and nothing else -- the handoff tool's own failure handling is
    already thorough, and duplicating it here would give two places to change.

    Order is the guarantee. The handoff is written first and the database
    effects share one ``BEGIN IMMEDIATE`` transaction, so:

    - a handoff that fails leaves the claim held and the log row open;
    - a database failure leaves the handoff on disk and the claim held;
    - there is no ordering in which the claim is released and no handoff was
      written.

    ``release_claim`` defaults False. See the module docstring: the ordinary
    session end is a handoff mid-item, and the resume path exists for it.
    """
    stamp = stamp or utcnow()
    held = work_claims.claim_for_instance(path, instance)
    item = None if held is None else held.item

    try:
        written = write_handoff()
    except Exception as exc:                      # noqa: BLE001 - reported
        return EndResult(instance=instance, session=session, item=item,
                         failure=f"handoff not written: {exc}")

    result_kw = {
        "instance": instance,
        "session": session,
        "item": item,
        "handoff_written": True,
        "handoff_path": None if written is None else str(written),
    }

    try:
        with connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            closed = _close_open_rows(conn, instance, stamp, ENDED,
                                      session=session)
            released = 0
            if release_claim and item is not None:
                released = conn.execute(
                    "DELETE FROM work_claims WHERE item = ? AND instance = ?",
                    (item, instance)).rowcount
            elif item is not None:
                conn.execute(
                    "UPDATE work_claims SET heartbeat_at = ? WHERE item = ?"
                    " AND instance = ?", (stamp, item, instance))
    except Exception as exc:                      # noqa: BLE001 - reported
        return EndResult(failure=f"session log and claim unchanged: {exc}",
                         **result_kw)

    notes = []
    if closed == 0:
        # Not a failure: `session end` twice, or an end for a session that was
        # never started through `session start`. Recorded rather than silent,
        # because a log that closes nothing is the shape of a wiring mistake.
        notes.append(f"no open session row for {instance!r} session {session}")
    if release_claim and item is None:
        notes.append("nothing to release: this instance holds no claim")

    return EndResult(log_closed=closed > 0,
                     claim_released=bool(release_claim and released),
                     claim_retained=bool(item is not None and not release_claim),
                     notes=tuple(notes), **result_kw)


def assignment_values(assignment: "Assignment | None") -> dict:
    """The four substitution values an assigned agent is told about.

    Always all four keys, always strings. A missing key and an empty one read
    identically to a template but not to the code that fills one in, and a
    substitution table whose shape depends on whether work was assigned is a
    table every consumer has to guard.
    """
    if assignment is None:
        return {"instanceName": "", "workItemRef": "", "worktreePath": "",
                "branchName": ""}
    return {
        "instanceName": assignment.instance or "",
        "workItemRef": assignment.item or "",
        "worktreePath": assignment.worktree or "",
        "branchName": assignment.branch or "",
    }


def describe(assignment: Assignment) -> str:
    """One paragraph for the agent's preamble, or ``""`` when there is nothing.

    Silence is the right output for :data:`NONE`. A line reading "you have no
    assignment" is weight on every token of every unassigned session, and the
    measurement behind this whole feature is that always-loaded lines are paid
    for whether or not they say anything.
    """
    if assignment.kind == RESUME and assignment.claim is not None:
        parts = [f"Your assignment is work item {assignment.item}."]
        if assignment.worktree:
            parts.append(f"It is checked out at {assignment.worktree}"
                         + (f" on branch {assignment.branch}."
                            if assignment.branch else "."))
        parts.append("Work only in that worktree; it was resolved for you and "
                     "you do not need to discover it.")
        return " ".join(parts)
    if assignment.kind == OFFER:
        names = ", ".join(o.item for o in assignment.offers)
        return (f"You hold no work item. These are held by instances that are "
                f"provably gone and can be taken with `operator work reclaim`, "
                f"oldest first: {names}. Reclaiming preserves the previous "
                f"owner's uncommitted work; never run git stash, reset, clean, "
                f"checkout or restore in their worktree.")
    return ""


__all__ = [
    "ABANDONED",
    "Assignment",
    "ENDED",
    "EndResult",
    "NONE",
    "OFFER",
    "Offer",
    "RESUME",
    "SESSION_SCHEMA",
    "assignment_values",
    "db_path",
    "describe",
    "end_session",
    "init_db",
    "open_session",
    "resolve_assignment",
    "runtime_identity",
    "sessions",
    "start_session",
]
