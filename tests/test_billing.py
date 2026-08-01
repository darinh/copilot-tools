"""Tests for AI-credit (token-based) billing.

GitHub replaced premium requests with AI credits on 2026-06-01. Usage is
metered on token consumption and reported by the CLI as nano-AIU.

The reference values here come from a real Copilot CLI session captured on
2026-07-28: 20,242,875,000 nano-AIU, which the CLI displayed as
"AI Credits 20.2".
"""
from __future__ import annotations

import json

import pytest

import copilot_operator as op
import operator_ingest

REAL_NANO_AIU = 20_242_875_000
REAL_CREDITS = 20.242875


_RESPONSE_SEQ = iter(range(1, 1_000_000))


def usage_response(
    model: str = "claude-opus-5",
    nano_aiu: int = REAL_NANO_AIU,
    input_tokens: int = 2,
    cache_read: int = 0,
    cache_write: int = 32371,
    output_tokens: int = 4,
    response_id: str | None = None,
) -> str:
    """A chat-completion response body shaped like the real CLI logs.

    Each call gets a distinct id by default, matching the real CLI. Pass
    ``response_id`` explicitly to simulate a duplicated body.
    """
    body = {
        "id": response_id or f"msg_{next(_RESPONSE_SEQ)}",
        "object": "chat.completion",
        "model": model,
        "usage": {
            "prompt_tokens": input_tokens + cache_write + cache_read,
            "completion_tokens": output_tokens,
        },
        "copilot_usage": {
            "token_details": [
                {"batch_size": 1000000, "cost_per_batch": 500000000000,
                 "token_count": input_tokens, "token_type": "input"},
                {"batch_size": 1000000, "cost_per_batch": 50000000000,
                 "token_count": cache_read, "token_type": "cache_read"},
                {"batch_size": 1000000, "cost_per_batch": 625000000000,
                 "token_count": cache_write, "token_type": "cache_write"},
                {"batch_size": 1000000, "cost_per_batch": 2500000000000,
                 "token_count": output_tokens, "token_type": "output"},
            ],
            "total_nano_aiu": nano_aiu,
        },
    }
    return (
        "2026-07-28T15:13:24.399Z [DEBUG] response:\n"
        "2026-07-28T15:13:24.399Z [DEBUG] data:\n"
        + json.dumps(body, indent=2) + "\n"
    )


# ── conversion ──────────────────────────────────────────────────
def test_nano_aiu_converts_to_credits():
    """Verified against a real session the CLI reported as 'AI Credits 20.2'."""
    assert operator_ingest.credits_from_nano(REAL_NANO_AIU) == pytest.approx(
        REAL_CREDITS, abs=1e-6
    )


def test_one_credit_is_one_cent():
    assert operator_ingest.usd_from_nano(operator_ingest.NANO_AIU_PER_CREDIT) == \
        pytest.approx(0.01)


def test_dollars_from_real_session():
    assert operator_ingest.usd_from_nano(REAL_NANO_AIU) == pytest.approx(0.2024, abs=1e-4)


def test_zero_and_none_are_safe():
    assert operator_ingest.credits_from_nano(0) == 0
    assert operator_ingest.credits_from_nano(None) == 0


# ── extraction ──────────────────────────────────────────────────
def test_extracts_credits_and_tokens():
    usage = operator_ingest.extract_ai_credit_usage(usage_response())
    assert usage["nano_aiu"] == REAL_NANO_AIU
    assert usage["calls"] == 1
    assert usage["tokens"]["input"] == 2
    assert usage["tokens"]["output"] == 4
    assert usage["tokens"]["cache_write"] == 32371
    assert usage["tokens"]["cache_read"] == 0


def test_sums_across_multiple_api_calls():
    """A session makes many calls; each response carries its own usage."""
    text = usage_response(nano_aiu=1_000_000_000) + usage_response(nano_aiu=2_500_000_000)
    usage = operator_ingest.extract_ai_credit_usage(text)
    assert usage["nano_aiu"] == 3_500_000_000
    assert usage["calls"] == 2
    assert operator_ingest.credits_from_nano(usage["nano_aiu"]) == pytest.approx(3.5)


def test_duplicate_response_body_is_not_double_counted():
    """The same payload echoed twice must not inflate reported usage."""
    body = usage_response(nano_aiu=1_000_000_000, response_id="msg_dup")
    usage = operator_ingest.extract_ai_credit_usage(body + body)
    assert usage["nano_aiu"] == 1_000_000_000
    assert usage["calls"] == 1


def test_attributes_usage_per_model():
    text = (usage_response(model="claude-opus-5", nano_aiu=2_000_000_000)
            + usage_response(model="gpt-5.4", nano_aiu=1_000_000_000))
    usage = operator_ingest.extract_ai_credit_usage(text)
    assert usage["models"]["claude-opus-5"]["nano_aiu"] == 2_000_000_000
    assert usage["models"]["gpt-5.4"]["nano_aiu"] == 1_000_000_000


def test_legacy_cost_appears_in_periodic_columns(tmp_path, monkeypatch, capsys):
    """A legacy premium-billed session dated today must show in today's cost,
    not only in the all-time column — otherwise the report claims $0.00 for
    real spend and the printed footnote is false."""
    db = tmp_path / "m.db"
    monkeypatch.setattr(op, "METRICS_DB", db)
    operator_ingest.init_db(db)
    with operator_ingest.connect(db) as c:
        c.execute(
            "INSERT INTO sessions (session_num, log_file, no_op, started_at,"
            " ended_at, nano_aiu, premium_requests)"
            " VALUES (1,'legacy.log',0,datetime('now'),datetime('now'),0,10)"
        )
    with operator_ingest.connect(db) as c:
        today = "date(ended_at,'localtime')=date('now','localtime')"
        usd = c.execute(
            f"SELECT {op._usd(today)} FROM sessions WHERE no_op=0").fetchone()[0]
    assert usd == pytest.approx(0.40), "10 premium requests at $0.04 = $0.40"


def test_log_without_usage_yields_zero():
    usage = operator_ingest.extract_ai_credit_usage("just some log text\n")
    assert usage["nano_aiu"] == 0
    assert usage["calls"] == 0


# ── ingestion ───────────────────────────────────────────────────
def test_ingest_records_credits_without_a_shutdown_event(tmp_path, db_path):
    """Current Copilot no longer writes a shutdown payload to the log, so
    credit usage alone must be enough to record a session."""
    log = tmp_path / "process-1700000000000-4242.log"
    log.write_text(usage_response(), encoding="utf-8")

    result = operator_ingest.ingest_file(log, db_path, session_num=1)
    assert result.startswith("OK")
    assert "AI credits" in result

    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions").fetchone()
    assert row["no_op"] == 0
    assert row["nano_aiu"] == REAL_NANO_AIU
    assert row["tokens_input"] == 2
    assert row["tokens_cache_write"] == 32371


def test_ingest_stores_per_model_credits(tmp_path, db_path):
    log = tmp_path / "process-1700000000000-1.log"
    log.write_text(
        usage_response(model="claude-opus-5", nano_aiu=2_000_000_000)
        + usage_response(model="gpt-5.4", nano_aiu=1_000_000_000),
        encoding="utf-8",
    )
    operator_ingest.ingest_file(log, db_path)
    with operator_ingest.connect(db_path) as conn:
        rows = {r["model_name"]: r["nano_aiu"] for r in
                conn.execute("SELECT model_name, nano_aiu FROM model_usage")}
    assert rows["claude-opus-5"] == 2_000_000_000
    assert rows["gpt-5.4"] == 1_000_000_000


def test_ingest_stores_per_model_cache_write_tokens(tmp_path, db_path):
    """Cache-write is the most expensive token type per million, so a
    per-model breakdown that omits it cannot explain where credits went.

    The parser has always computed this figure; it was collected into the
    per-model entry and then dropped on the floor because ``model_usage`` had
    no column to bind it to.
    """
    log = tmp_path / "process-1700000000000-2.log"
    log.write_text(
        usage_response(model="claude-opus-5", cache_write=32371)
        + usage_response(model="gpt-5.4", cache_write=1_500_000),
        encoding="utf-8",
    )
    operator_ingest.ingest_file(log, db_path)
    with operator_ingest.connect(db_path) as conn:
        rows = {r["model_name"]: r["tokens_cache_write"] for r in
                conn.execute(
                    "SELECT model_name, tokens_cache_write FROM model_usage")}
    assert rows["claude-opus-5"] == 32371
    assert rows["gpt-5.4"] == 1_500_000


def test_per_model_cache_write_stays_summable(tmp_path, db_path):
    """Its three sibling columns hold fmt_tokens() output, where SUM() reads
    "32.4k" as 32 and reports a plausible, wrong total. This column is for
    auditing spend, so it stores the integer."""
    log = tmp_path / "process-1700000000000-5.log"
    log.write_text(
        usage_response(model="claude-opus-5", cache_write=32371)
        + usage_response(model="gpt-5.4", cache_write=1_500_000),
        encoding="utf-8",
    )
    operator_ingest.ingest_file(log, db_path)
    with operator_ingest.connect(db_path) as conn:
        total = conn.execute(
            "SELECT SUM(tokens_cache_write) FROM model_usage").fetchone()[0]
    assert total == 1_532_371


def test_per_model_cache_write_is_not_confused_with_cache_read(tmp_path, db_path):
    """Two adjacent columns holding the same number prove nothing. These are
    billed at 12.5x different rates, so they must come from different fields."""
    log = tmp_path / "process-1700000000000-3.log"
    log.write_text(
        usage_response(model="claude-opus-5", cache_read=4000, cache_write=7000),
        encoding="utf-8",
    )
    operator_ingest.ingest_file(log, db_path)
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute(
            "SELECT tokens_cached, tokens_cache_write FROM model_usage"
        ).fetchone()
    assert row["tokens_cached"] == "4.0k"
    assert row["tokens_cache_write"] == 7000


def test_legacy_premium_logs_still_ingest(tmp_path, db_path):
    """Annual plans stayed on premium requests, so old logs must still work."""
    from conftest import make_log

    log = make_log(tmp_path / "process-1700000000000-9.log")
    result = operator_ingest.ingest_file(log, db_path)
    assert result.startswith("OK")
    with operator_ingest.connect(db_path) as conn:
        row = conn.execute("SELECT premium_requests, nano_aiu FROM sessions").fetchone()
    assert row["premium_requests"] > 0
    assert row["nano_aiu"] == 0


def test_schema_migrates_an_existing_database(tmp_path):
    """A user's history from before the billing change must survive."""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_num INTEGER NOT NULL, log_file TEXT UNIQUE, started_at TEXT NOT NULL, "
        "ended_at TEXT NOT NULL, no_op INTEGER NOT NULL DEFAULT 0, "
        "premium_requests INTEGER);"
        "CREATE TABLE model_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id INTEGER NOT NULL, model_name TEXT NOT NULL);"
        "INSERT INTO sessions (session_num, log_file, started_at, ended_at, "
        "premium_requests) VALUES (1, 'old.log', 'x', 'y', 42);"
    )
    conn.commit()
    conn.close()

    operator_ingest.init_db(db)

    with operator_ingest.connect(db) as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
        model_cols = {r["name"] for r in c.execute("PRAGMA table_info(model_usage)")}
        row = c.execute("SELECT premium_requests, nano_aiu FROM sessions").fetchone()
    assert {"nano_aiu", "tokens_input", "tokens_output"} <= cols
    assert {"nano_aiu", "tokens_cache_write"} <= model_cols
    assert row["premium_requests"] == 42, "existing history must be preserved"
    assert row["nano_aiu"] == 0


def test_migrated_database_accepts_a_per_model_insert(tmp_path):
    """Adding a column to the schema string is only half a migration.

    An existing database is upgraded by ALTER TABLE, and the INSERT names its
    columns explicitly, so a column present in ``SCHEMA`` but absent from
    ``_ADDED_COLUMNS`` would make every ingest fail against a real user's
    database while passing every test that starts from a fresh one.
    """
    import sqlite3

    db = tmp_path / "old2.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        # The pre-billing-change schema: SCHEMA minus every column listed in
        # _ADDED_COLUMNS, which is exactly what a user's database looks like.
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_num INTEGER NOT NULL, log_file TEXT UNIQUE, "
        "no_op INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, "
        "ended_at TEXT NOT NULL, work_dir TEXT, git_branch TEXT, "
        "premium_requests INTEGER, api_time_seconds INTEGER, "
        "session_time_seconds INTEGER, lines_added INTEGER, "
        "lines_removed INTEGER, raw_metrics TEXT);"
        "CREATE TABLE model_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id INTEGER NOT NULL, model_name TEXT NOT NULL, "
        "tokens_in TEXT, tokens_out TEXT, tokens_cached TEXT, "
        "premium_requests INTEGER);"
    )
    conn.commit()
    conn.close()

    log = tmp_path / "process-1700000000000-4.log"
    log.write_text(usage_response(cache_write=9000), encoding="utf-8")
    assert operator_ingest.ingest_file(log, db).startswith("OK")

    with operator_ingest.connect(db) as c:
        assert c.execute(
            "SELECT tokens_cache_write FROM model_usage").fetchone()[0] == 9000


# ── launch behavior ─────────────────────────────────────────────
def test_operator_forces_debug_logging(monkeypatch):
    """At the default log level Copilot writes no usage data at all, so the
    metrics pipeline would silently record nothing."""
    monkeypatch.delenv("COPILOT_OPERATOR_NO_DEBUG_LOG", raising=False)
    argv = op._ensure_usage_logging(["copilot", "--yolo"])
    assert argv[-2:] == ["--log-level", "debug"]


def test_user_log_level_is_respected(monkeypatch):
    monkeypatch.delenv("COPILOT_OPERATOR_NO_DEBUG_LOG", raising=False)
    argv = op._ensure_usage_logging(["copilot", "--log-level", "info"])
    assert argv.count("--log-level") == 1


def test_debug_logging_can_be_opted_out(monkeypatch):
    monkeypatch.setenv("COPILOT_OPERATOR_NO_DEBUG_LOG", "1")
    argv = op._ensure_usage_logging(["copilot", "--yolo"])
    assert "--log-level" not in argv


def test_equals_form_log_level_is_respected(monkeypatch):
    monkeypatch.delenv("COPILOT_OPERATOR_NO_DEBUG_LOG", raising=False)
    argv = op._ensure_usage_logging(["copilot", "--log-level=info"])
    assert argv == ["copilot", "--log-level=info"]


def test_injected_flag_goes_after_the_preamble(monkeypatch):
    """The preamble is the value of -i. Appending must not land between -i and
    its value, which would hand Copilot the wrong prompt."""
    monkeypatch.delenv("COPILOT_OPERATOR_NO_DEBUG_LOG", raising=False)
    argv = op._ensure_usage_logging(["copilot", "--yolo", "-i", "a long preamble"])
    assert argv[argv.index("-i") + 1] == "a long preamble"
    assert argv[-2:] == ["--log-level", "debug"]


# ── cost SQL ────────────────────────────────────────────────────
def _seed(db, rows):
    operator_ingest.init_db(db)
    with operator_ingest.connect(db) as c:
        for name, nano, premium in rows:
            c.execute(
                "INSERT INTO sessions (session_num, log_file, no_op, started_at,"
                " ended_at, nano_aiu, premium_requests) VALUES (1,?,0,?,?,?,?)",
                (name, "2026-07-28T10:00:00Z", "2026-07-28T10:00:00Z", nano, premium),
            )


def test_mixed_legacy_and_credit_rows_cost_correctly(tmp_path, monkeypatch):
    """History spans the billing change. A row carrying both signals must be
    costed once, on credits — not counted under both schemes."""
    db = tmp_path / "m.db"
    monkeypatch.setattr(op, "METRICS_DB", db)
    _seed(db, [
        ("new.log", 20_000_000_000, 0),   # 20 credits -> $0.20
        ("old.log", 0, 10),               # 10 premium -> $0.40
        ("both.log", 5_000_000_000, 7),   # 5 credits  -> $0.05, premium ignored
    ])
    with operator_ingest.connect(db) as c:
        credits = c.execute(
            f"SELECT {op._credits()} FROM sessions WHERE no_op=0").fetchone()[0]
        usd = c.execute(
            f"SELECT {op._usd()} FROM sessions WHERE no_op=0").fetchone()[0]
    assert credits == pytest.approx(25.0)
    assert usd == pytest.approx(0.65)


def test_large_credit_sums_stay_exact(tmp_path, monkeypatch):
    """nano_aiu is ~2e10 per session; a long run must not lose precision."""
    db = tmp_path / "m.db"
    monkeypatch.setattr(op, "METRICS_DB", db)
    _seed(db, [(f"s{i}.log", REAL_NANO_AIU, 0) for i in range(2000)])
    with operator_ingest.connect(db) as c:
        total = c.execute("SELECT SUM(nano_aiu) FROM sessions").fetchone()[0]
    assert total == 2000 * REAL_NANO_AIU


def test_every_report_renders_with_credit_data(tmp_path, monkeypatch, capsys):
    db = tmp_path / "m.db"
    monkeypatch.setattr(op, "METRICS_DB", db)
    _seed(db, [("a.log", REAL_NANO_AIU, 0), ("b.log", 0, 5)])
    for kind in ("summary", "sessions", "models", "projects", "costs", "tokens"):
        assert op.report_metrics(kind) == 0, f"{kind} report failed"
        capsys.readouterr()


# ── log management ──────────────────────────────────────────────
def _make_logs(log_dir, names, age_days=0):
    import os
    import time

    log_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for name in names:
        p = log_dir / name
        p.write_text("x" * 1024, encoding="utf-8")
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(p, (old, old))
        made.append(p)
    return made


def test_logs_reports_size_without_deleting(tmp_path, monkeypatch, capsys):
    logs = tmp_path / "logs"
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", logs)
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "m.db")
    _make_logs(logs, ["process-1-1.log", "process-2-2.log"])
    assert op.manage_logs([]) == 0
    assert len(list(logs.glob("*.log"))) == 2, "reporting must not delete"
    assert "files: 2" in capsys.readouterr().out


def test_prune_keeps_logs_not_yet_ingested(tmp_path, monkeypatch, capsys):
    """Logs are the only record of usage. Pruning one that was never ingested
    would destroy data the user has not captured."""
    logs = tmp_path / "logs"
    db = tmp_path / "m.db"
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", logs)
    monkeypatch.setattr(op, "METRICS_DB", db)
    _make_logs(logs, ["process-1-1.log", "process-2-2.log"], age_days=60)
    _seed(db, [("process-1-1.log", REAL_NANO_AIU, 0)])   # only the first ingested

    assert op.manage_logs(["--prune", "--days", "30"]) == 0
    remaining = {p.name for p in logs.glob("*.log")}
    assert remaining == {"process-2-2.log"}
    assert "not yet ingested" in capsys.readouterr().out


def test_prune_leaves_recent_logs(tmp_path, monkeypatch, capsys):
    logs = tmp_path / "logs"
    db = tmp_path / "m.db"
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", logs)
    monkeypatch.setattr(op, "METRICS_DB", db)
    _make_logs(logs, ["process-1-1.log"], age_days=1)
    _seed(db, [("process-1-1.log", REAL_NANO_AIU, 0)])
    assert op.manage_logs(["--prune", "--days", "30"]) == 0
    assert len(list(logs.glob("*.log"))) == 1


def test_logs_handles_empty_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "nope")
    assert op.manage_logs([]) == 0
    assert "No Copilot logs" in capsys.readouterr().out
