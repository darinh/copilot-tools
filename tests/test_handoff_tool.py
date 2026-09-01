"""Module-level invariants of ``handoff_tool`` itself.

The behavioural tests for the checkout guard live in
``test_handoff_checkout_guard.py``; what belongs here is the handful of
constants the rest of that behaviour is measured against, which have to be
right on every platform rather than on the one that happens to define them.
"""
from __future__ import annotations

import stat

import handoff_tool as ho


def test_the_mount_point_tag_is_the_windows_abi_value():
    """The constant `_is_junction` rests on, pinned independently of ``stat``.

    The comparison used to spell its POSIX fallback ``object()``, so on every
    non-Windows leg the tag compared unequal to everything: not a junction
    test but a platform test, and it kept
    ``test_only_the_mount_point_tag_counts_as_a_junction`` red on every Linux
    and macOS run for a month while passing on Windows.

    The fallback is the real tag now, which means it has to be the *right*
    tag. On Windows the production value is read from ``stat``, so ``stat``
    cannot testify about it there -- a mistyped literal would agree with
    itself. This pins the literal from the Windows ABI on every platform, and
    checks the agreement only where there is something to agree with.
    """
    assert ho.IO_REPARSE_TAG_MOUNT_POINT == 0xA0000003
    if hasattr(stat, "IO_REPARSE_TAG_MOUNT_POINT"):
        assert ho.IO_REPARSE_TAG_MOUNT_POINT == stat.IO_REPARSE_TAG_MOUNT_POINT
