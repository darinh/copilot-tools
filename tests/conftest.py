"""Shared test fixtures for copilot-tools tests."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a temporary directory."""
    return tmp_path


@pytest.fixture
def metrics_db(tmp_path):
    """Create a metrics database with schema."""
    db_path = tmp_path / "test-metrics.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_num INTEGER NOT NULL,
            log_file TEXT UNIQUE,
            log_file_mtime TEXT,
            no_op INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            work_dir TEXT,
            git_branch TEXT,
            premium_requests INTEGER,
            api_time_seconds INTEGER,
            session_time_seconds INTEGER,
            lines_added INTEGER,
            lines_removed INTEGER,
            raw_metrics TEXT
        );
        CREATE TABLE IF NOT EXISTS model_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id),
            model_name TEXT NOT NULL,
            tokens_in TEXT,
            tokens_out TEXT,
            tokens_cached TEXT,
            premium_requests INTEGER
        );
    """)
    conn.close()
    return db_path


@pytest.fixture
def sample_shutdown_event():
    """Return a sample session_shutdown telemetry event."""
    return {
        "kind": "session_shutdown",
        "properties": {
            "shutdown_type": "routine",
            "current_model": "claude-opus-4.6-1m",
            "model_claude-opus-4.6-1m_input_tokens": "500000",
            "model_claude-opus-4.6-1m_output_tokens": "10000",
            "model_claude-opus-4.6-1m_cache_read_tokens": "400000",
            "model_claude-opus-4.6-1m_cache_write_tokens": "0",
            "model_claude-opus-4.6-1m_request_count": "15",
            "model_claude-opus-4.6-1m_request_cost": "75",
        },
        "metrics": {
            "total_premium_requests": 75,
            "total_api_duration_ms": 120000,
            "session_duration_ms": 600000,
            "lines_added": 150,
            "lines_removed": 30,
        }
    }


@pytest.fixture
def sample_log_with_shutdown(tmp_path, sample_shutdown_event):
    """Create a sample log file containing a shutdown event."""
    logfile = tmp_path / "process-1234567890-12345.log"
    lines = []
    # Opening timestamp line
    lines.append("2026-04-07T10:00:00.000Z [INFO] Starting copilot session")
    # Some filler content
    for i in range(10):
        lines.append(f"2026-04-07T10:0{i}:00.000Z [DEBUG] Processing turn {i}")
    # assistant_usage events
    usage_event = {
        "kind": "assistant_usage",
        "properties": {
            "model": "claude-opus-4.6-1m",
        },
        "metrics": {
            "cost": 5.0,
        }
    }
    for i in range(15):
        lines.append(f"2026-04-07T10:{10+i:02d}:00.000Z [INFO] [Telemetry] cli.telemetry:")
        lines.append(json.dumps(usage_event, indent=2))
    # The shutdown event
    lines.append("2026-04-07T10:30:00.000Z [INFO] [Telemetry] cli.telemetry:")
    lines.append(json.dumps(sample_shutdown_event, indent=2))
    # Final line
    lines.append("2026-04-07T10:30:01.000Z [INFO] Session ended")

    logfile.write_text('\n'.join(lines))
    return logfile


@pytest.fixture
def sample_log_without_shutdown(tmp_path):
    """Create a sample log file without a shutdown event."""
    logfile = tmp_path / "process-9999999999-99999.log"
    lines = [
        "2026-04-07T10:00:00.000Z [INFO] Starting copilot session",
        "2026-04-07T10:01:00.000Z [DEBUG] Processing turn 1",
        "2026-04-07T10:02:00.000Z [DEBUG] Processing turn 2",
        "2026-04-07T10:03:00.000Z [INFO] Session ended",
    ]
    logfile.write_text('\n'.join(lines))
    return logfile


@pytest.fixture
def sample_log_with_quoted_shutdown(tmp_path, sample_shutdown_event):
    """Create a log where 'session_shutdown' appears in quoted content AND as real event."""
    logfile = tmp_path / "process-1111111111-11111.log"
    lines = []
    lines.append("2026-04-07T10:00:00.000Z [INFO] Starting")
    # False positive: session_shutdown inside a content string
    content_with_false_positive = {
        "role": "assistant",
        "content": 'The session_shutdown event is parsed by extract_shutdown_event function. '
                   'It looks for "kind": "session_shutdown" in the log.',
    }
    lines.append(json.dumps(content_with_false_positive, indent=2))
    # Real shutdown event
    lines.append("2026-04-07T10:30:00.000Z [INFO] [Telemetry] cli.telemetry:")
    lines.append(json.dumps(sample_shutdown_event, indent=2))
    lines.append("2026-04-07T10:30:01.000Z [INFO] Done")
    logfile.write_text('\n'.join(lines))
    return logfile
