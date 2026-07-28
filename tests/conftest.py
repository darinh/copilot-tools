"""Shared fixtures for the copilot-tools test suite."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "restart"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "metrics.db"


def make_log(
    path: Path,
    *,
    premium_calls=((("claude-opus-4.6", 3.0),) * 1),
    lines_added: int = 10,
    lines_removed: int = 2,
    cwd: str = "/home/dev/project",
    extra_text: str = "",
) -> Path:
    """Write a synthetic Copilot process log containing a shutdown event."""
    header = (
        '2026-07-27T10:00:00.000Z [info] starting\n'
        f'2026-07-27T10:00:00.100Z [info] {{"cwd": "{cwd}"}}\n'
    )
    usage_blocks = []
    for model, cost in premium_calls:
        usage_blocks.append(
            '2026-07-27T10:01:00.000Z [telemetry] {\n'
            '  "kind": "assistant_usage",\n'
            f'  "model": "{model}",\n'
            f'  "cost": {cost}\n'
            '}\n'
        )
    shutdown = (
        '2026-07-27T10:30:00.000Z [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {\n'
        '    "model_claude-opus-4.6_input_tokens": "1500000",\n'
        '    "model_claude-opus-4.6_output_tokens": "24000",\n'
        '    "model_claude-opus-4.6_cache_read_tokens": "900",\n'
        '    "model_claude-opus-4.6_request_count": "7"\n'
        '  },\n'
        '  "metrics": {\n'
        '    "total_premium_requests": 1,\n'
        '    "total_api_duration_ms": 120000,\n'
        '    "session_duration_ms": 1800000,\n'
        f'    "lines_added": {lines_added},\n'
        f'    "lines_removed": {lines_removed}\n'
        '  }\n'
        '}\n'
    )
    footer = '2026-07-27T10:30:05.000Z [info] done\n'
    path.write_text(
        header + extra_text + "".join(usage_blocks) + shutdown + footer,
        encoding="utf-8",
    )
    return path


@pytest.fixture
def launch_spec(tmp_path: Path, state_dir: Path, db_path: Path):
    def _make(argv, session_num=1, log_dir=None):
        spec = {
            "instance": "testinst",
            "argv": list(argv),
            "cwd": str(tmp_path),
            "session_num": session_num,
            "state_dir": str(state_dir),
            "metrics_db": str(db_path),
            "copilot_log_dir": str(log_dir or (tmp_path / "logs")),
        }
        path = tmp_path / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    return _make
