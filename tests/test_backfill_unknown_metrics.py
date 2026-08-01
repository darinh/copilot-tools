"""Tests for the one-off repair that turns fabricated zeros into NULLs.

The script rewrites a user's real metrics database, so the property that
matters most is the one it must *not* do: clear values that a shutdown event
actually measured. Every test here pins one side of that decision.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

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


def test_the_summary_and_the_columns_land_together(db_path, logs):
    """Both writes are one transaction, so a row can never be left with a
    '0s' summary next to a NULL column.

    Named for what it actually pins. It does NOT prove the TOCTOU fix -- it
    passes against the old non-transactional code too, because nothing here
    interleaves a competing writer. ``test_a_row_remeasured_mid_run_is_not_
    erased`` is the test that discriminates.
    """
    add_session(db_path, "process-1.log")
    (logs / "process-1.log").write_text("no shutdown here", encoding="utf-8")

    backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] is None
    assert "API time spent: unknown" in row["raw_metrics"]
    assert "API time spent: 0s" not in row["raw_metrics"]


def test_a_log_that_gains_a_shutdown_event_mid_run_is_not_erased(
        db_path, logs, capsys, monkeypatch):
    """The verdict has two halves and the write must re-check both.

    All-zero on its own is not evidence: a short session really can measure 0s
    of API time, a 0-second duration and no line changes -- that is exactly
    what ``test_measured_zeros_are_left_alone`` pins. So if the log acquires a
    shutdown event after the scan read it, those zeros have become measured
    data, and clearing them on the strength of the zero test alone destroys
    the very thing the log check exists to protect.
    """
    add_session(db_path, "process-1.log")
    log = logs / "process-1.log"
    log.write_text("no shutdown here", encoding="utf-8")

    original = backfill_unknown_metrics.write_backup

    def measure(conn, dest):
        original(conn, dest)
        log.write_text('{"kind": "session_shutdown"}', encoding="utf-8")

    monkeypatch.setattr(backfill_unknown_metrics, "write_backup", measure)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] == 0, "erased zeros a shutdown event backs"
    assert row["raw_metrics"] == ZEROED_RAW
    assert "Cleared 0 row(s)" in capsys.readouterr().out


def test_a_shutdown_event_added_within_one_clock_tick_is_still_caught(
        db_path, logs, capsys, monkeypatch):
    """The mtime half of the fingerprint cannot be relied on alone.

    Windows' system clock ticks around every 15ms, far slower than a file can
    be rewritten, so a log modified immediately after the scan can carry the
    scan's own timestamp. Pinning the timestamp to a fixed value makes that
    collision certain instead of occasional -- the size half is what has to
    catch it.
    """
    import os

    add_session(db_path, "process-1.log")
    log = logs / "process-1.log"
    log.write_text("no shutdown here", encoding="utf-8")
    frozen = log.stat().st_mtime_ns

    original = backfill_unknown_metrics.write_backup

    def measure(conn, dest):
        original(conn, dest)
        log.write_text('{"kind": "session_shutdown"}', encoding="utf-8")
        os.utime(log, ns=(frozen, frozen))
        assert log.stat().st_mtime_ns == frozen, (
            "test precondition: the timestamp collision must be real")

    monkeypatch.setattr(backfill_unknown_metrics, "write_backup", measure)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    assert read_row(db_path, "process-1.log")["api_time_seconds"] == 0
    assert "Cleared 0 row(s)" in capsys.readouterr().out


def test_a_log_that_gains_its_event_during_the_scan_read_is_not_erased(
        db_path, logs, capsys, monkeypatch):
    """The fingerprint has to be taken before the evidence is read, not after.

    Reading the log is the slow part of the scan, so it is the likeliest
    moment for a live operator to append the shutdown event. Fingerprinting
    after the read records the file in its NEW state while the verdict
    reflects the OLD one -- the two agree at write time, and the check that
    exists to catch precisely this waves it through.
    """
    add_session(db_path, "process-1.log")
    log = logs / "process-1.log"
    log.write_text("no shutdown here", encoding="utf-8")

    def read_and_grow(path):
        path.read_text(encoding="utf-8", errors="replace")
        path.write_text('{"kind": "session_shutdown"}', encoding="utf-8")
        return False  # what the read saw: no event, a moment too early

    monkeypatch.setattr(
        backfill_unknown_metrics, "log_has_shutdown_event", read_and_grow)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] == 0, (
        "erased a row whose log grew its shutdown event mid-read")
    assert row["raw_metrics"] == ZEROED_RAW
    assert "Cleared 0 row(s)" in capsys.readouterr().out


class _Traced:
    """A connection that records the transaction verbs it is asked to run.

    Lets a test assert the *shape* of the locking -- where transactions open
    and close relative to the file probes -- rather than a proxy for it.
    """

    def __init__(self, conn, trace):
        self._conn = conn
        self.trace = trace

    def execute(self, sql, *args):
        verb = sql.strip().split()[0].upper()
        if verb in ("BEGIN", "COMMIT", "ROLLBACK"):
            self.trace.append(verb)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _trace_run(db_path, logs, monkeypatch, argv, on_begin=None):
    """Run the repair, recording probes and transaction verbs in one order.

    ``on_begin`` fires as each row's transaction opens -- the moment a live
    operator that held the lock has just let go of it.
    """
    trace = []
    real_fingerprint = backfill_unknown_metrics.log_fingerprint
    real_read = backfill_unknown_metrics.log_has_shutdown_event
    real_connect = backfill_unknown_metrics.connect

    class Hooked(_Traced):
        def execute(self, sql, *args):
            if on_begin is not None and sql.strip().upper().startswith("BEGIN"):
                on_begin()
            return super().execute(sql, *args)

    def stat(path):
        trace.append("STAT")
        return real_fingerprint(path)

    def read(path):
        trace.append("READ")
        return real_read(path)

    monkeypatch.setattr(backfill_unknown_metrics, "log_fingerprint", stat)
    monkeypatch.setattr(
        backfill_unknown_metrics, "log_has_shutdown_event", read)
    monkeypatch.setattr(backfill_unknown_metrics, "connect",
                        lambda db: Hooked(real_connect(db), trace))

    rc = backfill_unknown_metrics.main(argv)
    return rc, trace


def _segments(trace):
    """The probes inside each transaction, one list per transaction."""
    out = []
    current = None
    for event in trace:
        if event == "BEGIN":
            assert current is None, "a transaction opened inside another one"
            current = []
        elif event in ("COMMIT", "ROLLBACK"):
            assert current is not None, f"{event} with no transaction open"
            out.append(current)
            current = None
        elif current is not None:
            current.append(event)
    assert current is None, "a transaction was left open"
    return out


def test_the_write_lock_holds_one_stat_and_never_a_log_read(db_path, logs,
                                                            monkeypatch):
    """A repair must not become an outage, and must not check stale evidence.

    Two constraints pull against each other and both are real. Reading every
    log inside one transaction blocks the live operator for the length of the
    whole scan, which on a loaded disk outlasts the 15s it waits. But checking
    the log *before* opening the transaction is worse than useless: acquiring
    the lock can itself block for that same 15s, so the verdict is stale by
    the time it is acted on.

    The shape that satisfies both: read logs with no transaction open, then
    per row take the lock and re-stat that one log inside it. Microseconds
    held, nothing trusted across the wait.
    """
    add_session(db_path, "process-1.log")
    add_session(db_path, "process-2.log")
    for name in ("process-1.log", "process-2.log"):
        (logs / name).write_text("no shutdown here", encoding="utf-8")

    rc, trace = _trace_run(
        db_path, logs, monkeypatch,
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    assert "READ" in trace, "test precondition: no log was ever read"
    assert "BEGIN" in trace, "test precondition: no transaction was opened"

    segments = _segments(trace)
    assert len(segments) == 2, (
        f"expected one transaction per row, got {len(segments)}: {trace}")
    for probes in segments:
        assert probes == ["STAT"], (
            f"a transaction did more than stat its own log: {probes}")

    assert "READ" not in trace[trace.index("BEGIN"):], (
        "a log was read while the write lock was held")


def test_a_log_that_gains_its_event_while_the_lock_is_awaited_is_not_erased(
        db_path, logs, capsys, monkeypatch):
    """``BEGIN IMMEDIATE`` can wait 15 seconds. Evidence cannot be older.

    The process this repair contends with for the write lock is the operator,
    and the operator is precisely the thing that appends a shutdown event and
    re-ingests the row. So the interleaving is not exotic, it is the expected
    one: check the log, block on the lock, and by the time the lock is granted
    the log has grown the event and the row has been measured -- legitimately
    to all zeros, because short sessions do that -- so the zero guard still
    matches and a genuinely measured row is erased.

    Simulated here by growing the log exactly as each transaction opens: the
    write lock has just been released by whoever appended it.
    """
    add_session(db_path, "process-1.log")
    log = logs / "process-1.log"
    log.write_text("no shutdown here", encoding="utf-8")

    fired = []

    def operator_finishes_the_session():
        fired.append(True)
        log.write_text(
            'no shutdown here\n{"kind": "session_shutdown"}', encoding="utf-8")

    rc, trace = _trace_run(
        db_path, logs, monkeypatch,
        ["--db", str(db_path), "--logs", str(logs), "--apply"],
        on_begin=operator_finishes_the_session)

    assert rc == 0
    assert fired, "test precondition: no transaction ever opened"
    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] == 0, (
        "erased a row whose log grew its shutdown event during the lock wait")
    assert row["raw_metrics"] == ZEROED_RAW
    assert "Cleared 0 row(s)" in capsys.readouterr().out


def test_an_unreadable_log_is_not_mistaken_for_a_deleted_one(
        db_path, logs, capsys, monkeypatch):
    """``--missing-logs`` clears on the strength of "the log is gone".

    A locked file, a dropped network mount or a permission error is not gone.
    Folding every ``OSError`` into the same "absent" answer lets a momentary
    I/O failure erase a session whose log is sitting on disk with a shutdown
    event in it -- and unlike a missing log, that one is provably measured.
    ``log_has_shutdown_event`` already treats an unreadable log as a reason to
    change nothing; the fingerprint has to agree with it.
    """
    add_session(db_path, "process-1.log")
    log = logs / "process-1.log"
    log.write_text('{"kind": "session_shutdown"}', encoding="utf-8")

    real_stat = Path.stat

    def denied(self, *args, **kwargs):
        if self.name == "process-1.log":
            raise PermissionError(13, "the file is locked by another process")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    assert backfill_unknown_metrics.log_fingerprint(log) == \
        backfill_unknown_metrics.UNREADABLE, (
        "test precondition: the stat was not actually denied")

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--missing-logs",
         "--apply"])

    assert rc == 0
    row = read_row(db_path, "process-1.log")
    assert row["api_time_seconds"] == 0, (
        "erased a measured row because its log could not be statted")
    assert row["raw_metrics"] == ZEROED_RAW
    assert "No fabricated zeros found" in capsys.readouterr().out


def test_a_genuinely_missing_log_is_still_cleared_with_the_flag(
        db_path, logs, capsys):
    """The other side of the same decision, so the fix above cannot pass by
    refusing to clear anything at all."""
    add_session(db_path, "process-gone.log")

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--missing-logs",
         "--apply"])

    assert rc == 0
    assert read_row(db_path, "process-gone.log")["api_time_seconds"] is None
    assert "Cleared 1 row(s)" in capsys.readouterr().out


def test_clears_more_rows_than_sqlite_takes_parameters(db_path, logs, capsys):
    """A user with a long history has more damaged rows than SQLite has
    parameter slots -- the limit is 999 before 3.32. Binding every id into one
    statement makes the repair fail precisely for the people who need it most.

    Honest caveat: this cannot fail on a modern SQLite, where the limit is
    32766, so locally it is a guard rather than a proof. It earns its place on
    the older interpreters this project still supports (>=3.10) and on any
    system SQLite that predates 3.32.
    """
    operator_ingest.init_db(db_path)
    with operator_ingest.connect(db_path) as conn:
        for n in range(1200):
            name = f"process-bulk-{n}.log"
            conn.execute(
                """
                INSERT INTO sessions (session_num, log_file, started_at,
                                      ended_at, api_time_seconds,
                                      session_time_seconds, lines_added,
                                      lines_removed, raw_metrics)
                VALUES (?, ?, 'x', 'y', 0, 0, 0, 0, ?)
                """,
                (n, name, ZEROED_RAW))
            (logs / name).write_text("no shutdown here", encoding="utf-8")
        conn.commit()

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    assert "Cleared 1200 row(s)" in capsys.readouterr().out
    with operator_ingest.connect(db_path) as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE api_time_seconds = 0"
        ).fetchone()[0]
    assert remaining == 0


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


# --- Two generations of log_file key ---------------------------------------
#
# ``sessions.log_file`` held ``Path.name`` until the log-identity fix and now
# holds the full case-folded path (:func:`operator_ingest.log_key`). A real
# database carries both at once: legacy rows are re-keyed only when their log
# is ingested again *and* its ``started_at`` still matches, so a row whose log
# was deleted, or whose stored start time came from the wall clock, keeps its
# basename forever. This script joins whatever it finds onto ``--logs``, which
# is correct for both only because an absolute right-hand side replaces the
# join root rather than extending it. Nothing pinned that, and the direction
# it fails in is the one this whole script exists to avoid: judge a row
# against the wrong file and it erases measurements a shutdown event took.

NO_EVENT = "no shutdown here"
HAS_EVENT = '{"kind": "session_shutdown"}'


def test_both_key_generations_are_repaired_in_one_pass(db_path, logs, capsys):
    """The mixed database is the normal case, not an edge case."""
    legacy = logs / "process-legacy.log"
    modern = logs / "process-modern.log"
    legacy.write_text(NO_EVENT, encoding="utf-8")
    modern.write_text(NO_EVENT, encoding="utf-8")
    modern_key = operator_ingest.log_key(modern)
    assert modern_key != modern.name, (
        "test precondition: log_key must not be a basename")
    add_session(db_path, legacy.name)
    add_session(db_path, modern_key)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    assert read_row(db_path, legacy.name)["lines_added"] is None, (
        "a pre-fix basename row was left unrepaired")
    assert read_row(db_path, modern_key)["lines_added"] is None, (
        "a full-path row was left unrepaired")
    assert "Cleared 2 row(s)" in capsys.readouterr().out


def test_a_full_path_row_is_read_from_its_own_directory(db_path, logs,
                                                        tmp_path):
    """A full-path row need not name a log under ``--logs`` at all.

    That is the whole point of the key: a second log directory, a restored
    backup or another machine's logs copied in for comparison produce logs
    whose basenames repeat. Here the row's own log lacks the event while a
    same-named file under ``--logs`` has one, so a repair that reduced the key
    to its basename would read the decoy and spare a fabricated row.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    own = elsewhere / "process-7.log"
    own.write_text(NO_EVENT, encoding="utf-8")
    (logs / "process-7.log").write_text(HAS_EVENT, encoding="utf-8")
    key = operator_ingest.log_key(own)
    add_session(db_path, key)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    assert read_row(db_path, key)["lines_added"] is None, (
        "the row was judged against a same-named log it does not own")


def test_a_measured_full_path_row_is_not_erased_by_a_same_named_log(
        db_path, logs, tmp_path, capsys):
    """The same swap the other way round, which is the destructive direction.

    The row's own log carries the shutdown event, so its zeros are measured
    fact; the decoy under ``--logs`` does not. Reading the decoy would clear
    four columns a session actually recorded and rewrite its summary to say
    "unknown" -- data loss the first test cannot detect, because a test that
    only ever asserts rows *are* cleared passes on a script that clears
    everything.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    own = elsewhere / "process-8.log"
    own.write_text(HAS_EVENT, encoding="utf-8")
    (logs / "process-8.log").write_text(NO_EVENT, encoding="utf-8")
    key = operator_ingest.log_key(own)
    add_session(db_path, key)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    row = read_row(db_path, key)
    assert row["api_time_seconds"] == 0, (
        "erased a measured row by reading a same-named log in --logs")
    assert row["raw_metrics"] == ZEROED_RAW
    assert "No fabricated zeros found" in capsys.readouterr().out


def test_two_rows_sharing_a_basename_get_their_own_verdicts(db_path, logs,
                                                            tmp_path,
                                                            capsys):
    """The collision the full-path key exists to allow, now in one database.

    Before the key change these two sessions could not coexist -- ``log_file``
    is ``UNIQUE``, so the second silently overwrote the first. They coexist
    now, and the repair has to reach a separate verdict for each: one log has
    the event and one does not, and both answers are wrong if the basename is
    what gets looked up.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    unmeasured = logs / "process-9.log"
    measured = elsewhere / "process-9.log"
    unmeasured.write_text(NO_EVENT, encoding="utf-8")
    measured.write_text(HAS_EVENT, encoding="utf-8")
    unmeasured_key = operator_ingest.log_key(unmeasured)
    measured_key = operator_ingest.log_key(measured)
    assert unmeasured_key != measured_key, (
        "test precondition: the two keys must differ")
    add_session(db_path, unmeasured_key)
    add_session(db_path, measured_key)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply"])

    assert rc == 0
    assert read_row(db_path, unmeasured_key)["lines_added"] is None, (
        "the unmeasured row was spared by its namesake's shutdown event")
    assert read_row(db_path, measured_key)["lines_added"] == 0, (
        "the measured row was erased by its namesake's missing event")
    assert "Cleared 1 row(s)" in capsys.readouterr().out


def test_missing_logs_flag_asks_after_the_rows_own_path(db_path, logs,
                                                        tmp_path):
    """``--missing-logs`` clears on "the log is gone", and gone is per path.

    A surviving log that merely shares the basename is a different file. If
    the fingerprint were taken from ``--logs`` the row would look present and
    stay fabricated forever, which is the failure this flag exists to end.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    key = operator_ingest.log_key(elsewhere / "process-10.log")
    (logs / "process-10.log").write_text(HAS_EVENT, encoding="utf-8")
    add_session(db_path, key)

    rc = backfill_unknown_metrics.main(
        ["--db", str(db_path), "--logs", str(logs), "--apply",
         "--missing-logs"])

    assert rc == 0
    assert read_row(db_path, key)["lines_added"] is None, (
        "a row whose own log is gone was held present by a namesake")
