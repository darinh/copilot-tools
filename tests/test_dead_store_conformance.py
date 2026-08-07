"""A module-level name may not be rebound before its first value is read.

This file exists because of a defect in the commit that added the handoff
contention notices: an edit that rewrote two of the three ``NOTICE_*``
constants pasted the third one back in as well, so ``handoff_tool.py`` ended
up with two identical, adjacent, unconditional definitions of
``NOTICE_BANKED_UNPUBLISHED``. The second won. The first could never be read
by anything. (Those constants have since been deleted outright -- re-keying
the handoff by instance removed the contention they described. The scan
outlives them, because the shape it detects has nothing to do with handoffs.)

It is the sibling of ``test_unreachable_code_conformance.py`` and it is here
for the same reason, one scope up. Every property that made *that* bug
invisible is present again:

* **It is behaviourally inert.** The surviving binding is the correct one, so
  no input distinguishes the file from a correct one. The full suite -- 1747
  tests -- was green with the duplicate in place.
* **The dead half is a perfect copy.** It reads as the constant, because it
  *is* the constant.
* **Nothing here could see it.** There is no linter in this repository; CI
  runs pytest. Three adversarial reviewers had read the file, though it is
  fair to say the duplicate arrived after their round -- which is the point,
  because the next one will too.

It was found by a mutation harness refusing to score a mutation whose anchor
matched twice, which is a piece of luck dressed up as a method: the anchor
happened to be that constant. Nothing else in this repository would have
reported it.

**What it demands.** That within a module's own top-level body, a name is not
unconditionally rebound while the value from its previous unconditional
binding has never been loaded. That is a dead store, and at module scope --
where these are constants, and where nothing loops -- it is never anything
else.

**Why so narrow.** Liveness in general needs a control-flow model, and this
scan does not have one and does not want one. It looks only at statements
that are *direct children of the module*, so every conditional spelling is
out of scope by construction: a ``try/except ImportError`` fallback, an
``if IS_WINDOWS`` branch and a ``for`` loop target are all bindings the module
body does not perform itself, and none of them is reported. It looks only at
bare ``Name`` targets, so tuple unpacking and attribute assignment are out
too. What is left is the shape that has no defensible spelling: two
unconditional assignments in a row, the first of which nothing between them
reads.

The narrowness is what makes it worth having. Measured across this
repository, in the 57 first-party Python files, it reported exactly one
instance -- the defect above -- and nothing else. A scan people argue with
gets an exemption list, and an exemption list is where the next one hides, so
this one has no annotation and no escape hatch.

The detector has positive controls asserting it fires and negative controls
asserting the legitimate spellings still pass. A detector broken into
matching nothing reports the whole tree clean, which reads exactly like
success.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist", ".venv", "venv")


def _python_sources() -> list[Path]:
    """Every ``*.py`` in the repository, discovered rather than listed.

    ``tests/`` is included. A duplicated constant in a test file is worse
    than one in a module, not better: the dead half is what a reader believes
    the test is using.
    """
    out = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        # Filter on the repo-relative path. An absolute-path filter matches
        # ".worktrees" for every file when the checkout *is* a worktree, and
        # an empty population passes every assertion below.
        if any(part in NOT_SOURCE for part in rel.parts):
            continue
        out.append(path)
    return sorted(out)


def _bound(statement: ast.stmt) -> list[str]:
    """Bare names this statement unconditionally binds, if any.

    ``Assign`` with ``Name`` targets and ``AnnAssign`` carrying a value. An
    ``AnnAssign`` *without* a value binds nothing -- ``x: int`` is a
    declaration -- so it is not a store and cannot be a dead one.

    Augmented assignment is deliberately absent: ``x += 1`` reads ``x``
    before it writes it, so it can never be the second half of a dead store,
    and treating it as a binding would be harmless but would say something
    untrue about what it does.
    """
    if isinstance(statement, ast.Assign):
        return [t.id for t in statement.targets if isinstance(t, ast.Name)]
    if isinstance(statement, ast.AnnAssign) and statement.value is not None:
        if isinstance(statement.target, ast.Name):
            return [statement.target.id]
    return []


def _referenced(statement: ast.stmt) -> set[str]:
    """Every name this statement reads, deletes, or declares global.

    Any of those makes the previous value observable, or makes the question
    of liveness one this scan is not equipped to answer -- so any of them
    clears the debt. The walk descends into nested functions and classes on
    purpose: a ``def`` that closes over a module constant is a statement that
    reads it, as far as this scan is concerned, and resolving *when* it reads
    it needs the control-flow model this scan does not have.
    """
    seen = set()
    for node in ast.walk(statement):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            seen.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            seen.update(node.names)
    return seen


def dead_stores(source: str) -> list[tuple[int, str, int]]:
    """``(line, name, line of the binding it kills)`` for each dead store.

    Only the module's own top-level body is examined. A binding inside an
    ``if``, a ``try`` or a loop is not a statement the module body performs,
    and reporting it would require knowing whether the branch is taken.
    """
    found = []
    pending: dict[str, int] = {}
    for statement in ast.parse(source).body:
        for name in _referenced(statement):
            pending.pop(name, None)
        for name in _bound(statement):
            if name in pending:
                found.append((statement.lineno, name, pending[name]))
            pending[name] = statement.lineno
    return sorted(found)


@pytest.mark.parametrize("path", _python_sources(),
                         ids=lambda p: str(p.relative_to(REPO)).replace("\\", "/"))
def test_no_module_level_binding_is_dead(path):
    dead = dead_stores(path.read_text(encoding="utf-8"))
    assert not dead, "\n".join(
        f"{path.relative_to(REPO)}:{line}: {name} is rebound here and the "
        f"binding at line {killed} was never read; that first one is dead"
        for line, name, killed in dead
    )


def test_the_population_is_not_empty_and_holds_what_we_ship():
    """A filter bug that empties the population passes every test above it."""
    found = {str(p.relative_to(REPO)).replace("\\", "/")
             for p in _python_sources()}
    assert len(found) > 20, f"suspiciously few Python files: {sorted(found)}"
    for expected in ("handoff_tool.py", "setup_tools.py",
                     "copilot_operator.py", "install_manifest.py",
                     "tests/test_unreachable_code_conformance.py"):
        assert expected in found, f"{expected} missing from the scan"


#: The shape that shipped, and the near neighbours it has.
FIRES = {
    "the constant that shipped twice": """
NOTICE = (
    "> a notice\\n"
    "> over two lines"
)
NOTICE = (
    "> a notice\\n"
    "> over two lines"
)
""",
    "adjacent rebinding with different values": """
TIMEOUT = 10.0
TIMEOUT = 30.0
""",
    "separated by statements that read something else": """
A = 1
B = 2
C = B + 1
A = 4
""",
    "annotated assignment overwritten": """
LIMIT: int = 1
LIMIT: int = 2
""",
    "plain assignment overwritten by an annotated one": """
LIMIT = 1
LIMIT: int = 2
""",
    "a read of a different name does not save it": """
PATH = "a"
OTHER = PATH
PATH = "b"
PATH = "c"
""",
}

#: Spellings that are correct and must not be reported.
PASSES = {
    "rebound after being read": """
A = 1
B = A
A = 2
""",
    "augmented assignment reads before it writes": """
A = 1
A += 1
""",
    "read by a function defined in between": """
A = 1
def f():
    return A
A = 2
""",
    "read by a class body in between": """
A = 1
class C:
    x = A
A = 2
""",
    "conditional fallback for an optional import": """
try:
    from fast import loads
except ImportError:
    loads = None
""",
    "platform branch": """
import sys
SEP = "/"
if sys.platform == "win32":
    SEP = "\\\\"
""",
    "rebinding inside a function is a different scope": """
A = 1
def f():
    A = 2
    A = 3
    return 0
""",
    "loop variable reusing a module name": """
ITEMS = [1, 2]
for ITEM in ITEMS:
    print(ITEM)
""",
    "tuple unpacking is out of scope": """
A, B = 1, 2
A, B = 3, 4
""",
    "attribute assignment is not a name binding": """
import os
os.environ["X"] = "1"
os.environ["X"] = "2"
""",
    "a bare annotation declares and does not bind": """
A: int
A = 1
""",
    "deleted before being rebound": """
A = 1
del A
A = 2
""",
    "two different names": """
A = 1
B = 2
""",
    "read inside an f-string": """
A = "x"
MSG = f"{A}!"
A = "y"
""",
    "read by a decorator in between": """
CACHE = {}
@register(CACHE)
def f():
    pass
CACHE = {}
""",
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_the_detector_fires(name):
    assert dead_stores(FIRES[name]), (
        f"the detector did not report {name!r}; a detector that matches "
        "nothing reports the whole tree clean"
    )


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_detector_leaves_correct_code_alone(name):
    assert not dead_stores(PASSES[name]), (
        f"{name!r} is ordinary correct code and was reported"
    )


def test_the_report_names_both_lines():
    """A line number for the survivor is not enough to find the corpse.

    The whole difficulty of this defect is that the two look identical, so
    the finding has to say which one is dead and where the other is.
    """
    found = dead_stores("A = 1\nB = 2\nA = 3\n")
    assert found == [(3, "A", 1)], found


def test_three_bindings_report_two_deaths():
    """Each dead store is its own finding; only the last binding survives."""
    found = dead_stores("A = 1\nA = 2\nA = 3\n")
    assert found == [(2, "A", 1), (3, "A", 2)], found


def test_the_defect_this_file_was_written_for_is_gone():
    """The instance from the wild, asserted against the real file.

    A synthetic control proves the detector works. It does not prove the
    detector was ever pointed at the thing it was written for, and a scan
    whose population silently excludes its own motivating case is the failure
    mode this repository keeps rediscovering.

    The three ``NOTICE_*`` constants this originally pinned no longer exist:
    re-keying the handoff by instance removed the contention they described,
    and the notices with it. What that pin was really for was keeping this
    assertion non-vacuous -- ``dead_stores`` returns nothing for a file with
    no module-level bindings to examine, which reads exactly like a clean
    one -- so the population is asserted directly instead, and now says what
    it means.
    """
    source = (REPO / "handoff_tool.py").read_text(encoding="utf-8")
    assert not dead_stores(source)
    top_level_bindings = [
        node for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) for t in node.targets)
    ]
    assert len(top_level_bindings) >= 5, (
        "handoff_tool.py no longer has module-level constants, so the scan "
        "above passed without examining anything")
