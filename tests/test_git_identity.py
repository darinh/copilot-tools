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

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

ALLOWED = "darinh@gmail.com"
CORPORATE = "dahoove@microsoft.com"


def _git(*args, cwd) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True)
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
    "darinh@gmail.com.attacker.net",  # suffix rules must anchor at the end
    "users.noreply.github.com",       # the suffix without the @
    "evil@notusers.noreply.github.com.evil.net",
])
def test_disallowed_identities_are_rejected(email):
    assert not is_allowed(email), f"{email!r} must not be treated as allowed"


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

    If `git log` ever answers in a shape this module does not recognise, the
    honest reading is "I do not know", not "I found no bad addresses".
    """
    real_run = git_identity.subprocess.run

    def mangle(cmd, **kwargs):
        proc = real_run(cmd, **kwargs)
        if "log" in cmd:
            proc.stdout = "not\x1fthe\x1eshape\x1fasked\x1efor\x1e"
        return proc

    monkeypatch.setattr(git_identity.subprocess, "run", mangle)
    result = scan(str(clean_repo))
    assert result.state == UNDETERMINED
    assert "unparseable" in result.reason


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

def _jobs_running(workflow: dict, needle: str) -> list[str]:
    """Names of jobs having a step whose ``run:`` mentions `needle`.

    Parsed, not grepped. A substring search over the raw file is satisfied by
    the string appearing in a comment -- including the explanatory comments
    this very change adds to ci.yml -- so it would keep passing after the step
    itself was deleted.
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


def test_ci_runs_the_commit_identity_scan():
    """Without this job the module is a script nobody executes."""
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert _jobs_running(workflow, "git_identity.py"), (
        "no ci.yml job runs git_identity.py; the check is not enforced")


def test_the_identity_job_checks_out_the_whole_history():
    """The line that makes the job mean anything.

    At the default depth the scan would see one commit. It refuses rather than
    certifying, so the job would go red rather than silent -- but red-on-every-
    push is its own pressure to weaken the check, so the depth is pinned here
    where the reason can be written down next to it.
    """
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    for name in _jobs_running(workflow, "git_identity.py"):
        checks_out, depth = _checkout_fetch_depth(workflow["jobs"][name])
        assert checks_out, f"job {name!r} scans history without checking it out"
        assert depth == 0, (
            f"job {name!r} checks out at fetch-depth {depth!r}; the identity "
            "scan needs 0 (full history) or it can only see the tip")


def test_the_matrix_jobs_are_the_reason_this_job_is_separate():
    """A premise assertion, so the rationale cannot quietly stop being true.

    The comment in ci.yml claims the other jobs are shallow. If that ever
    stops being so, the argument for a separate job is worth revisiting --
    deliberately, rather than discovering the comment has been wrong for
    months.
    """
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    scanning = set(_jobs_running(workflow, "git_identity.py"))
    others = [n for n in workflow["jobs"] if n not in scanning]
    assert others, "premise: ci.yml has jobs besides the identity scan"
    depths = {n: _checkout_fetch_depth(workflow["jobs"][n])[1] for n in others}
    assert all(d is None for d in depths.values()), (
        f"a non-identity job now sets fetch-depth: {depths}. Not a failure of "
        "the scan -- revisit whether it still needs its own job.")


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


def test_the_workflow_parser_is_not_satisfied_by_a_comment():
    """The defect this repository found in its own ci.yml check the same week.

    A raw substring search passes on a workflow that only *mentions* the
    script -- and this change deliberately writes the script's name into a
    ci.yml comment, so the grep spelling of this test would pass with the step
    deleted.
    """
    mentioned_only = yaml.safe_load(
        "jobs:\n"
        "  x:\n"
        "    # runs git_identity.py, honestly\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n")
    assert _jobs_running(mentioned_only, "git_identity.py") == []
