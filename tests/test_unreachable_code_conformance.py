"""No statement may follow ``return``, ``raise``, ``continue`` or ``break``.

This file exists because of a defect in the commit that introduced its
sibling, ``test_presence_probe_conformance.py``: the entire body of
``setup_tools._dir_entries`` was pasted twice, so ten lines sat after an
unconditional ``return`` and could never run.

Every property that made that bug cheap to make also made it invisible:

* **It is behaviourally inert.** The live half ran, the tests passed, and
  coverage of the reachable code was total. There is no input that
  distinguishes the file from a correct one.
* **The dead half was a perfect copy.** In review it reads as the function,
  because it *is* the function. Nothing looks wrong at a glance; the only
  tell is the ``return`` above it.
* **Nothing in this repository could see it.** There is no linter here — CI
  runs pytest and nothing else — and pyflakes and ruff's default rules do not
  report statements after a ``return``. It was found by two humans-in-the-loop
  reading the diff, on the one function the commit was named for.

That last point is the whole argument. A defect class that only careful
reading catches is a defect class that ships the week nobody reads carefully,
and this one had already survived a full test suite, a static scan written in
the same commit, and the author's own re-read.

**What it demands.** That within any block of statements, a terminator is the
last one. This is not a style rule about early returns — a terminator
anywhere else is fine, and common. It is that nothing may be *written after*
one in the same block, because nothing written there executes.

**Why static, and why so narrow.** Reachability is undecidable in general and
this scan does not attempt it: it says nothing about ``sys.exit()``, about
``while True`` without a ``break``, or about a condition that happens to be
always false. Those need a model of what the code means. This needs only the
shape of the syntax tree, and the shape is conclusive — no interpreter, no
platform, and no input can reach a statement that follows a ``return`` in the
same block. A narrow check that is never wrong is worth more than a broad one
that has to be argued with, because a scan people argue with gets an
exemption list, and an exemption list is where the next one hides.

The detector has a positive control asserting it fires and negative controls
asserting the legitimate spellings still pass. A detector broken into
matching nothing reports the whole tree clean, which reads exactly like
success.
"""
import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Statements after which control cannot continue in the same block.
TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist", ".venv", "venv")

#: The statement lists a node can own. ``handlers`` holds ``ExceptHandler``
#: nodes rather than statements, and each of those is itself visited by
#: ``ast.walk``, so its own ``body`` is reached on its own turn.
BLOCKS = ("body", "orelse", "finalbody")


def _python_sources() -> list[Path]:
    """Every ``*.py`` in the repository, discovered rather than listed.

    Unlike the presence-probe scan this covers ``tests/`` too. That scan
    excludes them because a probe in a test is a probe against a fixture the
    test just built, where "cannot tell" is not a state the fixture can be
    in. Unreachable code has no such exemption: a test whose assertions sit
    after a ``return`` passes for the wrong reason, and passing for the wrong
    reason is the failure this suite is most concerned with everywhere else.
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


def unreachable(source: str) -> list[tuple[int, str, str]]:
    """``(line, terminator, what follows)`` for each statement that cannot run.

    Only the *first* orphan in a block is reported. The rest are unreachable
    for the same reason, and one line number is what a reader needs to find
    the ``return``.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        for name in BLOCKS:
            block = getattr(node, name, None)
            if not isinstance(block, list):
                continue
            for statement, following in zip(block, block[1:]):
                if isinstance(statement, TERMINATORS):
                    found.append((following.lineno,
                                  type(statement).__name__.lower(),
                                  type(following).__name__))
                    break
    return sorted(found)


@pytest.mark.parametrize("path", _python_sources(),
                         ids=lambda p: str(p.relative_to(REPO)).replace("\\", "/"))
def test_no_statement_follows_a_terminator(path):
    orphans = unreachable(path.read_text(encoding="utf-8"))
    assert not orphans, "\n".join(
        f"{path.relative_to(REPO)}:{line}: {kind} on the line above ends the "
        f"block; this {node} cannot run"
        for line, kind, node in orphans
    )


def test_the_population_is_not_empty_and_holds_what_we_ship():
    """A filter bug that empties the population passes every test above it."""
    found = {str(p.relative_to(REPO)).replace("\\", "/")
             for p in _python_sources()}
    assert len(found) > 20, f"suspiciously few Python files: {sorted(found)}"
    for expected in ("setup_tools.py", "operator_ingest.py",
                     "copilot_operator.py", "install_manifest.py",
                     "tests/test_presence_probe_conformance.py"):
        assert expected in found, f"{expected} missing from the scan"


#: The shape that shipped, plus one per terminator. Each must be reported.
FIRES = {
    "duplicated function body": """
def f(root):
    entries = sorted(root.iterdir())
    return entries
    entries = sorted(root.iterdir())
    return entries
""",
    "after raise": """
def f():
    raise ValueError("no")
    cleanup()
""",
    "after continue": """
for x in items:
    if x:
        continue
        never()
""",
    "after break": """
while True:
    break
    never()
""",
    "in an except handler": """
try:
    go()
except OSError:
    return None
    log("unreached")
""",
    "in a finally block": """
try:
    go()
finally:
    return 1
    log("unreached")
""",
    "in an else clause": """
def f(x):
    if x:
        return 1
    else:
        return 2
        log("unreached")
""",
    "at module level": """
import sys
raise SystemExit(1)
print("unreached")
""",
}

#: Spellings that are correct and must not be reported. An early return, a
#: terminator in one branch of a conditional, and a terminator that ends a
#: nested block while the outer block continues are all ordinary code.
PASSES = {
    "early return": """
def f(x):
    if not x:
        return None
    return x * 2
""",
    "terminator ends each branch": """
def f(x):
    if x:
        return 1
    return 2
""",
    "nested block ends, outer continues": """
def f(items):
    for x in items:
        if x:
            continue
        touch(x)
    return len(items)
""",
    "raise inside a handler, function continues after the try": """
def f():
    try:
        go()
    except OSError:
        raise RuntimeError("wrapped")
    return "done"
""",
    "return in a nested function": """
def outer():
    def inner():
        return 1
    return inner
""",
    "break ends the loop body, code follows the loop": """
def f(items):
    for x in items:
        if x:
            break
    return items
""",
    "docstring then return": """
def f():
    \"\"\"Doc.\"\"\"
    return 1
""",
    "terminator last in a with block": """
def f(path):
    with open(path) as handle:
        return handle.read()
""",
    "match case ending in return": """
def f(x):
    match x:
        case 1:
            return "one"
        case _:
            return "other"
""",
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_the_detector_fires(name):
    assert unreachable(FIRES[name]), (
        f"the detector did not report {name!r}; a detector that matches "
        "nothing reports the whole tree clean"
    )


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_detector_leaves_correct_code_alone(name):
    assert not unreachable(PASSES[name]), (
        f"{name!r} is ordinary correct code and was reported"
    )


def test_the_first_orphan_in_a_block_is_reported_once():
    """Three dead statements in one block are one finding, not three."""
    found = unreachable("""
def f():
    return 1
    a()
    b()
    c()
""")
    assert len(found) == 1, found
    line, kind, node = found[0]
    assert kind == "return"
    assert node == "Expr"
