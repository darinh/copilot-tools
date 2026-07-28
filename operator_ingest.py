#!/usr/bin/env python3
"""Parse Copilot process logs and store usage metrics in SQLite.

Cross-platform: uses only the standard library. The predecessor shelled out to
`sqlite3`, `grep`, `head` and `tail`, none of which exist on a stock Windows
machine, and none of which are needed.

Three correctness properties this module is responsible for:

* **Explicit UTF-8.** Copilot writes UTF-8. Python's `open()` defaults to the
  locale code page on Windows, so non-ASCII content would be mis-decoded and the
  JSON parse would fail, silently discarding a session's metrics.
* **Parameter binding.** Values parsed out of logs (model names, working
  directories) are bound, never interpolated into SQL text.
* **Concurrency.** Several operator instances share one database, so connections
  use WAL and a busy timeout instead of failing with "database is locked".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BUSY_TIMEOUT = 15.0

SCHEMA = """
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
"""


# ── database ────────────────────────────────────────────────────
def connect(db_path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
    return conn


def init_db(db_path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "log_file_mtime" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN log_file_mtime TEXT")
        conn.commit()


# ── helpers ─────────────────────────────────────────────────────
def fmt_tokens(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_ts(line: str) -> str | None:
    line = (line or "").strip()
    if len(line) > 24 and line[0] == "2" and line[4] == "-":
        ts = line[:24]
        return ts if ts.endswith("Z") else ts + "Z"
    return None


# ── parsing ─────────────────────────────────────────────────────
def extract_shutdown_event(text: str) -> dict | None:
    """Return the session_shutdown telemetry event as a dict, if present."""
    marker = text.find('"kind": "session_shutdown"')
    if marker < 0:
        marker = text.find('"kind":"session_shutdown"')
    if marker < 0:
        return None

    # Walk backwards to the '{' that opens the object containing the marker.
    depth = 0
    start = -1
    for i in range(marker, -1, -1):
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start < 0:
        return None

    # Forward-balance to the matching close brace.
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = text[start : i + 1]
                raw = re.sub(r",(\s*[}\]])", r"\1", raw)
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


def extract_premium_from_usage(text: str) -> tuple[dict, int]:
    """Sum assistant_usage cost fields per model.

    `session_shutdown.total_premium_requests` reports only the last call's cost
    rather than the sum, so the per-call `cost` values are authoritative.
    """
    models: dict[str, dict] = {}
    total = 0.0
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if '"kind": "assistant_usage"' not in line and '"kind":"assistant_usage"' not in line:
            continue
        model = None
        for j in range(idx, min(idx + 40, len(lines))):
            probe = lines[j].strip()
            if model is None and '"model":' in probe:
                candidate = probe.split(":", 1)[1].strip().strip('",')
                if candidate and " " not in candidate and ")" not in candidate and len(candidate) <= 40:
                    model = candidate
                continue
            if model is not None and probe.startswith('"cost":'):
                try:
                    cost = float(probe.split(":", 1)[1].strip().rstrip(","))
                except ValueError:
                    break
                entry = models.setdefault(model, {"cost": 0.0, "calls": 0})
                entry["cost"] += cost
                entry["calls"] += 1
                total += cost
                break
    return models, round(total)


def git_branch(work_dir: str) -> str:
    if not work_dir or not Path(work_dir).is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", work_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ── ingestion ───────────────────────────────────────────────────
def ingest_file(
    logfile,
    db_path,
    session_num: int = 0,
    work_dir: str = "",
    force: bool = False,
) -> str:
    """Parse one log file into the database. Returns a short status string."""
    logfile = Path(logfile).resolve()
    if not logfile.is_file():
        raise FileNotFoundError(str(logfile))

    init_db(db_path)
    basename = logfile.name
    mtime = _iso(logfile.stat().st_mtime)

    with connect(db_path) as conn:
        if not force:
            row = conn.execute(
                "SELECT log_file_mtime FROM sessions WHERE log_file = ?", (basename,)
            ).fetchone()
            if row and row["log_file_mtime"] == mtime:
                return f"SKIP {basename} (already processed)"

        text = _read_text(logfile)
        lines = text.splitlines()
        first_line = lines[0] if lines else ""
        last_line = lines[-1] if lines else ""
        event = extract_shutdown_event(text)

        if not event:
            ts = _extract_ts(first_line) or _now()
            conn.execute(
                """
                INSERT INTO sessions (session_num, log_file, log_file_mtime, no_op,
                                      started_at, ended_at)
                VALUES (0, ?, ?, 1, ?, ?)
                ON CONFLICT(log_file) DO UPDATE SET
                    log_file_mtime = excluded.log_file_mtime,
                    no_op = 1,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at
                """,
                (basename, mtime, ts, ts),
            )
            conn.commit()
            return f"SKIP {basename} (no shutdown event)"

        props = event.get("properties", {}) or {}
        metrics = event.get("metrics", {}) or {}

        usage_models, usage_premium = extract_premium_from_usage(text)
        total_premium = usage_premium or metrics.get("total_premium_requests", 0) or 0

        api_time_s = int((metrics.get("total_api_duration_ms", 0) or 0) / 1000)
        session_time_s = int((metrics.get("session_duration_ms", 0) or 0) / 1000)
        lines_added = metrics.get("lines_added", 0) or 0
        lines_removed = metrics.get("lines_removed", 0) or 0

        started_at = _extract_ts(first_line) or _now()
        ended_at = _extract_ts(last_line) or _now()

        if not work_dir:
            m = re.search(r'"cwd":\s*"([^"]+)"', text[:50000])
            work_dir = m.group(1) if m else ""

        branch = git_branch(work_dir)

        # Per-model token counts come from the shutdown properties; premium
        # request counts are overridden with the authoritative usage sums.
        models: dict[str, dict] = {}
        pattern = re.compile(
            r"^model_(.+?)_(input_tokens|output_tokens|cache_read_tokens|"
            r"request_count|request_cost)$"
        )
        for key, value in props.items():
            m = pattern.match(key)
            if m:
                models.setdefault(m.group(1), {})[m.group(2)] = value
        for name, data in usage_models.items():
            entry = models.setdefault(name, {})
            entry["request_cost"] = str(round(data["cost"]))
            entry.setdefault("request_count", str(data["calls"]))

        raw_lines = [
            f"Total usage est: {total_premium} Premium requests",
            f"API time spent: {api_time_s}s",
        ]
        if session_time_s >= 3600:
            h, rem = divmod(session_time_s, 3600)
            mn, sec = divmod(rem, 60)
            raw_lines.append(f"Total session time: {h}h {mn}m {sec}s")
        else:
            mn, sec = divmod(session_time_s, 60)
            raw_lines.append(f"Total session time: {mn}m {sec}s")
        raw_lines.append(f"Total code changes: +{lines_added} -{lines_removed}")
        if models:
            raw_lines.append("Breakdown by AI model:")
            for name in sorted(models, key=lambda n: -int(models[n].get("request_cost", 0) or 0)):
                md = models[name]
                raw_lines.append(
                    f"  {name}  {fmt_tokens(md.get('input_tokens', 0))} in, "
                    f"{fmt_tokens(md.get('output_tokens', 0))} out, "
                    f"{fmt_tokens(md.get('cache_read_tokens', 0))} cached "
                    f"(Est. {int(md.get('request_cost', 0) or 0)} Premium requests)"
                )

        conn.execute(
            """
            INSERT INTO sessions (session_num, log_file, log_file_mtime, no_op,
                                  started_at, ended_at, work_dir, git_branch,
                                  premium_requests, api_time_seconds,
                                  session_time_seconds, lines_added, lines_removed,
                                  raw_metrics)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(log_file) DO UPDATE SET
                session_num = excluded.session_num,
                log_file_mtime = excluded.log_file_mtime,
                no_op = 0,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                work_dir = excluded.work_dir,
                git_branch = excluded.git_branch,
                premium_requests = excluded.premium_requests,
                api_time_seconds = excluded.api_time_seconds,
                session_time_seconds = excluded.session_time_seconds,
                lines_added = excluded.lines_added,
                lines_removed = excluded.lines_removed,
                raw_metrics = excluded.raw_metrics
            """,
            (
                session_num, basename, mtime, started_at, ended_at, work_dir, branch,
                total_premium, api_time_s, session_time_s, lines_added, lines_removed,
                "\n".join(raw_lines),
            ),
        )
        row = conn.execute(
            "SELECT id FROM sessions WHERE log_file = ?", (basename,)
        ).fetchone()
        session_id = row["id"]

        conn.execute("DELETE FROM model_usage WHERE session_id = ?", (session_id,))
        for name, md in models.items():
            conn.execute(
                """
                INSERT INTO model_usage (session_id, model_name, tokens_in,
                                         tokens_out, tokens_cached, premium_requests)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    name,
                    fmt_tokens(md.get("input_tokens", 0)),
                    fmt_tokens(md.get("output_tokens", 0)),
                    fmt_tokens(md.get("cache_read_tokens", 0)),
                    int(md.get("request_cost", 0) or 0),
                ),
            )
        conn.commit()

    return (
        f"OK {basename}: {total_premium} premium, {api_time_s}s api, "
        f"+{lines_added} -{lines_removed}"
    )


def ingest_all(log_dir, db_path, force: bool = False) -> list[str]:
    log_dir = Path(log_dir)
    results = []
    if not log_dir.is_dir():
        return results
    for path in sorted(log_dir.glob("process-*.log")):
        try:
            results.append(ingest_file(path, db_path, force=force))
        except Exception as exc:  # pragma: no cover - defensive
            results.append(f"ERROR {path.name}: {exc}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator-ingest")
    parser.add_argument("logfile")
    parser.add_argument("db_path")
    parser.add_argument("--session-num", type=int, default=0)
    parser.add_argument("--work-dir", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(
            ingest_file(
                args.logfile,
                args.db_path,
                session_num=args.session_num,
                work_dir=args.work_dir,
                force=args.force,
            )
        )
    except FileNotFoundError:
        print(f"ERROR: {args.logfile} not found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
