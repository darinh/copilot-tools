"""Who invoked the operator, and from where.

``operator.log`` records what the operator *did*. It has never recorded who
asked, and on 2026-08-03 that gap cost an evening: every loop on the machine
gave up within nine seconds of the others, and the log could say only that
five unrelated projects had each exited unexpectedly five times. Nothing in it
distinguished a human typing ``operator stop`` from an agent shelling out to
``operator send`` from a third-party supervisor restarting the world. The
answer turned out to be visible in the process tree the whole time -- the
loops were being started by a launcher nobody had thought to look for -- but
the process tree is gone the moment the process is, and the log kept none of
it.

This module writes one JSON line per invocation to ``~/.operator/trace.jsonl``
naming the argv, the working directory, and the *source*: a user at a
terminal, an agent inside a Copilot session, the operator's own background
supervisor, or something external. It is deliberately a separate file from
``operator.log``: that log is read by humans watching a run, and one line per
CLI call would drown it.

Three rules shape everything here.

**Tracing must never be able to break the operator.** This runs at the top of
every invocation, including the supervisor that keeps unattended sessions
alive. A tracer that raises would take down the thing it exists to
investigate, so every public function swallows its own failures and the
recorder degrades to writing nothing. That is the one place in this repo where
losing an answer silently is the right trade -- but see the next rule for what
it is not allowed to do.

**"Could not tell" never collapses into an answer.** The ancestry walk returns
``None`` when the process tree could not be read, never ``[]``: an empty list
means "this process has no recorded ancestors", which is a claim, and a false
one. Likewise :func:`classify` reports ``unknown`` rather than guessing
``user``. This module is instrumentation for a failure that was invisible
precisely because a missing observation read as a normal one, and reproducing
that shape inside the fix would be its own punchline.

**Nothing here interprets the trace.** No heuristic decides which launcher is
legitimate, because the launcher that mattered on 2026-08-03 was one no
list would have contained. The classifier reports what it observed and names
the executable; a human reads the file and draws the conclusion.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = os.name == "nt"

#: Longest single argv element kept verbatim. Long arguments are message
#: bodies and prompts far more often than they are flags, and this file is a
#: provenance record rather than a transcript.
_MAX_ARG = 120

#: Rotate at this size. One line is a few hundred bytes, so this is on the
#: order of a hundred thousand invocations -- long enough to cover an incident
#: that unfolded over days, short enough not to become the largest file in
#: the directory.
_MAX_BYTES = 8 * 1024 * 1024

#: Subcommands whose trailing free text is a message body rather than a flag.
#: The text is replaced by its length: knowing that `send` carried 400
#: characters is what a provenance record needs, and the 400 characters
#: themselves are the recipient's business.
_REDACT_TRAILING = {"send"}

#: Executable stems that mean "a Copilot session is above us in the tree", so
#: this invocation came from an agent rather than from a person.
_COPILOT_STEMS = frozenset({"copilot"})

#: Interactive shells. Used only to strengthen a `user` verdict that a tty has
#: already suggested -- never to weaken one, because an unrecognised shell is
#: not evidence of anything.
_SHELL_STEMS = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh",
    "powershell", "pwsh", "cmd", "wt", "windowsterminal",
    "conhost", "openconsole", "alacritty", "wezterm", "kitty",
})


def trace_path(operator_home: Path) -> Path:
    return Path(operator_home) / "trace.jsonl"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── process ancestry ────────────────────────────────────────────
#
# Three implementations, one contract: given a pid, answer "what is its parent
# and what is it running", or say you could not tell. `None` is a real answer
# here and is never to be smoothed into an empty result.


def _stem(name: str) -> str:
    """The lowercase executable name without directory or extension.

    Both separators are split on, not just the host's. `os.path.basename`
    leaves a Windows path intact on POSIX, so `C:\\x\\copilot.exe` would be
    read whole and never match `copilot` -- and a trace is a file, which can
    be written on one machine and read on another.
    """
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _win_process_table() -> "dict[int, tuple[int, str]] | None":
    """``{pid: (ppid, exe_name)}`` for every visible process, or ``None``.

    Uses the ToolHelp snapshot, which needs no special privilege and is a
    single call for the whole table -- one syscall beats walking ``ps`` per
    generation, and this runs on every operator invocation.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declared, not inferred. ctypes defaults every restype to `c_int`,
        # which on 64-bit Windows truncates a HANDLE and — worse — turns the
        # INVALID_HANDLE_VALUE returned by a failed snapshot into -1, so the
        # guard below would compare it against c_void_p(-1) (2**64-1), miss,
        # and walk on with a handle that was never valid.
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                     wintypes.DWORD]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            table: dict[int, tuple[int, str]] = {}
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                table[int(entry.th32ProcessID)] = (
                    int(entry.th32ParentProcessID), str(entry.szExeFile))
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
            # `table`, not `table or None`: the snapshot was taken, so an empty
            # result is something we read, not something we failed to read.
            return table
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return None


def _win_image_path(pid: int) -> "str | None":
    """Full executable path for ``pid``, or ``None`` if it cannot be read.

    The ToolHelp table gives only a bare filename, and a bare filename is not
    enough to tell two launchers apart: on the machine this module was written
    for, the third-party supervisor and the operator's own children were both
    ``python.exe``, and only the path distinguished them. Failure here is
    ordinary -- protected and elevated processes refuse the handle -- so it is
    reported as ``None`` rather than as an empty string that would read like
    a process with no path.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        # PROCESS_QUERY_LIMITED_INFORMATION: the weakest right that answers
        # this question, so it succeeds for processes we may not open fully.
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return None
            return buf.value or None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


class _TreeUnreadable(Exception):
    """Raised when the process tree could not be read, as distinct from read
    and found empty. It exists so a permission failure cannot arrive at
    ``classify`` wearing the same clothes as a genuine dead end."""


def _posix_parent(pid: int) -> "tuple[int, str] | None":
    """``(ppid, command)`` for ``pid`` on Linux.

    ``None`` means the process is gone -- a real dead end. A tree that could
    not be read raises ``_TreeUnreadable`` instead, because those two answers
    lead to opposite conclusions and ``None`` for both would let "we were not
    allowed to look" be reported as "there is nothing above this process".
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8",
                                                   errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _TreeUnreadable(str(exc)) from exc
    # The comm field is parenthesised and may itself contain spaces and
    # parentheses, so the fields after it are found from the LAST ')'.
    close = stat.rfind(")")
    if close == -1:
        return None
    rest = stat[close + 2:].split()
    if len(rest) < 2:
        return None
    try:
        ppid = int(rest[1])
    except ValueError:
        return None
    name = ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        name = raw.split(b"\0")[0].decode("utf-8", "replace")
    except OSError:
        pass
    if not name:
        open_paren = stat.find("(")
        if open_paren != -1 and close > open_paren:
            name = stat[open_paren + 1:close]
    return ppid, name


def _ps_process_table() -> "dict[int, tuple[int, str]] | None":
    """``{pid: (ppid, command)}`` via one ``ps`` call, for macOS and BSD."""
    try:
        import subprocess
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    table: dict[int, tuple[int, str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]),
                                    parts[2] if len(parts) > 2 else "")
        except ValueError:
            continue
    # `table`, not `table or None`: ps ran and exited 0, so an empty result is
    # an answer we read rather than a failure to read one.
    return table


def _procfs_available() -> bool:
    """True when Linux's ``/proc`` can be used for the ancestry walk.

    A wrong ``False`` is harmless here, and that is the reason this probe is
    allowed to be two-valued: the caller falls through to the ``ps`` table,
    which answers the same question on every platform that has ``/proc``. The
    failure that matters in this module is a wrong *answer about ancestry*,
    and both branches lead to one that is either right or explicitly ``None``.
    """
    try:
        return Path("/proc").is_dir()  # probe-ok: a wrong False falls back to ps
    except OSError:
        return False


def ancestry(pid: "int | None" = None,
             limit: int = 12) -> "list[dict] | None":
    """The process chain above ``pid``, nearest parent first.

    Returns ``None`` when the process table could not be read at all. That is
    the whole point of the return type: an empty list is a statement that this
    process has no ancestors, which is never true, and a caller that cannot
    tell the two apart would report a machine it failed to examine exactly as
    it reports a machine with nothing to find.

    The walk stops at pid 0/1, at ``limit`` generations, or on a cycle -- pids
    are recycled, and a table read while processes are exiting can contain a
    loop that would otherwise spin here forever.
    """
    try:
        current = int(pid if pid is not None else os.getppid())
    except Exception:
        return None

    table: "dict[int, tuple[int, str]] | None"
    if IS_WINDOWS:
        table = _win_process_table()
        if table is None:
            return None
    elif _procfs_available():
        table = None  # walked one generation at a time below
    else:
        table = _ps_process_table()
        if table is None:
            return None

    chain: list[dict] = []
    seen: set[int] = set()
    try:
        while current and current > 0 and len(chain) < limit:
            if current in seen:
                break
            seen.add(current)

            if table is not None:
                entry = table.get(current)
                if entry is None:
                    break
                ppid, name = entry
                path = _win_image_path(current) if IS_WINDOWS else None
            else:
                got = _posix_parent(current)
                if got is None:
                    break
                ppid, name = got
                path = name or None

            chain.append({
                "pid": current,
                "name": os.path.basename(name) if name else None,
                "path": path,
            })
            current = ppid
    except _TreeUnreadable:
        # Whatever was gathered so far is true, but it is a prefix, and a
        # prefix is indistinguishable from a complete chain once returned.
        # The dangerous direction is the silent one: an incomplete chain with
        # no copilot ancestor in it reads as "launched by a human". Report
        # that we could not look rather than let a partial answer pass as a
        # whole one.
        return None

    # A table that was readable but produced nothing for our own parent is a
    # genuine dead end rather than a failure to look, so an empty chain here
    # is reported as an empty chain. It is still distinguishable from `None`.
    return chain


# ── source attribution ──────────────────────────────────────────


def classify(chain: "list[dict] | None", argv: "list[str]",
             tty: "bool | None") -> dict:
    """Say where this invocation came from.

    ``kind`` is one of:

    ``supervisor``
        The operator's own background process, re-exec'd with ``--_supervise``.
        Decided from argv rather than from the tree, because it is the one
        case the operator knows about itself for certain.
    ``agent``
        A Copilot session is an ancestor, so an agent shelled out to the
        operator. This is the case that had no evidence at all before: an
        agent running ``operator send`` was indistinguishable from a human
        typing it.
    ``user``
        Attached to a terminal, with a recognised shell above us.
    ``external``
        The tree was readable and contains neither -- something else started
        this. The executable is named rather than judged; the launcher worth
        finding is by definition the one no allow-list anticipated.
    ``orphan``
        The tree was readable and the parent is *not in it*: whatever launched
        this has already exited. That is a finding, not a failure to look, and
        it is deliberately not ``unknown`` -- a reader filtering for the
        machine they could not examine must not be handed the processes whose
        launcher died, which during an incident is a distinction worth having.
    ``unknown``
        The process tree could not be read. Never merged into ``user``.
    """
    if "--_supervise" in argv:
        return {"kind": "supervisor", "why": "argv carries --_supervise"}

    if chain is None:
        return {"kind": "unknown",
                "why": "the process tree could not be read"}

    for proc in chain:
        for field in (proc.get("path"), proc.get("name")):
            if field and _stem(field) in _COPILOT_STEMS:
                return {"kind": "agent",
                        "why": f"copilot session at pid {proc.get('pid')}",
                        "session_pid": proc.get("pid")}

    parent = chain[0] if chain else None
    parent_name = (parent or {}).get("name") or ""
    parent_stem = _stem(parent_name) if parent_name else ""

    if tty and parent_stem in _SHELL_STEMS:
        return {"kind": "user", "why": f"tty, launched from {parent_name}"}
    if tty:
        # A readable tree with no ancestors rules out a Copilot session above
        # us, so an attached terminal really is evidence of a person here --
        # which it is not when the tree could not be read at all, because an
        # agent's shell has a tty too.
        return {"kind": "user",
                "why": f"tty, parent {parent_name or 'not in the process table'}"}
    if parent is None:
        return {"kind": "orphan",
                "why": "the process tree was readable but the parent is no "
                       "longer in it -- whatever launched this has exited"}
    return {"kind": "external",
            "why": f"no tty; launched by {parent.get('path') or parent_name}",
            "launcher": parent.get("path") or parent_name}


# ── recording ───────────────────────────────────────────────────


_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9][A-Za-z0-9._-]*$|^-[A-Za-z]$")


def _safe_argv(argv: "list[str]") -> "list[str]":
    """Argv with message bodies replaced and long values truncated.

    "Starts with a dash" is not the same as "is a flag": an armoured key
    begins ``-----BEGIN``, and treating that as a flag walks a secret straight
    past the redaction and into the file. A flag has a shape, so the shape is
    what is matched.
    """
    out: list[str] = []
    redacting = bool(argv) and argv[0] in _REDACT_TRAILING
    for i, arg in enumerate(argv):
        text = str(arg)
        is_flag = bool(_FLAG_RE.match(text))
        # A value belonging to the preceding flag (`--to NAME`), as opposed to
        # a positional argument. Flag values are names and counts, which are
        # the attribution this file exists to record; positionals after a
        # `send` are prose the user typed.
        prev = str(argv[i - 1]) if i else ""
        is_flag_value = bool(_FLAG_RE.match(prev)) and "=" not in prev
        if redacting and i >= 1 and not is_flag and not is_flag_value:
            # Length is not a safety property. The first version redacted only
            # arguments longer than 40 characters, which wrote `operator send
            # "hunter2"` into the trace verbatim; a secret is not less of one
            # for being short.
            out.append(f"<redacted:{len(text)} chars>")
        elif len(text) > _MAX_ARG:
            out.append(text[:_MAX_ARG] + f"…<+{len(text) - _MAX_ARG}>")
        else:
            out.append(text)
    return out


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size < _MAX_BYTES:
            return
    except OSError:
        return
    try:
        path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def _append(path: Path, record: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def record_invocation(operator_home: Path, argv: "list[str]") -> "dict | None":
    """Write the ``invoke`` line and return the context for :func:`record_exit`.

    Returns ``None`` if nothing could be written, which is also the signal to
    :func:`record_exit` that there is no open record to close. Never raises:
    a failure to trace must not become a failure to run.
    """
    try:
        argv = list(argv)
        try:
            tty: "bool | None" = sys.stdin.isatty()
        except Exception:
            tty = None

        chain = ancestry()
        source = classify(chain, argv, tty)
        trace_id = uuid.uuid4().hex[:16]
        record = {
            "ts": _utcnow(),
            "event": "invoke",
            "trace_id": trace_id,
            "pid": os.getpid(),
            "ppid": _safe_ppid(),
            "argv": _safe_argv(argv),
            "cwd": str(Path.cwd()) if _cwd_readable() else None,
            "tty": tty,
            "source": source,
            "ancestry": chain,
        }
        wrote = _append(trace_path(operator_home), record)
        if not wrote:
            return None
        return {"trace_id": trace_id, "started": time.monotonic(),
                "home": Path(operator_home)}
    except Exception:
        return None


def record_exit(context: "dict | None", rc: int) -> None:
    """Close the record opened by :func:`record_invocation`. Never raises."""
    if not context:
        return
    try:
        elapsed = time.monotonic() - float(context.get("started") or 0)
        _append(trace_path(context["home"]), {
            "ts": _utcnow(),
            "event": "exit",
            "trace_id": context.get("trace_id"),
            "pid": os.getpid(),
            "rc": rc,
            "ms": int(elapsed * 1000),
        })
    except Exception:
        return


def record_session_exit(operator_home: Path, *, instance: str, session: int,
                        pid: "int | None", markers: "dict",
                        consecutive: int, limit: int) -> None:
    """Record that a supervised copilot session was found gone. Never raises.

    This is the event the trace was built for and the one an invocation log
    cannot see. When seven loops died together on 2026-08-03 no operator
    command was run at all -- each supervisor was already inside its poll
    loop, so there was nothing to attribute. ``operator.log`` said "copilot
    exited unexpectedly" seven times and could say nothing else, because the
    supervisor never waits on the child: it polls liveness, and a process that
    is gone leaves no exit code behind to read.

    So "unexpected" here means only *unexplained* -- no stop, detach or
    restart marker was set. It is not evidence of a crash, and the distinction
    matters: the copilot logs for that incident end with an orderly
    ``[shutdown] Shutdown complete``, and the extensions died with
    ``0xC000013A`` (``STATUS_CONTROL_C_EXIT``), which is a console control
    event delivered to every process sharing the console rather than a fault
    in any one session. What is recorded here is therefore the observation and
    the marker states it was judged against, so a later reader can re-judge
    it. Nothing here decides what killed the session.
    """
    try:
        _append(trace_path(Path(operator_home)), {
            "ts": _utcnow(),
            "event": "session_exit",
            "pid": os.getpid(),
            "instance": str(instance),
            "session": session,
            "session_pid": pid,
            # Tri-state per marker: True set, False absent, None unreadable.
            # An unreadable marker is why the supervisor waits instead of
            # relaunching, so flattening it here would hide the reason.
            "markers": dict(markers),
            "consecutive": consecutive,
            "limit": limit,
            "giving_up": consecutive >= limit,
        })
    except Exception:
        return


def _safe_ppid() -> "int | None":
    try:
        return os.getppid()
    except Exception:
        return None


def _cwd_readable() -> bool:
    try:
        Path.cwd()
        return True
    except OSError:
        return False


def read_records(operator_home: Path, limit: int = 50,
                 kind: "str | None" = None) -> "list[dict] | None":
    """The most recent trace records, newest last.

    ``None`` means the trace could not be read, which is not the same as a
    trace with nothing in it -- the distinction this whole module is about.
    """
    path = trace_path(operator_home)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError:
        return None

    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            # A torn final line from a concurrent append is expected; a record
            # we cannot parse is skipped rather than allowed to end the read.
            continue
        if not isinstance(rec, dict):
            continue
        if kind and (rec.get("source") or {}).get("kind") != kind:
            continue
        out.append(rec)
    return out[-limit:] if limit else out
