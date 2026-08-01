"""A presence probe that cannot tell must not answer "absent", anywhere.

``Path.exists``, ``Path.is_dir`` and ``Path.is_file`` are two-valued answers
to a three-valued question. They get it wrong in both directions on the
interpreters this project supports, and
:func:`install_manifest.path_present` documents the full argument: they
*raise* on a permission denial, and they *return False* for a dangling
symlink, a symlink loop, a drive that exists but is not ready, and WINERROR
21. False is the one answer that lets a caller write, delete, or report
nothing found.

This repository has been removing that bug one module at a time for weeks,
and the removals kept not generalising:

* ``copilot_operator.manage_logs`` learned that an unexaminable log directory
  is not an empty one, and got a test saying so. ``operator_ingest.ingest_all``
  answered the same question about the same directory with a bare ``is_dir``
  and returned "no logs" — and ``copilot_operator._log_files``, the corrected
  version, sits nine lines from the call.
* ``install_manifest`` and ``project_paths`` guard every probe they make and
  explain the polarity in the docstring. ``setup_tools`` enumerated the skills
  and extensions it deploys with ``if not root.is_dir(): return []``, so an
  unreadable ``skills/`` was a repository that ships no skills; setup then
  installed nothing and reported success.
* ``backfill_unknown_metrics.backup_path`` promised in its first line to never
  overwrite an earlier backup, and asked ``Path.exists`` — which answers False
  for a backup that is there but unexaminable, so the file it destroyed was
  the one the function existed to preserve.

Three modules, one bug, found three times because nothing was looking. This
scan is the thing that looks. It is the direct counterpart of
``tests/test_shell_bash32_conformance.py``, which exists because a tripwire
that read one file let the same construct survive in another: **a rule
enforced against one file is not a rule, it is that file's history.**

**What it demands.** Not that these methods are never called — plenty of
calls are fine. It demands that every call be either

* lexically inside a ``try`` that catches ``OSError``, so "cannot tell"
  reaches a branch that can express it, or
* annotated ``# probe-ok: <reason>``, which is a claim that a wrong False is
  harmless *here* and a place to say why.

The annotation is deliberately cheap to write and impossible to write
silently: it appears in the diff, it carries a reason, and this file's
``test_every_annotation_carries_a_reason`` refuses an empty one. The point is
not to make the probe unreachable, it is to make choosing it deliberate.

**Why static.** The same argument as the bash 3.2 scan. These are not proxies
for the defect; the two-valued return *is* the defect, and there is no way to
introduce it without calling one of these three names. A behavioural test can
only find the instance someone already suspected, which is precisely how the
three above survived — each had a passing test suite around it.

Every detector below has a positive control asserting it fires and a negative
control asserting the correct spellings still pass. A detector broken into
matching nothing reports the whole tree clean, which reads exactly like
success, and that is the failure this file exists to prevent.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PROBES = ("exists", "is_dir", "is_file")

#: Exception names that mean the ``try`` can express "cannot tell".
CATCHES = frozenset({"OSError", "EnvironmentError", "IOError",
                     "Exception", "BaseException"})

ANNOTATION = re.compile(r"#\s*probe-ok:(?P<reason>.*)$")

#: The shortest reason worth writing. Anything below this is a silencer
#: wearing an annotation's clothes.
MIN_REASON = 12

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist")


def _shipped_modules() -> list[Path]:
    """The Python this repository ships and runs.

    Top-level ``*.py`` is the whole of it: ``extensions/`` is JavaScript,
    ``skills/`` is Markdown, and nothing under a subdirectory is imported at
    runtime. The population is *discovered*, not listed, so a new module is
    covered the day it is added rather than the day someone remembers.

    ``tests/`` is excluded, and the exclusion is a judgement rather than an
    oversight, so it is named here where it can be argued with. A probe in a
    test is usually the assertion itself — ``assert dest.exists()`` — and a
    wrong False there fails the test loudly and immediately, which is the same
    standard the ``probe-ok`` annotations are held to. The cost of including
    them would be several hundred annotations that all say the same thing,
    and an exemption list that large stops being read.

    Filtered on the path *relative to the repo*. Every agent on this project
    works in ``<repo>/.worktrees/<branch>/``, so an absolute-path filter
    matches ``.worktrees`` for every file in the tree, the population comes
    back empty, and an empty population passes every "no module contains X"
    assertion in this file. That is not hypothetical — it is what the sibling
    bash scan did when it was first written.
    """
    return sorted(
        p for p in REPO.glob("*.py")
        if not any(part in NOT_SOURCE for part in p.relative_to(REPO).parts)
    )


def _handles_oserror(handler: ast.ExceptHandler) -> bool:
    """Whether ``except`` clause ``handler`` catches a filesystem failure."""
    node = handler.type
    if node is None:  # bare `except:`
        return True
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    names = []
    for part in parts:
        if isinstance(part, ast.Name):
            names.append(part.id)
        elif isinstance(part, ast.Attribute):
            names.append(part.attr)
    return any(name in CATCHES for name in names)


class _ProbeFinder(ast.NodeVisitor):
    """Collect ``(lineno, attr, span)`` for probes not inside an OSError guard.

    Guarding is judged lexically — the probe is inside the ``try`` *body* of a
    ``try`` that catches ``OSError``. A handler or ``finally`` block is not a
    guard; code there runs after the failure and can raise its own.

    ``span`` is the line range an annotation may occupy: the header of the
    innermost statement holding the probe. A call wrapped across three lines
    is one decision and takes one annotation, but a compound statement's span
    stops before its body so that a reason written about an ``if`` cannot
    silently license a probe several lines inside it.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.span: tuple[int, int] | None = None
        self.hits: list[tuple[int, str, tuple[int, int]]] = []

    def visit_Try(self, node: ast.Try) -> None:
        guards = any(_handles_oserror(h) for h in node.handlers)
        self.depth += bool(guards)
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= bool(guards)
        for handler in node.handlers:
            self.visit(handler)
        for stmt in [*node.orelse, *node.finalbody]:
            self.visit(stmt)

    # `try/except*` is a different node with the same shape.
    visit_TryStar = visit_Try

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.stmt) and not isinstance(node, ast.Try):
            outer = self.span
            self.span = _header_span(node)
            super().generic_visit(node)
            self.span = outer
            return
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (isinstance(func, ast.Attribute)
                and func.attr in PROBES
                and not node.args
                and not node.keywords
                and self.depth == 0):
            span = self.span or (node.lineno, node.lineno)
            self.hits.append((node.lineno, func.attr, span))
        self.generic_visit(node)


def _header_span(node: ast.stmt) -> tuple[int, int]:
    """The lines of ``node`` that are not part of a nested block."""
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    body = getattr(node, "body", None)
    if body:
        end = min(end, body[0].lineno - 1)
    return node.lineno, max(node.lineno, end)


def _annotation_in(lines: list[str], span: tuple[int, int]) -> str | None:
    """The ``probe-ok`` reason covering ``span``, if there is one.

    Accepted anywhere in the statement's own header, or in the contiguous
    comment block directly above it. The block rather than a single line
    because a reason worth writing rarely fits in one, and contiguous rather
    than "nearby" because a blank line or a statement between them means the
    comment is about something else — which is the difference between an
    annotation and a reason that drifted onto the next call somebody added.
    """
    start, end = span
    candidates = list(range(start, end + 1))
    above = start - 1
    while above >= 1 and lines[above - 1].lstrip().startswith("#"):
        candidates.append(above)
        above -= 1
    for candidate in candidates:
        if not 1 <= candidate <= len(lines):
            continue
        found = ANNOTATION.search(lines[candidate - 1])
        if found:
            return found.group("reason").strip()
    return None


def unguarded_probes(source: str) -> list[tuple[int, str]]:
    """Every probe in ``source`` that is neither guarded nor annotated."""
    lines = source.splitlines()
    finder = _ProbeFinder()
    finder.visit(ast.parse(source))
    return [(lineno, attr) for lineno, attr, span in finder.hits
            if _annotation_in(lines, span) is None]


def annotated_probes(source: str) -> list[tuple[int, str]]:
    """Every ``(lineno, reason)`` where a probe was annotated rather than fixed."""
    lines = source.splitlines()
    finder = _ProbeFinder()
    finder.visit(ast.parse(source))
    out = []
    for lineno, _attr, span in finder.hits:
        reason = _annotation_in(lines, span)
        if reason is not None:
            out.append((lineno, reason))
    return out


#: Modules with unguarded probes that a *different* live branch is fixing, and
#: the exact number each still has. Not an exemption — an exemption would let
#: the count grow. The assertion below is equality, so this register fails
#: when new debt arrives *and* when the fix lands, and the second failure is
#: the only thing that ever gets an entry removed. Nothing here is allowed to
#: age quietly.
KNOWN_UNFIXED = {
    "operator_mail.py": (
        4,
        "`fix/mail-unreadable-inbox` replaces all four: a mailbox that cannot "
        "be read is being reported as a mailbox with no mail. Annotating them "
        "here would be a false claim about known-broken code, and editing "
        "them would collide with that branch. When it merges this entry "
        "fails — delete it then.",
    ),
}


# ── the scan ────────────────────────────────────────────────────
@pytest.mark.parametrize("module", _shipped_modules(), ids=lambda p: p.name)
def test_no_unguarded_presence_probe(module: Path) -> None:
    if module.name in KNOWN_UNFIXED:
        pytest.skip(f"tracked in KNOWN_UNFIXED: {KNOWN_UNFIXED[module.name][1]}")
    source = module.read_text(encoding="utf-8")
    hits = unguarded_probes(source)
    lines = source.splitlines()
    detail = "\n".join(
        f"    {module.name}:{lineno}  .{attr}()   {lines[lineno - 1].strip()}"
        for lineno, attr in hits
    )
    assert not hits, (
        f"{module.name} asks a three-valued question with a two-valued call "
        f"{len(hits)} time(s). `.exists()`, `.is_dir()` and `.is_file()` "
        f"answer False for a path that is occupied but unexaminable, and "
        f"raise on a permission denial:\n{detail}\n"
        f"  Use install_manifest.path_present() and decide the None case, or "
        f"wrap the probe in try/except OSError, or - if a wrong False is "
        f"genuinely harmless here - annotate the line `# probe-ok: <reason>`."
    )


@pytest.mark.parametrize("name", sorted(KNOWN_UNFIXED))
def test_the_known_unfixed_register_is_exact(name: str) -> None:
    expected, reason = KNOWN_UNFIXED[name]
    module = REPO / name
    assert module.is_file(), f"{name} is registered but no longer exists"
    actual = len(unguarded_probes(module.read_text(encoding="utf-8")))
    assert actual == expected, (
        f"{name} has {actual} unguarded presence probes, the register says "
        f"{expected}.\n  If it went up, the register is being used as a "
        f"licence: fix or annotate the new one.\n  If it went down, the fix "
        f"landed — remove this entry so the module rejoins the scan.\n"
        f"  Registered because: {reason}"
    )


def test_the_population_is_not_empty_and_holds_the_modules_we_ship() -> None:
    """An empty population passes every assertion above it.

    The scan's own blind spot, asserted against rather than hoped about. The
    named modules are the ones that have actually carried this bug, so a
    filter that stops reaching them is a filter that has stopped working.
    """
    names = {p.name for p in _shipped_modules()}
    assert len(names) >= 10, f"the scan found almost nothing to read: {names}"
    for expected in ("setup_tools.py", "install_manifest.py",
                     "operator_ingest.py", "copilot_operator.py",
                     "backfill_unknown_metrics.py", "project_paths.py"):
        assert expected in names, (
            f"{expected} has held this defect before and is no longer being "
            f"scanned; the population filter is wrong"
        )


def test_every_annotation_carries_a_reason() -> None:
    """`# probe-ok:` with nothing after it is a silencer, not a judgement."""
    for module in _shipped_modules():
        for lineno, reason in annotated_probes(
                module.read_text(encoding="utf-8")):
            assert len(reason) >= MIN_REASON, (
                f"{module.name}:{lineno} silences the probe scan without "
                f"saying why (reason: {reason!r}). Say what makes a wrong "
                f"False harmless at this call."
            )


# ── controls ────────────────────────────────────────────────────
#: Source that must trip the detector, one spelling per entry.
FIRES = {
    "bare exists": "from pathlib import Path\nif Path('x').exists():\n    pass\n",
    "bare is_dir": "from pathlib import Path\nif Path('x').is_dir():\n    pass\n",
    "bare is_file": "from pathlib import Path\nif Path('x').is_file():\n    pass\n",
    "inside a comprehension": (
        "from pathlib import Path\n"
        "xs = [p for p in Path('x').iterdir() if p.is_dir()]\n"
    ),
    "inside a nested function": (
        "from pathlib import Path\n"
        "def outer():\n"
        "    def inner(p):\n"
        "        return p.is_file()\n"
        "    return inner\n"
    ),
    "in a try that catches something else": (
        "from pathlib import Path\n"
        "try:\n"
        "    Path('x').exists()\n"
        "except ValueError:\n"
        "    pass\n"
    ),
    "in the handler rather than the body": (
        "from pathlib import Path\n"
        "try:\n"
        "    pass\n"
        "except OSError:\n"
        "    Path('x').is_dir()\n"
    ),
    "in the finally rather than the body": (
        "from pathlib import Path\n"
        "try:\n"
        "    pass\n"
        "except OSError:\n"
        "    pass\n"
        "finally:\n"
        "    Path('x').is_dir()\n"
    ),
}

#: Source that must NOT trip it. Each is a spelling the repo actually uses.
PASSES = {
    "guarded by OSError": (
        "from pathlib import Path\n"
        "try:\n"
        "    Path('x').is_dir()\n"
        "except OSError:\n"
        "    pass\n"
    ),
    "guarded by a tuple including OSError": (
        "from pathlib import Path\n"
        "try:\n"
        "    Path('x').is_dir()\n"
        "except (ValueError, OSError):\n"
        "    pass\n"
    ),
    "guarded by a bare except": (
        "from pathlib import Path\n"
        "try:\n"
        "    Path('x').is_dir()\n"
        "except:\n"
        "    pass\n"
    ),
    "guarded further out": (
        "from pathlib import Path\n"
        "try:\n"
        "    if True:\n"
        "        for _ in range(1):\n"
        "            Path('x').is_file()\n"
        "except OSError:\n"
        "    pass\n"
    ),
    "annotated on the line": (
        "from pathlib import Path\n"
        "if Path('x').exists():  # probe-ok: a wrong False costs one retry\n"
        "    pass\n"
    ),
    "annotated on the line above": (
        "from pathlib import Path\n"
        "# probe-ok: a wrong False costs one retry and nothing else\n"
        "if Path('x').exists():\n"
        "    pass\n"
    ),
    "annotated inside a wrapped call": (
        "from pathlib import Path\n"
        "print('x',\n"
        "      Path('x').exists(),  # probe-ok: one decision, one annotation\n"
        "      Path('y').exists())\n"
    ),
    "path_present instead": (
        "import install_manifest\n"
        "from pathlib import Path\n"
        "if install_manifest.path_present(Path('x')) is False:\n"
        "    pass\n"
    ),
    "an unrelated exists with arguments": (
        "import os\n"
        "if os.path.exists('x'):\n"
        "    pass\n"
    ),
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_every_detector_fires_on_source_that_has_the_defect(name: str) -> None:
    assert unguarded_probes(FIRES[name]), (
        f"the detector did not fire on {name!r}. A detector that matches "
        f"nothing reports the whole tree clean, which reads exactly like "
        f"success."
    )


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_correct_spellings_still_pass(name: str) -> None:
    assert not unguarded_probes(PASSES[name]), (
        f"the detector fired on {name!r}, which is the shape this repo asks "
        f"for. A scan that rejects the fix teaches people to disable it."
    )


def test_an_empty_annotation_is_rejected() -> None:
    """The control for ``test_every_annotation_carries_a_reason``."""
    source = ("from pathlib import Path\n"
              "if Path('x').exists():  # probe-ok:\n"
              "    pass\n")
    assert not unguarded_probes(source), \
        "premise: an empty annotation still counts as annotated"
    assert annotated_probes(source) == [(2, "")], \
        "the empty reason must be visible to the reason check"


def test_a_comment_two_lines_up_does_not_count() -> None:
    """The annotation is local or it is not an annotation.

    Allowing it to drift means a reason written about one call silently
    licenses the next one someone adds below it.
    """
    source = ("from pathlib import Path\n"
              "# probe-ok: this reason is about something else entirely\n"
              "x = 1\n"
              "if Path('x').exists():\n"
              "    pass\n")
    assert unguarded_probes(source) == [(4, "exists")]


def test_an_annotation_on_an_if_does_not_reach_into_its_body() -> None:
    """A compound statement's span stops before its body.

    Otherwise one reason on an ``if`` licenses every probe anybody later adds
    inside the block, which is how an annotation stops being a judgement about
    a call and becomes a switch for a region.
    """
    source = ("from pathlib import Path\n"
              "if True:  # probe-ok: this reason is about the header only\n"
              "    if Path('x').exists():\n"
              "        pass\n")
    assert unguarded_probes(source) == [(3, "exists")]
