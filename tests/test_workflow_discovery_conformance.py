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

#: Calls whose string arguments are doing the filtering. A suffix filter is
#: spelled as a comparison or as one of these; a literal anywhere else in the
#: statement is not filtering, and reading it as though it were is what let a
#: stray both-suffix string silence a real defect.
_FILTER_CALLS = ("endswith", "startswith", "match", "full_match",
                 "fnmatch", "fnmatchcase")


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


def _filter_coverage(literals: set[str]) -> set[str] | None:
    """What `literals` admit between them, or ``None`` if none of them filter.

    ``None`` and ``set()`` are deliberately different answers, and collapsing
    them is the bug class this repository keeps finding in its own code: no
    filter at all means an unfiltered listing, which reaches *both* suffixes
    and is correct, while a filter that admits nothing recognisable is a
    listing narrowed by something this scan cannot read.
    """
    reached: set[str] = set()
    found = False
    for literal in literals:
        admitted = _admits(literal)
        if admitted:
            found = True
            reached |= admitted
    return reached if found else None


def _string_constants(node: ast.AST) -> set[str]:
    """Every string literal anywhere under `node`."""
    return {sub.value for sub in ast.walk(node)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)}


def _called_name(node: ast.Call) -> str:
    """The bare name a call is spelled with, receiver ignored."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _filter_terms(node: ast.AST, negated: bool,
                  out: list[tuple[bool, str]]) -> None:
    """Collect ``(negated, literal)`` for every literal in a filtering position.

    Filtering positions are comparison operands and the arguments of the
    calls that narrow a name -- ``n.endswith(".yml")``, ``p.match("*.yml")``,
    ``p.suffix in {".yml", ".yaml"}``. Reading *every* literal under the
    statement was the first attempt, and a reviewer showed what it cost: a
    string merely co-located with a real single-suffix filter, if it happened
    to admit both suffixes, silenced the statement.

    Polarity is tracked because an exclusion is not evidence that a suffix is
    reached -- it is evidence of the opposite. ``p.suffix == ".yml" and
    "yaml" not in p.name`` is a genuinely single-suffix filter, and reading
    its second half as "and .yaml is covered too" is how a second reviewer
    silenced it again after the first fix.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _filter_terms(node.operand, not negated, out)
        return
    if isinstance(node, ast.Compare):
        left = node.left
        for op, comparator in zip(node.ops, node.comparators):
            flipped = negated != isinstance(op, (ast.NotIn, ast.NotEq))
            for operand in (left, comparator):
                out += [(flipped, literal) for literal in _string_constants(operand)]
            left = comparator
    elif isinstance(node, ast.Call) and _called_name(node) in _FILTER_CALLS:
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        for operand in arguments:
            out += [(negated, literal) for literal in _string_constants(operand)]
    for child in ast.iter_child_nodes(node):
        _filter_terms(child, negated, out)


def _statement_filter_coverage(node: ast.AST) -> set[str] | None:
    """What the filtering in `node` admits, or ``None`` if it does not filter."""
    terms: list[tuple[bool, str]] = []
    _filter_terms(node, False, terms)
    reached: set[str] = set()
    found = False
    for negated, literal in terms:
        admitted = _admits(literal)
        if not admitted:
            continue
        found = True
        reached |= (set(_PROBE_NAMES) - admitted) if negated else admitted
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
    """True if `node` is, or is built from, the workflow directory.

    The alias search runs over the whole sub-tree rather than over the node
    itself, because the directory rarely arrives naked: ``str(WF_DIR)`` and
    ``WF_DIR / "*.yml"`` are the argument ``os.listdir`` and ``glob.glob``
    actually get, and neither the source text nor the top-level node type
    says "workflow" once the name has been aliased.
    """
    if node is None:
        return False
    if _mentions_workflows(ast.get_source_segment(source, node)):
        return True
    return any(isinstance(sub, ast.Name) and sub.id in aliases
               for sub in ast.walk(node))


def _argument_names_workflows(node: ast.Call, source: str, aliases: set[str]) -> bool:
    """True if any argument of `node` is the workflow directory.

    ``os.listdir`` and ``glob.glob`` take the directory as an argument rather
    than as a receiver, so the receiver test that finds ``WORKFLOW_DIR.glob``
    looks straight past them.
    """
    arguments = list(node.args) + [kw.value for kw in node.keywords]
    return any(_names_workflows(arg, source, aliases) for arg in arguments)


def _imported_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """The names by which ``glob``'s and ``os``'s enumerators reached this file.

    ``from glob import glob as find`` is the same call wearing a different
    name, and a rule keyed on the literal spellings cannot see it. The plain
    spellings stay in the sets whether or not the file imports anything,
    because a scan that only recognises what it can prove was imported goes
    quiet on a file whose imports it failed to read.
    """
    pattern_names = set(_PATTERN_FUNCTIONS)
    listing_names = set(_LISTING_FUNCTIONS)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if node.module == "glob" and alias.name in _PATTERN_FUNCTIONS:
                pattern_names.add(alias.asname or alias.name)
            elif node.module == "os" and alias.name in _LISTING_FUNCTIONS:
                listing_names.add(alias.asname or alias.name)
    return pattern_names, listing_names


def _literal_pattern(node: ast.Call) -> str | None:
    """The literal pattern `node` was called with, if it has one.

    ``Path.glob`` takes its pattern as an ordinary parameter, so
    ``WORKFLOW_DIR.glob(pattern="*.yml")`` runs and means exactly what the
    positional spelling means. An f-string with nothing interpolated into it
    is a literal too -- it is only a different node type.
    """
    candidate: ast.AST | None = node.args[0] if node.args else None
    if candidate is None:
        for keyword in node.keywords:
            if keyword.arg == "pattern":
                candidate = keyword.value
                break
    if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
        return candidate.value
    if isinstance(candidate, ast.JoinedStr) and all(
            isinstance(part, ast.Constant) and isinstance(part.value, str)
            for part in candidate.values):
        return "".join(part.value for part in candidate.values)
    return None


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

    Known boundaries, stated rather than implied. Each of these is a genuinely
    single-suffix read that goes unreported, and every one was constructed by
    a reviewer rather than imagined here:

    * the filter in a *later* statement -- ``names = os.listdir(d)`` and then
      ``[n for n in names if n.endswith(".yml")]``;
    * the filter behind a helper -- ``if keep(p)``, where ``keep`` compares
      the suffix;
    * the suffix set bound to a name -- ``suffixes = {".yml"}`` and then
      ``p.suffix in suffixes``;
    * a filter spelled as a regex, or as anything that is neither a
      comparison nor one of ``_FILTER_CALLS``;
    * a pattern assembled at runtime, and a hardcoded list of filenames;
    * a pattern reaching the call through ``**kwargs``;
    * the directory itself spelled so that neither its source text nor a
      one-level alias says "workflow" -- ``REPO / ".github" / ("work" +
      "flows")``;
    * ``os.walk``, which is not read at all.

    It is a guard against the defect being rewritten in the shapes it has
    actually taken, not a proof that no such defect can exist.
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
    pattern_names, listing_names = _imported_names(tree)
    # Every enumerator name this file could be using. Checked before the
    # receiver is examined at all, because `ast.get_source_segment` re-splits
    # the whole file on every call and asking it about every call node in the
    # repository turned this scan from 30 seconds into three minutes.
    enumerators = (set(_PATTERN_METHODS) | set(_LISTING_METHODS)
                   | pattern_names | listing_names)
    # Keyed by the id of the enclosing statement, which is stable for as long
    # as `tree` is alive -- and it is, for the whole of this function.
    by_statement: dict[int, list[tuple[int, str, set[str]]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in enumerators:
            continue
        # Dispatch on where the directory actually is, not on how the
        # receiver is spelled. `glob.glob` and `Path.glob` are the same
        # attribute and take the directory in opposite positions, and a
        # heuristic keyed on the name `glob` fails in both directions: a
        # local named `glob` holding the workflow directory reads as the
        # module, and `helpers.glob(str(WORKFLOW_DIR / "*.yml"))` reads as a
        # path whose receiver says nothing about workflows.
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        on_workflows = _names_workflows(receiver, source, aliases)
        of_workflows = _argument_names_workflows(node, source, aliases)

        statement = _enclosing_statement(node, parents)
        site: tuple[str, set[str]] | None = None

        if on_workflows and name in _PATTERN_METHODS:
            pattern = _literal_pattern(node)
            if pattern is None:
                continue
            site = (f"enumerates the workflow directory with {pattern!r}",
                    _covers(pattern))
        elif of_workflows and name in pattern_names:
            covered = _filter_coverage(_string_constants(node))
            if covered is None:
                # A pattern with no readable suffix in it cannot be
                # classified, so it is not reported -- the same answer this
                # scan has always given `glob(pattern)`.
                continue
            site = (f"enumerates the workflow directory with {name}()", covered)
        elif ((on_workflows and name in _LISTING_METHODS)
                or (of_workflows and name in listing_names)):
            covered = (_statement_filter_coverage(statement)
                       if statement is not None else None)
            if covered is None:
                # No filter beside it: the listing reaches both suffixes and
                # there is nothing to report. Crediting it with both instead
                # would make it a *site*, and a site covering both suffixes
                # masks every other site in the same statement -- an
                # unfiltered `iterdir()` would then silence a single-suffix
                # `glob()` written next to it.
                continue
            site = (f"lists the workflow directory with {name}() and filters "
                    "it by suffix", covered)
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
    'paths = glob.glob(os.path.join(WORKFLOW_DIR, "*.yml"))',
    'paths = glob.glob(f"{WORKFLOW_DIR}/*.yml")',
    # --- everything below was constructed by an adversarial reviewer against
    # the first draft of this scan, and every one of them went unreported ---
    # `Path.glob` takes its pattern as an ordinary parameter, so the keyword
    # spelling runs and means the same thing.
    'paths = WORKFLOW_DIR.glob(pattern="*.yml")',
    # An import alias is the same call wearing a different name.
    'import glob as g\npaths = g.glob(str(WORKFLOW_DIR / "*.yml"))',
    'from glob import glob as find\npaths = find(str(WORKFLOW_DIR / "*.yml"))',
    'import os as o\nnames = [n for n in o.listdir(WORKFLOW_DIR) if n.endswith(".yml")]',
    'from os import listdir as ls\nnames = [n for n in ls(WORKFLOW_DIR) if n.endswith(".yml")]',
    # The aliased directory wrapped in a call, which is how it actually
    # reaches `os.listdir` and `glob.glob`: neither the source text of the
    # argument nor its top-level node says "workflow" any more.
    ('WF_DIR = REPO / ".github" / "workflows"\n'
     'paths = glob.glob(str(WF_DIR / "*.yml"))'),
    ('WF_DIR = REPO / ".github" / "workflows"\n'
     'names = [n for n in os.listdir(str(WF_DIR)) if n.endswith(".yml")]'),
    ('WF_DIR = REPO / ".github" / "workflows"\n'
     'names = [n for n in os.listdir(path=str(WF_DIR)) if n.endswith(".yml")]'),
    # A local named `glob` holding the workflow directory. Reading it as the
    # module sends the call down the branch that looks for the directory in
    # the arguments, where it is not.
    'glob = WORKFLOW_DIR\npaths = glob.glob("*.yml")',
    # A stray literal admitting both suffixes, sitting in the same statement
    # as a real single-suffix filter but not in a filtering position. Reading
    # every literal under the statement let this silence the defect.
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.suffix == ".yml" and ("*.y*ml" or True)]',
    'x = ([p for p in WORKFLOW_DIR.iterdir() if p.suffix == ".yml"], "*.y*ml")',
    # An unfiltered listing beside a single-suffix glob. Crediting the
    # listing with both suffixes made it mask the glob.
    'x = (list(WORKFLOW_DIR.iterdir()), list(WORKFLOW_DIR.glob("*.yml")), ".yaml")',
    'x = (list(WORKFLOW_DIR.iterdir()), list(WORKFLOW_DIR.glob("*.yml")))',
    # An f-string with nothing interpolated is a literal wearing a different
    # node type.
    'paths = WORKFLOW_DIR.glob(f"*.yml")',
    # An *exclusion* is not evidence that a suffix is reached. Reading the
    # second half of this as "and .yaml is covered" silenced a filter that
    # explicitly throws .yaml away.
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.suffix == ".yml" and "yaml" not in p.name]',
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.suffix != ".yaml"]',
    'names = [n for n in os.listdir(WORKFLOW_DIR) if not n.endswith(".yaml")]',
    # A module that is not spelled `glob` or `os` still takes the directory
    # as an argument, and dispatching on the receiver's name looked past it.
    'paths = helpers.glob(str(WORKFLOW_DIR / "*.yml"))',
    'names = [n for n in helpers.listdir(WORKFLOW_DIR) if n.endswith(".yml")]',
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
    # A literal that is not filtering anything. Stripping the suffix off the
    # names of an unfiltered listing narrows nothing, and reading every
    # literal under the statement reported this correct code as a fault.
    'entries = sorted(WORKFLOW_DIR.iterdir(), key=lambda p: p.name.removesuffix(".yml"))',
    'names = [p.name.removesuffix(".yml") for p in WORKFLOW_DIR.iterdir()]',
    # Both suffixes reached through spellings other than a set literal.
    'paths = [p for p in WORKFLOW_DIR.iterdir() if p.suffix in frozenset({".yml", ".yaml"})]',
    'names = [n for n in os.listdir(WORKFLOW_DIR) if n.endswith((".yml", ".yaml"))]',
    # Nothing to see.
    'x = 1',
])
def test_the_scan_stays_quiet(source: str):
    """Negative control: the correct spellings are not reported as faults."""
    assert single_suffix_workflow_scans(source, "probe.py") == []


@pytest.mark.parametrize("source", [
    # The filter in a later statement.
    ('names = os.listdir(WORKFLOW_DIR)\n'
     'only = [n for n in names if n.endswith(".yml")]'),
    # The filter behind a helper.
    ('def keep(p):\n    return p.suffix == ".yml"\n'
     'paths = [p for p in WORKFLOW_DIR.iterdir() if keep(p)]'),
    # The suffix set bound to a name.
    'suffixes = {".yml"}\npaths = [p for p in WORKFLOW_DIR.iterdir() if p.suffix in suffixes]',
    # A regex filter: the literal is neither a comparison operand nor an
    # argument of a filtering call, and it would not classify if it were.
    'names = [n for n in os.listdir(WORKFLOW_DIR) if re.search(r"\\.yml$", n)]',
    # The directory spelled so that neither its source text nor a one-level
    # alias contains the word.
    'd = REPO / ".github" / ("work" + "flows")\npaths = d.glob("*.yml")',
    # The pattern reaching the call through a mapping.
    'kwargs = {"pattern": "*.yml"}\npaths = WORKFLOW_DIR.glob(**kwargs)',
])
def test_the_documented_boundaries_are_really_boundaries(source: str):
    """The misses named in the docstring, asserted as misses.

    Not a blessing -- a tripwire on the prose. A boundary list is the one
    part of a guard nobody re-derives, so it rots into a claim that the scan
    is weaker than it is, and the next author writes a second guard for a
    shape that was already covered. If one of these starts being reported,
    this test fails and whoever closed it edits the docstring in the same
    change.
    """
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


@pytest.mark.parametrize("relative, correct, broken", [
    (
        "tests/test_shell_tests_are_executed.py",
        'if p.suffix in {".yml", ".yaml"}',
        'if p.suffix in {".yml"}',
    ),
    (
        "tests/test_workflow_discovery_conformance.py",
        'if name.endswith(".yml") or name.endswith(".yaml")',
        'if name.endswith(".yml")',
    ),
])
def test_the_real_listings_are_cleared_and_the_clearance_is_losable(
        relative: str, correct: str, broken: str):
    """Both halves, against the files rather than against strings I chose.

    The negative controls elsewhere in this module are inputs I wrote; these
    two are the listings this repository actually contains, and a detector
    that reported them would be deleted within the hour rather than obeyed.

    Clearing them proves nothing on its own, though -- "no offenders" is
    equally consistent with a detector that never looked -- so the same file
    is scanned again with one suffix removed and has to go red. That second
    half is also what makes the premise honest. Asserting the spelling is
    *present* in the source is a substring test, and a reviewer pointed out
    that a commented-out listing satisfies it; the mutation does not care,
    because a commented-out listing produces no report and the test fails.
    """
    source = (REPO / relative).read_text(encoding="utf-8")
    mutated = source.replace(correct, broken)
    assert mutated != source, (
        f"{relative} no longer contains {correct!r}, so neither half of this "
        "test is about the code it was written for"
    )
    assert single_suffix_workflow_scans(source, relative) == []
    reported = single_suffix_workflow_scans(mutated, relative)
    assert reported, (
        f"{relative} with one suffix dropped went unreported, so clearing "
        "the real file proves nothing about the detector"
    )
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
