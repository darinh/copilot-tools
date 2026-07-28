#!/usr/bin/env python3
"""Stdlib-only cross-platform verification.

Exists because the target environment may have no pytest available. Exercises
the same behavior the pytest suite covers, including a real multiplexer
session, so the POSIX path is verified by execution rather than assumption.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FAILURES = []
PASSES = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def section(title):
    print(f"\n=== {title} ===")


def make_log(path, cwd="/tmp/project"):
    path.write_text(
        '2026-07-27T10:00:00.000Z [info] start\n'
        f'2026-07-27T10:00:00.100Z [info] {{"cwd": "{cwd}"}}\n'
        '2026-07-27T10:00:00.200Z [info] {"session_id": "3f2a9c1e-1111-2222-3333-444455556666"}\n'
        '2026-07-27T10:01:00.000Z [telemetry] {\n'
        '  "kind": "assistant_usage",\n'
        '  "model": "test-model",\n'
        '  "cost": 4.0\n'
        '}\n'
        '2026-07-27T10:30:00.000Z [telemetry] {\n'
        '  "kind": "session_shutdown",\n'
        '  "properties": {"model_test-model_input_tokens": "1000"},\n'
        '  "metrics": {"total_premium_requests": 1, "total_api_duration_ms": 5000,\n'
        '              "session_duration_ms": 60000, "lines_added": 3, "lines_removed": 1}\n'
        '}\n'
        '2026-07-27T10:30:05.000Z [info] done\n',
        encoding="utf-8",
    )
    return path


def main():
    print(f"Platform: {sys.platform}  Python: {sys.version.split()[0]}")

    section("imports")
    import operator_console
    import operator_ingest
    import operator_mux
    import operator_runner
    import handoff_tool
    import setup_tools
    os.environ.setdefault("COPILOT_OPERATOR_HOME", tempfile.mkdtemp())
    import copilot_operator
    check("all modules import", True)

    section("naming")
    from operator_mux import safe_instance_id, sanitize_name
    check("sanitize replaces dots", sanitize_name("a.b") == "a-b")
    check("sanitize replaces colons", sanitize_name("a:b") == "a-b")
    check("sanitize replaces slashes", sanitize_name("a/b") == "a-b")
    ids = {safe_instance_id(n) for n in ("a.b", "a:b", "a-b")}
    check("distinct names stay distinct", len(ids) == 3, f"got {ids}")
    check("plain name unchanged", safe_instance_id("frontend") == "frontend")
    check("reserved device name avoided", safe_instance_id("CON") != "CON")

    section("process tree (POSIX path)")
    parents = operator_runner._process_parents()
    check("parent map is populated", len(parents) > 0, f"len={len(parents)}")
    check("own pid present in map", os.getpid() in parents)
    tree = operator_runner._process_tree(os.getpid())
    check("process tree contains self", os.getpid() in tree)

    section("ingest")
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "metrics.db"
    log = make_log(tmp / "process-1700000000000-4242.log")
    result = operator_ingest.ingest_file(log, db, session_num=7)
    check("ingest reports OK", result.startswith("OK"), result)
    with operator_ingest.connect(db) as conn:
        row = conn.execute("SELECT * FROM sessions").fetchone()
    check("session_num stored", row["session_num"] == 7)
    check("premium summed from usage", row["premium_requests"] == 4,
          f"got {row['premium_requests']}")
    check("api time parsed", row["api_time_seconds"] == 5)
    check("lines parsed", row["lines_added"] == 3 and row["lines_removed"] == 1)
    check("idempotent", "SKIP" in operator_ingest.ingest_file(log, db))

    nasty = "/tmp/it's \"quoted\"; DROP TABLE sessions;--"
    log2 = make_log(tmp / "process-1700000000001-4243.log", cwd=nasty)
    operator_ingest.ingest_file(log2, db, work_dir=nasty)
    with operator_ingest.connect(db) as conn:
        got = conn.execute(
            "SELECT work_dir FROM sessions WHERE log_file = ?", (log2.name,)
        ).fetchone()["work_dir"]
        survived = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='sessions'").fetchone()
    check("SQL injection neutralized", got == nasty and survived is not None)

    section("log attribution")
    logs = Path(tempfile.mkdtemp())
    make_log(logs / "process-1700000000000-1111.log")
    check("exact pid matches",
          operator_runner._find_log(logs, {1111}, 1700000000000) is not None)
    check("no fallback to newest on miss",
          operator_runner._find_log(logs, {9999}, 1700000000000) is None)

    section("operator state")
    inst = copilot_operator.Instance("my.proj")
    check("instance id is filesystem safe", "." not in inst.id and ":" not in inst.id)
    inst.save_state(4, "2026-07-27T10:00:00Z", "3f2a9c1e-1111-2222-3333-444455556666")
    state = inst.load_state()
    check("state roundtrip", state["SESSION_NUM"] == "4" and
          state["COPILOT_SESSION_ID"].startswith("3f2a9c1e"))
    inst.claim("tok")
    owner = inst.ownership()
    check("ownership token recorded", owner["token"] == "tok")
    check("display name preserved", owner["display_name"] == "my.proj")
    preamble = copilot_operator.build_preamble("anvil:anvil", inst)
    check("preamble has no POSIX-only touch", "touch " not in preamble)
    check("args_have_explicit_session detects resume",
          copilot_operator.args_have_explicit_session(["--resume=x"]) is True)
    check("args_have_explicit_session ignores lookalike",
          copilot_operator.args_have_explicit_session(["--resumearg"]) is False)
    inst.cleanup_files()

    section("handoff")
    body = handoff_tool.render("s", "", "n", "ctx", "")
    check("handoff omits empty sections", "## In Progress" not in body)
    check("handoff includes context", "## Context" in body)
    d = Path(tempfile.mkdtemp())
    (d / "app").mkdir()
    (d / "app2").mkdir()
    check("sibling prefix not treated as child",
          not handoff_tool.same_or_within(str(d / "app2"), str(d / "app")))
    check("child path detected",
          handoff_tool.same_or_within(str(d / "app"), str(d)))

    section("multiplexer integration")
    mux = operator_mux.Mux()
    if not mux.available():
        print("  SKIP  no multiplexer installed")
    else:
        print(f"  backend: {mux.binary} {mux.version()}")
        name = f"verify-{os.getpid()}"
        try:
            mux.new_session(name, str(tmp),
                            [sys.executable, "-c", "import time; time.sleep(30)"])
            check("session created", mux.has_session(name))
            check("session listed", name in mux.list_sessions())
            check("pane pid is an int", isinstance(mux.pane_pid(name), int))
            mux.kill_session(name)
            check("session killed", not mux.has_session(name))
        finally:
            mux.kill_session(name)

        # Full runner supervision through a real session.
        rdir = Path(tempfile.mkdtemp())
        rlogs = Path(tempfile.mkdtemp())
        rdb = rdir / "m.db"
        child = rdir / "child.py"
        child.write_text(
            "import os, time\n"
            "from pathlib import Path\n"
            f"sys_path = r'''{Path(__file__).resolve().parent}'''\n"
            "import sys; sys.path.insert(0, sys_path)\n"
            "from verify_cross_platform import make_log\n"
            f"make_log(Path(r'''{rlogs}''') / f'process-{{int(time.time()*1000)}}-{{os.getpid()}}.log')\n"
            "time.sleep(2)\n",
            encoding="utf-8",
        )
        spec = rdir / "spec.json"
        spec.write_text(json.dumps({
            "instance": "verify",
            "argv": [sys.executable, str(child)],
            "cwd": str(rdir),
            "session_num": 9,
            "state_dir": str(rdir),
            "metrics_db": str(rdb),
            "copilot_log_dir": str(rlogs),
        }), encoding="utf-8")
        runner = Path(__file__).resolve().parent / "operator_runner.py"
        rname = f"verify-run-{os.getpid()}"
        try:
            mux.new_session(rname, str(rdir),
                            [sys.executable, str(runner), str(spec)])
            deadline = time.time() + 60
            while time.time() < deadline and not (rdir / "verify.exit").exists():
                time.sleep(0.5)
            check("runner recorded exit", (rdir / "verify.exit").exists())
            if rdb.exists():
                with operator_ingest.connect(rdb) as conn:
                    rows = conn.execute(
                        "SELECT session_num FROM sessions WHERE no_op = 0").fetchall()
                check("runner captured metrics for its own process",
                      len(rows) == 1 and rows[0]["session_num"] == 9,
                      f"rows={[dict(r) for r in rows]}")
            else:
                check("runner captured metrics for its own process", False,
                      "no database created")
        finally:
            mux.kill_session(rname)

    section("summary")
    print(f"  passed: {len(PASSES)}   failed: {len(FAILURES)}")
    for f in FAILURES:
        print(f"    - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
