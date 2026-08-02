"""Every shell test suite in ``tests/`` must be executed by CI, not just parsed.

``tests/test-todo-claims.sh`` was committed with the parallel-agent feature and
then run by nothing at all. It was not invisible: the ``shellcheck`` job's
``bash -n`` loop parsed it on every push, and it passed, every time. A parse is
not an execution. Its assertions -- the ones that prove two agents racing for
the same todo end up with one claim between them -- had never run in CI once.

Nothing about that looks like a gap from the outside. The file is in ``tests/``,
its name says test, the pipeline is green, and the docstring of
``tests/test_instructions_template.py::test_the_todo_claims_sql_runs_and_claims``
names it as the half of a stated division of labour that covers *the protocol's
semantics*. Both halves read as covered while one of them never ran.

So the rule here is not "run this one script". A rule enforced against one file
is that file's history, as the repository already learned when a bash 3.2
tripwire that read only ``operator.sh`` let ``handoff.sh`` keep an associative
array through the change that removed operator.sh's. The rule is that *every*
tracked ``tests/*.sh`` is executed by some workflow job, so the next one added
cannot repeat this by being added quietly.

A shell file in ``tests/`` that is a helper rather than a suite has nowhere to
hide here, and that is deliberate: it either gets a run step or it does not
belong in ``tests/``. Being unsure which one a file is has been the expensive
state, not being wrong about it.

**What counts as "executed" is deliberately narrow**, because the two ways of
being wrong here are not equally bad. Certifying a suite that never runs
reproduces the original defect exactly, in green; refusing to certify one that
does run fails loudly and names the file. So where the two are in tension this
refuses, and three specific ways a mention could be mistaken for an execution
are closed:

* a step or job carrying an ``if:`` may never run at all -- ``if: false`` is
  the limit case, but ``if: runner.os == 'Linux'`` is in this very workflow;
* ``echo tests/foo.sh`` mentions a path without running it, and a ``#`` comment
  *inside* a run block is shell text, not YAML the parser already stripped;
* a run block is matched per command, not per line, so ``echo x && bash
  tests/foo.sh`` still counts -- skipping the whole line because it starts
  with ``echo`` would buy the false positive back as a false negative.

``working-directory`` is honoured for the same reason: a step that cd's into
``tests/`` and runs ``bash foo.sh`` is a real execution, and reporting it as
missing would train the next reader to disbelieve this file.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

# Imported unconditionally rather than through `pytest.importorskip`. PyYAML is
# declared in the `dev` extra, and turning its absence into a skip would delete
# these assertions silently -- the same shape of failure this file exists to
# catch, since a suite that does not run reports nothing rather than red.
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Commands that take a path as an argument without executing it.
NON_EXECUTING = {"echo", "printf", ":", "true", "false", "cat"}

#: Shell operators that separate one command from the next within a line.
_SEPARATORS = re.compile(r"&&|\|\||;|\|")


def _tracked_shell_tests() -> list[str]:
    """Repo-relative paths of every tracked ``*.sh`` under ``tests/``.

    ``git ls-files`` rather than a filesystem glob, so an untracked scratch
    script someone left in ``tests/`` cannot fail the build, and so a suite
    only comes under the rule once it is committed -- which is exactly when it
    starts claiming to be covered.
    """
    out = subprocess.run(
        ["git", "ls-files", "--", "tests"],
        cwd=str(REPO_ROOT), check=True, capture_output=True,
        encoding="utf-8", errors="replace").stdout
    return sorted(line for line in out.splitlines() if line.endswith(".sh"))


def _workflows() -> dict:
    """Every workflow file, parsed. Keyed by filename.

    Both extensions: GitHub accepts ``.yaml`` as readily as ``.yml``, and a
    glob for one of them would report a suite as unrun because the job that
    runs it was spelled the other way.
    """
    paths = sorted(list(WORKFLOW_DIR.glob("*.yml"))
                   + list(WORKFLOW_DIR.glob("*.yaml")))
    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
            for p in paths}


def _run_steps(workflow: dict) -> list[dict]:
    """Every step in a workflow that runs a shell, with what qualifies it.

    Parsed, not grepped. This change deliberately writes
    ``tests/test-todo-claims.sh`` into a *comment* in ci.yml explaining why the
    step exists, and PyYAML drops comments -- so deleting the step while
    leaving the comment behind fails here, where a substring search over the
    raw file would go on passing and cite the comment as its evidence.

    ``conditional`` is true when the step *or its job* carries an ``if:``.
    Which way such an expression evaluates is a question about the runner, not
    about this file, so it is not guessed at: a conditional step does not
    count as the thing keeping a suite alive.
    """
    steps = []
    for name, job in ((workflow or {}).get("jobs") or {}).items():
        job_conditional = (job or {}).get("if") is not None
        for step in (job or {}).get("steps") or []:
            run = (step or {}).get("run")
            if not run:
                continue
            steps.append({
                "job": name,
                "run": str(run),
                "working-directory": (step or {}).get("working-directory"),
                "conditional": (job_conditional
                                or (step or {}).get("if") is not None),
            })
    return steps


def _candidate_spellings(path: str, working_directory) -> set[str]:
    """How `path` may legitimately be written from a step's directory.

    ``working-directory`` is a *prefix* to remove, so it is removed as one.
    ``str.lstrip`` would not: it takes a character set, and turns ``../tests``
    into ``tests`` and ``.github`` into ``github``. The first of those is the
    dangerous direction -- a step running from a sibling checkout would have
    its ``bash test-todo-claims.sh`` credited against ``tests/`` here, and the
    guard would certify an execution that never happens.

    A prefix that escapes the repository (``..``) or is absolute cannot be
    resolved against a ``git ls-files`` path at all, so it yields no relative
    spellings: the step is then only credited if it names the path in full.
    Refusing to certify is the failure this file is allowed to have.
    """
    spellings = {path, f"./{path}"}
    if working_directory:
        prefix = str(working_directory).strip().rstrip("/")
        if prefix.startswith("./"):
            prefix = prefix[2:]
        escapes = (prefix.startswith("/")
                   or ".." in PurePosixPath(prefix).parts)
        if prefix and not escapes and path.startswith(f"{prefix}/"):
            relative = path[len(prefix) + 1:]
            spellings |= {relative, f"./{relative}"}
    return spellings


def _run_block_executes(run: str, spellings: set[str]) -> bool:
    """True when a run block *invokes* one of `spellings`, not merely names it.

    Split per command rather than per line, so ``echo x && bash tests/f.sh``
    counts. A leading ``#`` is a shell comment inside the block; YAML dropped
    the workflow's own comments long before this saw it.
    """
    for line in run.splitlines():
        for segment in _SEPARATORS.split(line):
            segment = segment.strip()
            if not segment or segment.startswith("#"):
                continue
            tokens = segment.split()
            if tokens[0] in NON_EXECUTING:
                continue
            if any(spelling in segment for spelling in spellings):
                return True
    return False


def _jobs_executing(path: str) -> list[str]:
    """``workflow.yml:job`` for every job whose ``run:`` invokes `path`."""
    found = []
    for filename, workflow in _workflows().items():
        for step in _run_steps(workflow):
            if step["conditional"]:
                continue
            if _run_block_executes(
                    step["run"],
                    _candidate_spellings(path, step["working-directory"])):
                found.append(f"{filename}:{step['job']}")
    return found


# ---------------------------------------------------------------------------
# The rule.
# ---------------------------------------------------------------------------

def test_there_are_shell_test_suites_to_check():
    """Premise. An empty population passes every assertion below.

    If ``git ls-files`` ever returns nothing here -- a pathspec typo, a run
    from the wrong directory, a checkout without git -- the parametrised test
    collects zero cases and the file reports success by saying nothing. That
    is the failure mode of the thing being guarded, so it is pinned.
    """
    assert _tracked_shell_tests(), (
        "no tracked *.sh under tests/ was found; this file would then be "
        "certifying an empty population")


def test_the_workflow_directory_was_actually_read():
    """Premise. `_workflows()` returning {} would make every path 'unrun'.

    The two premises fail in opposite directions -- an empty script list
    passes everything, an empty workflow list fails everything -- and pinning
    only one of them leaves a way for this file to be wrong quietly.
    """
    workflows = _workflows()
    assert workflows, f"no workflow files parsed from {WORKFLOW_DIR}"
    assert any(_run_steps(w) for w in workflows.values()), (
        "no workflow contains a single `run:` step; the parser is not "
        "reading what it thinks it is")


@pytest.mark.parametrize("script", _tracked_shell_tests())
def test_every_shell_test_suite_is_executed_by_ci(script):
    """The assertion that would have caught test-todo-claims.sh.

    ``bash -n`` over every ``*.sh`` does not count and must not be allowed to:
    it is a syntax check, it passed on this file for the whole time the suite
    was dead, and treating a parse as coverage is what made the gap invisible.
    """
    jobs = _jobs_executing(script)
    assert jobs, (
        f"{script} is a committed shell test suite that no workflow job "
        f"executes unconditionally. CI parses it with `bash -n` and never "
        f"runs it, so its assertions cannot fail no matter what breaks. Add "
        f"a `run:` step without an `if:`, or move the file out of tests/ if "
        f"it is not a suite.")


# ---------------------------------------------------------------------------
# Controls. A checker that matches nothing certifies everything.
# ---------------------------------------------------------------------------

def _synthetic(*step_lines: str) -> dict:
    body = "".join(f"      {line}\n" for line in step_lines)
    return yaml.safe_load("jobs:\n  shellcheck:\n    steps:\n" + body)


def test_the_checker_notices_a_suite_with_no_run_step():
    """Positive control, on a synthetic workflow rather than the real one."""
    workflow = _synthetic("- run: bash tests/test_setup_sh.sh")
    runs = [s["run"] for s in _run_steps(workflow)]
    assert any("tests/test_setup_sh.sh" in r for r in runs)
    assert not any("tests/test-todo-claims.sh" in r for r in runs), (
        "the step scan claims to find a script the synthetic workflow never "
        "runs; it would certify anything")


def test_a_script_named_only_in_a_comment_does_not_count_as_executed():
    """The trap this file walks straight into, pinned so it stays shut.

    ci.yml explains the todo-claim step in a comment that names the script.
    A grep-based checker is satisfied by that comment alone, so deleting the
    step would leave the guard green and pointing at prose as proof.
    """
    workflow = _synthetic(
        "# tests/test-todo-claims.sh used to run here",
        "- run: echo unrelated",
    )
    runs = [s["run"] for s in _run_steps(workflow)]
    assert runs, "premise: the synthetic workflow has a run step"
    assert not any("tests/test-todo-claims.sh" in r for r in runs), (
        "a script named only in a YAML comment was counted as executed")


def test_a_step_without_run_is_not_mistaken_for_one():
    """``uses:`` steps have no ``run:``. Reading them as empty strings would be
    harmless; crashing on them would not, and an early version did."""
    workflow = _synthetic(
        "- uses: actions/checkout@v4",
        "- run: bash tests/test-todo-claims.sh",
    )
    assert [s["job"] for s in _run_steps(workflow)] == ["shellcheck"]


# -- the ways a mention could be mistaken for an execution -------------------

def test_a_step_that_only_echoes_the_path_is_not_an_execution():
    """False-positive control. ``echo`` takes the path without running it."""
    assert not _run_block_executes(
        "echo tests/test-todo-claims.sh", {"tests/test-todo-claims.sh"})


def test_a_shell_comment_inside_a_run_block_is_not_an_execution():
    """YAML strips workflow comments; it does not strip shell comments."""
    assert not _run_block_executes(
        "# bash tests/test-todo-claims.sh\ntrue",
        {"tests/test-todo-claims.sh"})


def test_a_real_invocation_after_an_echo_still_counts():
    """Negative control for the two above.

    Skipping a whole line because its first token is ``echo`` would buy the
    false positive back as a false negative, and a guard that reports a live
    suite as dead is one nobody will leave switched on.
    """
    assert _run_block_executes(
        "echo running && bash tests/test-todo-claims.sh",
        {"tests/test-todo-claims.sh"})


def test_a_plain_invocation_counts():
    """The ordinary case, so the controls above cannot pass by the matcher
    having been broken into matching nothing at all."""
    assert _run_block_executes(
        "bash tests/test-todo-claims.sh", {"tests/test-todo-claims.sh"})


def test_a_conditional_step_does_not_keep_a_suite_alive():
    """``if: false`` runs never, and so may an ``if:`` this cannot evaluate.

    ci.yml already carries ``if: runner.os == 'Linux'`` on three steps, so
    this is not a hypothetical shape.
    """
    workflow = _synthetic(
        "- run: bash tests/test-todo-claims.sh",
        "  if: false",
    )
    steps = _run_steps(workflow)
    assert len(steps) == 1, "premise: the synthetic step was parsed"
    assert steps[0]["conditional"], "an `if:` on the step was not noticed"


def test_a_condition_on_the_job_disqualifies_its_steps_too():
    """A job-level ``if:`` skips every step inside it."""
    workflow = yaml.safe_load(
        "jobs:\n"
        "  shellcheck:\n"
        "    if: github.event_name == 'schedule'\n"
        "    steps:\n"
        "      - run: bash tests/test-todo-claims.sh\n"
    )
    assert _run_steps(workflow)[0]["conditional"], (
        "a job-level `if:` was not inherited by its steps")


def test_an_unconditional_step_is_not_reported_as_conditional():
    """Negative control: the ``if:`` check must not disqualify everything."""
    workflow = _synthetic("- run: bash tests/test-todo-claims.sh")
    assert not _run_steps(workflow)[0]["conditional"]


def test_a_conditional_step_is_not_counted_as_executing():
    """The `if:` rule, exercised end to end rather than on the flag alone.

    Asserting only that ``conditional`` is set would pass even if
    ``_jobs_executing`` never consulted it -- which is where the decision is
    actually made.
    """
    workflow = _synthetic(
        "- run: bash tests/test-todo-claims.sh",
        "  if: false",
    )
    live = [s for s in _run_steps(workflow) if not s["conditional"]]
    assert not live, "a conditional step survived the filter _jobs_executing uses"


def test_working_directory_is_honoured():
    """A step that cd's into tests/ and runs ``bash foo.sh`` is an execution."""
    spellings = _candidate_spellings("tests/test-todo-claims.sh", "tests")
    assert _run_block_executes("bash test-todo-claims.sh", spellings)


def test_working_directory_does_not_invent_a_match():
    """Negative control: the relative spelling is only added for the
    directory the script is actually in."""
    spellings = _candidate_spellings("tests/test-todo-claims.sh", "extensions")
    assert not _run_block_executes("bash test-todo-claims.sh", spellings)


def test_working_directory_is_a_prefix_not_a_character_set():
    """``../tests`` must not be credited as ``tests``.

    ``lstrip("./")`` -- the obvious spelling, and the one this had first --
    strips *characters*, so it turns ``../tests`` into ``tests`` and would
    credit a step running ``bash test-todo-claims.sh`` from a sibling checkout
    against this repository's ``tests/``. That is the false-green direction:
    a suite reported as executed by a step that never touches it.
    """
    for escaping in ("../tests", "../../tests", "/tests", "a/../tests"):
        spellings = _candidate_spellings("tests/test-todo-claims.sh", escaping)
        assert not _run_block_executes("bash test-todo-claims.sh", spellings), (
            f"working-directory {escaping!r} was resolved to tests/")

    # The other half: a legitimate dot-prefixed directory keeps its dot, and
    # the ordinary spellings still resolve.
    assert _candidate_spellings("x/f.sh", ".github") == {"x/f.sh", "./x/f.sh"}
    for spelling in ("tests", "./tests", "tests/"):
        spellings = _candidate_spellings("tests/test-todo-claims.sh", spelling)
        assert _run_block_executes("bash test-todo-claims.sh", spellings), (
            f"working-directory {spelling!r} stopped resolving")


def test_both_workflow_extensions_are_read():
    """``.yaml`` is as valid as ``.yml``; globbing one would miss a real job."""
    found = {p.name for p in WORKFLOW_DIR.iterdir()
             if p.suffix in {".yml", ".yaml"}}
    assert found, "premise: the workflow directory has workflow files"
    assert set(_workflows()) == found, (
        f"_workflows() read {set(_workflows())} but the directory holds "
        f"{found}; a workflow spelled with the other extension is invisible "
        f"to the rule")


def test_the_real_workflows_run_both_shell_suites_today():
    """The concrete claim, spelled out so a regression names the file.

    The parametrised test above is the rule; this is the observation that made
    it worth writing, and it fails with the script's name rather than with a
    parameter id if either step is dropped.
    """
    for script in ("tests/test_setup_sh.sh", "tests/test-todo-claims.sh"):
        assert _jobs_executing(script), f"no workflow job runs {script}"
