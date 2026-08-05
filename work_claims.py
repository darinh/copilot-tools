#!/usr/bin/env python3
"""One work item, one owner: the claim store behind ``operator work``.

A claim records *which instance is working what*, and the runtime identity of
that instance at the moment it claimed -- boot id, mux session, pid and the
pid's start time. Those four are not bookkeeping: they are what
:mod:`operator_liveness` reads to decide whether a claim's owner is provably
gone, and a claim written without them can only ever be judged by its
heartbeat, which is the one signal the spec refuses to act on alone.

**Keyed by the work item, with the owner recorded.** The predecessor keyed the
row by ``agent_id UNIQUE``, which answers "what is this agent doing" and
cannot answer "who has this item" without a scan -- and the second question is
the one a reclaim asks. The item is the primary key here; ``instance`` carries
its own UNIQUE constraint because an agent holds at most one item at a time
(spec D6). Batching is deliberately *not* designed for: allowing a set now
means designing release and reclaim for a shape nobody has needed.

Nothing in this module judges liveness or steals a claim. :func:`reassign` is
a compare-and-swap on the *current* owner, so the caller has to have decided
who that is and to have been right; the policy that decides -- and the
`wip/` commit that must precede it -- lives with ``operator work reclaim``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# An editable install freezes the module list into its import finder, so a
# module added to this directory after the last `pip install -e .` is invisible
# to the installed entry points even though the file sits right here.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from operator_ingest import connect                            # noqa: E402

#: The claim database, inside the project directory that already holds the
#: project's handoffs. Per project, because a work item is named relative to
#: one; the subproject a monorepo item belongs to is a column, not a database.
DB_NAME = "work.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_claims (
    item         TEXT PRIMARY KEY,
    subproject   TEXT NOT NULL DEFAULT '',
    instance     TEXT NOT NULL UNIQUE,
    worktree     TEXT,
    branch       TEXT,
    boot_id      TEXT,
    mux_session  TEXT,
    pid          INTEGER,
    pid_start    TEXT,
    claimed_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);
"""

#: Why a claim was refused. Two reasons, kept apart because they call for
#: opposite responses: an item held by somebody else may be reclaimable once
#: its owner is shown to be dead, while an instance already holding an item
#: has to release that one first and no liveness check will change it.
ITEM_HELD = "item-held"
INSTANCE_BUSY = "instance-busy"

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ClaimRefused(Exception):
    """A claim could not be taken, and by whom it is held instead.

    Carries the reason rather than returning ``None`` because "somebody else
    has this item" and "you already hold another item" lead to different next
    moves, and a caller handed a bare ``None`` has to guess which happened.
    """

    def __init__(self, reason: str, *, item: str, instance: str,
                 holder: "Claim | None" = None):
        self.reason = reason
        self.item = item
        self.instance = instance
        self.holder = holder
        if reason == INSTANCE_BUSY and holder is not None:
            msg = (f"instance {instance!r} already holds {holder.item!r}; "
                   f"release it before claiming {item!r}")
        elif holder is not None:
            msg = f"{item!r} is held by {holder.instance!r}"
        else:
            msg = f"{item!r} could not be claimed by {instance!r}"
        super().__init__(msg)


@dataclass(frozen=True)
class Claim:
    """One row of ``work_claims``."""

    item: str
    instance: str
    subproject: str = ""
    worktree: "str | None" = None
    branch: "str | None" = None
    boot_id: "str | None" = None
    mux_session: "str | None" = None
    pid: "int | None" = None
    pid_start: "str | None" = None
    claimed_at: str = ""
    heartbeat_at: str = ""


def utcnow() -> str:
    return datetime.now(tz=timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: "str | None") -> "datetime | None":
    """A stored timestamp as an aware datetime, or ``None`` if it will not read.

    ``None`` is a real answer and is never smoothed into "now" or "the epoch".
    A heartbeat that cannot be read is not a fresh one and not an ancient one;
    it is a claim whose age is unknown, and :mod:`operator_liveness` reports
    that rather than acting on it.
    """
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, TS_FORMAT)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def db_path(project_dir) -> Path:
    """Where one project's claims live.

    Takes the project *directory* rather than a guid so the catalog is not a
    dependency of the store: the tests, and any caller that already resolved
    the project, hand over a path.
    """
    return Path(project_dir) / DB_NAME


def init_db(path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


def _row_to_claim(row) -> Claim:
    return Claim(
        item=row["item"],
        instance=row["instance"],
        subproject=row["subproject"] or "",
        worktree=row["worktree"],
        branch=row["branch"],
        boot_id=row["boot_id"],
        mux_session=row["mux_session"],
        pid=row["pid"],
        pid_start=row["pid_start"],
        claimed_at=row["claimed_at"],
        heartbeat_at=row["heartbeat_at"],
    )


def _claim_for_item(conn, item: str) -> "Claim | None":
    row = conn.execute(
        "SELECT * FROM work_claims WHERE item = ?", (item,)).fetchone()
    return None if row is None else _row_to_claim(row)


def _claim_for_instance(conn, instance: str) -> "Claim | None":
    row = conn.execute(
        "SELECT * FROM work_claims WHERE instance = ?", (instance,)).fetchone()
    return None if row is None else _row_to_claim(row)


def claim_for_item(path, item: str) -> "Claim | None":
    with connect(path) as conn:
        return _claim_for_item(conn, item)


def claim_for_instance(path, instance: str) -> "Claim | None":
    """What this instance is working, if anything.

    The resume case of ``operator session start``: an instance that comes back
    from a restart holding a claim resumes it rather than being offered work.
    """
    with connect(path) as conn:
        return _claim_for_instance(conn, instance)


def claims(path) -> "list[Claim]":
    with connect(path) as conn:
        return [_row_to_claim(row) for row in
                conn.execute("SELECT * FROM work_claims ORDER BY claimed_at, item")]


def claim(path, *, item: str, instance: str, subproject: str = "",
          worktree=None, branch=None, boot_id=None, mux_session=None,
          pid=None, pid_start=None, now=None) -> Claim:
    """Take ``item`` for ``instance``, or raise :class:`ClaimRefused`.

    Re-claiming an item this instance already holds is a **resume**, not a
    conflict: a restarted agent has a new pid, and often a new mux session,
    so the runtime identity is rewritten and the heartbeat refreshed. Refusing
    it would make the ordinary restart path an error, and an agent that has to
    release-then-claim to resume has a window in which its own item is free
    for somebody else to take.

    The whole check-and-write is one ``BEGIN IMMEDIATE`` transaction: reading
    "unclaimed" and writing the claim in two statements is the race this table
    exists to prevent.
    """
    stamp = now or utcnow()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        held = _claim_for_item(conn, item)
        if held is not None and held.instance != instance:
            raise ClaimRefused(ITEM_HELD, item=item, instance=instance,
                               holder=held)
        elsewhere = _claim_for_instance(conn, instance)
        if elsewhere is not None and elsewhere.item != item:
            raise ClaimRefused(INSTANCE_BUSY, item=item, instance=instance,
                               holder=elsewhere)
        if held is None:
            conn.execute(
                "INSERT INTO work_claims (item, subproject, instance, worktree,"
                " branch, boot_id, mux_session, pid, pid_start, claimed_at,"
                " heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item, subproject, instance, worktree, branch, boot_id,
                 mux_session, pid, pid_start, stamp, stamp))
        else:
            conn.execute(
                "UPDATE work_claims SET subproject = ?, worktree = ?,"
                " branch = ?, boot_id = ?, mux_session = ?, pid = ?,"
                " pid_start = ?, heartbeat_at = ? WHERE item = ?"
                " AND instance = ?",
                (subproject, worktree, branch, boot_id, mux_session, pid,
                 pid_start, stamp, item, instance))
        return _claim_for_item(conn, item)


def heartbeat(path, *, item: str, instance: str, now=None) -> bool:
    """Refresh the claim's heartbeat. False when ``instance`` is not the owner.

    Owner-guarded, so a heartbeat from an agent that lost its claim to a
    reclaim cannot resurrect it: the update names both the item and the
    instance, and a row that no longer matches both is not touched.
    """
    stamp = now or utcnow()
    with connect(path) as conn:
        cur = conn.execute(
            "UPDATE work_claims SET heartbeat_at = ? WHERE item = ?"
            " AND instance = ?", (stamp, item, instance))
        return cur.rowcount == 1


def release(path, *, item: str, instance: str) -> bool:
    """Give the item up. False when ``instance`` is not the owner."""
    with connect(path) as conn:
        cur = conn.execute(
            "DELETE FROM work_claims WHERE item = ? AND instance = ?",
            (item, instance))
        return cur.rowcount == 1


def reassign(path, *, item: str, expect_owner: str, to_instance: str,
             worktree=None, branch=None, boot_id=None, mux_session=None,
             pid=None, pid_start=None, now=None) -> Claim:
    """Move a claim from a departed owner to a new one, if ``expect_owner``
    still holds it.

    A compare-and-swap, not a steal: the caller states who it believes the
    owner to be, and a claim that changed hands in the meantime refuses the
    write rather than overwriting whatever is there now. Deciding that the
    old owner is gone is :mod:`operator_liveness`'s job, and preserving that
    owner's uncommitted work is ``operator work reclaim``'s -- this function
    is only the last, atomic step.
    """
    stamp = now or utcnow()
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        held = _claim_for_item(conn, item)
        if held is None or held.instance != expect_owner:
            raise ClaimRefused(ITEM_HELD, item=item, instance=to_instance,
                               holder=held)
        busy = _claim_for_instance(conn, to_instance)
        if busy is not None and busy.item != item:
            raise ClaimRefused(INSTANCE_BUSY, item=item,
                               instance=to_instance, holder=busy)
        conn.execute(
            "UPDATE work_claims SET instance = ?, worktree = ?, branch = ?,"
            " boot_id = ?, mux_session = ?, pid = ?, pid_start = ?,"
            " claimed_at = ?, heartbeat_at = ? WHERE item = ? AND instance = ?",
            (to_instance, worktree, branch, boot_id, mux_session, pid,
             pid_start, stamp, stamp, item, expect_owner))
        return _claim_for_item(conn, item)


__all__ = [
    "Claim",
    "ClaimRefused",
    "DB_NAME",
    "INSTANCE_BUSY",
    "ITEM_HELD",
    "SCHEMA",
    "claim",
    "claim_for_instance",
    "claim_for_item",
    "claims",
    "db_path",
    "heartbeat",
    "init_db",
    "parse_ts",
    "reassign",
    "release",
    "utcnow",
]
