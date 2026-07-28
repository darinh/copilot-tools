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


def usage_response(
    model: str = "claude-opus-5",
    nano_aiu: int = REAL_NANO_AIU,
    input_tokens: int = 2,
    cache_read: int = 0,
    cache_write: int = 32371,
    output_tokens: int = 4,
) -> str:
    """A chat-completion response body shaped like the real CLI logs."""
    body = {
        "id": "msg_test",
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


def test_attributes_usage_per_model():
    text = (usage_response(model="claude-opus-5", nano_aiu=2_000_000_000)
            + usage_response(model="gpt-5.4", nano_aiu=1_000_000_000))
    usage = operator_ingest.extract_ai_credit_usage(text)
    assert usage["models"]["claude-opus-5"]["nano_aiu"] == 2_000_000_000
    assert usage["models"]["gpt-5.4"]["nano_aiu"] == 1_000_000_000


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
        row = c.execute("SELECT premium_requests, nano_aiu FROM sessions").fetchone()
    assert {"nano_aiu", "tokens_input", "tokens_output"} <= cols
    assert row["premium_requests"] == 42, "existing history must be preserved"
    assert row["nano_aiu"] == 0


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
