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


def test_connect_waits_as_long_as_every_other_writer(db_path):
    """The operator holds this database open from a live loop.

    ``sqlite3.connect``'s own default is 5000ms, so "has a busy timeout" is
    not the property worth asserting -- the script had one. The property is
    that it waits as long as ``operator_ingest`` does, because a repair that
    gives up first is a repair that fails on ``--apply``, after the dry run
    has already told the user it is safe.
    """
    add_session(db_path, "process-1.log")
    conn = backfill_unknown_metrics.connect(db_path)
    try:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert timeout_ms == int(operator_ingest.BUSY_TIMEOUT * 1000)
    assert timeout_ms > 5000, "stdlib default; the whole point is to beat it"


def test_apply_waits_out_a_concurrent_writer(db_path, logs):
    """A held write lock must delay the repair, not defeat it.

    This is a regression guard, not proof of the timeout fix -- it passes
    against the old 5000ms default too, because the lock here is held for
    well under 5s. What it pins is that the repair survives contention at all
    rather than aborting half-applied.

    The elapsed-time assertion is what stops it being vacuous: without it this
    passes just as happily when the lock was never held.
    """
    import threading
    import time

    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    hold = 0.75
    holder = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE sessions SET session_num = 99")
    released = threading.Event()

    def release():
        holder.commit()
        released.set()

    timer = threading.Timer(hold, release)
    started = time.monotonic()
    timer.start()
    try:
        rc = backfill_unknown_metrics.main(
            ["--db", str(db_path), "--logs", str(logs), "--apply"])
    finally:
        elapsed = time.monotonic() - started
        timer.cancel()
        if not released.is_set():
            holder.commit()
        holder.close()

    assert rc == 0
    assert elapsed >= hold, (
        f"finished in {elapsed:.2f}s, so the write lock was never contended "
        f"and this test proves nothing")
    assert read_row(db_path, "process-1.log")["lines_added"] is None


def test_a_row_remeasured_mid_run_is_not_erased(db_path, logs, capsys,
                                                monkeypatch):
    """Scanning logs takes long enough for a live operator to re-ingest one of
    these sessions -- and a re-ingest is exactly the event that replaces the
    zeros with real measurements. Clearing on the strength of a stale scan
    would destroy the only copy of data that had just arrived.

    The backup runs between the scan and the write, so it is the honest place
    to simulate the interleaving.
    """
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    original = backfill_unknown_metrics.write_backup

    def remeasure(conn, dest):
        original(conn, dest)
        other = sqlite3.connect(db_path, timeout=10)
        try:
            other.execute(
                "UPDATE sessions SET api_time_seconds = 12, "
                "session_time_seconds = 340, lines_added = 7, "
                "lines_removed = 2, raw_metrics = ? WHERE log_file = ?",
                ("API time spent: 12s", "process-1.log"))
            other.commit()
        finally:
            other.close()

    monkeypatch.setattr(backfill_unknown_metrics, "write_backup", remeasure)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] == 12, "erased a real measurement"
    assert row["lines_added"] == 7
    assert row["raw_metrics"] == "API time spent: 12s"
    assert "Cleared 0 row(s)" in capsys.readouterr().out


def test_raw_metrics_never_disagrees_with_the_columns(db_path, logs):
    """The text summary and the columns describe the same session. If only one
    of the two writes lands, the report shows 'unknown' next to a 0 -- or a 0
    next to a NULL -- and there is no way to tell which is true."""
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] is None
    assert "API time spent: unknown" in row["raw_metrics"]
    assert "API time spent: 0s" not in row["raw_metrics"]


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


def test_missing_logs_flag_clears_rows_with_no_log_left(db_path, logs):
    """Opt-in: an all-zero row claims a zero session duration, which is not a
    thing a measured session has, so the row is fabricated on its own evidence
    even though the log that would prove it is gone."""
    add_session(db_path, "process-gone.log")

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply",
         "--missing-logs"])

    assert rc == 0
    row = read_row(db_path, "process-gone.log")
    assert row["lines_added"] is None
    assert row["session_time_seconds"] is None
    assert "Total code changes: unknown" in row["raw_metrics"]


def test_missing_logs_flag_still_spares_measured_sessions(db_path, logs):
    """The flag widens which *unmeasured* rows are cleared, nothing else."""
    add_session(db_path, "process-kept.log")
    (logs / "process-kept.log").write_text(
        '{"kind": "session_shutdown"}', encoding="utf-8")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply",
         "--missing-logs"])

    assert read_row(db_path, "process-kept.log")["lines_added"] == 0


def test_missing_logs_flag_still_spares_partly_measured_rows(db_path, logs):
    add_session(db_path, "process-gone.log", sess=1800)

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply",
         "--missing-logs"])

    assert read_row(db_path, "process-gone.log")["session_time_seconds"] == 1800


def test_a_widened_second_run_keeps_the_first_backup(db_path, logs):
    """Run two clears rows run one did not, so its backup is not a superset."""
    add_session(db_path, "process-nolog.log")
    add_session(db_path, "process-haslog.log")
    (logs / "process-haslog.log").write_text("no shutdown here",
                                             encoding="utf-8")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])
    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply",
         "--missing-logs"])

    first = db_path.with_name(db_path.name + ".bak-prezero")
    second = db_path.with_name(db_path.name + ".bak-prezero.2")
    assert second.exists(), "second run must not reuse the first backup name"
    assert read_row(first, "process-haslog.log")["lines_added"] == 0, (
        "the first backup is the only record of what run one cleared")
    assert read_row(second, "process-haslog.log")["lines_added"] is None
    assert read_row(second, "process-nolog.log")["lines_added"] == 0


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
