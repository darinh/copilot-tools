"""`operator.sh` must find its own directory on a shell without GNU readlink.

macOS ships BSD readlink, which has no `-f`. `operator.sh` line 32 used to be:

    SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

and the reason this needs a test rather than a grep is that it did not fail.
`set -euo pipefail` is set two lines above, which is what made it look safe --
but the failing `readlink` sits in a *nested* command substitution feeding
`dirname`, so the assignment's exit status is that of `cd ... && pwd`, which
succeeds. readlink writes to stderr and nothing to stdout, `dirname ""` is
`.`, `cd .` works, and SCRIPT_DIR silently becomes **the caller's working
directory**. Exit 0. No abort.

That is worse than the crash it was first diagnosed as, and the impact is
quieter than even that suggests. SCRIPT_DIR's only real uses are
`${SCRIPT_DIR}/operator-ingest.py` in `capture_and_store_metrics` and
`ingest_all_logs`. The first runs inside a subshell that opens with `set +e`
and whose last statement is a successful `log`, so the subshell exits 0 and
the ``|| log "  Warning: metrics capture failed (non-fatal)"`` arm never runs.
Python's error is captured by `2>&1` into `$result` and printed as an ordinary
``Metrics: python3: can't open file ...`` line. Nothing warning-shaped appears
anywhere. A macOS user gets a loop that looks healthy and stores no metrics,
which is the entire purpose of the wrapper.

So these run the real shipped function under a `readlink` that refuses `-f`
the way BSD's does, from a directory that is *not* the script's, and check
where it thinks it lives. The two possible answers are two different
directories rather than pass and fail: an exit code could not tell them
apart, because the broken version exits 0.

The observable is a marker file written into the resolved directory, not the
printed path. Under Git for Windows' msys bash the same directory has two
spellings (`/tmp/x` and `C:\\Users\\...\\Temp\\x`), and comparing the strings
fails there for a reason that has nothing to do with the defect -- which
reads exactly like the bug reproducing. A file either appears in a directory
or it does not, in any spelling.

`test_the_stub_really_refuses_dash_f` runs first, and it earned its place on
the first run: the stub was not on PATH at all, because msys bash ignores
PATH entries in Windows form. The real GNU readlink answered every probe.
Without that control the module would have been testing nothing, and saying
so in the shape of three passes.
"""
from __future__ import annotations

import subprocess

import pytest

from test_operator import OPERATOR_SH, _bash_executable, _shell_function, bash

# A readlink that behaves like BSD's: no `-f`, one hop, POSIX spelling only.
# The real binary is located by absolute path -- `exec readlink` would find
# this stub again and recurse.
BSD_READLINK = """#!/bin/sh
if [ "$1" = "-f" ]; then
    echo "readlink: illegal option -- f" >&2
    exit 1
fi
for real in /usr/bin/readlink /bin/readlink; do
    [ -x "$real" ] && exec "$real" "$@"
done
echo "no real readlink found" >&2
exit 127
"""

# Puts the stub on PATH in the shell's *own* spelling, then runs the probe
# from a chosen directory. Building the PATH entry inside bash is the point:
# a Windows-form directory prepended from Python is silently ignored by msys
# bash, which is how the first version of this module came to run every probe
# against the real GNU readlink and pass.
RUNNER = """#!/usr/bin/env bash
set -euo pipefail
export PATH="$(pwd)/fakebin:$PATH"
cd "$1"
exec bash "$2"
"""


def _harness(tmp_path):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    stub = fakebin / "readlink"
    stub.write_text(BSD_READLINK, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    runner = tmp_path / "runner.sh"
    runner.write_text(RUNNER, encoding="utf-8", newline="\n")
    return runner


def _run(tmp_path, run_from, script) -> subprocess.CompletedProcess:
    """Run `script` from `run_from`, with the BSD-like readlink on PATH."""
    runner = _harness(tmp_path)
    return subprocess.run(
        [_bash_executable(), runner.name, run_from.name, str(script)],
        cwd=tmp_path, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60)


def _probe_source() -> str:
    """A script that records where the shipped function says it lives.

    The function body is read out of `operator.sh` rather than copied here,
    so this cannot go on passing against a function the script no longer
    contains.
    """
    return (
        "set -euo pipefail\n"
        "resolve_script_dir() {\n"
        + _shell_function("resolve_script_dir")
        + "}\n"
        'SCRIPT_DIR="$(resolve_script_dir "${BASH_SOURCE[0]}")"\n'
        'echo "SCRIPT_DIR=$SCRIPT_DIR"\n'
        'printf resolved > "$SCRIPT_DIR/resolved.marker"\n'
    )


@bash
def test_the_stub_really_refuses_dash_f(tmp_path):
    """The control for every test below. It runs first for that reason."""
    run_from = tmp_path / "anywhere"
    run_from.mkdir()
    probe = tmp_path / "probe.sh"
    probe.write_text("readlink -f /etc/hostname\n", encoding="utf-8", newline="\n")

    result = _run(tmp_path, run_from, probe)

    assert result.returncode != 0, (
        "the BSD readlink stub is not on PATH, or accepted -f. Every "
        "assertion in this module would then pass against the GNU-only code "
        f"it exists to reject. stdout={result.stdout!r}")
    assert "illegal option" in result.stderr, result.stderr


@bash
def test_script_dir_is_the_scripts_own_directory_without_gnu_readlink(tmp_path):
    called_from = tmp_path / "elsewhere"
    lives = tmp_path / "checkout"
    called_from.mkdir()
    lives.mkdir()
    probe = lives / "probe.sh"
    probe.write_text(_probe_source(), encoding="utf-8", newline="\n")

    result = _run(tmp_path, called_from, probe)

    assert result.returncode == 0, (
        f"resolve_script_dir aborted without GNU readlink:\n{result.stderr}")
    assert (lives / "resolved.marker").is_file(), (
        "SCRIPT_DIR is not the directory the script lives in. "
        f"reported: {result.stdout.strip()!r}")
    assert not (called_from / "resolved.marker").exists(), (
        "SCRIPT_DIR fell back to the caller's working directory -- this is "
        "the original defect, exactly as it behaved on macOS")


@bash
def test_script_dir_follows_a_symlink_the_way_the_rollback_installs_one(tmp_path):
    """The documented rollback is a symlink, so resolving one is the case.

    `ln -sf /path/to/copilot-tools/operator.sh ~/.local/bin/operator` is what
    `docs/operator.md` tells a user to run. Without following the link,
    SCRIPT_DIR is `~/.local/bin`, where `operator-ingest.py` does not live --
    and BSD readlink resolves exactly one hop, which is what the loop is for.
    """
    lives = tmp_path / "checkout"
    bindir = tmp_path / "bin"
    called_from = tmp_path / "somewhere"
    for path in (lives, bindir, called_from):
        path.mkdir()
    real = lives / "probe.sh"
    real.write_text(_probe_source(), encoding="utf-8", newline="\n")
    link = bindir / "operator"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"cannot create a symlink here: {exc}")

    result = _run(tmp_path, called_from, link)

    assert result.returncode == 0, result.stderr
    assert (lives / "resolved.marker").is_file(), (
        "a symlinked launcher did not resolve back to the checkout, which is "
        f"the documented rollback layout. reported: {result.stdout.strip()!r}")
    assert not (bindir / "resolved.marker").exists(), (
        "SCRIPT_DIR stopped at the symlink's own directory; operator-ingest.py "
        "does not live there")


ORIGINAL_LINE = (
    'SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"'
)


@bash
def test_the_harness_catches_the_original_defect(tmp_path):
    """The positive control, and the reason to trust the three tests above.

    They assert an absence -- that SCRIPT_DIR is *not* the caller's directory
    -- and an absence is satisfied by a harness that does nothing at all. This
    runs the exact line that shipped, under the same stub, and requires the
    harness to catch it. It is kept as source text rather than pulled from
    git so the control still works on a checkout with no history.

    It also pins the *mechanism*, which was misdiagnosed once as an abort:
    the assertion is that the script exits **0** while resolving to the wrong
    directory. If some future bash makes this a hard failure instead, that is
    a real change in the story and this test should be the thing that says so.
    """
    called_from = tmp_path / "elsewhere"
    lives = tmp_path / "checkout"
    called_from.mkdir()
    lives.mkdir()
    probe = lives / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        + ORIGINAL_LINE + "\n"
        'echo "SCRIPT_DIR=$SCRIPT_DIR"\n'
        'printf resolved > "$SCRIPT_DIR/resolved.marker"\n',
        encoding="utf-8", newline="\n")

    result = _run(tmp_path, called_from, probe)

    assert result.returncode == 0, (
        "the original line aborted rather than silently misresolving. That is "
        "a different -- and better -- defect than the one this module "
        f"describes:\n{result.stderr}")
    assert (called_from / "resolved.marker").is_file(), (
        "the harness cannot reproduce the original defect, so the tests above "
        "prove nothing. Check that the BSD readlink stub is on PATH and that "
        f"the probe ran from {called_from}. stdout={result.stdout!r}")
    assert not (lives / "resolved.marker").exists()


def _gnu_readlink_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if "readlink -f" in line and not line.strip().startswith("#")]


def test_operator_sh_does_not_call_gnu_only_readlink_dash_f():
    """The static half. Cheap, and it runs on the CI legs that have no bash.

    Pinned to `readlink -f` specifically rather than to the shape of the fix,
    so rewriting `resolve_script_dir` some other correct way does not fail it.
    """
    offending = _gnu_readlink_lines(OPERATOR_SH.read_text(encoding="utf-8"))
    assert not offending, (
        "operator.sh calls GNU-only `readlink -f`, which macOS BSD readlink "
        "rejects -- silently, yielding the caller's CWD:\n  "
        + "\n  ".join(offending))


def test_the_static_check_fires_on_the_line_that_shipped():
    """...and leaves the comment that explains why it is gone alone.

    `operator.sh` now discusses `readlink -f` at length in a comment. A
    detector that flagged that would have to be weakened or deleted, which is
    how a guard ends up pinned to nothing.
    """
    assert _gnu_readlink_lines(ORIGINAL_LINE), (
        "the static check does not object to the exact line that shipped")
    assert not _gnu_readlink_lines(
        "# NOT `readlink -f`. That is GNU-only, and macOS ships BSD readlink."), (
        "the static check objects to a comment explaining the fix")

