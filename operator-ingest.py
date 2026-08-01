#!/usr/bin/env python3
"""Parse copilot process logs and store usage metrics in SQLite.

Uses the session_shutdown telemetry event which contains the complete
session summary: total premium requests, API duration, session duration,
lines changed, and per-model token breakdowns — all in one event.

Usage:
    operator-ingest.py <logfile> <db_path> [--session-num N] [--work-dir DIR] [--force]

Exit codes: 0=success/skip, 1=error, 2=no shutdown event
"""
import re, sys, subprocess, os, argparse, json, stat
from datetime import datetime, timezone


def run_cmd(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout, r.returncode


def run_sqlite(db_path, sql):
    out, _ = run_cmd(['sqlite3', db_path, sql])
    return out.strip()


def sql_esc(s):
    return str(s).replace("'", "''")


def measured(metrics, key, scale=1):
    """The metric, or None when the shutdown event never reported it.

    Defaulting to 0 would fabricate a measurement: "this session changed no
    code" would be indistinguishable from "nobody looked", and every average
    over the column would be dragged toward zero by sessions never observed.
    Kept identical to operator_ingest.py so both ingesters agree.
    """
    if key not in metrics:
        return None
    return int((metrics.get(key) or 0) / scale)


def sql_num(value):
    """SQL literal for a number that may be unknown."""
    return 'NULL' if value is None else str(int(value))


def fmt_changes(added, removed):
    if added is None or removed is None:
        return "unknown"
    return f"+{added} -{removed}"


def fmt_tokens(n):
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000: return f"{n/1_000:.1f}k"
    return str(n)


def extract_shutdown_event(logfile):
    """Use grep to pull session_shutdown properties and metrics."""
    out, rc = run_cmd(['grep', '-B', '1', '-A', '150', '"kind": "session_shutdown"', logfile], timeout=60)
    if rc != 0 or not out:
        return None

    # Find the complete JSON object (need ~97 lines for the full event)
    start = out.find('{')
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(out)):
        if out[i] == '{': depth += 1
        elif out[i] == '}':
            depth -= 1
            if depth == 0:
                raw = out[start:i+1]
                # Fix trailing commas before closing braces (common in log format)
                raw = re.sub(r',(\s*[}\]])', r'\1', raw)
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


def extract_premium_from_usage(logfile):
    """Sum assistant_usage cost fields for accurate premium request counts.

    session_shutdown.total_premium_requests is unreliable (reports last call's
    cost, not the sum). The assistant_usage cost field is the per-call premium
    request multiplier — summing it gives the actual billed amount.

    Returns: {model_name: {'cost': float, 'calls': int}, ...}, total_cost
    """
    out, rc = run_cmd(['grep', '-A', '20', '"kind": "assistant_usage"', logfile], timeout=60)
    if rc != 0 or not out:
        return {}, 0

    models = {}
    total = 0.0
    lines = out.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if '"model":' in line:
            model = line.split(':', 1)[1].strip().strip('",')
            # Skip garbage model names from malformed log entries
            if not model or ' ' in model or ')' in model or len(model) > 40:
                i += 1
                continue
            for j in range(i+1, min(i+20, len(lines))):
                cline = lines[j].strip()
                if cline.startswith('"cost":'):
                    cost = float(cline.split(':', 1)[1].strip().rstrip(','))
                    if model not in models:
                        models[model] = {'cost': 0.0, 'calls': 0}
                    models[model]['cost'] += cost
                    models[model]['calls'] += 1
                    total += cost
                    break
        i += 1

    return models, round(total)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('logfile')
    parser.add_argument('db_path')
    parser.add_argument('--session-num', type=int, default=0)
    parser.add_argument('--work-dir', default='')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    logfile = os.path.abspath(args.logfile)
    log_basename = os.path.basename(logfile)

    # `os.stat` rather than `os.path.isfile`. os.path swallows every OSError
    # and answers False, so a log that is there but cannot be examined -- a
    # denial, a dangling symlink, a disconnected network home -- was reported
    # to the user as "not found", which sends them looking for a file that is
    # sitting right where they left it. This script stays stdlib-only (it is
    # invoked directly by operator.sh), so it spells out what
    # `install_manifest.file_present` does for the importable modules.
    try:
        usable = stat.S_ISREG(os.stat(logfile).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        usable = False
    except OSError as exc:
        print(f"ERROR: {logfile} could not be examined ({exc})",
              file=sys.stderr)
        sys.exit(1)
    if not usable:
        print(f"ERROR: {logfile} not found", file=sys.stderr)
        sys.exit(1)

    # Get current file mtime as ISO timestamp
    file_mtime = datetime.fromtimestamp(
        os.path.getmtime(logfile), tz=timezone.utc
    ).strftime('%Y-%m-%dT%H:%M:%SZ')

    if not args.force:
        existing = run_sqlite(args.db_path,
            f"SELECT log_file_mtime FROM sessions WHERE log_file = '{sql_esc(log_basename)}';")
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
        first_line, _ = run_cmd(['head', '-1', logfile])
        ts = first_line.strip()[:24] if first_line.strip() else now
        if not ts.endswith('Z'):
            ts += 'Z'
        run_sqlite(args.db_path,
            f"INSERT INTO sessions (session_num, log_file, log_file_mtime, no_op, started_at, ended_at) "
            f"VALUES (0, '{sql_esc(log_basename)}', '{sql_esc(file_mtime)}', 1, '{sql_esc(ts)}', '{sql_esc(ts)}') "
            f"ON CONFLICT(log_file) DO UPDATE SET "
            f"log_file_mtime = '{sql_esc(file_mtime)}', no_op = 1, "
            f"started_at = '{sql_esc(ts)}', ended_at = '{sql_esc(ts)}';")
        print(f"SKIP {log_basename} (no shutdown event)")
        sys.exit(2)

    props = event.get('properties', {})
    metrics = event.get('metrics', {})

    # Get accurate premium request counts from assistant_usage events.
    # session_shutdown.total_premium_requests is unreliable.
    usage_models, usage_premium = extract_premium_from_usage(logfile)
    total_premium = usage_premium if usage_premium > 0 else metrics.get('total_premium_requests', 0)

    api_time_s = measured(metrics, 'total_api_duration_ms', 1000)
    session_time_s = measured(metrics, 'session_duration_ms', 1000)
    lines_added = measured(metrics, 'lines_added')
    lines_removed = measured(metrics, 'lines_removed')

    # Timestamps from head/tail
    first_line, _ = run_cmd(['head', '-1', logfile])
    last_line, _ = run_cmd(['tail', '-1', logfile])

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
        header, _ = run_cmd(['head', '-c', '50000', logfile])
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
        except:
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
                 "API time spent: unknown" if api_time_s is None
                 else f"API time spent: {api_time_s}s"]
    if session_time_s is None:
        raw_lines.append("Total session time: unknown")
    elif session_time_s >= 3600:
        h, rem = divmod(session_time_s, 3600)
        mn, s = divmod(rem, 60)
        raw_lines.append(f"Total session time: {h}h {mn}m {s}s")
    else:
        mn, s = divmod(session_time_s, 60)
        raw_lines.append(f"Total session time: {mn}m {s}s")
    raw_lines.append(
        f"Total code changes: {fmt_changes(lines_added, lines_removed)}")
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
    session_id = run_sqlite(args.db_path, f"""
        INSERT INTO sessions (session_num, log_file, log_file_mtime, started_at, ended_at, work_dir, git_branch,
                              premium_requests, api_time_seconds, session_time_seconds,
                              lines_added, lines_removed, raw_metrics)
        VALUES ({args.session_num}, '{sql_esc(log_basename)}', '{sql_esc(file_mtime)}',
                '{sql_esc(start_time)}', '{sql_esc(end_time)}',
                '{sql_esc(work_dir)}', '{sql_esc(git_branch)}',
                {total_premium}, {sql_num(api_time_s)}, {sql_num(session_time_s)},
                {sql_num(lines_added)}, {sql_num(lines_removed)}, '{sql_esc(chr(10).join(raw_lines))}')
        ON CONFLICT(log_file) DO UPDATE SET
            log_file_mtime = '{sql_esc(file_mtime)}',
            no_op = 0,
            started_at = '{sql_esc(start_time)}',
            ended_at = '{sql_esc(end_time)}',
            work_dir = '{sql_esc(work_dir)}',
            git_branch = '{sql_esc(git_branch)}',
            premium_requests = {total_premium},
            api_time_seconds = {sql_num(api_time_s)},
            session_time_seconds = {sql_num(session_time_s)},
            lines_added = {sql_num(lines_added)},
            lines_removed = {sql_num(lines_removed)},
            raw_metrics = '{sql_esc(chr(10).join(raw_lines))}';
        SELECT id FROM sessions WHERE log_file = '{sql_esc(log_basename)}';
    """)

    # Clear old model_usage rows (idempotent for new inserts, necessary for reprocessing)
    run_sqlite(args.db_path, f"DELETE FROM model_usage WHERE session_id = {session_id};")

    # Insert per-model usage
    for name, md in models.items():
        mc = int(md.get('request_cost', 0))
        run_sqlite(args.db_path, f"""
            INSERT INTO model_usage (session_id, model_name, tokens_in, tokens_out, tokens_cached, premium_requests)
            VALUES ({session_id}, '{sql_esc(name)}', '{fmt_tokens(md.get("input_tokens", 0))}',
                    '{fmt_tokens(md.get("output_tokens", 0))}',
                    '{fmt_tokens(md.get("cache_read_tokens", 0))}', {mc});
        """)

    print(f"OK {log_basename}: {total_premium} premium, "
          f"{'?' if api_time_s is None else api_time_s}s api, "
          f"{fmt_changes(lines_added, lines_removed)}")
    for name in sorted(models, key=lambda n: -int(models[n].get('request_cost', 0))):
        md = models[name]
        mc = int(md.get('request_cost', 0))
        print(f"  {name}: {fmt_tokens(md.get('input_tokens', 0))} in, "
              f"{fmt_tokens(md.get('output_tokens', 0))} out, "
              f"{fmt_tokens(md.get('cache_read_tokens', 0))} cached "
              f"({mc} premium, {md.get('request_count', 0)} calls)")


if __name__ == '__main__':
    main()
