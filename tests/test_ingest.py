"""Tests for operator_ingest.py — the log parser."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Import the ingest module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import operator_ingest


class TestFmtTokens:
    def test_small_number(self):
        assert operator_ingest.fmt_tokens(500) == "500"

    def test_thousands(self):
        assert operator_ingest.fmt_tokens(1500) == "1.5k"

    def test_millions(self):
        assert operator_ingest.fmt_tokens(1500000) == "1.5M"

    def test_exact_thousand(self):
        assert operator_ingest.fmt_tokens(1000) == "1.0k"

    def test_exact_million(self):
        assert operator_ingest.fmt_tokens(1000000) == "1.0M"

    def test_zero(self):
        assert operator_ingest.fmt_tokens(0) == "0"

    def test_string_input(self):
        assert operator_ingest.fmt_tokens("2500") == "2.5k"


class TestSqlEsc:
    def test_no_quotes(self):
        assert operator_ingest.sql_esc("hello") == "hello"

    def test_single_quote(self):
        assert operator_ingest.sql_esc("it's") == "it''s"

    def test_multiple_quotes(self):
        assert operator_ingest.sql_esc("it's a 'test'") == "it''s a ''test''"


class TestReadFirstLine:
    def test_normal_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("first line\nsecond line\nthird line")
        result = operator_ingest.read_first_line(str(f))
        assert result.strip() == "first line"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = operator_ingest.read_first_line(str(f))
        assert result == ""

    def test_single_line(self, tmp_path):
        f = tmp_path / "single.txt"
        f.write_text("only line")
        result = operator_ingest.read_first_line(str(f))
        assert result.strip() == "only line"


class TestReadLastLine:
    def test_normal_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("first\nsecond\nthird")
        result = operator_ingest.read_last_line(str(f))
        assert result == "third"

    def test_trailing_newline(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("first\nsecond\nthird\n")
        result = operator_ingest.read_last_line(str(f))
        assert result == "third"

    def test_single_line(self, tmp_path):
        f = tmp_path / "single.txt"
        f.write_text("only line")
        result = operator_ingest.read_last_line(str(f))
        assert result == "only line"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = operator_ingest.read_last_line(str(f))
        assert result == ""


class TestReadHeadBytes:
    def test_read_limited(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("a" * 1000)
        result = operator_ingest.read_head_bytes(str(f), 100)
        assert len(result) == 100

    def test_read_more_than_file(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("short")
        result = operator_ingest.read_head_bytes(str(f), 1000)
        assert result == "short"


class TestExtractShutdownEvent:
    def test_finds_shutdown_event(self, sample_log_with_shutdown):
        event = operator_ingest.extract_shutdown_event(str(sample_log_with_shutdown))
        assert event is not None
        assert event['kind'] == 'session_shutdown'
        assert 'metrics' in event
        assert event['metrics']['total_premium_requests'] == 75

    def test_no_shutdown_event(self, sample_log_without_shutdown):
        event = operator_ingest.extract_shutdown_event(str(sample_log_without_shutdown))
        assert event is None

    def test_ignores_quoted_shutdown(self, sample_log_with_quoted_shutdown):
        """Should find the real event, not the one inside a content string."""
        event = operator_ingest.extract_shutdown_event(str(sample_log_with_quoted_shutdown))
        assert event is not None
        assert event['kind'] == 'session_shutdown'
        assert 'metrics' in event

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.log"
        f.write_text("")
        event = operator_ingest.extract_shutdown_event(str(f))
        assert event is None


class TestExtractPremiumFromUsage:
    def test_extracts_usage(self, sample_log_with_shutdown):
        models, total = operator_ingest.extract_premium_from_usage(str(sample_log_with_shutdown))
        assert total == 75  # 15 events * 5.0 cost each
        assert 'claude-opus-4.6-1m' in models
        assert models['claude-opus-4.6-1m']['calls'] == 15

    def test_no_usage_events(self, sample_log_without_shutdown):
        models, total = operator_ingest.extract_premium_from_usage(str(sample_log_without_shutdown))
        assert total == 0
        assert len(models) == 0


class TestRunSqlite:
    def test_insert_and_select(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")
        conn.close()

        operator_ingest.run_sqlite(str(db), "INSERT INTO test VALUES (1, 'hello')", fetch=False)
        result = operator_ingest.run_sqlite(str(db), "SELECT name FROM test WHERE id = 1")
        assert result == "hello"


class TestEndToEnd:
    def test_ingest_with_shutdown(self, sample_log_with_shutdown, metrics_db):
        """Full end-to-end test: ingest a log and check DB."""
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / 'operator_ingest.py'),
             str(sample_log_with_shutdown), str(metrics_db), '--force'],
            capture_output=True, text=True, timeout=30)
        assert r.returncode == 0
        assert 'OK' in r.stdout

        conn = sqlite3.connect(str(metrics_db))
        sessions = conn.execute("SELECT * FROM sessions WHERE no_op = 0").fetchall()
        assert len(sessions) == 1

        models = conn.execute("SELECT * FROM model_usage").fetchall()
        assert len(models) >= 1
        conn.close()

    def test_ingest_without_shutdown(self, sample_log_without_shutdown, metrics_db):
        """Log without shutdown should exit code 2 and create no_op record."""
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / 'operator_ingest.py'),
             str(sample_log_without_shutdown), str(metrics_db), '--force'],
            capture_output=True, text=True, timeout=30)
        assert r.returncode == 2
        assert 'SKIP' in r.stdout

        conn = sqlite3.connect(str(metrics_db))
        sessions = conn.execute("SELECT * FROM sessions WHERE no_op = 1").fetchall()
        assert len(sessions) == 1
        conn.close()

    def test_idempotent_ingest(self, sample_log_with_shutdown, metrics_db):
        """Running ingest twice with --force should update, not duplicate."""
        for _ in range(2):
            subprocess.run(
                [sys.executable, str(Path(__file__).parent.parent / 'operator_ingest.py'),
                 str(sample_log_with_shutdown), str(metrics_db), '--force'],
                capture_output=True, text=True, timeout=30)

        conn = sqlite3.connect(str(metrics_db))
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == 1
        conn.close()
