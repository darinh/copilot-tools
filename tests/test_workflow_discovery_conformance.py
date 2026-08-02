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
  that reads the workflow directory in a way that can only reach one of the
  two suffixes -- a glob pattern naming one, or a whole-directory listing
  (``iterdir``, ``os.listdir``, ``os.scandir``) narrowed to one by the
  filtering beside it. That is what stops the next guard being written with
  the same blind spot.
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

#: ``Path`` methods whose own literal argument *is* the filter.
_PATTERN_METHODS = ("glob", "rglob")

#: ``glob`` module functions, same shape: the filter is inside the call.
_PATTERN_FUNCTIONS = ("glob", "iglob")

#: ``Path`` methods that hand back the whole directory. Whatever narrows the
#: result is a separate expression, so the filter is looked for in the
#: enclosing statement rather than in the call.
_LISTING_METHODS = ("iterdir",)

#: ``os`` functions of the same shape.
_LISTING_FUNCTIONS = ("listdir", "scandir")

#: Receivers that make ``x.y(...)`` a module-level call rather than a method
#: on a path. ``glob.glob`` and ``Path.glob`` are spelled identically as an
#: attribute, and they take their directory in opposite positions.
_MODULE_RECEIVERS = ("glob", "os")


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


def _admits(literal: str) -> set[str]:
    """Which probe names a filter written as `literal` would let through.

    Two spellings, because a listing is narrowed two ways: as a glob pattern
    (``"*.yml"``, read with fnmatch) and as a bare suffix compared or handed
    to ``str.endswith`` (``".yml"``, ``"yml"``). A literal that admits
    neither probe -- a filename, a message, a dict key -- is not a suffix
    filter, and returning an empty set for it is what keeps the ordinary
    strings in a statement from being read as filtering.
    """
    if not literal:
        return set()
    # A pattern handed to ``glob.glob`` arrives with its directory attached
    # (``str(WORKFLOW_DIR) + "/*.yml"``), and it is the last path segment
    # that decides which filenames it reaches.
    tail = literal.replace("\\", "/").rsplit("/", 1)[-1]
    candidates = {literal, tail} - {""}
    return {name for name in _PROBE_NAMES
            for candidate in candidates
            if fnmatch.fnmatch(name, candidate) or name.endswith(candidate)}


def _filter_coverage(node: ast.AST) -> set[str] | None:
    """What the suffix filtering under `node` admits, or ``None`` for none.

    ``None`` and ``set()`` are deliberately different answers, and collapsing
    them is the bug this repository keeps finding in its own code: no filter
    at all means an unfiltered listing, which reaches *both* suffixes and is
    correct, while a filter that admits nothing recognisable is a listing
    narrowed by something this scan cannot read.

    Literals are collected blind, from anywhere under the node. That
    over-matches -- a ``".yml"`` in the statement for some unrelated reason
    makes a correct unfiltered listing report -- and that direction is the
    cheap one, because it fails loudly on code a human then reads. The
    expensive direction is the other one, and it survives here: a stray
    literal that admits *both* suffixes silences the statement.
    """
    reached: set[str] = set()
    found = False
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Constant) and isinstance(sub.value, str)):
            continue
        admitted = _admits(sub.value)
        if admitted:
            found = True
            reached |= admitted
    return reached if found else None


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


def _names_workflows(node: ast.AST | None, source: str, aliases: set[str]) -> bool:
    """True if `node` is the workflow directory, spelled out or aliased."""
    if node is None:
        return False
    if _mentions_workflows(ast.get_source_segment(source, node)):
        return True
    return isinstance(node, ast.Name) and node.id in aliases


def _argument_names_workflows(node: ast.Call, source: str, aliases: set[str]) -> bool:
    """True if any argument of `node` is the workflow directory.

    ``os.listdir`` and ``glob.glob`` take the directory as an argument rather
    than as a receiver, so the receiver test that finds ``WORKFLOW_DIR.glob``
    looks straight past them.
    """
    arguments = list(node.args) + [kw.value for kw in node.keywords]
    return any(_names_workflows(arg, source, aliases) for arg in arguments)


def single_suffix_workflow_scans(source: str, filename: str) -> list[str]:
    """Report workflow-directory reads that cannot reach both suffixes.

    Three shapes are recognised, because the defect is "the population is
    narrowed to one suffix", not "someone called ``glob``":

    * ``WORKFLOW_DIR.glob("*.yml")`` and ``rglob`` -- the filter is the
      call's own literal argument.
    * ``glob.glob(str(WORKFLOW_DIR / "*.yml"))`` -- same, one module over.
    * ``WORKFLOW_DIR.iterdir()``, ``os.listdir(WORKFLOW_DIR)`` and
      ``os.scandir`` -- these hand back everything, so the filter is a
      *separate* expression and is looked for in the enclosing statement.
      An unfiltered listing reaches both suffixes and is not reported; it is
      the ``if p.suffix == ".yml"`` next to it that makes it half a rule.
      Both correct spellings in this repository are of this shape --
      ``p.suffix in {".yml", ".yaml"}`` in
      ``tests/test_shell_tests_are_executed.py`` and the ``os.listdir``
      cross-check below -- so they are the standing negative controls.

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

    Known boundary, stated rather than implied: a pattern assembled at
    runtime is invisible, as is a hardcoded list of filenames, a filter
    written as a regex, and a listing whose filtering happens in a *later*
    statement (``names = os.listdir(d)`` then ``[n for n in names if ...]``).
    ``os.walk`` is not read either. It is a guard against the defect being
    rewritten in the shapes it has actually taken, not a proof that no such
    defect can exist.
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
        if isinstance(func, ast.Name):
            name, receiver, module_call = func.id, None, True
        elif isinstance(func, ast.Attribute):
            name, receiver = func.attr, func.value
            module_call = (isinstance(receiver, ast.Name)
                           and receiver.id in _MODULE_RECEIVERS)
        else:
            continue

        statement = _enclosing_statement(node, parents)
        site: tuple[str, set[str]] | None = None

        if not module_call and name in _PATTERN_METHODS:
            if not _names_workflows(receiver, source, aliases):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            pattern = node.args[0].value
            if not isinstance(pattern, str):
                continue
            site = (f"enumerates the workflow directory with {pattern!r}",
                    _covers(pattern))
        elif module_call and name in _PATTERN_FUNCTIONS:
            if not _argument_names_workflows(node, source, aliases):
                continue
            covered = _filter_coverage(node)
            if covered is None:
                # A pattern with no readable suffix in it cannot be
                # classified, so it is not reported -- the same answer this
                # scan has always given `glob(pattern)`.
                continue
            site = (f"enumerates the workflow directory with {name}()", covered)
        elif not module_call and name in _LISTING_METHODS:
            if not _names_workflows(receiver, source, aliases):
                continue
            covered = _filter_coverage(statement) if statement is not None else None
            site = (f"lists the workflow directory with {name}() and filters "
                    "it by suffix",
                    set(_PROBE_NAMES) if covered is None else covered)
        elif module_call and name in _LISTING_FUNCTIONS:
            if not _argument_names_workflows(node, source, aliases):
                continue
            covered = _filter_coverage(statement) if statement is not None else None
            site = (f"lists the workflow directory with {name}() and filters "
                    "it by suffix",
                    set(_PROBE_NAMES) if covered is None else covered)
        else:
            continue

        key = id(statement) if statement is not None else 0
        by_statement.setdefault(key, []).append((node.lineno, *site))

    out = []
    for sites in by_statement.values():
        reached: set[str] = set()
        for _lineno, _description, covered in sites:
            reached |= covered
        if reached == set(_PROBE_NAMES):
            continue
        missing = sorted(set(_PROBE_NAMES) - reached)
        absent = ", ".join("." + name.split(".")[-1] for name in missing)
        for lineno, description, _covered in sites:
            out.append(
                f"{filename}:{lineno}: {description}, and nothing else in "
                f"that statement covers {absent}. GitHub loads both .yml and "
                ".yaml, so a workflow with that suffix would be silently "
                "excluded from the check. Use `workflow_paths()` from "
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
    # --- the listing shapes: the directory is read whole and narrowed by a
    # separate expression, which a receiver-and-pattern rule looks past ---
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.suffix == ".yml"]',
    'paths = {p for p in WORKFLOW_DIR.iterdir() if p.suffix in (".yml",)}',
    'names = [n for n in os.listdir(WORKFLOW_DIR) if n.endswith(".yml")]',
    'names = [e.name for e in os.scandir(WORKFLOW_DIR) if e.name.endswith(".yaml")]',
    ('for p in WORKFLOW_DIR.iterdir():\n'
     '    if p.name.endswith(".yml"):\n'
     '        pass\n'),
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.match("*.yml")]',
    # The listing shapes under an alias, same as the glob shapes above.
    ('WF_DIR = REPO / ".github" / "workflows"\n'
     'names = [n for n in os.listdir(WF_DIR) if n.endswith(".yml")]'),
    ('wf = REPO / ".github" / "workflows"\n'
     'paths = [p for p in wf.iterdir() if p.suffix == ".yml"]'),
    # The `glob` module reaches the same directory by argument rather than by
    # receiver, so the receiver test never sees it.
    'paths = glob.glob(str(WORKFLOW_DIR / "*.yml"))',
    'paths = list(glob.iglob(str(WORKFLOW_DIR) + "/*.yaml"))',
    'paths = glob(str(WORKFLOW_DIR / "*.yml"))',
])
def test_the_scan_reports_a_single_suffix_glob(source: str):
    """Positive control: each real spelling of the defect is detected."""
    assert single_suffix_workflow_scans(source, "probe.py")


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
    # --- the listing shapes ---
    # Both suffixes. This one is verbatim from
    # `tests/test_shell_tests_are_executed.py`, which is where the shape was
    # already written correctly.
    ('found = {p.name for p in WORKFLOW_DIR.iterdir()\n'
     '         if p.suffix in {".yml", ".yaml"}}'),
    # And this one is verbatim from `test_the_repository_really_has_workflows`
    # in this very file, which is why the `os.listdir` detector has to read
    # the whole statement rather than the call.
    ('independent = sorted(\n'
     '    name for name in os.listdir(WORKFLOW_DIR)\n'
     '    if name.endswith(".yml") or name.endswith(".yaml")\n'
     ')'),
    # An unfiltered listing reaches both suffixes; it is the filter that makes
    # a listing half a rule, and there is no filter here. Reporting this would
    # be the detector calling correct code wrong.
    'entries = sorted(WORKFLOW_DIR.iterdir())',
    'names = os.listdir(WORKFLOW_DIR)',
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.is_file()]',
    # The receiver set must not widen to every listing in the repository:
    # these read directories that have nothing to do with workflows, and they
    # are narrowed to one suffix on purpose.
    'names = [n for n in os.listdir(LOG_DIR) if n.endswith(".yml")]',
    'paths = [p for p in LOG_DIR.iterdir() if p.suffix == ".yml"]',
    'paths = [p for p in (tmp_path / "cfg").iterdir() if p.suffix == ".yml"]',
    'LOG_DIR = REPO / "logs"\nnames = [n for n in os.listdir(LOG_DIR) if n.endswith(".yml")]',
    'paths = glob.glob(str(LOG_DIR / "*.yml"))',
    # A runtime-assembled pattern cannot be classified, the same answer this
    # scan has always given `WORKFLOW_DIR.glob(pattern)`.
    'paths = glob.glob(str(WORKFLOW_DIR / pattern))',
    # Nothing to see.
    'x = 1',
])
def test_the_scan_stays_quiet(source: str):
    """Negative control: the correct spellings are not reported as faults."""
    assert single_suffix_workflow_scans(source, "probe.py") == []


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
    reported = single_suffix_workflow_scans(historical, "test_git_identity.py")
    assert len(reported) == 1
    assert ".yaml" in reported[0]


def test_the_scan_clears_the_real_both_suffix_listings():
    """The two correct listings this repository actually contains, from disk.

    The negative controls above are strings I chose; these are the files, and
    a detector that reports them would be deleted within the hour rather than
    obeyed. The premise assertions matter as much as the verdict: if either
    file stops listing the workflow directory in the shape the detector was
    written for, this test would keep passing while proving nothing, which is
    the failure mode the whole module exists to argue against.
    """
    listings = {
        "tests/test_shell_tests_are_executed.py": "WORKFLOW_DIR.iterdir()",
        "tests/test_workflow_discovery_conformance.py": "os.listdir(WORKFLOW_DIR)",
    }
    for relative, spelling in listings.items():
        source = (REPO / relative).read_text(encoding="utf-8")
        assert spelling in source, (
            f"{relative} no longer contains {spelling!r}, so clearing it "
            "proves nothing about the listing detector"
        )
        assert single_suffix_workflow_scans(source, relative) == []


@pytest.mark.parametrize("shape, correct, broken", [
    (
        "iterdir",
        'found = {p.name for p in WORKFLOW_DIR.iterdir()\n'
        '         if p.suffix in {".yml", ".yaml"}}',
        'found = {p.name for p in WORKFLOW_DIR.iterdir()\n'
        '         if p.suffix in {".yml"}}',
    ),
    (
        "os.listdir",
        'independent = sorted(\n'
        '    name for name in os.listdir(WORKFLOW_DIR)\n'
        '    if name.endswith(".yml") or name.endswith(".yaml")\n'
        ')',
        'independent = sorted(\n'
        '    name for name in os.listdir(WORKFLOW_DIR)\n'
        '    if name.endswith(".yml")\n'
        ')',
    ),
])
def test_dropping_one_suffix_from_a_real_listing_is_reported(
        shape: str, correct: str, broken: str):
    """Falsifiability, per shape: the clean verdict has to be losable.

    A detector that clears the real code is only evidence if the same code
    with one suffix removed goes red -- otherwise "no offenders" is equally
    consistent with a detector that never looked. Both halves are asserted
    here so a future change cannot satisfy one by breaking the other.
    """
    assert single_suffix_workflow_scans(correct, "probe.py") == [], shape
    reported = single_suffix_workflow_scans(broken, "probe.py")
    assert reported, f"{shape}: dropping a suffix went unreported"
    assert ".yaml" in reported[0]


def test_no_first_party_source_enumerates_workflows_by_one_suffix():
    """The live rule, over the whole tree rather than over one file.

    The population guard is not decoration. A scan whose discovery quietly
    shrinks reports the whole tree clean, which is indistinguishable from
    success -- so the population is pinned two ways: a floor on its size, and
    the files in the repository that actually read the workflow directory, by
    name. If either ever stops being scanned, the rule is enforcing nothing
    and should say so rather than pass.
    """
    sources = _python_sources()
    assert len(sources) > 20, (
        f"the python source discovery came back with {len(sources)} files, "
        "which is too few to be the repository -- the scan below would "
        "report clean without having looked at anything"
    )
    relative = {str(p.relative_to(REPO)).replace("\\", "/") for p in sources}
    for pinned in ("tests/test_shell_tests_are_executed.py",
                   "tests/test_workflow_discovery_conformance.py"):
        assert pinned in relative, (
            f"{pinned} reads the workflow directory but is not in the "
            "scanned population, so a regression in it would go unreported"
        )
    offenders = []
    for path in sources:
        offenders += single_suffix_workflow_scans(
            path.read_text(encoding="utf-8", errors="replace"),
            str(path.relative_to(REPO)).replace("\\", "/"),
        )
    assert not offenders, "\n".join(offenders)
