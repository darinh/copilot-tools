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
import os
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


def _workflow_alias_names(tree: ast.AST, source: str) -> set[str]:
    """Names bound to the workflow directory under some other spelling.

    ``WF_DIR = REPO / ".github" / "workflows"`` followed by ``WF_DIR.glob(...)``
    is the same defect wearing a different variable name, and a receiver test
    that reads only the receiver cannot see it. So assignments are read first
    and their targets remembered.

    One level of indirection, deliberately, and no further: a fixed point over
    aliases-of-aliases buys very little against the way this code is actually
    written, and every additional inference is another thing that can widen
    the receiver set silently.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not _mentions_workflows(ast.get_source_segment(source, value)):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _enclosing_statement(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    """The nearest ``ast.stmt`` containing `node`."""
    current = parents.get(id(node))
    while current is not None and not isinstance(current, ast.stmt):
        current = parents.get(id(current))
    return current


def single_suffix_workflow_globs(source: str, filename: str) -> list[str]:
    """Report workflow-directory globs that cannot reach both suffixes.

    Coverage is unioned per *statement*, which is the smallest scope that does
    not fail correct code. The correct inline spelling is two calls in one
    expression --
    ``sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml")))``, which is how
    ``tests/test_shell_tests_are_executed.py`` already had it -- so judging
    each call alone would fail the one file in the repository that got this
    right, and a rule that fires on correct code gets deleted rather than
    obeyed.

    Unioning over the whole *file* was the first attempt and it was too loose:
    an unrelated ``*.yaml`` glob anywhere in the file then masked a real
    ``*.yml`` check elsewhere in it. A reviewer built exactly that, so the
    scope is now the statement.

    Known boundary, stated rather than implied: this sees ``Path.glob`` and
    ``Path.rglob`` with a literal pattern. ``iterdir()`` filtered by suffix,
    ``os.listdir``, ``glob.glob``, and a pattern assembled at runtime are all
    invisible to it, as is a hardcoded list of filenames. It is a guard
    against the defect being rewritten in the shape it already took, not a
    proof that no such defect can exist.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:                                # pragma: no cover
        pytest.fail(f"{filename}: could not parse: {exc}")

    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    aliases = _workflow_alias_names(tree, source)
    # Keyed by the id of the enclosing statement, which is stable for as long
    # as `tree` is alive -- and it is, for the whole of this function.
    by_statement: dict[int, list[tuple[int, str, set[str]]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("glob", "rglob"):
            continue
        receiver = func.value
        named = _mentions_workflows(ast.get_source_segment(source, receiver))
        aliased = isinstance(receiver, ast.Name) and receiver.id in aliases
        if not (named or aliased):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        pattern = node.args[0].value
        if not isinstance(pattern, str):
            continue
        statement = _enclosing_statement(node, parents)
        key = id(statement) if statement is not None else 0
        by_statement.setdefault(key, []).append(
            (node.lineno, pattern, _covers(pattern)))

    out = []
    for sites in by_statement.values():
        reached: set[str] = set()
        for _lineno, _pattern, covered in sites:
            reached |= covered
        if reached == set(_PROBE_NAMES):
            continue
        missing = sorted(set(_PROBE_NAMES) - reached)
        absent = ", ".join("." + name.split(".")[-1] for name in missing)
        for lineno, pattern, _covered in sites:
            out.append(
                f"{filename}:{lineno}: enumerates the workflow directory with "
                f"{pattern!r}, and nothing else in that statement covers "
                f"{absent}. GitHub loads both .yml and .yaml, so a workflow "
                "with that suffix would be silently excluded from the check. "
                "Use `workflow_paths()` from "
                "test_workflow_discovery_conformance."
            )
    return sorted(out)


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
    """Population guard. Every assertion below is satisfied by an empty tree.

    Cross-checked against an independent listing rather than against a pinned
    filename: asserting ``"ci.yml" in found`` would break on a legitimate
    rename while proving nothing about discovery that the listing does not
    prove better.
    """
    found = workflow_paths()
    assert found, f"no workflow files found under {WORKFLOW_DIR}"
    independent = sorted(
        name for name in os.listdir(WORKFLOW_DIR)
        if name.endswith(".yml") or name.endswith(".yaml")
    )
    assert [p.name for p in found] == independent


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
    # The receiver under another name. An alias is the same defect wearing a
    # different variable, and reading only the receiver cannot see it.
    'WF_DIR = REPO / ".github" / "workflows"\npaths = WF_DIR.glob("*.yml")',
    'd = REPO / ".github" / "workflows"\npaths = d.glob("*.yml")',
    # A real single-suffix check masked by an unrelated `*.yaml` glob
    # elsewhere in the same file. This is why the union is per statement: over
    # the whole file, the second line here silenced the first.
    ('for p in WORKFLOW_DIR.glob("*.yml"):\n    pass\n'
     '_ = list(WORKFLOW_DIR.glob("*.yaml"))\n'),
    # The same masking one scope up: one suffix per function is not a file
    # that reaches both, it is two half-checks that each miss one.
    ('def a():\n    return WORKFLOW_DIR.glob("*.yml")\n\n\n'
     'def b():\n    return WORKFLOW_DIR.glob("*.yaml")\n'),
])
def test_the_scan_reports_a_single_suffix_glob(source: str):
    """Positive control: each real spelling of the defect is detected."""
    assert single_suffix_workflow_globs(source, "probe.py")


@pytest.mark.parametrize("source", [
    # Both suffixes, the correct spelling.
    'paths = list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml"))',
    # The same, spread over a statement rather than a line. This is verbatim
    # how `tests/test_shell_tests_are_executed.py` already had it, and the
    # scope of the union exists so that this file passes.
    ('paths = sorted(list(WORKFLOW_DIR.glob("*.yml"))\n'
     '               + list(WORKFLOW_DIR.glob("*.yaml")))'),
    # A pattern that covers both on its own.
    'paths = WORKFLOW_DIR.glob("*.y*ml")',
    'paths = WORKFLOW_DIR.glob("*")',
    # Not the workflow directory at all -- other single-suffix globs are fine.
    'paths = LOG_DIR.glob("*.yml")',
    # Alias tracking must not widen to every assigned name: this one is bound
    # to a directory that has nothing to do with workflows.
    'LOG_DIR = REPO / "logs"\npaths = LOG_DIR.glob("*.yml")',
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
