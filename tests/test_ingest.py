"""Tests for the pure-Python log parser and metrics store."""
from __future__ import annotations

import sqlite3

import pytest

import operator_ingest
from conftest import make_log


def test_init_db_creates_schema(db_path):
    operator_ingest.init_db(db_path)
    with operator_ingest.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "model_usage"} <= tables


def test_connect_sets_busy_timeout(db_path):
    """Concurrent instances share one database, so waiting beats failing."""
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000


def test_connect_closes_the_connection(db_path):
    """sqlite3's own `with` manages the transaction but does NOT close, so a
    bare connection would leak a handle on every report and ingest call."""
    with operator_ingest.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS probe(a)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_connect_rolls_back_on_error(db_path):
    operator_ingest.init_db(db_path)
    with pytest.raises(RuntimeError):
        with operator_ingest.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO sessions (session_num, log_file, started_at, ended_at) "
                "VALUES (1, 'rollback.log', 'x', 'y')"
            )
            raise RuntimeError("boom")
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE log_file='rollback.log'"
        ).fetchone()[0] == 0


def test_cost_lookahead_does_not_cross_into_next_event():
    """A malformed usage record must not borrow the next event's cost, which
    would silently misreport billing."""
    text = (
        '{"kind": "assistant_usage",\n'
        '  "model": "model-a"\n'          # no cost for this event
        '}\n'
        '{"kind": "assistant_usage",\n'
        '  "model": "model-b",\n'
        '  "cost": 5.0\n'
        '}\n'
    )
    models, total = operator_ingest.extract_premium_from_usage(text)
    assert total == 5
    assert "model-a" not in models
    assert models["model-b"]["cost"] == 5.0


def test_cost_before_model_is_still_counted():
    """Field order within an event must not matter."""
    text = (
        '{"kind": "assistant_usage",\n'
        '  "cost": 4.0,\n'
        '  "model": "model-a"\n'
        '}\n'
        '{"kind": "assistant_usage",\n'
        '  "cost": 3.0,\n'
        '  "model": "model-b"\n'
        '}\n'
    )
    models, total = operator_ingest.extract_premium_from_usage(text)
    assert total == 7
    assert models["model-a"]["cost"] == 4.0
    assert models["model-b"]["cost"] == 3.0


def test_shutdown_event_survives_braces_inside_strings():
    """A '}' in a string value must not be read as structure; treating it as
    one drops the event and silently discards the session's metrics."""
    text = (
        '2026 [telemetry] {\n'
        '  "note": "a } brace { inside a string",\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {},\n'
        '  "metrics": {"total_premium_requests": 9, "lines_added": 1, "lines_removed": 0}\n'
        '}\n'
    )
    event = operator_ingest.extract_shutdown_event(text)
    assert event is not None
    assert event["metrics"]["lines_added"] == 1


def test_shutdown_marker_inside_a_log_message_is_not_mistaken_for_the_event():
    text = (
        '2026 [info] {"msg": "waiting for the session_shutdown event"}\n'
        '2026 [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {},\n'
        '  "metrics": {"total_premium_requests": 3, "lines_added": 2, "lines_removed": 1}\n'
        '}\n'
    )
    event = operator_ingest.extract_shutdown_event(text)
    assert event is not None
    assert event["metrics"]["total_premium_requests"] == 3


def test_truncated_log_returns_none_rather_than_raising():
    text = '2026 [telemetry] {\n  "kind": "session_shutdown",\n  "metrics": {"lines_added": 1'
    assert operator_ingest.extract_shutdown_event(text) is None


def test_windows_path_with_brace_in_shutdown_event():
    text = (
        '{\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {"path": "C:/a{b/c"},\n'
        '  "metrics": {"lines_added": 5, "lines_removed": 0}\n'
        '}\n'
    )
    event = operator_ingest.extract_shutdown_event(text)
    assert event is not None
    assert event["metrics"]["lines_added"] == 5


def test_multiple_usage_events_are_summed_not_double_counted():
    text = "".join(
        '{"kind": "assistant_usage",\n  "model": "m",\n  "cost": 2.0\n}\n'
        for _ in range(3)
    )
    models, total = operator_ingest.extract_premium_from_usage(text)
    assert total == 6
    assert models["m"]["calls"] == 3


def test_ingest_records_metrics(tmp_path, db_path):
    log = make_log(tmp_path / "process-1700000000000-4242.log")
    result = operator_ingest.ingest_file(log, db_path, session_num=3)
    assert result.startswith("OK")
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions").fetchone()
    assert row["session_num"] == 3
    assert row["no_op"] == 0
    assert row["api_time_seconds"] == 120
    assert row["session_time_seconds"] == 1800
    assert row["lines_added"] == 10
    assert row["lines_removed"] == 2


def test_premium_requests_sum_usage_events(tmp_path, db_path):
    """total_premium_requests reports the last call only; costs must be summed."""
    log = make_log(
        tmp_path / "process-1700000000000-1.log",
        premium_calls=(("m1", 2.0), ("m1", 3.0), ("m2", 1.0)),
    )
    operator_ingest.ingest_file(log, db_path)
    with operator_ingest.connect(db_path) as conn:
        total = conn.execute("SELECT premium_requests FROM sessions").fetchone()[0]
    assert total == 6


def test_ingest_is_idempotent(tmp_path, db_path):
    log = make_log(tmp_path / "process-1700000000000-7.log")
    operator_ingest.ingest_file(log, db_path)
    second = operator_ingest.ingest_file(log, db_path)
    assert "SKIP" in second
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_force_reprocesses(tmp_path, db_path):
    log = make_log(tmp_path / "process-1700000000000-8.log")
    operator_ingest.ingest_file(log, db_path)
    assert operator_ingest.ingest_file(log, db_path, force=True).startswith("OK")


def test_model_usage_rows_replaced_not_duplicated(tmp_path, db_path):
    log = make_log(tmp_path / "process-1700000000000-9.log")
    operator_ingest.ingest_file(log, db_path)
    operator_ingest.ingest_file(log, db_path, force=True)
    with operator_ingest.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT model_name, COUNT(*) c FROM model_usage GROUP BY model_name"
        ).fetchall()
    assert all(r["c"] == 1 for r in rows)


def test_log_without_shutdown_event_recorded_as_noop(tmp_path, db_path):
    log = tmp_path / "process-1700000000000-11.log"
    log.write_text("2026-07-27T10:00:00.000Z [info] nothing here\n", encoding="utf-8")
    result = operator_ingest.ingest_file(log, db_path)
    assert "no shutdown event" in result
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute("SELECT no_op FROM sessions").fetchone()[0] == 1


def test_non_ascii_log_is_decoded_as_utf8(tmp_path, db_path):
    """Windows defaults to the locale code page, which would corrupt the JSON
    and silently discard the session's metrics."""
    log = make_log(
        tmp_path / "process-1700000000000-12.log",
        extra_text='2026-07-27T10:00:01.000Z [info] emoji \U0001f600 caf\u00e9 \u4e2d\u6587\n',
    )
    assert operator_ingest.ingest_file(log, db_path).startswith("OK")
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE no_op = 0").fetchone()[0] == 1


def test_values_are_bound_not_interpolated(tmp_path, db_path):
    """A quote-laden working directory must not break or inject SQL."""
    nasty = "/tmp/it's \"quoted\"; DROP TABLE sessions;--"
    log = make_log(tmp_path / "process-1700000000000-13.log", cwd=nasty)
    operator_ingest.ingest_file(log, db_path, work_dir=nasty)
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT work_dir FROM sessions").fetchone()
        assert row["work_dir"] == nasty
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='sessions'").fetchone()


def test_ingest_all_processes_directory(tmp_path, db_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    make_log(logs / "process-1700000000000-1.log")
    make_log(logs / "process-1700000000001-2.log")
    (logs / "unrelated.txt").write_text("ignore me", encoding="utf-8")
    results = operator_ingest.ingest_all(logs, db_path)
    assert len(results) == 2
    with operator_ingest.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2


def test_ingest_all_on_missing_directory_is_empty(tmp_path, db_path):
    assert operator_ingest.ingest_all(tmp_path / "nope", db_path) == []


def test_missing_file_raises(tmp_path, db_path):
    with pytest.raises(FileNotFoundError):
        operator_ingest.ingest_file(tmp_path / "absent.log", db_path)


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (999, "999"), (1000, "1.0k"), (24000, "24.0k"), (1500000, "1.5M"),
])
def test_fmt_tokens(value, expected):
    assert operator_ingest.fmt_tokens(value) == expected
