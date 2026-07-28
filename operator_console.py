#!/usr/bin/env python3
"""Console output helpers.

Windows consoles default to a legacy code page (commonly cp1252), so printing
the box-drawing characters and em-dashes used throughout the operator's output
produces mojibake, and on some configurations raises ``UnicodeEncodeError``
outright. Reconfiguring the standard streams to UTF-8 fixes rendering without
requiring the user to change their console settings.

POSIX terminals are already UTF-8 in practice, so this is a no-op there.
"""
from __future__ import annotations

import sys

__all__ = ["enable_utf8_output"]

_configured = False


def enable_utf8_output() -> None:
    """Force stdout/stderr to UTF-8. Safe to call more than once."""
    global _configured
    if _configured:
        return
    _configured = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A redirected or already-wrapped stream may refuse; rendering is
            # cosmetic, so never let this break the command.
            pass
