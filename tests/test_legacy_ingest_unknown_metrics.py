"""The legacy bash-path ingester must agree with the Python one on "unknown".

`operator-ingest.py` is retained for rollback and is only reachable from
`operator.sh`, but it writes into the *same* database, so if it still defaults
absent shutdown metrics to 0 it can reintroduce fabricated measurements that
the Python ingester and the backfill just removed.

Its `main()` shells out to grep/head/tail/sqlite3 and only runs on POSIX, so
what is exercised here is the value rendering that decides between a number
and NULL — the part that changed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import operator_ingest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def legacy():
    """Import the dash-named legacy module, which `import` cannot name."""
    spec = importlib.util.spec_from_file_location(
        "legacy_operator_ingest", ROOT / "operator-ingest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_absent_metric_is_unknown_not_zero(legacy):
    assert legacy.measured({}, "lines_added") is None
    assert legacy.measured({}, "total_api_duration_ms", 1000) is None


def test_present_zero_stays_zero(legacy):
    """A measured zero is a fact and must survive."""
    assert legacy.measured({"lines_added": 0}, "lines_added") == 0


def test_scaling_matches_the_python_ingester(legacy):
    assert legacy.measured(
        {"total_api_duration_ms": 120000}, "total_api_duration_ms", 1000) == 120


def test_null_reaches_sql_as_a_literal_not_the_string_none(legacy):
    """The legacy writer interpolates values straight into SQL text."""
    assert legacy.sql_num(None) == "NULL"
    assert legacy.sql_num(0) == "0"
    assert legacy.sql_num(120) == "120"


def test_half_known_line_counts_render_as_unknown(legacy):
    """`+5 -None` is worse than admitting the pair was never measured."""
    assert legacy.fmt_changes(5, None) == "unknown"
    assert legacy.fmt_changes(None, 5) == "unknown"
    assert legacy.fmt_changes(5, 2) == "+5 -2"


def test_python_ingester_also_refuses_half_known_line_counts(tmp_path,
                                                             db_path):
    """Same rule on the maintained path, checked through real ingestion."""
    log = tmp_path / "process-1700000000000-1.log"
    log.write_text(
        '2026-07-27T10:00:00.000Z [info] {"cwd": "/home/dev/p"}\n'
        '2026-07-27T10:30:00.000Z [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {},\n'
        '  "metrics": {"total_premium_requests": 1, "lines_added": 5}\n'
        '}\n'
        '2026-07-27T10:30:05.000Z [info] done\n',
        encoding="utf-8",
    )

    assert operator_ingest.ingest_file(log, db_path).startswith("OK")
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions").fetchone()

    assert row["lines_added"] == 5
    assert row["lines_removed"] is None
    assert "Total code changes: unknown" in row["raw_metrics"]
