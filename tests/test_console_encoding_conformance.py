"""Every console entry point must force its own stdout to UTF-8, first thing.

This is the *encoding* direction of the bug that
``test_subprocess_encoding_conformance.py`` scans for in the *decoding*
direction. There, a subprocess' UTF-8 output was read back through cp1252;
here, our own UTF-8 text is written out through cp1252.

``sys.stdout``'s encoding is ``locale.getpreferredencoding(False)`` whenever
stdout is not a console -- which is to say, whenever the caller is a pipe, a
file, CI, or an agent. On Windows that is cp1252, and cp1252 cannot encode
``U+2192``. Measured on this repository, 2026-08-05::

    $ python backlog_tool.py show 1
    UnicodeEncodeError: 'charmap' codec can't encode character '\\u2192'
    in position 5548: character maps to <undefined>

**Interactively it works.** Python 3.6+ writes to a real Windows console
through ``WriteConsoleW``, which takes the text as UTF-16 and never consults
the code page, so ``backlog show`` is fine for a human at a terminal and
raises for the agent that pipes it. That asymmetry is why this needs a scan:
the failing configuration is the unattended one, which is precisely the one
nobody watches.

``operator_console.enable_utf8_output()`` has existed since the Windows-native
work (``specs/003-windows-native-operator``) and fixes this in two lines. The
defect was never a missing capability -- it was three of the five declared
console scripts never calling it, and nothing that could notice. A rule
enforced by remembering is that entry point's history, not a rule.

Why the call must be the *first* statement
------------------------------------------

Not style. ``argparse`` writes to stdout and stderr before ``main`` gets a
value back: ``--help`` prints the whole parser, and a bad argument prints a
usage block and raises ``SystemExit``. Both render text this repository
controls (subcommand help strings, and interpolated paths, which on this
machine include a user directory). A guard installed after ``parse_args`` is
a guard that is not installed on either of those paths.

The scan therefore requires the call to precede everything except a
docstring, and it also requires the name to be the real one: a module-level
``def enable_utf8_output(): pass`` would satisfy a scan that only looked for
a call, while doing nothing at all.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from test_dependency_declaration_conformance import parse_scalar_strings

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

#: The helper every console entry point has to call, and where it lives. The
#: module is checked as well as the name because a local definition of the
#: same name is the one way to pass this scan while shipping the bug.
GUARD = "enable_utf8_output"
GUARD_MODULE = "operator_console"

#: The table PEP 621 puts console scripts in. This is the population rather
#: than "every module with a ``__main__`` block", because it is the list pip
#: actually installs entry points from -- a module can grow and lose a
#: debugging ``__main__`` guard without ever being something a user runs.
SCRIPTS_TABLE = "project.scripts"

#: Entry points that are not in ``[project.scripts]`` and still are ones.
#: ``setup_tools`` is run by ``setup.sh``/``setup.ps1`` as a module, before
#: anything is installed -- so it cannot be a console script, and it prints
#: the same box-drawing output ``operator`` does. Named here rather than
#: inferred, and ``test_the_named_extras_exist`` keeps the names honest.
ALSO_ENTRY_POINTS = ("setup_tools",)


def console_entry_points(text):
    """``{script name: (module, function)}`` for every declared console script.

    Reuses the dependency scan's TOML reader rather than carrying a second
    one. That reader masks strings and comments before reading any line and
    is cross-checked against ``tomllib`` over an adversarial corpus; a second
    hand-rolled copy here is the thing that would drift.
    """
    found = {}
    for (table, key), value in parse_scalar_strings(text).items():
        if table != SCRIPTS_TABLE:
            continue
        module, _, function = value.partition(":")
        found[key] = (module, function or "main")
    return found


def _module_scope_statements(body):
    """Every statement that runs in module scope, control flow included.

    Descends into ``if``/``try``/``for``/``while``/``with`` because a binding
    in there still changes what ``main`` resolves at call time, and stops at
    ``def``/``class`` because a name bound inside one of those is local to it
    and cannot.
    """
    for node in body:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            yield from _module_scope_statements(getattr(node, field, []) or [])
        for handler in getattr(node, "handlers", []) or []:
            yield from _module_scope_statements(handler.body)


def _is_the_real_import(node):
    """True for exactly ``from operator_console import enable_utf8_output``."""
    if not isinstance(node, ast.ImportFrom) or node.module != GUARD_MODULE:
        return False
    return any(alias.name == GUARD and alias.asname in (None, GUARD)
               for alias in node.names)


def _bound_names(node):
    """The names a single statement binds in the scope that contains it."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0]
                for alias in node.names]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    targets = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [item.optional_vars for item in node.items
                   if item.optional_vars is not None]
    names = []
    for target in targets:
        names.extend(child.id for child in ast.walk(target)
                     if isinstance(child, ast.Name))
    return names


def _guard_binding_problem(tree):
    """Why the module's ``GUARD`` name is not trustworthy, or ``None``.

    Existence of the import is not enough. The name has to be bound by that
    import, unconditionally, and nothing may rebind it afterwards -- a
    ``def``, an ``= lambda: None``, or a second import of the same name from
    somewhere else all leave the call site spelled identically while calling
    something that does nothing.
    """
    unconditional = [node for node in tree.body if _is_the_real_import(node)]
    rebindings = [node for node in _module_scope_statements(tree.body)
                  if GUARD in _bound_names(node) and not _is_the_real_import(node)]
    if rebindings:
        first = rebindings[0]
        return (f"line {first.lineno}: {GUARD} is rebound at module level, so "
                f"the call in the entry point need not reach "
                f"{GUARD_MODULE}.{GUARD}")
    if not unconditional:
        return (f"never imports {GUARD} from {GUARD_MODULE} at module level "
                f"unconditionally")
    return None


def _first_real_statement(func):
    """The first statement of ``func``, looking past a docstring."""
    body = func.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return body[0] if body else None


def _is_guard_call(statement):
    """True for a bare ``enable_utf8_output()`` expression statement."""
    if not isinstance(statement, ast.Expr):
        return False
    call = statement.value
    return (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == GUARD
            and not call.args and not call.keywords)


def unguarded(source, function_name):
    """Why ``function_name`` fails the rule, or ``None`` when it passes.

    A reason rather than a bool so a failure names the line to go and read.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:                            # pragma: no cover
        return f"could not be parsed: {exc}"

    defined = [node for node in _module_scope_statements(tree.body)
               if function_name in _bound_names(node)]
    if not defined:
        return f"has no module-level def {function_name}(...)"
    # The *last* binding is the one that ends up on the module, so it is the
    # one the console script calls. Reading the first would let a correct
    # earlier copy vouch for whatever replaced it -- and the replacement need
    # not be another `def`: `main = _broken` rebinds it just as well.
    func = defined[-1]
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (f"line {func.lineno}: {function_name} is rebound after its "
                f"def, so the function this scan can read is not the one "
                f"that runs")

    # A decorator runs before the body does, and it can both write to stdout
    # itself and replace the function with a wrapper that writes before it
    # delegates. Either way the guard is no longer the first thing that runs,
    # so the shape this scan certifies is no longer the shape being shipped.
    if func.decorator_list:
        return (f"line {func.lineno}: {function_name}() is decorated, and a "
                f"decorator runs before the body, so {GUARD}() is no longer "
                f"the first thing to happen")

    binding = _guard_binding_problem(tree)
    if binding is not None:
        return binding

    statement = _first_real_statement(func)
    if statement is None:
        return f"{function_name}() has an empty body"
    if not _is_guard_call(statement):
        return (f"line {statement.lineno}: {function_name}() does something "
                f"before calling {GUARD}(); argparse can write to stdout "
                f"first")
    return None


def _entry_points():
    """Every entry point this repository ships, as ``(module, function)``."""
    declared = console_entry_points(PYPROJECT.read_text(encoding="utf-8"))
    found = dict(declared)
    for module in ALSO_ENTRY_POINTS:
        found.setdefault(module, (module, "main"))
    return found


ENTRY_POINTS = _entry_points()


# --------------------------------------------------------------------------
# The scan over real repository code.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("script", sorted(ENTRY_POINTS))
def test_every_console_entry_point_forces_utf8_before_it_writes(script):
    module, function = ENTRY_POINTS[script]
    path = REPO / f"{module}.py"
    assert path.is_file(), f"{script} names {module}, which is not a module here"
    problem = unguarded(path.read_text(encoding="utf-8"), function)
    assert problem is None, (
        f"{module}.py ({script}) {problem}. Call {GUARD}() as the first "
        f"statement of {function}(); see this file's docstring for why "
        f"stdout is cp1252 for every caller that is not a terminal.")


# --------------------------------------------------------------------------
# Controls. A scan with an empty population reports the whole tree clean,
# which reads exactly like success.
# --------------------------------------------------------------------------

def test_the_population_is_not_empty_and_holds_what_we_ship():
    assert ENTRY_POINTS, "no entry points found -- the scan proves nothing"
    # `backlog` is the regression that motivated this file and `operator` is
    # the one users type most; naming them stops a parser change from
    # silently emptying the population while the parametrised test still
    # reports green over whatever is left.
    for expected in ("backlog", "operator", "handoff"):
        assert expected in ENTRY_POINTS, f"{expected} vanished from the scan"


def test_the_named_extras_exist():
    for module in ALSO_ENTRY_POINTS:
        assert (REPO / f"{module}.py").is_file(), (
            f"ALSO_ENTRY_POINTS names {module}, which is not a module here")


def test_the_declared_scripts_are_read_from_the_scripts_table_only():
    text = (
        '[project]\n'
        'dependencies = ["ghost"]\n'
        '[project.scripts]\n'
        'operator = "copilot_operator:main"\n'
        'backlog = "backlog_tool:main"\n'
        '[project.entry-points."pytest11"]\n'
        'notascript = "somewhere:plugin"\n'
    )
    assert console_entry_points(text) == {
        "operator": ("copilot_operator", "main"),
        "backlog": ("backlog_tool", "main"),
    }


def test_an_entry_point_with_no_function_defaults_to_main():
    text = '[project.scripts]\nthing = "thing_module"\n'
    assert console_entry_points(text) == {"thing": ("thing_module", "main")}


GOOD = '''\
from operator_console import enable_utf8_output


def main(argv=None) -> int:
    enable_utf8_output()
    print("hello")
    return 0
'''

GOOD_WITH_DOCSTRING = '''\
from operator_console import enable_utf8_output


def main(argv=None) -> int:
    """A docstring is not a statement that can write to stdout."""
    enable_utf8_output()
    return 0
'''

MISSING = '''\
import argparse


def main(argv=None) -> int:
    print("hello")
    return 0
'''

TOO_LATE = '''\
import argparse
from operator_console import enable_utf8_output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    enable_utf8_output()
    return 0
'''

SHADOWED = '''\
from operator_console import enable_utf8_output


def enable_utf8_output():
    pass


def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

IMPORTED_FROM_ELSEWHERE = '''\
from somewhere_else import enable_utf8_output


def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

RENAMED_ON_IMPORT = '''\
from operator_console import enable_utf8_output as _utf8


def main(argv=None) -> int:
    _utf8()
    return 0
'''

NO_MAIN = '''\
from operator_console import enable_utf8_output


def run(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

REDEFINED_MAIN = '''\
from operator_console import enable_utf8_output


def main(argv=None) -> int:
    enable_utf8_output()
    return 0


def main(argv=None) -> int:
    print("this is the one that runs")
    return 0
'''

DECORATED_MAIN = '''\
from operator_console import enable_utf8_output


def announce(func):
    print("about to run")
    return func


@announce
def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

LOCAL_IMPORT_AFTER_THE_CALL = '''\
def main(argv=None) -> int:
    enable_utf8_output()
    from operator_console import enable_utf8_output
    return 0
'''

IMPORT_IS_CONDITIONAL = '''\
if False:
    from operator_console import enable_utf8_output


def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

REBOUND_TO_A_LAMBDA = '''\
from operator_console import enable_utf8_output

enable_utf8_output = lambda: None


def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

REIMPORTED_FROM_ELSEWHERE = '''\
from operator_console import enable_utf8_output
from somewhere_else import enable_utf8_output


def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

REBOUND_CONDITIONALLY = '''\
from operator_console import enable_utf8_output

if sys.platform == "win32":
    def enable_utf8_output():
        pass


def main(argv=None) -> int:
    enable_utf8_output()
    return 0
'''

REASSIGNED_MAIN = '''\
from operator_console import enable_utf8_output


def _broken(argv=None) -> int:
    print("no guard here")
    return 0


def main(argv=None) -> int:
    enable_utf8_output()
    return 0


main = _broken
'''

FIRES = {
    "the call is absent": MISSING,
    "the call is not first": TOO_LATE,
    "a local def shadows the import": SHADOWED,
    "the name comes from another module": IMPORTED_FROM_ELSEWHERE,
    "the import is renamed so the call is not the name": RENAMED_ON_IMPORT,
    "the named function does not exist": NO_MAIN,
    "a later def replaces a compliant one": REDEFINED_MAIN,
    "a decorator runs before the body": DECORATED_MAIN,
    "the import is function-local and after the call":
        LOCAL_IMPORT_AFTER_THE_CALL,
    "the import only happens conditionally": IMPORT_IS_CONDITIONAL,
    "the name is rebound to a do-nothing lambda": REBOUND_TO_A_LAMBDA,
    "a second import takes the name over": REIMPORTED_FROM_ELSEWHERE,
    "a conditional def takes the name over": REBOUND_CONDITIONALLY,
    "the entry point is reassigned after its def": REASSIGNED_MAIN,
}

PASSES = {
    "the plain spelling": GOOD,
    "a docstring precedes the call": GOOD_WITH_DOCSTRING,
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_the_detector_fires(name):
    assert unguarded(FIRES[name], "main") is not None, (
        f"the detector missed: {name}")


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_detector_leaves_compliant_code_alone(name):
    assert unguarded(PASSES[name], "main") is None, (
        f"the detector fired on compliant code: {name}")


def test_the_finding_names_the_line():
    """The number is computed from the fixture, not pinned to its layout.

    Asserting the literal "line 6" would pass for a detector that always said
    6, and would break on an edit to the fixture that changed nothing about
    the behaviour. Asserting only that *some* number appears would accept a
    detector that reported the wrong one.
    """
    offending = TOO_LATE.splitlines().index(
        "    parser = argparse.ArgumentParser()") + 1
    problem = unguarded(TOO_LATE, "main")
    assert f"line {offending}" in problem, problem
