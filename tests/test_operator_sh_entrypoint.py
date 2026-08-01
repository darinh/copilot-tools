"""`operator.sh` has to survive its own first four lines, not just its functions.

Everything the bash 3.2 effort built tests function *bodies*: `_shell_function`
lifts a `name() {` ... `}` block out of the script and runs it from a generated
probe. That is the right tool for the set logic it was built for, and it is
structurally blind to the top level -- the script could abort, or quietly
compute the wrong value, on the line before the first function is defined and
every one of those tests would still pass. The static conformance scan is blind
in the same direction for a different reason: it proves the functions contain no
bash 4 construct, inside a file that may never reach them.

`operator.sh:32` sat in that blind spot and was wrong the whole time:

    SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

`readlink -f` is GNU-only. macOS ships BSD readlink, which has no `-f` -- and
`setup.sh` has carried a comment saying exactly that since it was written, so
the knowledge was in the repository the entire time, one file away.

The interesting part is the failure mode, because it dictates what these tests
may assert. It is *not* an abort, despite `set -euo pipefail` two lines above.
The failing `readlink` is nested inside `$(dirname "$(...)")`, so `set -e` never
sees it: the assignment takes the status of the OUTER `$(cd ... && pwd)`, which
succeeds. `dirname ""` is `.`, `cd .` works, and `SCRIPT_DIR` silently became
**the caller's working directory**. The script starts, runs, and exits 0.

So a test that asks "does operator.sh start on macOS?" passes against the bug.
The assertion has to be on the *value* of SCRIPT_DIR, with the script invoked
from a directory that is not the one it lives in -- otherwise cwd and the
correct answer coincide and the bug is invisible. That is why every case below
runs from `elsewhere/`.

Observed rather than inferred: the value is read out of `bash -x` trace output
of a real, whole-file run, not from a re-implementation of the line.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from test_operator import (OPERATOR_SH, _bash_executable, _shell_function,
                           bash)

# BSD readlink, reduced to the one thing that matters here: it does not have
# `-f` and says so on stderr. Everything else is delegated to the real one, so
# the plain `readlink` the fix relies on keeps working and the shim cannot pass
# these tests by crippling the resolution wholesale.
BSD_READLINK = """#!/bin/bash
if [ "$1" = "-f" ]; then
    echo "readlink: illegal option -- f" >&2
    echo "usage: readlink [-n] [file ...]" >&2
    exit 1
fi
exec /usr/bin/readlink "$@"
"""

# `operator list` is the shortest path through the real entrypoint that exits 0
# without launching anything: main() -> migrate_legacy_state -> list_instances.
# It needs tmux to exist; it does not need tmux to do anything.
TMUX_STUB = "#!/bin/bash\nexit 0\n"

# The driver computes its own location with `pwd` inside bash rather than being
# handed a path from Python. On Windows that matters twice: `pwd` returns a
# POSIX path, so `$here/bin` is a legal PATH entry, whereas a native
# `C:/...` path contains the colon that PATH uses as its separator and silently
# fails to take effect -- which looks exactly like a shim that was never
# consulted, i.e. like the bug being absent.
DRIVER = """#!/bin/bash
here="$(cd "$(dirname "$0")" && pwd)"
echo "HERE=$here"
export PATH="$here/bin:$PATH"
export HOME="$here/home"
export COPILOT_OPERATOR_HOME="$here/home/.operator"
cd "$here/elsewhere" || exit 9
bash -x "$here/$1" list >"$here/stdout.txt" 2>"$here/trace.txt"
echo "EXIT=$?"
"""


def _checkout(tmp_path, *, bsd_readlink: bool):
    """A synthetic checkout holding the real `operator.sh`, plus shims."""
    for sub in ("checkout", "bin", "elsewhere", "home", "linkdir"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)

    target = tmp_path / "checkout" / "operator.sh"
    # The shipped file, never a copy of its text -- these tests must not be
    # able to pass against an operator.sh the repository no longer contains.
    shutil.copyfile(OPERATOR_SH, target)
    target.chmod(0o755)

    if bsd_readlink:
        shim = tmp_path / "bin" / "readlink"
        shim.write_text(BSD_READLINK, encoding="utf-8", newline="\n")
        shim.chmod(0o755)

    tmux = tmp_path / "bin" / "tmux"
    tmux.write_text(TMUX_STUB, encoding="utf-8", newline="\n")
    tmux.chmod(0o755)

    driver = tmp_path / "driver.sh"
    driver.write_text(DRIVER, encoding="utf-8", newline="\n")
    driver.chmod(0o755)
    return target


def _run_entrypoint(tmp_path, relative_target: str):
    """Run the whole script and report what its top level actually computed."""
    proc = subprocess.run([_bash_executable(), str(tmp_path / "driver.sh"),
                           relative_target],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    trace = (tmp_path / "trace.txt").read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^\+ SCRIPT_DIR=(.*)$", trace, re.MULTILINE)
    return proc, trace, (match.group(1).strip() if match else None)


def _posix_dir(tmp_path, name: str, proc, trace) -> str:
    """`name` under tmp_path, as the running bash spells it.

    Comparing against a Python-built path would compare `C:\\Users\\...` with
    `/c/Users/...` on Windows and fail for a reason that has nothing to do with
    the defect. The driver prints its own resolved location; everything is
    measured against that. It comes from the driver's stdout rather than the
    xtrace, because only the script under test runs under `-x`.
    """
    match = re.search(r"^HERE=(.*)$", proc.stdout, re.MULTILINE)
    assert match, (
        "the driver's own location was not in its output, so nothing below can "
        f"be compared against anything:\n{proc.stdout}\n{proc.stderr}")
    return f"{match.group(1).strip()}/{name}"


@bash
def test_entrypoint_resolves_its_own_directory_without_gnu_readlink(tmp_path):
    """The regression test proper: BSD readlink, run from another directory.

    Against the unfixed script this reports the caller's cwd -- verified, not
    assumed: the same harness on `readlink -f` produced
    `SCRIPT_DIR=<tmp>/elsewhere` where the answer is `<tmp>/checkout`, with
    exit 0 and one line of readlink usage text as the only other symptom.
    """
    _checkout(tmp_path, bsd_readlink=True)
    proc, trace, script_dir = _run_entrypoint(tmp_path, "checkout/operator.sh")

    assert script_dir is not None, f"no SCRIPT_DIR assignment in the trace:\n{trace[:2000]}"
    expected = _posix_dir(tmp_path, "checkout", proc, trace)
    cwd = _posix_dir(tmp_path, "elsewhere", proc, trace)

    # Naming the wrong answer separately from the right one: if these two are
    # ever equal the test has stopped being able to see the defect, and that
    # must fail here rather than pass quietly.
    assert expected != cwd
    assert script_dir != cwd, (
        "SCRIPT_DIR is the directory the user was standing in, not the one the "
        "script lives in -- this is the GNU-only `readlink -f` defect, and it "
        "does not announce itself: the script still exits 0")
    assert script_dir == expected, f"SCRIPT_DIR={script_dir!r}, expected {expected!r}"

    assert "illegal option" not in trace, (
        "something still calls `readlink -f`, which BSD readlink refuses:\n"
        + "\n".join(l for l in trace.splitlines() if "illegal option" in l))
    assert "EXIT=0" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


@bash
def test_entrypoint_agrees_with_itself_under_a_real_readlink(tmp_path):
    """The control for the harness, not for the script.

    Without this, a harness broken into always reporting the checkout
    directory -- or one whose shim never made it onto PATH -- passes the test
    above. Running the identical scenario with the platform's real readlink
    must produce the identical answer; the fix is supposed to be indifferent
    to which readlink is present.
    """
    _checkout(tmp_path, bsd_readlink=False)
    proc, trace, script_dir = _run_entrypoint(tmp_path, "checkout/operator.sh")

    assert script_dir == _posix_dir(tmp_path, "checkout", proc, trace)
    assert "EXIT=0" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


@bash
def test_entrypoint_still_follows_a_symlink_to_the_real_checkout(tmp_path):
    """`readlink -f` was there for a reason; dropping it must not cost that.

    The original line resolved symlinks, which is what makes `operator.sh`
    work when it is linked onto a PATH directory from a checkout elsewhere --
    and SCRIPT_DIR is used to find `operator-ingest.py` beside the real file,
    so resolving to the link's directory would break it just as thoroughly as
    resolving to the cwd. Both hops are exercised: an absolute link, and a
    relative link pointing at that link.
    """
    _checkout(tmp_path, bsd_readlink=True)
    link = tmp_path / "linkdir" / "operator.sh"
    chained = tmp_path / "linkdir" / "rel-link.sh"
    try:
        link.symlink_to(tmp_path / "checkout" / "operator.sh")
        # A relative target, which is the branch that has to rejoin the link's
        # own directory rather than the cwd.
        chained.symlink_to("operator.sh")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks on this machine: {exc}")

    for relative in ("linkdir/operator.sh", "linkdir/rel-link.sh"):
        proc, trace, script_dir = _run_entrypoint(tmp_path, relative)
        expected = _posix_dir(tmp_path, "checkout", proc, trace)
        linkdir = _posix_dir(tmp_path, "linkdir", proc, trace)
        assert script_dir != linkdir, (
            f"{relative} resolved to the symlink's own directory; "
            "operator-ingest.py does not live there")
        assert script_dir == expected, f"{relative}: {script_dir!r} != {expected!r}"


GNU_READLINK = re.compile(r"\breadlink\s+(-\w*\s+)*-\w*f")


def _gnu_readlink_lines(text: str) -> list[str]:
    """Lines that really call `readlink -f`, with two exclusions.

    Comments are stripped, or this scan reports every file that *discusses*
    the problem -- including the comment in `operator.sh` explaining the fix
    and the one in `setup.sh` that documented the hazard years before anyone
    acted on it. A detector that fires on its own documentation gets deleted.

    Lines naming `/proc/` are exempt. `diagnose-restart-deleter.sh` resolves
    `/proc/$pid/exe`, which is a Linux kernel interface that does not exist on
    macOS in any form: the whole line is unreachable there, so GNU readlink is
    not an assumption it is making. Narrow and stated beats a file-level
    exemption, which would also excuse a future line in that script that had
    nothing to do with /proc.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not GNU_READLINK.search(stripped):
            continue
        if "/proc/" in stripped:
            continue
        out.append(stripped)
    return out


def test_no_first_party_script_relies_on_gnu_readlink():
    """Scanned across every shipped script, deliberately not just operator.sh.

    `readlink -f` is a silent no-op-shaped failure on macOS rather than a loud
    one, so it is exactly the kind of thing that gets reintroduced by a
    copy-paste and noticed by nobody. This repository has already learned the
    narrower version of the lesson the expensive way: the tripwire for
    associative arrays read only `operator.sh`, which is how `handoff.sh` kept
    one through the change that removed operator.sh's. A rule enforced against
    one file is not a rule, it is that file's history.
    """
    pytest.importorskip("test_shell_bash32_conformance")
    from test_shell_bash32_conformance import _first_party_scripts

    scripts = _first_party_scripts()
    # The population guard. An empty or mis-rooted list satisfies every
    # "no script contains X" assertion ever written against it.
    assert len(scripts) >= 2, f"suspiciously small scan population: {scripts}"
    assert OPERATOR_SH.name in [p.name for p in scripts]

    offenders = {}
    for path in scripts:
        hits = _gnu_readlink_lines(path.read_text(encoding="utf-8"))
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        "`readlink -f` is GNU-only and macOS BSD readlink refuses it, without "
        f"aborting the script that called it: {offenders}")


def test_the_readlink_detector_can_actually_fire():
    """The negative control for the scan above.

    A regex that matches nothing reports the whole tree clean, which reads
    exactly like success -- the failure mode this repository writes a control
    for every time. Every exclusion in `_gnu_readlink_lines` widens the set of
    things it stays quiet about, so each one is pinned here alongside a case
    proving it did not swallow the rule whole.
    """
    # Fires: plain, bundled, and separated flags.
    assert _gnu_readlink_lines('SCRIPT_DIR="$(readlink -f "$0")"')
    assert _gnu_readlink_lines("readlink -e -f x")
    assert _gnu_readlink_lines("readlink -nf x")

    # Quiet: the spelling the fix uses, which the scan must tolerate forever.
    assert not _gnu_readlink_lines('src="$(readlink "$src")"')
    assert not _gnu_readlink_lines("readlink -n x")

    # Quiet: comments, including the exact ones in this repo that made the
    # first version of this test fail against a tree with no defect in it.
    assert not _gnu_readlink_lines("# `readlink -f` is GNU-only.")
    assert not _gnu_readlink_lines(
        "  # (avoids relying on GNU-only `readlink -f`, which BSD lacks).")

    # Quiet: the /proc exemption -- and still loud for the same script on a
    # line that is not about /proc, which is what keeps the exemption narrow.
    assert not _gnu_readlink_lines('exe=$(readlink -f "/proc/$pid/exe")')
    assert _gnu_readlink_lines('exe=$(readlink -f "$HOME/thing")')


@bash
def test_resolve_script_dir_refuses_a_symlink_loop(tmp_path):
    """The hop limit, called directly, because the entrypoint cannot reach it.

    `readlink -f` returned ELOOP here; a hand-rolled `while [ -L ]` spins
    forever unless it is bounded. Failing loudly is the deliberate opposite of
    the defect being fixed -- the complaint about the old line is precisely
    that an unresolvable path produced a plausible wrong answer rather than a
    complaint.

    It is exercised through `_shell_function` rather than by running the
    script, and that is not laziness: see the test below. bash resolves a
    script's own path before it executes a byte of it, so `BASH_SOURCE[0]` can
    never be a loop by the time this function runs. The guard is reachable
    only by calling it, so calling it is the only honest way to prove it
    works.
    """
    a = tmp_path / "a.sh"
    b = tmp_path / "b.sh"
    try:
        a.symlink_to(b)
        b.symlink_to(a)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks on this machine: {exc}")

    probe = tmp_path / "probe.sh"
    probe.write_text(
        "set -euo pipefail\n"
        f"resolve_script_dir() {{\n{_shell_function('resolve_script_dir')}}}\n"
        'if out="$(resolve_script_dir "$1")"; then\n'
        '    echo "RESOLVED=$out"\n'
        "else\n"
        '    echo "REFUSED"\n'
        "fi\n",
        encoding="utf-8", newline="\n")

    proc = subprocess.run([_bash_executable(), "probe.sh", str(a)],
                          cwd=tmp_path, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    assert "REFUSED" in proc.stdout, (
        f"a symlink loop resolved to something:\n{proc.stdout}\n{proc.stderr}")
    assert "too many symbolic links" in proc.stderr, proc.stderr

    # The control: the same function, the same call shape, on a path that is
    # resolvable. Without it "REFUSED" is also what a resolve_script_dir
    # broken into always failing would print.
    real = tmp_path / "real.sh"
    real.write_text("", encoding="utf-8")
    ok = subprocess.run([_bash_executable(), "probe.sh", str(real)],
                        cwd=tmp_path, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
    assert "RESOLVED=" in ok.stdout, f"{ok.stdout}\n{ok.stderr}"


@bash
def test_entrypoint_with_a_looping_path_never_reaches_the_script(tmp_path):
    """Why the test above calls the function instead of running the script.

    Recorded as an assertion rather than a comment because it is the reason
    the hop limit looks untested, and a future reader who assumes otherwise
    would "fix" that by writing a test that can only ever pass vacuously.
    bash refuses to open the file itself, so the failure is loud and arrives
    before line one.
    """
    _checkout(tmp_path, bsd_readlink=True)
    a = tmp_path / "linkdir" / "a.sh"
    b = tmp_path / "linkdir" / "b.sh"
    try:
        a.symlink_to(b)
        b.symlink_to(a)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create symlinks on this machine: {exc}")

    proc, trace, _ = _run_entrypoint(tmp_path, "linkdir/a.sh")
    assert "EXIT=0" not in proc.stdout, (
        "a symlink loop resolved to something and the script carried on:\n"
        f"{proc.stdout}\n{trace[-2000:]}")
    assert "symbolic links" in trace.lower(), trace[-2000:]
    # Nothing of the script ran: no assignment, no trace of its first line.
    assert "SCRIPT_DIR=" not in trace, trace[-2000:]
