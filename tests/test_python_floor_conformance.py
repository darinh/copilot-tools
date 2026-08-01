"""Code that runs on the floor this project declares, not the one you have.

``pyproject.toml`` says ``requires-python = ">=3.10"`` and CI runs 3.10 on
all three operating systems. Every API newer than that is a ``TypeError``,
an ``AttributeError`` or a ``ModuleNotFoundError`` on half the matrix, and
none of them is a syntax error — so the module imports, the file parses, and
the failure waits in whichever branch nobody exercised locally. The
developer's interpreter is 3.11 or 3.12; the floor is 3.10; the gap is
invisible until a user on the floor reaches the line.

This is the Python counterpart of ``tests/test_shell_bash32_conformance.py``,
and it exists for the same reason: **macOS ships bash 3.2 forever, and
somebody's Python is 3.10 forever.** A portability floor that is only
checked by running on the floor is checked on one leg of the matrix, after
the fact, in whichever test happens to reach the line.

**The finding that produced this file.** ``test_presence_probe_conformance``
demands that presence probes stop answering a three-valued question with two
values, and it names ``follow_symlinks=False`` as the fix — the spelling that
makes the call ``lstat``-based. Its ``PASSES`` register, the negative control
that certifies correct spellings, contained::

    if Path('x').is_dir(follow_symlinks=False):

Measured on the two interpreters CI actually runs:

===========================  ==========  ==========
call                         3.10        3.12
===========================  ==========  ==========
``exists(follow_symlinks=)``   TypeError   ok
``is_dir(follow_symlinks=)``   TypeError   TypeError
``is_file(follow_symlinks=)``  TypeError   TypeError
===========================  ==========  ==========

``Path.exists`` grew the keyword in 3.12; ``is_dir`` and ``is_file`` in 3.13.
So the scan's certified remedy for the tri-state bug **raises on every
interpreter this project supports**, and the file that exists to stop a
silent wrong answer was recommending a crash. That is the second time a
negative control in that file has certified the defect it guards against: the
previous round found ``os.path.exists`` sitting in the same register. A wrong
negative control is worse than a missing one, because it does not read like a
gap — it reads like a decision.

**The registry is measured, not remembered.** Every construct below carries a
``probe`` that asks *the running interpreter* whether it exists, and
:func:`test_the_registry_agrees_with_this_interpreter` checks each answer
against the declared version. On the 3.10 leg that test proves the constructs
really are absent; on 3.12 it proves the 3.11 and 3.12 entries really did
arrive and the 3.13 ones have not. A version number typed from memory is
exactly how the entry above got written, and a table of them that nothing
executes is a comment with a test's file extension.

**What it demands.** Not that these APIs are never used — a guarded use is
how ``install_manifest.file_digest`` reaches ``hashlib.file_digest`` on 3.11
and hashes by hand on 3.10. It demands that every use be either

* lexically inside a guard — a ``try`` catching the error the absence
  raises, or an ``if`` testing ``hasattr`` or ``sys.version_info`` — or
* annotated ``# floor-ok: <reason>``, which is a claim that the line cannot
  be reached on the floor, and a place to say why.

**The floor is read, never typed.** :func:`declared_floor` parses
``requires-python`` out of ``pyproject.toml``, and
:func:`test_the_floor_agrees_with_the_ci_matrix` asserts it matches the
lowest interpreter in ``.github/workflows/ci.yml``. Raising the floor is then
a one-line edit that reclassifies every construct at once, and a floor that
disagrees with what CI runs fails here rather than in a review.

Every detector has a positive control asserting it fires and a negative
control asserting the portable spelling still passes. A detector broken into
matching nothing reports the whole tree clean, which reads exactly like
success.

**What this cannot do, stated plainly because a scan is trusted.** The
registry is *curated*. Nothing here discovers a post-floor API that nobody
thought to add, so this file cannot catch the next ``follow_symlinks``
incident — only the next recurrence of one already listed.

That limit is worth naming precisely, because the machinery around it
disguises how narrow it is. Identity-pinning, positive controls and
interpreter-checked versions all defend against the registry *rotting*; not
one of them notices it having been incomplete on the day it was written. The
scan's failure mode is therefore not a wrong answer but a confident silence,
and silence from a tool with this much apparatus behind it reads like
coverage.

So: a green run here means "none of the constructs listed below is
unguarded", never "this tree runs on 3.10". The distinction is the whole
honest content of the result. (No count appears in that sentence on purpose
— a number in prose that nothing executes is the exact defect this file was
built out of, and it would be a poor place to reintroduce it.) The way to
add an entry is to find a defect the hard way and register it, which is how
every entry below got here, ``follow_symlinks`` included.
"""
import ast
import hashlib
import io
import re
import sys
import tokenize
from pathlib import Path
from typing import Callable, NamedTuple

import pytest

REPO = Path(__file__).resolve().parent.parent

ANNOTATION = re.compile(r"#\s*floor-ok:(?P<reason>.*)$")

#: The shortest reason worth writing, matching the probe scan's bar.
MIN_REASON = 12

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist")

#: Exceptions that mean a ``try`` can express "this interpreter lacks it".
#: ``TypeError`` is here because an unknown *keyword* raises that rather than
#: ``AttributeError`` — the method exists, the parameter does not.
CATCHES = frozenset({"AttributeError", "ImportError", "ModuleNotFoundError",
                     "TypeError", "NameError", "Exception", "BaseException"})


def declared_floor() -> tuple[int, int]:
    """The lowest Python ``pyproject.toml`` claims to support.

    Read rather than typed. A constant here would be a second place to raise
    the floor, and the second place is the one that gets forgotten — which is
    the whole failure mode this file was built out of.
    """
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'requires-python\s*=\s*"[^"]*?(\d+)\.(\d+)', text)
    assert found, "pyproject.toml declares no requires-python floor"
    return int(found.group(1)), int(found.group(2))


FLOOR = declared_floor()


class Construct(NamedTuple):
    """One API that arrived after some Python version.

    ``probe`` answers whether *this* interpreter has it. It is what stops
    ``since`` from being a number somebody remembered: the registry test
    below runs every probe and checks it against ``since``, so an entry with
    the wrong version fails on whichever leg of the CI matrix straddles it.
    """

    since: tuple[int, int]
    remedy: str
    probe: Callable[[], bool]


def _accepts_keyword(method_name: str, keyword: str) -> bool:
    """Whether ``Path.<method_name>`` accepts ``<keyword>`` on this build.

    Asked by calling it, not by reading a signature: these are C-accelerated
    on some builds, where ``inspect.signature`` raises rather than answering.
    The call is made against this file, which certainly exists, so the only
    thing that can distinguish the two outcomes is the keyword itself.
    """
    try:
        getattr(Path(__file__), method_name)(**{keyword: False})
    except TypeError:
        return False
    except OSError:
        return True
    return True


def _accepts_relative_to_walk_up() -> bool:
    """Whether ``Path.relative_to`` accepts ``walk_up=`` on this build."""
    try:
        Path("/a/b").relative_to(Path("/a"), walk_up=True)
    except TypeError:
        return False
    except ValueError:
        return True
    return True


def _importable(module: str, attr: str | None = None) -> Callable[[], bool]:
    """A probe for ``import module`` / ``from module import attr``."""

    def probe() -> bool:
        try:
            loaded = __import__(module, fromlist=["_"] if attr else [])
        except ImportError:
            return False
        return hasattr(loaded, attr) if attr else True

    return probe


#: Every construct newer than some Python, keyed by the id a detector emits.
#:
#: This is not a survey of the language. It is the set this project could
#: plausibly reach for, weighted towards the ones whose absence is a
#: ``TypeError`` in a rarely-taken branch rather than an ``ImportError`` at
#: the top of the file — an import that fails, fails on every leg and gets
#: noticed. ``follow_symlinks`` is the whole reason the file exists.
#:
#: *Syntax* is deliberately absent — ``except*`` (3.11) and PEP 695 type
#: parameters (3.12). Those are ``SyntaxError`` on the floor, so the module
#: does not import and pytest does not collect: every 3.10 leg goes red at
#: collection, which is the loudest failure CI has. This registry is for the
#: constructs that let the file parse and wait. A positive control for a
#: syntax construct could not be written here either — the control's own
#: source would have to be parsed by the interpreter that rejects it.
#: (``match`` is *not* an example: PEP 634 landed in 3.10, which is the floor
#: itself, and ``tests/test_unreachable_code_conformance.py`` already uses
#: it. It is named here only because listing it as post-floor is the obvious
#: mistake to make, and this file has already been bitten once by a version
#: number written from memory.)
CONSTRUCTS: dict[str, Construct] = {
    "pathlib.Path.exists(follow_symlinks=)": Construct(
        (3, 12),
        "use os.lstat()/os.stat() directly, or install_manifest.path_present()",
        lambda: _accepts_keyword("exists", "follow_symlinks"),
    ),
    "pathlib.Path.is_dir(follow_symlinks=)": Construct(
        (3, 13),
        "use os.lstat() and stat.S_ISDIR, or install_manifest.dir_present()",
        lambda: _accepts_keyword("is_dir", "follow_symlinks"),
    ),
    "pathlib.Path.is_file(follow_symlinks=)": Construct(
        (3, 13),
        "use os.lstat() and stat.S_ISREG, or install_manifest.file_present()",
        lambda: _accepts_keyword("is_file", "follow_symlinks"),
    ),
    "pathlib.Path.relative_to(walk_up=)": Construct(
        (3, 12),
        "compute the relative path with os.path.relpath()",
        lambda: _accepts_relative_to_walk_up(),
    ),
    "pathlib.Path.walk": Construct(
        (3, 12), "use os.walk()", lambda: hasattr(Path, "walk"),
    ),
    "hashlib.file_digest": Construct(
        (3, 11),
        "read the file in chunks into hashlib.new(...) by hand",
        lambda: hasattr(hashlib, "file_digest"),
    ),
    "tomllib": Construct(
        (3, 11), "parse the TOML by hand or vendor a parser",
        _importable("tomllib"),
    ),
    "datetime.UTC": Construct(
        (3, 11), "use datetime.timezone.utc",
        _importable("datetime", "UTC"),
    ),
    "enum.StrEnum": Construct(
        (3, 11), "subclass (str, Enum)", _importable("enum", "StrEnum"),
    ),
    "typing.Self": Construct(
        (3, 11), 'use a TypeVar, or the string literal "Self" under TYPE_CHECKING',
        _importable("typing", "Self"),
    ),
    "typing.Never": Construct(
        (3, 11), "use typing.NoReturn", _importable("typing", "Never"),
    ),
    "typing.assert_never": Construct(
        (3, 11), "raise AssertionError in the unreachable branch",
        _importable("typing", "assert_never"),
    ),
    "asyncio.TaskGroup": Construct(
        (3, 11), "use asyncio.gather()", _importable("asyncio", "TaskGroup"),
    ),
    "contextlib.chdir": Construct(
        (3, 11), "save and restore os.getcwd() in a try/finally",
        _importable("contextlib", "chdir"),
    ),
    "itertools.batched": Construct(
        (3, 12), "slice the sequence in a loop",
        _importable("itertools", "batched"),
    ),
    "os.path.splitroot": Construct(
        (3, 12), "use os.path.splitdrive()",
        _importable("os.path", "splitroot"),
    ),
    "ExceptionGroup": Construct(
        (3, 11), "raise the first error, or a single error carrying the rest",
        lambda: hasattr(__import__("builtins"), "ExceptionGroup"),
    ),
}


#: Keyword-gated calls: method name -> keyword -> construct id.
KEYWORD_GATED = {
    "exists": {"follow_symlinks": "pathlib.Path.exists(follow_symlinks=)"},
    "is_dir": {"follow_symlinks": "pathlib.Path.is_dir(follow_symlinks=)"},
    "is_file": {"follow_symlinks": "pathlib.Path.is_file(follow_symlinks=)"},
    "relative_to": {"walk_up": "pathlib.Path.relative_to(walk_up=)"},
}

#: Attribute-gated: the trailing dotted name -> construct id. Matched on the
#: attribute and its immediate parent, so ``hashlib.file_digest`` matches and
#: ``self.file_digest`` does not.
ATTRIBUTE_GATED = {
    ("hashlib", "file_digest"): "hashlib.file_digest",
    ("datetime", "UTC"): "datetime.UTC",
    ("enum", "StrEnum"): "enum.StrEnum",
    ("typing", "Self"): "typing.Self",
    ("typing", "Never"): "typing.Never",
    ("typing", "assert_never"): "typing.assert_never",
    ("asyncio", "TaskGroup"): "asyncio.TaskGroup",
    ("contextlib", "chdir"): "contextlib.chdir",
    ("itertools", "batched"): "itertools.batched",
    ("path", "splitroot"): "os.path.splitroot",
}

#: Methods that only pathlib has, keyed by the construct they belong to.
#: Recognised on a pathlib *receiver* only, because ``os.walk`` is as old as
#: ``os`` and a rule keyed on the bare method name would flag it. Two receiver
#: shapes are understood — the class itself (``Path.walk(p)``) and a
#: construction (``Path('x').walk()``). A local variable holding a ``Path``
#: is not, for the same reason the sibling probe scan does not resolve a
#: local alias of ``Path``: a rule that has to follow names is a type checker.
METHOD_GATED = {"walk": "pathlib.Path.walk"}

#: Spellings of the pathlib classes, shared with the probe scan's reasoning.
PATHLIB_CLASSES = frozenset({"Path", "PurePath", "PosixPath", "WindowsPath",
                             "PurePosixPath", "PureWindowsPath"})

#: ``from <module> import <name>`` -> construct id.
IMPORT_GATED = {
    ("hashlib", "file_digest"): "hashlib.file_digest",
    ("datetime", "UTC"): "datetime.UTC",
    ("enum", "StrEnum"): "enum.StrEnum",
    ("typing", "Self"): "typing.Self",
    ("typing", "Never"): "typing.Never",
    ("typing", "assert_never"): "typing.assert_never",
    ("asyncio", "TaskGroup"): "asyncio.TaskGroup",
    ("contextlib", "chdir"): "contextlib.chdir",
    ("itertools", "batched"): "itertools.batched",
    ("os.path", "splitroot"): "os.path.splitroot",
}

#: Bare builtin names that did not always exist.
BUILTIN_GATED = {
    "ExceptionGroup": "ExceptionGroup",
    "BaseExceptionGroup": "ExceptionGroup",
}

#: Whole modules that arrived late, by ``import <name>``.
MODULE_GATED = {"tomllib": "tomllib"}


def _guards_absence(handler: ast.ExceptHandler) -> bool:
    """Whether an ``except`` clause can express "this interpreter lacks it"."""
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


def _is_version_test(node: ast.expr) -> bool:
    """Whether an ``if`` test asks what interpreter this is.

    ``sys.version_info`` only. It is construct-agnostic on purpose: a version
    comparison is a statement about the whole interpreter, so it can license
    any post-floor call in its branches.

    ``hasattr`` is deliberately *not* here. It is a feature test for one
    named attribute, and treating it as a general licence let an unrelated
    probe silence a real violation — ``if hasattr(obj, 'x'):
    hashlib.file_digest(...)`` passed the first version of this scan. That is
    handled by :func:`_feature_test_names` instead, which only licenses the
    construct actually being probed.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "version_info":
            return True
        if isinstance(child, ast.Name) and child.id == "version_info":
            return True
    return False


def _feature_test_names(node: ast.expr) -> set[str]:
    """The attribute names an ``if`` test probes for existence.

    ``hasattr(hashlib, "file_digest")`` yields ``{"file_digest"}`` — the
    probed *string* only. Three-argument ``getattr`` counts too: it is a
    feature test with a fallback, where the two-argument form is ordinary
    attribute access.

    The object being probed is deliberately not included. It was, and a
    reviewer showed that ``hasattr(config, "hashlib")`` therefore licensed
    ``hashlib.file_digest`` — the token ``hashlib`` appeared on both sides
    while the two expressions had nothing to do with each other. Matching on
    tokens that appear *somewhere* in a name is the same error as a control
    asserting that *something* fired.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)):
            continue
        if child.func.id == "hasattr" and len(child.args) == 2:
            pass
        elif child.func.id == "getattr" and len(child.args) == 3:
            pass
        else:
            continue
        attribute = child.args[1]
        if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
            names.add(attribute.value)
    return names


def _is_type_checking_test(node: ast.expr, typing_names: set[str]) -> bool:
    """Whether an ``if`` test is ``typing.TYPE_CHECKING``.

    ``typing.TYPE_CHECKING`` is ``False`` at runtime and ``True`` only to a
    type checker, so the body never executes on any interpreter. A
    ``from typing import Self`` there is correct code on 3.10, and flagging it
    is the kind of false positive that gets a scan switched off. The ``else``
    branch of such an ``if`` *is* runtime code and is not licensed.

    The receiver is checked. Any attribute called ``TYPE_CHECKING`` used to
    qualify, so ``if config.TYPE_CHECKING:`` — an ordinary runtime flag that
    happens to share the name — licensed everything in its body.
    """
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING":
        return isinstance(node.value, ast.Name) and node.value.id in typing_names
    return False


def _guard_names(construct: str) -> set[str]:
    """The names a ``hasattr`` would use to feature-test ``construct``.

    The *terminal* names only, derived from the registry key rather than
    listed again: ``hashlib.file_digest`` yields ``{"file_digest"}`` and
    ``pathlib.Path.is_dir(follow_symlinks=)`` yields ``{"is_dir",
    "follow_symlinks"}``. Including the module and class names made the match
    far too generous — see :func:`_feature_test_names`.
    """
    head, _, keyword = construct.partition("(")
    names = {head.split(".")[-1]} if head else set()
    if keyword:
        names.add(keyword.rstrip("=)"))
    return names


def _is_pathlib_receiver(node: ast.expr) -> bool:
    """True for ``Path`` and for a ``Path(...)`` construction."""
    if isinstance(node, ast.Name):
        return node.id in PATHLIB_CLASSES
    if isinstance(node, ast.Attribute):
        return node.attr in PATHLIB_CLASSES
    if isinstance(node, ast.Call):
        return _is_pathlib_receiver(node.func)
    return False


def _header_span(node: ast.stmt) -> tuple[int, int]:
    """The lines of ``node`` that are not part of a nested block."""
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    body = getattr(node, "body", None)
    if body:
        end = min(end, body[0].lineno - 1)
    return node.lineno, max(node.lineno, end)


def _literal_dict_keys(node: ast.expr) -> set[str]:
    """String keys of a literal dict, following nested ``**`` unpacks.

    ``{**{"follow_symlinks": False}}`` has the same keys as the dict inside
    it. A nested unpack appears as a key of ``None``.
    """
    if not isinstance(node, ast.Dict):
        return set()
    names: set[str] = set()
    for key, value in zip(node.keys, node.values):
        if key is None:
            names |= _literal_dict_keys(value)
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
    return names


class _FloorFinder(ast.NodeVisitor):
    """Collect ``(lineno, construct_id, span)`` for unguarded post-floor uses.

    Guarding is lexical, like the presence-probe scan's, and comes in two
    strengths.

    *Construct-agnostic*: a ``try`` that catches the absence, either branch of
    an ``if sys.version_info ...``, or the body of ``if TYPE_CHECKING:``.
    These license anything inside them, because each is a statement about the
    interpreter rather than about one name.

    Either branch of a version test, deliberately. ``if sys.version_info >=
    (3, 11):`` puts the new API in the body and ``if sys.version_info < (3,
    11):`` puts it in the ``else``, and deciding which by reading the
    comparison is the start of writing a type checker. What the guard is
    really evidence of is that the author thought about the version at all,
    and that is true in both branches. The cost is that an inverted guard
    passes; the benefit is that the rule stays a rule rather than a partial
    evaluator.

    *Construct-specific*: ``hasattr(hashlib, "file_digest")`` and
    three-argument ``getattr`` license only the construct they name. This
    used to be construct-agnostic too, and a reviewer showed that ``if
    hasattr(obj, 'x'): hashlib.file_digest(...)`` therefore passed — a probe
    for an unrelated attribute silencing a real violation. The version-test
    argument does not carry over: ``hasattr`` is a claim about one name, so
    it can only be evidence about that name.

    Alias spellings are resolved, because a scan with a one-token bypass is a
    scan whose next violation is deliberate: ``import os.path as osp`` then
    ``osp.splitroot(...)``, ``from hashlib import file_digest as fd`` then
    ``fd(...)``, and ``exists(**{"follow_symlinks": False})``. Rebinding
    through an ordinary variable (``h = hashlib``) is *not* resolved — that is
    name resolution, which is a type checker, and the sibling probe scan
    records the same limit for aliases of ``Path``.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.licensed: list[set[str]] = []
        self.span: tuple[int, int] | None = None
        self.hits: list[tuple[int, str, tuple[int, int]]] = []
        self.module_alias: dict[str, str] = {}
        self.symbol_alias: dict[str, str] = {}
        self.typing_names: set[str] = {"typing"}

    def scan(self, tree: ast.AST) -> None:
        """Collect aliases across the whole module, then walk it.

        Two passes because an import can sit below its use — inside a
        function defined earlier in the file, or after a conditional import
        block. A single pass would resolve aliases only for code that happens
        to appear later in the source, which is a property of layout rather
        than of the program.
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.module_alias[alias.asname] = alias.name
                    if alias.name.split(".")[0] == "typing":
                        self.typing_names.add(alias.asname or "typing")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if not alias.asname:
                        continue
                    construct = IMPORT_GATED.get((module, alias.name))
                    if construct is None:
                        tail = module.split(".")[-1]
                        construct = ATTRIBUTE_GATED.get((tail, alias.name))
                    if construct is not None:
                        self.symbol_alias[alias.asname] = construct
                    else:
                        # `from os import path as p` imports a *module* under
                        # a new name, so `p.splitroot(...)` is the same call
                        # as `os.path.splitroot(...)`. Only the trailing
                        # component is ever matched, so binding the bare name
                        # is enough.
                        self.module_alias[alias.asname] = alias.name
                    if module == "typing" and alias.name == "TYPE_CHECKING":
                        self.typing_names.add(alias.asname)
        self.visit(tree)

    def _record(self, node: ast.AST, construct: str) -> None:
        if self.depth:
            return
        names = _guard_names(construct)
        if any(names & scope for scope in self.licensed):
            return
        lineno = getattr(node, "lineno", 0)
        span = self.span or (lineno, lineno)
        self.hits.append((lineno, construct, span))

    def visit_Try(self, node: ast.Try) -> None:
        guarded = any(_guards_absence(h) for h in node.handlers)
        self.depth += bool(guarded)
        for stmt in node.body:
            self.visit(stmt)
        self.depth -= bool(guarded)
        for handler in node.handlers:
            self.visit(handler)
        for stmt in [*node.orelse, *node.finalbody]:
            self.visit(stmt)

    visit_TryStar = visit_Try

    def visit_If(self, node: ast.If) -> None:
        broad = _is_version_test(node.test)
        typing_only = _is_type_checking_test(node.test, self.typing_names)
        named = _feature_test_names(node.test)
        outer = self.span
        self.span = _header_span(node)
        self.visit(node.test)
        self.span = outer

        # A named feature test licenses its construct in *both* branches, for
        # the same reason a version test does: `if not hasattr(m, 'x'):
        # fallback / else: use(m.x)` is the correct spelling and flagging it
        # is a false positive, while reading the polarity is the start of
        # writing a partial evaluator. The licence is still confined to the
        # construct actually probed, which is what stops an unrelated
        # `hasattr` from silencing anything.
        if named:
            self.licensed.append(named)
        try:
            self.depth += bool(broad or typing_only)
            for stmt in node.body:
                self.visit(stmt)
            self.depth -= bool(broad or typing_only)

            # The `else` of a version test is still a version-aware branch;
            # the `else` of `if TYPE_CHECKING:` is ordinary runtime code.
            self.depth += bool(broad)
            for stmt in node.orelse:
                self.visit(stmt)
            self.depth -= bool(broad)
        finally:
            if named:
                self.licensed.pop()

    def generic_visit(self, node: ast.AST) -> None:
        if isinstance(node, ast.stmt) and not isinstance(node, (ast.Try, ast.If)):
            outer = self.span
            self.span = _header_span(node)
            super().generic_visit(node)
            self.span = outer
            return
        super().generic_visit(node)

    def _keyword_names(self, node: ast.Call) -> set[str]:
        """Keyword argument names, including literal ``**{...}`` unpacking.

        ``f(**{"follow_symlinks": False})`` is the same call as
        ``f(follow_symlinks=False)`` and fails identically on the floor.
        Nested literal dicts are flattened, so ``f(**{**{"k": v}})`` is read
        too. A non-literal unpack (``f(**opts)``) is invisible here and
        always will be; a literal one is a spelling, not a computation.
        """
        names = set()
        for keyword in node.keywords:
            if keyword.arg is not None:
                names.add(keyword.arg)
            else:
                names |= _literal_dict_keys(keyword.value)
        return names

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            gated = KEYWORD_GATED.get(func.attr, {})
            for name in self._keyword_names(node):
                if name in gated:
                    self._record(node, gated[name])
            construct = METHOD_GATED.get(func.attr)
            if construct is not None and _is_pathlib_receiver(func.value):
                self._record(node, construct)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parent = node.value
        name = None
        if isinstance(parent, ast.Name):
            name = self.module_alias.get(parent.id, parent.id).split(".")[-1]
        elif isinstance(parent, ast.Attribute):
            name = parent.attr
        if name is not None:
            construct = ATTRIBUTE_GATED.get((name, node.attr))
            if construct is not None:
                self._record(node, construct)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # Load context only. An alias register keyed by bare name flagged
        # `fd = None` as a use of `hashlib.file_digest`, because rebinding a
        # name reads as the name.
        if isinstance(node.ctx, ast.Load):
            construct = BUILTIN_GATED.get(node.id) or self.symbol_alias.get(node.id)
            if construct is not None:
                self._record(node, construct)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            construct = MODULE_GATED.get(alias.name.split(".")[0])
            if construct is not None:
                self._record(node, construct)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if MODULE_GATED.get(module.split(".")[0]) is not None:
            self._record(node, MODULE_GATED[module.split(".")[0]])
        for alias in node.names:
            construct = IMPORT_GATED.get((module, alias.name))
            if construct is not None:
                self._record(node, construct)
        self.generic_visit(node)


def _comments(source: str) -> dict[int, str]:
    """Line number to comment text, from the tokenizer rather than a regex.

    A regex over raw source cannot tell a comment from a string containing
    one, so ``print('# floor-ok: not a comment')`` would silence the scan
    with no comment in the file. The sibling probe scan was silenced exactly
    that way before it read tokens; a detector whose purpose is to stop a
    one-token bypass cannot ship with one.
    """
    out: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                out[token.start[0]] = token.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def _annotation_context(source: str) -> tuple[dict[int, str], set[int]]:
    """Comments by line, and the lines whose comment is the whole line."""
    comments = _comments(source)
    lines = source.splitlines()
    standalone = {
        line for line in comments
        if 1 <= line <= len(lines) and lines[line - 1].lstrip().startswith("#")
    }
    return comments, standalone


def _annotation_in(comments: dict[int, str], standalone: set[int],
                   span: tuple[int, int]) -> str | None:
    """The ``floor-ok`` reason covering ``span``, if there is one."""
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


def _above_floor(construct: str) -> bool:
    """Whether ``construct`` needs a Python newer than the declared floor."""
    return CONSTRUCTS[construct].since > FLOOR


def floor_violations(source: str) -> list[tuple[int, str]]:
    """Every post-floor construct in ``source`` that is neither guarded nor annotated."""
    comments, standalone = _annotation_context(source)
    finder = _FloorFinder()
    finder.scan(ast.parse(source))
    return [(lineno, construct) for lineno, construct, span in finder.hits
            if _above_floor(construct)
            and _annotation_in(comments, standalone, span) is None]


def annotated_uses(source: str) -> list[tuple[int, str]]:
    """Every ``(lineno, reason)`` where a post-floor use was annotated."""
    comments, standalone = _annotation_context(source)
    finder = _FloorFinder()
    finder.scan(ast.parse(source))
    out = []
    for lineno, construct, span in finder.hits:
        if not _above_floor(construct):
            continue
        reason = _annotation_in(comments, standalone, span)
        if reason is not None:
            out.append((lineno, reason))
    return out


def _first_party_python(root: Path = REPO) -> list[Path]:
    """Every Python file this repository ships or runs on CI.

    Both the shipped modules and ``tests/``, because CI runs the test suite
    on 3.10 too: a post-floor construct in a test file is a red leg on the
    floor exactly like one in a module, and the sibling bash scan covers
    every first-party script rather than the ones that ship.

    Filtered on the path *relative to* ``root``, because every agent here
    works in ``<repo>/.worktrees/<branch>/`` and an absolute-path filter
    matches ``.worktrees`` for every file in the tree — leaving an empty
    population, which passes every "no file contains X" assertion below.

    ``root`` is a parameter for one reason: without it the only test of that
    filter is whether *this* checkout happens to sit under a ``.worktrees``
    path. It does for an agent and it does not on CI, so the assertion would
    hold in the place that already knows about the trap and lapse in the
    place that runs the suite eight times. Mutation-testing this scan turned
    the absolute-path filter back on and nothing failed, purely because the
    export under test was in a temp directory. See
    :func:`test_the_population_filter_survives_a_worktree_path`.
    """
    return sorted(
        p for p in [*root.glob("*.py"), *root.glob("tests/*.py")]
        if not any(part in NOT_SOURCE for part in p.relative_to(root).parts)
    )


# ── the floor itself ─────────────────────────────────────────────
def test_the_floor_agrees_with_the_ci_matrix() -> None:
    """``requires-python`` and the CI matrix must name the same lowest Python.

    Two declarations of one fact, in files that are edited for different
    reasons. If they drift, this scan is measuring against a floor nothing
    runs on, and every entry in the registry is classified against the wrong
    number — silently, because a scan with a too-high floor simply finds
    less.
    """
    matrix = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    listed = re.search(r"python-version:\s*\[(?P<items>[^\]]*)\]", matrix)
    assert listed, (
        "ci.yml has no `python-version: [...]` matrix list. If the matrix was "
        "restructured, this check is reading the wrong thing and is no longer "
        "comparing anything - fix the parse rather than deleting the test."
    )
    versions = sorted(
        (int(major), int(minor))
        for major, minor in re.findall(r"(\d+)\.(\d+)", listed.group("items"))
    )
    assert versions, f"no versions in the CI matrix list: {listed.group('items')!r}"
    assert versions[0] == FLOOR, (
        f"pyproject.toml declares requires-python >={FLOOR[0]}.{FLOOR[1]} but "
        f"the lowest interpreter in the CI matrix is "
        f"{versions[0][0]}.{versions[0][1]}. One of them is wrong, and until "
        f"they agree this scan is classifying constructs against a floor "
        f"nothing is tested on."
    )


def test_the_registry_agrees_with_this_interpreter() -> None:
    """Every ``since`` in the registry, checked against the running Python.

    This is the assertion that makes the table evidence instead of memory.
    The entry that produced this whole file — ``follow_symlinks`` on
    ``is_dir`` — was written into a negative control as *correct* because
    somebody recalled a version number and nothing executed the claim.

    Across the CI matrix the coverage is real: the 3.10 legs prove every
    entry is absent, and the 3.12 legs prove the 3.11 and 3.12 entries
    arrived while the 3.13 ones have not. A wrong version number survives
    only if it is wrong in a way no supported interpreter straddles.
    """
    running = sys.version_info[:2]
    wrong = []
    for name, construct in CONSTRUCTS.items():
        expected = running >= construct.since
        actual = construct.probe()
        if expected != actual:
            wrong.append(
                f"    {name}: registry says {construct.since[0]}."
                f"{construct.since[1]}+, so Python {running[0]}.{running[1]} "
                f"should {'have' if expected else 'lack'} it — it "
                f"{'has' if actual else 'lacks'} it"
            )
    assert not wrong, (
        "the registry disagrees with the interpreter running it:\n"
        + "\n".join(wrong)
        + "\n  Fix the `since` field. It is a measurement, not a recollection."
    )


# ── the scan ─────────────────────────────────────────────────────
@pytest.mark.parametrize("source_file", _first_party_python(),
                         ids=lambda p: p.name)
def test_no_unguarded_post_floor_api(source_file: Path) -> None:
    source = source_file.read_text(encoding="utf-8")
    hits = floor_violations(source)
    lines = source.splitlines()
    detail = "\n".join(
        f"    {source_file.name}:{lineno}  {construct}  "
        f"(needs {CONSTRUCTS[construct].since[0]}."
        f"{CONSTRUCTS[construct].since[1]}+)\n"
        f"        {lines[lineno - 1].strip()}\n"
        f"        on the floor: {CONSTRUCTS[construct].remedy}"
        for lineno, construct in hits
    )
    assert not hits, (
        f"{source_file.name} uses {len(hits)} API(s) newer than the "
        f"{FLOOR[0]}.{FLOOR[1]} floor this project declares. None of these is "
        f"a syntax error, so the file imports cleanly and fails only when the "
        f"line is reached:\n{detail}\n"
        f"  Guard it with `hasattr`/`sys.version_info`/`try`, use the "
        f"spelling named above, or - if the line cannot be reached on the "
        f"floor - annotate it `# floor-ok: <reason>`."
    )


def test_the_population_is_not_empty_and_holds_what_we_ship() -> None:
    """An empty population passes every assertion above it."""
    names = {p.name for p in _first_party_python()}
    assert len(names) >= 20, f"the scan found almost nothing to read: {names}"
    for expected in ("install_manifest.py", "setup_tools.py",
                     "copilot_operator.py", "operator_ingest.py",
                     "test_presence_probe_conformance.py"):
        assert expected in names, (
            f"{expected} is not being scanned; the population filter is wrong"
        )


def test_the_population_filter_survives_a_worktree_path(tmp_path: Path) -> None:
    """A repo living under ``.worktrees/`` must still have a population.

    The test above cannot make this claim. It reads whichever checkout is
    running it, so it only exercises the trap when that checkout happens to
    sit under a ``.worktrees`` path — true for an agent, false on CI, where
    the suite runs eight times and would never notice the filter regressing.

    That is not a hypothetical. Mutation-testing this scan changed the filter
    back to the absolute path and *nothing failed*, because the tree under
    test was an export in a temp directory. Re-run from a directory with
    ``.worktrees`` in it, the same mutation emptied the population
    immediately. The mutation had not been caught; it had been asked in a
    place where it made no difference.

    So the invariant is asserted against a tree built for the purpose, and
    the assertion now means the same thing everywhere it runs.
    """
    root = tmp_path / ".worktrees" / "some-branch"
    (root / "tests").mkdir(parents=True)
    (root / "copilot_operator.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "test_thing.py").write_text("y = 2\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "stale.py").write_text("z = 3\n", encoding="utf-8")

    found = {p.name for p in _first_party_python(root)}
    assert found == {"copilot_operator.py", "test_thing.py"}, (
        f"a repository checked out under a `.worktrees` path yielded "
        f"{found or 'nothing'}. The filter is matching against the absolute "
        f"path, so every file in every agent's worktree is excluded and the "
        f"population is empty - which passes every assertion above."
    )


def _annotation_defects(root: Path = REPO) -> list[tuple[str, int, str]]:
    """Every ``# floor-ok:`` in ``root`` whose reason is too short to be one.

    Takes a root so the rule can be exercised against a tree that actually
    contains an annotation. Run against this repo it is currently empty —
    nothing here needs the escape hatch yet — and an assertion over an empty
    population passes no matter what it says. A reviewer proved that by
    replacing the comparison with ``assert False`` and watching the test go
    green, which is the same defect this file exists to stop, one level up.
    """
    out = []
    for source_file in _first_party_python(root):
        for lineno, reason in annotated_uses(
                source_file.read_text(encoding="utf-8")):
            if len(reason) < MIN_REASON:
                out.append((source_file.name, lineno, reason))
    return out


def test_every_annotation_carries_a_reason() -> None:
    """`# floor-ok:` with nothing after it is a silencer, not a judgement."""
    defects = _annotation_defects()
    assert not defects, (
        f"{len(defects)} annotation(s) silence the floor scan without saying "
        f"why: {defects}. Say what stops the line being reached on "
        f"Python {FLOOR[0]}.{FLOOR[1]}."
    )


def test_the_annotation_rule_would_catch_a_bare_marker(tmp_path: Path) -> None:
    """The rule above, run against a tree that has annotations in it.

    :func:`_annotation_defects` returns nothing for this repo, so the test
    that consumes it cannot fail here for any reason — including the rule
    being deleted. Both halves are pinned against a purpose-built tree: a
    bare marker must be caught, and a real reason must not be.
    """
    body = ("import hashlib\n"
            "digest = hashlib.file_digest(fh, 'sha256')  # floor-ok:{}\n")
    (tmp_path / "bare.py").write_text(body.format(" x"), encoding="utf-8")
    (tmp_path / "reasoned.py").write_text(
        body.format(" only reached on the 3.12 leg, guarded by the caller"),
        encoding="utf-8")

    defects = _annotation_defects(tmp_path)
    caught = {name for name, _lineno, _reason in defects}
    assert "bare.py" in caught, (
        "a `# floor-ok:` with a one-character reason was accepted; the "
        "annotation rule is not running"
    )
    assert "reasoned.py" not in caught, (
        f"a `# floor-ok:` with a real reason was rejected: {defects}"
    )


# ── the finding that started it ──────────────────────────────────
def test_the_probe_scan_certifies_nothing_that_crashes_on_the_floor() -> None:
    """Every spelling the probe scan calls correct must run on the floor.

    The two scans compose here, and this is the assertion that closes the
    original defect for the whole class rather than for one entry:
    ``test_presence_probe_conformance.PASSES`` is a register of spellings
    certified as the *fix* for the tri-state bug, and one of them
    (``is_dir(follow_symlinks=False)``) raised ``TypeError`` on every
    interpreter this project supports.

    A negative control is the one place a wrong entry does not look wrong. It
    does not fail, it does not warn, and it is read as a decision somebody
    made — so it needs a check from outside itself, and static analysis is
    the only kind that works when the certified spelling would crash the
    interpreter doing the checking.

    Checked in **both directions**, because a one-way check is an invitation
    to move the offending entry rather than fix it: ``PASSES`` must be
    floor-clean, and every entry in ``PASSES_ABOVE_FLOOR`` must genuinely be
    floor-dirty. The second half is what makes those entries expire. When the
    floor rises to 3.13 the ``follow_symlinks`` entry stops being a violation
    and *this test fails*, which is the only event that ever gets a spelling
    promoted back into ``PASSES``. A register whose entries cannot fail is a
    register that ages quietly, and this repository has now paid for that
    twice in one file.
    """
    from test_presence_probe_conformance import PASSES, PASSES_ABOVE_FLOOR

    guilty = {
        label: floor_violations(source)
        for label, source in PASSES.items()
        if floor_violations(source)
    }
    detail = "\n".join(
        f"    {label!r}: {[c for _line, c in hits]}"
        for label, hits in guilty.items()
    )
    assert not guilty, (
        f"test_presence_probe_conformance.PASSES certifies "
        f"{len(guilty)} spelling(s) that raise on Python "
        f"{FLOOR[0]}.{FLOOR[1]}:\n{detail}\n"
        f"  A negative control that endorses a crash is worse than a missing "
        f"one: it does not read like a gap, it reads like a decision."
    )

    stale = sorted(label for label, source in PASSES_ABOVE_FLOOR.items()
                   if not floor_violations(source))
    assert not stale, (
        f"PASSES_ABOVE_FLOOR holds {len(stale)} spelling(s) that this scan no "
        f"longer considers post-floor: {stale}\n"
        f"  Either the floor rose past them - in which case move them into "
        f"PASSES, which is the whole point of this half of the check - or a "
        f"detector stopped matching and the entry is now certifying nothing."
    )


# ── controls ─────────────────────────────────────────────────────
#: Source that must trip the scan, one construct per entry.
FIRES = {
    "exists with follow_symlinks": (
        "from pathlib import Path\n"
        "if Path('x').exists(follow_symlinks=False):\n"
        "    pass\n"
    ),
    "is_dir with follow_symlinks, the spelling PASSES certified": (
        "from pathlib import Path\n"
        "if Path('x').is_dir(follow_symlinks=False):\n"
        "    pass\n"
    ),
    "is_file with follow_symlinks": (
        "from pathlib import Path\n"
        "if Path('x').is_file(follow_symlinks=True):\n"
        "    pass\n"
    ),
    "relative_to with walk_up": (
        "from pathlib import Path\n"
        "rel = Path('a').relative_to(Path('b'), walk_up=True)\n"
    ),
    "Path.walk": (
        "from pathlib import Path\n"
        "for root, dirs, files in Path('x').walk():\n"
        "    pass\n"
    ),
    "hashlib.file_digest unguarded": (
        "import hashlib\n"
        "with open('x', 'rb') as fh:\n"
        "    digest = hashlib.file_digest(fh, 'sha256')\n"
    ),
    "tomllib import": "import tomllib\nconfig = tomllib.loads('')\n",
    "tomllib from-import": "from tomllib import loads\n",
    "datetime.UTC": (
        "import datetime\n"
        "now = datetime.datetime.now(datetime.UTC)\n"
    ),
    "datetime.UTC by from-import": "from datetime import UTC\n",
    "enum.StrEnum": (
        "import enum\n"
        "class Colour(enum.StrEnum):\n"
        "    RED = 'red'\n"
    ),
    "typing.Self": "import typing\ndef f() -> typing.Self: ...\n",
    "typing.Self by from-import": "from typing import Self\n",
    "typing.Never": "import typing\ndef f() -> typing.Never: ...\n",
    "typing.assert_never": (
        "import typing\n"
        "def f(x):\n"
        "    typing.assert_never(x)\n"
    ),
    "asyncio.TaskGroup": (
        "import asyncio\n"
        "async def go():\n"
        "    async with asyncio.TaskGroup() as tg:\n"
        "        pass\n"
    ),
    "contextlib.chdir": (
        "import contextlib\n"
        "with contextlib.chdir('/tmp'):\n"
        "    pass\n"
    ),
    "itertools.batched": (
        "import itertools\n"
        "chunks = list(itertools.batched(range(9), 3))\n"
    ),
    "os.path.splitroot": (
        "import os\n"
        "drive, root, tail = os.path.splitroot('/a/b')\n"
    ),
    "ExceptionGroup": (
        "def f(errors):\n"
        "    raise ExceptionGroup('several', errors)\n"
    ),
    "BaseExceptionGroup": (
        "def f(errors):\n"
        "    raise BaseExceptionGroup('several', errors)\n"
    ),
    "in a try that catches something else": (
        "import hashlib\n"
        "try:\n"
        "    hashlib.file_digest(fh, 'sha256')\n"
        "except OSError:\n"
        "    pass\n"
    ),
    "in an if that tests something else": (
        "import hashlib\n"
        "if fast:\n"
        "    hashlib.file_digest(fh, 'sha256')\n"
    ),
    "a marker hidden in a string rather than a comment": (
        "import hashlib\n"
        "hashlib.file_digest(fh, 'x'); print('# floor-ok: not a comment')\n"
    ),
    "inside a comprehension": (
        "import itertools\n"
        "xs = [c for c in itertools.batched(range(9), 3)]\n"
    ),
    "inside a nested function": (
        "import hashlib\n"
        "def outer():\n"
        "    def inner(fh):\n"
        "        return hashlib.file_digest(fh, 'sha256')\n"
        "    return inner\n"
    ),
    # The four below are alias and guard bypasses. Every one of them passed
    # the first version of this scan, and a reviewer found them by running
    # them rather than by reading the visitor. They are the reason `scan()`
    # takes an alias pass and `_feature_test_names` exists.
    "reached through a module alias": (
        # `import os.path` is not itself gated, so nothing fires at the
        # import line: this control can only be satisfied by resolving the
        # alias at the use site. Written that way on purpose - a control
        # whose subject is already flagged for another reason proves nothing
        # about the mechanism it is named after.
        "import os.path as osp\n"
        "head, root, tail = osp.splitroot('/a/b')\n"
    ),
    "imported under an alias inside a guard, then used outside it": (
        # The realistic shape of this bug: the import is version-guarded and
        # the use is not, so the module loads on the floor and dies at the
        # call. The import line is correctly silent, which again leaves the
        # use site as the only thing this control can be detecting.
        "import sys\n"
        "if sys.version_info >= (3, 11):\n"
        "    from hashlib import file_digest as fd\n"
        "digest = fd(fh, 'sha256')\n"
    ),
    "a gated keyword passed as a literal ** unpack": (
        "from pathlib import Path\n"
        "Path('x').exists(**{'follow_symlinks': False})\n"
    ),
    "under a hasattr about something unrelated": (
        # A feature test is evidence about the name it probes and nothing
        # else. Treating any hasattr as a blanket licence let this pass.
        "import hashlib\n"
        "if hasattr(config, 'fast_mode'):\n"
        "    hashlib.file_digest(fh, 'sha256')\n"
    ),
    "in the else of a TYPE_CHECKING block, which is runtime code": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    pass\n"
        "else:\n"
        "    from typing import Self\n"
    ),
    # Round two. Every one of these passed the fixes made in round one, and
    # was again found by running the detector rather than reading it.
    "reached through `from os import path as p`": (
        # The module-alias fix handled `import os.path as osp` and stopped
        # there. Both spellings bind a module object under a new name.
        "from os import path as p\n"
        "head, root, tail = p.splitroot('/a/b')\n"
    ),
    "under a TYPE_CHECKING that is somebody's runtime flag": (
        # `if config.TYPE_CHECKING:` is an ordinary attribute that happens to
        # share a name with typing's. The first version licensed any
        # attribute called TYPE_CHECKING regardless of what it hung off.
        "import hashlib\n"
        "if config.TYPE_CHECKING:\n"
        "    digest = hashlib.file_digest(fh, 'sha256')\n"
    ),
    "under a hasattr whose probed name merely appears in the construct": (
        # `hasattr(config, 'hashlib')` and `hashlib.file_digest` share a
        # token and nothing else. Matching on any component of the dotted
        # name let the first one license the second.
        "import hashlib\n"
        "if hasattr(config, 'hashlib'):\n"
        "    digest = hashlib.file_digest(fh, 'sha256')\n"
    ),
    "a gated keyword inside a nested literal ** unpack": (
        "from pathlib import Path\n"
        "Path('x').exists(**{**{'follow_symlinks': False}})\n"
    ),
}

#: Which construct each control in FIRES is a control *for*.
#:
#: Separate from FIRES so the sources stay readable, and pinned to it by
#: :func:`test_every_control_names_the_construct_it_exercises` so the two
#: cannot drift. Without this, a control asserted only that *something*
#: fired: a reviewer swapped the sources of the ``datetime.UTC`` and
#: ``ExceptionGroup`` entries and the whole suite stayed green, because each
#: construct was still exercised *somewhere*. Coverage counted by union does
#: not notice a control pointing at the wrong detector, and a control aimed
#: at the wrong detector is not a control.
EXERCISES = {
    "exists with follow_symlinks": "pathlib.Path.exists(follow_symlinks=)",
    "is_dir with follow_symlinks, the spelling PASSES certified":
        "pathlib.Path.is_dir(follow_symlinks=)",
    "is_file with follow_symlinks": "pathlib.Path.is_file(follow_symlinks=)",
    "relative_to with walk_up": "pathlib.Path.relative_to(walk_up=)",
    "Path.walk": "pathlib.Path.walk",
    "hashlib.file_digest unguarded": "hashlib.file_digest",
    "tomllib import": "tomllib",
    "tomllib from-import": "tomllib",
    "datetime.UTC": "datetime.UTC",
    "datetime.UTC by from-import": "datetime.UTC",
    "enum.StrEnum": "enum.StrEnum",
    "typing.Self": "typing.Self",
    "typing.Self by from-import": "typing.Self",
    "typing.Never": "typing.Never",
    "typing.assert_never": "typing.assert_never",
    "asyncio.TaskGroup": "asyncio.TaskGroup",
    "contextlib.chdir": "contextlib.chdir",
    "itertools.batched": "itertools.batched",
    "os.path.splitroot": "os.path.splitroot",
    "ExceptionGroup": "ExceptionGroup",
    "BaseExceptionGroup": "ExceptionGroup",
    "in a try that catches something else": "hashlib.file_digest",
    "in an if that tests something else": "hashlib.file_digest",
    "a marker hidden in a string rather than a comment": "hashlib.file_digest",
    "inside a comprehension": "itertools.batched",
    "inside a nested function": "hashlib.file_digest",
    "reached through a module alias": "os.path.splitroot",
    "imported under an alias inside a guard, then used outside it":
        "hashlib.file_digest",
    "a gated keyword passed as a literal ** unpack":
        "pathlib.Path.exists(follow_symlinks=)",
    "under a hasattr about something unrelated": "hashlib.file_digest",
    "in the else of a TYPE_CHECKING block, which is runtime code":
        "typing.Self",
    "reached through `from os import path as p`": "os.path.splitroot",
    "under a TYPE_CHECKING that is somebody's runtime flag":
        "hashlib.file_digest",
    "under a hasattr whose probed name merely appears in the construct":
        "hashlib.file_digest",
    "a gated keyword inside a nested literal ** unpack":
        "pathlib.Path.exists(follow_symlinks=)",
}

#: Source that must NOT trip it. Each is a spelling this repo can actually run.
PASSES = {
    "hashlib.file_digest behind hasattr, as install_manifest spells it": (
        "import hashlib\n"
        "if hasattr(hashlib, 'file_digest'):\n"
        "    digest = hashlib.file_digest(fh, 'sha256')\n"
        "else:\n"
        "    digest = hashlib.new('sha256')\n"
    ),
    "behind a sys.version_info test": (
        "import hashlib\n"
        "import sys\n"
        "if sys.version_info >= (3, 11):\n"
        "    hashlib.file_digest(fh, 'sha256')\n"
    ),
    "a typing-only import under TYPE_CHECKING, which never runs": (
        # `typing.TYPE_CHECKING` is False at runtime, so this body is correct
        # code on the floor. Flagging it was a false positive, and false
        # positives are how a scan gets switched off rather than fixed.
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from typing import Self\n"
    ),
    "a typing-only import under the qualified typing.TYPE_CHECKING": (
        "import typing\n"
        "if typing.TYPE_CHECKING:\n"
        "    from typing import Never\n"
    ),
    "a hasattr naming the construct, which still licenses it": (
        # The negative half of "under a hasattr about something unrelated".
        # Tightening the guard must not break the spelling install_manifest
        # actually uses, so both directions are pinned.
        "import itertools\n"
        "if hasattr(itertools, 'batched'):\n"
        "    groups = itertools.batched(range(9), 3)\n"
    ),
    "an aliased module whose attribute is not gated": (
        # Alias resolution must not start inventing violations either: the
        # alias is followed, and `os.path.join` is as old as os.path.
        "import os.path as osp\n"
        "target = osp.join('a', 'b')\n"
    ),
    "the negated hasattr spelling, where the use is in the else": (
        # `if not hasattr(...): fallback` / `else: use` is the ordinary way
        # to write this, and flagging it was a false positive. A named
        # feature test licenses its construct in both branches for the same
        # reason a version test does - reading the polarity is a partial
        # evaluator, and the licence is confined to the probed name either
        # way.
        "import hashlib\n"
        "if not hasattr(hashlib, 'file_digest'):\n"
        "    digest = hashlib.new('sha256')\n"
        "else:\n"
        "    digest = hashlib.file_digest(fh, 'sha256')\n"
    ),
    "an aliased import rebound to something else afterwards": (
        # `fd = None` is a Store, not a use. The alias register is keyed by
        # bare name, so without a context check this read as a call to the
        # gated symbol on the assignment line.
        "import sys\n"
        "if sys.version_info >= (3, 11):\n"
        "    from hashlib import file_digest as fd\n"
        "else:\n"
        "    fd = None\n"
    ),
    "a runtime flag that happens to be called TYPE_CHECKING": (
        # The mirror of the FIRES entry: narrowing TYPE_CHECKING to typing's
        # must not stop it recognising typing's own, under an alias.
        "import typing as t\n"
        "if t.TYPE_CHECKING:\n"
        "    from typing import Self\n"
    ),
    "behind an inverted sys.version_info test": (
        "import hashlib\n"
        "import sys\n"
        "if sys.version_info < (3, 11):\n"
        "    pass\n"
        "else:\n"
        "    hashlib.file_digest(fh, 'sha256')\n"
    ),
    "in a try that catches AttributeError": (
        "import hashlib\n"
        "try:\n"
        "    hashlib.file_digest(fh, 'sha256')\n"
        "except AttributeError:\n"
        "    pass\n"
    ),
    "a keyword-gated call in a try that catches TypeError": (
        "from pathlib import Path\n"
        "try:\n"
        "    Path('x').is_dir(follow_symlinks=False)\n"
        "except TypeError:\n"
        "    pass\n"
    ),
    "tomllib import guarded by ImportError": (
        "try:\n"
        "    import tomllib\n"
        "except ImportError:\n"
        "    tomllib = None\n"
    ),
    "the floor-safe lstat spelling the probe scan should be naming": (
        "import os\n"
        "import stat\n"
        "try:\n"
        "    mode = os.lstat('x').st_mode\n"
        "except OSError:\n"
        "    mode = None\n"
        "present = mode is not None and stat.S_ISDIR(mode)\n"
    ),
    "os.path.islink, which is lstat-based and as old as os.path": (
        "import os\n"
        "if os.path.islink('x'):\n"
        "    pass\n"
    ),
    "a probe with no version-gated keyword at all": (
        "from pathlib import Path\n"
        "if Path('x').is_dir():\n"
        "    pass\n"
    ),
    "a same-named attribute on something that is not the module": (
        "class Hasher:\n"
        "    def file_digest(self, fh):\n"
        "        return None\n"
        "value = self.file_digest(fh)\n"
    ),
    "a same-named keyword on an unrelated call": (
        "shutil.copytree('a', 'b', symlinks=True)\n"
        "os.stat('x', follow_symlinks=False)\n"
    ),
    "os.walk, which is as old as os and shares a name with Path.walk": (
        "import os\n"
        "for root, dirs, files in os.walk('x'):\n"
        "    pass\n"
    ),
    "a walk() on something that is not a pathlib object": (
        "for node in tree.walk():\n"
        "    pass\n"
    ),
    "an annotated use with a real reason": (
        "import hashlib\n"
        "# floor-ok: this branch is only reached from the 3.12-only fast path\n"
        "hashlib.file_digest(fh, 'sha256')\n"
    ),
}


@pytest.mark.parametrize("label", sorted(FIRES), ids=lambda s: s[:48])
def test_the_detector_fires(label: str) -> None:
    """A detector that matches nothing reports the whole tree clean."""
    hits = floor_violations(FIRES[label])
    assert hits, (
        f"the floor scan did not fire on {label!r}:\n{FIRES[label]}"
        f"  This construct is newer than {FLOOR[0]}.{FLOOR[1]} and the scan "
        f"walked past it."
    )
    fired = {construct for _line, construct in hits}
    assert EXERCISES[label] in fired, (
        f"the floor scan fired on {label!r}, but on {sorted(fired)} rather "
        f"than {EXERCISES[label]!r}:\n{FIRES[label]}"
        f"  A control that only asserts *something* matched will keep passing "
        f"after the detector it is named for stops working, as long as any "
        f"other detector happens to cover the same source."
    )


def test_every_control_names_the_construct_it_exercises() -> None:
    """EXERCISES and FIRES must describe the same set of controls.

    Two registers keyed alike drift apart silently, so the identity is
    pinned rather than trusted. Without it, a control could be added with no
    declared construct and quietly fall back to "fired on something".
    """
    only_fires = sorted(set(FIRES) - set(EXERCISES))
    only_exercises = sorted(set(EXERCISES) - set(FIRES))
    assert not only_fires and not only_exercises, (
        f"controls with no declared construct: {only_fires}\n"
        f"declared constructs with no control: {only_exercises}"
    )
    unknown = sorted(set(EXERCISES.values()) - set(CONSTRUCTS))
    assert not unknown, (
        f"controls declare constructs that are not in the registry: {unknown}"
    )


@pytest.mark.parametrize("label", sorted(PASSES), ids=lambda s: s[:48])
def test_the_detector_stays_quiet(label: str) -> None:
    """A scan that flags the correct spelling gets turned off."""
    hits = floor_violations(PASSES[label])
    assert not hits, (
        f"the floor scan wrongly flagged {label!r}: {hits}\n{PASSES[label]}"
    )


def test_every_registry_entry_has_a_control() -> None:
    """A construct nothing exercises is a line of documentation.

    The registry is where a detector silently stops matching: an entry can be
    added, mis-keyed, and never fire, and nothing about a passing suite
    distinguishes that from a clean tree.

    Read from EXERCISES rather than from what the controls happen to trip.
    The earlier version unioned the constructs actually reported across all
    of FIRES, which meant a control could exercise the wrong detector and
    still count towards coverage — a reviewer swapped the sources of two
    entries and this test, and every per-control test, stayed green.
    """
    exercised = set(EXERCISES.values())
    missing = sorted(set(CONSTRUCTS) - exercised - _below_floor_constructs())
    assert not missing, (
        f"{len(missing)} registry entr(y/ies) are never exercised by a "
        f"positive control, so nothing would notice if the detector stopped "
        f"matching them: {missing}\n"
        f"  Add an entry to FIRES for each."
    )


def _below_floor_constructs() -> set[str]:
    """Registry entries the floor has risen past, which cannot be flagged."""
    return {name for name, c in CONSTRUCTS.items() if c.since <= FLOOR}
