"""End-to-end: restart-loop must swap the supervisor and keep the session.

Runs real processes against a real multiplexer in an isolated operator home,
with a stub `copilot` on PATH so nothing bills. Verifies the property that
matters and that no unit test can prove: after `operator restart-loop`, the
mux session is the *same* session -- same pane pid -- while the supervisor
process behind it is a different one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
OP = str(REPO / "copilot_operator.py")
NAME = "e2erestart"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(label)
    return ok


def run(args: list[str], env: dict, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, OP, *args], env=env, cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def stub_copilot(bindir: Path) -> None:
    """A `copilot` that just stays alive, so a session can exist to adopt."""
    if os.name == "nt":
        (bindir / "copilot.cmd").write_text(
            "@echo off\r\nping -n 900 127.0.0.1 >nul\r\n", encoding="utf-8")
    else:
        p = bindir / "copilot"
        p.write_text("#!/bin/sh\nsleep 900\n", encoding="utf-8")
        p.chmod(0o755)


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def wait_for(fn, timeout: float = 60.0, interval: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def _changed_pid(path: Path, old: int | None) -> int | None:
    """The pid in path, but only once it differs from old."""
    pid = read_pid(path)
    return pid if pid and pid != old else None


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="op-e2e-"))
    home = tmp / "operator-home"
    bindir = tmp / "bin"
    project = tmp / "project"
    for d in (home, bindir, project):
        d.mkdir(parents=True)
    stub_copilot(bindir)

    env = dict(os.environ)
    env["COPILOT_OPERATOR_HOME"] = str(home)
    env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    env["OPERATOR_NO_TAB_PROGRESS"] = "1"
    env["PYTHONPATH"] = str(REPO)

    restart_dir = home / "restart"
    loop_pid_file = restart_dir / f"{NAME}.loop.pid"

    print("=== setup ===")
    check("stub copilot on PATH", shutil.which("copilot", path=env["PATH"]) is not None)

    sys.path.insert(0, str(REPO))
    os.environ["COPILOT_OPERATOR_HOME"] = str(home)
    import operator_mux  # noqa: E402
    mux = operator_mux.Mux()
    check("multiplexer available", mux.available(), getattr(mux, "name", "?"))

    try:
        print("=== start loop ===")
        proc = run(["--loop", "--headless", "--name", NAME,
                    "--agent", "test:agent"], env, str(project))
        check("start exited 0", proc.returncode == 0,
              (proc.stderr or proc.stdout or "").strip()[-200:])

        session = wait_for(lambda: mux.has_session(NAME) or None)
        check("session came up", bool(session))
        old_loop_pid = wait_for(lambda: read_pid(loop_pid_file))
        check("supervisor recorded a pid", old_loop_pid is not None, str(old_loop_pid))
        if not session or old_loop_pid is None:
            return 1

        old_pane_pid = wait_for(lambda: mux.pane_pid(NAME))
        check("pane has a pid", old_pane_pid is not None, str(old_pane_pid))

        args_file = restart_dir / f"{NAME}.loopargs.json"
        check("loop args recorded", args_file.exists(),
              args_file.read_text(encoding="utf-8") if args_file.exists() else "")

        print("=== restart-loop ===")
        proc = run(["restart-loop", NAME], env, str(project))
        check("restart-loop exited 0", proc.returncode == 0,
              (proc.stdout + proc.stderr).strip()[-300:])

        print("=== the property under test ===")
        check("session still exists", mux.has_session(NAME))
        new_pane_pid = mux.pane_pid(NAME)
        check("pane pid UNCHANGED (session survived)",
              new_pane_pid == old_pane_pid, f"{old_pane_pid} -> {new_pane_pid}")

        new_loop_pid = wait_for(lambda: _changed_pid(loop_pid_file, old_loop_pid))
        check("supervisor pid CHANGED (new code loaded)",
              new_loop_pid is not None and new_loop_pid != old_loop_pid,
              f"{old_loop_pid} -> {new_loop_pid}")
        check("old supervisor is gone", not pid_alive(old_loop_pid), str(old_loop_pid))

        print("=== adopted supervisor still supervises ===")
        # Kill the pane's program: a live supervisor must notice and relaunch.
        mux.kill_session(NAME)
        relaunched = wait_for(lambda: mux.has_session(NAME) or None, timeout=90)
        check("adopted supervisor relaunched a dead session", bool(relaunched))
        return 1 if failures else 0
    finally:
        print("=== cleanup ===")
        run(["stop", NAME], env, str(project))
        for _ in range(20):
            if not mux.has_session(NAME):
                break
            time.sleep(0.5)
        if mux.has_session(NAME):
            mux.kill_session(NAME)
        pid = read_pid(loop_pid_file)
        if pid and pid_alive(pid):
            kill_pid(pid)
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"  cleaned up {tmp}")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        os.kill(pid, 15)


if __name__ == "__main__":
    rc = main()
    print(f"\n=== summary ===\n  failures: {len(failures)}"
          + ("" if not failures else "\n  " + "\n  ".join(failures)))
    sys.exit(rc)
