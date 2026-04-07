#!/usr/bin/env python3
"""Parse copilot process logs and store usage metrics in SQLite.

Uses the session_shutdown telemetry event which contains the complete
session summary: total premium requests, API duration, session duration,
lines changed, and per-model token breakdowns — all in one event.

Usage:
    operator-ingest.py <logfile> <db_path> [--session-num N] [--work-dir DIR] [--force]

Exit codes: 0=success/skip, 1=error, 2=no shutdown event
"""
import re, sys, subprocess, os, argparse, json, sqlite3
from pathlib import Path
from datetime import datetime, timezone


def run_sqlite(db_path, sql, fetch=True):
    """Execute SQL against a SQLite database using Python's sqlite3 module."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.cursor()
        # Handle multi-statement SQL by splitting on semicolons
        statements = [s.strip() for s in sql.split(';') if s.strip()]
        result = ''
        for stmt in statements:
            cursor.execute(stmt)
            if fetch and cursor.description:
                rows = cursor.fetchall()
                if rows:
                    result = str(rows[-1][0]) if len(rows[-1]) == 1 else str(rows[-1])
        conn.commit()
        return result
    finally:
        conn.close()


def sql_esc(s):
    return str(s).replace("'", "''")


def fmt_tokens(n):
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}k"
    return str(n)


def read_first_line(filepath):
    """Read the first line of a file."""
    with open(filepath, 'r', errors='replace') as f:
        return f.readline()


def read_last_line(filepath):
    """Read the last non-empty line of a file efficiently."""
    with open(filepath, 'rb') as f:
        # Seek to end
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return ''
        # Read backwards to find last newline
        pos = size - 1
        while pos > 0:
            f.seek(pos)
            ch = f.read(1)
            if ch == b'\n' and pos < size - 1:
                return f.read().decode('utf-8', errors='replace').strip()
            pos -= 1
        # File has no newline — return entire content
        f.seek(0)
        return f.read().decode('utf-8', errors='replace').strip()


def read_head_bytes(filepath, num_bytes):
    """Read the first N bytes of a file."""
    with open(filepath, 'r', errors='replace') as f:
        return f.read(num_bytes)


def extract_shutdown_event(logfile):
    """Find and parse the session_shutdown JSON event from a log file.

    The log contains many false positives where 'session_shutdown' appears
    inside quoted string values (e.g., the agent discussing this script).
    The real telemetry event is near the end of the file, preceded by a log
    line like '[INFO] [Telemetry] cli.telemetry:' with JSON on the next line.
    We search from the end to find it quickly and avoid false positives.
    """
    with open(logfile, 'r', errors='replace') as f:
        content = f.read()

    # Search from the end — the real shutdown event is always near EOF
    marker = '"kind": "session_shutdown"'
    search_from = len(content)
    while True:
        idx = content.rfind(marker, 0, search_from)
        if idx < 0:
            return None

        # Check if this occurrence is inside a JSON string value.
        # If the line containing the marker also contains "content": or starts
        # with a quote that suggests it's inside a string value, skip it.
        line_start = content.rfind('\n', 0, idx)
        line = content[line_start+1:idx+len(marker)+5] if line_start >= 0 else content[:idx+len(marker)+5]

        # Heuristic: real event has "kind" as a top-level key with leading whitespace
        # False positives are inside escaped strings or "content" fields
        stripped_line = line.lstrip()
        if stripped_line.startswith('"kind"'):
            # This looks like a real JSON field — walk back to find opening brace
            start = idx
            depth = 0
            while start > 0:
                start -= 1
                if content[start] == '}':
                    depth += 1
                elif content[start] == '{':
                    if depth == 0:
                        break
                    depth -= 1

            if content[start] == '{':
                # Walk forward to find matching close
                depth = 0
                for i in range(start, len(content)):
                    if content[i] == '{':
                        depth += 1
                    elif content[i] == '}':
                        depth -= 1
                        if depth == 0:
                            raw = content[start:i+1]
                            raw = re.sub(r',(\s*[}\]])', r'\1', raw)
                            try:
                                event = json.loads(raw)
                                # Verify it's really a shutdown event with metrics
                                if event.get('kind') == 'session_shutdown' and 'metrics' in event:
                                    return event
                            except json.JSONDecodeError:
                                pass
                            break

        search_from = idx


def extract_premium_from_usage(logfile):
    """Sum assistant_usage cost fields for accurate premium request counts.

    session_shutdown.total_premium_requests is unreliable (reports last call's
    cost, not the sum). The assistant_usage cost field is the per-call premium
    request multiplier — summing it gives the actual billed amount.

    Returns: {model_name: {'cost': float, 'calls': int}, ...}, total_cost
    """
    with open(logfile, 'r', errors='replace') as f:
        content = f.read()

    models = {}
    total = 0.0

    # Find all assistant_usage events and extract model + cost
    for match in re.finditer(r'"kind":\s*"assistant_usage"', content):
        # Look in the surrounding context (back to previous '{', forward ~500 chars)
        search_start = content.rfind('{', max(0, match.start() - 2000), match.start())
        if search_start < 0:
            continue
        search_end = min(len(content), match.end() + 2000)
        block = content[search_start:search_end]

        model_match = re.search(r'"model":\s*"([^"]+)"', block)
        cost_match = re.search(r'"cost":\s*([0-9.]+)', block)

        if model_match and cost_match:
            model = model_match.group(1)
            # Skip garbage model names
            if not model or ' ' in model or ')' in model or len(model) > 40:
                continue
            cost = float(cost_match.group(1))
            if model not in models:
                models[model] = {'cost': 0.0, 'calls': 0}
            models[model]['cost'] += cost
            models[model]['calls'] += 1
            total += cost

    return models, round(total)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('logfile')
    parser.add_argument('db_path')
    parser.add_argument('--session-num', type=int, default=0)
    parser.add_argument('--work-dir', default='')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    logfile = str(Path(args.logfile).resolve())
    log_basename = Path(logfile).name

    if not Path(logfile).is_file():
        print(f"ERROR: {logfile} not found", file=sys.stderr)
        sys.exit(1)

    # Get current file mtime as ISO timestamp
    file_mtime = datetime.fromtimestamp(
        os.path.getmtime(logfile), tz=timezone.utc
    ).strftime('%Y-%m-%dT%H:%M:%SZ')

    if not args.force:
        existing = run_sqlite(args.db_path,
            f"SELECT log_file_mtime FROM sessions WHERE log_file = '{sql_esc(log_basename)}'")
        if existing:
            if existing.strip() == file_mtime:
                print(f"SKIP {log_basename} (already processed, mtime unchanged)")
                sys.exit(0)
            else:
                print(f"STALE {log_basename} (mtime changed, reprocessing)")

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    event = extract_shutdown_event(logfile)

    if not event:
        # UPSERT a no_op record so we don't re-scan this file
        first_line = read_first_line(logfile)
        ts = first_line.strip()[:24] if first_line.strip() else now
        if not ts.endswith('Z'):
            ts += 'Z'
        run_sqlite(args.db_path,
            f"INSERT INTO sessions (session_num, log_file, log_file_mtime, no_op, started_at, ended_at) "
            f"VALUES (0, '{sql_esc(log_basename)}', '{sql_esc(file_mtime)}', 1, '{sql_esc(ts)}', '{sql_esc(ts)}') "
            f"ON CONFLICT(log_file) DO UPDATE SET "
            f"log_file_mtime = '{sql_esc(file_mtime)}', no_op = 1, "
            f"started_at = '{sql_esc(ts)}', ended_at = '{sql_esc(ts)}'",
            fetch=False)
        print(f"SKIP {log_basename} (no shutdown event)")
        sys.exit(2)

    props = event.get('properties', {})
    metrics = event.get('metrics', {})

    # Get accurate premium request counts from assistant_usage events.
    # session_shutdown.total_premium_requests is unreliable.
    usage_models, usage_premium = extract_premium_from_usage(logfile)
    total_premium = usage_premium if usage_premium > 0 else metrics.get('total_premium_requests', 0)

    api_duration_ms = metrics.get('total_api_duration_ms', 0)
    session_duration_ms = metrics.get('session_duration_ms', 0)
    lines_added = metrics.get('lines_added', 0)
    lines_removed = metrics.get('lines_removed', 0)

    api_time_s = int(api_duration_ms / 1000)
    session_time_s = int(session_duration_ms / 1000)

    # Timestamps from first/last lines
    first_line = read_first_line(logfile)
    last_line = read_last_line(logfile)

    def extract_ts(line):
        line = line.strip()
        if line and line[0] == '2' and len(line) > 24 and line[4] == '-':
            ts = line[:24]
            return ts if ts.endswith('Z') else ts + 'Z'
        return None

    start_time = extract_ts(first_line) or now
    end_time = extract_ts(last_line) or now

    # Work dir from arg or log
    work_dir = args.work_dir
    if not work_dir:
        header = read_head_bytes(logfile, 50000)
        m = re.search(r'"cwd":\s*"([^"]+)"', header)
        if m:
            work_dir = m.group(1)

    git_branch = ''
    if work_dir:
        try:
            r = subprocess.run(['git', '-C', work_dir, 'rev-parse', '--abbrev-ref', 'HEAD'],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                git_branch = r.stdout.strip()
        except Exception:
            pass

    # Extract per-model data from shutdown properties (token counts)
    # and merge with assistant_usage data (accurate premium counts)
    models = {}
    model_pattern = re.compile(r'^model_(.+?)_(input_tokens|output_tokens|cache_read_tokens|request_count|request_cost)$')
    for key, val in props.items():
        m = model_pattern.match(key)
        if m:
            model_name = m.group(1)
            field = m.group(2)
            if model_name not in models:
                models[model_name] = {}
            models[model_name][field] = val

    # Override request_cost with accurate values from assistant_usage
    for model_name, udata in usage_models.items():
        if model_name not in models:
            models[model_name] = {}
        models[model_name]['request_cost'] = str(round(udata['cost']))
        if 'request_count' not in models[model_name]:
            models[model_name]['request_count'] = str(udata['calls'])

    # Build raw summary
    raw_lines = [f"Total usage est: {total_premium} Premium requests",
                 f"API time spent: {api_time_s}s"]
    if session_time_s >= 3600:
        h, rem = divmod(session_time_s, 3600)
        mn, s = divmod(rem, 60)
        raw_lines.append(f"Total session time: {h}h {mn}m {s}s")
    else:
        mn, s = divmod(session_time_s, 60)
        raw_lines.append(f"Total session time: {mn}m {s}s")
    raw_lines.append(f"Total code changes: +{lines_added} -{lines_removed}")
    if models:
        raw_lines.append("Breakdown by AI model:")
        for name in sorted(models, key=lambda n: -int(models[n].get('request_cost', 0))):
            md = models[name]
            mc = int(md.get('request_cost', 0))
            raw_lines.append(f"  {name}  {fmt_tokens(md.get('input_tokens', 0))} in, "
                             f"{fmt_tokens(md.get('output_tokens', 0))} out, "
                             f"{fmt_tokens(md.get('cache_read_tokens', 0))} cached "
                             f"(Est. {mc} Premium requests)")

    # UPSERT session (log_file UNIQUE constraint drives ON CONFLICT)
    raw_metrics = '\n'.join(raw_lines)
    run_sqlite(args.db_path,
        f"INSERT INTO sessions (session_num, log_file, log_file_mtime, started_at, ended_at, work_dir, git_branch, "
        f"premium_requests, api_time_seconds, session_time_seconds, lines_added, lines_removed, raw_metrics) "
        f"VALUES ({args.session_num}, '{sql_esc(log_basename)}', '{sql_esc(file_mtime)}', "
        f"'{sql_esc(start_time)}', '{sql_esc(end_time)}', '{sql_esc(work_dir)}', '{sql_esc(git_branch)}', "
        f"{total_premium}, {api_time_s}, {session_time_s}, {lines_added}, {lines_removed}, "
        f"'{sql_esc(raw_metrics)}') "
        f"ON CONFLICT(log_file) DO UPDATE SET "
        f"log_file_mtime = '{sql_esc(file_mtime)}', no_op = 0, "
        f"started_at = '{sql_esc(start_time)}', ended_at = '{sql_esc(end_time)}', "
        f"work_dir = '{sql_esc(work_dir)}', git_branch = '{sql_esc(git_branch)}', "
        f"premium_requests = {total_premium}, api_time_seconds = {api_time_s}, "
        f"session_time_seconds = {session_time_s}, lines_added = {lines_added}, "
        f"lines_removed = {lines_removed}, raw_metrics = '{sql_esc(raw_metrics)}'",
        fetch=False)

    session_id = run_sqlite(args.db_path,
        f"SELECT id FROM sessions WHERE log_file = '{sql_esc(log_basename)}'")

    # Clear old model_usage rows (idempotent for new inserts, necessary for reprocessing)
    run_sqlite(args.db_path, f"DELETE FROM model_usage WHERE session_id = {session_id}", fetch=False)

    # Insert per-model usage
    for name, md in models.items():
        mc = int(md.get('request_cost', 0))
        run_sqlite(args.db_path,
            f"INSERT INTO model_usage (session_id, model_name, tokens_in, tokens_out, tokens_cached, premium_requests) "
            f"VALUES ({session_id}, '{sql_esc(name)}', '{fmt_tokens(md.get('input_tokens', 0))}', "
            f"'{fmt_tokens(md.get('output_tokens', 0))}', '{fmt_tokens(md.get('cache_read_tokens', 0))}', {mc})",
            fetch=False)

    print(f"OK {log_basename}: {total_premium} premium, {api_time_s}s api, +{lines_added} -{lines_removed}")
    for name in sorted(models, key=lambda n: -int(models[n].get('request_cost', 0))):
        md = models[name]
        mc = int(md.get('request_cost', 0))
        print(f"  {name}: {fmt_tokens(md.get('input_tokens', 0))} in, "
              f"{fmt_tokens(md.get('output_tokens', 0))} out, "
              f"{fmt_tokens(md.get('cache_read_tokens', 0))} cached "
              f"({mc} premium, {md.get('request_count', 0)} calls)")


if __name__ == '__main__':
    main()
