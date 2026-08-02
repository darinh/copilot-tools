"""End-to-end integration tests against a real terminal multiplexer.

Skipped automatically when no multiplexer is installed, so the unit suite still
runs everywhere. These are the tests that actually prove the Windows process
model works: a real psmux/tmux session runs the real runner, which spawns a
real child and records metrics for that exact process.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

import operator_ingest
from conftest import make_log
from operator_mux import Mux, MuxSessionError

MUX = Mux()
# These are the only tests allowed near the machine's real multiplexer;
# conftest's _no_real_multiplexer makes the spawn raise for everything else.
pytestmark = [
    pytest.mark.real_multiplexer,
    pytest.mark.skipif(not MUX.available(), reason="no terminal multiplexer installed"),
]

SESSION_PREFIX = "optest-"


def _session_name() -> str:
    return f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"


@pytest.fixture
def session():
    name = _session_name()
    yield name
    try:
        MUX.kill_session(name)
    except Exception:
        pass


def _wait(predicate, timeout=30.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ── backend behavior ────────────────────────────────────────────
def test_create_query_and_kill(session, tmp_path):
    MUX.new_session(session, str(tmp_path),
                    [sys.executable, "-c", "import time; time.sleep(60)"])
    assert MUX.has_session(session)
    assert session in MUX.list_sessions()
    assert MUX.kill_session(session) is True
    assert _wait(lambda: not MUX.has_session(session), timeout=10)


def test_session_survives_the_creating_process(session, tmp_path):
    """The whole point of a multiplexer: the session outlives its launcher."""
    MUX.new_session(session, str(tmp_path),
                    [sys.executable, "-c", "import time; time.sleep(30)"])
    assert MUX.has_session(session)
    pid = MUX.pane_pid(session)
    assert pid and pid > 0


def test_working_directory_with_spaces_is_preserved(session, tmp_path):
    workdir = tmp_path / "dir with spaces"
    workdir.mkdir()
    MUX.new_session(session, str(workdir),
                    [sys.executable, "-c", "import time; time.sleep(30)"])
    assert _wait(lambda: MUX.pane_current_path(session) is not None, timeout=15)
    reported = MUX.pane_current_path(session)
    assert reported is not None
    assert Path(reported).resolve() == workdir.resolve()


def test_unsafe_session_name_is_rejected_loudly(tmp_path):
    """psmux returns exit 0 while creating nothing for names containing ':'.
    The abstraction must surface that rather than pretend it worked."""
    bad = f"{SESSION_PREFIX}bad:name"
    try:
        with pytest.raises(MuxSessionError):
            MUX.new_session(bad, str(tmp_path), [sys.executable, "-c", "pass"])
    finally:
        MUX.kill_session(bad)


def test_remain_on_exit_keeps_session_after_program_exits(session, tmp_path):
    # The program has to outlive the set_remain_on_exit call: a program that
    # has already exited takes its pane (and the session) with it, so the
    # option would be set on nothing and the pane could never report dead.
    MUX.new_session(session, str(tmp_path),
                    [sys.executable, "-c", "import time; time.sleep(5)"])
    assert _wait(lambda: MUX.has_session(session), timeout=10)
    MUX.set_remain_on_exit(session, True)
    assert _wait(lambda: MUX.pane_dead(session), timeout=30)
    assert MUX.has_session(session)


# ── runner integration ──────────────────────────────────────────
def test_runner_in_real_session_records_pid_and_metrics(session, tmp_path):
    """The defect this design exists to fix.

    On Windows the pane PID is the multiplexer's own shell, so PID-based log
    lookup silently fails. Here the runner reports the PID of the process it
    actually launched, and that PID is what the log is matched on.
    """
    state_dir = tmp_path / "restart"
    state_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    db = tmp_path / "metrics.db"
    instance = "itest"

    # The child writes a Copilot-shaped log named after its OWN pid, then exits.
    child = tmp_path / "fake_copilot.py"
    child.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, r'''" + str(Path(__file__).resolve().parent) + "''')\n"
        "from conftest import make_log\n"
        "from pathlib import Path\n"
        f"logs = Path(r'''{logs}''')\n"
        "make_log(logs / f'process-{int(time.time()*1000)}-{os.getpid()}.log')\n"
        "time.sleep(2)\n",
        encoding="utf-8",
    )

    spec = {
        "instance": instance,
        "argv": [sys.executable, str(child)],
        "cwd": str(tmp_path),
        "session_num": 11,
        "state_dir": str(state_dir),
        "metrics_db": str(db),
        "copilot_log_dir": str(logs),
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    runner = Path(__file__).resolve().parent.parent / "operator_runner.py"
    MUX.new_session(session, str(tmp_path),
                    [sys.executable, str(runner), str(spec_path)])

    pid_file = state_dir / f"{instance}.pid"
    assert _wait(pid_file.exists, timeout=30), "runner never published a PID"
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())

    pane_pid = MUX.pane_pid(session)
    assert child_pid > 0
    # On Windows these differ (pane is the mux's shell); on POSIX they may not.
    # Either way, correctness must not depend on them being equal.
    assert isinstance(pane_pid, int)

    exit_file = state_dir / f"{instance}.exit"
    assert _wait(exit_file.exists, timeout=60), "runner never recorded an exit"
    assert exit_file.read_text(encoding="utf-8").strip() == "0"

    operator_ingest.init_db(db)
    with operator_ingest.connect(db) as conn:
        rows = conn.execute(
            "SELECT session_num, no_op, premium_requests FROM sessions"
        ).fetchall()
    assert len(rows) == 1, "metrics were not captured for the launched process"
    assert rows[0]["session_num"] == 11
    assert rows[0]["no_op"] == 0


def test_runner_captures_metrics_after_detach(session, tmp_path):
    """Metrics must land even though no operator process is watching.

    The bash implementation printed "metrics will be captured when copilot
    exits" and then exited, so nothing captured them. The supervisor lives
    inside the session, so detaching changes nothing.
    """
    state_dir = tmp_path / "restart"
    state_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    db = tmp_path / "metrics.db"

    child = tmp_path / "slow_child.py"
    child.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, r'''" + str(Path(__file__).resolve().parent) + "''')\n"
        "from conftest import make_log\n"
        "from pathlib import Path\n"
        f"logs = Path(r'''{logs}''')\n"
        "make_log(logs / f'process-{int(time.time()*1000)}-{os.getpid()}.log')\n"
        "time.sleep(6)\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "instance": "detached",
        "argv": [sys.executable, str(child)],
        "cwd": str(tmp_path),
        "session_num": 2,
        "state_dir": str(state_dir),
        "metrics_db": str(db),
        "copilot_log_dir": str(logs),
    }), encoding="utf-8")

    runner = Path(__file__).resolve().parent.parent / "operator_runner.py"
    MUX.new_session(session, str(tmp_path),
                    [sys.executable, str(runner), str(spec_path)])

    # Never attach at all — the strongest form of "detached".
    assert _wait((state_dir / "detached.exit").exists, timeout=60)
    with operator_ingest.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE no_op = 0").fetchone()[0] == 1


def test_concurrent_instances_do_not_cross_contaminate(tmp_path):
    """Two instances finishing together must each record their own usage."""
    logs = tmp_path / "logs"
    logs.mkdir()
    db = tmp_path / "metrics.db"
    state_dir = tmp_path / "restart"
    state_dir.mkdir()
    runner = Path(__file__).resolve().parent.parent / "operator_runner.py"

    child = tmp_path / "child.py"
    child.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, r'''" + str(Path(__file__).resolve().parent) + "''')\n"
        "from conftest import make_log\n"
        "from pathlib import Path\n"
        f"logs = Path(r'''{logs}''')\n"
        "make_log(logs / f'process-{int(time.time()*1000)}-{os.getpid()}.log')\n"
        "time.sleep(1)\n",
        encoding="utf-8",
    )

    names = []
    try:
        for idx in (1, 2):
            name = _session_name()
            names.append(name)
            spec_path = tmp_path / f"spec{idx}.json"
            spec_path.write_text(json.dumps({
                "instance": f"inst{idx}",
                "argv": [sys.executable, str(child)],
                "cwd": str(tmp_path),
                "session_num": idx * 10,
                "state_dir": str(state_dir),
                "metrics_db": str(db),
                "copilot_log_dir": str(logs),
            }), encoding="utf-8")
            MUX.new_session(name, str(tmp_path),
                            [sys.executable, str(runner), str(spec_path)])

        assert _wait(
            lambda: all((state_dir / f"inst{i}.exit").exists() for i in (1, 2)),
            timeout=90,
        )
        with operator_ingest.connect(db) as conn:
            nums = sorted(
                r["session_num"] for r in
                conn.execute("SELECT session_num FROM sessions WHERE no_op = 0")
            )
        # Each runner attributed its own log; neither stole the other's.
        assert nums == [10, 20]
    finally:
        for name in names:
            MUX.kill_session(name)
