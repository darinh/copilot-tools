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

# A claim token is not a claim when it is being denied. "Here 'legacy' means
# superseded, not abandoned" is the true statement, and a guard that rejects
# the true statement forces the false one back -- so the negation is handled
# rather than worked around by banning the word from our own prose. The window
# stops at sentence punctuation, because a negation in the previous sentence
# denies nothing here: "This is not the Python path. The scripts are
# unmaintained" is still a lie.
#
# The window is 20 characters and not 40, which is where it started. A negator
# reaches across a comma into the *next clause* long before it reaches the next
# sentence, and adversarial review put its finger on the class even though the
# example it offered did not reproduce. Measured: at 40, "It is never simple,
# the scripts are frozen" is read as a denial and the lie passes. Twenty is
# chosen from both sides rather than by taste -- "operator.sh is not, in fact,
# abandoned" needs 11 characters of reach and must keep it, that cross-clause
# lie needs 25 and must lose it. Both are pinned as tests below, so the number
# cannot drift back without something going red. Banning the comma outright was
# the other candidate and is worse: it breaks the true sentence, which is the
# failure mode that reintroduces the false one.
NEGATED = re.compile(
    r"\b(?:not|never|nor|isn't|aren't|rather than|far from)\b[^.;:!?]{0,20}$",
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


# A block that continues the one above it rather than starting a new thought:
# a list item, a blockquote, a table row, an indented continuation. Markdown
# requires a blank line before a list, so "For `operator.sh`:" and the bullets
# under it are *always* different blocks.
CONTINUATION = re.compile(r"^(?:[-*+]\s|\d+[.)]\s|>|\||\s+\S)")
HEADING = re.compile(r"^#{1,6}\s")


def _attributed_blocks(text: str) -> list[str]:
    """Blocks in which a claim would be *about* the bash scripts.

    Paragraph scope fixed the bug that line scope missed, and then had one of
    its own in the same shape one level up. "For ``operator.sh``:" followed by
    a bulleted "it is unmaintained" puts the subject and the claim in adjacent
    *blocks*, and a block-at-a-time scan finds neither block objectionable --
    measured against the real implementation, that evasion returned zero
    offences. So the subject carries forward, but only into blocks that are
    syntactically continuations of it, and only until the next heading.

    Carrying it into everything was the obvious alternative and is wrong: these
    documents mention the bash scripts constantly, so a document-wide subject
    makes every later claim about anything read as a claim about them, and a
    guard that fires on true prose is one somebody deletes.
    """
    attributed: list[str] = []
    carried = False
    for block in _blocks(text):
        first = block.lstrip("\n")
        if HEADING.match(first):
            carried = False
        has_subject = bool(SUBJECT.search(block))
        if has_subject or (carried and CONTINUATION.match(first)):
            attributed.append(block)
            carried = True
        else:
            carried = False
    return attributed


def _stale_claims(block: str) -> list[str]:
    """Claim tokens in `block` that are asserted rather than denied."""
    return [
        m.group(0) for m in STALE_CLAIM.finditer(block)
        if not NEGATED.search(" ".join(block[:m.start()].split()))
    ]


def _details_sections(text: str) -> list[tuple[str, str]]:
    """(summary, body) for every ``<details>`` block."""
    return [
        (m.group("summary"), m.group("body"))
        for m in re.finditer(
            r"<details>\s*<summary>(?P<summary>.*?)</summary>"
            r"(?P<body>.*?)</details>",
            text, re.DOTALL)
    ]


def _fenced_lines(text: str) -> list[str]:
    """Every line inside a fenced code block -- i.e. things a reader will run."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            lines.append(line)
    return lines


SETUP_MENTION = re.compile(r"(?:^|[\s(`])\.?/?setup\.sh\b")

# A mention of `setup.sh` that is telling the reader it moves them *off* the
# bash scripts is the opposite of prescribing it, and the correct prose in
# `docs/operator.md` does exactly that: "`./setup.sh` will not give you one --
# it migrates `operator`/`handoff` *off* ...". Scanning prose without this
# turns that warning into an offence, which is the same self-defeating shape
# as a claim guard that rejects "superseded, not abandoned".
MIGRATES_AWAY = re.compile(
    r"\bmigrat\w+|\bwill not\b|\bwon't\b|\bdoes not\b|\bdo not\b|\bdon't\b"
    r"|\bnever\b|\binstead of\b|\brather than\b|\bback to python\b",
    re.IGNORECASE,
)

# A period inside `setup.sh` or `./setup.sh` is not the end of a sentence.
# Requiring whitespace after the mark is what tells them apart.
SENTENCE_END = re.compile(r"[.!?;](?=\s)")


def _setup_prescriptions(body: str) -> list[str]:
    """Sentences in `body` that offer ``setup.sh`` without saying it migrates.

    Fenced lines are not special-cased because they no longer need to be: a
    fenced command is a sentence with no prose in it, so it carries no
    migrates-away marker and is reported. Scanning only fences -- which is
    what this did first -- misses "you can restore the bash install by running
    ./setup.sh", which is the same instruction with the backticks taken off.
    """
    flat = " ".join(body.split())
    bounds = [m.start() for m in SENTENCE_END.finditer(flat)]
    found: list[str] = []
    for hit in SETUP_MENTION.finditer(flat):
        start = max((b + 1 for b in bounds if b < hit.start()), default=0)
        end = min((b for b in bounds if b >= hit.end()), default=len(flat))
        sentence = flat[start:end].strip()
        if not MIGRATES_AWAY.search(sentence):
            found.append(sentence)
    return found


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
        for block in _attributed_blocks(
                doc.read_text(encoding="utf-8", errors="replace")):
            for claim in _stale_claims(block):
                offences.append(
                    f"{doc.relative_to(REPO).as_posix()}: {claim!r} in "
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

def test_setup_sh_is_not_offered_as_a_way_to_get_a_bash_install():
    """The premise first, then the rule -- an idea taken from a peer's version.

    ``docs/operator.md`` used to head its rollback recipe with ``./setup.sh``,
    which is the one command in the repository guaranteed *not* to produce a
    bash install: it stashes the ``operator``/``handoff`` symlinks and points
    them at the Python console scripts. A reader who followed it got the
    supported install and no error, which is exactly why it survived.

    The premise is asserted rather than assumed, so that if ``setup.sh`` ever
    stops migrating, this reports an expired reason instead of going on
    quietly enforcing a rule that no longer has one.
    """
    setup = (REPO / "setup.sh").read_text(encoding="utf-8", errors="replace")
    assert "stash_legacy_link operator operator.sh" in setup, (
        "premise expired: setup.sh no longer migrates the operator symlink "
        "off operator.sh, so it may be fine to recommend it again -- check, "
        "then delete this rule rather than editing the docs around it")

    offences = []
    for doc in _docs():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for summary, body in _details_sections(text):
            if not SUBJECT.search(summary) and not re.search(
                    r"roll(ing)? ?back", summary, re.IGNORECASE):
                continue
            for line in _setup_prescriptions(body):
                offences.append(
                    f"{doc.relative_to(REPO).as_posix()}: {summary!r} "
                    f"tells the reader to run {line.strip()!r}")
    assert not offences, (
        "a rollback section prescribes setup.sh, which migrates away from the "
        "bash scripts:\n  " + "\n  ".join(offences))



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
    "operator.sh                    # Legacy bash wrapper (Linux/WSL/macOS)",
])
def test_the_detector_leaves_accurate_descriptions_alone(acceptable):
    """A guard that also rejects the true statement forces the false one back."""
    blocks = _blocks(acceptable)
    assert not any(STALE_CLAIM.search(b) for b in blocks), (
        f"the detector objects to an accurate description: {acceptable!r}")


# ── Controls for the three holes adversarial review opened ──────────
# Each of these was a live evasion in this file, measured against the real
# implementation before it was fixed. They are kept as tests rather than
# described in a comment because a fix nobody can re-run is a claim.


def test_a_claim_in_a_list_beneath_its_subject_is_still_attributed():
    """Paragraph scope had the same bug as line scope, one level up."""
    doc = "For `operator.sh`:\n\n- It is unmaintained and you should not read it.\n"
    offences = [c for b in _attributed_blocks(doc) for c in _stale_claims(b)]
    assert offences == ["unmaintained"], (
        "a subject introducing a list does not reach the claim in it")


def test_the_subject_does_not_carry_into_unrelated_prose():
    """The negative control for carrying it -- over-reach is the other failure.

    A subject that carried to end-of-document would make this pass while
    reporting the whole README, and a guard that fires on true prose gets
    deleted rather than obeyed.
    """
    doc = ("For `operator.sh`:\n\n- it is fine\n\n"
           "The templates directory is frozen for the release.\n")
    offences = [c for b in _attributed_blocks(doc) for c in _stale_claims(b)]
    assert offences == [], (
        "the subject leaked into a paragraph that is not about the scripts")


def test_a_heading_ends_the_subjects_reach():
    doc = ("For `operator.sh`:\n\n## Templates\n\n"
           "- this directory is unmaintained\n")
    assert [c for b in _attributed_blocks(doc) for c in _stale_claims(b)] == []


@pytest.mark.parametrize("denied", [
    "Here 'legacy' means superseded, not abandoned",
    "operator.sh is not, in fact, abandoned",
    "The bash scripts are far from unmaintained",
])
def test_an_adjacent_denial_still_suppresses_the_claim(denied):
    assert _stale_claims(denied) == [], (
        f"the guard rejects a true statement: {denied!r}")


@pytest.mark.parametrize("lie", [
    # Cross-clause: the negator denies the *previous* clause, not this one.
    # At the original 40-character window this one passed.
    "It is never simple, the scripts are frozen",
    "This is not the Python path. The scripts are unmaintained",
    "This is not the final form, and operator.sh is abandoned",
])
def test_a_denial_of_something_else_does_not_suppress_the_claim(lie):
    assert _stale_claims(lie), (
        f"a negation elsewhere in the sentence hid a real claim: {lie!r}")


def test_setup_sh_is_caught_in_prose_and_not_only_inside_fences():
    """Scanning fenced lines only misses the instruction with its backticks off."""
    body = "You can restore the bash install by running ./setup.sh again.\n"
    assert _fenced_lines(body) == [], "premise: this text is not in a fence"
    assert _setup_prescriptions(body), (
        "a prose instruction to run setup.sh is not reported")


def test_the_setup_sh_rule_leaves_the_warning_that_replaced_it_alone():
    """The negative control, taken verbatim from the prose this branch ships.

    Without denial-awareness the fix for the test above reports our own
    correct warning as an offence, and the only way to green is to stop
    warning -- which is how the original bad instruction got there.
    """
    body = ("**`./setup.sh` will not give you one** -- it migrates "
            "`operator`/`handoff` *off* the bash scripts.\n")
    assert SETUP_MENTION.search(" ".join(body.split())), "premise: it is mentioned"
    assert _setup_prescriptions(body) == [], (
        "the guard reports our own warning as if it prescribed setup.sh")


def test_the_readme_ci_claim_matches_the_workflow():
    """The README cites CI as evidence. Cite, then verify -- against the yaml.

    "on every push" was false: `ci.yml` restricts both triggers to `main`, so
    a push to a feature branch runs nothing. Two independent reviewers caught
    it in prose this very module was written to keep honest, which is the
    argument for checking the claim mechanically rather than proofreading it.
    """
    workflow = REPO / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file(), f"premise expired: no {workflow}"
    ci = workflow.read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "Shell script syntax" in ci, (
        "premise expired: the README names a 'Shell script syntax' job that "
        "ci.yml no longer defines")

    triggers = ci.split("\njobs:")[0]
    restricted = re.search(r"\n  push:\n\s+branches:\s*\[[^\]]*\]", triggers)
    if restricted:
        assert not re.search(r"on every push\b(?! to)", readme), (
            "README says the shell job runs 'on every push', but ci.yml "
            "restricts the push trigger to specific branches")
        assert "push to `main`" in readme, (
            "ci.yml restricts pushes to named branches; the README should say "
            "which, rather than implying all of them")
