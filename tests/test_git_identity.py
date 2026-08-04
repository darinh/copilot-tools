"""The commit-identity scan, and the ways it could certify without looking.

This file is the counterpart to ``git_identity.py``. That module exists
because purging one accidentally-committed corporate address from this
repository cost a full history rewrite *and* the deletion and recreation of
the GitHub remote -- ``refs/pull/*`` kept the address alive through the
rewrite -- and the repository is now public, so the window between a push and
a discovery is publication.

Two kinds of test live here and the second kind is the one that matters.

The first kind is ordinary: a disallowed address in an author, a committer or
a ``Co-authored-by:`` trailer must be found, and every permitted spelling must
still pass. Each detector has both -- a positive control asserting it fires
and a negative control asserting the clean spelling is not flagged -- because
a detector broken into matching nothing reports the whole history clean, which
reads exactly like success.

The second kind guards the failure this module was actually designed around:
**a scan that certifies a history it never read.** Every job in
``.github/workflows/ci.yml`` checks out at the default ``fetch-depth: 1``, so
a history scan added to any of them would examine one commit and pronounce 323
clean. "I looked and found nothing" and "I could not look" are byte-identical
at the exit code unless something forces them apart, so the tests below assert
that a shallow clone, an empty repository, an absent git and an unparseable
log each produce ``UNDETERMINED`` and a *non-zero* exit -- and, in the case
that matters most, that a shallow clone of a repository which genuinely
*contains* a violation still refuses rather than passing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Imported unconditionally, and declared in the `dev` extra so it is present
# wherever the suite runs. A `pytest.importorskip` here would turn "PyYAML is
# missing" into a silent skip, which is the same defect the module under test
# exists to prevent: the assertions below would stop running and report that
# by saying nothing at all.
import yaml

import git_identity
from git_identity import CLEAN, UNDETERMINED, VIOLATIONS, Result, is_allowed, scan

# One owner for "what are the workflow files". Globbing them here is what let
# a `.yaml` workflow escape every assertion in this file.
from test_workflow_discovery_conformance import workflow_paths

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED = "darinh@gmail.com"
CORPORATE = "dahoove@microsoft.com"


def _git(*args, cwd) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=path)
    return path


def _commit(repo, message="a commit", *, email=ALLOWED, name="Test",
            body=None, filename="f.txt", content="x\n"):
    """One commit authored *and* committed as `email`, with an optional body."""
    (repo / filename).write_text(content, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    args = ["-c", f"user.email={email}", "-c", f"user.name={name}",
            "commit", "-m", message]
    if body is not None:
        args += ["-m", body]
    _git(*args, cwd=repo)


@pytest.fixture
def clean_repo(tmp_path):
    repo = _repo(tmp_path / "clean")
    _commit(repo, "init")
    return repo


# ---------------------------------------------------------------------------
# is_allowed: the rule itself, independent of git.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("email", [
    ALLOWED,
    "noreply@github.com",                          # GitHub web-flow committer
    "223556219+Copilot@users.noreply.github.com",  # the required co-author
    "someone@users.noreply.github.com",
    "  darinh@gmail.com  ",                        # git can hand back padding
    "DarinH@Gmail.Com",                            # git preserves given case
])
def test_permitted_identities_are_allowed(email):
    assert is_allowed(email), f"{email!r} is a permitted identity"


@pytest.mark.parametrize("email", [
    CORPORATE,
    "someone@microsoft.com",
    "someone@example.corp",          # the NEXT employer, not the last one
    "",
    "darinh@gmail.com.attacker.net",  # exact rules must not match a prefix
    "users.noreply.github.com",       # the suffix without the @
    "evil@notusers.noreply.github.com",
    # The suffix embedded mid-string, under a domain somebody else controls.
    # This is the case that anchoring exists for: `endswith` refuses it and a
    # substring test accepts it, and a mutation of exactly that shape survived
    # this suite until these three were added.
    "attacker@users.noreply.github.com.evil.net",
    "attacker@users.noreply.github.com.co",
    "@users.noreply.github.com.example.org",
])
def test_disallowed_identities_are_rejected(email):
    assert not is_allowed(email), f"{email!r} must not be treated as allowed"


def test_the_allowed_suffix_must_be_anchored_at_the_end():
    """A named test for the mutation that survived, so it cannot come back.

    Substring-matching the suffix is the single most plausible wrong
    implementation of this rule, and it hands every address under
    ``users.noreply.github.com.<anything-they-registered>`` a pass. Registering
    that domain is not exotic; it is the standard shape of a suffix-matching
    bypass.
    """
    allowed_suffix = "@users.noreply.github.com"
    hostile = f"attacker{allowed_suffix}.evil.example"
    assert allowed_suffix in hostile, "premise: a substring test would accept it"
    assert not is_allowed(hostile), (
        "the suffix rule is matching anywhere in the address rather than at "
        "the end")
    assert is_allowed(f"someone{allowed_suffix}"), (
        "negative control: the genuine suffix still passes")


def test_the_allowlist_names_what_is_permitted_rather_than_what_is_banned():
    """A blocklist would only ever catch the address that already burned us.

    The documented risk is a *different machine's* global config, and the next
    machine belongs to a different employer. This pins the shape of the rule,
    not its contents: an allowlist rejects an unknown address by default, so
    the assertion is that some arbitrary never-seen domain is refused.
    """
    assert not is_allowed("engineer@some-company-nobody-has-heard-of.example")


# ---------------------------------------------------------------------------
# Positive controls: the detector fires, on each identity field.
# ---------------------------------------------------------------------------

def test_a_corporate_author_is_found(tmp_path):
    repo = _repo(tmp_path / "r")
    _commit(repo, "leak", email=CORPORATE)
    result = scan(str(repo))
    assert result.state == VIOLATIONS
    assert CORPORATE in {o.email for o in result.offenders}
    assert "author" in {o.field for o in result.offenders}


def test_a_corporate_committer_beside_a_clean_author_is_found(tmp_path):
    """The rebase/amend shape: the author survives, the committer is rewritten.

    Checking only the author would pass this, and it is the more likely
    accident of the two -- `git rebase` and `git commit --amend` rewrite the
    committer with the local config while preserving the original author.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init")
    _git("-c", f"user.email={CORPORATE}", "-c", "user.name=Corp",
         "commit", "--amend", "--no-edit", f"--author=Good <{ALLOWED}>",
         cwd=repo)
    result = scan(str(repo))
    assert result.state == VIOLATIONS
    fields = {(o.field, o.email) for o in result.offenders}
    assert ("committer", CORPORATE) in fields
    assert ("author", ALLOWED) not in fields, "the clean author is not an offender"


def test_a_corporate_co_author_trailer_is_found(tmp_path):
    """This project writes a co-author trailer into every commit by convention.

    That makes the trailer a routinely-populated identity field, not an exotic
    one, and an address there is published exactly as widely as the author.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=f"Co-authored-by: Someone <{CORPORATE}>")
    result = scan(str(repo))
    assert result.state == VIOLATIONS
    assert ("co-author", CORPORATE) in {(o.field, o.email) for o in result.offenders}


@pytest.mark.parametrize("trailer", [
    # Two addresses on one line. The first regex here captured only the LAST
    # one, so putting an allowed address after a corporate one made the commit
    # read as clean. Found independently by two reviewers; it is a false
    # negative, which is the direction that publishes.
    f"Co-authored-by: Evil <{CORPORATE}> and Good <{ALLOWED}>",
    # A bare address beside a bracketed one. The first fix for the bug above
    # branched on spelling -- bracketed if any, bare otherwise -- so the
    # bracketed branch won and this address was never examined at all.
    f"Co-authored-by: Evil {CORPORATE} and Good <{ALLOWED}>",
    # Two bare addresses, the allowed one last. The whole line was kept as a
    # single "address", so it ended with an allowed suffix and passed.
    f"Co-authored-by: {CORPORATE} 223556219+Copilot@users.noreply.github.com",
    # Trailing text after the bracket. The old `\\s*$` anchor made the line
    # fail to match at all, so the address was never examined.
    f"Co-authored-by: Evil <{CORPORATE}>  # a note",
    # No brackets. Git's tooling writes them; a human typing the trailer by
    # hand does not, and the address is published either way.
    f"Co-authored-by: {CORPORATE}",
    # Indented, and in a different case than the canonical spelling.
    f"co-authored-BY: Evil <{CORPORATE}>",
    # Comma-separated, which is how a multi-author trailer is often written.
    f"Co-authored-by: <{ALLOWED}>, Evil <{CORPORATE}>",
    # Two separate trailer lines: the allowed one first, so an implementation
    # that stops at the first line it can approve of misses the second.
    f"Co-authored-by: Good <{ALLOWED}>\nCo-authored-by: Evil <{CORPORATE}>",
])
def test_a_corporate_address_cannot_hide_in_a_trailer_line(tmp_path, trailer):
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=trailer)
    result = scan(str(repo))
    assert result.state == VIOLATIONS, (
        f"the corporate address hid in {trailer!r}")
    assert CORPORATE in {o.email for o in result.offenders}


@pytest.mark.parametrize("trailer", [
    # Bare, with a display name in front. An earlier fix kept the whole line
    # as one address, so this was reported as the violation
    # "Someone darinh@gmail.com" -- a false positive, which is the direction
    # that erodes a check by making it noisy enough to switch off.
    f"Co-authored-by: Someone {ALLOWED}",
    f"Co-authored-by: {ALLOWED}",
    f"Co-authored-by: Someone <{ALLOWED}>",
    # Sentence punctuation after a bare address.
    f"Co-authored-by: Someone {ALLOWED}.",
    f"Co-authored-by: A <{ALLOWED}>, B <223556219+Copilot@users.noreply.github.com>",
])
def test_an_allowed_trailer_is_not_reported_however_it_is_spelled(tmp_path, trailer):
    """Negative control for every spelling accepted above.

    The strict direction and the permissive direction need pinning equally:
    the first fix for the multi-address bug traded a false negative for a
    false positive, and only the false negative had a test.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=trailer)
    result = scan(str(repo))
    assert result.state == CLEAN, (
        f"clean trailer {trailer!r} was reported as "
        f"{[o.email for o in result.offenders]}")


# ---------------------------------------------------------------------------
# Where a co-author stops being a co-author. This boundary is git's, not this
# module's, and it is load-bearing in both directions -- so both directions
# are asserted against the SAME address, which is the only way to show the
# rule is about position rather than about the address.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prose", [
    # Indented. Measured: git does not treat an indented line as a trailer,
    # with spaces or with a tab, so GitHub does not attribute co-authorship
    # for it either.
    f"    Co-authored-by: Evil <{CORPORATE}>",
    f"\tCo-authored-by: Evil <{CORPORATE}>",
    # In an earlier paragraph. Git reads trailers from the final paragraph
    # only, so a message that *discusses* a trailer does not acquire one.
    f"Co-authored-by: Evil <{CORPORATE}>\n\nthe real body ends here",
])
def test_a_co_author_line_in_prose_is_not_an_identity(tmp_path, prose):
    """The deliberate non-goal, pinned so it stays deliberate.

    This module found it by reporting a violation against its own commit: the
    commit message explaining the trailer bug quoted four example trailers,
    and a regex over the message body could not tell them from real ones.

    The resolution was to ask git's parser instead, which draws the line at
    the final paragraph -- the same line that decides whether GitHub
    attributes co-authorship. So this is not a hole in the check; it is the
    difference between an address that is an identity on the commit and an
    address that is text in it. The address is still published, and if that
    ever needs catching it needs its own check with its own name, rather than
    a quietly widened definition of "co-author".
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=prose)
    assert scan(str(repo)).state == CLEAN


def test_the_same_address_in_a_real_trailer_is_still_caught(tmp_path):
    """Negative control for the boundary above, and the reason it is safe.

    Without this, every test in the block above is satisfied by a module that
    stopped reading trailers altogether. Same address, same repository shape,
    only the position differs -- so what is asserted is the position rule and
    not the absence of a check.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=f"Co-authored-by: Evil <{CORPORATE}>")
    result = scan(str(repo))
    assert result.state == VIOLATIONS
    assert CORPORATE in {o.email for o in result.offenders}


def test_a_git_too_old_for_trailer_parsing_is_undetermined_not_clean(clean_repo, monkeypatch):
    """The silent half of asking git to do the parsing.

    Measured: given a `%(trailers:...)` option it does not understand, git
    prints the placeholder back verbatim and exits 0. Nothing is logged and
    nothing fails. Believing that answer means reporting "no co-authors" for
    every commit in the repository -- a clean bill of health from a scan that
    never ran, which is the exact failure this module's third state exists to
    prevent.
    """
    real_run = git_identity.subprocess.run
    good = _git("log", "--format=%H", cwd=clean_repo).strip()

    def unsupported(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if "log" in cmd:
            proc.stdout = (f"{good}\x1f{ALLOWED}\x1f{ALLOWED}\x1f"
                           "%(trailers:key=Co-authored-by,valueonly,unfold)\x1e")
            proc.returncode = 0
        return proc

    monkeypatch.setattr(git_identity.subprocess, "run", unsupported)
    result = scan(str(clean_repo))
    assert result.state == UNDETERMINED, (
        "git echoed the trailer placeholder instead of expanding it and the "
        "scan reported the history as clean")
    assert "trailers" in result.reason


def test_an_allowed_address_beside_a_corporate_one_is_not_a_pass(tmp_path):
    """Negative control for the case above: both addresses are reported.

    The bug was not "misses trailers"; it was "stops at the first address it
    can approve of". So the allowed address must still be seen as allowed
    while the corporate one is reported.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=f"Co-authored-by: A <{ALLOWED}> and B <{CORPORATE}>")
    result = scan(str(repo))
    assert {o.email for o in result.offenders} == {CORPORATE}


def test_the_ordinary_single_address_trailer_is_still_read(tmp_path):
    """Negative control: the spelling every commit here uses is not flagged."""
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=(
        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"))
    assert scan(str(repo)).state == CLEAN


def test_one_bad_commit_among_many_good_ones_is_found(tmp_path):
    """The realistic case: a single commit from the other laptop."""
    repo = _repo(tmp_path / "r")
    for i in range(4):
        _commit(repo, f"good {i}", filename=f"g{i}.txt")
    _commit(repo, "from the other machine", email=CORPORATE, filename="bad.txt")
    for i in range(4):
        _commit(repo, f"later {i}", filename=f"l{i}.txt")
    result = scan(str(repo))
    assert result.state == VIOLATIONS
    assert result.examined == 9, "every commit is examined, not just the tip"
    assert {o.email for o in result.offenders} == {CORPORATE}


# ---------------------------------------------------------------------------
# Negative controls: every permitted spelling still passes.
# ---------------------------------------------------------------------------

def test_a_clean_history_passes(clean_repo):
    result = scan(str(clean_repo))
    assert result.state == CLEAN
    assert result.offenders == ()
    assert result.examined == 1


def test_the_required_copilot_trailer_does_not_trip_the_scan(tmp_path):
    """The exact trailer this project mandates must not be a violation.

    A detector that flags the spelling every commit is required to carry is
    one that gets disabled within a day.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init", body=(
        "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"))
    result = scan(str(repo))
    assert result.state == CLEAN, result.offenders


def test_a_github_web_flow_committer_does_not_trip_the_scan(tmp_path):
    """`noreply@github.com` is what GitHub records for a merge made in the UI.

    It is already present in this repository's real history, so rejecting it
    would make the check red on arrival -- and a check that is red on arrival
    gets its allowlist widened in a hurry rather than carefully.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "init")
    _git("-c", "user.email=noreply@github.com", "-c", "user.name=GitHub",
         "commit", "--amend", "--no-edit", cwd=repo)
    assert scan(str(repo)).state == CLEAN


# ---------------------------------------------------------------------------
# The refusals. A scan that cannot look must not report health.
# ---------------------------------------------------------------------------

def test_a_shallow_clone_is_undetermined_not_clean(tmp_path):
    """The failure this module exists to prevent.

    Every job in ci.yml checks out at the default `fetch-depth: 1`. Bolting a
    history scan onto one of them would examine the tip commit and certify
    everything behind it, in green, forever.
    """
    origin = _repo(tmp_path / "origin")
    for i in range(3):
        _commit(origin, f"c{i}", filename=f"f{i}.txt")
    shallow = tmp_path / "shallow"
    _git("clone", "--depth", "1", origin.as_uri(), str(shallow), cwd=tmp_path)

    result = scan(str(shallow))
    assert result.state == UNDETERMINED
    assert result.state != CLEAN
    assert "shallow" in result.reason
    assert "fetch-depth" in result.reason, "the reason names the remedy"


def test_a_shallow_clone_hiding_a_real_violation_still_refuses(tmp_path):
    """The refusal is load-bearing, not cosmetic.

    The bad commit is behind the tip, so a depth-1 clone genuinely cannot see
    it. This asserts the scan says so instead of reporting the one commit it
    can see as a clean history -- which is precisely how a real leak would
    have been certified.
    """
    origin = _repo(tmp_path / "origin")
    _commit(origin, "leak", email=CORPORATE, filename="bad.txt")
    for i in range(3):
        _commit(origin, f"later {i}", filename=f"f{i}.txt")
    shallow = tmp_path / "shallow"
    _git("clone", "--depth", "1", origin.as_uri(), str(shallow), cwd=tmp_path)

    # Premise: the violation really is invisible at this depth, so the test is
    # measuring the refusal rather than a lucky miss.
    log = _git("log", "--format=%ae %ce", cwd=shallow)
    assert CORPORATE not in log, "premise: the bad commit is not in the shallow clone"

    result = scan(str(shallow))
    assert result.state == UNDETERMINED, (
        "a shallow clone must refuse; reporting CLEAN here is the leak")


def test_an_empty_repository_is_undetermined_not_clean(tmp_path):
    """Zero commits satisfies every assertion made about it."""
    repo = _repo(tmp_path / "empty")
    result = scan(str(repo))
    assert result.state == UNDETERMINED
    assert result.examined == 0
    assert "nothing to certify" in result.reason


def test_a_directory_that_is_not_a_repository_is_undetermined(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert scan(str(plain)).state == UNDETERMINED


def test_an_empty_revision_range_is_undetermined_not_clean(clean_repo):
    """`main..main` is empty. Certifying it would certify nothing at all."""
    result = scan(str(clean_repo), ("main..main",))
    assert result.state == UNDETERMINED
    assert result.examined == 0


def test_git_being_absent_is_undetermined_not_clean(clean_repo, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_identity.subprocess, "run", no_git)
    result = scan(str(clean_repo))
    assert result.state == UNDETERMINED
    assert "git is not installed" in result.reason


def test_git_timing_out_is_undetermined_not_clean(clean_repo, monkeypatch):
    def times_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=120)

    monkeypatch.setattr(git_identity.subprocess, "run", times_out)
    assert scan(str(clean_repo)).state == UNDETERMINED


def test_an_unparseable_log_record_is_undetermined_not_clean(clean_repo, monkeypatch):
    """A format change must not quietly become a clean bill of health.

    The malformed record is served *beside a valid one*, and that detail is
    the test. An earlier version returned only the malformed record, so an
    implementation that silently dropped bad records would have produced an
    empty population and been caught by the empty-history refusal instead --
    the test would have stayed green while the behaviour it names was gone.
    With a valid record present, dropping the bad one yields CLEAN over one
    commit, which is exactly the wrong answer this asserts against.
    """
    real_run = git_identity.subprocess.run
    good = _git("log", "--format=%H", cwd=clean_repo).strip()

    def mangle(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if "log" in cmd:
            valid = f"{good}\x1f{ALLOWED}\x1f{ALLOWED}\x1fsubject\x1e"
            proc.stdout = valid + "not\x1fthe\x1eshape\x1e"
        return proc

    monkeypatch.setattr(git_identity.subprocess, "run", mangle)
    result = scan(str(clean_repo))
    assert result.state == UNDETERMINED, (
        "a malformed record was dropped rather than refused, so a log this "
        "module cannot fully read was reported as a clean history")
    assert "unparseable" in result.reason


def test_the_valid_half_of_that_log_really_would_have_parsed(clean_repo, monkeypatch):
    """Premise control for the test above.

    If the hand-built 'valid' record did not actually parse, the previous test
    would pass for the wrong reason -- refusing the record it was supposed to
    accept. This asserts the good half alone is read as one clean commit.
    """
    real_run = git_identity.subprocess.run
    good = _git("log", "--format=%H", cwd=clean_repo).strip()

    def only_valid(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if "log" in cmd:
            proc.stdout = f"{good}\x1f{ALLOWED}\x1f{ALLOWED}\x1fsubject\x1e"
        return proc

    monkeypatch.setattr(git_identity.subprocess, "run", only_valid)
    result = scan(str(clean_repo))
    assert result.state == CLEAN and result.examined == 1


def test_an_unrecognised_shallow_answer_is_undetermined(clean_repo, monkeypatch):
    """`--is-shallow-repository` answering anything but true/false is unknown."""
    real_run = git_identity.subprocess.run

    def mangle(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if "--is-shallow-repository" in cmd:
            proc.stdout = "maybe\n"
        return proc

    monkeypatch.setattr(git_identity.subprocess, "run", mangle)
    assert scan(str(clean_repo)).state == UNDETERMINED


def test_clean_is_unreachable_without_examining_a_commit(tmp_path):
    """The invariant behind every refusal above, asserted directly.

    Each case is measured rather than argued: whatever else happens, no input
    produces the pair (CLEAN, examined == 0).
    """
    empty = _repo(tmp_path / "e")
    plain = tmp_path / "p"
    plain.mkdir()
    shallow_src = _repo(tmp_path / "src")
    _commit(shallow_src, "c0")
    _commit(shallow_src, "c1", filename="b.txt")
    shallow = tmp_path / "sh"
    _git("clone", "--depth", "1", shallow_src.as_uri(), str(shallow), cwd=tmp_path)

    for repo in (empty, plain, shallow):
        result = scan(str(repo))
        assert not (result.state == CLEAN and result.examined == 0), repo
        assert result.state == UNDETERMINED, repo


def test_the_default_scope_reaches_refs_outside_branches_and_tags(tmp_path):
    """`--all` is load-bearing, and nothing else in this file pinned it.

    A reviewer narrowed the default from `--all` to `--branches --tags` and
    the whole suite still passed. That mutation is not theoretical: it is
    exactly the blind spot that made the previous incident unfixable, because
    `refs/pull/*` retained the purged address after every branch had been
    rewritten. A commit reachable only from such a ref is published, and the
    scan must reach it.
    """
    repo = _repo(tmp_path / "r")
    _commit(repo, "clean")
    _git("checkout", "-q", "-b", "tmp", cwd=repo)
    _commit(repo, "leak", email=CORPORATE, filename="bad.txt")
    hidden = _git("rev-parse", "HEAD", cwd=repo).strip()
    _git("update-ref", "refs/pull/1/head", hidden, cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    _git("branch", "-q", "-D", "tmp", cwd=repo)

    # Premise: the commit really is outside branches and tags now, so this
    # measures the scope rather than a lucky reachability.
    assert hidden not in _git("log", "--branches", "--tags", "--format=%H",
                              cwd=repo)
    assert hidden in _git("log", "--all", "--format=%H", cwd=repo)

    result = scan(str(repo))
    assert result.state == VIOLATIONS, (
        "a commit reachable only from refs/pull/* was not examined; the "
        "default scope no longer covers every published ref")


def test_a_commit_git_cannot_decode_is_clean_not_a_violation(tmp_path):
    """Measured, not imagined: this crashed before the encoding was named.

    `text=True` decodes with the locale's preferred encoding, which is cp1252
    on Windows. A commit message byte that no codepage covers raised
    UnicodeDecodeError inside subprocess's reader thread, left stdout as None
    and surfaced as an uncaught AttributeError -- so Python exited 1, which
    this program defines as VIOLATIONS. An unreadable log reported a bad
    identity.

    The outcome asserted here is CLEAN, not UNDETERMINED, and the name says so
    -- an earlier name claimed UNDETERMINED while the body asserted CLEAN. The
    reason it is CLEAN is that `errors="replace"` confines the damage to the
    bytes that are actually undecodable: the message becomes a replacement
    character, the *addresses* are still read exactly, and a commit whose
    identities are all allowed is clean no matter what its subject line
    contains. Refusing here would be its own defect -- an unreadable byte in
    prose is not evidence about an identity.
    """
    repo = _repo(tmp_path / "r")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    message = tmp_path / "msg.bin"
    # The undecodable byte goes in a TRAILER VALUE, not the subject. When this
    # module read `%B` the subject was enough; asking git for `%(trailers:...)`
    # means the subject never reaches the decoder, and the mutation harness
    # caught that this test had stopped exercising the encoding at all -- it
    # passed with and without the fix. The byte has to land in a field the
    # format actually emits or the test is measuring nothing.
    message.write_bytes(
        b"subject \x81 \xc3\xa9\n\nCo-authored-by: N\x81me <darinh@gmail.com>\n")
    # Not via `_git`: that helper decodes with the locale too, so it would
    # raise here and the test would fail before reaching the thing it tests.
    subprocess.run(
        ["git", "-c", f"user.email={ALLOWED}", "-c", "user.name=T",
         "commit", "-q", "-F", str(message)],
        cwd=str(repo), check=True, capture_output=True)

    result = scan(str(repo))  # must not raise
    assert result.state != VIOLATIONS, (
        "an undecodable byte was reported as a disallowed identity")
    assert result.state == CLEAN
    assert result.examined == 1


@pytest.mark.parametrize("mangled", [
    "darinh@gmail.co\ufffd",
    "\ufffd" + "darinh@gmail.com",
    "darinh@gmail.com\ufffd",
    "\ufffd@users.noreply.github.com\ufffd",
])
def test_an_address_damaged_by_the_replacement_codec_is_not_allowed(mangled):
    """The other direction of `errors="replace"`, which must fail closed.

    Replacing undecodable bytes keeps the scan running over a log it can
    mostly read, but it also means an address can arrive damaged. A damaged
    address must not resemble an allowed one: the substitution has to be able
    to turn an allowed address into an unrecognised one, never the reverse.
    """
    assert not is_allowed(mangled)


# ---------------------------------------------------------------------------
# The command line. Assert the message, not only the code: a refusal code is
# the most multiplexed value this program returns.
# ---------------------------------------------------------------------------

def test_main_exits_zero_and_names_the_population_when_clean(clean_repo, capsys):
    code = git_identity.main(["--repo", str(clean_repo)])
    out = capsys.readouterr().out
    assert code == CLEAN == 0
    assert "CLEAN" in out
    assert "1 commit" in out, (
        "a clean verdict is only meaningful beside the size of what was read")


def test_main_exits_one_and_names_the_offender(tmp_path, capsys):
    repo = _repo(tmp_path / "r")
    _commit(repo, "leak", email=CORPORATE)
    code = git_identity.main(["--repo", str(repo)])
    out = capsys.readouterr().out
    assert code == VIOLATIONS == 1
    assert "VIOLATIONS" in out
    assert CORPORATE in out, "the report names the address, not just a count"


def test_main_exits_two_and_refuses_rather_than_certifying(tmp_path, capsys):
    """Exit 2 is not a softer 0. The message has to say so out loud."""
    plain = tmp_path / "plain"
    plain.mkdir()
    code = git_identity.main(["--repo", str(plain)])
    out = capsys.readouterr().out
    assert code == UNDETERMINED == 2
    assert "UNDETERMINED" in out
    assert "Refusing" in out
    assert "CLEAN" not in out


def test_the_three_states_are_distinct_exit_codes():
    """Collapsing any two of these is the bug class this whole file guards."""
    assert len({CLEAN, VIOLATIONS, UNDETERMINED}) == 3
    assert CLEAN == 0, "only a genuine pass may be a zero exit"


def test_report_of_a_clean_result_never_says_undetermined(capsys):
    git_identity.report(Result(CLEAN, 7, (), ""), ("--all",))
    out = capsys.readouterr().out
    assert "CLEAN" in out and "7 commit" in out
    assert "UNDETERMINED" not in out


# ---------------------------------------------------------------------------
# This repository.
# ---------------------------------------------------------------------------

def test_this_repository_has_no_disallowed_identities():
    """The real history, when the checkout is deep enough to have one.

    A shallow checkout is tolerated here and *only* here, because the matrix
    jobs clone at depth 1 and this assertion is not their job -- the dedicated
    `git-identity` workflow job clones at full depth and fails on exit 2. Any
    other UNDETERMINED reason (git missing, an unparseable log) is a real
    problem and is not excused.
    """
    root = REPO_ROOT
    result = scan(str(root))
    if result.state == UNDETERMINED:
        assert "shallow" in result.reason, (
            f"undetermined for a reason other than depth: {result.reason}")
        pytest.skip("shallow checkout; the git-identity CI job covers this")
    assert result.state == CLEAN, [o.describe() for o in result.offenders]
    assert result.examined > 0


# ---------------------------------------------------------------------------
# The pipeline. A gate nobody runs is documentation, and a gate that runs
# against one commit is worse -- it is documentation that reports success.
# ---------------------------------------------------------------------------

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _workflows() -> dict:
    """Every workflow file, parsed. Keyed by filename.

    Discovery is shared rather than spelled here: GitHub loads both ``.yml``
    and ``.yaml``, and this function used to glob only the first, so a
    workflow with the other suffix would have been excluded from every
    assertion below while they all kept passing.
    """
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in workflow_paths()}


def _jobs_running(workflow: dict, needle: str) -> list[str]:
    """Names of jobs having a step whose ``run:`` mentions `needle`.

    Parsed, not grepped. A substring search over the raw file is satisfied by
    the string appearing in a comment -- and this change deliberately writes
    the script's name into workflow comments -- so a grep would keep passing
    after the step itself was deleted.
    """
    jobs = (workflow or {}).get("jobs") or {}
    return [name for name, job in jobs.items()
            if any(needle in str((step or {}).get("run") or "")
                   for step in ((job or {}).get("steps") or []))]


def _checkout_fetch_depth(job: dict) -> tuple[bool, object]:
    """``(checks_out, depth)`` for a job's ``actions/checkout`` step.

    ``depth`` is ``None`` when the step names none, which is not "unknown" --
    it is git's default of 1, a shallow clone. Keeping that distinct from an
    explicit 0 is the entire point of the assertion below.
    """
    for step in (job or {}).get("steps") or []:
        if str((step or {}).get("uses") or "").startswith("actions/checkout"):
            with_block = (step or {}).get("with") or {}
            if "fetch-depth" not in with_block:
                return True, None
            return True, int(with_block["fetch-depth"])
    return False, None


def _scanning_jobs() -> list[tuple[str, str, dict]]:
    """``(workflow_file, job_name, job)`` for every job running the scan."""
    out = []
    for filename, workflow in _workflows().items():
        for name in _jobs_running(workflow, "git_identity.py"):
            out.append((filename, name, workflow["jobs"][name]))
    return out


def test_some_workflow_runs_the_commit_identity_scan():
    """Without this the module is a script nobody executes."""
    assert _scanning_jobs(), (
        "no workflow job runs git_identity.py; the check is not enforced")


def test_every_job_running_the_scan_checks_out_the_whole_history():
    """The line that makes the job mean anything.

    At the default depth the scan would see one commit. It refuses rather than
    certifying, so the job would go red rather than silent -- but the depth is
    pinned here where the reason can be written down beside it.
    """
    for filename, name, job in _scanning_jobs():
        checks_out, depth = _checkout_fetch_depth(job)
        assert checks_out, f"{filename}:{name} scans history without checking it out"
        assert depth == 0, (
            f"{filename}:{name} checks out at fetch-depth {depth!r}; the "
            "identity scan needs 0 (full history) or it can only see the tip")


def test_the_identity_scan_is_not_restricted_to_the_main_branch():
    """A commit is published the moment it is pushed to any branch.

    ci.yml restricts its push trigger to `main` -- deliberately, and another
    test asserts it. Inheriting that restriction here would leave every other
    branch unwatched, so the scan lives in a workflow whose push trigger names
    no branches at all.
    """
    for filename, name, _job in _scanning_jobs():
        triggers = _workflows()[filename].get(True) or _workflows()[filename].get("on")
        assert triggers is not None, f"{filename}: no trigger block found"
        assert "push" in triggers, f"{filename}: the scan does not run on push"
        push = triggers["push"] or {}
        assert not (push or {}).get("branches"), (
            f"{filename}:{name} only runs for branches {push.get('branches')!r}; "
            "a push to any other branch publishes without being checked")


def test_the_ci_matrix_jobs_are_the_reason_the_scan_is_separate():
    """A premise assertion, so the rationale cannot quietly stop being true.

    The workflow comments claim the ci.yml jobs are shallow. If that ever
    stops being so, the argument for a separate full-depth workflow is worth
    revisiting deliberately, rather than discovering the comment has been
    wrong for months.
    """
    ci = _workflows()["ci.yml"]
    depths = {n: _checkout_fetch_depth(j)[1] for n, j in ci["jobs"].items()}
    assert depths, "premise: ci.yml defines jobs"
    assert all(d is None for d in depths.values()), (
        f"a ci.yml job now sets fetch-depth: {depths}. Not a failure of the "
        "scan -- revisit whether it still needs its own workflow.")


def test_the_scan_workflow_fetches_the_pull_request_refs():
    """refs/pull/* is what made the previous incident unfixable.

    actions/checkout fetches refs/heads/* and refs/tags/* even at depth 0, so
    an address living only in a pull request is published while `git log
    --all` walks straight past it. This asserts the fetch that closes that gap
    is still there, and that it is not neutered with `|| true`.
    """
    for filename, _name, job in _scanning_jobs():
        runs = " ".join(str((s or {}).get("run") or "")
                        for s in job.get("steps") or [])
        assert "refs/pull/" in runs, (
            f"{filename}: the scan job does not fetch refs/pull/*, so a "
            "commit living only in a pull request is invisible to it")
        assert "|| true" not in runs, (
            f"{filename}: a step is allowed to fail silently, which lets the "
            "scan examine a narrower population than it reports")


def test_the_workflow_parser_notices_a_job_that_stopped_running_the_scan():
    """Positive control. A helper that matches nothing certifies everything."""
    without = yaml.safe_load(
        "jobs:\n"
        "  test:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: python -m pytest\n")
    assert _jobs_running(without, "git_identity.py") == []


def test_the_workflow_parser_reads_a_default_depth_as_shallow():
    """Positive control for the depth rule, on both spellings of the step."""
    unset = yaml.safe_load(
        "jobs:\n"
        "  x:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - run: python3 git_identity.py\n")
    assert _checkout_fetch_depth(unset["jobs"]["x"]) == (True, None)

    explicit_one = yaml.safe_load(
        "jobs:\n"
        "  x:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with: {fetch-depth: 1}\n")
    assert _checkout_fetch_depth(explicit_one["jobs"]["x"]) == (True, 1)


def test_the_workflow_parser_accepts_a_full_checkout():
    """Negative control: the correct spelling is not reported as a fault."""
    full = yaml.safe_load(
        "jobs:\n"
        "  x:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          fetch-depth: 0\n")
    assert _checkout_fetch_depth(full["jobs"]["x"]) == (True, 0)


def test_a_grep_would_pass_on_the_real_workflows_where_the_parse_does_not():
    """The reason these checks parse rather than search, measured on the tree.

    An earlier version of this test fed a commented YAML string to
    ``_jobs_running`` and asserted it found nothing -- which was vacuous,
    because ``yaml.safe_load`` strips comments before the helper ever sees
    them. It proved a property of PyYAML, not of the check.

    This one is not vacuous: it asserts against the actual workflow files that
    the naive spelling of this check would pass on a file where the *step* had
    been deleted, because the script's name also appears in prose there.
    """
    mentions = {p.name for p in workflow_paths()
                if "git_identity.py" in p.read_text(encoding="utf-8")}
    running = {filename for filename, _n, _j in _scanning_jobs()}
    assert running, "premise: some workflow really does run the scan"
    assert mentions >= running

    # The load-bearing half: strip every `run:` line from the real workflow
    # and the grep still passes, because the comments survive.
    for filename in running:
        text = (WORKFLOW_DIR / filename).read_text(encoding="utf-8")
        stripped = "\n".join(line for line in text.splitlines()
                             if "run:" not in line)
        assert "git_identity.py" in stripped, (
            f"{filename}: this test's premise expired -- the script name no "
            "longer appears outside a run: line, so a grep would no longer "
            "be fooled")
        assert _jobs_running(yaml.safe_load(stripped), "git_identity.py") == [], (
            f"{filename}: the parsed check still reports the scan as running "
            "after every run: line was removed")
