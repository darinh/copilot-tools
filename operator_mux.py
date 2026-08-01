#!/usr/bin/env python3
"""Session-backend abstraction for the Copilot operator.

Owns every terminal-multiplexer invocation. Nothing else in the toolkit shells
out to tmux/psmux directly, which keeps the backend replaceable and keeps the
platform-divergent behavior auditable in one place.

Backends
--------
tmux    Linux, macOS, WSL
psmux   Windows (ships `tmux`, `pmux`, `psmux` as identical aliases)

Verified psmux divergences from tmux (psmux 3.3.7)
--------------------------------------------------
1. A session name containing ':' produces exit code 0 but creates NO session.
   A success-shaped failure is the most dangerous kind, so `new_session` always
   verifies with `has_session` afterwards and raises when the session is absent.
2. '.' is preserved rather than rewritten to '_' as tmux does. Sanitizing both
   characters keeps names identical across platforms.
3. `list-sessions` exits 0 with empty output when no server is running, whereas
   tmux exits 1. Emptiness is therefore detected from output, never exit status.

Server-lifecycle race (tmux, all platforms)
-------------------------------------------
The server exits once its last session is killed, and shutdown is not
instantaneous. A `new-session` issued inside that window connects to a socket
whose server is already leaving and fails with "server exited unexpectedly"
even though the request was perfectly valid. Restarting a loop immediately
after the previous one ended hits this in production, not only in tests, so
`new_session` retries the transient signatures rather than surfacing them.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import time

__all__ = [
    "MuxError",
    "MuxNotFoundError",
    "MuxSessionError",
    "Mux",
    "sanitize_name",
    "safe_instance_id",
]

# Characters that are illegal in Windows filenames or that a multiplexer
# rewrites. Instance names become filenames, so this applies on every platform
# to keep state directories portable.
_UNSAFE = r'[.:\\/*?"<>|\x00-\x1f]'

# Windows reserved device names. A file called CON or NUL cannot be created.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# On Windows, a process with no console of its own (e.g. `operator --loop`'s
# background supervisor) auto-allocates a brand-new *visible* console for any
# plain console-subsystem child it spawns. CREATE_NO_WINDOW suppresses that.
#
# Important: CREATE_NO_WINDOW does not merely hide a window, it gives the child
# a *fresh* invisible console and rebinds its std handles to it. That is
# harmless -- and correct -- for the captured control-plane calls below, which
# pass explicit pipes for stdout/stderr and so never touch the console. It is
# NOT safe for `attach`, which must inherit the user's real terminal to render
# the interactive session; applying it there would leave the user staring at a
# dead prompt. Hence this is applied only to the capturing branch of _run().
_POPEN_KWARGS: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if platform.system() == "Windows" else {}
)

# Errors that mean "the server was going away", not "the request was bad".
# Retrying these is safe because a fresh server is started by the next attempt;
# anything not listed here is reported immediately so real failures stay loud.
_TRANSIENT_SERVER_ERRORS = (
    "server exited unexpectedly",
    "lost server",
    "error connecting to",
    "no server running",
)

#: Total `new-session` attempts, and the base linear backoff between them. Three
#: attempts spanning ~0.75s comfortably outlast a server shutdown while keeping
#: a genuinely broken backend fast to report.
_NEW_SESSION_ATTEMPTS = 3
_NEW_SESSION_BACKOFF = 0.25


def _is_transient_server_error(err: str) -> bool:
    lowered = (err or "").casefold()
    return any(signature in lowered for signature in _TRANSIENT_SERVER_ERRORS)


class MuxError(Exception):
    """Base class for multiplexer failures."""


class MuxNotFoundError(MuxError):
    """No terminal multiplexer is installed."""


class MuxSessionError(MuxError):
    """A session operation failed, including silent-failure detection."""


def sanitize_name(name: str) -> str:
    """Replace characters that are unsafe in session names or filenames."""
    cleaned = re.sub(_UNSAFE, "-", name)
    cleaned = cleaned.strip().strip("-") or "instance"
    return cleaned


_DIGEST_SUFFIX = re.compile(r"-[0-9a-f]{6}$")

# Windows and macOS filesystems are case-insensitive by default, and psmux
# matches session names case-insensitively, so names differing only in case
# must not be treated as distinct instances.
_CASE_INSENSITIVE_FS = platform.system() in ("Windows", "Darwin")


def safe_instance_id(name: str) -> str:
    """Map a display name to a collision-free, filesystem-safe instance id.

    Sanitizing alone is not enough: 'a.b', 'a:b' and 'a-b' all sanitize to
    'a-b', so three distinct instances would share one set of state files and
    silently destroy each other. When sanitizing changes the name, or produces
    a Windows reserved device name, a short digest of the ORIGINAL name is
    appended to keep distinct inputs distinct.

    A name that already ends in something shaped like a digest is also
    suffixed, otherwise a literal name such as ``a-b-69f664`` would collide
    with the generated id for ``a.b``.

    On case-insensitive filesystems the digest is computed from the
    case-folded name, because ``Build`` and ``build`` would otherwise produce
    two ids that resolve to the same files and the same backend session.
    """
    cleaned = sanitize_name(name)
    reference = name
    if _CASE_INSENSITIVE_FS:
        # Fold the id: 'Build' and 'build' address the same file and the same
        # backend session, so they must be one instance rather than two that
        # silently share state. The display name keeps the original spelling.
        cleaned = cleaned.casefold()
        reference = name.casefold()
    stem = cleaned.split(".", 1)[0].upper()
    if cleaned != reference or stem in _RESERVED or _DIGEST_SUFFIX.search(cleaned):
        digest = hashlib.sha1(reference.encode("utf-8")).hexdigest()[:6]
        cleaned = f"{cleaned}-{digest}"
    return cleaned


def _install_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return (
            "No terminal multiplexer found. Install psmux:\n"
            "    winget install --id marlocarlo.psmux"
        )
    if system == "Darwin":
        return "No terminal multiplexer found. Install tmux:\n    brew install tmux"
    return (
        "No terminal multiplexer found. Install tmux with your package manager, "
        "e.g.:\n    sudo apt install tmux"
    )


class Mux:
    """Thin wrapper over the tmux verb surface."""

    #: Probe order. psmux registers a `tmux` alias on Windows, so probing
    #: `tmux` first is correct on every platform and needs no OS branch.
    CANDIDATES = ("tmux", "psmux", "pmux")

    def __init__(self, binary: str | None = None):
        self._binary = binary or os.environ.get("COPILOT_OPERATOR_MUX") or None

    # ── discovery ────────────────────────────────────────────────
    @property
    def binary(self) -> str:
        if self._binary is None:
            for candidate in self.CANDIDATES:
                if shutil.which(candidate):
                    self._binary = candidate
                    break
            else:
                raise MuxNotFoundError(_install_hint())
        return self._binary

    def available(self) -> bool:
        try:
            return bool(self.binary)
        except MuxNotFoundError:
            return False

    def version(self) -> str:
        out, _, _ = self._run("-V")
        return out.strip()

    # ── plumbing ─────────────────────────────────────────────────
    def _run(self, *args: str, capture: bool = True) -> tuple[str, str, int]:
        cmd = [self.binary, *args]
        if not capture:
            # No _POPEN_KWARGS here: this is the interactive `attach` path and
            # it must inherit the caller's real console. See _POPEN_KWARGS.
            return "", "", subprocess.run(cmd).returncode
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_POPEN_KWARGS,
        )
        return (proc.stdout or "").strip(), (proc.stderr or "").strip(), proc.returncode

    def _display(self, session: str, fmt: str) -> str | None:
        out, _, rc = self._run("display-message", "-t", session, "-p", fmt)
        if rc != 0 or not out:
            return None
        return out

    # ── session verbs ────────────────────────────────────────────
    def has_session(self, session: str) -> bool:
        _, _, rc = self._run("has-session", "-t", session)
        return rc == 0

    def list_sessions(self) -> list[str]:
        # psmux exits 0 with empty output when no server is running while tmux
        # exits 1, so emptiness is read from output rather than exit status.
        out, _, _ = self._run("list-sessions", "-F", "#{session_name}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def new_session(self, session: str, cwd: str, argv: list[str]) -> None:
        """Create a detached session running argv.

        argv is passed after `--` so the backend does not re-parse quoting;
        this preserves arguments containing spaces and quotes exactly.

        A transient server-shutdown failure is retried (see the module
        docstring); every other failure is raised on the first attempt so a
        genuinely bad request is never hidden behind a delay.
        """
        if self.has_session(session):
            raise MuxSessionError(f"Session already exists: {session}")
        err: str = ""
        rc: int = 0
        for attempt in range(_NEW_SESSION_ATTEMPTS):
            _, err, rc = self._run(
                "new-session", "-d", "-s", session, "-c", str(cwd), "--", *argv
            )
            if rc == 0 or not _is_transient_server_error(err):
                break
            # The dying server may still have created the session before the
            # client lost it. Adopt that session instead of retrying, which
            # would otherwise fail as a duplicate name.
            if self.has_session(session):
                rc = 0
                break
            if attempt + 1 < _NEW_SESSION_ATTEMPTS:
                time.sleep(_NEW_SESSION_BACKOFF * (attempt + 1))
        if rc != 0:
            raise MuxSessionError(f"Failed to create session {session!r}: {err or rc}")
        # psmux can report success while creating nothing (notably for names
        # containing ':'). Verify rather than trust the exit code.
        if not self.has_session(session):
            raise MuxSessionError(
                f"Backend reported success but session {session!r} does not exist. "
                "This is the known psmux silent-failure mode for unsafe session names."
            )

    def kill_session(self, session: str) -> bool:
        """Destroy a session. Returns False when it was already absent.

        Raises when the backend reports failure or the session survives, so a
        caller never deletes its state believing a still-running session is
        gone.
        """
        if not self.has_session(session):
            return False
        _, err, rc = self._run("kill-session", "-t", session)
        if rc != 0 and self.has_session(session):
            raise MuxSessionError(
                f"Failed to kill session {session!r}: {err or f'exit {rc}'}"
            )
        if self.has_session(session):
            raise MuxSessionError(
                f"Session {session!r} still exists after kill-session reported success."
            )
        return True

    def set_remain_on_exit(self, session: str, enabled: bool) -> None:
        self._run(
            "set-option", "-t", session, "remain-on-exit", "on" if enabled else "off"
        )

    def send_keys(self, session: str, text: str, enter: bool = True,
                  literal: bool = True) -> None:
        """Type ``text`` into a session, optionally followed by Enter.

        Literal by default, and callers should keep it that way for any text
        they did not write themselves. Without ``-l`` the backend looks up
        every whitespace-separated token in the string as a *key name*, so
        arbitrary text is an input-injection vector rather than data:
        verified on psmux 3.3.7, the word ``Enter`` inside a message submits
        the line early and truncates the rest, and ``C-c`` would deliver
        Ctrl-C to the program in the pane.

        ``-l`` does not accept a trailing key name, so Enter is a second call.
        """
        if literal:
            self._run("send-keys", "-t", session, "-l", text)
            if enter:
                self._run("send-keys", "-t", session, "Enter")
            return
        args = ["send-keys", "-t", session, text]
        if enter:
            args.append("Enter")
        self._run(*args)

    def pane_pid(self, session: str) -> int | None:
        """PID of the pane's direct child.

        Deliberately NOT used to identify the Copilot process. On POSIX the run
        script `exec`s Copilot so the two coincide, but on Windows this is the
        multiplexer's own shell, two levels above Copilot. Copilot's real PID
        comes from the runner's pid file instead.
        """
        value = self._display(session, "#{pane_pid}")
        try:
            return int(value) if value else None
        except ValueError:
            return None

    def pane_dead(self, session: str) -> bool:
        return self._display(session, "#{pane_dead}") == "1"

    def pane_current_path(self, session: str) -> str | None:
        return self._display(session, "#{pane_current_path}")

    def attach(self, session: str) -> int:
        """Attach the current terminal. Returns when the user detaches."""
        _, _, rc = self._run("attach", "-t", session, capture=False)
        return rc
