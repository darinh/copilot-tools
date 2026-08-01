#!/usr/bin/env python3
"""Certify that every commit in this repository was made under an allowed identity.

**Why this exists.** On 2026-07-31 the entire history of this repository was
rewritten to purge a corporate address that had been committed by accident,
and that was not enough: the address survived in ``refs/pull/*``, so the
GitHub repository itself had to be deleted and recreated before it was gone.
On 2026-08-01 the repository went public. The rule that every commit uses
``darinh@gmail.com`` was, until this module, a *habit* -- written in the
project instructions as "verify before committing: ``git config user.email``"
-- and a habit is enforced by whoever happens to remember it. The address came
from a **different machine's global config**, which is precisely the condition
under which nobody remembers.

An identity mistake is unlike the other defects here: it cannot be fixed
forward. Every second between the push and the discovery is publication, and
the remedy is not a patch but a history rewrite plus the destruction of the
remote. That asymmetry is the whole argument for checking it mechanically.

**Three outcomes, not two.** This module answers ``CLEAN``, ``VIOLATIONS`` or
``UNDETERMINED``, and the third one is the point. "I examined the history and
every identity is allowed" and "I could not examine the history" are the two
most easily confused outcomes in any scanner, and they are byte-identical at
the reporting layer unless something forces them apart. A checker that returns
success when it could not look does not fail to find anything -- it fails to
*look*, and it reports that as health.

That is not hypothetical here. Every job in ``.github/workflows/ci.yml`` uses
``actions/checkout@v4`` at its default ``fetch-depth: 1``. A history scan
bolted onto any of them would see exactly one commit and certify 323 commits
of history clean, forever, in green. So a shallow clone is ``UNDETERMINED``
and exits non-zero, and an empty range is ``UNDETERMINED`` too: zero commits
examined must never be reported as zero commits in violation.

**An allowlist, not a blocklist.** Blocking ``@microsoft.com`` would catch
only the address that already burned this repository. The risk documented in
the project instructions is a *different machine's* global config, and the
next machine belongs to a different employer. Naming what is permitted is the
only form of this rule that survives the next laptop.

**What is checked.** Author *and* committer, for every commit reachable from
the given revisions, plus the ``Co-authored-by:`` trailers -- an amend or a
rebase can leave a clean author beside a corporate committer, and this
project's own convention writes a co-author trailer into every commit, so all
three are identity fields that routinely carry an address.

Exit codes: ``0`` clean, ``1`` violations found, ``2`` could not determine.
Usage::

    python git_identity.py [--repo PATH] [REV ...]      # default: --all

**What this cannot see, and what it therefore costs.** Three limits are worth
writing down beside the check rather than discovering them in a red build:

* ``actions/checkout`` fetches ``refs/heads/*`` and ``refs/tags/*`` even at
  ``fetch-depth: 0``. It does **not** fetch ``refs/pull/*`` -- the very refs
  that made the 2026-07-31 incident unfixable. The workflow therefore fetches
  them explicitly before scanning. A local run without that fetch is scanning
  a narrower population than CI is.
* Merging through the GitHub web UI authors the merge commit with the
  *clicking user's* primary GitHub address, which is not necessarily the one
  configured on any machine. Merge locally, or keep that address allowed.
* Co-authors are read with git's own trailer parser, which means the final
  paragraph only, and no indented lines. An address written into
  commit-message *prose* is therefore not reported. That boundary is not a
  convenience: it is the same one GitHub uses to decide whether a commit has
  a co-author, so it separates "an address that is an identity on this
  commit" from "an address that is text in it". This module found the
  distinction by reporting a violation against its own commit, whose message
  quoted four example trailers. The address in prose is still published; if
  that ever needs catching it needs its own check under its own name, not a
  quietly widened definition of "co-author".
* The scan is unconditional over all of history, so a single bad commit
  reddens every subsequent build until the history is rewritten. That is
  deliberate and it is the same bill the incident already presented: there is
  no version of "tolerate the published address" that is cheaper than removing
  it. A fork inherits this, and a fork containing its owner's commits will
  fail -- correctly, by this repository's rule, which is not theirs.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import NamedTuple

# Addresses permitted to appear as author, committer or co-author.
#
# Both entries are measured from this repository's history rather than
# assumed: `git log --all --format=%ae` yields darinh@gmail.com alone, and
# `%ce` adds noreply@github.com, which is the committer GitHub records for a
# merge performed through the web UI.
ALLOWED_EXACT = frozenset({
    "darinh@gmail.com",
    "noreply@github.com",
})

# Per-user GitHub noreply addresses. This covers the `Co-authored-by: Copilot
# <223556219+Copilot@users.noreply.github.com>` trailer this project requires
# on every commit, and any commit made with GitHub's email-privacy setting on.
# These are issued by GitHub and cannot carry an employer's domain.
ALLOWED_SUFFIXES = ("@users.noreply.github.com",)

CLEAN = 0
VIOLATIONS = 1
UNDETERMINED = 2

# Field separators for `git log --format`. Deliberately not spaces or newlines:
# git imposes no character restrictions on the email field, so a crafted or
# corrupted address containing whitespace would otherwise shift every
# subsequent column and be parsed as a different, possibly allowed, value.
# 0x1f and 0x1e cannot appear in a well-formed address and are stripped by git
# from none of them.
_FIELD = "\x1f"
_RECORD = "\x1e"

# Co-author trailers are read with git's own trailer parser rather than by
# searching the message, and that choice was forced by this module reporting a
# violation against its own commit. A commit message that *discusses* trailers
# -- as every commit on this branch does -- contains lines that are
# indistinguishable from trailers to a regex, and the scan flagged four of
# them. A check that fires on its own documentation is the kind of noise that
# gets a check deleted, which is a worse outcome than the false positive.
#
# `%(trailers:key=...)` is also the more correct question. Git defines a
# trailer as living in the message's final paragraph, and that same definition
# is what decides whether GitHub attributes co-authorship -- so it is exactly
# the line between "an address that is an identity on this commit" and "an
# address that appears in prose". Author and committer are unaffected.
#
# The deliberate non-goal: an address written into commit-message prose is not
# an identity and is not reported here. It is still published, and if that
# ever needs catching it needs its own check with its own name, not a widened
# definition of "co-author".
_TRAILER_KEY = "Co-authored-by"
_TRAILERS_FMT = f"%(trailers:key={_TRAILER_KEY},valueonly,unfold)"

# Addresses are tokens containing an `@`, wherever they sit in the trailer's
# value. The value may hold several, and this deliberately does not branch on
# whether they are wrapped in angle brackets: a previous version took the
# bracketed ones when there were any and the bare remainder otherwise, so
# `Evil bad@corp and Good <ok@allowed>` reported only the allowed address.
# That is the same "stop at the first thing you can approve of" bug the
# bracket handling was written to fix, rotated one axis. Brackets are
# punctuation; an address is an address.
_ADDRESS = re.compile(r"[^\s<>(),;:\"']+@[^\s<>(),;:\"']+")


def _trailer_addresses(value: str) -> list[str]:
    """Every address in the ``Co-authored-by`` values git reported."""
    found: list[str] = []
    for address in _ADDRESS.findall(value):
        # A trailing period is sentence punctuation, never part of an
        # address; leaving it on would turn an allowed address into an
        # unrecognised one and report a violation that is not there.
        found.append(address.rstrip(".").strip())
    return found


class Identity(NamedTuple):
    """One address, and where it was found."""

    commit: str
    field: str  # "author", "committer" or "co-author"
    email: str

    def describe(self) -> str:
        return f"{self.commit[:12]} {self.field:<9} {self.email}"


class Result(NamedTuple):
    """The verdict, the evidence for it, and how much was actually read.

    ``examined`` is reported on every outcome including the clean one, because
    "clean" is only meaningful alongside the size of the population it was
    measured over. A clean verdict over zero commits is the failure this
    module exists to prevent, and it is invisible unless the count is printed.
    """

    state: int
    examined: int
    offenders: tuple[Identity, ...]
    reason: str  # why undetermined; "" otherwise


def is_allowed(email: str) -> bool:
    """True when `email` is an identity this project permits.

    Compared case-insensitively: git preserves the case it was given, so the
    same mailbox can be committed as `Darinh@Gmail.com` and would otherwise
    read as an unknown address -- a false positive that teaches people to
    ignore the check.
    """
    normalised = email.strip().lower()
    if normalised in {a.lower() for a in ALLOWED_EXACT}:
        return True
    return any(normalised.endswith(s.lower()) for s in ALLOWED_SUFFIXES)


class _GitUnavailable(Exception):
    """git could not be run, or refused the question."""


def _git(args: list[str], repo: str) -> str:
    """Run a git command in `repo` and return stdout, or raise _GitUnavailable.

    Every failure mode collapses to one exception on purpose: the caller turns
    it into UNDETERMINED, and the one thing it must never do is turn it into
    an empty list of offenders.

    The encoding is named rather than inherited. `text=True` alone decodes with
    the locale's preferred encoding, which on Windows is cp1252 -- and git
    stores commit data as bytes, so a single byte no codepage covers (measured:
    0x81 in a commit message) raised UnicodeDecodeError inside subprocess's
    reader thread, left `proc.stdout` as None, and surfaced as an uncaught
    AttributeError. Python then exits 1, which in this program means
    VIOLATIONS. An unreadable log would have been reported as a bad identity.

    `errors="replace"` keeps that failure safe in the other direction too: a
    mangled address becomes replacement characters, matches nothing in the
    allowlist, and is reported rather than skipped.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
    except FileNotFoundError as exc:  # git is not installed
        raise _GitUnavailable("git is not installed or not on PATH") from exc
    except OSError as exc:
        raise _GitUnavailable(f"git could not be run: {exc}") from exc
    except subprocess.SubprocessError as exc:  # includes TimeoutExpired
        raise _GitUnavailable(f"git did not complete: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        first = detail[0] if detail else f"exit {proc.returncode}"
        raise _GitUnavailable(f"`git {' '.join(args)}` failed: {first}")
    if proc.stdout is None:
        # Belt and braces for the mode above: a successful exit with no stdout
        # object at all is git answering nothing, not git answering "none".
        raise _GitUnavailable(
            f"`git {' '.join(args)}` produced no readable output")
    return proc.stdout


def _parse_log(raw: str) -> list[Identity]:
    """Identities from `git log` output in this module's record format."""
    found: list[Identity] = []
    for record in raw.split(_RECORD):
        record = record.strip("\r\n")
        if not record:
            continue
        parts = record.split(_FIELD)
        if len(parts) != 4:
            # A record that does not have the shape we asked for is not a
            # record with no addresses in it. Refusing here keeps a format
            # change from quietly becoming a clean bill of health.
            raise _GitUnavailable(
                f"unparseable git log record ({len(parts)} fields, expected 4)")
        commit, author, committer, trailers = parts
        if "%(trailers" in trailers:
            # git echoed the placeholder instead of expanding it, which is what
            # a version that does not support `%(trailers:key=...)` does --
            # silently, with exit status 0. Believing it would mean reporting
            # "no co-authors" for every commit in the repository, which is a
            # clean bill of health issued by a scan that never ran.
            raise _GitUnavailable(
                "this git does not support `%(trailers:key=...)`; co-author "
                "trailers could not be read")
        found.append(Identity(commit, "author", author))
        found.append(Identity(commit, "committer", committer))
        for address in _trailer_addresses(trailers):
            found.append(Identity(commit, "co-author", address))
    return found


def scan(repo: str = ".", revs: tuple[str, ...] = ("--all",)) -> Result:
    """Examine every commit reachable from `revs` and rule on its identities."""
    try:
        shallow = _git(["rev-parse", "--is-shallow-repository"], repo).strip()
    except _GitUnavailable as exc:
        return Result(UNDETERMINED, 0, (), str(exc))

    # The load-bearing refusal. A shallow clone answers questions about the
    # commits it has and says nothing about the ones it does not, so a scan of
    # it is not a weaker certification -- it is an unrelated one wearing the
    # same exit code.
    if shallow == "true":
        return Result(
            UNDETERMINED, 0, (),
            "the clone is shallow, so most of the history is not present. "
            "This scan cannot certify commits it cannot see. In GitHub "
            "Actions, set `fetch-depth: 0` on actions/checkout.")
    if shallow != "false":
        return Result(
            UNDETERMINED, 0, (),
            f"git could not say whether the clone is shallow (answered "
            f"{shallow!r}), and an unanswered question is not a 'no'.")

    fmt = f"--format=%H{_FIELD}%ae{_FIELD}%ce{_FIELD}{_TRAILERS_FMT}{_RECORD}"
    try:
        raw = _git(["log", fmt, *revs], repo)
        identities = _parse_log(raw)
    except _GitUnavailable as exc:
        return Result(UNDETERMINED, 0, (), str(exc))

    commits = {i.commit for i in identities}
    if not commits:
        # Zero commits is the vacuous pass in its purest form: every "no commit
        # uses a forbidden address" assertion is true of an empty history.
        return Result(
            UNDETERMINED, 0, (),
            f"no commits were found for {' '.join(revs)}, so there was "
            "nothing to certify. An empty history satisfies every assertion "
            "about it, which is why this is not reported as clean.")

    offenders = tuple(i for i in identities if not is_allowed(i.email))
    state = VIOLATIONS if offenders else CLEAN
    return Result(state, len(commits), offenders, "")


def report(result: Result, revs: tuple[str, ...], stream=None) -> None:
    """Print the verdict, including the population it was measured over.

    `stream` is resolved here rather than in the signature. A default of
    `sys.stdout` is evaluated once, at import, and binds whatever stdout was
    at that moment -- so every later redirection is ignored and the report is
    written past it. That is not merely a testing inconvenience: it is a
    reporter that keeps writing to a stream nobody is reading, which is the
    same shape as the failures this module is about.
    """
    stream = sys.stdout if stream is None else stream
    scope = " ".join(revs)
    if result.state == UNDETERMINED:
        print(f"UNDETERMINED: {result.reason}", file=stream)
        print("Refusing to report this history as clean.", file=stream)
        return
    if result.state == CLEAN:
        print(f"CLEAN: {result.examined} commit(s) in {scope}; every author, "
              f"committer and co-author is an allowed identity.", file=stream)
        return
    print(f"VIOLATIONS: {len(result.offenders)} disallowed identit(ies) "
          f"across {result.examined} commit(s) in {scope}.", file=stream)
    for offender in result.offenders:
        print(f"  {offender.describe()}", file=stream)
    print("", file=stream)
    print("Allowed: " + ", ".join(sorted(ALLOWED_EXACT))
          + "".join(f", *{s}" for s in ALLOWED_SUFFIXES), file=stream)
    print("A published commit cannot be fixed forward. See the module "
          "docstring for what removing one previously cost.", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="git_identity.py",
        description="Certify commit author/committer/co-author identities.")
    parser.add_argument("--repo", default=".",
                        help="repository to examine (default: cwd)")
    parser.add_argument("revs", nargs="*", default=None,
                        help="revisions to scan (default: --all)")
    args = parser.parse_args(argv)
    revs = tuple(args.revs) if args.revs else ("--all",)
    result = scan(args.repo, revs)
    report(result, revs)
    return result.state


if __name__ == "__main__":
    sys.exit(main())
