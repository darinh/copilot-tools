"""The docs may not call the bash scripts dead while the suite still runs them.

``README.md`` described ``operator.sh``/``handoff.sh`` as "bash, legacy,
unmaintained" and said they were "left on disk, untouched"; ``docs/operator.md``
said ``operator.sh`` was "retained unchanged". All three were false, and one of
them was falsified on the day it was read: nine commits landed in those scripts
in a single day, including ``operator list`` and ``operator stop`` being dead on
macOS, and ``handoff.sh``'s instance inference never having worked there at all.
Four test modules read or execute the two files.

The claim was wrong in the expensive direction. A reader who believes a rollback
path is frozen has no reason to fix a bug in it, and no reason to check whether
the bug they are looking at is already fixed -- so the sentence that says
"nobody maintains this" is the sentence that stops it being maintained.

What is checked here is not the prose. It is the *agreement* between two things
this repo can measure:

* whether a script is exercised by the test suite, discovered by scanning
  ``tests/`` rather than listed here, and
* whether the shipped documentation makes a no-longer-changes claim about it.

Those may not both be true at once. Either the docs stop saying it, or the tests
stop running it -- and if someone ever really does freeze the bash scripts, the
way to make this file green is to delete the tests that run them, which is a
change nobody makes by accident.

``specs/`` is excluded deliberately and named here rather than silently: a spec
is a dated record of what was true when it was written, and
``specs/003-windows-native-operator/quickstart.md`` says "``operator.sh``
remains functional and untouched" as a statement about that migration, not about
today. Amending it would be falsifying a record. ``README.md`` and ``docs/`` are
statements in the present tense, and those are what a reader acts on.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# The scripts whose maintenance status the documentation makes claims about.
BASH_SCRIPTS = ("operator.sh", "handoff.sh")

# Present-tense documentation only. See the module docstring for why `specs/`
# is not in here; naming the exclusion is the point, because a population that
# quietly shrinks to nothing satisfies every "no document says X" assertion in
# this file.
DOC_PATHS = ("README.md", "docs")

# A claim that the file has stopped changing. Deliberately not "legacy" or
# "superseded" or "original" -- those are true, and they are what the docs
# should say. These are the ones that tell a reader not to look.
STALE_CLAIM = re.compile(
    r"\bunmaintained\b"
    r"|\bno longer maintained\b"
    r"|\bnot maintained\b"
    r"|\buntouched\b"
    r"|\bunchanged\b"
    r"|\bfrozen\b"
    r"|\babandoned\b",
    re.IGNORECASE,
)

# The subject matcher is broader than the filenames on purpose. The sentence
# that actually shipped was "the bash scripts themselves are left on disk,
# untouched" -- it never named a file, and a filename-keyed scan would have
# read it as clean.
SUBJECT = re.compile(
    r"operator\.sh"
    r"|handoff\.sh"
    r"|bash scripts?\b"
    r"|bash implementation\b"
    r"|bash entry point\b"
    r"|legacy bash\b",
    re.IGNORECASE,
)


def _docs() -> list[Path]:
    found: list[Path] = []
    for entry in DOC_PATHS:
        path = REPO / entry
        if path.is_dir():
            found.extend(sorted(path.rglob("*.md")))
        elif path.is_file():
            found.append(path)
    return found


def _blocks(text: str) -> list[str]:
    """Blank-line-separated blocks.

    The unit has to be a paragraph and not a line. The subject and the claim
    landed on *different lines* of the same sentence in the text this module
    was written against, so a line-at-a-time scan finds neither line
    objectionable and reports the paragraph clean.
    """
    return [block for block in re.split(r"\n[ \t]*\n", text) if block.strip()]


def _test_files_exercising(script: str) -> list[str]:
    """Test modules that name `script`, i.e. read or run it."""
    hits = []
    for path in sorted((REPO / "tests").rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        if script in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(path.name)
    return hits


# ── Population guards ───────────────────────────────────────────────
# Everything below asserts that something is *absent*. An empty population
# satisfies that perfectly, and reads in a pass count exactly like a clean
# tree. These two run first for that reason.


def test_the_document_population_is_not_empty_and_holds_what_we_ship():
    names = {p.relative_to(REPO).as_posix() for p in _docs()}
    assert "README.md" in names, f"README.md is not being scanned: {sorted(names)}"
    assert "docs/operator.md" in names, (
        f"docs/operator.md is not being scanned: {sorted(names)}")
    assert not any(name.startswith("specs/") for name in names), (
        "specs/ is a dated record and must stay out of the scan")


@pytest.mark.parametrize("script", BASH_SCRIPTS)
def test_the_scripts_really_are_exercised_by_the_suite(script):
    """The premise of this whole module, measured rather than assumed.

    If this ever fails, the documentation is free to say the script is
    unmaintained -- because by then it would be true.
    """
    exercising = _test_files_exercising(script)
    assert exercising, (
        f"no test module names {script}; if that is deliberate, the claim "
        f"this file guards is no longer false and the guard should go")


# ── The check ───────────────────────────────────────────────────────


def test_no_shipped_document_calls_the_maintained_scripts_dead():
    offences = []
    for doc in _docs():
        for block in _blocks(doc.read_text(encoding="utf-8", errors="replace")):
            if not SUBJECT.search(block):
                continue
            claim = STALE_CLAIM.search(block)
            if claim:
                offences.append(
                    f"{doc.relative_to(REPO).as_posix()}: {claim.group(0)!r} in "
                    f"{' '.join(block.split())[:160]!r}")
    assert not offences, (
        "documentation claims the bash scripts have stopped changing, while "
        "the test suite still runs them:\n  " + "\n  ".join(offences))


def test_the_readme_names_test_modules_that_exist_and_run_the_scripts():
    """The README's maintenance claim cites specific files. Cite, then verify.

    A promise of coverage is the easy half. This is why the README names the
    modules instead of asserting "the suite covers them": a named file can be
    checked, and an unnamed one is prose.
    """
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    cited = set(re.findall(r"tests/(test_[a-z0-9_]+\.py)", readme))
    relevant = {
        name for name in cited
        if any(script in (REPO / "tests" / name).read_text(
            encoding="utf-8", errors="replace")
            for script in BASH_SCRIPTS)
        if (REPO / "tests" / name).is_file()
    }
    missing = sorted(name for name in cited if not (REPO / "tests" / name).is_file())
    assert not missing, f"README cites test modules that do not exist: {missing}"
    assert len(relevant) >= 2, (
        "the README's bash-maintenance claim should cite at least two test "
        f"modules that actually name the scripts; it cites {sorted(relevant)}")


# ── Controls ────────────────────────────────────────────────────────
# A detector that matches nothing reports every document clean. These pin
# both directions: the exact prose that shipped must trip it, and the prose
# that replaced it must not.


@pytest.mark.parametrize("shipped", [
    "| `operator.sh` / `handoff.sh` (bash, legacy, unmaintained) | \u274c |",
    "`setup.sh` migrates existing installs off the bash scripts\n"
    "automatically; the bash scripts themselves\n"
    "are left on disk, untouched, purely so a failed migration cannot strand\n"
    "a user.",
    "`operator.sh`/`handoff.sh` themselves are\nleft on disk unchanged; they're "
    "just no longer the thing installed into\n`PATH`.",
    "> The original bash `operator.sh` is retained unchanged for existing Linux "
    "and\n> WSL users.",
])
def test_the_detector_fires_on_the_prose_that_actually_shipped(shipped):
    blocks = _blocks(shipped)
    assert any(SUBJECT.search(b) and STALE_CLAIM.search(b) for b in blocks), (
        f"the detector does not object to text that was really in the docs: "
        f"{shipped!r}")


@pytest.mark.parametrize("acceptable", [
    "| `operator.sh` / `handoff.sh` (bash, superseded, still maintained) |",
    "The original bash implementation is retained on disk for rollback but no "
    "longer installed fresh by `setup.sh`.",
    "operator.sh                    # Legacy bash wrapper (Linux/WSL)",
])
def test_the_detector_leaves_accurate_descriptions_alone(acceptable):
    """A guard that also rejects the true statement forces the false one back."""
    blocks = _blocks(acceptable)
    assert not any(STALE_CLAIM.search(b) for b in blocks), (
        f"the detector objects to an accurate description: {acceptable!r}")
