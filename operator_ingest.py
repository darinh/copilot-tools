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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import install_manifest

BUSY_TIMEOUT = 15.0

# Billing constants.
#
# GitHub replaced premium requests with AI credits on 2026-06-01. Usage is
# metered on token consumption and reported by the CLI as "nano AIU" —
# billionths of an AI credit.
#
#   credits = total_nano_aiu / NANO_AIU_PER_CREDIT
#   dollars = credits * USD_PER_CREDIT
#
# Verified against a live session: 20,242,875,000 nano AIU was displayed by the
# CLI as "AI Credits 20.2".
NANO_AIU_PER_CREDIT = 1_000_000_000
USD_PER_CREDIT = 0.01

# Legacy request-based billing, still in force for annual plans that have not
# yet expired. Retained so old logs and legacy accounts still report correctly.
USD_PER_PREMIUM_REQUEST = 0.04

TOKEN_TYPES = ("input", "cache_read", "cache_write", "output")

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
    nano_aiu INTEGER NOT NULL DEFAULT 0,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_cache_read INTEGER NOT NULL DEFAULT 0,
    tokens_cache_write INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
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
    -- Deliberately INTEGER where its three siblings are TEXT. Those hold
    -- fmt_tokens() output ("32.4k"), which SUM() silently reads as 32. This
    -- column exists so a user can audit where their credits went, and
    -- cache-write is the dearest token type per million, so it has to stay
    -- summable. Symmetry is not worth a wrong number.
    tokens_cache_write INTEGER,
    premium_requests INTEGER,
    nano_aiu INTEGER NOT NULL DEFAULT 0
);
"""

# Columns added after the original schema shipped. Existing databases are
# migrated in place so a user's history survives the billing change.
_ADDED_COLUMNS = {
    "sessions": {
        "log_file_mtime": "TEXT",
        "nano_aiu": "INTEGER NOT NULL DEFAULT 0",
        "tokens_input": "INTEGER NOT NULL DEFAULT 0",
        "tokens_cache_read": "INTEGER NOT NULL DEFAULT 0",
        "tokens_cache_write": "INTEGER NOT NULL DEFAULT 0",
        "tokens_output": "INTEGER NOT NULL DEFAULT 0",
    },
    "model_usage": {
        "nano_aiu": "INTEGER NOT NULL DEFAULT 0",
        "tokens_cache_write": "INTEGER",
    },
}


def credits_from_nano(nano_aiu) -> float:
    return (nano_aiu or 0) / NANO_AIU_PER_CREDIT


def usd_from_nano(nano_aiu) -> float:
    return credits_from_nano(nano_aiu) * USD_PER_CREDIT


# ── database ────────────────────────────────────────────────────
@contextmanager
def connect(db_path):
    """Open a connection, commit on success, and always close.

    Deliberately a context manager rather than a bare connection: sqlite3's own
    ``with`` block manages the *transaction* but does **not** close the
    connection. Returning a raw connection therefore leaks a file handle on
    every call, which matters for a long-running loop-mode process that reports
    and ingests repeatedly.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT * 1000)}")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name in existing:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError as exc:
                    # Two operators can upgrade the same database at once and
                    # both observe a column as missing. Losing that race is
                    # fine as long as the column now exists.
                    if "duplicate column" not in str(exc).lower():
                        raise
                    recheck = {r["name"] for r in
                               conn.execute(f"PRAGMA table_info({table})")}
                    if name not in recheck:
                        raise


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
def _iter_json_objects(text: str):
    """Yield ``(start, raw)`` for every top-level ``{...}`` block in the text.

    A single string-aware pass. Hand-counting braces without tracking string and
    escape state misreads a ``}`` inside a string literal as structure, which
    makes the enclosing event unparseable and silently discards a session's
    metrics.

    Copilot logs interleave JSON with plain text, and that text can contain an
    unmatched ``{`` or ``"``. Scanner state is therefore reset at each newline
    while no object is open, so one malformed line cannot swallow every event
    after it.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    at_line_start = True

    for i, ch in enumerate(text):
        # Resynchronize at a record boundary. Without this, an unmatched quote
        # or brace in ordinary prose leaves the scanner stuck for the rest of
        # the file and every later event is lost.
        if at_line_start and depth == 0:
            in_string = False
            escaped = False
            start = -1
        at_line_start = ch == "\n"

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    yield start, text[start : i + 1]
                    start = -1


def _parse_object(raw: str) -> dict | None:
    candidate = re.sub(r",(\s*[}\]])", r"\1", raw)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _telemetry_events(text: str):
    """Yield parsed top-level telemetry objects that carry a ``kind``.

    Falls back to a decoder-driven rescan when the primary pass finds nothing,
    so a malformed region cannot hide an otherwise valid event.
    """
    seen_any = False
    for _, raw in _iter_json_objects(text):
        if '"kind"' not in raw:
            continue
        parsed = _parse_object(raw)
        if parsed is not None and "kind" in parsed:
            seen_any = True
            yield parsed
    if seen_any:
        return
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(parsed, dict) and "kind" in parsed:
            yield parsed


def extract_shutdown_event(text: str) -> dict | None:
    """Return the session_shutdown telemetry event as a dict, if present.

    Matching is on the parsed ``kind`` field rather than a substring search, so
    a log message that merely mentions ``session_shutdown`` is never mistaken
    for the event itself.
    """
    for event in _telemetry_events(text):
        if event.get("kind") == "session_shutdown":
            return event
    return None


def extract_ai_credit_usage(text: str) -> dict:
    """Aggregate AI credit and token usage from Copilot API response bodies.

    Since 2026-06-01 usage is metered in AI credits rather than premium
    requests. Each chat-completion response the CLI logs carries a
    ``copilot_usage`` object::

        "copilot_usage": {
          "token_details": [
            {"batch_size": 1000000, "cost_per_batch": 500000000000,
             "token_count": 2, "token_type": "input"}, ...
          ],
          "total_nano_aiu": 20242875000
        }

    ``total_nano_aiu`` is billionths of an AI credit and is authoritative — the
    per-type costs are reported alongside it for attribution, not for the
    caller to recompute.

    Returns totals plus a per-model breakdown. The response body's ``model``
    field supplies the model name when present.

    Note: these bodies are only written when Copilot runs at debug log level.
    At the default level the process log contains no usage data at all.
    """
    totals = {
        "nano_aiu": 0,
        "tokens": {t: 0 for t in TOKEN_TYPES},
        "models": {},
        "calls": 0,
    }
    seen_ids: set[str] = set()

    for obj in _iter_parsed_objects(text):
        usage = obj.get("copilot_usage")
        if not isinstance(usage, dict):
            continue
        # Responses carry a unique id. Dedupe on it so a body echoed twice in
        # the log — a retry, or the same payload logged at two levels — cannot
        # inflate reported usage.
        response_id = obj.get("id")
        if isinstance(response_id, str) and response_id:
            if response_id in seen_ids:
                continue
            seen_ids.add(response_id)
        nano = usage.get("total_nano_aiu")
        if not isinstance(nano, (int, float)):
            nano = 0
        model = obj.get("model") if isinstance(obj.get("model"), str) else "unknown"

        entry = totals["models"].setdefault(
            model, {"nano_aiu": 0, "calls": 0,
                    "tokens": {t: 0 for t in TOKEN_TYPES}}
        )
        totals["nano_aiu"] += int(nano)
        entry["nano_aiu"] += int(nano)
        totals["calls"] += 1
        entry["calls"] += 1

        details = usage.get("token_details")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                ttype = detail.get("token_type")
                count = detail.get("token_count")
                if ttype in TOKEN_TYPES and isinstance(count, (int, float)):
                    totals["tokens"][ttype] += int(count)
                    entry["tokens"][ttype] += int(count)

    return totals


def _iter_parsed_objects(text: str):
    """Yield every top-level JSON object in the text that parses to a dict."""
    for _, raw in _iter_json_objects(text):
        parsed = _parse_object(raw)
        if parsed is not None:
            yield parsed


def extract_premium_from_usage(text: str) -> tuple[dict, int]:
    """Sum assistant_usage cost fields per model.

    `session_shutdown.total_premium_requests` reports only the last call's cost
    rather than the sum, so the per-call `cost` values are authoritative.

    Fields are read from the parsed object, so key order does not matter and a
    cost can never be borrowed from a neighbouring event.
    """
    models: dict[str, dict] = {}
    total = 0.0
    for event in _telemetry_events(text):
        if event.get("kind") != "assistant_usage":
            continue
        model = event.get("model")
        cost = event.get("cost")
        if not isinstance(model, str) or not model:
            continue
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            continue
        entry = models.setdefault(model, {"cost": 0.0, "calls": 0})
        entry["cost"] += cost
        entry["calls"] += 1
        total += cost
    return models, round(total)


def git_branch(work_dir: str) -> str:
    # probe-ok: a wrong False returns "" and the row is recorded without a
    # branch. The git call below would fail on the same directory anyway, and
    # this reaches the same "" by the guarded route.
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
    # probe-ok: a wrong answer raises rather than skipping — the only caller
    # is `ingest_all`, which turns the exception into an `ERROR <name>` line
    # for that one log and goes on to the rest.
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
        credit_usage = extract_ai_credit_usage(text)

        # A log is worth recording if it carries either the legacy shutdown
        # summary or AI credit usage. Current Copilot versions no longer write
        # a shutdown payload to the process log, so requiring one would discard
        # every modern session.
        if not event and credit_usage["calls"] == 0:
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
            return f"SKIP {basename} (no usage data)"

        event = event or {}
        props = event.get("properties", {}) or {}
        metrics = event.get("metrics", {}) or {}

        usage_models, usage_premium = extract_premium_from_usage(text)
        total_premium = usage_premium or metrics.get("total_premium_requests", 0) or 0

        nano_aiu = credit_usage["nano_aiu"]
        tokens = credit_usage["tokens"]

        # A shutdown event is the only source of these four. When the CLI does
        # not emit one, recording 0 would be a lie that reads as a measurement:
        # "this session changed no code" is indistinguishable from "nobody
        # looked". NULL keeps unknown and zero distinguishable, so an average
        # over these columns cannot be silently dragged down by absent data.
        def _measured(key, scale=1):
            if key not in metrics:
                return None
            value = metrics.get(key) or 0
            return int(value / scale)

        api_time_s = _measured("total_api_duration_ms", 1000)
        session_time_s = _measured("session_duration_ms", 1000)
        lines_added = _measured("lines_added")
        lines_removed = _measured("lines_removed")

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
        # Merge AI credit usage, which is the current billing signal.
        for name, data in credit_usage["models"].items():
            entry = models.setdefault(name, {})
            entry["nano_aiu"] = data["nano_aiu"]
            entry.setdefault("request_count", str(data["calls"]))
            entry.setdefault("input_tokens", data["tokens"]["input"])
            entry.setdefault("output_tokens", data["tokens"]["output"])
            entry.setdefault("cache_read_tokens", data["tokens"]["cache_read"])
            entry.setdefault("cache_write_tokens", data["tokens"]["cache_write"])

        if nano_aiu:
            raw_lines = [
                f"Total usage: {credits_from_nano(nano_aiu):.2f} AI credits "
                f"(${usd_from_nano(nano_aiu):.2f})",
                f"Tokens: {fmt_tokens(tokens['input'])} in, "
                f"{fmt_tokens(tokens['output'])} out, "
                f"{fmt_tokens(tokens['cache_read'])} cache read, "
                f"{fmt_tokens(tokens['cache_write'])} cache write",
            ]
        else:
            raw_lines = [f"Total usage est: {total_premium} Premium requests"]
        raw_lines.append(
            "API time spent: unknown" if api_time_s is None
            else f"API time spent: {api_time_s}s")
        if session_time_s is None:
            raw_lines.append("Total session time: unknown")
        elif session_time_s >= 3600:
            h, rem = divmod(session_time_s, 3600)
            mn, sec = divmod(rem, 60)
            raw_lines.append(f"Total session time: {h}h {mn}m {sec}s")
        else:
            mn, sec = divmod(session_time_s, 60)
            raw_lines.append(f"Total session time: {mn}m {sec}s")
        raw_lines.append(
            "Total code changes: unknown"
            if lines_added is None or lines_removed is None
            else f"Total code changes: +{lines_added} -{lines_removed}")
        if models:
            raw_lines.append("Breakdown by AI model:")
            for name in sorted(
                models,
                key=lambda n: (-int(models[n].get("nano_aiu", 0) or 0),
                               -int(models[n].get("request_cost", 0) or 0)),
            ):
                md = models[name]
                cost = (f"{credits_from_nano(md['nano_aiu']):.2f} AI credits"
                        if md.get("nano_aiu")
                        else f"Est. {int(md.get('request_cost', 0) or 0)} Premium requests")
                raw_lines.append(
                    f"  {name}  {fmt_tokens(md.get('input_tokens', 0))} in, "
                    f"{fmt_tokens(md.get('output_tokens', 0))} out, "
                    f"{fmt_tokens(md.get('cache_read_tokens', 0))} cached "
                    f"({cost})"
                )

        conn.execute(
            """
            INSERT INTO sessions (session_num, log_file, log_file_mtime, no_op,
                                  started_at, ended_at, work_dir, git_branch,
                                  premium_requests, nano_aiu, tokens_input,
                                  tokens_cache_read, tokens_cache_write,
                                  tokens_output, api_time_seconds,
                                  session_time_seconds, lines_added, lines_removed,
                                  raw_metrics)
            VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(log_file) DO UPDATE SET
                session_num = excluded.session_num,
                log_file_mtime = excluded.log_file_mtime,
                no_op = 0,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                work_dir = excluded.work_dir,
                git_branch = excluded.git_branch,
                premium_requests = excluded.premium_requests,
                nano_aiu = excluded.nano_aiu,
                tokens_input = excluded.tokens_input,
                tokens_cache_read = excluded.tokens_cache_read,
                tokens_cache_write = excluded.tokens_cache_write,
                tokens_output = excluded.tokens_output,
                api_time_seconds = excluded.api_time_seconds,
                session_time_seconds = excluded.session_time_seconds,
                lines_added = excluded.lines_added,
                lines_removed = excluded.lines_removed,
                raw_metrics = excluded.raw_metrics
            """,
            (
                session_num, basename, mtime, started_at, ended_at, work_dir, branch,
                total_premium, nano_aiu, tokens["input"], tokens["cache_read"],
                tokens["cache_write"], tokens["output"],
                api_time_s, session_time_s, lines_added, lines_removed,
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
                                         tokens_out, tokens_cached,
                                         tokens_cache_write,
                                         premium_requests, nano_aiu)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    name,
                    fmt_tokens(md.get("input_tokens", 0)),
                    fmt_tokens(md.get("output_tokens", 0)),
                    fmt_tokens(md.get("cache_read_tokens", 0)),
                    int(md.get("cache_write_tokens", 0) or 0),
                    int(md.get("request_cost", 0) or 0),
                    int(md.get("nano_aiu", 0) or 0),
                ),
            )
        conn.commit()

    if nano_aiu:
        summary = (f"{credits_from_nano(nano_aiu):.2f} AI credits "
                   f"(${usd_from_nano(nano_aiu):.2f})")
    else:
        summary = f"{total_premium} premium"
    return (
        f"OK {basename}: {summary}, "
        f"{'?' if api_time_s is None else api_time_s}s api, "
        + ("+? -?" if lines_added is None or lines_removed is None
           else f"+{lines_added} -{lines_removed}")
    )


def ingest_all(log_dir, db_path, force: bool = False) -> "list[str] | None":
    """Ingest every process log in ``log_dir``; None when it cannot be read.

    ``[]`` is a census -- the directory was read and found to hold no logs.
    A directory that could not be examined establishes nothing of the kind,
    and the caller spends an empty list as "No Copilot logs found" and exits
    0, so a machine that has silently stopped recording metrics is
    indistinguishable from one that has simply not run yet.

    ``Path.is_dir`` cannot draw that line: it answers False for a dangling
    symlink, a symlink loop and a disconnected network home, and raises on a
    permission denial. :func:`copilot_operator._log_files` already reaches the
    correct answer for the same directory a few lines from the caller of this
    function; ``ingest_all`` kept the bare probe because the rule had only
    ever been applied to the other module.

    ``iterdir`` rather than ``glob``, and that is the whole mechanism.
    ``glob`` swallows the error and yields nothing, so a directory link whose
    target is gone comes back as a readable directory holding no logs --
    which is the same wrong answer in a new place. ``iterdir`` raises, and
    the raise is the only thing here that distinguishes "read it, found
    nothing" from "never read it".
    """
    log_dir = Path(log_dir)
    results: list[str] = []
    if install_manifest.path_present(log_dir) is False:
        return results
    try:
        entries = sorted(log_dir.iterdir())
    except OSError:
        return None
    logs = [p for p in entries
            if p.name.startswith("process-") and p.name.endswith(".log")]
    for path in logs:
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
