"""GitHub reads both ``*.yml`` and ``*.yaml``; a guard that reads one is half a guard.

Several tests here assert properties over "every workflow" -- that the commit
identity scan runs somewhere, that every job running it checks out full
history, that the extension tests are executed by CI. Each of them enumerated
``.github/workflows`` with ``glob("*.yml")``.

GitHub Actions accepts either suffix. So the day someone adds
``.github/workflows/release.yaml``, those assertions keep passing and simply
stop covering it -- and the ones written as a ``for`` loop over the discovered
jobs do not merely miss it, they iterate zero extra times and report success.
That is the same shape as the defect this repository has now hit twice: a rule
enforced against one spelling is not a rule, it is that spelling's history.

The blind spot was real but narrow, because a *total* miss is loud:
``test_some_workflow_runs_the_commit_identity_scan`` asserts the population is
non-empty, so renaming every workflow to ``.yaml`` would go red. What passes
silently is the mixed case -- one ``.yml`` that satisfies the population guard
plus one ``.yaml`` that nothing ever looks at.

This module owns the discovery (:func:`workflow_paths`) and enforces it two
ways, because either alone is weak:

* a **behavioural** test -- point the helper at a directory holding one file of
  each suffix and assert it returns both. A static rule cannot catch the helper
  itself regressing, since the helper's receiver is a local variable and says
  nothing about workflows.
* a **static scan** over every first-party ``*.py``, failing any *other* file
  that enumerates the workflow directory with a pattern matching only one of
  the two suffixes. That is what stops the next guard being written with the
  same blind spot.
"""
from __future__ import annotations

import ast
import fnmatch
from pathlib import Path

import pytest

from test_subprocess_encoding_conformance import _python_sources

REPO = Path(__file__).resolve().parent.parent

WORKFLOW_DIR = REPO / ".github" / "workflows"

#: The two suffixes GitHub Actions will load from ``.github/workflows``.
WORKFLOW_SUFFIXES = ("*.yml", "*.yaml")

#: Names used only to classify a glob pattern, never opened.
_PROBE_NAMES = ("probe.yml", "probe.yaml")


def workflow_paths(directory: Path | None = None) -> list[Path]:
    """Every workflow file in `directory`, both suffixes, sorted.

    The one place in this repository that answers "what are the workflows".
    Callers get a list rather than a generator so that a caller which consumes
    it twice cannot silently see nothing the second time.
    """
    root = WORKFLOW_DIR if directory is None else directory
    found: set[Path] = set()
    for pattern in WORKFLOW_SUFFIXES:
        found.update(root.glob(pattern))
    return sorted(found)


def _covers(pattern: str) -> set[str]:
    """Which of the two suffixes `pattern` would actually match."""
    return {name for name in _PROBE_NAMES if fnmatch.fnmatch(name, pattern)}


def _mentions_workflows(source_segment: str | None) -> bool:
    """True if an expression names the workflow directory in some spelling.

    Deliberately a substring test over the receiver's *source*, because the
    two real spellings look nothing alike as syntax trees --
    ``WORKFLOW_DIR.glob(...)`` and
    ``(REPO_ROOT / ".github" / "workflows").glob(...)`` -- while both contain
    the word. Over-matching here costs a false failure, which is loud and
    cheap; under-matching costs a guard nobody notices is gone.
    """
    return "workflow" in (source_segment or "").lower()


def single_suffix_workflow_globs(source: str, filename: str) -> list[str]:
    """Report every workflow-directory glob, if the file misses a suffix overall.

    The verdict is per *file*, not per call site. The correct inline spelling
    is two calls -- ``list(d.glob("*.yml")) + list(d.glob("*.yaml"))`` -- and
    each of those, judged alone, covers exactly one suffix. Reporting per call
    site would fail the one file in this repository that already got it right,
    and a rule that fires on correct code gets deleted rather than obeyed.

    So the patterns are collected first and their coverage unioned. A file
    whose workflow globs between them reach both suffixes is silent; one that
    cannot is reported at every site, because any of them may be the one to
    fix.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:                                # pragma: no cover
        pytest.fail(f"{filename}: could not parse: {exc}")

    sites: list[tuple[int, str, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("glob", "rglob"):
            continue
        if not _mentions_workflows(ast.get_source_segment(source, func.value)):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        pattern = node.args[0].value
        if not isinstance(pattern, str):
            continue
        sites.append((node.lineno, pattern, _covers(pattern)))

    if not sites:
        return []
    reached: set[str] = set()
    for _lineno, _pattern, covered in sites:
        reached |= covered
    if reached == set(_PROBE_NAMES):
        return []

    missing = sorted(set(_PROBE_NAMES) - reached)
    absent = ", ".join("." + name.split(".")[-1] for name in missing)
    return [
        f"{filename}:{lineno}: enumerates the workflow directory with "
        f"{pattern!r}, and nothing else in this file covers {absent}. GitHub "
        "loads both .yml and .yaml, so a workflow with that suffix would be "
        "silently excluded from the check. Use `workflow_paths()` from "
        "test_workflow_discovery_conformance."
        for lineno, pattern, _covered in sites
    ]


# ---------------------------------------------------------------------------
# The helper's own behaviour. A static rule cannot see this one: the receiver
# inside `workflow_paths` is a local variable, so the scan below would not
# object if the `*.yaml` pattern were deleted tomorrow.
# ---------------------------------------------------------------------------

def test_the_helper_finds_both_suffixes(tmp_path: Path):
    """The whole point, asserted against files rather than against the source."""
    (tmp_path / "a.yml").write_text("on: push\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("on: push\n", encoding="utf-8")
    assert [p.name for p in workflow_paths(tmp_path)] == ["a.yml", "b.yaml"]


def test_the_helper_ignores_other_files(tmp_path: Path):
    """Negative control: it is a workflow enumerator, not a directory listing."""
    (tmp_path / "notes.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / "config.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "keep.yml").write_text("on: push\n", encoding="utf-8")
    assert [p.name for p in workflow_paths(tmp_path)] == ["keep.yml"]


def test_the_helper_returns_a_reusable_sequence(tmp_path: Path):
    """A generator here would make a second consumer see an empty population."""
    (tmp_path / "a.yml").write_text("on: push\n", encoding="utf-8")
    paths = workflow_paths(tmp_path)
    assert list(paths) == list(paths) != []


def test_the_repository_really_has_workflows():
    """Population guard. Every assertion below is satisfied by an empty tree."""
    found = workflow_paths()
    assert found, f"no workflow files found under {WORKFLOW_DIR}"
    assert "ci.yml" in {p.name for p in found}


# ---------------------------------------------------------------------------
# The static scan, and controls for it in both directions. A detector that
# matches nothing reports the whole tree clean, which reads like success.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    'paths = WORKFLOW_DIR.glob("*.yml")',
    'paths = WORKFLOW_DIR.glob("*.yaml")',
    'paths = (REPO_ROOT / ".github" / "workflows").glob("*.yml")',
    'paths = list(WORKFLOW_DIR.rglob("*.yml"))',
    'mentions = {p.name for p in WORKFLOW_DIR.glob("*.yml") if p}',
    'paths = workflow_dir.glob("*.yml")',
])
def test_the_scan_reports_a_single_suffix_glob(source: str):
    """Positive control: each real spelling of the defect is detected."""
    assert single_suffix_workflow_globs(source, "probe.py")


@pytest.mark.parametrize("source", [
    # Both suffixes, the correct spelling.
    'paths = list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml"))',
    # Both suffixes reached from separate statements, which is why the verdict
    # is unioned over the file rather than decided at each call site.
    ('def a():\n    return WORKFLOW_DIR.glob("*.yml")\n\n\n'
     'def b():\n    return WORKFLOW_DIR.glob("*.yaml")\n'),
    # A pattern that covers both on its own.
    'paths = WORKFLOW_DIR.glob("*.y*ml")',
    'paths = WORKFLOW_DIR.glob("*")',
    # Not the workflow directory at all -- other single-suffix globs are fine.
    'paths = LOG_DIR.glob("*.yml")',
    'paths = (tmp_path / "cfg").glob("*.yml")',
    # Not a glob.
    'text = WORKFLOW_DIR.read_text("*.yml")',
    # A non-literal pattern cannot be classified, so it is not reported.
    'paths = WORKFLOW_DIR.glob(pattern)',
    # Nothing to see.
    'x = 1',
])
def test_the_scan_stays_quiet(source: str):
    """Negative control: the correct spellings are not reported as faults."""
    assert single_suffix_workflow_globs(source, "probe.py") == []


def test_the_scan_would_have_caught_the_defect_this_module_fixes():
    """The exact line that motivated this file, verbatim, must be reported.

    Keeps the controls honest against the real thing rather than a
    paraphrase of it: this is how ``test_git_identity.py`` read before the
    change, and it is what a reviewer would write again by habit.
    """
    historical = (
        'def _workflows() -> dict:\n'
        '    return {p.name: yaml.safe_load(p.read_text(encoding="utf-8"))\n'
        '            for p in sorted(WORKFLOW_DIR.glob("*.yml"))}\n'
    )
    reported = single_suffix_workflow_globs(historical, "test_git_identity.py")
    assert len(reported) == 1
    assert ".yaml" in reported[0]


def test_no_first_party_source_enumerates_workflows_by_one_suffix():
    """The live rule, over the whole tree rather than over one file."""
    sources = _python_sources()
    assert sources, "the python source discovery came back empty"
    offenders = []
    for path in sources:
        offenders += single_suffix_workflow_globs(
            path.read_text(encoding="utf-8", errors="replace"),
            str(path.relative_to(REPO)).replace("\\", "/"),
        )
    assert not offenders, "\n".join(offenders)
