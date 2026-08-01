"""Tests for the one-off repair that turns fabricated zeros into NULLs.

The script rewrites a user's real metrics database, so the property that
matters most is the one it must *not* do: clear values that a shutdown event
actually measured. Every test here pins one side of that decision.
"""
from __future__ import annotations

import sqlite3

import pytest

import backfill_unknown_metrics
import operator_ingest

ZEROED_RAW = (
    "Total usage est: 0 Premium requests\n"
    "API time spent: 0s\n"
    "Total session time: 0m 0s\n"
    "Total code changes: +0 -0"
)


def add_session(db_path, log_file, *, api=0, sess=0, added=0, removed=0,
                raw=ZEROED_RAW):
    operator_ingest.init_db(db_path)
    with operator_ingest.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_num, log_file, started_at, ended_at,
                                  api_time_seconds, session_time_seconds,
                                  lines_added, lines_removed, raw_metrics)
            VALUES (1, ?, '2026-07-27T10:00:00Z', '2026-07-27T10:30:00Z',
                    ?, ?, ?, ?, ?)
            """,
            (log_file, api, sess, added, removed, raw),
        )
        conn.commit()


def read_row(db_path, log_file):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM sessions WHERE log_file = ?", (log_file,)
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    return d


def test_dry_run_reports_but_changes_nothing(db_path, logs, capsys):
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs)])

    assert rc == 0
    assert "Dry run" in capsys.readouterr().out
    row = read_row(db_path, "process-1.log")
    assert row["lines_added"] == 0, "dry run must not write"
    assert row["raw_metrics"] == ZEROED_RAW


def test_apply_clears_zeros_and_rewrites_summary(db_path, logs, capsys):
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    row = read_row(db_path, "process-1.log")
    for column in ("api_time_seconds", "session_time_seconds",
                   "lines_added", "lines_removed"):
        assert row[column] is None, f"{column} is {row[column]!r}"
    assert "API time spent: unknown" in row["raw_metrics"]
    assert "Total session time: unknown" in row["raw_metrics"]
    assert "Total code changes: unknown" in row["raw_metrics"]
    # The premium line is not sourced from the shutdown event, so it stays.
    assert "Total usage est: 0 Premium requests" in row["raw_metrics"]
    assert "Backup" in capsys.readouterr().out


def test_apply_writes_a_backup_before_touching_the_database(db_path, logs):
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    backup = db_path.with_name(db_path.name + ".bak-prezero")
    assert backup.exists()
    assert read_row(backup, "process-1.log")["lines_added"] == 0


def test_backup_captures_rows_still_in_the_wal(db_path, logs):
    """The operator runs this database in WAL mode.

    A plain file copy of ``metrics.db`` can miss committed rows that are still
    only in ``metrics.db-wal``, producing a backup that restores to a state
    that never existed. The backup must read through the WAL.
    """
    add_session(db_path, "process-1.log")
    holder = sqlite3.connect(db_path)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute(
            """
            INSERT INTO sessions (session_num, log_file, started_at, ended_at,
                                  api_time_seconds, session_time_seconds,
                                  lines_added, lines_removed, raw_metrics)
            VALUES (2, 'process-wal.log', '2026-07-27T11:00:00Z',
                    '2026-07-27T11:30:00Z', 0, 0, 0, 0, ?)
            """,
            (ZEROED_RAW,),
        )
        holder.commit()
        assert db_path.with_name(db_path.name + "-wal").exists(), (
            "test precondition: the row should still be sitting in the WAL")
        for name in ("process-1.log", "process-wal.log"):
            (logs / name).write_text("no shutdown here", encoding="utf-8")

        backfill_unknown_metrics.main(
            ["--db", str(db_path), "--logs", str(logs), "--apply"])

        backup = db_path.with_name(db_path.name + ".bak-prezero")
        row = read_row(backup, "process-wal.log")
        assert row is not None, "backup lost a row that lived in the WAL"
        assert row["lines_added"] == 0
    finally:
        holder.close()


def test_measured_zeros_are_left_alone(db_path, logs):
    """A session that really did change nothing keeps its zeros."""
    add_session(db_path, "process-2.log")
    (logs / "process-2.log").write_text(
        '{"kind": "session_shutdown"}', encoding="utf-8")

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    row = read_row(db_path, "process-2.log")
    assert row["lines_added"] == 0
    assert row["raw_metrics"] == ZEROED_RAW


def test_unreadable_log_is_treated_as_measured(db_path, logs):
    """A vanished log is not evidence the session was unmeasured."""
    add_session(db_path, "process-gone.log")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert read_row(db_path, "process-gone.log")["lines_added"] == 0


def test_partial_zeros_are_not_touched(db_path, logs):
    """Only all-four-zero rows match the fabrication pattern."""
    add_session(db_path, "process-3.log", added=4)
    (logs / "process-3.log").write_text("no shutdown here", encoding="utf-8")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    row = read_row(db_path, "process-3.log")
    assert row["lines_added"] == 4
    assert row["api_time_seconds"] == 0


def test_second_run_is_a_no_op(db_path, logs, capsys):
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")
    argv = ["--db", str(db_path), "--logs", str(logs), "--apply"]

    backfill_unknown_metrics.main(argv)
    capsys.readouterr()
    rc = backfill_unknown_metrics.main(argv)

    assert rc == 0
    assert "No fabricated zeros found." in capsys.readouterr().out


def test_missing_database_reports_and_fails(tmp_path, logs, capsys):
    rc = backfill_unknown_metrics.main(
        ["--db", str(tmp_path / "nope.db"), "--logs", str(logs)])

    assert rc == 1
    assert "No metrics database" in capsys.readouterr().out
