"""Unmeasured sessions must read as unknown in both report renderers.

The Python operator and `operator.sh` render the same database. If one of them
turns a NULL back into `0`, the platform a user happens to be on decides
whether they see a measurement that never happened, so both are pinned here.
"""
from __future__ import annotations

import sqlite3
import re
from pathlib import Path

import pytest

import copilot_operator
import operator_ingest

ROOT = Path(__file__).resolve().parent.parent
OPERATOR_SH = ROOT / "operator.sh"


def seed(db_path, *, measured: bool):
    """One non-no_op session, either measured or never measured."""
    operator_ingest.init_db(db_path)
    values = (120, 1800, 10, 2) if measured else (None, None, None, None)
    with operator_ingest.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_num, log_file, no_op, started_at,
                                  ended_at, work_dir, git_branch,
                                  api_time_seconds, session_time_seconds,
                                  lines_added, lines_removed)
            VALUES (1, 'process-1.log', 0, '2026-07-27T10:00:00Z',
                    '2026-07-27T10:30:00Z', '/home/dev/project', 'main',
                    ?, ?, ?, ?)
            """,
            values,
        )
        conn.commit()
    return db_path


def sh_sessions_sql() -> str:
    """The `report sessions` query as operator.sh actually runs it."""
    text = OPERATOR_SH.read_text(encoding="utf-8")
    start = text.index("SELECT session_num AS '#',")
    end = text.index("LIMIT 20;", start) + len("LIMIT 20")
    return text[start:end].replace("${home_esc}", "/home/dev")


def sh_run_summary_sql() -> str:
    """The run-summary aggregate as operator.sh actually runs it.

    `\\$` and `${OPERATOR_RUN_STARTED}` are bash escapes that the shell expands
    before sqlite3 ever sees them, so they are expanded here too.
    """
    text = OPERATOR_SH.read_text(encoding="utf-8")
    match = re.search(r"SELECT\s+COUNT\(\*\) AS sessions,.*?;", text, re.S)
    assert match, "run summary query not found in operator.sh"
    return (match.group(0).rstrip(";")
            .replace("${OPERATOR_RUN_STARTED}", "2026-01-01T00:00:00Z")
            .replace("\\$", "$"))


def run_sql(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(sql)
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))
    finally:
        conn.close()


def run_sh_query(db_path):
    return run_sql(db_path, sh_sessions_sql())


@pytest.fixture
def python_report(monkeypatch, capsys):
    def _run(db_path):
        monkeypatch.setattr(copilot_operator, "METRICS_DB", Path(db_path))
        assert copilot_operator.report_metrics("sessions") == 0
        return capsys.readouterr().out

    return _run


def test_python_report_shows_unknown_not_zero(db_path, python_report):
    out = python_report(seed(db_path, measured=False))

    assert "+0 -0" not in out, out
    assert "0m 0s" not in out, out
    assert out.count("—") >= 3, out


def test_python_report_still_shows_real_measurements(db_path, python_report):
    out = python_report(seed(db_path, measured=True))

    assert "+10 -2" in out, out
    assert "120s" in out, out
    assert "30m 0s" in out, out


def test_sh_report_shows_unknown_not_zero(db_path):
    row = run_sh_query(seed(db_path, measured=False))

    assert row["changes"] == "—", row
    assert row["api_time"] == "—", row
    assert row["sess_time"] == "—", row


def test_sh_report_still_shows_real_measurements(db_path):
    row = run_sh_query(seed(db_path, measured=True))

    assert row["changes"] == "+10 -2", row
    assert row["api_time"] == "120s", row
    assert row["sess_time"] == "30m 0s", row


def test_python_aggregates_do_not_invent_zero_totals(db_path, monkeypatch,
                                                     capsys):
    """A run of nothing but unmeasured sessions has no total, not a zero one."""
    monkeypatch.setattr(copilot_operator, "METRICS_DB",
                        Path(seed(db_path, measured=False)))

    copilot_operator.show_run_summary("2026-01-01T00:00:00Z")
    run_out = capsys.readouterr().out
    assert copilot_operator.report_metrics("projects") == 0
    projects_out = capsys.readouterr().out

    assert "+0 -0" not in run_out, run_out
    assert "0s" not in run_out, run_out
    assert "0s" not in projects_out, projects_out


def test_python_aggregates_still_total_measured_sessions(db_path, monkeypatch,
                                                         capsys):
    monkeypatch.setattr(copilot_operator, "METRICS_DB",
                        Path(seed(db_path, measured=True)))

    copilot_operator.show_run_summary("2026-01-01T00:00:00Z")
    out = capsys.readouterr().out

    assert "+10 -2" in out, out
    assert "120s" in out, out


def test_sh_aggregates_do_not_invent_zero_totals(db_path):
    row = run_sql(seed(db_path, measured=False), sh_run_summary_sql())

    assert row["total_changes"] == "—", row
    assert row["total_api_time"] == "—", row
    assert row["total_sess_time"] == "—", row
    assert row["sessions"] == 1, row


def test_sh_aggregates_still_total_measured_sessions(db_path):
    row = run_sql(seed(db_path, measured=True), sh_run_summary_sql())

    assert row["total_changes"] == "+10 -2", row
    assert row["total_api_time"] == "120s", row
    assert row["total_sess_time"] == "30m 0s", row
