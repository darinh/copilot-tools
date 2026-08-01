"""Every shell script must check out with LF endings, on every platform.

``core.autocrlf=true`` is the Windows default and this repository had no
``.gitattributes``, so all eleven tracked ``*.sh`` files were stored LF in the
index and written **CRLF** into a Windows working tree. Real bash then refuses
the file at its second line::

    t.sh: line 2: set: pipefail
    : invalid option name

-- measured on GNU bash 5.2.21, with the exact bytes a Windows checkout
produces. Not one assertion in the file runs. The shell suites in ``tests/``
were therefore unrunnable by anyone working on Windows, which is the mechanism
behind ``tests/test-todo-claims.sh`` going its entire life unexecuted: the
people who could have noticed could not run it, and the job that could have
run it only parsed it.

The failure is worse than an error message, because it is not always one.
``msys`` bash -- what Git for Windows ships -- tolerates the trailing CR and
runs the script fine, so the same file is broken on the platform CI uses and
healthy on the shell the Windows developer reaches for first. And depending on
how the run is invoked, the shell's exit status is easy to lose: a previous
session recorded a CRLF script reporting ``EXIT=0`` while its body never ran,
which reads exactly like a clean pass.

So this file checks the property, not the paperwork:

* ``git check-attr`` proves the rule is *declared* for every shell script;
* ``git cat-file`` proves the *index* is clean, which the attribute does not
  fix retroactively -- ``eol=lf`` governs checkout, and a blob committed with
  CRLF before the rule existed would keep its CRLF through it;
* ``git checkout-index`` into a temp dir proves what a **fresh checkout
  actually writes** on the machine running the test. That is the claim users
  care about, and it is the only one of the three that could catch the rule
  being correct and ineffective.

The population is discovered by shebang as well as by suffix. A rule enforced
against a list of filenames is that list's history: the day someone adds
``bin/doctor`` with a ``#!/usr/bin/env bash`` line and no suffix, it must fail
here rather than quietly become the next file nobody on Windows can run.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Suffixes that are a shell script regardless of content.
SHELL_SUFFIXES = frozenset({".sh", ".bash", ".ksh", ".zsh"})

#: Interpreters whose scripts break on a trailing CR.
_SHEBANG = re.compile(rb"^#!.*?\b(?:ba|da|k|z|a)?sh\b")

#: Tracked, textual, and deliberately *outside* the rule. It anchors the
#: control that shows an LF checkout is caused by the attribute rather than by
#: a platform that was never going to write CRLF in the first place.
CONTROL_PATH = "setup.ps1"

#: Scripts whose absence from the population would mean the discovery broke
#: rather than that the repository changed. Chosen to span the three places
#: shell scripts live here: the root, ``tests/``, and vendored Spec Kit.
MUST_BE_FOUND = (
    "setup.sh",
    "operator.sh",
    "handoff.sh",
    "tests/test-todo-claims.sh",
    ".specify/scripts/bash/common.sh",
)


def _git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    """Run git at the repository root, or fail this check loudly."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout; there is no index to inspect")
    if shutil.which("git") is None:
        pytest.fail("git is required to verify line-ending attributes")
    kwargs = {"encoding": "utf-8", "errors": "replace"} if text else {}
    return subprocess.run(
        ["git", *args], cwd=str(REPO), check=True, capture_output=True, **kwargs
    )


def _tracked() -> list[str]:
    """Every tracked path, repo-relative and slash-separated, as git reports it.

    ``git ls-files`` rather than a filesystem walk: every agent on this project
    works in ``<repo>/.worktrees/<branch>/``, and a walk from the repository
    root would descend into a peer's checkout and judge their files.
    """
    out = _git("ls-files", "-z").stdout
    return sorted(p for p in out.split("\0") if p)


def _index_blobs(paths: list[str]) -> dict[str, bytes]:
    """Raw index contents for `paths`, unconverted, in one git invocation.

    ``cat-file --batch`` applies no smudge filter and no EOL conversion, so
    what comes back is the bytes that are committed -- which is the thing the
    CR assertion needs, and precisely what reading the working-tree file would
    not tell you on a platform that converts.
    """
    if not paths:
        return {}
    stdin = "".join(f":{p}\n" for p in paths).encode("utf-8")
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=str(REPO), input=stdin, check=True, capture_output=True,
    )
    blobs: dict[str, bytes] = {}
    rest = proc.stdout
    for path in paths:
        header, _, rest = rest.partition(b"\n")
        fields = header.split(b" ")
        assert len(fields) == 3 and fields[1] == b"blob", (
            f"unexpected `git cat-file --batch` header for {path!r}: {header!r}"
        )
        size = int(fields[2])
        blobs[path], rest = rest[:size], rest[size + 1:]
    assert not rest, f"unconsumed `git cat-file --batch` output: {rest[:80]!r}"
    return blobs


def _looks_like_shell_script(path: str, blob: bytes) -> bool:
    """True if `path` is a shell script, by suffix or by shebang.

    The shebang branch is what keeps this from being a list of filenames. It
    matches the interpreter word, so ``#!/usr/bin/env bash`` and ``#!/bin/sh``
    both count while ``#!/usr/bin/env python3`` does not -- and neither does a
    ``#!`` appearing anywhere below the first line, which is a comment.
    """
    if Path(path).suffix.lower() in SHELL_SUFFIXES:
        return True
    first_line = blob.split(b"\n", 1)[0]
    return _SHEBANG.match(first_line) is not None


def _shell_scripts() -> list[str]:
    tracked = _tracked()
    # Only files that could carry a shebang need their bytes read; a suffix
    # match settles the question without them.
    by_suffix = [p for p in tracked if Path(p).suffix.lower() in SHELL_SUFFIXES]
    maybe = [p for p in tracked if p not in set(by_suffix)]
    blobs = _index_blobs(maybe)
    by_shebang = [p for p in maybe if _looks_like_shell_script(p, blobs[p])]
    return sorted(set(by_suffix) | set(by_shebang))


def _check_attr(attr: str, paths: list[str]) -> dict[str, str]:
    """``git check-attr`` for `paths`, parsed, with the parse itself asserted."""
    out = _git("check-attr", attr, "--", *paths).stdout
    parsed: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            continue
        path, _, tail = line.rpartition(f": {attr}: ")
        assert path, f"could not parse `git check-attr {attr}` output: {line!r}"
        parsed[path] = tail
    assert set(parsed) == set(paths), (
        "`git check-attr` did not answer for every path asked about; "
        f"missing {sorted(set(paths) - set(parsed))}"
    )
    return parsed


def _checkout_index(dest: Path, paths: list[str]) -> dict[str, bytes]:
    """Write `paths` into `dest` exactly as a fresh checkout would.

    ``checkout-index`` runs the same conversion pipeline as ``git checkout``,
    so this answers "what would land in a working tree here" rather than "what
    does the config say should land there".
    """
    prefix = dest.as_posix().rstrip("/") + "/"
    _git("checkout-index", "-f", f"--prefix={prefix}", "--", *paths)
    return {p: (dest / p).read_bytes() for p in paths}


SHELL_SCRIPTS = _shell_scripts() if (REPO / ".git").exists() else []


def test_the_population_is_not_empty_and_holds_the_scripts_we_ship():
    """A discovery that finds nothing passes every rule in this file.

    That is not hypothetical here: ``test_shell_bash32_conformance.py`` shipped
    with a path filter that matched ``.worktrees`` for every script in the tree
    and came back empty, and only its own population control caught it.
    """
    assert SHELL_SCRIPTS, "no shell scripts found; the discovery is broken"
    missing = [p for p in MUST_BE_FOUND if p not in SHELL_SCRIPTS]
    assert not missing, f"shell script discovery lost known scripts: {missing}"


def test_gitattributes_is_tracked():
    """The rule has to be committed to apply to anybody else's checkout."""
    assert ".gitattributes" in _tracked(), (
        ".gitattributes is not tracked; an untracked one governs only this "
        "working tree, which is the one place the problem was already fixed"
    )


@pytest.mark.parametrize("path", SHELL_SCRIPTS)
def test_shell_script_is_declared_lf(path: str):
    """Every shell script resolves ``eol=lf`` through the attributes file."""
    assert _check_attr("eol", [path])[path] == "lf", (
        f"{path} has no eol=lf attribute, so it checks out CRLF wherever "
        "core.autocrlf is set and cannot be run by real bash there"
    )


@pytest.mark.parametrize("path", SHELL_SCRIPTS)
def test_shell_script_index_blob_has_no_cr(path: str):
    """`eol=lf` governs checkout only; a CRLF blob would survive it."""
    blob = _index_blobs([path])[path]
    assert b"\r" not in blob, (
        f"{path} carries carriage returns in the index. eol=lf will not "
        "strip them on checkout -- re-add the file with `git add "
        "--renormalize` so the committed blob is LF"
    )


@pytest.mark.parametrize("path", SHELL_SCRIPTS)
def test_shell_script_checks_out_lf(path: str, tmp_path: Path):
    """What a fresh checkout writes here, measured rather than inferred."""
    written = _checkout_index(tmp_path, [path])[path]
    assert b"\r" not in written, (
        f"a fresh checkout of {path} on this platform contains CR; bash will "
        "abort at `set -euo pipefail` with 'invalid option name'"
    )


def test_the_lf_checkout_is_caused_by_the_attribute(tmp_path: Path):
    """Control: on a converting checkout, an uncovered file still gets CRLF.

    Without this, every assertion above is satisfied by a platform that was
    never going to write a CR anyway -- which is every Linux CI leg, i.e. seven
    of the eight. This is the one test that can tell "the rule works" apart
    from "the rule was never load-bearing here".
    """
    assert CONTROL_PATH in _tracked(), (
        f"the control file {CONTROL_PATH} is no longer tracked; pick another "
        "tracked text file outside the shell-script patterns"
    )
    assert CONTROL_PATH not in SHELL_SCRIPTS, (
        f"{CONTROL_PATH} is now classed as a shell script and can no longer "
        "serve as the outside-the-rule control"
    )
    written = _checkout_index(tmp_path, [CONTROL_PATH])[CONTROL_PATH]
    if b"\r\n" not in written:
        pytest.skip(
            "this checkout does not convert line endings (core.autocrlf is "
            "off), so an LF result proves nothing about the attribute"
        )
    sample = _checkout_index(tmp_path, [MUST_BE_FOUND[0]])[MUST_BE_FOUND[0]]
    assert b"\r" not in sample, (
        f"{CONTROL_PATH} checked out CRLF, so conversion is active here, yet "
        f"{MUST_BE_FOUND[0]} did not check out LF"
    )


def test_the_rule_does_not_reach_beyond_shell_scripts():
    """`*.ps1` and friends stay on the platform default, deliberately.

    A blanket ``* eol=lf`` would pass every assertion above while changing
    what lands in every Windows working tree in the repository.
    """
    assert _check_attr("eol", [CONTROL_PATH])[CONTROL_PATH] == "unspecified", (
        f"{CONTROL_PATH} now has an eol attribute; the line-ending rule was "
        "meant to cover shell scripts only"
    )


@pytest.mark.parametrize(
    "path, blob",
    [
        ("bin/doctor", b"#!/usr/bin/env bash\nset -euo pipefail\n"),
        ("bin/doctor", b"#!/bin/sh\n"),
        ("bin/doctor", b"#!/bin/bash -e\n"),
        ("bin/doctor", b"#! /usr/bin/env sh\n"),
        ("bin/doctor", b"#!/usr/bin/env zsh\n"),
        ("script.SH", b"echo hi\n"),
        ("script.sh", b""),
    ],
)
def test_the_detector_fires(path: str, blob: bytes):
    """Positive control: the shebang branch is unexercised by this repository.

    Every shell script here happens to end in ``.sh``, so the branch that
    catches the *next* one is dead code as far as the live population is
    concerned -- and dead detectors report a clean tree, which reads exactly
    like success.
    """
    assert _looks_like_shell_script(path, blob) is True


@pytest.mark.parametrize(
    "path, blob",
    [
        ("tool.py", b"#!/usr/bin/env python3\nprint(1)\n"),
        ("tool.mjs", b"#!/usr/bin/env node\n"),
        ("README.md", b"# heading\n"),
        ("notes.txt", b"a line\n#!/bin/bash\n"),
        ("shrink.py", b"#!/usr/bin/env pythonsh\n"),
        ("empty", b""),
    ],
)
def test_the_detector_stays_quiet(path: str, blob: bytes):
    """Negative control: over-matching would put non-shell files under the rule.

    ``notes.txt`` is the one that matters -- a ``#!`` below the first line is a
    comment, and treating it as a shebang would demand ``eol=lf`` for prose.
    """
    assert _looks_like_shell_script(path, blob) is False
