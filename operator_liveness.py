#!/usr/bin/env python3
"""Is the agent holding this claim still there? -- and the honest "I cannot tell".

Four signals, cheapest first, exactly as the spec's FR-3 orders them:

1. **Boot id differs** -> DEAD. The unplanned-reboot case, and it costs no
   timeout at all: nothing from the previous boot is running, so there is
   nothing to wait for.
2. **Mux session absent** -> DEAD. Direct and exact, because every agent runs
   inside one.
3. **Pid absent, or present with a different start time** -> DEAD. The
   start-time comparison is what makes this safe after pid reuse; without it
   a recycled pid reads as its dead predecessor still running.
4. **Heartbeat older than ``stale_after``, with 1-3 having concluded nothing**
   -> STALE. Reported, never acted on. This combination means something
   unusual -- a hung process, a clock that moved -- and guessing is how two
   agents end up in one worktree.

The first three are conclusive; the fourth deliberately is not. So every probe
here is **tri-state**: ``True``, ``False``, or ``None`` for *could not tell*.
A probe that answers "absent" when it means "I could not look" is the whole
failure mode this module exists to avoid -- it does not lose a session, it
hands a live agent's tree to somebody else while the first is still writing
to it.

Nothing here mutates anything. :func:`assess` reads; ``operator work reclaim``
decides, and only for :data:`DEAD`.
"""
from __future__ import annotations

import ctypes
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# An editable install freezes the module list into its import finder, so a
# module added to this directory after the last `pip install -e .` is invisible
# to the installed entry points even though the file sits right here.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from operator_mux import Mux                                   # noqa: E402
from work_claims import parse_ts                               # noqa: E402

#: The three verdicts. `STALE` is not a weaker `DEAD`: it is the answer that
#: says the cascade found nothing conclusive and the heartbeat is old, which
#: is a thing to report to a person, not a licence to reassign.
LIVE = "live"
DEAD = "dead"
STALE = "stale"

#: Spec D4. Configurable, and never the sole signal -- a claim is `STALE` on
#: heartbeat age alone and `DEAD` only on one of the three conclusive probes.
DEFAULT_STALE_AFTER = 30 * 60

#: How far apart two *computed* boot instants may be and still be one boot.
#:
#: Only platforms whose boot identity is a timestamp are affected. Linux hands
#: out a kernel-generated uuid, which is exact and compares exactly; Windows
#: and macOS report an instant, and an instant moves -- the kernel adjusts its
#: recorded boot time when the wall clock is corrected, and the tick-count
#: fallback drifts a little on its own.
#:
#: Two minutes, and the direction of the error is the point. Too *wide* costs
#: a conclusive DEAD that the mux and pid probes then have to reach instead;
#: too *narrow* reports a live agent as dead because NTP nudged the clock. The
#: first is a slower answer, the second is two agents in one worktree.
BOOT_INSTANT_TOLERANCE = 120

_LINUX_BOOT_ID = "/proc/sys/kernel/random/boot_id"

#: 100-nanosecond intervals between 1601-01-01 (the Windows epoch) and
#: 1970-01-01 (the Unix one).
_FILETIME_EPOCH_DELTA = 116_444_736_000_000_000

_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87
_ERROR_ACCESS_DENIED = 5
#: The weakest access right that answers "does this pid exist, and when did it
#: start", so the probe succeeds for processes we may not open fully.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

IS_WINDOWS = platform.system() == "Windows"


# ── boot identity ───────────────────────────────────────────────
def _linux_boot_uuid() -> "str | None":
    try:
        raw = Path(_LINUX_BOOT_ID).read_bytes()
    except OSError:
        return None
    token = raw.decode("utf-8", "replace").strip()
    return token or None


def _windows_boot_instant() -> "int | None":
    """Unix seconds at which this Windows box booted, or ``None``.

    ``NtQuerySystemInformation(SystemTimeOfDayInformation)`` first, because it
    reports the boot time the kernel *recorded* rather than one derived from
    an uptime counter. The ``GetTickCount64`` fallback subtracts uptime from
    now, which is correct to within the timer's drift -- covered by
    :data:`BOOT_INSTANT_TOLERANCE`, which is why the fallback is allowed to
    exist at all rather than reporting "cannot tell".
    """
    class _TimeOfDay(ctypes.Structure):
        _fields_ = [
            ("BootTime", ctypes.c_longlong),
            ("CurrentTime", ctypes.c_longlong),
            ("TimeZoneBias", ctypes.c_longlong),
            ("TimeZoneId", ctypes.c_ulong),
            ("Reserved", ctypes.c_ulong),
            ("BootTimeBias", ctypes.c_ulonglong),
            ("SleepTimeBias", ctypes.c_ulonglong),
        ]

    try:
        ntdll = ctypes.WinDLL("ntdll")
        info = _TimeOfDay()
        returned = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(
            3, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned))
        if status == 0 and info.BootTime > 0:
            return int((info.BootTime - _FILETIME_EPOCH_DELTA) // 10_000_000)
    except Exception:
        pass
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        uptime = kernel32.GetTickCount64() / 1000.0
    except Exception:
        return None
    return int(time.time() - uptime)


def _sysctl_boot_instant() -> "int | None":
    """Unix seconds at which this macOS/BSD box booted, or ``None``.

    ``kern.boottime`` prints ``{ sec = 1690000000, usec = 12345 } ...``; only
    the seconds are read, and a line that does not carry them is reported as
    unknown rather than parsed optimistically.
    """
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    marker = "sec ="
    if marker not in text:
        return None
    tail = text.split(marker, 1)[1].strip()
    digits = ""
    for char in tail:
        if not char.isdigit():
            break
        digits += char
    if not digits:
        return None
    return int(digits)


def boot_identity() -> "str | None":
    """This machine's identity for the current boot, or ``None``.

    Two shapes, tagged so they can never be compared across kinds by accident:

    * ``uuid:<token>`` -- Linux's kernel-generated boot id. Exact.
    * ``instant:<unix seconds>`` -- Windows and macOS. Compared with
      :data:`BOOT_INSTANT_TOLERANCE`, because the value moves.

    The tag is not decoration. A claim written on one platform and read on
    another -- or on a machine whose exact source stopped answering -- must
    compare as *unknown*, not as a mismatch, and an untagged string could only
    do the latter.
    """
    if IS_WINDOWS:
        instant = _windows_boot_instant()
        return None if instant is None else f"instant:{instant}"
    token = _linux_boot_uuid()
    if token:
        return f"uuid:{token}"
    instant = _sysctl_boot_instant()
    return None if instant is None else f"instant:{instant}"


def same_boot(recorded: "str | None", current: "str | None") -> "bool | None":
    """Whether two boot identities name the same boot. ``None`` if unknowable.

    Different kinds are unknowable, not different. So is either side being
    missing or unparseable: the caller must not read "we have no boot id for
    this claim" as "this claim is from another boot".
    """
    if not recorded or not current:
        return None
    kind, _, left = str(recorded).partition(":")
    other, _, right = str(current).partition(":")
    if not left or not right or kind != other:
        return None
    if kind == "uuid":
        return left == right
    if kind == "instant":
        try:
            return abs(int(left) - int(right)) <= BOOT_INSTANT_TOLERANCE
        except ValueError:
            return None
    return None


# ── process identity ────────────────────────────────────────────
def _win_open(pid: int):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int,
                                     ctypes.c_ulong]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False,
                                  int(pid))
    return kernel32, handle


def _win_process_present(pid: int, *, opener=None, last_error=None) -> "bool | None":
    # The two seams exist so the failure branches below can be tested: an
    # access-denied process cannot be conjured on demand, and a probe branch
    # nothing can reach is a branch nothing checks.
    opener = opener or _win_open
    last_error = last_error or ctypes.get_last_error
    try:
        kernel32, handle = opener(pid)
    except Exception:
        return None
    if not handle:
        err = last_error()
        if err == _ERROR_INVALID_PARAMETER:
            return False
        if err == _ERROR_ACCESS_DENIED:
            # Refused, therefore it is there. "We were not allowed to look" and
            # "there is nothing there" are opposite answers and this is the one
            # that must not be rounded down to absent.
            return True
        return None
    try:
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_ulong)]
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        # A pid can stay openable after the process exits while somebody holds
        # a handle to it. The exit code is what separates the two.
        return code.value == _STILL_ACTIVE
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _win_start_token(pid: int) -> "str | None":
    try:
        kernel32, handle = _win_open(pid)
    except Exception:
        return None
    if not handle:
        return None
    try:
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_ulong)]
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != _STILL_ACTIVE:
            # An exited process stays openable for as long as somebody holds a
            # handle to it, and it keeps its creation time. Handing that back
            # would name a run that has ended -- and this token exists to tell
            # one run of a pid from the next, so a token for a finished run is
            # the exact ambiguity it is supposed to remove.
            return None
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong)]
        created = ctypes.c_ulonglong(0)
        exited = ctypes.c_ulonglong(0)
        kernel = ctypes.c_ulonglong(0)
        user = ctypes.c_ulonglong(0)
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user))
        if not ok or not created.value:
            return None
        return f"win:{created.value}"
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_present(pid: int) -> "bool | None":
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, but a process. Same reasoning as the Windows
        # access-denied branch: refused is not absent.
        return True
    except OSError:
        return None
    return True


def _linux_start_token(pid: int) -> "str | None":
    """Field 22 of ``/proc/<pid>/stat`` -- start time in clock ticks since boot.

    Opaque on purpose. Nothing here needs to know *when* the process started,
    only whether it is the same process as the one that wrote the claim, and
    equality of the raw field answers that without a unit conversion that could
    be wrong on a kernel with a different ``HZ``.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    # The comm field is parenthesised and may itself contain spaces and
    # parentheses, so the fields after it are found from the LAST ')'.
    close = stat.rfind(")")
    if close == -1:
        return None
    fields = stat[close + 2:].split()
    # stat's field 22 is the 20th after comm, and the split above drops the
    # first two fields (pid and comm) -- so index 19 counting from 'state'.
    if len(fields) < 20:
        return None
    return f"linux:{fields[19]}"


def _ps_start_token(pid: int) -> "str | None":
    """``ps -o lstart=`` for macOS and BSD, kept as the raw string.

    Not parsed: the format is locale-dependent, and every parse of it is a
    chance to turn one process into another. Two reads of the same process
    give byte-identical strings, and that is the entire requirement.
    """
    try:
        proc = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "lstart="],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    token = (proc.stdout or "").strip()
    return f"ps:{token}" if token else None


def _coerce_pid(pid) -> "int | None":
    """``pid`` as a positive integer, or ``None`` if it is not one.

    Deliberately not ``int(pid)``. ``int(1.5)`` is ``1``, so a float pid would
    not be *refused*, it would be silently answered about a different, real
    process -- and the caller would be told, with confidence, whether init was
    running. ``bool`` is an ``int`` subclass in Python and ``int(True)`` is
    likewise pid 1, so it is refused by name.

    Zero and negatives are refused for the same reason rather than as
    tidiness: on POSIX they are signal-group selectors, and ``os.kill(0, 0)``
    probes *our own* process group -- an answer about something nobody asked
    that would always come back True.
    """
    if pid is None or isinstance(pid, bool):
        return None
    if isinstance(pid, int):
        value = pid
    elif isinstance(pid, str):
        text = pid.strip()
        if not text.isdigit():
            return None
        value = int(text)
    else:
        return None
    return value if value > 0 else None


def process_present(pid: "int | None") -> "bool | None":
    """Is there a process with this pid? ``None`` when it cannot be established."""
    pid = _coerce_pid(pid)
    if pid is None:
        return None
    if IS_WINDOWS:
        return _win_process_present(pid)
    return _posix_process_present(pid)


def process_start_token(pid: "int | None") -> "str | None":
    """An opaque token identifying *this* run of ``pid``, or ``None``.

    Compared only for equality, and only against a token recorded for the same
    pid. That is what makes the pid probe safe across reuse: a recycled pid
    carries a different start time, so a claim whose owner died and whose pid
    was handed to something else reads as DEAD rather than as still running.
    """
    pid = _coerce_pid(pid)
    if pid is None:
        return None
    if IS_WINDOWS:
        return _win_start_token(pid)
    token = _linux_start_token(pid)
    if token:
        return token
    return _ps_start_token(pid)


# ── the cascade ─────────────────────────────────────────────────
class SystemProbes:
    """The real probes, gathered so a test can hand over fakes instead.

    Injected rather than monkeypatched because the cascade's logic is the part
    worth testing exhaustively and it must be testable without a reboot, a
    multiplexer or a doomed child process. The probes themselves are covered
    separately, against this machine.
    """

    def __init__(self, mux=None):
        self._mux = mux

    @property
    def mux(self) -> Mux:
        if self._mux is None:
            self._mux = Mux()
        return self._mux

    def boot_identity(self) -> "str | None":
        return boot_identity()

    def session_present(self, session: str) -> "bool | None":
        return self.mux.session_present(session)

    def process_present(self, pid) -> "bool | None":
        return process_present(pid)

    def process_start_token(self, pid) -> "str | None":
        return process_start_token(pid)


class Liveness:
    """A verdict, the reason for it, and every signal that produced it.

    The signals are carried because the verdict alone is not reportable: a
    person asked to confirm that an agent is gone needs to see *which* probe
    said so, and a STALE claim is only actionable if it says what could not be
    established.
    """

    __slots__ = ("verdict", "reason", "signals")

    def __init__(self, verdict: str, reason: str, signals: dict):
        self.verdict = verdict
        self.reason = reason
        self.signals = signals

    @property
    def reclaimable(self) -> bool:
        """Only DEAD. A STALE claim is reported to a person, never taken."""
        return self.verdict == DEAD

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Liveness({self.verdict!r}, {self.reason!r}, {self.signals!r})"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Liveness):
            return NotImplemented
        return (self.verdict == other.verdict and self.reason == other.reason
                and self.signals == other.signals)


def heartbeat_age(claim, now=None) -> "float | None":
    """Seconds since the claim's heartbeat, or ``None`` if it will not parse."""
    stamp = parse_ts(getattr(claim, "heartbeat_at", None))
    if stamp is None:
        return None
    moment = now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - stamp).total_seconds()


def assess(claim, *, probes=None, now=None,
           stale_after: float = DEFAULT_STALE_AFTER) -> Liveness:
    """Judge one claim's owner: :data:`LIVE`, :data:`DEAD` or :data:`STALE`.

    Read-only, always. Returning DEAD is a statement about the world, not an
    instruction -- the caller still has to preserve the dead owner's
    uncommitted work before anything is reassigned (FR-4).
    """
    probes = probes or SystemProbes()
    signals: dict = {"boot": None, "mux": None, "pid": None, "start": None,
                     "heartbeat_age": None}

    same = same_boot(getattr(claim, "boot_id", None), probes.boot_identity())
    signals["boot"] = same
    if same is False:
        return Liveness(DEAD, "boot id differs: the machine has rebooted since "
                              "this claim was taken", signals)

    session = getattr(claim, "mux_session", None)
    if session:
        present = probes.session_present(session)
        signals["mux"] = present
        if present is False:
            return Liveness(DEAD, f"mux session {session!r} is gone", signals)

    pid = getattr(claim, "pid", None)
    if pid:
        running = probes.process_present(pid)
        signals["pid"] = running
        if running is False:
            return Liveness(DEAD, f"pid {pid} is not running", signals)
        if running:
            recorded = getattr(claim, "pid_start", None)
            token = probes.process_start_token(pid)
            signals["start"] = token
            if recorded and token and token != recorded:
                return Liveness(
                    DEAD, f"pid {pid} was reused: it started at {token}, the "
                          f"claim recorded {recorded}", signals)

    age = heartbeat_age(claim, now=now)
    signals["heartbeat_age"] = age
    if age is None:
        # Unreadable is not fresh and not ancient. It is exactly the "something
        # unusual, report it" case the fourth step exists for.
        return Liveness(STALE, "heartbeat cannot be read", signals)
    if age > stale_after:
        return Liveness(
            STALE, f"heartbeat is {int(age)}s old (limit {int(stale_after)}s) "
                   f"and nothing else could establish the owner is gone",
            signals)
    return Liveness(LIVE, "owner appears to be running", signals)


__all__ = [
    "BOOT_INSTANT_TOLERANCE",
    "DEAD",
    "DEFAULT_STALE_AFTER",
    "LIVE",
    "Liveness",
    "STALE",
    "SystemProbes",
    "assess",
    "boot_identity",
    "heartbeat_age",
    "process_present",
    "process_start_token",
    "same_boot",
]
