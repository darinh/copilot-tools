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
declare it.

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
              ".specify", "build", "dist", ".venv", "venv")

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

#: PEP 508 names, loosely: enough to find where the name stops.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


# --------------------------------------------------------------------------
# Reading pyproject.toml
#
# `tomllib` arrived in 3.11 and this suite must run on 3.10, so the parser
# below is the one that decides. It is narrow on purpose: it understands
# table headers and arrays of strings, which is the whole of what PEP 621
# uses for dependency lists, and it ignores everything else rather than
# guessing at it. Failing to find a key removes entries from the *allowed*
# set, so the failure direction is a loud false positive rather than a silent
# pass -- and `test_the_narrow_parser_agrees_with_tomllib` pins it against
# the real implementation wherever one exists.
# --------------------------------------------------------------------------

def _outside_strings(text):
    """``(index, character)`` for each character not inside a TOML string."""
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        yield index, char


def _strip_comment(line):
    """``line`` up to its first ``#`` that is not inside a string."""
    for index, char in _outside_strings(line):
        if char == "#":
            return line[:index]
    return line


def _bracket_depth(line):
    """Net ``[`` minus ``]`` outside strings."""
    depth = 0
    for _, char in _outside_strings(line):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    return depth


def _strings_in(text):
    """Every string literal in ``text``, in order."""
    values = []
    current = []
    quote = ""
    escaped = False
    for char in text:
        if quote:
            if escaped:
                current.append(char)
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                values.append("".join(current))
                current = []
                quote = ""
            else:
                current.append(char)
            continue
        if char in "\"'":
            quote = char
    return values


def parse_string_arrays(text):
    """``{(table, key): [strings]}`` for every array-of-strings assignment."""
    arrays = {}
    table = ""
    key = None
    buffer = ""
    depth = 0
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if key is None:
            stripped = line.strip()
            if not stripped:
                continue
            if (stripped.startswith("[") and stripped.endswith("]")
                    and "=" not in stripped):
                table = stripped[1:-1].strip().strip("\"'")
                continue
            if "=" not in stripped:
                continue
            name, _, rest = stripped.partition("=")
            rest = rest.strip()
            if not rest.startswith("["):
                continue
            key = name.strip().strip("\"'")
            buffer = rest
            depth = _bracket_depth(rest)
        else:
            buffer += "\n" + line
            depth += _bracket_depth(line)
        if depth <= 0:
            arrays[(table, key)] = _strings_in(buffer)
            key = None
            buffer = ""
            depth = 0
    return arrays


def declared_requirements(text):
    """Every requirement string ``pyproject.toml`` asks for, from anywhere."""
    found = []
    for (table, key), values in parse_string_arrays(text).items():
        if table == "project" and key == "dependencies":
            found.extend(values)
        elif table == "project.optional-dependencies":
            found.extend(values)
        elif table == "build-system" and key == "requires":
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

def _python_sources():
    """Every ``*.py`` in the repository, discovered rather than listed."""
    out = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        # Filter on the repo-relative path. An absolute-path filter matches
        # ".worktrees" for every file when the checkout *is* a worktree --
        # which is where every agent on this project works -- and an empty
        # population passes every assertion below.
        if any(part in NOT_SOURCE for part in rel.parts):
            continue
        out.append(path)
    return sorted(out)


def first_party_modules():
    """Module names this repository itself provides.

    ``tests/`` is included because ``pyproject.toml`` puts it on the path
    (``pythonpath = ["tests"]``) precisely so the conformance scans can
    import one another's AST helpers.
    """
    return {path.stem for path in _python_sources()}


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
                             "pytest", "yaml", "setup_tools"})

#: An undeclared import in every spelling Python offers. Each must be found.
FIRES = {
    "plain import": "import requests\n",
    "dotted import": "import requests.adapters\n",
    "aliased import": "import requests as http\n",
    "from-import": "from requests import get\n",
    "from-import of a submodule": "from requests.adapters import HTTPAdapter\n",
    "second name in one import statement": "import os, requests\n",
    "inside a function": (
        "def fetch():\n"
        "    import requests\n"
        "    return requests\n"
    ),
    # The soft spellings are reported too. Both still bet on the environment
    # being able to supply the name; see the module docstring.
    "guarded by ImportError": (
        "try:\n"
        "    import requests\n"
        "except ImportError:\n"
        "    requests = None\n"
    ),
    "under TYPE_CHECKING": (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import requests\n"
    ),
    "inside a class body": (
        "class Client:\n"
        "    import requests\n"
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
        assert import_names_for(distribution) <= provided[distribution], (
            f"IMPORT_NAMES says {distribution} provides "
            f"{sorted(import_names_for(distribution))}, but the installed "
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
    "quoted key": (
        '[project]\n"dependencies" = ["a"]\n',
        {("project", "dependencies"): ["a"]},
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


def test_the_narrow_parser_agrees_with_tomllib():
    """The fallback is checked against the reference wherever one exists.

    ``tomllib`` is 3.11+, so this runs on the 3.12 legs of CI and not the
    3.10 ones. That is acceptable *because the scan itself never uses
    tomllib*: the narrow parser decides on every leg, and this test is what
    keeps it honest on the legs that can tell.
    """
    if tomllib is None:
        pytest.skip("tomllib is 3.11+; the narrow parser is checked on 3.12")
    text = PYPROJECT.read_text(encoding="utf-8")
    assert sorted(declared_requirements(text)) == sorted(
        _requirements_from_tomllib(text))


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

