"""Tests for copilot_operator.py — the cross-platform operator."""
import os
import platform
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import copilot_operator


class TestSqlEscape:
    def test_no_quotes(self):
        assert copilot_operator.sql_escape("hello") == "hello"

    def test_single_quote(self):
        assert copilot_operator.sql_escape("it's") == "it''s"


class TestExtractAgentFromArgs:
    def test_equals_syntax(self):
        assert copilot_operator.extract_agent_from_args(
            ['--agent=myagent', '--yolo']) == 'myagent'

    def test_space_syntax(self):
        assert copilot_operator.extract_agent_from_args(
            ['--agent', 'myagent', '--yolo']) == 'myagent'

    def test_default(self):
        assert copilot_operator.extract_agent_from_args(['--yolo']) == 'anvil:anvil'


class TestInstance:
    def test_name_prefix(self):
        inst = copilot_operator.Instance('test')
        assert inst.name == 'operator-copilot-test'

    def test_name_already_prefixed(self):
        inst = copilot_operator.Instance('operator-copilot-test')
        assert inst.name == 'operator-copilot-test'

    def test_paths(self):
        inst = copilot_operator.Instance('mytest')
        assert inst.restart_marker.name == 'operator-copilot-mytest'
        assert inst.state_file.name == 'operator-copilot-mytest.state'

    def test_save_and_load_state(self, tmp_path):
        inst = copilot_operator.Instance('statetest')
        inst.state_file = tmp_path / 'test.state'
        inst.save_state(5, '2026-01-01T00:00:00Z')
        state = inst.load_state()
        assert state['SESSION_NUM'] == '5'
        assert state['RUN_STARTED'] == '2026-01-01T00:00:00Z'

    def test_load_no_state(self, tmp_path):
        inst = copilot_operator.Instance('nostate')
        inst.state_file = tmp_path / 'nonexistent.state'
        assert inst.load_state() is None

    def test_cleanup(self, tmp_path):
        inst = copilot_operator.Instance('cleantest')
        inst.run_script = tmp_path / 'run.sh'
        inst.restart_marker = tmp_path / 'marker'
        inst.run_script.write_text('#!/bin/bash')
        inst.restart_marker.write_text('')
        inst.cleanup()
        assert not inst.run_script.exists()
        assert not inst.restart_marker.exists()


class TestBuildPreamble:
    def test_contains_agent_name(self):
        inst = copilot_operator.Instance('test')
        preamble = copilot_operator.build_preamble('anvil:anvil', inst)
        assert 'anvil:anvil' in preamble
        assert str(inst.restart_marker) in preamble

    def test_contains_key_instructions(self):
        inst = copilot_operator.Instance('test')
        preamble = copilot_operator.build_preamble('test-agent', inst)
        assert 'blanket human approval' in preamble
        assert 'session handoff' in preamble


class TestInitMetricsDb:
    def test_creates_tables(self, tmp_path):
        db_path = tmp_path / 'test.db'
        with patch.object(copilot_operator, 'METRICS_DB', db_path), \
             patch.object(copilot_operator, 'COPILOT_DIR', tmp_path):
            copilot_operator.init_metrics_db()
        conn = sqlite3.connect(str(db_path))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        assert 'sessions' in tables
        assert 'model_usage' in tables
        conn.close()

    def test_idempotent(self, tmp_path):
        db_path = tmp_path / 'test.db'
        with patch.object(copilot_operator, 'METRICS_DB', db_path), \
             patch.object(copilot_operator, 'COPILOT_DIR', tmp_path):
            copilot_operator.init_metrics_db()
            copilot_operator.init_metrics_db()  # should not error


class TestFindCopilotLog:
    def test_find_by_pid(self, tmp_path):
        with patch.object(copilot_operator, 'COPILOT_LOG_DIR', tmp_path):
            log1 = tmp_path / 'process-111-100.log'
            log2 = tmp_path / 'process-222-200.log'
            log1.write_text('test')
            log2.write_text('test')
            result = copilot_operator.find_copilot_log(pid='200')
            assert result.name == 'process-222-200.log'

    def test_fallback_most_recent(self, tmp_path):
        with patch.object(copilot_operator, 'COPILOT_LOG_DIR', tmp_path):
            log1 = tmp_path / 'process-111-100.log'
            log2 = tmp_path / 'process-222-200.log'
            log1.write_text('old')
            import time
            time.sleep(0.05)
            log2.write_text('new')
            result = copilot_operator.find_copilot_log()
            assert result.name == 'process-222-200.log'

    def test_no_logs(self, tmp_path):
        with patch.object(copilot_operator, 'COPILOT_LOG_DIR', tmp_path):
            assert copilot_operator.find_copilot_log() is None

    def test_nonexistent_dir(self, tmp_path):
        with patch.object(copilot_operator, 'COPILOT_LOG_DIR', tmp_path / 'nope'):
            assert copilot_operator.find_copilot_log() is None


class TestShellQuote:
    def test_simple_string(self):
        assert copilot_operator._shell_quote("hello") == "hello"

    def test_string_with_spaces(self):
        result = copilot_operator._shell_quote("hello world")
        assert result == "'hello world'"

    def test_empty_string(self):
        result = copilot_operator._shell_quote("")
        assert result == "''"


class TestGenerateRunScript:
    def test_generates_script(self, tmp_path):
        inst = copilot_operator.Instance('test')
        inst.run_script = tmp_path / 'run.sh'
        copilot_operator.generate_run_script(inst, ['--yolo', '--agent', 'anvil:anvil'])
        assert inst.run_script.exists()
        content = inst.run_script.read_text()
        assert 'copilot' in content
        assert '--yolo' in content

    def test_generates_with_preamble(self, tmp_path):
        inst = copilot_operator.Instance('test')
        inst.run_script = tmp_path / 'run.sh'
        copilot_operator.generate_run_script(inst, ['--yolo'], preamble='hello world')
        content = inst.run_script.read_text()
        assert 'PREAMBLE' in content

    @pytest.mark.skipif(platform.system() == 'Windows', reason="Unix permissions")
    def test_script_executable(self, tmp_path):
        inst = copilot_operator.Instance('test')
        inst.run_script = tmp_path / 'run.sh'
        copilot_operator.generate_run_script(inst, ['--yolo'])
        assert os.access(inst.run_script, os.X_OK)


class TestCheckForRestartSignal:
    def test_no_signal(self, tmp_path):
        inst = copilot_operator.Instance('test')
        inst.restart_marker = tmp_path / 'marker'
        assert not copilot_operator.check_for_restart_signal(inst)

    def test_signal_present(self, tmp_path):
        inst = copilot_operator.Instance('test')
        inst.restart_marker = tmp_path / 'marker'
        inst.restart_marker.write_text('')
        assert copilot_operator.check_for_restart_signal(inst)


class TestReportMetrics:
    def test_summary(self, tmp_path, capsys):
        db_path = tmp_path / 'test.db'
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_num INTEGER NOT NULL, log_file TEXT UNIQUE,
                log_file_mtime TEXT, no_op INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
                work_dir TEXT, git_branch TEXT,
                premium_requests INTEGER, api_time_seconds INTEGER,
                session_time_seconds INTEGER, lines_added INTEGER,
                lines_removed INTEGER, raw_metrics TEXT
            );
            CREATE TABLE model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL, model_name TEXT NOT NULL,
                tokens_in TEXT, tokens_out TEXT, tokens_cached TEXT,
                premium_requests INTEGER
            );
            INSERT INTO sessions (session_num, log_file, started_at, ended_at,
                premium_requests, api_time_seconds, session_time_seconds,
                lines_added, lines_removed)
            VALUES (1, 'test.log', '2026-04-07T10:00:00Z', '2026-04-07T10:30:00Z',
                50, 120, 1800, 100, 20);
        """)
        conn.close()

        with patch.object(copilot_operator, 'METRICS_DB', db_path):
            copilot_operator.report_metrics('summary')

        captured = capsys.readouterr()
        assert '50' in captured.out  # premium requests
        assert 'Usage Summary' in captured.out
