"""No shipped code can enroll a project. Nothing offers, so nothing refuses.

`AGENTS.md` currently spends three lines telling an agent that this project
is already registered and that it **must not offer to enroll this directory
or write to the project catalog**. That rule is real, and it is a rule about
a hazard that no longer exists in the code: it was written when
`templates/copilot-instructions.md` was installed at *user* scope, where its
"resolve a project root, read the catalog, offer to enroll" opening ran in
every Copilot session on the machine, in every directory, project or not.
`project_instructions.py` retired that file. The prohibition outlived the
instruction it was contradicting.

The audit (`specs/004-operator-session/audit.md`) proposes deleting it, and
decision D10 forbids deleting a rule without landing its check in the same
commit. This is that check, and its shape follows from what "enrollment"
actually is. Registering a project means two writes, and only two: a row in
`~/.operator/projects/catalog.csv` mapping a directory to an id, and the id
itself, minted fresh. **Nothing in first-party production code performs
either.** That is not an accident of the current implementation -- it is why
the rule was ever satisfiable -- and this file is what stops it changing
without somebody noticing.

That gives the prohibition a stronger backing than the prose had. Prose asks
an agent not to do something it was, at the time, perfectly able to do. A
scan says the machinery is absent, so the instruction has nothing to act on
even if a future agent never reads the line.

**Why an AST scan rather than a text search.** `"catalog" in line and
"write" in line` matches this module's own docstring, matches a comment
explaining the rule, and misses `p = catalog_path(); p.write_text(...)`
entirely -- wrong in both directions at once. The scan below resolves the
*call* and asks whether its target expression is derived from the catalog,
which is the question the rule is actually about.

**Why `tests/` is exempt, and why that is safe.** Half the suite writes a
fixture catalog into `tmp_path`; a scan that reported those would have to be
switched off. What makes the exemption safe is that a test's catalog is one
it created in a temp directory, and `conftest` already guards the real one:
`tests/test_artifact_guard.py` covers a suite that clobbers
`~/.operator/projects/catalog.csv`, banks the original and fails the run.
The two guards divide the space -- this one says production code cannot
write a catalog anywhere, that one says test code cannot write *the* one.

Every detector here has a positive control asserting it fires on the shape it
is looking for, and a negative control asserting the legitimate spellings
still pass. A detector that matches nothing reports the whole tree clean,
which reads exactly like success -- and here it would read as "enrollment is
impossible" at the moment enrollment became possible.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist", ".venv", "venv")

#: Names whose value is, or is derived from, the project catalog. Both the
#: module-level constant and the two accessors, because a caller reaches it
#: through whichever is in scope.
CATALOG_SOURCES = frozenset({
    "CATALOG", "catalog", "project_catalog_path", "catalog_path",
})

#: Attribute calls that write to a filesystem path.
WRITING_METHODS = frozenset({"write_text", "write_bytes", "touch", "mkdir"})

#: The generators of a fresh identifier. Enrolling a project mints one; a
#: temp-file suffix, a message id and a trace id also do, so the scan asks
#: where the result *goes*, not merely that the call happened.
MINTERS = frozenset({"uuid1", "uuid3", "uuid4", "uuid5"})


def _production_sources() -> list[Path]:
    """First-party `*.py` outside `tests/`, discovered rather than listed.

    Filtering happens on the repo-*relative* path. An absolute-path filter
    matches `.worktrees` for every file when the checkout is itself a
    worktree -- which is where all work in this repository happens -- and an
    empty population passes every assertion in this file.
    """
    out = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if any(part in NOT_SOURCE for part in rel.parts):
            continue
        if rel.parts[0] == "tests":
            continue
        out.append(path)
    return sorted(out)



def _catalog_derived(node: ast.AST, extra=frozenset()) -> bool:
    """Whether this expression names the catalog or something built from it.

    Deliberately covers `catalog.parent`, `CATALOG.with_name(...)` and
    `project_catalog_path()`: a write beside the catalog under a name derived
    from it is the same act as a write to it, and demanding the exact
    attribute chain would let one `.with_suffix("")` through.

    Attribute *names* count as well as bare names, so `self.catalog` and
    `env["paths"].catalog` are the catalog. Holding it on an object is the
    obvious way to write enrollment, and a scan that only understood module
    globals would have watched it go past.
    """
    names = CATALOG_SOURCES | extra
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in names:
            return True
    return False


def _scopes(tree: ast.AST) -> list:
    """(scope, direct children) for the module body and every function body.

    Alias resolution has to be per-scope. A flat pass over a 5,000-line
    module was measured turning 200-odd unrelated locals into "the catalog":
    once any name enters the set, an assignment from it *anywhere* in the
    file adds another, and the transitive closure eats the module. A scan
    that reports everything gets switched off exactly as fast as one that
    reports nothing, and it wastes a maintainer's afternoon on the way.
    """
    out = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(node)
    return out


def _own_nodes(scope: ast.AST) -> list:
    """Every node under `scope` that is not inside a nested function."""
    nested = set()
    for node in ast.walk(scope):
        if node is scope:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                nested.add(id(inner))
    return [n for n in ast.walk(scope) if id(n) not in nested]


def _aliases(tree: ast.AST) -> frozenset:
    """Names bound to a catalog-derived expression, resolved per scope.

    `p = CATALOG` followed by `open(p, "w")` is enrollment, and without this
    it is enrollment the scan reports as clean. Resolution is crude on
    purpose -- no reassignment tracking, no branch analysis -- but it is
    *scoped*, which is the only reason it means anything (see `_scopes`).

    Tuple unpacking is deliberately not resolved. `a, b = x, CATALOG` would
    need positional matching, and the shape does not occur: nothing assigns
    the catalog that way, so supporting it would be a branch with no input
    to distinguish it from its own absence.

    Within a scope it over-approximates, and knowingly. `found =
    catalog_guid(root, CATALOG)` returns a lookup result, not a path, but
    the catalog appears in the expression so `found` joins the set. Deciding
    otherwise means knowing what each callee returns. Over-approximation is
    the safe direction -- the cost is one spurious line in a traceback -- and
    the scoping is what stops it compounding: measured over this repository
    the sets are `{catalog, path}`, `{catalog}` and `{found}`, not the 200+
    names the unscoped draft produced.

    It iterates to a fixed point within a scope so a chain (`a = CATALOG`,
    `b = a.parent`) resolves. The set only grows, so it terminates.
    """
    out: set = set()
    for scope in _scopes(tree):
        nodes = _own_nodes(scope)
        found: set = set()
        while True:
            before = len(found)
            for node in nodes:
                if not isinstance(node, ast.Assign):
                    continue
                if not _catalog_derived(node.value, frozenset(found)):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        found.add(target.attr)
            if len(found) == before:
                break
        out |= found
    return frozenset(out)


def _writes_to_catalog(tree: ast.AST) -> list[tuple[int, str]]:
    """Every call in `tree` that writes to a catalog-derived path."""
    found = []
    seen = set()
    for scope in _scopes(tree):
        nodes = _own_nodes(scope)
        alias = _aliases(ast.Module(body=[scope], type_ignores=[])
                         if not isinstance(scope, ast.Module) else scope)
        for node in nodes:
            if not isinstance(node, ast.Call) or id(node) in seen:
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in WRITING_METHODS:
                if _catalog_derived(func.value, alias):
                    seen.add(id(node))
                    found.append((node.lineno, f".{func.attr}()"))
                continue
            if isinstance(func, ast.Name) and func.id == "open":
                args = list(node.args)
                mode = args[1] if len(args) > 1 else None
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode = kw.value
                opens_for_write = (
                    isinstance(mode, ast.Constant)
                    and isinstance(mode.value, str)
                    and any(ch in mode.value for ch in "wax+"))
                if opens_for_write and args and _catalog_derived(args[0],
                                                                 alias):
                    seen.add(id(node))
                    found.append((node.lineno, "open(..., write mode)"))
    return sorted(found)


def _mints_a_project_id(tree: ast.AST) -> list[tuple[int, str]]:
    """Every `uuid` call whose result is written to a catalog-derived path.

    A bare `uuid4()` is not reportable and must not be: this repository uses
    one for atomic temp-file names, for message ids and for trace ids, and a
    scan that objected to those would be an exemption list waiting to happen.
    What distinguishes minting a *project* id is where the value goes, so
    that is what is asked.
    """
    found = []
    alias = _aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name not in MINTERS:
            continue
        for parent in ast.walk(tree):
            if not isinstance(parent, ast.Call):
                continue
            pfunc = parent.func
            if not (isinstance(pfunc, ast.Attribute)
                    and pfunc.attr in WRITING_METHODS):
                continue
            if not _catalog_derived(pfunc.value, alias):
                continue
            if any(sub is node for sub in ast.walk(parent)):
                found.append((node.lineno, f"{name}() into a catalog write"))
    return found


# ── the scans ───────────────────────────────────────────────────
def test_no_production_module_writes_the_project_catalog() -> None:
    """The catalog is read-only to everything that ships.

    An agent cannot enroll a directory by running these tools, whatever it
    was told -- so the instruction telling it not to has nothing to prevent.
    """
    offenders = []
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for line, what in _writes_to_catalog(tree):
            offenders.append(f"{path.relative_to(REPO)}:{line} {what}")
    assert offenders == [], (
        "Production code writes the project catalog. Enrollment is now a "
        "code path, so AGENTS.md's 'you must not offer to enroll this "
        "directory' needs to be a rule again, or this needs to not be here:"
        "\n  " + "\n  ".join(offenders))


def test_no_production_module_mints_a_project_id() -> None:
    offenders = []
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for line, what in _mints_a_project_id(tree):
            offenders.append(f"{path.relative_to(REPO)}:{line} {what}")
    assert offenders == [], "\n  ".join(offenders)


def test_the_population_is_not_empty() -> None:
    """Without this, deleting the repository passes every scan above.

    The two assertions are `== []` over a loop, and a loop over nothing
    satisfies them perfectly.
    """
    sources = _production_sources()
    assert len(sources) > 20, sources
    names = {p.name for p in sources}
    assert "project_paths.py" in names
    assert "copilot_operator.py" in names
    assert not any(p.parts[0] == "tests"
                   for p in (s.relative_to(REPO) for s in sources))


# ── positive controls: the detector fires ───────────────────────
@pytest.mark.parametrize("source", [
    'CATALOG.write_text(rows)',
    'catalog.write_bytes(b"x")',
    'catalog.touch()',
    'project_catalog_path().write_text(rows)',
    'catalog_path(home).write_text(rows)',
    'CATALOG.parent.mkdir(parents=True)',
    'CATALOG.with_name("catalog.csv.new").write_text(rows)',
    'p = CATALOG\nopen(p, "w").write(rows)',
    'open(catalog, "a", encoding="utf-8").write(row)',
    'open(CATALOG, mode="w").close()',
    'self.catalog.write_text(rows)',
    'rows[0].catalog.write_text(rows)',
    'p = CATALOG\nopen(p, "w").write(rows)',
    'a = CATALOG\nb = a.parent\nb.mkdir()',
    'self.catalog = project_catalog_path()\nself.catalog.write_text(rows)',
])
def test_a_catalog_write_is_reported(source: str) -> None:
    assert _writes_to_catalog(ast.parse(source)), source


@pytest.mark.parametrize("source", [
    'p = CATALOG',
    'a = CATALOG\nb = a.parent',
    'self.catalog = project_catalog_path()',
    'def f():\n    p = CATALOG\n    return p',
])
def test_an_alias_of_the_catalog_is_resolved(source: str) -> None:
    """Without this, one `p = CATALOG` turns the scan off for a whole file
    and it still reports the tree clean."""
    assert _aliases(ast.parse(source)), source


def test_an_alias_chain_resolves_regardless_of_visit_order() -> None:
    """`_own_nodes` yields `ast.walk` order, which is breadth-first, not
    source order -- so an assignment inside an `if` is visited *after* one
    at the top of the function that reads it. One pass adds `q` never; the
    fixed point is what makes the write below reportable, and this is the
    only shape that can tell the two apart."""
    source = ('def f():\n'
              '    if fresh:\n'
              '        p = CATALOG\n'
              '    q = p.parent\n'
              '    q.mkdir()\n')
    assert {"p", "q"} <= _aliases(ast.parse(source))
    assert _writes_to_catalog(ast.parse(source)) == [(5, ".mkdir()")]


def test_an_alias_does_not_escape_the_function_that_made_it() -> None:
    """The contagion that made the first draft useless. `p` is the catalog
    in `f` and somebody else's local in `g`; a scope-free pass reports the
    write in `g`, then everything reachable from it."""
    source = ('def f():\n    p = CATALOG\n    p.write_text(rows)\n'
              'def g():\n    p = Path("notes.md")\n    p.write_text(text)\n')
    hits = _writes_to_catalog(ast.parse(source))
    assert [line for line, _ in hits] == [3], hits


def test_an_unrelated_binding_is_not_an_alias() -> None:
    assert _aliases(ast.parse('p = 1\nq = Path("x")\nr = home / "y"')) == \
        frozenset()


@pytest.mark.parametrize("source", [
    'CATALOG.write_text(uuid.uuid4().hex)',
    'CATALOG.write_text(f"{root},{uuid4()}")',
    'project_catalog_path().write_text(str(uuid.uuid1()))',
])
def test_minting_an_id_into_the_catalog_is_reported(source: str) -> None:
    assert _mints_a_project_id(ast.parse(source)), source


# ── negative controls: the legitimate spellings pass ────────────
@pytest.mark.parametrize("source", [
    'rows = CATALOG.read_text()',
    'with open(CATALOG, "r", encoding="utf-8") as fh:\n    pass',
    'open(catalog, encoding="utf-8").read()',
    'open(catalog, mode="rb").read()',
    'agents.write_text(block)',
    'archive.write_bytes(original)',
    'Path(root, "AGENTS.md").write_text(block)',
    'catalogue_of_songs = 1',
    'handoff.write_text(text)',
], ids=range(9))
def test_a_read_or_an_unrelated_write_is_not_reported(source: str) -> None:
    assert _writes_to_catalog(ast.parse(source)) == [], source


@pytest.mark.parametrize("source", [
    'tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")',
    'trace_id = uuid.uuid4().hex[:16]',
    'instance.claim(uuid.uuid4().hex)',
    'agents.write_text(uuid.uuid4().hex)',
    'CATALOG.write_text(rows)',
    'catalog.write_bytes(existing)',
    'tmp = uuid.uuid4().hex\nCATALOG.write_text(rows)',
    'CATALOG.write_text(rows)\nlog(uuid.uuid4().hex)',
])
def test_an_unrelated_uuid_is_not_reported(source: str) -> None:
    """`uuid4()` is not the offence, and neither is writing the catalog.
    Minting an id *into* the catalog is, so both halves have to be present
    and the uuid has to be inside the write -- not merely nearby, which is
    the whole content of the containment check."""
    assert _mints_a_project_id(ast.parse(source)) == [], source


def test_a_name_that_merely_contains_catalog_is_not_the_catalog() -> None:
    """`catalogue_of_songs.write_text(...)` is somebody's music library.

    A substring match over source lines reports it, which is the first thing
    a maintainer would have to add an exemption for -- and an exemption list
    is where the next real one hides.
    """
    assert _writes_to_catalog(ast.parse('catalogue.write_text(x)')) == []
    assert _writes_to_catalog(ast.parse('my_catalog.write_text(x)')) == []
    assert _writes_to_catalog(ast.parse('catalog.write_text(x)')) != []


def test_a_comment_or_docstring_naming_the_catalog_is_not_a_write() -> None:
    """This file's own docstring says `catalog.write_text`. A text scan
    reports itself, which is how a scan acquires its first exemption."""
    source = ('"""Do not let anything call catalog.write_text(rows)."""\n'
              '# catalog.write_bytes(b"x") would be wrong\n'
              'x = 1\n')
    assert _writes_to_catalog(ast.parse(source)) == []
