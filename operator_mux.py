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
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess

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
    """
    cleaned = sanitize_name(name)
    stem = cleaned.split(".", 1)[0].upper()
    if cleaned != name or stem in _RESERVED or _DIGEST_SUFFIX.search(cleaned):
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
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
            return "", "", subprocess.run(cmd).returncode
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
        """
        if self.has_session(session):
            raise MuxSessionError(f"Session already exists: {session}")
        _, err, rc = self._run(
            "new-session", "-d", "-s", session, "-c", str(cwd), "--", *argv
        )
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
        if not self.has_session(session):
            return False
        self._run("kill-session", "-t", session)
        return True

    def set_remain_on_exit(self, session: str, enabled: bool) -> None:
        self._run(
            "set-option", "-t", session, "remain-on-exit", "on" if enabled else "off"
        )

    def send_keys(self, session: str, text: str, enter: bool = True) -> None:
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
