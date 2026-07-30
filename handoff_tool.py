#!/usr/bin/env python3
"""Atomic session handoff for Copilot CLI agents.

Writes ``next-session.md`` for the project and raises the operator's restart
marker so the loop picks up a fresh session. Cross-platform.

Differences from the bash predecessor:

* Catalog paths are compared case-insensitively on Windows, where
  ``C:\\Users\\x`` and ``c:\\users\\x`` denote the same directory.
* Instance inference compares real paths rather than string prefixes, so
  ``/srv/app2`` is no longer treated as living under ``/srv/app``.
* The transitional dual-write to the legacy ``~/.copilot/restart`` path is not
  carried over; operator state has lived under ``~/.operator`` for some time.
"""
from __future__ import annotations

import argparse
import csv
import os
import platform
import sys
from pathlib import Path

# An editable install freezes the module list into its import finder, so a
# module added to this directory after the last `pip install -e .` is invisible
# to the installed `handoff` entry point even though the file sits right here.
# Making our own directory importable turns that stale-install failure into a
# no-op instead of a traceback on startup.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from operator_console import enable_utf8_output               # noqa: E402
from operator_mux import Mux, MuxError, safe_instance_id      # noqa: E402
from project_paths import primary_repo_root                   # noqa: E402

IS_WINDOWS = platform.system() == "Windows"
CATALOG = Path.home() / ".copilot" / "projects" / "catalog.csv"


def state_dir() -> Path:
    override = os.environ.get("COPILOT_OPERATOR_HOME")
    root = Path(override) if override else Path.home() / ".operator"
    return root / "restart"


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def normalize(path) -> str:
    resolved = str(Path(path).resolve())
    return resolved.lower() if IS_WINDOWS else resolved


def same_or_within(child: str, parent: str) -> bool:
    """True when child is parent or lives beneath it, comparing path parts."""
    try:
        cp = Path(child)
        pp = Path(parent)
    except (TypeError, ValueError):
        return False
    if cp == pp:
        return True
    try:
        cp.relative_to(pp)
        return True
    except ValueError:
        return False


def resolve_guid(project_root) -> str:
    if not CATALOG.is_file():
        die(f"Catalog not found: {CATALOG}")
    target = normalize(project_root)
    try:
        with open(CATALOG, "r", encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2:
                    continue
                path, guid = row[0].strip().strip('"'), row[1].strip().strip('"')
                if not path:
                    continue
                try:
                    if normalize(path) == target:
                        return guid
                except OSError:
                    continue
    except OSError as exc:
        die(f"Cannot read catalog {CATALOG}: {exc}")
    die(f"No catalog entry for: {target}\n"
        f"Add it with a line such as:\n  \"{Path(project_root).resolve()}\",<guid>")


def managed_ids() -> set[str]:
    found: set[str] = set()
    d = state_dir()
    if not d.is_dir():
        return found
    for path in d.iterdir():
        if path.suffix in (".managed", ".state"):
            found.add(path.name.rsplit(".", 1)[0])
    return found


def infer_instance(project_root, mux: Mux) -> str | None:
    target = str(Path(project_root).resolve())
    managed = managed_ids()
    matches = []
    try:
        sessions = mux.list_sessions()
    except MuxError:
        return None
    for session in sessions:
        if session not in managed:
            continue
        cwd = mux.pane_current_path(session)
        if cwd and same_or_within(cwd, target):
            matches.append(session)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print("Multiple operator instances found for this project:", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        print("Specify one with --instance NAME", file=sys.stderr)
        sys.exit(1)
    return None


def render(status: str, in_progress: str, next_steps: str,
           context: str, prompt: str) -> str:
    parts = ["# Session Handoff", "", "## Status", status, ""]
    if in_progress:
        parts += ["## In Progress", in_progress, ""]
    parts += ["## Next Steps", next_steps, ""]
    if context:
        parts += ["## Context", context, ""]
    if prompt:
        parts += ["## Prompt", prompt, ""]
    return "\n".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff",
        description="Atomic session handoff for Copilot CLI agents.",
    )
    parser.add_argument("--instance", default="",
                        help="Operator instance name. Inferred when omitted.")
    parser.add_argument("--status", default="", help="What was just completed")
    parser.add_argument("--next", dest="next_steps", default="",
                        help="Prioritized next steps")
    parser.add_argument("--in-progress", dest="in_progress", default="",
                        help="What was actively being worked on")
    parser.add_argument("--context", default="",
                        help="Key decisions, gotchas, architectural notes")
    parser.add_argument("--prompt", default="",
                        help="Ready-to-execute prompt for the next session")
    parser.add_argument("--project-root", dest="project_root", default=None,
                        help="Project root (default: cwd), used for GUID lookup")
    return parser


def main(argv: list[str] | None = None) -> int:
    enable_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.status:
        die("Missing required: --status")
    if not args.next_steps:
        die("Missing required: --next")

    project_root = Path(args.project_root or Path.cwd())
    if not project_root.is_dir():
        die(f"Directory not found: {project_root}")

    mux = Mux()
    instance_name = args.instance
    if not instance_name:
        inferred = infer_instance(project_root, mux) if mux.available() else None
        if not inferred:
            die("Cannot infer instance. Use --instance NAME")
        instance_name = inferred
        instance_id = inferred
    else:
        instance_id = safe_instance_id(instance_name)

    # A missing session is a warning, not a failure: the handoff file is still
    # worth writing so the next session can pick it up.
    if mux.available() and not mux.has_session(instance_id):
        print(f"Warning: no running session '{instance_name}' found. "
              "Handoff file will be written but restart may not trigger.",
              file=sys.stderr)

    guid = resolve_guid(primary_repo_root(project_root))
    project_dir = Path.home() / ".copilot" / "projects" / guid
    handoff_file = project_dir / "next-session.md"
    marker = state_dir() / instance_id

    project_dir.mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)

    handoff_file.write_text(
        render(args.status, args.in_progress, args.next_steps,
               args.context, args.prompt),
        encoding="utf-8",
    )
    marker.touch()

    print(f"✅ Handoff written: {handoff_file}")
    print(f"✅ Restart signal: {marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
