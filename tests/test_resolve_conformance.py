"""``Path.resolve`` fails three ways, and a guard written for one is not a guard.

``resolve()`` is not a total function, and the three ways it fails do not
share a base class:

* a component that cannot be traversed raises ``OSError``;
* a symlink loop raises ``RuntimeError`` -- not an ``OSError``, on every
  interpreter this project supports;
* an embedded NUL raises ``ValueError`` from inside ``stat``, which the
  catalog and the log directory are both hand-editable enough to contain.

``except OSError`` therefore catches a third of the problem and reads like all
of it. That is a worse position than no guard at all, because the handler is
right there in the diff and looks like the decision was made.

This repository has paid for it three times, always the same way -- the fix
existed, in a module the next caller did not import:

* ``handoff_tool.resolved_str`` was written with all three handlers and a
  docstring explaining why, and stayed private to ``handoff_tool``.
* ``copilot_operator._dir_matches`` re-derived it and re-derived it wrong,
  resolving both sides of a comparison outside any guard. ``operator inbox``
  with no instance name ended in a traceback where it had a decision to make,
  and a parent that would not resolve made *every* comparison answer
  "elsewhere" -- so the instance census came back confidently empty and one
  agent read a peer's mail.
* ``copilot_operator.project_handoff_file`` had the same call, wholly
  unguarded, on the path that tells a restarting session where its handoff
  lives.
* ``operator_ingest.ingest_file`` still had a bare ``Path(logfile).resolve()``
  after all three were fixed. Its CLI catches ``FileNotFoundError`` and
  ``OSError`` -- two handlers written precisely so an unusable log gets a
  named refusal -- and the other two exceptions walked past both.

Four instances, one bug, found four times because nothing was looking. This
scan is the thing that looks, and it is the direct counterpart of
``tests/test_presence_probe_conformance.py`` and
``tests/test_shell_bash32_conformance.py``: **a rule enforced against one file
is not a rule, it is that file's history.**

**What it demands.** Not that ``resolve`` is never called. It demands that
every call be one of:

* lexically inside a ``try`` whose handlers cover *all three* exceptions --
  ``except (OSError, RuntimeError, ValueError)``, or a blanket ``Exception``,
  or a bare ``except``. Partial coverage is a hit, and it is the most
  interesting hit this scan produces, because partial coverage is what every
  instance above actually looked like;
* rooted at ``__file__``. ``Path(__file__).resolve().parent`` is how five
  modules find the directory they were imported from. It runs at import time
  on a path the interpreter has already opened, and there is no branch to
  write: if it fails the module does not exist. Exempting it is a judgement,
  so it is named here where it can be argued with rather than left implicit
  in a pattern;
* annotated ``# resolve-ok: <reason>``, which is a claim that a traceback out
  of this call is acceptable, and a place to say why.

The one-line fix is usually :func:`project_paths.resolved_str`, which catches
all three and falls back to a lexically absolute path. It lives in
``project_paths`` rather than in a caller for exactly the reason this file
exists -- it is the module both the writer and the reader import.

**What is deliberately not flagged.** ``os.path.realpath`` swallows its
errors rather than raising them, which makes it the wrong shape for this rule
and the right subject for a different one; and ``Path.absolute`` does no
filesystem access at all. Neither is the defect here.

**Why static.** The same argument as the two sibling scans. The narrow
handler *is* the defect rather than a proxy for it, and there is no way to
introduce it without writing this call. A behavioural test can only find the
instance someone already suspected, which is precisely how the four above
survived -- each had a passing suite around it, and two had a test asserting
the ``OSError`` branch worked.

Every detector below has a positive control asserting it fires and a negative
control asserting the correct spellings still pass. A detector broken into
matching nothing reports the whole tree clean, which reads exactly like
success, and that is the failure this file exists to prevent.
"""
import ast
import re

import pytest

# Imported rather than re-derived. Two answers to "which modules do we ship"
# or "where may an annotation sit" that drift apart is the same defect this
# file is about, one level up. The sibling scan owns them; if it is renamed
# this import fails at collection, which is loud, rather than quietly
# scanning nothing.
from test_presence_probe_conformance import (
    MIN_REASON,
    REPO,
    _annotation_context,
    _header_span,
    _is_pathlib_class,
    _shipped_modules,
)

ANNOTATION = re.compile(r"#\s*resolve-ok:(?P<reason>.*)$")

#: The three failure modes, as opaque tokens. A handler covers a token when it
#: names that exception or a blanket one; a call is guarded only when the
#: union of its enclosing guards covers all three.
FAILURES = frozenset({"oserror", "runtimeerror", "valueerror"})

#: Names that mean "and everything else", including the two non-OSError modes.
BLANKET = frozenset({"Exception", "BaseException"})

#: ``OSError`` and its aliases. Subclasses -- ``FileNotFoundError``,
#: ``PermissionError`` -- deliberately do not count: they name one errno and
#: leave the rest of the OSError family escaping, which is the same
#: too-narrow-handler defect in miniature.
OSERROR_NAMES = frozenset({"OSError", "EnvironmentError", "IOError",
                           "WindowsError"})

COVERS = {
    "ValueError": "valueerror",
    "RuntimeError": "runtimeerror",
}

#: How deep the receiver chain of a call may be walked looking for
#: ``__file__``. Bounded so a pathological expression cannot spin here.
_CHAIN_LIMIT = 20


def _handler_covers(handler: ast.ExceptHandler) -> frozenset:
    """Which of the three failure modes ``handler`` can express."""
    node = handler.type
    if node is None:  # bare `except:`
        return FAILURES
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    covered = set()
    for part in parts:
        if isinstance(part, ast.Name):
            name = part.id
        elif isinstance(part, ast.Attribute):
            name = part.attr
        else:
            continue
        if name in BLANKET:
            return FAILURES
        if name in OSERROR_NAMES:
            covered.add("oserror")
        elif name in COVERS:
            covered.add(COVERS[name])
    return frozenset(covered)


def _rooted_at_file(node: ast.expr) -> bool:
    """True when the receiver of a call is built from ``__file__``.

    Walks back down the receiver chain, through attribute access and through
    a pathlib constructor, so ``Path(__file__).parent.resolve()`` is
    recognised as the same thing ``Path(__file__).resolve()`` is.

    A method call in the chain is walked through its receiver, and **only
    when it took no positional argument**. Both halves matter, and the second
    was a real hole in the first draft of this file. Walking the receiver
    rather than the arguments stops ``Path(cfg).relative_to(__file__)`` being
    mistaken for the module's own location. Refusing to walk a call that took
    an argument stops the reverse: ``Path(__file__).joinpath(user).resolve()``
    and ``Path(__file__).with_name(user).resolve()`` are rooted at ``__file__``
    and then leave it, so the thing being resolved is whatever the argument
    said. Exempting those would have made the exemption a two-token bypass of
    the whole scan.
    A pathlib constructor is walked through its first argument, and **only
    when that is its only argument**. ``Path(__file__, user)`` joins a
    component onto the module's location, so what gets resolved is again not
    the module's location; exempting it would be the same hole as the one
    above wearing a comma.
    """
    for _ in range(_CHAIN_LIMIT):
        if isinstance(node, ast.Name):
            return node.id == "__file__"
        if isinstance(node, ast.Attribute):
            node = node.value
            continue
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and not node.args:
                node = func.value
                continue
            if (_is_pathlib_class(func) and len(node.args) == 1
                    and not node.keywords):
                node = node.args[0]
                continue
        return False
    return False


def _resolve_call(func: ast.expr, call: ast.Call) -> ast.expr | None:
    """The receiver of a ``resolve()`` call, or None if this is not one.

    Three spellings, because a rule enforced against one spelling is that
    spelling's history -- the same three the presence-probe scan closes:

    ``p.resolve()``
        The ordinary one.

    ``Path.resolve(p)``
        The unbound form, which has one argument rather than none and walks
        past a detector keyed on the bound shape.

    ``getattr(p, "resolve")()``
        Nobody writes this by accident, which is exactly why it is closed. A
        tripwire with a one-token bypass is a tripwire whose next violation
        is deliberate too.
    """
    if isinstance(func, ast.Call):
        inner = func
        if (isinstance(inner.func, ast.Name) and inner.func.id == "getattr"
                and len(inner.args) == 2
                and isinstance(inner.args[1], ast.Constant)
                and inner.args[1].value == "resolve"):
            return inner.args[0]
        return None
    if isinstance(func, ast.Attribute) and func.attr == "resolve":
        if not call.args:
            return func.value
        # `Path.resolve(p)` and `Path.resolve(p, True)`: the unbound form,
        # whose first positional argument is the receiver.
        if _is_pathlib_class(func.value):
            return call.args[0]
        # `p.resolve(True)`: the bound form with `strict` passed positionally.
        # Keying on "no arguments" missed it, and `strict=True` -- the same
        # call, spelled with a keyword -- was already caught, so the rule was
        # enforced against a spelling rather than against the call. The cost
        # of the wider net is a method named `resolve` on something that is
        # not a path; nothing in this repository has one, and the annotation
        # is there for the day something does.
        return func.value
    return None


class _ResolveFinder(ast.NodeVisitor):
    """Collect ``(lineno, missing, span)`` for every ``resolve()`` call.

    ``missing`` is the set of failure modes no enclosing guard can express;
    an empty set means the call is fully guarded. Guarding is judged
    lexically and cumulatively -- nested ``try`` statements contribute their
    handlers to the same call -- and only a ``try`` *body* guards. A handler
    or ``finally`` block runs after the failure and can raise its own.

    ``span`` is the line range an annotation may occupy, computed by the
    sibling scan's ``_header_span`` so that a reason written about an ``if``
    cannot silently license a call several lines inside it.

    A ``def``, ``lambda`` or generator expression nested in a guarded body
    clears the stack for its *deferred* part only, because that part does not
    run inside the ``try`` that lexically encloses it -- it runs wherever it
    is called or iterated, which is somewhere this scan cannot see. What is
    deferred is narrower than the node: a function's decorators, annotations
    and argument defaults all run at ``def`` time and are genuinely guarded,
    and a generator expression's outermost iterable is evaluated eagerly.
    Clearing the stack for the whole node would flag correct code, and a scan
    that flags correct code teaches people to silence it.

    ``ListComp``, ``SetComp`` and ``DictComp`` deliberately do not clear it.
    They look like a generator expression and they are not: they run to
    completion where they are written.
    """

    def __init__(self) -> None:
        self.stack: list = []
        self.span: tuple[int, int] | None = None
        #: Every resolve call seen, guarded or not -- the denominator that
        #: proves this walker reaches real code.
        self.seen: list[tuple[int, bool]] = []
        self.hits: list[tuple[int, frozenset, tuple[int, int]]] = []

    def visit_Try(self, node: ast.Try) -> None:
        covered = set()
        for handler in node.handlers:
            covered |= _handler_covers(handler)
        self.stack.append(frozenset(covered))
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()
        for handler in node.handlers:
            self.visit(handler)
        for stmt in [*node.orelse, *node.finalbody]:
            self.visit(stmt)

    # `try/except*` is a different node with the same shape.
    visit_TryStar = visit_Try

    def _deferred(self, nodes) -> None:
        """Visit ``nodes`` as though no enclosing ``try`` were in effect."""
        outer = self.stack
        self.stack = []
        for child in nodes:
            self.visit(child)
        self.stack = outer

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Argument defaults are evaluated where the lambda is written.
        self.visit(node.args)
        self._deferred([node.body])

    def visit_FunctionDef(self, node) -> None:
        outer = self.span
        self.span = _header_span(node)
        for decorator in node.decorator_list:
            self.visit(decorator)
        # `ast.arguments` carries the defaults and the annotations, all of
        # which are evaluated at def time and are therefore guarded.
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self._deferred(node.body)
        self.span = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """A genexp is a lambda that looks like a comprehension.

        Its body runs when something iterates it, which may be after the
        ``try`` it was written in has exited -- so ``(p.resolve() for p in
        ps)`` inside a guarded body is not guarded. The one part that *is* is
        the outermost iterable, which the expression evaluates eagerly.
        """
        deferred = [node.elt]
        for index, comp in enumerate(node.generators):
            if index == 0:
                self.visit(comp.iter)
            else:
                deferred.append(comp.iter)
            deferred.append(comp.target)
            deferred.extend(comp.ifs)
        self._deferred(deferred)

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.stmt) and not isinstance(node, ast.Try):
            outer = self.span
            self.span = _header_span(node)
            super().generic_visit(node)
            self.span = outer
            return
        super().generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        receiver = _resolve_call(node.func, node)
        if receiver is not None:
            exempt = _rooted_at_file(receiver)
            self.seen.append((node.lineno, exempt))
            if not exempt:
                guarded = frozenset().union(*self.stack) if self.stack \
                    else frozenset()
                missing = FAILURES - guarded
                if missing:
                    span = self.span or (node.lineno, node.lineno)
                    self.hits.append((node.lineno, missing, span))
        self.generic_visit(node)


def _annotation_in(comments, standalone, span) -> "str | None":
    """The ``resolve-ok`` reason covering ``span``, if there is one.

    Same acceptance rule as the sibling scan -- the statement's own header, or
    the contiguous comment block directly above it -- but bound to this
    file's marker. It is spelled out again rather than shared because the
    sibling's version closes over its own ``probe-ok`` pattern, and widening
    that function to take a regex would mean editing a file a live branch has
    to edit too. The rule itself is asserted here by its own controls, so the
    two cannot drift silently.
    """
    start, end = span
    candidates = list(range(start, end + 1))
    above = start - 1
    while above >= 1 and above in standalone:
        candidates.append(above)
        above -= 1
    for candidate in candidates:
        text = comments.get(candidate)
        if text is None:
            continue
        found = ANNOTATION.search(text)
        if found:
            return found.group("reason").strip()
    return None


def _find(source: str) -> _ResolveFinder:
    finder = _ResolveFinder()
    finder.visit(ast.parse(source))
    return finder


def unguarded_resolves(source: str) -> "list[tuple[int, frozenset]]":
    """Every ``resolve()`` in ``source`` that is neither guarded nor annotated."""
    comments, standalone = _annotation_context(source)
    return [(lineno, missing) for lineno, missing, span in _find(source).hits
            if _annotation_in(comments, standalone, span) is None]


def annotated_resolves(source: str) -> "list[tuple[int, str]]":
    """Every ``(lineno, reason)`` where a call was annotated rather than fixed."""
    comments, standalone = _annotation_context(source)
    out = []
    for lineno, _missing, span in _find(source).hits:
        reason = _annotation_in(comments, standalone, span)
        if reason is not None:
            out.append((lineno, reason))
    return out


# ── the scan ────────────────────────────────────────────────────
@pytest.mark.parametrize("module", _shipped_modules(), ids=lambda p: p.name)
def test_no_partially_guarded_resolve(module) -> None:
    source = module.read_text(encoding="utf-8")
    hits = unguarded_resolves(source)
    lines = source.splitlines()
    detail = "\n".join(
        f"    {module.name}:{lineno}  unhandled: "
        f"{', '.join(sorted(missing))}   {lines[lineno - 1].strip()}"
        for lineno, missing in hits
    )
    assert not hits, (
        f"{module.name} calls Path.resolve() {len(hits)} time(s) without "
        f"covering everything it raises. `resolve` raises OSError on a "
        f"denial, RuntimeError on a symlink loop and ValueError on an "
        f"embedded NUL, and only the first is an OSError:\n{detail}\n"
        f"  Use project_paths.resolved_str(), or catch "
        f"(OSError, RuntimeError, ValueError), or - if a traceback out of "
        f"this call is genuinely acceptable - annotate the line "
        f"`# resolve-ok: <reason>`."
    )


def test_the_scan_reaches_real_calls_rather_than_exempting_everything() -> None:
    """The exemption is the blind spot, so it is asserted against.

    ``__file__``-rooted calls are the majority by count, and a chain walker
    that answered True too readily would exempt the whole tree and leave every
    assertion above it passing on an empty set. Two floors: the walker must
    find resolve calls at all, and a useful number of them must be reaching
    the guard check rather than the exemption.
    """
    total = 0
    examined = 0
    carriers = set()
    for module in _shipped_modules():
        finder = _find(module.read_text(encoding="utf-8"))
        total += len(finder.seen)
        for _lineno, exempt in finder.seen:
            if not exempt:
                examined += 1
                carriers.add(module.name)
    assert total >= 10, f"the walker found almost no resolve calls: {total}"
    assert examined >= 4, (
        f"only {examined} of {total} resolve calls reached the guard check; "
        f"the __file__ exemption is swallowing the scan"
    )
    assert "project_paths.py" in carriers, (
        "project_paths no longer contains a guarded resolve. resolved_str() "
        "is the sanctioned fix this scan points people at; if it moved, "
        "point them somewhere real"
    )


def test_every_annotation_carries_a_reason() -> None:
    """`# resolve-ok:` with nothing after it is a silencer, not a judgement."""
    for module in _shipped_modules():
        for lineno, reason in annotated_resolves(
                module.read_text(encoding="utf-8")):
            assert len(reason) >= MIN_REASON, (
                f"{module.name}:{lineno} silences the resolve scan without "
                f"saying why (reason: {reason!r}). Say what makes a traceback "
                f"out of this call acceptable."
            )


def test_the_population_is_the_one_the_sibling_scan_reads() -> None:
    """An empty population passes every assertion above it."""
    names = {p.name for p in _shipped_modules()}
    assert len(names) >= 10, f"the scan found almost nothing to read: {names}"
    for expected in ("copilot_operator.py", "handoff_tool.py",
                     "operator_ingest.py", "project_paths.py",
                     "setup_tools.py"):
        assert expected in names, (
            f"{expected} has held this defect before and is no longer being "
            f"scanned; the population filter is wrong"
        )
    assert (REPO / "project_paths.py").is_file()


# ── controls ────────────────────────────────────────────────────
_IMPORT = "from pathlib import Path\n"

#: Source that must trip the detector, one shape per entry.
FIRES = {
    "bare call": _IMPORT + "p = Path('x').resolve()\n",
    "only OSError caught": (
        _IMPORT
        + "try:\n    p = Path('x').resolve()\nexcept OSError:\n    p = None\n"
    ),
    "OSError and ValueError but not RuntimeError": (
        _IMPORT
        + "try:\n    p = Path('x').resolve()\n"
        + "except (OSError, ValueError):\n    p = None\n"
    ),
    "a subclass is not the family": (
        _IMPORT
        + "try:\n    p = Path('x').resolve()\n"
        + "except (FileNotFoundError, RuntimeError, ValueError):\n    p = None\n"
    ),
    "unbound form": (
        _IMPORT + "q = Path('x')\np = Path.resolve(q)\n"
    ),
    "reached through getattr": (
        _IMPORT + "q = Path('x')\np = getattr(q, 'resolve')()\n"
    ),
    "strict passed positionally, bound": (
        _IMPORT + "q = Path('x')\np = q.resolve(True)\n"
    ),
    "strict passed positionally, unbound": (
        _IMPORT + "q = Path('x')\np = Path.resolve(q, True)\n"
    ),
    "strict passed by keyword": (
        _IMPORT + "q = Path('x')\np = q.resolve(strict=True)\n"
    ),
    "a component joined on in the constructor": (
        _IMPORT + "p = Path(__file__, user).resolve()\n"
    ),
    "in the handler, not the body": (
        _IMPORT
        + "try:\n    pass\nexcept (OSError, RuntimeError, ValueError):\n"
        + "    p = Path('x').resolve()\n"
    ),
    "in the finally, not the body": (
        _IMPORT
        + "try:\n    pass\nexcept (OSError, RuntimeError, ValueError):\n"
        + "    pass\nfinally:\n    p = Path('x').resolve()\n"
    ),
    "__file__ passed as an argument is not the receiver": (
        _IMPORT + "cfg = Path('x')\np = cfg.relative_to(__file__).resolve()\n"
    ),
    "rooted at __file__ and then left, via joinpath": (
        _IMPORT + "p = Path(__file__).joinpath(user).resolve()\n"
    ),
    "rooted at __file__ and then left, via with_name": (
        _IMPORT + "p = Path(__file__).with_name(user).resolve()\n"
    ),
    "a lambda does not run inside the try that encloses it": (
        _IMPORT
        + "try:\n    f = lambda: Path(x).resolve()\n"
        + "except (OSError, RuntimeError, ValueError):\n    f = None\n"
    ),
    "a nested def does not run inside the try that encloses it": (
        _IMPORT
        + "try:\n    def later(p):\n        return Path(p).resolve()\n"
        + "except (OSError, RuntimeError, ValueError):\n    later = None\n"
    ),
    "a generator expression body is iterated later": (
        _IMPORT
        + "try:\n    g = (Path(p).resolve() for p in ps)\n"
        + "except (OSError, RuntimeError, ValueError):\n    g = iter([])\n"
    ),
    "a generator expression's inner iterable is deferred too": (
        _IMPORT
        + "try:\n    g = (q for p in ps for q in Path(p).resolve().parts)\n"
        + "except (OSError, RuntimeError, ValueError):\n    g = iter([])\n"
    ),
    "an annotation two lines up does not reach": (
        _IMPORT
        + "# resolve-ok: this reason is about something else entirely\n"
        + "x = 1\n"
        + "p = Path('y').resolve()\n"
    ),
}

#: Source that must NOT trip it. A detector that flags the correct spelling
#: gets silenced, and a silenced detector is the same as no detector.
PASSES = {
    "all three caught": (
        _IMPORT
        + "try:\n    p = Path('x').resolve()\n"
        + "except (OSError, RuntimeError, ValueError):\n    p = None\n"
    ),
    "blanket Exception": (
        _IMPORT
        + "try:\n    p = Path('x').resolve()\nexcept Exception:\n    p = None\n"
    ),
    "bare except": (
        _IMPORT
        + "try:\n    p = Path('x').resolve()\nexcept:\n    p = None\n"
    ),
    "nested guards combine": (
        _IMPORT
        + "try:\n    try:\n        p = Path('x').resolve()\n"
        + "    except (RuntimeError, ValueError):\n        p = None\n"
        + "except OSError:\n    p = None\n"
    ),
    "the module's own location": _IMPORT + "here = Path(__file__).resolve()\n",
    "the module's own location, one attribute along": (
        _IMPORT + "here = Path(__file__).parent.resolve().parent\n"
    ),
    "the module's own location, resolved twice": (
        _IMPORT + "here = Path(__file__).resolve().parent.resolve()\n"
    ),
    "a comprehension really does run inside the try": (
        _IMPORT
        + "try:\n    ps = [Path(p).resolve() for p in xs]\n"
        + "except (OSError, RuntimeError, ValueError):\n    ps = []\n"
    ),
    "a guarded call inside a nested def": (
        _IMPORT
        + "def later(p):\n    try:\n        return Path(p).resolve()\n"
        + "    except (OSError, RuntimeError, ValueError):\n        return None\n"
    ),
    "a nested def's argument default runs at def time": (
        _IMPORT
        + "try:\n    def later(p=Path(x).resolve()):\n        return p\n"
        + "except (OSError, RuntimeError, ValueError):\n    later = None\n"
    ),
    "a nested def's keyword-only default runs at def time": (
        _IMPORT
        + "try:\n    def later(*, p=Path(x).resolve()):\n        return p\n"
        + "except (OSError, RuntimeError, ValueError):\n    later = None\n"
    ),
    "a nested def's decorator runs at def time": (
        _IMPORT
        + "try:\n    @register(Path(x).resolve())\n"
        + "    def later(p):\n        return p\n"
        + "except (OSError, RuntimeError, ValueError):\n    later = None\n"
    ),
    "a lambda's argument default runs where it is written": (
        _IMPORT
        + "try:\n    f = lambda p=Path(x).resolve(): p\n"
        + "except (OSError, RuntimeError, ValueError):\n    f = None\n"
    ),
    "a generator expression's outermost iterable is eager": (
        _IMPORT
        + "try:\n    g = (p for p in Path(x).resolve().parts)\n"
        + "except (OSError, RuntimeError, ValueError):\n    g = iter([])\n"
    ),
    "annotated with a reason": (
        _IMPORT
        + "# resolve-ok: import-time constant, a failure here is a dead module\n"
        + "p = Path('x').resolve()\n"
    ),
    "annotated on its own line": (
        _IMPORT
        + "p = Path('x').resolve()  # resolve-ok: nothing here can be a link\n"
    ),
    "the sanctioned fix is not a call to resolve": (
        "from project_paths import resolved_str\np = resolved_str('x')\n"
    ),
    "realpath is a different rule": (
        "import os\np = os.path.realpath('x')\n"
    ),
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_every_detector_fires_on_source_that_has_the_defect(name: str) -> None:
    assert unguarded_resolves(FIRES[name]), (
        f"the {name!r} control did not trip the scan. A detector that matches "
        f"nothing reports the whole tree clean, which reads exactly like "
        f"success:\n{FIRES[name]}"
    )


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_correct_spellings_still_pass(name: str) -> None:
    hits = unguarded_resolves(PASSES[name])
    assert not hits, (
        f"the {name!r} control was flagged at line(s) "
        f"{[line for line, _ in hits]}. Flagging a correct spelling is how a "
        f"scan gets switched off:\n{PASSES[name]}"
    )


def test_the_missing_set_names_what_is_actually_unhandled() -> None:
    """The failure message has to be actionable, so it is asserted.

    ``assert not hits`` would pass on a detector that reported every call as
    missing all three, and the reader would go looking for handlers that are
    already there. The set is the part of the message that says what to add.
    """
    source = (_IMPORT + "try:\n    p = Path('x').resolve()\n"
              + "except (OSError, ValueError):\n    p = None\n")
    hits = unguarded_resolves(source)
    assert [missing for _line, missing in hits] == [frozenset({"runtimeerror"})]


def test_an_empty_annotation_is_rejected() -> None:
    source = _IMPORT + "p = Path('x').resolve()  # resolve-ok:\n"
    annotated = annotated_resolves(source)
    assert annotated, "the annotation was not seen at all"
    assert all(len(reason) < MIN_REASON for _line, reason in annotated)


def test_a_marker_inside_a_string_does_not_silence_the_scan() -> None:
    """The annotation is read from the tokenizer, not matched against text.

    A regex over raw source cannot tell a comment from a string containing
    one. That bypass was found in the sibling scan and in checkout-guard's
    detector in the same week; a scan whose whole purpose is to stop a rule
    being enforced one file at a time cannot have a one-token bypass of its
    own.
    """
    source = _IMPORT + "p = Path(str('# resolve-ok: not a comment')).resolve()\n"
    assert unguarded_resolves(source), \
        "a string literal silenced the scan; the marker must come from a comment"
