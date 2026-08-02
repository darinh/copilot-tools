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

**What counts as a call.** Four spellings, because a rule enforced against
one spelling is that spelling's history:

* ``p.exists()`` — the ordinary one.
* ``Path.is_dir(p)`` — the unbound form, which has one argument rather than
  none and walks straight past a detector keyed on "no arguments".
* ``getattr(p, "is_dir")()`` — nobody writes this by accident, which is
  exactly why it is closed. A tripwire with a one-token bypass is a tripwire
  whose next violation is deliberate too.
* ``os.path.exists`` / ``isfile`` / ``isdir`` — **this is the bug class in
  its purest form.** os.path swallows every ``OSError`` and answers False;
  it does not even offer the raise. A scan that flagged the pathlib
  spellings and permitted these would be pointed at the half that fails
  loudly. A ``try`` does not exempt them either — there is nothing to catch,
  so the handler is a placebo and the wrong False walks out untouched.

``follow_symlinks=False``, ``os.path.islink`` and ``os.path.lexists`` are
lstat-based. They are the fix, not the defect, and are deliberately not
flagged.

**But ``follow_symlinks=`` is not available on this project's floor.**
``pyproject.toml`` declares 3.10 and CI runs 3.10; ``Path.exists`` grew the
keyword in 3.12 and ``is_dir``/``is_file`` in 3.13, so writing it here is a
``TypeError``, not a fix. That is a separate rule with a separate scan —
``tests/test_python_floor_conformance.py`` — and the two compose: this file
says the call is not two-valued, that one says you cannot write it yet.
Reach for ``install_manifest.path_present()`` or ``os.stat(p,
follow_symlinks=False)``, which has been lstat-based since 3.3. The
keyword spelling is kept below in ``PASSES_ABOVE_FLOOR`` rather than
``PASSES``, because it sat in the negative control certifying correctness
for a whole release cycle while raising on every interpreter this project
supports.

The annotation is deliberately cheap to write and impossible to write
silently: it appears in the diff, it carries a reason, and this file's
``test_every_annotation_carries_a_reason`` refuses an empty one. The point is
not to make the probe unreachable, it is to make choosing it deliberate.

An annotation is read from ``tokenize.COMMENT``, not matched against the raw
line. A regex could be silenced by a *string* containing the marker —
``if p.exists(): print('# probe-ok: not a comment at all')`` — with no
comment anywhere in the file. That is the same defect two reviewers found in
``checkout-guard``'s detector the same week, where ``//`` inside a string
literal silenced the comment stripper; it was closed there with a character
scanner. A detector whose whole purpose is to stop a rule being enforced one
file at a time cannot have a one-token bypass of its own.

**An annotation must account for both failure modes.** These calls fail two
ways — they *raise* on a denial and they *return False* on a dangling link —
and a reason that addresses only one of them is the shape this repository
keeps paying for. Five annotations in the first draft of this very commit
said "a wrong False only costs a PATH entry"; every one was true, and every
one omitted that the raise aborts the entire setup run. They are guarded
now. When you write a reason, say what the raise does too, or guard it.

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
import io
import re
import tokenize
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


def _shipped_modules(root: Path = REPO) -> list[Path]:
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

    Filtered on the path relative to ``root``. Every agent on this project
    works in ``<repo>/.worktrees/<branch>/``, so an absolute-path filter
    matches ``.worktrees`` for every file in the tree, the population comes
    back empty, and an empty population passes every "no module contains X"
    assertion in this file. That is not hypothetical — it is what the sibling
    bash scan did when it was first written.

    ``root`` is a parameter so that claim can be *tested* rather than
    described. Without it the only exercise of the filter is whether this
    checkout happens to live under a ``.worktrees`` path — true for an agent,
    false on CI, so the assertion lapses exactly where the suite runs eight
    times. See :func:`test_the_population_filter_survives_a_worktree_path`.
    """
    return sorted(
        p for p in root.glob("*.py")
        if not any(part in NOT_SOURCE for part in p.relative_to(root).parts)
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
        attr = _probe_name(func, node)
        if attr is not None and (self.depth == 0
                                 or attr.startswith("os.path.")):
            # A `try/except OSError` does not guard an `os.path.*` probe:
            # those never raise, so the handler is a placebo and the False
            # walks out of the try untouched. Only replacing the call, or
            # annotating why a wrong False is harmless, is an answer there.
            span = self.span or (node.lineno, node.lineno)
            self.hits.append((node.lineno, attr, span))
        self.generic_visit(node)


def _probe_name(func: ast.expr, call: ast.Call) -> str | None:
    """The probe this call makes, or None if it is not one of ours.

    Three spellings reach the same two-valued answer and all three count:

    ``p.is_dir()``
        The ordinary one, and the only one anybody writes on purpose.

    ``Path.is_dir(p)``
        The unbound form. It takes one positional argument rather than none,
        so a detector that keys on "no arguments" walks straight past it.

    ``getattr(p, "is_dir")()``
        Nobody reaches for this by accident, which is the point — a tripwire
        with a one-token bypass is a tripwire whose next violation is also a
        deliberate one. It costs three lines to close.

    ``follow_symlinks=False`` is deliberately *not* a probe. It makes the
    call ``lstat``-based, which is the fix rather than the defect, so
    flagging it would demand a guard against a failure mode it does not have.

    That is a claim about *semantics*, and it stays true. It is not a claim
    about *availability*: the keyword arrived in 3.12 for ``exists`` and 3.13
    for ``is_dir``/``is_file``, above this project's 3.10 floor, so a call
    written this way raises ``TypeError`` before its symlink behaviour ever
    matters. ``tests/test_python_floor_conformance.py`` is what says so —
    keeping the two questions in two scans is why this one does not have to
    know what interpreter you are on.
    """
    if _lstat_based(call):
        return None
    # `getattr(p, "is_dir")()` — the call being made is the *result* of a
    # getattr, so the probe name is a constant inside the callee.
    if isinstance(func, ast.Call):
        inner = func
        if (isinstance(inner.func, ast.Name) and inner.func.id == "getattr"
                and len(inner.args) == 2
                and isinstance(inner.args[1], ast.Constant)
                and inner.args[1].value in PROBES):
            return str(inner.args[1].value)
        return None
    if isinstance(func, ast.Attribute) and func.attr in OSPATH_PROBES:
        # `os.path.exists(p)` / `isfile` / `isdir`. This is the bug class in
        # its purest form: os.path swallows *every* OSError and answers
        # False, so unlike the pathlib spellings it never even offers the
        # raise. A scan that flags the two that fail loudly and permits the
        # two that fail silently is pointed at the wrong half.
        if _is_os_path(func.value) and len(call.args) == 1:
            return f"os.path.{func.attr}"
    if isinstance(func, ast.Attribute) and func.attr in PROBES:
        # Bound `p.is_dir()` takes no positional argument.
        if not call.args:
            return func.attr
        # Unbound `Path.is_dir(p)` takes exactly the receiver. The shape
        # alone is not enough to recognise it: `os.path.exists(p)` is the
        # same tree, and it is handled above with its own rules.
        if len(call.args) == 1 and _is_pathlib_class(func.value):
            return func.attr
    return None


#: The ``os.path`` predicates that collapse every error into False.
#: ``islink`` and ``lexists`` are lstat-based and are deliberately absent:
#: they are the correct spellings, not the defect.
OSPATH_PROBES = frozenset({"exists", "isfile", "isdir"})


def _is_os_path(node: ast.expr) -> bool:
    """True for the ``os.path`` in ``os.path.isfile``, and for a bare ``path``."""
    if isinstance(node, ast.Attribute):
        return node.attr == "path"
    return isinstance(node, ast.Name) and node.id == "path"


#: Spellings of the class in `Path.is_dir(p)`. A local alias defeats this,
#: which is accepted: the unbound form is already the rare one, and a rule
#: that has to resolve names is a type checker.
PATHLIB_CLASSES = frozenset({"Path", "PurePath", "PosixPath", "WindowsPath",
                             "PurePosixPath", "PureWindowsPath"})


def _is_pathlib_class(node: ast.expr) -> bool:
    """True for ``Path`` and for ``pathlib.Path``."""
    if isinstance(node, ast.Name):
        return node.id in PATHLIB_CLASSES
    if isinstance(node, ast.Attribute):
        return node.attr in PATHLIB_CLASSES
    return False


def _lstat_based(call: ast.Call) -> bool:
    """True when ``follow_symlinks=False`` makes the call lstat-based."""
    for keyword in call.keywords:
        if keyword.arg == "follow_symlinks":
            value = keyword.value
            if isinstance(value, ast.Constant) and value.value is False:
                return True
    return False


def _header_span(node: ast.stmt) -> tuple[int, int]:
    """The lines of ``node`` that are not part of a nested block."""
    end = getattr(node, "end_lineno", node.lineno) or node.lineno
    body = getattr(node, "body", None)
    if body:
        end = min(end, body[0].lineno - 1)
    return node.lineno, max(node.lineno, end)


def _comments(source: str) -> dict[int, str]:
    """Line number to comment text, from the tokenizer rather than a regex.

    A regex over raw source cannot tell a comment from a string that
    contains one, so ::

        if Path('x').exists(): print('# probe-ok: not a comment at all')

    silenced this scan with no comment anywhere in the file. The marker only
    has to appear on the probe's own line, which makes it unlikely by
    accident and trivial on purpose — and a tripwire with a deliberate
    one-token bypass is a tripwire whose next violation is deliberate too.

    This is the same defect, in the same week, that two reviewers found in
    ``checkout-guard``'s detector, where ``//`` inside a string literal could
    silence the comment stripper; it was closed there with a character
    scanner (196c56a). ``tokenize`` is this language's version of that fix:
    the tokenizer already knows what a comment is, so the question stops
    being a pattern match on text.
    """
    out: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                out[token.start[0]] = token.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # The AST parse upstream has already succeeded, so this cannot be a
        # broken file. An unterminated final line can still upset the
        # tokenizer; the comments found before it are still correct.
        pass
    return out


def _annotation_in(comments: dict[int, str], standalone: set[int],
                   span: tuple[int, int]) -> str | None:
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


def _annotation_context(source: str) -> tuple[dict[int, str], set[int]]:
    """Comments by line, and the lines whose comment is the whole line."""
    comments = _comments(source)
    lines = source.splitlines()
    standalone = {
        line for line in comments
        if 1 <= line <= len(lines) and lines[line - 1].lstrip().startswith("#")
    }
    return comments, standalone


def unguarded_probes(source: str) -> list[tuple[int, str]]:
    """Every probe in ``source`` that is neither guarded nor annotated."""
    comments, standalone = _annotation_context(source)
    finder = _ProbeFinder()
    finder.visit(ast.parse(source))
    return [(lineno, attr) for lineno, attr, span in finder.hits
            if _annotation_in(comments, standalone, span) is None]


def annotated_probes(source: str) -> list[tuple[int, str]]:
    """Every ``(lineno, reason)`` where a probe was annotated rather than fixed."""
    comments, standalone = _annotation_context(source)
    finder = _ProbeFinder()
    finder.visit(ast.parse(source))
    out = []
    for lineno, _attr, span in finder.hits:
        reason = _annotation_in(comments, standalone, span)
        if reason is not None:
            out.append((lineno, reason))
    return out


#: Modules with unguarded probes that a *different* live branch is fixing, and
#: the exact probes each still has. Not an exemption — an exemption would let
#: the count grow. The assertion below is equality on the *multiset of probe
#: names*, not on a total, because a total only measures the size of the debt
#: and not its identity: fixing one ``is_dir`` while adding one ``exists``
#: leaves any count unchanged and would license the new one silently. The
#: register therefore fails when new debt arrives, when debt changes shape,
#: *and* when the fix lands — and the last of those is the only thing that
#: ever gets an entry removed. Nothing here is allowed to age quietly.
KNOWN_UNFIXED = {
    "operator_mail.py": (
        ("is_dir", "is_dir", "is_dir", "is_dir"),
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
    actual = tuple(sorted(attr for _line, attr
                          in unguarded_probes(module.read_text(encoding="utf-8"))))
    assert actual == expected, (
        f"{name} has unguarded presence probes {actual}, the register says "
        f"{expected}.\n  If any were added, the register is being used as a "
        f"licence: fix or annotate the new one.\n  If any were removed, the "
        f"fix landed — remove this entry so the module rejoins the scan.\n"
        f"  If the count matches but the names changed, one was fixed and "
        f"another introduced; the new one still needs a decision.\n"
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


def test_the_population_filter_survives_a_worktree_path(tmp_path: Path) -> None:
    """A repo living under ``.worktrees/`` must still have a population.

    The test above cannot make this claim: it reads whichever checkout is
    running it, so it only exercises the trap when that checkout happens to
    sit under a ``.worktrees`` path. True for an agent, false on CI — the
    assertion lapses exactly where the suite runs eight times.

    Found by mutation-testing the sibling floor scan, which has the same
    filter: switching it back to the absolute path failed nothing, because
    the tree under test was an export in a temp directory. Re-run from a
    directory containing ``.worktrees``, the same mutation emptied the
    population at once. The mutation had not been caught, it had been asked
    somewhere it made no difference — so the question is now asked against a
    tree built for it.
    """
    root = tmp_path / ".worktrees" / "some-branch"
    root.mkdir(parents=True)
    (root / "setup_tools.py").write_text("x = 1\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "stale.py").write_text("z = 3\n", encoding="utf-8")

    found = {p.name for p in _shipped_modules(root)}
    assert found == {"setup_tools.py"}, (
        f"a repository checked out under a `.worktrees` path yielded "
        f"{found or 'nothing'}. The filter is matching against the absolute "
        f"path, so every module in every agent's worktree is excluded and "
        f"the population is empty - which passes every assertion above."
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
#: Which probe each control in FIRES is a control *for*.
#:
#: Pinned to FIRES by :func:`test_every_control_names_the_probe_it_exercises`.
#: Without it the positive controls asserted only that the detector returned
#: *something*: a reviewer swapped the sources of ``bare exists`` and ``bare
#: is_dir`` and all sixteen controls stayed green, because each spelling was
#: still detected under the other's name. A control aimed at the wrong
#: detector still passes, and still reports the tree clean when the detector
#: it was named for stops matching.
EXERCISES = {
    "bare exists": "exists",
    "bare is_dir": "is_dir",
    "bare is_file": "is_file",
    "unbound form": "is_dir",
    "reached through getattr": "is_dir",
    "follow_symlinks left at its default": "is_dir",
    "os.path.exists": "os.path.exists",
    "os.path.isfile": "os.path.isfile",
    "os.path.isdir": "os.path.isdir",
    "os.path inside an OSError guard, which cannot help it": "os.path.isfile",
    "a marker hidden in a string rather than a comment": "exists",
    "inside a comprehension": "is_dir",
    "inside a nested function": "is_file",
    "in a try that catches something else": "exists",
    "in the handler rather than the body": "is_dir",
    "in the finally rather than the body": "is_dir",
}

#: Source that must trip the detector, one spelling per entry.
FIRES = {
    "bare exists": "from pathlib import Path\nif Path('x').exists():\n    pass\n",
    "bare is_dir": "from pathlib import Path\nif Path('x').is_dir():\n    pass\n",
    "bare is_file": "from pathlib import Path\nif Path('x').is_file():\n    pass\n",
    "unbound form": (
        "from pathlib import Path\n"
        "if Path.is_dir(Path('x')):\n"
        "    pass\n"
    ),
    "reached through getattr": (
        "from pathlib import Path\n"
        "if getattr(Path('x'), 'is_dir')():\n"
        "    pass\n"
    ),
    "follow_symlinks left at its default": (
        "from pathlib import Path\n"
        "if Path('x').is_dir(follow_symlinks=True):\n"
        "    pass\n"
    ),
    "os.path.exists": (
        "import os\n"
        "if os.path.exists('x'):\n"
        "    pass\n"
    ),
    "os.path.isfile": (
        "import os\n"
        "if not os.path.isfile(logfile):\n"
        "    pass\n"
    ),
    "os.path.isdir": (
        "import os\n"
        "if os.path.isdir('x'):\n"
        "    pass\n"
    ),
    "os.path inside an OSError guard, which cannot help it": (
        "import os\n"
        "try:\n"
        "    os.path.isfile('x')\n"
        "except OSError:\n"
        "    pass\n"
    ),
    "a marker hidden in a string rather than a comment": (
        "from pathlib import Path\n"
        "if Path('x').exists(): print('# probe-ok: not a comment at all')\n"
    ),
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

#: Source that must NOT trip it. Each is a spelling the repo actually uses,
#: **and can actually run on the floor** — see ``PASSES_ABOVE_FLOOR`` below
#: for the spellings that are correct but not yet available.
PASSES = {
    "os.stat with follow_symlinks=False, lstat-based since 3.3": (
        "import os\n"
        "import stat\n"
        "try:\n"
        "    mode = os.stat('x', follow_symlinks=False).st_mode\n"
        "except OSError:\n"
        "    mode = None\n"
        "if mode is not None and stat.S_ISDIR(mode):\n"
        "    pass\n"
    ),
    "os.path.islink is lstat-based and is the fix, not the defect": (
        "import os\n"
        "if os.path.islink('x'):\n"
        "    pass\n"
    ),
    "os.path.lexists is lstat-based too": (
        "import os\n"
        "if os.path.lexists('x'):\n"
        "    pass\n"
    ),
    "an unrelated method with a probe's name on another object": (
        "if parser.exists('x', 'y', 'z'):\n"
        "    pass\n"
    ),
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
    "an unrelated method that merely shares a name": (
        "if parser.exists('x', 'y', 'z'):\n"
        "    pass\n"
    ),
}

#: Correct spellings that this project **cannot use yet**.
#:
#: ``follow_symlinks=False`` genuinely makes a probe ``lstat``-based, so
#: :func:`_lstat_based` is right to stop calling it a two-valued probe — that
#: is a statement about semantics, and it is true on any interpreter that has
#: the keyword. What is *not* true is that it is available: ``Path.exists``
#: grew it in 3.12 and ``is_dir``/``is_file`` in 3.13, while
#: ``pyproject.toml`` declares 3.10 and CI runs 3.10 on three operating
#: systems. Measured on both CI interpreters, ``is_dir(follow_symlinks=False)``
#: raises ``TypeError`` on each.
#:
#: It sat in ``PASSES`` for a whole release cycle — this file's negative
#: control, the register that certifies the *fix*, endorsing a call that
#: crashes everywhere the project runs. That is the second wrong entry this
#: register has held (``os.path.exists`` was the first), and both survived for
#: the same reason: a negative control is the one place a mistake does not
#: look like one. It does not fail, it does not warn, and it reads as a
#: decision somebody made.
#:
#: So the two claims are separated. These entries still assert that the probe
#: detector stays quiet, because that part was always true. What they no
#: longer do is claim you may write this.
#: ``tests/test_python_floor_conformance.py`` pins the split from outside,
#: in both directions: every ``PASSES`` entry must be floor-clean, and every
#: entry here must be floor-*dirty*. Parking a crash in the safe register
#: fails there, and so does leaving a spelling here after the floor rises
#: past it — which is what finally gets these entries promoted rather than
#: forgotten.
PASSES_ABOVE_FLOOR = {
    "lstat-based by keyword (Path.exists 3.12+, is_dir/is_file 3.13+)": (
        "from pathlib import Path\n"
        "if Path('x').is_dir(follow_symlinks=False):\n"
        "    pass\n"
    ),
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_every_detector_fires_on_source_that_has_the_defect(name: str) -> None:
    hits = unguarded_probes(FIRES[name])
    assert hits, (
        f"the detector did not fire on {name!r}. A detector that matches "
        f"nothing reports the whole tree clean, which reads exactly like "
        f"success."
    )
    fired = {probe for _line, probe in hits}
    assert EXERCISES[name] in fired, (
        f"the detector fired on {name!r}, but reported {sorted(fired)} "
        f"rather than {EXERCISES[name]!r}. A control that only asserts "
        f"*something* matched keeps passing after the detector it is named "
        f"for stops working, as long as any other one covers the same source."
    )


def test_every_control_names_the_probe_it_exercises() -> None:
    """EXERCISES and FIRES must describe the same set of controls."""
    only_fires = sorted(set(FIRES) - set(EXERCISES))
    only_exercises = sorted(set(EXERCISES) - set(FIRES))
    assert not only_fires and not only_exercises, (
        f"controls with no declared probe: {only_fires}\n"
        f"declared probes with no control: {only_exercises}"
    )


def test_every_probe_in_the_registry_has_a_control() -> None:
    """A probe nothing exercises is a line of documentation.

    The floor scan grew this check first and this file did not, which left
    the older and more load-bearing of the two registers unpinned: a
    reviewer added ``is_symlink`` to PROBES and all 52 tests stayed green.
    An entry can be added, mis-keyed, and never fire, and a passing suite
    looks exactly the same as a clean tree.

    ``os.path`` probes are named ``os.path.<fn>`` by the detector, so the two
    registers are compared in the spelling each is reported in.
    """
    declared = set(EXERCISES.values())
    expected = set(PROBES) | {f"os.path.{name}" for name in OSPATH_PROBES}
    missing = sorted(expected - declared)
    assert not missing, (
        f"{len(missing)} probe(s) in the registry are never exercised by a "
        f"positive control, so nothing would notice if the detector stopped "
        f"matching them: {missing}\n"
        f"  Add an entry to FIRES and EXERCISES for each."
    )
    unknown = sorted(declared - expected)
    assert not unknown, (
        f"controls declare probes that are not in the registry: {unknown}"
    )


@pytest.mark.parametrize(
    "name", [*sorted(PASSES), *sorted(PASSES_ABOVE_FLOOR)])
def test_the_correct_spellings_still_pass(name: str) -> None:
    source = PASSES.get(name) or PASSES_ABOVE_FLOOR[name]
    assert not unguarded_probes(source), (
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
