"""Every third-party module this repository imports must be *declared*.

On 2026-08-01 CI went red on the two Python 3.12 legs and stayed green on the
four others. ``tests/test_worktree_install_guard.py`` imports ``setuptools``
to compare our build backend's hooks against ``setuptools.build_meta``'s, and
nothing in ``pyproject.toml`` ever said so. It worked anyway for as long as it
did because Python shipped ``setuptools`` in every fresh environment -- until
3.12, which stopped. The import had been ambient the whole time; the
interpreter release is what turned an undeclared dependency into a failure.

Every property of that bug is worth naming, because they are the properties of
the whole class:

* **A full green local suite could not see it.** The author's machine is 3.11
  with ``setuptools`` ambiently installed. 1976 tests passed. The environment
  is not in the test run's control, so the run cannot report on it.
* **Four adversarial review passes across three models could not see it.**
  Reviewers read diffs, and an *absent* line in ``pyproject.toml`` is not in
  the diff. Nothing about ``import setuptools`` looks wrong; it is wrong only
  relative to a file it does not mention.
* **The fix was a one-line addition to the ``dev`` extra, protected by
  nothing.** The next undeclared import would have cost exactly the same
  amount to find, and would have been found the same way: by a stranger's CI,
  on a Python version nobody was testing on at the time.

That last point is the argument for this file. The repository's answer to a
defect class it has paid for is a conformance scan -- ``bash`` 3.2 constructs,
statements after ``return``, unguarded ``Path.resolve``, tri-state presence
probes -- because **a rule enforced against one file is not a rule, it is that
file's history**, and a rule enforced against one *incident* is not a rule at
all.

**What it demands.** That the top-level name of every ``import`` in every
first-party ``*.py`` file resolves to one of three things: the standard
library, another module in this repository, or a distribution named in
``pyproject.toml`` -- under ``[project] dependencies``, any
``[project.optional-dependencies]`` extra, or ``[build-system] requires``.

**No exemption list, deliberately.** A guarded ``try: import x / except
ImportError`` is reported too, and so is an import under ``TYPE_CHECKING``.
Both are still a name this repository expects the environment to be able to
supply, and the repository's position on the softer spelling of that bet is
already on record: a ``pytest.importorskip`` "would turn 'PyYAML is missing'
into a silent skip ... the assertions below would stop running and report that
by saying nothing at all" (``tests/test_git_identity.py``). If a dependency is
genuinely optional, declare it in an extra and say so there. A scan people
argue with grows an exemption list, and an exemption list is where the next
one hides.

**Two axes this scan deliberately does not own.**

* *Standard-library modules that do not exist at the 3.10 floor* --
  ``tomllib`` and its kin -- belong to ``tests/test_python_floor_conformance
  .py``, which already gates them and demands an ``ImportError`` guard. They
  are listed in :data:`STDLIB_AFTER_FLOOR` here purely so this scan stays
  silent about them rather than reporting a second, differently-worded
  failure on the 3.10 legs only.
* *Declared-but-unused* distributions. A test-runner plugin is declared and
  never imported, so the reverse check has a false-positive shape this one
  does not. A narrow check that is never wrong is worth more than a broad one
  that has to be argued with.

One asymmetry is intended rather than overlooked: a module that *was* standard
library at the floor and was removed later -- ``distutils``, gone in 3.12 --
is reported on the newer legs and not the older ones. That is the correct
verdict and it is the setuptools shape exactly: on 3.12 the name is not the
standard library's any more, so something has to ship it, so something has to
declare it. Resolving one costs two edits rather than one, and the second is
not obvious: declaring ``setuptools`` is not enough, because ``setuptools``
does not *record* ``distutils`` in its metadata, so the mapping must be added
to :data:`IMPORT_NAMES` and excused in :data:`IMPORT_NAMES_UNRECORDED` with
the reason written next to it. That is a deliberate cost. A name the standard
library no longer owns is a supply-chain decision, and it should read like
one.

**Why the standard library is ``sys.stdlib_module_names`` and not what
imports.** The set includes names that cannot be imported on the running
platform -- ``fcntl`` on Windows, ``winreg`` on Linux -- so an allow-list
built by trying to import each one would be *narrower* on some legs than on
others. ``import fcntl`` would then be reported on the two Windows legs and
nowhere else: a red CI that no dependency declaration can fix, in the one
failure shape this repository has already learned to hate. The documented set
is platform-independent for a given minor version by design, so it is the one
used here. Guarding platform-specific imports is a real and separate concern;
it is not this scan's, and pretending otherwise would cost the six other legs
their credibility.

The detector has positive controls asserting it fires and negative controls
asserting the legitimate spellings still pass, and a control that empties the
allow-list to prove the scan reports on *real* repository code rather than
only on synthetic strings. A detector broken into matching nothing reports the
whole tree clean, which reads exactly like success.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

# The project floor is 3.10, where `tomllib` does not exist. The guarded
# spelling is the one `tests/test_python_floor_conformance.py` requires, and
# it is used here only to cross-check the narrow parser below against the
# reference implementation on the legs that have one.
try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

REPO = Path(__file__).resolve().parent.parent

PYPROJECT = REPO / "pyproject.toml"

NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__",
              ".specify", "build", "dist", ".eggs", "site-packages")

#: Directory *prefixes* that name a virtual environment. Matching by exact
#: name misses ``.venv-3.10``, ``venv-linux`` and ``.tox``, and a scan that
#: walks into ``site-packages`` reports thousands of findings against code
#: the contributor did not write -- which reads as "your change broke
#: dependency conformance" and is the fastest way to get a scan switched off.
NOT_SOURCE_PREFIXES = (".venv", "venv", ".tox", ".nox", ".env", "env",
                       "virtualenv")


def _is_environment(directory):
    """True if ``directory`` looks like, or declares itself, a virtualenv.

    ``pyvenv.cfg`` is the definitive marker and costs one ``exists`` per
    directory; the name prefixes catch the rest. Both are needed: a venv can
    be called anything, and a partially-built one may have no marker yet.
    """
    if directory.name.startswith(NOT_SOURCE_PREFIXES):
        return True
    try:
        return (directory / "pyvenv.cfg").exists()
    except OSError:
        # An unexaminable directory is not evidence of a source tree.
        # Resolving toward "environment" here would hide real files, so
        # resolve toward "source" and let the scan look.
        return False


#: Names that are the standard library on some supported interpreter but not
#: on the 3.10 floor. Owned by the floor scan; excluded here so a correctly
#: guarded import is not reported twice in two different vocabularies.
STDLIB_AFTER_FLOOR = frozenset({"tomllib"})

#: Distributions whose import name is not derivable from their name on PyPI.
#: ``test_the_import_names_we_claim_are_the_ones_installed`` checks every
#: entry against the installed metadata, so this table is a claim under test
#: rather than an assumption.
IMPORT_NAMES = {
    "pyyaml": frozenset({"yaml"}),
}

#: ``(distribution, module)`` pairs that a distribution genuinely ships but
#: its installed metadata does not record. Empty today, and the mechanism
#: exists anyway because without it one documented resolution in this file is
#: undeliverable: ``setuptools`` ships ``distutils`` on 3.12, but
#: ``packages_distributions()`` reports only ``setuptools``, ``pkg_resources``
#: and ``_distutils_hack``, so claiming the mapping would fail the very test
#: that keeps :data:`IMPORT_NAMES` honest. An exception that has to be written
#: down, next to its reason, is the difference between an escape hatch and a
#: hole; :func:`checkable_claim` is what applies it, and it is controlled
#: below against synthetic input so that an empty table is not untested
#: machinery.
IMPORT_NAMES_UNRECORDED = frozenset()


def checkable_claim(distribution, names, unrecorded=IMPORT_NAMES_UNRECORDED):
    """The subset of ``names`` that installed metadata can be asked about."""
    excused = {module for owner, module in unrecorded if owner == distribution}
    return frozenset(names) - excused


#: PEP 508 names, loosely: enough to find where the name stops.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


# --------------------------------------------------------------------------
# Reading pyproject.toml
#
# `tomllib` arrived in 3.11 and this suite must run on 3.10, so the parser
# below is the one that decides on every leg. It is narrow on purpose: it
# understands table headers and arrays of strings, which is the whole of what
# PEP 621 uses for dependency lists, and it ignores everything else rather
# than guessing at it.
#
# It is *not* line-based, and that is the whole design. A line-based scan
# cannot see TOML's multi-line strings, and a `description = """..."""` whose
# text happens to contain a line reading `dependencies = ["ghost"]` is then
# parsed as an assignment -- silently *widening* the allow-list, which is the
# one failure direction this file cannot afford. So the document is masked
# once, in a single pass that knows about comments and about all four string
# forms, and the line structure is read off the mask.
#
# `test_the_narrow_parser_agrees_with_tomllib` pins it against the reference
# implementation over a corpus of adversarial documents, not merely over this
# repository's own pyproject.toml -- which exercises none of these shapes and
# so proved nothing about them.
# --------------------------------------------------------------------------

_BLANK = "\x00"

#: Longest first: ``\"\"\"`` must win over ``\"``.
_DELIMITERS = ('"""', "'''", '"', "'")

#: The escapes TOML defines for basic strings. Anything else after a
#: backslash is passed through as itself, which is wrong in general and
#: harmless here -- what matters is that the backslash never terminates the
#: string, and that ``\\n`` does not silently become the letter ``n`` in the
#: middle of a distribution name.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"',
            "b": "\b", "f": "\f", "'": "'"}


def _read_string(text, start):
    """``(index past the closing delimiter, value)`` for the literal at
    ``start``, or ``None`` if it never closes."""
    for delimiter in _DELIMITERS:
        if text.startswith(delimiter, start):
            break
    else:
        return None
    literal = delimiter[0] == "'"   # '' strings have no escapes at all
    multiline = len(delimiter) == 3
    index = start + len(delimiter)
    value = []
    while index < len(text):
        char = text[index]
        if not literal and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            value.append(_ESCAPES.get(following, following))
            index += 2
            continue
        if char == "\n" and not multiline:
            return None                     # unterminated: not a string
        if text.startswith(delimiter, index):
            return index + len(delimiter), "".join(value)
        value.append(char)
        index += 1
    return None


def scan(text):
    """``(masked, spans)`` -- the document with strings and comments blanked.

    ``masked`` has the same length as ``text`` and the same newlines in the
    same places, so a character offset means the same thing in both. Every
    comment, and the interior *and delimiters* of every string literal, is
    replaced by a blank. A ``[``, ``]``, ``=``, ``.`` or ``#`` therefore
    survives in ``masked`` only where it is structure.

    ``spans`` is ``[(start, end, value)]`` for each string literal, in
    document order. Comments are masked but never recorded, so a
    commented-out ``# "ghost",`` inside a dependency array contributes no
    value -- which is the difference between a comment control with teeth and
    one without.
    """
    masked = list(text)
    spans = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "#":
            while index < len(text) and text[index] != "\n":
                masked[index] = _BLANK
                index += 1
            continue
        if char in "\"'":
            found = _read_string(text, index)
            if found is None:
                masked[index] = _BLANK
                index += 1
                continue
            end, value = found
            spans.append((index, end, value))
            for position in range(index, end):
                if text[position] != "\n":
                    masked[position] = _BLANK
            index = end
            continue
        index += 1
    return "".join(masked), spans


def dotted_name(text, spans, low, high):
    """``text[low:high]`` as a dotted name, with each segment unquoted.

    ``[project."optional-dependencies"]`` is valid TOML and names exactly the
    same table as ``[project.optional-dependencies]``. Reading the header
    literally loses every extra declared in it -- a false positive, and one
    that arrives the day somebody reformats a file this scan only reads.
    """
    starts = {start: (end, value) for start, end, value in spans}
    segments = []
    current = []
    index = low
    while index < high:
        if index in starts:
            end, value = starts[index]
            current.append(value)
            index = end
            continue
        char = text[index]
        if char == ".":
            segments.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    segments.append("".join(current).strip())
    return ".".join(segments)


def _depth(fragment):
    """Net ``[`` minus ``]`` in a fragment of *masked* text."""
    return fragment.count("[") - fragment.count("]")


def _split_table_and_key(table, key):
    """Fold a dotted key into the table path, the way TOML defines it.

    ``optional-dependencies.dev = [...]`` under ``[project]`` declares
    exactly the table ``[project.optional-dependencies]`` and the key
    ``dev``. Keeping the dotted spelling separate means the same declaration
    is matched in one form and missed in the other.
    """
    head, _, last = key.rpartition(".")
    if not head:
        return table, key
    return (f"{table}.{head}" if table else head), last


def parse_string_arrays(text):
    """``{(table, key): [strings]}`` for every array-of-strings assignment."""
    masked, spans = scan(text)
    arrays = {}
    table = ""
    key = None
    values_from = 0
    depth = 0
    offset = 0
    for line in masked.split("\n"):
        line_start = offset
        offset += len(line) + 1
        if key is None:
            stripped = line.strip()
            if not stripped:
                continue
            if (stripped.startswith("[") and stripped.endswith("]")
                    and "=" not in stripped):
                low = line_start + line.index("[") + 1
                high = line_start + line.rindex("]")
                if masked[low] == "[" and masked[high - 1] == "]":
                    low, high = low + 1, high - 1   # [[array.of.tables]]
                table = dotted_name(text, spans, low, high)
                continue
            equals = line.find("=")
            if equals < 0 or not line[equals + 1:].strip().startswith("["):
                continue
            key = dotted_name(text, spans, line_start, line_start + equals)
            values_from = line_start + equals + 1
            depth = _depth(line[equals + 1:])
        else:
            depth += _depth(line)
        if depth <= 0:
            values_to = line_start + len(line)
            arrays[_split_table_and_key(table, key)] = [
                value for start, _, value in spans
                if values_from <= start < values_to]
            key = None
            depth = 0
    return arrays


def parse_scalar_strings(text):
    """``{(table, key): value}`` for every ``key = "string"`` assignment."""
    masked, spans = scan(text)
    starts = {start: value for start, _, value in spans}
    scalars = {}
    table = ""
    offset = 0
    for line in masked.split("\n"):
        line_start = offset
        offset += len(line) + 1
        stripped = line.strip()
        if not stripped:
            continue
        if (stripped.startswith("[") and stripped.endswith("]")
                and "=" not in stripped):
            table = dotted_name(text, spans, line_start + line.index("[") + 1,
                                line_start + line.rindex("]"))
            continue
        equals = line.find("=")
        if equals < 0:
            continue
        rest = line[equals + 1:]
        value_at = line_start + equals + 1 + (len(rest) - len(rest.lstrip()))
        if value_at in starts:
            key = dotted_name(text, spans, line_start, line_start + equals)
            scalars[_split_table_and_key(table, key)] = starts[value_at]
    return scalars


#: The three places PEP 621 lets a project ask for a distribution, as#: fully-qualified dotted paths. ``[project] optional-dependencies.dev = [...]``
#: and ``[project.optional-dependencies] dev = [...]`` are the same table
#: spelled two ways, so the table and the key are joined before matching
#: rather than compared separately.
_DEPENDENCY_PATHS = ("project.dependencies", "build-system.requires")
_EXTRAS_PREFIX = "project.optional-dependencies."


def declared_requirements(text):
    """Every requirement string ``pyproject.toml`` asks for, from anywhere."""
    found = []
    for (table, key), values in parse_string_arrays(text).items():
        path = f"{table}.{key}" if table else key
        if path in _DEPENDENCY_PATHS or path.startswith(_EXTRAS_PREFIX):
            found.extend(values)
    return found



def _requirements_from_tomllib(text):
    """The same thing, via the reference parser. ``None`` before 3.11."""
    if tomllib is None:
        return None
    data = tomllib.loads(text)
    found = list(data.get("project", {}).get("dependencies", []))
    for values in data.get("project", {}).get("optional-dependencies",
                                              {}).values():
        found.extend(values)
    found.extend(data.get("build-system", {}).get("requires", []))
    return found


# --------------------------------------------------------------------------
# Distribution names to import names
# --------------------------------------------------------------------------

def normalize(name):
    """PEP 503 normalisation, so ``Name_With.Dots`` and ``name-with-dots``
    are one distribution rather than two."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def distribution_name(requirement):
    """The distribution a PEP 508 requirement names, normalised."""
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        return None
    return normalize(match.group(1))


def import_names_for(distribution):
    """The top-level module names a distribution is expected to provide."""
    known = IMPORT_NAMES.get(distribution)
    if known is not None:
        return set(known)
    return {distribution.replace("-", "_")}


def allowed_import_names(requirements):
    """Every module name the declared dependencies entitle us to import."""
    names = set()
    for requirement in requirements:
        distribution = distribution_name(requirement)
        if distribution is not None:
            names |= import_names_for(distribution)
    return names


# --------------------------------------------------------------------------
# Reading the repository
# --------------------------------------------------------------------------

def _walk(directory):
    """Every ``*.py`` under ``directory``, pruning what is not our source."""
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            if entry.name in NOT_SOURCE or _is_environment(entry):
                continue
            yield from _walk(entry)
        elif entry.suffix == ".py":
            yield entry


def _python_sources():
    """Every ``*.py`` in the repository, discovered rather than listed.

    Pruning happens during the walk rather than as a filter afterwards: a
    ``rglob`` descends into ``site-packages`` and ``.tox`` before anything
    gets to reject them, which on a developer machine is tens of thousands of
    files and several seconds per collection.

    The filter is on the repository-relative path. An absolute-path filter
    matches ``.worktrees`` for *every* file when the checkout itself is a
    worktree -- which is where every agent on this project works -- and an
    empty population passes every assertion below.
    """
    return sorted(_walk(REPO))


def import_roots():
    """Directories that will be on ``sys.path`` when this suite runs.

    The repository root, plus whatever ``[tool.pytest.ini_options]
    pythonpath`` adds. This is read from ``pyproject.toml`` rather than
    hardcoded so that adding a source directory cannot silently desynchronise
    the scan from the thing it scans.
    """
    arrays = parse_string_arrays(PYPROJECT.read_text(encoding="utf-8"))
    added = arrays.get(("tool.pytest.ini_options", "pythonpath"), [])
    return [REPO] + [REPO / entry for entry in added]


def module_names(paths, roots):
    """The importable module names ``paths`` provide, given ``roots``.

    A file contributes a name only when it sits *directly* in a directory
    that is on the path. ``docs/examples/requests.py`` is not importable as
    ``requests``, so it must not be allowed to answer for ``import requests``
    -- which, when the answer is drawn from every ``*.py`` in the tree, is
    exactly what it does: one stray file anywhere silently switches the
    detector off for that distribution across the whole repository, and every
    control still passes because the controls score against a fixed set.

    Deeper files are still *scanned*. They just do not get a vote on what
    counts as ours.
    """
    resolved = [Path(root).resolve() for root in roots]
    names = set()
    for path in paths:
        if Path(path).resolve().parent in resolved:
            names.add(Path(path).stem)
    return names


def first_party_modules():
    """Module names this repository itself provides.

    ``tests/`` counts because ``pyproject.toml`` puts it on the path
    (``pythonpath = ["tests"]``) precisely so the conformance scans can
    import one another's AST helpers.
    """
    return module_names(_python_sources(), import_roots())


def _literal_import_arguments(node):
    """The module name a dynamic-import call names, if it names one literally.

    ``importlib.import_module("requests")`` and ``__import__("requests")``
    need the distribution just as much as the statement form does, and the
    statement form is the only one the AST's import nodes describe.

    Two restraints, both deliberate. The receiver of an attribute call must
    be spelled ``importlib``, so that somebody else's ``loader.import_module``
    is not reported as a dependency it has nothing to do with. And a
    non-literal argument is skipped rather than guessed at: ``__import__(name)``
    proves nothing, and a finding no reader can act on and no declaration can
    silence is worse than no finding. ``importlib`` aliased to another name
    is missed for the same reason -- this scan reports what it can prove.
    """
    function = node.func
    if isinstance(function, ast.Name):
        if function.id not in ("__import__", "import_module"):
            return None
    elif isinstance(function, ast.Attribute):
        if function.attr != "import_module":
            return None
        if not (isinstance(function.value, ast.Name)
                and function.value.id == "importlib"):
            return None
    else:
        return None
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.split(".")[0]
    return None


def top_level_imports(source):
    """``(line, top-level module)`` for every absolute import in ``source``.

    Relative imports are skipped: ``from . import x`` names nothing the
    environment has to supply.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name.split(".")[0]))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.append((node.lineno, node.module.split(".")[0]))
        elif isinstance(node, ast.Call):
            module = _literal_import_arguments(node)
            if module:
                found.append((node.lineno, module))
    return sorted(set(found))



def undeclared_imports(source, allowed):
    """``(line, module)`` for every import ``allowed`` does not cover."""
    return [(line, module) for line, module in top_level_imports(source)
            if module not in allowed]


STDLIB = frozenset(sys.stdlib_module_names) | STDLIB_AFTER_FLOOR

DECLARED = allowed_import_names(
    declared_requirements(PYPROJECT.read_text(encoding="utf-8")))

ALLOWED = STDLIB | first_party_modules() | DECLARED


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", _python_sources(),
                         ids=lambda p: str(p.relative_to(REPO)).replace("\\", "/"))
def test_every_import_is_stdlib_first_party_or_declared(path):
    findings = undeclared_imports(path.read_text(encoding="utf-8"), ALLOWED)
    assert not findings, "\n".join(
        f"{path.relative_to(REPO)}:{line}: {module!r} is neither the standard "
        f"library nor a module of this repository, and no dependency in "
        f"pyproject.toml provides it. Declare it (add it to the `dev` extra, "
        f"or to [project] dependencies) rather than relying on it being "
        f"ambiently installed."
        for line, module in findings
    )


def test_the_population_is_not_empty_and_holds_what_we_ship():
    """A filter bug that empties the population passes every test above it."""
    found = {str(p.relative_to(REPO)).replace("\\", "/")
             for p in _python_sources()}
    assert len(found) > 20, f"suspiciously few Python files: {sorted(found)}"
    for expected in ("setup_tools.py", "copilot_operator.py",
                     "worktree_guard_backend.py", "verify_cross_platform.py",
                     "tests/test_worktree_install_guard.py",
                     "tests/test_git_identity.py"):
        assert expected in found, f"{expected} missing from the scan"


def test_the_population_excludes_what_is_not_our_source(tmp_path):
    """The test above passes with the exclusion filter deleted entirely.

    "Everything we ship is in the list" is satisfied by a list containing
    everything on the disk -- including the tens of thousands of files in a
    ``site-packages`` or a ``.tox``, every one of which would be reported
    against a contributor who did not write it. Over-population and
    under-population are different bugs and need different controls.
    """
    for name in (".venv", "venv-3.10", ".tox", "build", "site-packages",
                 "node_modules", "__pycache__", ".worktrees"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "intruder.py").write_text("import requests\n",
                                                     encoding="utf-8")
    unmarked = tmp_path / "quietly_an_environment"
    unmarked.mkdir()
    (unmarked / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    (unmarked / "intruder.py").write_text("import requests\n", encoding="utf-8")

    (tmp_path / "ours.py").write_text("import os\n", encoding="utf-8")
    nested = tmp_path / "package" / "deeper"
    nested.mkdir(parents=True)
    (nested / "also_ours.py").write_text("import os\n", encoding="utf-8")

    found = {path.name for path in _walk(tmp_path)}
    assert found == {"ours.py", "also_ours.py"}, sorted(found)


def test_only_files_on_the_import_path_name_a_first_party_module(tmp_path):
    """A stray ``*.py`` deep in the tree must not widen the allow-list.

    This is the failure that survives every control in this file: taking the
    first-party names from the stem of every ``*.py`` anywhere means one
    ``docs/examples/requests.py`` -- not importable as ``requests``, not
    intended as anything -- silently switches the detector off for
    ``requests`` across the whole repository. Nothing goes red. The controls
    all score against :data:`CONTROL_ALLOWED`, which is fixed, so they keep
    passing while the real scan reports nothing.
    """
    root = tmp_path
    (root / "tests").mkdir()
    (root / "docs" / "examples").mkdir(parents=True)
    for path in (root / "ours.py", root / "tests" / "helper.py",
                 root / "docs" / "examples" / "requests.py"):
        path.write_text("", encoding="utf-8")

    names = module_names(_walk(root), [root, root / "tests"])
    assert names == {"ours", "helper"}, sorted(names)
    assert "requests" not in names, (
        "a file that is not on the import path named a first-party module"
    )


def test_no_first_party_module_shadows_an_installed_distribution():
    """The other half of the same problem, and the half a path check misses.

    A ``requests.py`` in the repository *root* is on the path, so it really
    would shadow the distribution -- and it would also switch this scan off
    for it. Either way it is worth knowing about, and it is the kind of file
    that arrives as a scratch script somebody forgot to delete.

    This project is itself installed on a developer machine, so its own
    modules are in ``packages_distributions()`` and are not shadows of
    anything. They are excluded by distribution name, read from the file
    rather than hardcoded.
    """
    from importlib.metadata import packages_distributions

    ours = normalize(parse_scalar_strings(
        PYPROJECT.read_text(encoding="utf-8"))[("project", "name")])
    installed = {module for module, distributions
                 in packages_distributions().items()
                 if any(normalize(d) != ours for d in distributions)}
    collisions = sorted(first_party_modules() & installed)
    assert not collisions, (
        f"{collisions} name both a module of this repository and an "
        f"installed distribution's top-level module. The repository's copy "
        f"wins on sys.path, which shadows the real one and silently exempts "
        f"it from this scan."
    )


def test_the_allow_list_is_populated_from_all_three_sources():
    """An empty allow-list fails loudly; a *partly* empty one is the risk.

    If the parser silently missed ``[project.optional-dependencies]`` the
    scan would still fail rather than pass -- but it would fail everywhere
    at once, which reads as a broken scan rather than a missing declaration.
    Pin the three names the repository actually declares.
    """
    assert "pytest" in DECLARED, DECLARED
    assert "yaml" in DECLARED, DECLARED       # PyYAML, dev extra
    assert "setuptools" in DECLARED, DECLARED  # dev extra and build-system
    assert "os" in ALLOWED and "sys" in ALLOWED, "stdlib missing from allow-list"
    assert "setup_tools" in ALLOWED, "first-party modules missing"


def test_the_scan_reports_on_real_repository_code_not_only_on_fixtures():
    """Empty the allow-list and a real file must produce findings.

    Every assertion above is satisfied by a collector that returns nothing.
    This is the control that says the whole path -- read, parse, compare --
    still reports when there is something to report, on code nobody wrote
    for this test.
    """
    source = (REPO / "tests" / "test_git_identity.py").read_text(encoding="utf-8")
    seen = {module for _, module in top_level_imports(source)}
    assert {"yaml", "subprocess", "git_identity"} <= seen, sorted(seen)
    assert undeclared_imports(source, frozenset()), (
        "an empty allow-list reported nothing; the scan is vacuous"
    )


# --------------------------------------------------------------------------
# Detector controls
# --------------------------------------------------------------------------

#: What the controls below are allowed to import: two stdlib names, the two
#: third-party distributions this repository declares, and one first-party
#: module. Fixed rather than derived, so a change to pyproject.toml cannot
#: quietly change what the controls prove.
CONTROL_ALLOWED = frozenset({"os", "sys", "pathlib", "typing", "__future__",
                             "importlib", "json",
                             "pytest", "yaml", "setup_tools"})

#: An undeclared import in every spelling Python offers. Each must be found.
#:
#: The module names are deliberately all different. When every case used
#: ``requests``, a detector hardcoded to find exactly that one name scored
#: ten green controls -- ten pieces of evidence that all say the same thing
#: cost the same to read as ten that say different things, and are worth a
#: tenth as much.
FIRES = {
    "plain import": "import requests\n",
    "dotted import": "import numpy.linalg\n",
    "aliased import": "import pandas as pd\n",
    "from-import": "from click import command\n",
    "from-import of a submodule": "from rich.console import Console\n",
    "second name in one import statement": "import os, boto3\n",
    "inside a function": (
        "def fetch():\n"
        "    import httpx\n"
        "    return httpx\n"
    ),
    # The soft spellings are reported too. Both still bet on the environment
    # being able to supply the name; see the module docstring.
    "guarded by ImportError": (
        "try:\n"
        "    import ujson\n"
        "except ImportError:\n"
        "    ujson = None\n"
    ),
    "under TYPE_CHECKING": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import sqlalchemy\n"
    ),
    "inside a class body": (
        "class Client:\n"
        "    import jinja2\n"
    ),
    # Dynamic forms. The name is still one the environment has to supply,
    # and the AST's import nodes describe none of these.
    "importlib.import_module": (
        "import importlib\n"
        "mod = importlib.import_module('psycopg2')\n"
    ),
    "import_module imported directly": (
        "from importlib import import_module\n"
        "mod = import_module('redis')\n"
    ),
    "dunder import": "mod = __import__('lxml')\n",
    "dynamic import of a submodule": (
        "import importlib\n"
        "mod = importlib.import_module('cryptography.fernet')\n"
    ),
}

#: Spellings that are correct and must not be reported.
PASSES = {
    "stdlib": "import os\n",
    "stdlib submodule": "import os.path\n",
    "stdlib from-import": "from pathlib import Path\n",
    "future import": "from __future__ import annotations\n",
    "declared third party": "import yaml\n",
    "declared third party from-import": "from yaml import safe_load\n",
    "declared test dependency": "import pytest\n",
    "first party": "import setup_tools\n",
    "first party from-import": "from setup_tools import install_source\n",
    "relative import": "from . import sibling\n",
    "relative dotted import": "from .helpers import thing\n",
    "relative parent import": "from .. import parent\n",
    "no imports at all": "value = 1\n",
    # Dynamic imports of names that are fine, and -- more importantly -- the
    # forms this scan must *not* guess at. `__import__(name)` names nothing
    # provable; reporting a variable would be a finding no reader could act
    # on and no declaration could silence.
    "dynamic import of stdlib": (
        "import importlib\n"
        "mod = importlib.import_module('json')\n"
    ),
    "dynamic import of a declared distribution": (
        "import importlib\n"
        "mod = importlib.import_module('yaml')\n"
    ),
    "dunder import of a variable": (
        "def load(name):\n"
        "    return __import__(name)\n"
    ),
    "import_module of a variable": (
        "import importlib\n"
        "def load(name):\n"
        "    return importlib.import_module(name)\n"
    ),
    "import_module of an f-string": (
        "import importlib\n"
        "def load(name):\n"
        "    return importlib.import_module(f'pkg.{name}')\n"
    ),
    "an unrelated method called import_module on our own object": (
        "loader = object()\n"
        "def go(loader):\n"
        "    return loader.import_module('requests')\n"
    ),
    "a call with no arguments at all": "__import__()\n",
}


@pytest.mark.parametrize("name", sorted(FIRES))
def test_the_detector_fires(name):
    assert undeclared_imports(FIRES[name], CONTROL_ALLOWED), (
        f"the detector did not report {name!r}; a detector that matches "
        "nothing reports the whole tree clean"
    )


@pytest.mark.parametrize("name", sorted(PASSES))
def test_the_detector_leaves_declared_code_alone(name):
    assert not undeclared_imports(PASSES[name], CONTROL_ALLOWED), (
        f"{name!r} is a correct, declared import and was reported"
    )


def test_the_finding_names_the_line_and_the_module():
    """A finding a reader cannot act on costs as much as no finding."""
    findings = undeclared_imports("import os\nimport requests\n",
                                  CONTROL_ALLOWED)
    assert findings == [(2, "requests")], findings


# --------------------------------------------------------------------------
# Requirement-parsing controls
# --------------------------------------------------------------------------

#: PEP 508 spellings mapped to the distribution they name.
REQUIREMENTS = {
    "pytest>=7.0": "pytest",
    "PyYAML>=6.0": "pyyaml",
    "setuptools>=64": "setuptools",
    "requests": "requests",
    "  spaced == 1.0  ": "spaced",
    "extras[all]>=1": "extras",
    "marked; python_version < '3.11'": "marked",
    "Name_With.Dots~=2.0": "name-with-dots",
    "excluded != 1.0": "excluded",
    "urls @ https://example.invalid/x.whl": "urls",
}


@pytest.mark.parametrize("requirement", sorted(REQUIREMENTS))
def test_the_distribution_name_is_extracted(requirement):
    assert distribution_name(requirement) == REQUIREMENTS[requirement]


def test_a_requirement_with_no_name_is_not_a_distribution():
    assert distribution_name("") is None
    assert distribution_name("   ") is None


def test_the_import_name_defaults_to_the_distribution_name():
    assert import_names_for("requests") == {"requests"}
    assert import_names_for("typing-extensions") == {"typing_extensions"}


def test_a_distribution_whose_import_name_differs_is_tabulated():
    assert import_names_for("pyyaml") == {"yaml"}


def test_the_import_names_we_claim_are_the_ones_installed():
    """:data:`IMPORT_NAMES` is a claim about the world; check it against it.

    A hand-maintained table drifts, and the way it drifts is silent: a wrong
    entry widens the allow-list and the scan goes on passing. Where the
    distribution is actually installed, the installed metadata is ground
    truth, so use it.
    """
    from importlib.metadata import packages_distributions

    provided = {}
    for module, distributions in packages_distributions().items():
        for distribution in distributions:
            provided.setdefault(normalize(distribution), set()).add(module)

    checked = 0
    for distribution in sorted(IMPORT_NAMES):
        if distribution not in provided:
            continue
        checked += 1
        claim = checkable_claim(distribution, import_names_for(distribution))
        assert claim <= provided[distribution], (
            f"IMPORT_NAMES says {distribution} provides "
            f"{sorted(claim)}, but the installed "
            f"distribution provides {sorted(provided[distribution])}"
        )

    assert "pytest" in provided, (
        "pytest is running yet reports no distribution; the ground truth "
        "this test relies on is not available and it proved nothing"
    )
    assert checked == len(IMPORT_NAMES), (
        f"only {checked} of {len(IMPORT_NAMES)} tabulated distributions were "
        "installed; the rest were not checked against anything"
    )


# --------------------------------------------------------------------------
# TOML parsing controls
# --------------------------------------------------------------------------

#: ``(source, expected arrays)``. The comment cases are not hypothetical:
#: ``tests/test_setup.py`` parses the ``py-modules`` array by naive line
#: splitting, so a comment inside that array breaks it, and the repository
#: carries "never put a comment inside it" as a standing instruction. A
#: parser that reads dependency lists should not add a second one.
TOML_CASES = {
    "single-line array": (
        '[project]\ndependencies = ["a", "b"]\n',
        {("project", "dependencies"): ["a", "b"]},
    ),
    "multi-line array": (
        '[project]\ndependencies = [\n    "a",\n    "b",\n]\n',
        {("project", "dependencies"): ["a", "b"]},
    ),
    "comment above the array": (
        '[project]\n# why\ndependencies = ["a"]\n',
        {("project", "dependencies"): ["a"]},
    ),
    "comment after the array": (
        '[project]\ndependencies = ["a"]  # why\n',
        {("project", "dependencies"): ["a"]},
    ),
    "comment inside a multi-line array": (
        '[project]\ndependencies = [\n    "a",\n    # why\n    "b",\n]\n',
        {("project", "dependencies"): ["a", "b"]},
    ),
    # The three cases below are the ones with teeth. A comment whose text
    # happens to contain no quote and no bracket is indistinguishable from a
    # stripped one, so a control built from `# why` passes with comment
    # handling removed entirely -- which is how this file's first draft
    # shipped a parser control that proved nothing. Comments in a dependency
    # list quote package names and mention tables by name, so these are the
    # realistic shapes, not the exotic ones.
    "a commented-out entry is not a dependency": (
        '[project]\ndependencies = [\n    "a",\n    # "ghost",\n]\n',
        {("project", "dependencies"): ["a"]},
    ),
    "a bracket in a comment does not unbalance the array": (
        '[project]\ndependencies = [\n    "a",\n    # see [project\n]\n',
        {("project", "dependencies"): ["a"]},
    ),
    "an apostrophe in a comment does not open a string": (
        '[project]\ndependencies = [\n    "a",  # don\'t\n    "b",\n]\n',
        {("project", "dependencies"): ["a", "b"]},
    ),
    "a hash inside a string is not a comment": (
        '[project]\ndependencies = ["a#b"]\n',
        {("project", "dependencies"): ["a#b"]},
    ),
    "an equals sign inside a string": (
        '[project]\ndependencies = ["a==1.0"]\n',
        {("project", "dependencies"): ["a==1.0"]},
    ),
    "a bracket inside a string": (
        '[project]\ndependencies = ["a[extra]==1"]\n',
        {("project", "dependencies"): ["a[extra]==1"]},
    ),
    # Balanced brackets inside a string survive a parser that counts them
    # blindly, and so does a stray closing one -- the depth only has to reach
    # zero. A stray *opening* bracket is the case that tells them apart:
    # counted blindly the array never closes and the key vanishes entirely.
    "an unbalanced bracket inside a string": (
        '[project]\ndependencies = [\n    "a",\n    "weird[name",\n]\n',
        {("project", "dependencies"): ["a", "weird[name"]},
    ),
    "single-quoted literal strings": (
        "[project]\ndependencies = ['a', 'b']\n",
        {("project", "dependencies"): ["a", "b"]},
    ),
    "a double quote inside a literal string": (
        "[project]\ndependencies = ['a; x < \"3.11\"']\n",
        {("project", "dependencies"): ['a; x < "3.11"']},
    ),
    "dotted table": (
        '[project.optional-dependencies]\ndev = ["a"]\n',
        {("project.optional-dependencies", "dev"): ["a"]},
    ),
    # A quoted segment in a header names exactly the same table as an
    # unquoted one, and a parser that keeps the header literally matches
    # neither -- so every extra declared this way reads as undeclared. It is
    # a false positive rather than a false negative, which makes it loud, but
    # it is loud on a day nobody changed a dependency: somebody reformatted a
    # file this scan only reads.
    "quoted segment in a dotted table header": (
        '[project."optional-dependencies"]\ndev = ["a"]\n',
        {("project.optional-dependencies", "dev"): ["a"]},
    ),
    "quoted segment with a dot inside it": (
        '[tool."a.b"]\nx = ["a"]\n',
        {("tool.a.b", "x"): ["a"]},
    ),
    # `optional-dependencies.dev = [...]` under `[project]` is the same
    # declaration as `dev = [...]` under `[project.optional-dependencies]`.
    # TOML lets a key be dotted; only the joined path is meaningful.
    "dotted key in an assignment": (
        '[project]\noptional-dependencies.dev = ["a"]\n',
        {("project.optional-dependencies", "dev"): ["a"]},
    ),
    "quoted key": (
        '[project]\n"dependencies" = ["a"]\n',
        {("project", "dependencies"): ["a"]},
    ),
    # The failure this parser was rewritten for. A line-based scan cannot
    # see a multi-line string, so a line of *prose* that happens to read like
    # an assignment becomes one -- and this direction is silent: it adds
    # names to the allow-list and everything goes on passing.
    "a multi-line string containing a fake assignment": (
        '[project]\ndependencies = ["real"]\n'
        'description = """\ndependencies = ["ghost"]\n"""\n',
        {("project", "dependencies"): ["real"]},
    ),
    "a multi-line literal string containing a fake assignment": (
        "[project]\ndependencies = ['real']\n"
        "readme = '''\ndependencies = ['ghost']\n'''\n",
        {("project", "dependencies"): ["real"]},
    ),
    "a multi-line string containing a fake table header": (
        '[project]\ndescription = """\n[build-system]\n"""\n'
        'dependencies = ["real"]\n',
        {("project", "dependencies"): ["real"]},
    ),
    "a multi-line string containing an unbalanced bracket": (
        '[project]\ndescription = """\nsee [project\n"""\n'
        'dependencies = ["real"]\n',
        {("project", "dependencies"): ["real"]},
    ),
    # A backslash escape dropped rather than translated silently rewrites the
    # value: `"a\\nb"` became `anb`, and in a requirement string that is a
    # different distribution name.
    "an escaped quote inside a string": (
        '[project]\ndependencies = ["a\\"b"]\n',
        {("project", "dependencies"): ['a"b']},
    ),
    "an escaped newline is a newline, not the letter n": (
        '[project]\ndependencies = ["a\\nb"]\n',
        {("project", "dependencies"): ["a\nb"]},
    ),
    "an escaped backslash does not close the string early": (
        '[project]\ndependencies = ["a\\\\", "b"]\n',
        {("project", "dependencies"): ["a\\", "b"]},
    ),
    "a backslash in a literal string is a backslash": (
        "[project]\ndependencies = ['a\\nb']\n",
        {("project", "dependencies"): ["a\\nb"]},
    ),
    "empty array": (
        '[project]\ndependencies = []\n',
        {("project", "dependencies"): []},
    ),
    "scalar assignments are ignored": (
        '[project]\nname = "copilot-tools"\ndependencies = ["a"]\n',
        {("project", "dependencies"): ["a"]},
    ),
    "two tables": (
        '[project]\ndependencies = ["a"]\n[build-system]\nrequires = ["b"]\n',
        {("project", "dependencies"): ["a"],
         ("build-system", "requires"): ["b"]},
    ),
}


@pytest.mark.parametrize("name", sorted(TOML_CASES))
def test_the_narrow_parser_reads_dependency_arrays(name):
    source, expected = TOML_CASES[name]
    assert parse_string_arrays(source) == expected


def _string_arrays_from_tomllib(data, table=""):
    """``{(table, key): [strings]}`` from a parsed document, for comparison."""
    arrays = {}
    for key, value in data.items():
        if isinstance(value, dict):
            path = f"{table}.{key}" if table else key
            arrays.update(_string_arrays_from_tomllib(value, path))
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            arrays[(table, key)] = value
    return arrays


#: Documents the reference implementation is asked about. Every case in
#: :data:`TOML_CASES` is valid TOML, so the corpus is the corpus -- checking
#: the parser against ``tomllib`` on this repository's own pyproject.toml
#: alone exercised none of the shapes above and so proved nothing about any
#: of them. That is the same mistake as a comment control written with a
#: comment containing no quotes.
@pytest.mark.parametrize("name", sorted(TOML_CASES))
def test_the_narrow_parser_agrees_with_tomllib(name):
    """The fallback is checked against the reference wherever one exists.

    ``tomllib`` is 3.11+, so this runs on the 3.12 legs of CI and not the
    3.10 ones. That is acceptable *because the scan itself never uses
    tomllib*: the narrow parser decides on every leg, and this test is what
    keeps it honest on the legs that can tell.
    """
    if tomllib is None:
        pytest.skip("tomllib is 3.11+; the narrow parser is checked on 3.12")
    source, _ = TOML_CASES[name]
    expected = _string_arrays_from_tomllib(tomllib.loads(source))
    assert parse_string_arrays(source) == expected


def test_the_narrow_parser_agrees_with_tomllib_on_our_own_pyproject():
    if tomllib is None:
        pytest.skip("tomllib is 3.11+; the narrow parser is checked on 3.12")
    text = PYPROJECT.read_text(encoding="utf-8")
    assert sorted(declared_requirements(text)) == sorted(
        _requirements_from_tomllib(text))


def test_the_corpus_the_reference_checks_is_not_trivially_satisfiable():
    """Every corpus document must really be TOML, or the comparison is empty.

    ``tomllib.loads`` raising would be an error, not a pass -- but a document
    that parses to nothing on both sides compares equal and proves nothing.
    """
    if tomllib is None:
        pytest.skip("tomllib is 3.11+")
    empty = [name for name in TOML_CASES
             if not _string_arrays_from_tomllib(tomllib.loads(
                 TOML_CASES[name][0]))]
    assert not empty, f"these corpus documents declare no arrays: {empty}"


def test_an_array_of_tables_header_is_read_as_its_table_path():
    """``[[x]]`` is kept out of the corpus because it is a different shape.

    An array of tables holds tables, not strings, so ``tomllib`` reports no
    string array for it and the comparison above would fail on a difference
    that is about structure rather than about parsing. What matters here is
    only that the header does not leak its brackets into a table name, and
    that nothing inside one can be mistaken for a dependency declaration.
    """
    arrays = parse_string_arrays('[[tool.things]]\nnames = ["a"]\n')
    assert arrays == {("tool.things", "names"): ["a"]}
    assert declared_requirements(
        '[[project.optional-dependencies]]\ndev = ["ghost"]\n'
    ) == ["ghost"], "an extras array-of-tables is still an extras declaration"


def test_a_multi_line_string_cannot_inject_a_dependency():
    """The whole reason the parser is not line-based, stated as a fact.

    This is the only failure direction that is silent: a fake assignment in
    prose *widens* the allow-list, so the scan keeps passing while quietly
    excusing an import nobody declared.
    """
    injected = (
        '[project]\n'
        'dependencies = ["real"]\n'
        'description = """\n'
        'dependencies = ["ghost>=1"]\n'
        '"""\n'
    )
    assert declared_requirements(injected) == ["real"]


def test_a_quoted_table_header_still_declares_its_extras():
    assert declared_requirements(
        '[project."optional-dependencies"]\ndev = ["a"]\n') == ["a"]


def test_a_dotted_key_still_declares_its_extras():
    assert declared_requirements(
        '[project]\noptional-dependencies.dev = ["a"]\n') == ["a"]


def test_scalar_strings_are_read_and_arrays_are_not():
    text = ('[project]\nname = "copilot-tools"\ndependencies = ["a"]\n'
            '[project."optional-dependencies"]\n')
    scalars = parse_scalar_strings(text)
    assert scalars == {("project", "name"): "copilot-tools"}


def test_a_claim_metadata_cannot_confirm_is_excused_only_when_written_down():
    """:data:`IMPORT_NAMES_UNRECORDED` is empty; the mechanism is not.

    An empty exception table that no test exercises is machinery nobody
    knows is broken. Control it with synthetic input instead of waiting for
    a real entry to arrive and find out then.
    """
    assert checkable_claim("d", {"a", "b"}, frozenset()) == {"a", "b"}
    assert checkable_claim("d", {"a", "b"}, frozenset({("d", "b")})) == {"a"}
    assert checkable_claim("d", {"a", "b"},
                           frozenset({("other", "b")})) == {"a", "b"}
    assert not IMPORT_NAMES_UNRECORDED, (
        "an entry was added here without a reason recorded next to it"
    )


def test_the_narrow_parser_finds_what_this_repository_declares():
    requirements = declared_requirements(
        PYPROJECT.read_text(encoding="utf-8"))
    names = {distribution_name(r) for r in requirements}
    assert {"pytest", "pyyaml", "setuptools"} <= names, sorted(names)


def test_only_the_dependency_tables_are_read_as_requirements():
    """``[tool.setuptools] py-modules`` is an array of strings too.

    A reader that returned every array it found would treat this
    repository's module list as a list of distributions -- widening the
    allow-list by exactly the names the scan is meant to be strict about.
    """
    text = ('[project]\n'
            'name = "copilot-tools"\n'
            'dependencies = ["runtime"]\n'
            '[project.optional-dependencies]\n'
            'dev = ["testing"]\n'
            'docs = ["sphinxish"]\n'
            '[build-system]\n'
            'requires = ["builder"]\n'
            '[tool.setuptools]\n'
            'py-modules = ["not_a_dependency"]\n')
    assert sorted(declared_requirements(text)) == [
        "builder", "runtime", "sphinxish", "testing"]

