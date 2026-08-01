"""One-off repair: turn fabricated zeros in the metrics store into NULLs.

Copilot CLI 1.0.77 stopped emitting the ``session_shutdown`` telemetry event in
most sessions (3 of 50 real logs had one). ``operator_ingest`` used to default
the four fields that only that event supplies to 0, so a session nobody
measured became a session that provably did nothing: "+0 -0 lines, 0s api,
0m 0s duration". That is worse than missing data, because it averages in.

Ingest now records NULL when the event is absent. This repairs rows written
before that fix, and only those whose log genuinely lacks the event -- rows
backed by a real shutdown event are left untouched.

Usage:
    python backfill_unknown_metrics.py [--db PATH] [--logs PATH] [--apply]
                                       [--missing-logs]

Without --apply it reports what it would change and touches nothing.
Rows whose log has since been deleted are skipped unless --missing-logs is
given, because without the log there is nothing left to check them against.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from operator_ingest import BUSY_TIMEOUT

COLUMNS = ("api_time_seconds", "session_time_seconds",
           "lines_added", "lines_removed")

ZERO_GUARD = " AND ".join(f"{c} = 0" for c in COLUMNS)

REPLACEMENTS = (
    ("API time spent: 0s", "API time spent: unknown"),
    ("Total session time: 0m 0s", "Total session time: unknown"),
    ("Total code changes: +0 -0", "Total code changes: unknown"),
)


def connect(db: Path) -> sqlite3.Connection:
    """Open the metrics database the way every other writer opens it.

    ``sqlite3.connect`` defaults to a 5-second busy timeout, so an unqualified
    connection here is not merely impatient -- it is the *first* participant to
    give up, because every other writer in this system waits ``BUSY_TIMEOUT``
    (15s). The operator keeps this database open from a live loop, and the
    failure lands on the ``--apply`` run, after the dry run has already told
    the user the repair is safe. ``BUSY_TIMEOUT`` is imported rather than
    redeclared so tuning it in one place cannot leave this script behind.

    Autocommit (``isolation_level=None``) is deliberate: the repair drives its
    own ``BEGIN IMMEDIATE`` so the decision and the write land as one unit.
    """
    conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
    return conn


def log_has_shutdown_event(path: Path) -> bool:
    """True when the log still contains the event the four fields come from.

    A substring test is deliberate: this only decides whether to *clear*
    values, so the conservative direction is to treat any mention as evidence
    the event was there and leave the row alone.
    """
    try:
        return "session_shutdown" in path.read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return True  # Unreadable log: assume measured, change nothing.


def log_fingerprint(path: Path) -> tuple[int, int] | None:
    """What the log looked like when the scan trusted it, or None if absent.

    The scan's verdict rests on the *contents* of this file, and the scan can
    run for a long time. Re-reading every log inside the write transaction
    would hold a write lock across arbitrary file I/O; a stat is cheap enough
    to redo.

    Size is part of the fingerprint because mtime alone is not enough: the
    system clock ticks far more coarsely than a file can be rewritten, so two
    writes can share a timestamp. A log that gained a shutdown event gained
    bytes, so the pair catches it. Not a hash -- this decides only whether to
    *skip* a row, so the failure it must avoid is a missed change, and a
    change that preserves both size and timestamp is not one a growing log
    makes.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def find_fabricated(conn: sqlite3.Connection, log_dir: Path,
                    missing_logs: bool = False
                    ) -> list[tuple[int, tuple[int, int] | None]]:
    """Rows whose four fields are all 0 and whose log has no shutdown event.

    Returns ``(row_id, log_fingerprint)`` pairs so the write can confirm the
    evidence it is acting on has not moved underneath it.

    With ``missing_logs``, rows whose log has since been deleted also count.
    They are provably fabricated on their own evidence: an all-zero row claims
    a session duration of zero, and no session a shutdown event actually
    measured lasted no time at all. Without the flag they are left alone,
    because "cannot check" is not "know it is wrong".
    """
    rows = conn.execute(
        f"SELECT id, log_file FROM sessions WHERE {ZERO_GUARD}").fetchall()
    fabricated = []
    for row in rows:
        if not row["log_file"]:
            continue
        path = log_dir / row["log_file"]
        # Fingerprint BEFORE reading, never after. The read is the slow part,
        # and a log that gains its shutdown event while being read would
        # otherwise be fingerprinted in its new state and then match perfectly
        # at write time -- the verdict stale, the evidence fresh, and the
        # check that exists to catch exactly this waved it through. Stat
        # first and any change from here on shows up as a mismatch.
        fingerprint = log_fingerprint(path)
        if fingerprint is None:
            if missing_logs:
                fabricated.append((row["id"], None))
            continue
        if not log_has_shutdown_event(path):
            fabricated.append((row["id"], fingerprint))
    return fabricated


def backup_path(db: Path) -> Path:
    """A backup name that never overwrites an earlier one.

    Widening the match (``--missing-logs``) makes a second run clear rows the
    first did not, so its backup is not a superset of the first one. Silently
    replacing the earlier file would destroy the only record of the rows run
    one cleared.
    """
    base = db.with_suffix(db.suffix + ".bak-prezero")
    if not base.exists():
        return base
    n = 2
    while base.with_name(f"{base.name}.{n}").exists():
        n += 1
    return base.with_name(f"{base.name}.{n}")


def write_backup(conn: sqlite3.Connection, dest: Path) -> None:
    """Snapshot the live database through SQLite's own backup API.

    A file copy is not safe here: the operator runs the database in WAL mode,
    so committed rows can still live in ``metrics.db-wal`` and a copy of the
    main file alone would restore to a state that never existed. ``backup()``
    reads through the WAL and produces a single consistent file.

    Note what this backup is and is not. It is a point-in-time copy of the
    whole database, so restoring it on a machine where the operator has kept
    running discards every session ingested since. It is an undo for this
    repair, not a general safety net -- stop the operator before restoring.
    """
    dst = sqlite3.connect(dest)
    try:
        conn.backup(dst)
    finally:
        dst.close()


def clear_rows(conn: sqlite3.Connection,
               found: list[tuple[int, tuple[int, int] | None]],
               log_dir: Path) -> tuple[int, int]:
    """Clear the scanned rows, re-checking each one's evidence as it goes.

    The scan reads every candidate log from disk, which takes long enough for
    a live operator to re-ingest one of these sessions in the meantime -- and
    a re-ingest is precisely the event that replaces the zeros with real
    measurements. So the write does not trust the scan's list. Per row it
    re-confirms *both* halves of the original verdict, because either half
    alone is not evidence:

    * the four columns are still all zero (in SQL, inside the transaction);
    * the log still looks the way it did when the scan read it -- a log that
      grew a shutdown event, or came back from the dead, changes its
      fingerprint.

    All-zero on its own would be the wrong test. A short session really can
    measure 0s of API time, a 0-second duration and no line changes, which is
    exactly the case ``find_fabricated`` uses the log to rule out.

    Rows are cleared one at a time, each in its own short transaction, and the
    stat happens *outside* it. Holding one ``BEGIN IMMEDIATE`` across the
    whole list would block the live operator for as long as the loop takes --
    thousands of statements plus a stat per row, which on a network mount or
    a loaded disk can outlast the 15s the operator is willing to wait, turning
    a repair into an outage. Nothing here needs cross-row atomicity: the only
    invariant is that one row's summary and columns agree, and that holds
    inside each per-row transaction.

    Updating one row at a time also keeps the parameter count flat instead of
    scaling with the number of damaged rows -- SQLite's limit is 999 before
    3.32 -- and it is what makes a per-row re-check possible at all.

    Returns ``(cleared, skipped)``.
    """
    sets = ", ".join(f"{c} = NULL" for c in COLUMNS)
    cleared = 0
    for row_id, fingerprint in found:
        row = conn.execute(
            "SELECT log_file FROM sessions WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None or not row["log_file"]:
            continue
        if log_fingerprint(log_dir / row["log_file"]) != fingerprint:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # The text edit runs first: its guard stops matching the moment
            # the columns become NULL. Both land in one transaction, so the
            # summary can never describe a different session than the columns.
            for old, new in REPLACEMENTS:
                conn.execute(
                    "UPDATE sessions SET raw_metrics = replace(raw_metrics, ?, ?) "
                    f"WHERE id = ? AND {ZERO_GUARD}", (old, new, row_id))
            cur = conn.execute(
                f"UPDATE sessions SET {sets} WHERE id = ? AND {ZERO_GUARD}",
                (row_id,))
            cleared += cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # SQLite rolls back on its own for some errors, and then there
                # is no transaction left to end. Never let that displace the
                # real exception -- it is the one that says what went wrong.
                pass
            raise
    return cleared, len(found) - cleared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=Path.home() / ".operator" / "metrics.db",
                        type=Path)
    parser.add_argument("--logs", default=Path.home() / ".copilot" / "logs",
                        type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="write the change (otherwise report only)")
    parser.add_argument("--missing-logs", action="store_true",
                        help="also clear all-zero rows whose log file is gone "
                             "(they claim a zero session duration, which no "
                             "measured session has)")
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"No metrics database at {args.db}")
        return 1

    conn = connect(args.db)
    try:
        found = find_fabricated(conn, args.logs, args.missing_logs)
        if not found:
            print("No fabricated zeros found.")
            return 0

        ids = [row_id for row_id, _ in found]
        print(f"{len(ids)} row(s) hold zeros with no shutdown event to back "
              f"them: {ids}")
        if not args.apply:
            print("Dry run. Re-run with --apply to clear them.")
            return 0

        backup = backup_path(args.db)
        write_backup(conn, backup)

        cleared, skipped = clear_rows(conn, found, args.logs)
        note = (f" {skipped} row(s) changed under the scan and were left "
                f"alone." if skipped else "")
        print(f"Cleared {cleared} row(s). Backup: {backup}{note}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
