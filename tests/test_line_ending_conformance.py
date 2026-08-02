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

import shutil
import subprocess
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Suffixes that are a shell script regardless of content.
SHELL_SUFFIXES = frozenset({".sh", ".bash", ".ksh", ".zsh"})

#: Interpreter names whose scripts break on a trailing CR. Matched as a whole
#: basename rather than as a substring: an earlier spelling of this searched
#: the shebang line for ``sh`` with a word boundary, which classed
#: ``#!/usr/bin/perl # sh`` as a shell script -- the trailing words of a
#: shebang are arguments, not a comment, so any interpreter can be handed a
#: word that ends the search in the wrong place.
SHELL_INTERPRETERS = frozenset({
    "sh", "bash", "dash", "ksh", "ksh93", "mksh", "pdksh", "zsh", "ash",
})

#: Interpreters that only name another interpreter. ``env`` is the common one;
#: busybox dispatches on its first argument the same way.
_INTERPRETER_WRAPPERS = frozenset({"env", "busybox"})

#: A UTF-8 byte-order mark, which a Windows editor will happily put in front of
#: a ``#!``. The kernel then refuses the file, so such a script is broken for a
#: second reason -- but it must not silently fall outside the line-ending rule
#: on the way past.
_BOM = b"\xef\xbb\xbf"

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


def _git(*args: str) -> subprocess.CompletedProcess:
    """Run git at the repository root, decoding as UTF-8, or fail loudly.

    Every caller here wants text. The one place that needs raw bytes --
    ``_index_blobs``, where a CR is the thing being measured -- calls
    ``subprocess.run`` itself rather than routing through a ``text=False``
    flag, because a helper that decodes on some calls and not on others cannot
    name its codec in a form the encoding conformance scan can read.

    It takes no environment guards. Whether this is a checkout and whether git
    exists are settled once, at import, by ``_require_git_checkout`` -- because
    a guard *inside* the helper cannot help the module-level discovery that
    calls it, and ``pytest.skip`` raised during collection is a collection
    error rather than a skip.
    """
    return subprocess.run(
        ["git", *args], cwd=str(REPO), check=True, capture_output=True,
        encoding="utf-8", errors="replace",
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

    The two non-blob replies are handled by name rather than left to the
    ``blob`` assertion, because getting them wrong is not a local mistake. A
    ``missing`` record has no size and no payload, and ``git cat-file`` still
    exits 0 for it, so ``check=True`` does not notice; consuming ``size + 1``
    bytes off the front for the *next* path then mis-slices every remaining
    reply in the batch. The failure would surface as a wrong verdict about
    some unrelated file rather than as a complaint about this one.
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
        # `<spec> missing` -- two fields, no payload line follows.
        assert fields[-1:] != [b"missing"], (
            f"{path!r} is listed by `git ls-files` but has no blob in the "
            "index; the two views of the index disagree, so the shell-script "
            "population cannot be trusted"
        )
        assert len(fields) == 3 and fields[1] == b"blob", (
            f"unexpected `git cat-file --batch` header for {path!r}: "
            f"{header!r}. A gitlink (submodule) reads as `commit` here; it "
            "would need excluding from the population rather than parsing"
        )
        size = int(fields[2])
        blobs[path], rest = rest[:size], rest[size + 1:]
    assert not rest, f"unconsumed `git cat-file --batch` output: {rest[:80]!r}"
    return blobs


def _shebang_interpreter(first_line: bytes) -> str | None:
    """The interpreter a ``#!`` line actually selects, or ``None``.

    Walks the line the way the kernel and ``env`` do between them, rather than
    searching it: the first word is the interpreter, a word beginning with
    ``-`` is a flag (``env -S``), and a wrapper such as ``env`` or ``busybox``
    defers to the next word. Everything after that is an argument to the
    program already chosen, which is why a search cannot be correct here --
    ``#!/usr/bin/perl # sh`` selects perl and passes it two arguments.

    A leading UTF-8 BOM is stripped first. Such a file is already broken as a
    script -- the kernel wants ``#!`` in the first two bytes -- but it must
    still be classified as one, or it drops out of the population silently and
    the line-ending rule stops covering it.
    """
    if first_line.startswith(_BOM):
        first_line = first_line[len(_BOM):]
    if not first_line.startswith(b"#!"):
        return None
    # `replace` rather than a raise: a decoding accident must not be able to
    # answer "not a shell script", which is the verdict that loses coverage.
    words = first_line[2:].decode("utf-8", "replace").split()
    while words:
        word = words.pop(0)
        if word.startswith("-"):
            continue
        name = PurePosixPath(word.replace("\\", "/")).name
        if name in _INTERPRETER_WRAPPERS:
            continue
        return name
    return None


def _looks_like_shell_script(path: str, blob: bytes) -> bool:
    """True if `path` is a shell script, by suffix or by shebang.

    The shebang branch is what keeps this from being a list of filenames. It
    resolves the interpreter and compares its basename, so ``#!/usr/bin/env
    bash`` and ``#!/bin/sh`` both count while ``#!/usr/bin/env python3`` does
    not -- and neither does a ``#!`` appearing anywhere below the first line,
    which is a comment.
    """
    if Path(path).suffix.lower() in SHELL_SUFFIXES:
        return True
    first_line = blob.split(b"\n", 1)[0]
    return _shebang_interpreter(first_line) in SHELL_INTERPRETERS


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


def _require_git_checkout() -> None:
    """Settle the environment once, at import, before anything discovers.

    The two unusable environments are not the same and must not get the same
    verdict:

    * **Not a checkout** (an unpacked sdist, a copied tree) has no index and no
      attributes, so there is genuinely nothing to measure. Skip the module.
    * **A checkout with no ``git``** is a case where the check was supposed to
      run and could not. Skipping it would report a clean tree, which is the
      failure direction this whole file exists to prevent -- so it is loud.

    Both verdicts are reached at import rather than inside ``_git``, because
    ``SHELL_SCRIPTS`` is computed at import and feeds ``parametrize``. A guard
    that only fires inside a test body arrives after the parametrised cases
    have already been built from an empty list -- at which point they do not
    fail, they cease to exist.
    """
    if not (REPO / ".git").exists():
        pytest.skip(
            "not a git checkout; there is no index or attributes file to "
            "inspect here",
            allow_module_level=True,
        )
    if shutil.which("git") is None:
        pytest.fail(
            "git is required to verify line-ending attributes, and this is a "
            "checkout where they were meant to be verified",
            pytrace=False,
        )


_require_git_checkout()

SHELL_SCRIPTS = _shell_scripts()


def test_the_batch_parser_refuses_a_path_with_no_blob():
    """Positive control for the ``missing`` branch of `_index_blobs`.

    ``git cat-file --batch`` answers an unresolvable spec with ``<spec>
    missing`` and **exits 0**, so ``check=True`` lets it through. Measured
    here rather than assumed: the branch is unreachable from the live
    population, because every path comes from ``git ls-files`` and does
    resolve. An unexercised branch in a parser is the parser's least trusted
    line, and this one guards a desync rather than a wrong answer.
    """
    with pytest.raises(AssertionError, match="no blob in the index"):
        _index_blobs(["no/such/path/at/all.sh"])


def test_the_batch_parser_keeps_replies_aligned_with_their_paths():
    """Negative control: the size/delimiter arithmetic over a real batch.

    The bug the ``missing`` guard prevents is a *silent* one -- a mis-sliced
    reply hands file A's bytes back under file B's name, and every assertion
    downstream then judges the wrong file while still passing or failing for
    reasons that look plausible. So check that a multi-path batch comes back
    attributed correctly, against bytes read independently of git.
    """
    sample = [p for p in MUST_BE_FOUND if p in SHELL_SCRIPTS][:3]
    assert len(sample) >= 2, "need at least two known scripts to detect a desync"
    batched = _index_blobs(sample)
    assert set(batched) == set(sample)
    for path in sample:
        alone = _index_blobs([path])[path]
        assert batched[path] == alone, (
            f"{path} came back with different bytes in a batch than alone; "
            "the `git cat-file --batch` reply framing is being mis-sliced"
        )
        assert batched[path], f"{path} came back empty, which no script is"


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


def _assert_control_is_usable() -> None:
    """The causation control is only a control while nothing governs it.

    ``test_the_lf_checkout_is_caused_by_the_attribute`` reads an LF result for
    ``CONTROL_PATH`` as "this checkout does not convert", and skips. That
    inference holds only if ``CONTROL_PATH`` is still on the platform default.
    Give it *any* attribute -- one line of ``setup.ps1 -text`` is enough -- and
    the control writes LF on a machine that is converting perfectly well, so
    the causation test skips with a reason that is simply untrue and the whole
    file passes without its only non-vacuous assertion ever running.

    Measured, not supposed: with that one line added, this file reported
    ``51 passed, 1 skipped`` on a checkout where ``core.autocrlf=true`` and
    ``README.md`` checked out with 345 carriage returns.

    So the premise is asserted separately, and loudly, rather than being left
    to a test that answers a failure with a skip.
    """
    assert CONTROL_PATH in _tracked(), (
        f"the control file {CONTROL_PATH} is no longer tracked; pick another "
        "tracked text file outside the shell-script patterns"
    )
    assert CONTROL_PATH not in SHELL_SCRIPTS, (
        f"{CONTROL_PATH} is now classed as a shell script and can no longer "
        "serve as the outside-the-rule control"
    )
    governed = {attr: _check_attr(attr, [CONTROL_PATH])[CONTROL_PATH]
                for attr in ("text", "eol")}
    named = {a: v for a, v in governed.items() if v != "unspecified"}
    assert not named, (
        f"{CONTROL_PATH} is the control for 'conversion is active here', so "
        f"it has to be on the platform default, but .gitattributes now gives "
        f"it {named}. An LF checkout of it no longer means the platform "
        "declined to convert, so the causation test would skip instead of "
        "failing -- pick a different control or drop the rule"
    )


def test_the_causation_control_is_still_governed_by_nothing():
    """Loud tripwire for the premise the causation test skips on.

    Deliberately a separate test with no skip in it: the failure this catches
    turns the causation test into a skip, and a skip is the one result nobody
    reads.
    """
    _assert_control_is_usable()


def test_the_lf_checkout_is_caused_by_the_attribute(tmp_path: Path):
    """Control: on a converting checkout, an uncovered file still gets CRLF.

    Without this, every assertion above is satisfied by a platform that was
    never going to write a CR anyway -- which is every Linux CI leg, i.e. seven
    of the eight. This is the one test that can tell "the rule works" apart
    from "the rule was never load-bearing here".
    """
    _assert_control_is_usable()
    written = _checkout_index(tmp_path, [CONTROL_PATH])[CONTROL_PATH]
    if b"\r\n" not in written:
        pytest.skip(
            f"{CONTROL_PATH} is on the platform default and still checked out "
            "LF, so this checkout does not convert line endings and an LF "
            "result proves nothing about the attribute"
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
        # `env -S` is how a shebang passes more than one argument portably;
        # the flag names no interpreter and must be walked past.
        ("bin/doctor", b"#!/usr/bin/env -S bash -e\n"),
        # busybox dispatches on its first argument, like `env`.
        ("bin/doctor", b"#!/bin/busybox sh\n"),
        ("bin/doctor", b"#!/bin/dash\n"),
        # A CRLF shebang: the file this rule exists to fix, classified before
        # it is fixed. `split()` treats the CR as whitespace.
        ("bin/doctor", b"#!/usr/bin/env bash\r\nset -e\r\n"),
        # A UTF-8 BOM in front of the `#!`. The kernel will not run this, but
        # it is still a shell script and must stay inside the rule rather than
        # dropping silently out of the population.
        ("bin/doctor", b"\xef\xbb\xbf#!/usr/bin/env bash\n"),
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
        # The words after the interpreter are ARGUMENTS, not a comment. A
        # search for `sh` anywhere in the line called this a shell script.
        ("tool.pl", b"#!/usr/bin/perl # sh\n"),
        ("tool.rb", b"#!/usr/bin/env ruby -e 'sh'\n"),
        # Near-misses on the interpreter name itself.
        ("tool", b"#!/usr/bin/env bash3\n"),
        ("tool", b"#!/usr/bin/env fish\n"),
        ("tool", b"#!/usr/bin/env shellcheck\n"),
        # A `#!` with no interpreter at all resolves to nothing.
        ("tool", b"#!\n"),
        ("tool", b"#!/usr/bin/env\n"),
    ],
)
def test_the_detector_stays_quiet(path: str, blob: bytes):
    """Negative control: over-matching would put non-shell files under the rule.

    ``notes.txt`` is the one that matters -- a ``#!`` below the first line is a
    comment, and treating it as a shebang would demand ``eol=lf`` for prose.
    """
    assert _looks_like_shell_script(path, blob) is False


@pytest.mark.parametrize(
    "first_line, expected",
    [
        (b"#!/bin/sh", "sh"),
        (b"#!/bin/bash -e", "bash"),
        (b"#!/usr/bin/env bash", "bash"),
        (b"#!/usr/bin/env -S bash -e", "bash"),
        (b"#!/bin/busybox sh", "sh"),
        (b"#!/usr/bin/perl # sh", "perl"),
        (b"\xef\xbb\xbf#!/bin/bash", "bash"),
        (b"#!/usr/bin/env", None),
        (b"#!", None),
        (b"not a shebang", None),
    ],
)
def test_the_interpreter_is_resolved_not_searched(first_line: bytes,
                                                  expected: str | None):
    """The resolution itself, asserted apart from the shell/not-shell verdict.

    ``#!/usr/bin/perl # sh`` is the case worth naming: it must come back as
    ``perl``. A detector that merely answered "not a shell" could do so for the
    wrong reason and would drift back to a substring search unnoticed.
    """
    assert _shebang_interpreter(first_line) == expected
