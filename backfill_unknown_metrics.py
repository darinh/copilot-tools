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

COLUMNS = ("api_time_seconds", "session_time_seconds",
           "lines_added", "lines_removed")

REPLACEMENTS = (
    ("API time spent: 0s", "API time spent: unknown"),
    ("Total session time: 0m 0s", "Total session time: unknown"),
    ("Total code changes: +0 -0", "Total code changes: unknown"),
)


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


def find_fabricated(conn: sqlite3.Connection, log_dir: Path,
                    missing_logs: bool = False) -> list[int]:
    """Rows whose four fields are all 0 and whose log has no shutdown event.

    With ``missing_logs``, rows whose log has since been deleted also count.
    They are provably fabricated on their own evidence: an all-zero row claims
    a session duration of zero, and no session a shutdown event actually
    measured lasted no time at all. Without the flag they are left alone,
    because "cannot check" is not "know it is wrong".
    """
    zeros = " AND ".join(f"{c} = 0" for c in COLUMNS)
    rows = conn.execute(
        f"SELECT id, log_file FROM sessions WHERE {zeros}").fetchall()
    fabricated = []
    for row in rows:
        if not row["log_file"]:
            continue
        path = log_dir / row["log_file"]
        if not path.exists():
            if missing_logs:
                fabricated.append(row["id"])
            continue
        if not log_has_shutdown_event(path):
            fabricated.append(row["id"])
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
    """
    dst = sqlite3.connect(dest)
    try:
        conn.backup(dst)
    finally:
        dst.close()


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

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        ids = find_fabricated(conn, args.logs, args.missing_logs)
        if not ids:
            print("No fabricated zeros found.")
            return 0

        print(f"{len(ids)} row(s) hold zeros with no shutdown event to back "
              f"them: {ids}")
        if not args.apply:
            print("Dry run. Re-run with --apply to clear them.")
            return 0

        backup = backup_path(args.db)
        write_backup(conn, backup)

        placeholders = ",".join("?" * len(ids))
        sets = ", ".join(f"{c} = NULL" for c in COLUMNS)
        conn.execute(
            f"UPDATE sessions SET {sets} WHERE id IN ({placeholders})", ids)
        for old, new in REPLACEMENTS:
            conn.execute(
                f"UPDATE sessions SET raw_metrics = replace(raw_metrics, ?, ?) "
                f"WHERE id IN ({placeholders})", (old, new, *ids))
        conn.commit()
        print(f"Cleared {len(ids)} row(s). Backup: {backup}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
