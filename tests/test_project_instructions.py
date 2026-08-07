"""Moving the conventions out of user scope and into each repository.

The module under test deletes a file the user may have spent a year editing,
so most of what is asserted here is about the *order* things happen in and
about what survives an interruption. Three properties carry the risk:

* every repository is written before anything is removed;
* nothing is removed without a preserved copy that was read back and verified;
* a repository's own ``AGENTS.md`` is never overwritten.

Each has a control that proves the guard fires -- a test that only ever
exercises the happy path would report this module clean at the moment it
started deleting files it had not replaced.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

import install_manifest
import operator_ownership
import project_features
import project_instructions as pi
from project_instructions import (
    AGENTS_NAME,
    DECLINED,
    FAILED,
    MANAGED_BEGIN,
    MANAGED_END,
    MERGED,
    MISSING,
    UNCHANGED,
    WRITTEN,
    InstructionsError,
)

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "copilot-instructions.md"


@pytest.fixture
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture
def defaults() -> dict:
    """Every flag on.

    Named `defaults` for history; since FR-8 the *defaults* are off, and a
    fixture that is everything-off makes every "turning this off drops its
    section" test vacuous — the section was already gone. What these tests
    need is a baseline where each section is present, so that removing one
    is observable.
    """
    return {**project_features.resolved_values(None),
            **{slug: project_features.ON
               for slug in project_features.SLUGS
               if slug != project_features.TRACKED_BACKLOG}}


@pytest.fixture
def shipped_defaults() -> dict:
    return project_features.resolved_values(None)


def _render(source: str, values: dict, *, guid: str = "GUID-1",
            path: str = "/repo/app", label: str = "app",
            platform: str = pi.POSIX) -> str:
    return pi.render(source=source, values=values, guid=guid,
                     project_path=path, label=label,
                     project_dir_path=f"/home/.operator/projects/{guid}",
                     config_path=f"/home/.operator/projects/{guid}/features.json",
                     version="9.9.9", platform=platform)


# ---------------------------------------------------------------------------
# The gate marker
# ---------------------------------------------------------------------------

def test_the_gate_pattern_matches_the_spelling_the_template_uses():
    """A control for the one regex both the renderer and the template tests use.

    ``tests/test_project_features.py`` imports this pattern rather than
    carrying its own copy, because a second definition of "which feature turns
    this section on" is exactly the duplicated discovery rule this repository
    has already paid for once. That makes an independent check of the pattern
    itself necessary: if it silently matched nothing, the renderer would ship
    every section regardless of configuration *and* the conformance tests
    would report the template perfectly clean.
    """
    assert pi.GATE.search("*Enabled by feature flag: `spec-driven`*\n")
    assert pi.gate_slug("*Enabled by feature flag: `spec-driven`*\n") == "spec-driven"


@pytest.mark.parametrize("line", [
    "Enabled by feature flag: `spec-driven`",          # no emphasis
    "  *Enabled by feature flag: `spec-driven`*",      # indented, not a marker
    "*Enabled by feature flag: spec-driven*",          # no backticks
    "*enabled by feature flag: `spec-driven`*",        # wrong case
])
def test_the_gate_pattern_rejects_near_misses(line):
    """The negative control. A pattern that matched these would gate sections
    on prose that merely mentions a feature."""
    assert pi.gate_slug(line + "\n") is None


def test_an_ungated_section_has_no_slug():
    assert pi.gate_slug("Some prose about worktrees.\n") is None


# ---------------------------------------------------------------------------
# Splitting the document
# ---------------------------------------------------------------------------

def test_headings_inside_a_fence_do_not_start_a_section():
    """The reason the splitter tracks fences at all.

    The real template's handoff section contains a fenced example of a handoff
    file, and that example has ``## Status`` and ``## Next Steps`` in it at
    column zero. A splitter that matched ``^## `` on raw text would cut the
    section at its own example, and the tail would carry no gate marker -- so
    turning the feature off would drop the prose and keep the fragments.
    """
    text = (
        "# Title\n\nintro\n\n"
        "## Real Section\n\n"
        "*Enabled by feature flag: `session-handoff`*\n\n"
        "```markdown\n"
        "## Status\n"
        "## Next Steps\n"
        "```\n\n"
        "tail of the real section\n"
    )
    preamble, sections = pi.split_sections(text)
    assert "intro" in preamble
    assert [s.title for s in sections] == ["Real Section"]
    assert "tail of the real section" in sections[0].body
    assert pi.gate_slug(sections[0].body) == "session-handoff"


def test_a_tilde_fence_is_tracked_too():
    text = "## A\n\n~~~\n## Not A Heading\n~~~\n\n## B\n\nbody\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["A", "B"]


def test_the_real_template_splits_into_sections_that_carry_every_gate(template):
    """The positive control against the document that actually ships.

    Every feature in the vocabulary must land on exactly one section here. If
    the splitter broke, sections would fragment and gates would go missing --
    and the visible symptom would be a project's ``AGENTS.md`` quietly
    containing conventions it had turned off.
    """
    _preamble, sections = pi.split_sections(template)
    gates = [pi.gate_slug(s.body) for s in sections]
    found = [g for g in gates if g is not None]
    assert sorted(found) == sorted(project_features.SLUGS)
    assert len(found) == len(set(found)), "a feature gates two sections"


def test_the_template_still_has_the_section_the_renderer_replaces(template):
    """Pins :data:`CONFIGURATION_SECTION` against the document.

    That section is the enrollment machinery -- catalog lookup, "would you
    like to set this up", the feature table -- and replacing it is the entire
    point of this module. A renamed heading would silently reinstate it, and
    every other test here would still pass, because nothing else looks at it.
    """
    _preamble, sections = pi.split_sections(template)
    assert pi.CONFIGURATION_SECTION in [s.title for s in sections]


def test_every_line_of_the_template_survives_the_split(template):
    """A splitter that dropped lines would be invisible in a 30 KB document."""
    preamble, sections = pi.split_sections(template)
    rebuilt = preamble + "".join(f"## {s.title}\n{s.body}" for s in sections)
    assert rebuilt == template


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_a_disabled_feature_drops_its_section(template, defaults):
    on = _render(template, defaults)
    off = _render(template, {**defaults, "session-handoff": "off"})
    assert "## Session Handoff Protocol" in on
    assert "## Session Handoff Protocol" not in off
    assert len(off) < len(on)


def test_a_backlog_backend_of_none_drops_the_backlog_section(template, defaults):
    """The feature that is a choice rather than a toggle.

    ``github-issues`` still counts as *having* a tracked backlog, so only
    ``none`` removes the section. A renderer that treated every non-default
    value as off would delete the section from projects that chose the other
    backend.
    """
    issues = _render(template, {**defaults, "tracked-backlog": "github-issues"})
    none = _render(template, {**defaults, "tracked-backlog": "none"})
    assert "## Tracked Backlog" in issues
    assert "## Tracked Backlog" not in none


def test_the_enrollment_instructions_are_replaced_not_copied(template, defaults):
    """The defect the whole item exists to fix.

    A user-scope file told every session -- in every directory on the machine
    -- to resolve a project root, read the catalog and offer to enroll the
    working directory. A project file knows which project it is, so those
    instructions become resolved facts.
    """
    rendered = _render(template, defaults, guid="GUID-1")
    assert f"## {pi.CONFIGURATION_SECTION}" in rendered
    assert "isn't in the catalog yet" not in rendered
    assert "Would you like to set it up" not in rendered
    assert "Do not offer to enroll this directory" in rendered
    assert "GUID-1" in rendered


def test_the_enabled_features_line_is_the_one_the_vocabulary_produces(
        template, defaults):
    values = {**defaults, "spec-driven": "off"}
    rendered = _render(template, values)
    assert project_features.enabled_features_line(values) in rendered
    assert "spec-driven" not in rendered.split("\n\n")[2]


def test_rendering_is_deterministic(template, defaults):
    """No timestamp anywhere in the block.

    A block that recorded when it was written would show up as a diff in every
    repository every time anything regenerated it, and a diff that is always
    there is a diff nobody reads -- which is how a real change to the
    conventions would go unnoticed.
    """
    assert _render(template, defaults) == _render(template, defaults)


def test_a_section_gated_behind_an_unknown_slug_is_kept(defaults):
    """A downgrade must arrive as a duplicate, never as a deletion.

    An older build meeting a section gated on a feature it has no name for
    cannot know whether the project turned it on. Dropping it would delete
    conventions on the strength of not understanding them, which is the same
    collapse ``project_features.write_config`` refuses for stored settings.
    """
    source = ("# T\n\nintro\n\n"
              "## From The Future\n\n"
              "*Enabled by feature flag: `time-travel`*\n\nbody\n")
    assert "## From The Future" in _render(source, defaults)


def test_the_block_is_delimited_by_both_markers(template, defaults):
    rendered = _render(template, defaults)
    assert rendered.startswith(MANAGED_BEGIN)
    assert rendered.rstrip().endswith(MANAGED_END)


# ---------------------------------------------------------------------------
# FR-8 -- the word budget
# ---------------------------------------------------------------------------

def test_the_shipped_template_fits_the_budget_with_every_feature_on(
        template, defaults):
    """The binding case, and the only one worth measuring.

    Features off make the block shorter, so a project with a few flags set is
    never the rendering that overflows. Turning everything on is what the
    budget is for -- and it is what the audit measured the predecessor at:
    4,332 words, against 700 now.
    """
    values = {**defaults}
    for feature in project_features.FEATURES:
        values[feature.slug] = (feature.options[0].value
                                if len(feature.options) > 2 else "on")
    spent = pi.block_words(_render(template, values))
    assert spent <= pi.WORD_BUDGET, (
        f"the shipped template renders {spent} words, over the budget")


def test_generation_refuses_a_block_over_the_budget(template, defaults):
    """It errors. It does not warn, and there is no override.

    A warning is a line of output nobody is obliged to act on, and the block
    that produced it still ships -- which is how the predecessor reached 4,332
    words with every one of them added for a reason. The refusal is the only
    part of this that is a mechanism rather than an intention.
    """
    fat = template + "\n\n## Padding\n\n" + ("word " * pi.WORD_BUDGET)
    with pytest.raises(pi.InstructionsError) as caught:
        _render(fat, defaults)
    message = str(caught.value)
    assert str(pi.WORD_BUDGET) in message, (
        "the error does not name the budget, so a reader cannot tell how much "
        "has to come out")
    assert "over" in message and "budget" in message


def test_the_budget_error_says_what_to_do_about_it(template, defaults):
    """An error that only reports a number gets satisfied by deleting a guardrail.

    The whole design is that adding costs removing, so the message has to name
    the three places a line can go instead -- skill, rationale, tool -- and
    D10, which is the rule that stops the removal happening on its own.
    """
    fat = template + "\n\n## Padding\n\n" + ("word " * pi.WORD_BUDGET)
    with pytest.raises(pi.InstructionsError) as caught:
        _render(fat, defaults)
    message = str(caught.value).lower()
    for expected in ("skill", "rationale", "tool", "d10"):
        assert expected in message, (
            f"the budget error never mentions {expected!r}; it reports a "
            "number and leaves the cheapest fix as deleting a rule")


@pytest.mark.parametrize("over", [0, 1])
def test_the_budget_binds_at_the_boundary_and_not_before(over):
    """Exactly at the budget passes; one word past it does not.

    Parametrised over both sides because an off-by-one here is invisible in
    ordinary use -- the shipped block is 26 words clear -- and would only ever
    be discovered by the edit that happened to land on the boundary.

    The padding is measured rather than calculated: `render` adds a generated
    header and an enrollment section of its own, and a test that assumed their
    size would drift into checking a boundary several words off the real one
    while still reporting both sides correctly.
    """
    values = project_features.resolved_values(None)
    # Under a heading: `render` discards the preamble and replaces it with a
    # generated header, so padding written above the first `##` never reaches
    # the block -- which is how the first draft of this test measured a floor
    # of 38 words for every input it tried.
    base = "# T\n\n## A\n\nintro\n"
    floor = pi.block_words(_render(base, values))
    body = "intro " + "word " * (pi.WORD_BUDGET - floor + over)
    source = f"# T\n\n## A\n\n{body}\n"
    if over:
        with pytest.raises(pi.InstructionsError):
            _render(source, values)
    else:
        assert pi.block_words(_render(source, values)) == pi.WORD_BUDGET


def test_the_budget_is_the_number_the_human_agreed(template, defaults):
    """700 is a decision, not an implementation detail.

    Mutation found this hole: every other budget test is written *relative*
    to `WORD_BUDGET`, so raising the constant moves them all with it and the
    suite stays green. That is precisely how a budget becomes a warning --
    one justified exception at a time -- and it is the failure the whole of
    FR-8 exists to prevent. The number is pinned here so that changing it is
    a diff somebody has to argue for.

    Also pinned: the delivered block is genuinely near the limit. A budget
    with 4,000 words of headroom is not a budget, and this is what tells the
    difference between "trimmed the block" and "raised the number".
    """
    assert pi.WORD_BUDGET == 700
    spent = pi.block_words(_render(template, {
        **defaults,
        **{f.slug: (f.options[0].value if len(f.options) > 2 else "on")
           for f in project_features.FEATURES}}))
    assert pi.WORD_BUDGET - spent <= 100, (
        f"the block spends {spent} of {pi.WORD_BUDGET}; a budget this loose "
        "no longer binds")


def test_the_subproject_budget_is_pinned_too():
    """Same hole, same fix, one order of magnitude down."""
    assert pi.SUBPROJECT_WORD_BUDGET == 120


def test_the_count_includes_the_markers_and_the_fences():
    """Over-counting is the safe direction and it is chosen deliberately.

    A counter that excluded machinery could be satisfied by moving prose into
    a fence, which is the one move that shortens the number without shortening
    what an agent reads. Counting everything binds slightly early; the failure
    it must never have is letting a block grow past its limit unnoticed.
    """
    assert pi.block_words("a b c") == 3
    # `<!--`, `BEGIN`, `-->`, ```` ``` ````, `x`, `y`, ```` ``` ````.
    assert pi.block_words("<!-- BEGIN -->\n```\nx y\n```\n") == 7
    assert pi.block_words("  spaced   out \n\n words ") == 3
    assert pi.block_words("") == 0


# ---------------------------------------------------------------------------
# FR-9 -- the subproject block is additive only
# ---------------------------------------------------------------------------

SUBPROJECT_TEMPLATE = REPO / "templates" / "subproject-instructions.md"

#: Words that make a sentence a rule rather than a fact. A subproject file may
#: carry neither, because a rule there is a rule stated twice under Claude Code
#: and a rule stated *instead* under Codex.
_DIRECTIVE_TELLS = ("must", "never", "always", "do not", "don't", "should")


def _sentences(text: str) -> set:
    """Normalised sentences of five words or more.

    Shorter fragments are headings, list items and marker lines, which the two
    blocks legitimately share; a five-word run of prose in both files is the
    thing FR-9 forbids.
    """
    flat = " ".join(text.split())
    out = set()
    for piece in re.split(r"(?<=[.!?])\s+", flat):
        words = piece.strip(" -*#`").lower().split()
        if len(words) >= 5:
            out.add(" ".join(words))
    return out


def _subproject(**kwargs) -> str:
    args = {"name": "api", "owns": ["services/api"],
            "contracts": ["specs/contracts"], "version": "9.9.9"}
    args.update(kwargs)
    return pi.render_subproject(**args)


def test_the_subproject_block_names_what_the_root_block_cannot_know():
    """The whole justification for a second file: resolved values.

    Name, owned paths and contracts are per-directory facts, so the root file
    physically cannot carry them. Everything else an agent needs is already in
    the root file and is deliberately not repeated here.
    """
    block = _subproject()
    assert "`api` subproject" in block
    assert "`services/api`" in block
    assert "`specs/contracts`" in block


def test_the_subproject_block_does_not_name_the_ownership_gate():
    """Review caught this, and my own overlap check did not.

    The first draft said "`operator ownership check` refuses a branch that
    changed anything else" -- the root block's rule, in different words. The
    sentence-overlap test could not see it precisely *because* the words
    differed, which is the drift FR-9 exists to stop: neither file can see the
    other, so the copy that goes stale is invisible from both.
    """
    block = _subproject()
    assert "ownership check" not in block
    assert "refuses" not in block


def test_the_contract_waiver_is_spelled_the_way_the_command_accepts_it():
    """A flag named wrongly is worse than a flag not named at all.

    The reader tries it, it is rejected, and the file that told them is the
    one place they had reason to trust. Measured against the dispatcher's own
    flag table rather than a second copy of the spelling.
    """
    source = (REPO / "copilot_operator.py").read_text(encoding="utf-8")
    assert '"--allow-contracts"' in source
    assert "--allow-contracts" in _subproject()


def test_the_contract_line_says_what_a_contract_is():
    """Mutation found this: naming the paths is not the same as being right.

    A block reading "Shared with nobody: `specs/contracts`" passed every
    other check here -- the path was named, the flag was named, no directive
    appeared. `operator_ownership.check` refuses a contract change from
    *every* subproject, so a file telling one subproject the contracts are
    theirs alone inverts the rule it is reporting, and inverts it in the file
    the reader has most reason to trust.
    """
    block = _subproject(contracts=["specs/contracts"]).lower()
    assert "shared with every subproject" in block


def test_a_subproject_with_no_contracts_says_nothing_about_them():
    """Absent facts are omitted, not rendered as an empty heading.

    A "Contracts: (none)" line costs words in every subproject that has none,
    and teaches a reader to skim a section that is usually empty -- which is
    how the section that matters gets skimmed too.
    """
    assert "contract" not in _subproject(contracts=[]).lower()


def test_a_subproject_that_owns_nothing_still_renders():
    """A declaration can name a subproject and give it no paths.

    That is a mistake in the declaration, and `operator ownership check`
    reports it. Generation is not the place to refuse it: failing here stops
    every *other* subproject's file being written over one bad entry.
    """
    assert "(none declared)" in _subproject(owns=[])


def test_the_subproject_block_states_no_rules():
    """FR-9, mechanically.

    Claude Code concatenates parent and child; Codex lets the nearer file win.
    A rule in both files therefore means two things depending on the harness,
    and an identical copy is no safer -- copies drift, and the one that is
    wrong is whichever was regenerated last, which is visible from neither
    file. Facts cannot contradict rules, so the block carries only facts.
    """
    text = _subproject().lower()
    found = [tell for tell in _DIRECTIVE_TELLS if tell in text]
    assert not found, f"the subproject block gives directions: {found}"


def test_the_directive_detector_would_fire():
    """The control. An absence check that cannot fire reports every block
    clean, including the one that broke the rule."""
    text = "The api subproject must never write outside its own tree."
    assert [t for t in _DIRECTIVE_TELLS if t in text.lower()]


def test_the_subproject_block_repeats_no_sentence_of_the_root_block(
        template, defaults):
    """The other half of FR-9: not duplicated *content*, not just not rules.

    Two texts saying the same thing are two texts to keep true, and nothing
    compares them. Under Codex the child wins outright, so a stale copy here
    silently replaces a current rule there.
    """
    root = _sentences(_render(template, defaults))
    shared = root & _sentences(_subproject())
    assert not shared, f"repeated from the root block: {sorted(shared)}"


def test_the_repetition_detector_would_fire(template, defaults):
    """The control for it, built from a real sentence of the real block."""
    root = _sentences(_render(template, defaults))
    borrowed = max(root, key=len)
    assert root & _sentences(borrowed)


def test_the_subproject_block_fits_its_own_budget():
    """A subproject file is read *in addition to* the root one, in the same
    turn, so the two are cumulative against one reader's attention."""
    spent = pi.block_words(_subproject())
    assert spent <= pi.SUBPROJECT_WORD_BUDGET, spent
    assert pi.SUBPROJECT_WORD_BUDGET < pi.WORD_BUDGET


def test_a_subproject_that_owns_many_paths_still_renders():
    """Review found this, and it would have blocked real repositories.

    The declared paths are data. Charging them to a word budget meant to stop
    *writing* creeping back in means a subproject legitimately owning thirty
    directories overflows, `operator projects` raises, and the repository
    cannot be set up at all — a refusal with no action behind it, since the
    only fix is to own fewer directories.
    """
    block = _subproject(owns=[f"services/s{n}" for n in range(200)])
    assert "`services/s199`" in block
    assert pi.block_words(block) > pi.SUBPROJECT_WORD_BUDGET


def test_a_subproject_block_over_the_budget_is_refused():
    """Same failure direction as FR-8: it errors, and there is no override.

    What can overflow it now is prose added to the generator, which is the
    only thing it was ever meant to catch. Forced with a lowered budget
    rather than a long path list, because a long path list is exactly the
    input that must *not* trip it.
    """
    with mock.patch.object(pi, "SUBPROJECT_WORD_BUDGET", 5):
        with pytest.raises(pi.InstructionsError) as caught:
            _subproject()
    message = str(caught.value).lower()
    assert "budget" in message and "fr-9" in message
    assert "prose" in message


def test_the_shipped_subproject_template_is_not_rendered():
    """It documents the shape; it is not the source of a single word shipped.

    Generation rather than templating is what makes "additive only"
    enforceable -- there is no prose file for a rule to be written into. A
    contributor who wires this file into the renderer has removed that
    property, so the wiring is what the test watches for.
    """
    assert SUBPROJECT_TEMPLATE.exists()
    assert "Not rendered" in SUBPROJECT_TEMPLATE.read_text(encoding="utf-8")
    source = (REPO / "project_instructions.py").read_text(encoding="utf-8")
    assert "subproject-instructions" not in source


# ---------------------------------------------------------------------------
# Composing it into a repository's own file
# ---------------------------------------------------------------------------

def test_composing_into_nothing_seeds_a_home_for_the_project_s_own_commands():
    """A new file gets the block *and* a `## Validation` section below it.

    This used to assert the file was the block alone. It changed with D11,
    which keeps build, test and lint commands out of the generated block:
    they are the three lines every project appends, and generating them puts
    the tool and the project in a fight over the same text that regeneration
    wins silently.

    Keeping them out is only half of it. Somewhere has to hold them, or the
    rule reads as "not here" and they come back inside the markers on the next
    pass. The stub is that somewhere, and it is outside the markers, so
    ``compose`` preserves whatever the project writes into it.
    """
    out = pi.compose(None, "BLOCK\n", seed_validation=True)
    assert out.startswith("BLOCK\n")
    assert pi.VALIDATION_STUB in out
    assert pi.compose("   \n\n", "BLOCK\n", seed_validation=True) == out


def test_the_seeded_section_is_outside_the_markers():
    """Inside them it would be deleted by the very next regeneration.

    The whole value of the stub is that it survives, so the thing to check is
    not that it appears but *where*: a stub written between the markers reads
    identically in the file and is gone on the next `operator projects`.
    """
    out = pi.compose(None, f"{MANAGED_BEGIN}\nx\n{MANAGED_END}\n",
                       seed_validation=True)
    assert out.index(MANAGED_END) < out.index(pi.VALIDATION_STUB.strip())


def test_a_project_that_deletes_the_stub_is_not_given_it_back():
    """Seeding once is a suggestion; seeding every time is the fight again.

    A project whose commands live in its README, or which has none, deletes
    the section. Regenerating it would restore a heading somebody removed on
    purpose -- the same silent overwrite D11 exists to prevent, arriving from
    the other direction.
    """
    seeded = pi.compose(None, f"{MANAGED_BEGIN}\nx\n{MANAGED_END}\n",
                       seed_validation=True)
    without = seeded.replace(pi.VALIDATION_STUB, "")
    again = pi.compose(without, f"{MANAGED_BEGIN}\ny\n{MANAGED_END}\n")
    assert "## Validation" not in again


def test_the_seeded_section_survives_regeneration_and_is_not_doubled():
    """What the project writes under the heading is the project's.

    Checked with edited content rather than the pristine stub, because
    ``compose`` preserving text it just wrote proves less than it preserving
    text somebody replaced.
    """
    seeded = pi.compose(None, f"{MANAGED_BEGIN}\nx\n{MANAGED_END}\n",
                       seed_validation=True)
    edited = seeded.replace(pi.VALIDATION_STUB,
                            "## Validation\n\n`pytest -q`, then `ruff check`.\n")
    again = pi.compose(edited, f"{MANAGED_BEGIN}\ny\n{MANAGED_END}\n")
    assert again.count("## Validation") == 1
    assert "`pytest -q`, then `ruff check`." in again
    assert "y" in again and "x" not in again.split(MANAGED_END)[0]


def test_an_existing_file_keeps_everything_it_had():
    existing = "# My project\n\nMy own notes.\n"
    out = pi.compose(existing, f"{MANAGED_BEGIN}\nx\n{MANAGED_END}\n")
    assert out.startswith(existing.rstrip("\n"))
    assert MANAGED_BEGIN in out


def test_a_second_pass_replaces_the_block_and_touches_nothing_else():
    """Idempotence, and the reason the markers exist.

    Everything above and below the block is compared byte for byte, because
    the failure that matters is not "the block is wrong" -- it is a
    regeneration that eats the paragraph its author wrote underneath.
    """
    first = pi.compose("# Mine\n\nabove\n",
                       f"{MANAGED_BEGIN}\nv1\n{MANAGED_END}\n")
    first += "\nbelow, written by hand\n"
    second = pi.compose(first, f"{MANAGED_BEGIN}\nv2\n{MANAGED_END}\n")
    assert "v1" not in second
    assert "v2" in second
    assert second.startswith("# Mine\n\nabove\n")
    assert second.endswith("\nbelow, written by hand\n")
    assert second.count(MANAGED_BEGIN) == 1


def test_recomposing_the_same_block_changes_nothing():
    block = f"{MANAGED_BEGIN}\nv1\n{MANAGED_END}\n"
    once = pi.compose("# Mine\n\nabove\n", block)
    assert pi.compose(once, block) == once


@pytest.mark.parametrize("broken", [
    f"{MANAGED_BEGIN}\na\n{MANAGED_END}\n{MANAGED_BEGIN}\nb\n{MANAGED_END}\n",
    f"{MANAGED_BEGIN}\nno end marker\n",
    f"{MANAGED_END}\nno begin marker\n",
    f"{MANAGED_END}\ninverted\n{MANAGED_BEGIN}\n",
])
def test_malformed_markers_raise_rather_than_being_repaired(broken):
    """Guessing which block is live is how a file ends up with two sets of
    conventions that disagree -- and the disagreement would then be invisible
    to the function meant to keep them in step."""
    with pytest.raises(InstructionsError):
        pi.compose(broken, f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")


# ---------------------------------------------------------------------------
# G9 / D3 -- migrating the marker spelling
#
# The rename is only safe because the old spelling is still *read*. A writer
# that knew the new marker alone would find no block in a file carrying the
# old one, append a second block below it, and leave the repository holding
# two sets of conventions that disagree -- with the disagreement invisible to
# the very function meant to keep them in step. That is the failure these
# tests exist for; "the new marker is emitted" is the easy half.
# ---------------------------------------------------------------------------

LEGACY_BEGIN = pi.LEGACY_BEGIN
LEGACY_END = pi.LEGACY_END


def test_the_two_spellings_are_actually_different():
    """Every test below is vacuous if the rename never happened -- they would
    all be asserting that the current marker replaces itself, which the
    idempotence tests already cover. This is the anchor."""
    assert MANAGED_BEGIN != LEGACY_BEGIN
    assert MANAGED_END != LEGACY_END
    assert "operator:managed" in MANAGED_BEGIN


def test_an_old_block_is_replaced_not_doubled():
    existing = ("# Mine\n\nabove\n"
                f"{LEGACY_BEGIN}\nold conventions\n{LEGACY_END}\n"
                "\nbelow, written by hand\n")
    out = pi.compose(existing, f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")
    assert "old conventions" not in out
    assert out.count(MANAGED_BEGIN) == 1
    assert LEGACY_BEGIN not in out
    assert LEGACY_END not in out
    assert out.startswith("# Mine\n\nabove\n")
    assert out.endswith("\nbelow, written by hand\n")


def test_migration_happens_once(machine):
    """A migrated file is an ordinary file. Regenerating it again must not
    find anything left to migrate, or the "migration" is a rewrite that
    happens forever and every repository shows a diff on every run."""
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    target.write_text(
        "# theirs\n\n"
        f"{LEGACY_BEGIN}\nancient\n{LEGACY_END}\n\ntrailing note\n",
        encoding="utf-8")
    _retire(machine)
    first = target.read_text(encoding="utf-8")
    assert LEGACY_BEGIN not in first
    assert MANAGED_BEGIN in first
    assert first.endswith("\ntrailing note\n")
    result = _retire(machine)
    assert target.read_text(encoding="utf-8") == first
    assert result.outcomes[0].state == UNCHANGED


def test_a_file_with_an_old_block_is_not_asked_for_consent_again(machine):
    """The consent question is "may we write into a file we did not write".
    A legacy block *is* ours; asking again would train the answer, and a
    caller that answers no would strand the repository on the old spelling
    forever."""
    asked = []
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    target.write_text(f"{LEGACY_BEGIN}\nancient\n{LEGACY_END}\n",
                      encoding="utf-8")
    _retire(machine, decide=lambda project, existing: asked.append(project) or False)
    assert asked == []
    assert MANAGED_BEGIN in target.read_text(encoding="utf-8")


def test_both_spellings_at_once_is_refused():
    """No writer produces this, so it is a hand-edit or an interrupted
    migration. Replacing one and leaving the other is the doubling this
    whole mechanism exists to prevent, and picking which to keep is a guess
    about which set of conventions somebody meant."""
    existing = (f"{LEGACY_BEGIN}\nold\n{LEGACY_END}\n"
                f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")
    with pytest.raises(InstructionsError):
        pi.compose(existing, f"{MANAGED_BEGIN}\nnewer\n{MANAGED_END}\n")


def test_a_begin_of_one_spelling_and_an_end_of_the_other_delimit_nothing():
    """Pooling both spellings' offsets into one pair of lists would make
    these two look like a well-formed block, and `compose` would replace a
    span whose real boundaries nobody knows -- silently, since the count
    check would be satisfied."""
    existing = f"{LEGACY_BEGIN}\nbody\n{MANAGED_END}\n"
    with pytest.raises(InstructionsError):
        pi.compose(existing, f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")


def test_crossed_markers_are_not_a_block_to_the_finder_either():
    """Driven at `_marker_offsets`, because `compose`'s two guards catch two
    different files and the test above only proves one of them fired.

    The mixed-spelling refusal covers a file with *whole blocks* in both
    spellings. This covers a file with a begin from one and an end from the
    other -- and only the per-pair discipline here catches it, because the
    pooled counts would be one and one, which is exactly what well-formed
    looks like. Mutation found this: pooling the pairs left every other test
    in the file green.
    """
    begins, ends = pi._marker_offsets(f"{LEGACY_BEGIN}\nbody\n{MANAGED_END}\n")
    assert (len(begins), len(ends)) != (1, 1)


@pytest.mark.parametrize("broken", [
    f"{LEGACY_BEGIN}\na\n{LEGACY_END}\n{LEGACY_BEGIN}\nb\n{LEGACY_END}\n",
    f"{LEGACY_BEGIN}\nno end marker\n",
    f"{LEGACY_END}\nno begin marker\n",
    f"{LEGACY_END}\ninverted\n{LEGACY_BEGIN}\n",
])
def test_malformed_old_markers_raise_too(broken):
    """The old spelling gets the same refusals, not a lenient path. A
    migration that repaired what the current spelling refuses to repair
    would be the one moment a file is least understood."""
    with pytest.raises(InstructionsError):
        pi.compose(broken, f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")


def test_an_old_marker_quoted_in_a_fence_is_not_a_block():
    """The fence narrowing applies to both spellings. A project documenting
    the *previous* marker -- exactly what a migration note looks like --
    would otherwise have its note replaced by the migration it describes."""
    existing = ("# theirs\n\n```markdown\n"
                f"{LEGACY_BEGIN}\n...\n{LEGACY_END}\n```\n")
    out = pi.compose(existing, f"{MANAGED_BEGIN}\nnew\n{MANAGED_END}\n")
    assert out.startswith(existing.rstrip("\n"))
    assert LEGACY_BEGIN in out
    assert MANAGED_BEGIN in out


def test_a_rendered_block_carries_only_the_new_spelling(template):
    rendered = pi.render(
        source=template, values=project_features.resolved_values(None),
        guid="GUID", project_path="/tmp/x", label="x",
        project_dir_path=Path("/tmp/p"), config_path=Path("/tmp/p/f.json"),
        version="1.0.0", platform=pi.POSIX)
    assert LEGACY_BEGIN not in rendered
    assert LEGACY_END not in rendered
    assert rendered.startswith(MANAGED_BEGIN)


# ---------------------------------------------------------------------------
# FR-8 -- flags default off, and what that must not silently do
# ---------------------------------------------------------------------------

def test_every_flag_ships_off(shipped_defaults):
    """The requirement itself: a section is in a project's conventions
    because somebody needs it, not because nobody chose."""
    flags = [f for f in project_features.FEATURES
             if f.slug != project_features.TRACKED_BACKLOG]
    assert flags, "no flags left to check -- this test would pass vacuously"
    for feature in flags:
        assert feature.default == feature.off_value, feature.slug
        assert not project_features.is_enabled(shipped_defaults, feature.slug)


def test_the_backlog_choice_still_defaults_to_the_enforcing_answer():
    """The one feature deliberately not flipped, and the reason is not
    cosmetic. `tracked_backlog_backend` answers with this default under
    every uncertainty -- no catalog in CI, an unreadable file, a project
    nobody registered. Defaulting it to `none` stands three real guards
    down on all eight legs while every one of them stays green."""
    feature = project_features.FEATURES_BY_SLUG[project_features.TRACKED_BACKLOG]
    assert feature.default == project_features.BACKLOG_FOLDER
    assert feature.default != feature.off_value


def test_a_project_that_never_chose_is_refused_not_answered_for(machine):
    """"Default off" must not mean "quietly delete what they were using".

    Every registered project on the machine this was written on had no
    configuration file, so resolving an absent one would have stripped the
    optional sections out of eight repositories at once on the next routine
    regeneration -- with the diff attributed to a version bump. Refusing is
    what makes an enabled section a live requirement rather than an
    accident of who last ran the tool.
    """
    (machine["projects_root"] / "GUID-1"
     / project_features.CONFIG_NAME).unlink()
    result = _retire(machine)
    outcome = result.outcomes[0]
    assert outcome.state == FAILED
    assert "has not chosen its features" in outcome.detail
    assert "operator projects" in outcome.detail


def test_the_refusal_keeps_the_global_file_where_it_is(machine):
    """The safe direction: conventions in two places, never in none."""
    (machine["projects_root"] / "GUID-1"
     / project_features.CONFIG_NAME).unlink()
    result = _retire(machine)
    assert not result.removed
    assert machine["global_path"].exists()


def test_the_refusal_does_not_touch_that_project_s_file(machine):
    """A project that has not chosen keeps whatever it already had. Writing
    an all-off block and *then* reporting failure would leave the damage
    behind the error message."""
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    before = target.read_text(encoding="utf-8")
    (machine["projects_root"] / "GUID-1"
     / project_features.CONFIG_NAME).unlink()
    _retire(machine)
    assert target.read_text(encoding="utf-8") == before


def test_an_unreadable_configuration_is_still_refused(machine):
    """Unchanged by FR-8, and re-pinned because the absent case now shares
    its code path: the two must not collapse into one another in either
    direction."""
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    config.write_text("{ not json", encoding="utf-8")
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert "has not chosen its features" not in result.outcomes[0].detail


def test_a_configured_project_renders_only_what_it_chose(machine):
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    config.write_text(json.dumps(
        {"version": 1, "features": {"session-handoff": project_features.ON}}),
        encoding="utf-8")
    _retire(machine)
    text = (Path(machine["projects"][0]["path"])
            / AGENTS_NAME).read_text(encoding="utf-8")
    assert "## Session Handoff Protocol" in text
    assert "## Parallel Agents" not in text


# ---------------------------------------------------------------------------
# FR-8 -- one platform's commands, chosen from the host
# ---------------------------------------------------------------------------

_TWO_PLATFORMS = (
    "## Commands\n"
    "\n"
    "Run it:\n"
    "\n"
    "<!-- operator:platform posix -->\n"
    "**bash**\n"
    "```bash\n"
    "touch marker\n"
    "```\n"
    "\n"
    "<!-- operator:endplatform -->\n"
    "<!-- operator:platform windows -->\n"
    "**PowerShell**\n"
    "```powershell\n"
    "New-Item marker\n"
    "```\n"
    "\n"
    "<!-- operator:endplatform -->\n"
    "Then read it.\n"
)


@pytest.mark.parametrize("os_name, expected", [("nt", pi.WINDOWS),
                                               ("posix", pi.POSIX),
                                               ("java", pi.POSIX)])
def test_the_host_names_one_of_two_vocabularies(os_name, expected):
    assert pi.host_platform(os_name) == expected


@pytest.mark.parametrize("platform, kept, dropped", [
    (pi.POSIX, "touch marker", "New-Item marker"),
    (pi.WINDOWS, "New-Item marker", "touch marker"),
])
def test_only_the_hosts_commands_survive(platform, kept, dropped):
    out = pi.select_platform(_TWO_PLATFORMS, platform)
    assert kept in out
    assert dropped not in out


def test_the_markers_themselves_never_reach_the_repository():
    """Both branches, because only one of them is the deleting one.

    A marker left on a *kept* block would be written into the repository, and
    the next run could no longer tell the template's marker from one a user
    had copied into their own text.
    """
    for platform in pi.PLATFORMS:
        out = pi.select_platform(_TWO_PLATFORMS, platform)
        assert "operator:platform" not in out
        assert pi.PLATFORM_END not in out


@pytest.mark.parametrize("platform", pi.PLATFORMS)
def test_removing_a_block_does_not_double_a_blank_line(platform):
    """The only visible difference a reader could catch between the two.

    Both halves are checked: the seam collapse must not run where nothing was
    removed either, or the prose loses paragraph breaks it meant to have.
    """
    out = pi.select_platform(_TWO_PLATFORMS, platform)
    assert "\n\n\n" not in out
    assert "Run it:\n\n" in out, "an ordinary paragraph break was eaten"
    assert out.endswith("Then read it.\n")


@pytest.mark.parametrize("platform", pi.PLATFORMS)
def test_a_blank_run_away_from_the_seam_is_left_exactly_as_written(platform):
    """The collapse is confined to the seam, and that has to be measured.

    An unconditional collapse passes every test above -- the shipped template
    happens to contain no doubled blank line today, so the two rules agree on
    it. They stop agreeing the moment somebody writes one, and at that point
    the renderer would be quietly editing prose it was only asked to select
    from.
    """
    source = _TWO_PLATFORMS.replace("Run it:\n", "Run it:\n\n\nStill here.\n", 1)
    assert "Run it:\n\n\nStill here.\n" in source
    out = pi.select_platform(source, platform)
    assert "Run it:\n\n\nStill here.\n" in out


def test_a_platform_this_build_does_not_know_is_kept():
    """The downgrade case, answered the same way as an unknown gate slug.

    A newer template naming a platform this build has never heard of is the
    only copy of that text. Dropping it would delete conventions purely by
    being out of date -- and it would do it on every platform at once, since
    the name matches none of them.
    """
    source = _TWO_PLATFORMS.replace("operator:platform posix",
                                    "operator:platform plan9", 1)
    for platform in pi.PLATFORMS:
        out = pi.select_platform(source, platform)
        assert "touch marker" in out


def test_a_marker_inside_a_fence_is_not_a_marker():
    """The same rule the managed-block finder lives by.

    A repository's own conventions may document this mechanism, and the
    documentation is written in a fence. Reading the sample as real would
    delete everything after it on one of the two platforms.
    """
    source = ("## Docs\n"
              "\n"
              "```markdown\n"
              "<!-- operator:platform windows -->\n"
              "sample\n"
              "<!-- operator:endplatform -->\n"
              "```\n"
              "\n"
              "kept\n")
    assert pi.select_platform(source, pi.POSIX) == source


@pytest.mark.parametrize("broken, why", [
    ("<!-- operator:platform posix -->\nx\n", "never closed"),
    ("<!-- operator:endplatform -->\nx\n", "with no platform"),
    ("<!-- operator:platform posix -->\n<!-- operator:platform windows -->\n"
     "x\n<!-- operator:endplatform -->\n", "is still open"),
])
def test_unbalanced_markers_are_refused(broken, why):
    """Never recovered from.

    A stray begin marker deletes the whole rest of a section on one platform
    and nothing at all on the other, so a run on either machine alone looks
    fine. Raising is what makes the two agree.
    """
    with pytest.raises(pi.InstructionsError) as caught:
        pi.select_platform(broken, pi.POSIX)
    assert why in str(caught.value)


def test_render_will_not_guess_the_platform():
    """No default, deliberately.

    A default would make every test that forgot the argument agree with the
    machine it ran on, so the Windows legs and the POSIX legs would each
    prove only their own half.
    """
    with pytest.raises(TypeError):
        pi.render(source="## A\n\nbody\n",
                  values=project_features.resolved_values(None),
                  guid="G", project_path="/p", label="p",
                  project_dir_path=Path("/d"), config_path=Path("/d/f.json"),
                  version="1.0.0")


#: Spellings that only work on one platform, and the platform they work on.
#:
#: These are the tells a bracket exists for. Deliberately spelled as fragments
#: rather than whole commands: what makes a line unportable is the syntax, and
#: a check against whole commands would pass the moment somebody wrote a new
#: one.
_PLATFORM_TELLS = {
    pi.WINDOWS: ("New-Item", "Get-ChildItem", "Select-Object", "$env:",
                 "-ItemType", "```powershell"),
    pi.POSIX: ("mktemp -d", "touch ~", "$(", "```bash", "```sh"),
}


def test_the_shipped_template_spells_no_command_for_one_platform_only(template):
    """Every platform-specific command is bracketed, or there are none.

    This used to assert the template brackets *at least one* command. The
    block no longer brackets any: every command in it is now `operator ...`,
    which is spelled identically on both platforms, and the two renderings are
    byte-for-byte equal.

    Asserting a non-empty bracket list would now be a rule that the document
    must contain platform-specific text -- satisfiable only by adding some.
    Inverting it is strictly stronger than the original, which could pass with
    an unbracketed PowerShell snippet sitting in the template as long as one
    *other* command happened to be bracketed correctly.
    """
    bracketed = {
        index for start, end in _bracket_ranges(template)
        for index in range(start, end + 1)
    }
    stray = [
        (platform, tell, line.strip())
        for index, line in pi.outside_fences(template)
        if index not in bracketed
        for platform, tells in _PLATFORM_TELLS.items()
        for tell in tells
        if tell in line
    ]
    assert not stray, (
        "the block spells commands that only work on one platform, without "
        "bracketing them:\n  "
        + "\n  ".join(f"{p}: {tell!r} in {line}" for p, tell, line in stray)
        + "\nAn agent on the other platform is handed a command that fails.")


def test_the_platform_tell_scan_would_fire():
    """The control. A scan that matched nothing would clear any document."""
    stray = [
        tell for _index, line in pi.outside_fences(
            "## A\n\nRun `New-Item -ItemType File x` first.\n")
        for tell in _PLATFORM_TELLS[pi.WINDOWS] if tell in line
    ]
    assert stray, "the tell list no longer matches a plainly PowerShell line"


def test_brackets_in_the_shipped_template_are_paired(template):
    """If the block ever brackets again, both halves must be there.

    Vacuous today by construction -- there are no brackets -- so it says so
    rather than passing quietly on an empty list. A guard that reports clean
    on nothing is indistinguishable from one that works.
    """
    names = [pi.PLATFORM_BEGIN.match(line.strip()).group("name")
             for _index, line in pi.outside_fences(template)
             if pi.PLATFORM_BEGIN.match(line.strip())]
    assert names.count(pi.WINDOWS) == names.count(pi.POSIX), names
    assert len(names) == template.count(pi.PLATFORM_END)
    for platform in pi.PLATFORMS:
        assert pi.select_platform(template, platform).strip()
    if not names:
        # Both renderings identical is the *reason* there is nothing to pair,
        # and checking it keeps this from being a test that proves nothing.
        assert (pi.select_platform(template, pi.WINDOWS)
                == pi.select_platform(template, pi.POSIX))


def _bracket_ranges(text: str) -> list[tuple[int, int]]:
    """Line-index spans covered by a platform bracket, ends included."""
    ranges, open_at = [], None
    for index, line in pi.outside_fences(text):
        stripped = line.strip()
        if pi.PLATFORM_BEGIN.match(stripped):
            open_at = index
        elif stripped == pi.PLATFORM_END and open_at is not None:
            ranges.append((open_at, index))
            open_at = None
    return ranges


#: A stand-in for what the shipped template used to contain.
#:
#: `select_platform` still has to work -- a project's own sections may bracket
#: commands, and an older template still in a checkout certainly does -- so
#: the mechanism is checked against a document written for the purpose rather
#: than against whichever commands the block happens to spell this month.
_BRACKETED = (
    "# T\n\nintro\n\n## A\n\n"
    "<!-- operator:platform windows -->\n"
    "```powershell\n"
    "New-Item -ItemType File -Force ~/.operator/restart/x\n"
    "```\n"
    "<!-- operator:endplatform -->\n"
    "<!-- operator:platform posix -->\n"
    "```bash\n"
    "touch ~/.operator/restart/x\n"
    "```\n"
    "<!-- operator:endplatform -->\n"
)


@pytest.mark.parametrize("platform, kept, dropped", [
    (pi.WINDOWS, "New-Item -ItemType File", "touch ~/.operator/restart"),
    (pi.POSIX, "touch ~/.operator/restart", "New-Item -ItemType File"),
])
def test_a_rendered_block_carries_one_platforms_commands(defaults, platform,
                                                         kept, dropped):
    out = _render(_BRACKETED, defaults, platform=platform)
    assert kept in out
    assert dropped not in out
    assert "operator:platform" not in out
    assert "\n\n\n" not in out


# ---------------------------------------------------------------------------
# FR-8 -- the CLAUDE.md import
# ---------------------------------------------------------------------------

def test_claude_imports_agents_rather_than_repeating_it():
    out = pi.render_claude(label="app", version="1.2.3")
    assert f"@{AGENTS_NAME}" in out
    assert out.startswith(MANAGED_BEGIN)
    assert out.rstrip("\n").endswith(pi.MANAGED_END)


def test_claude_carries_none_of_the_conventions(template, defaults):
    """Two texts that can disagree, read by one agent in one turn.

    The import exists so there is one copy. A generated ``CLAUDE.md`` that
    also carried a section would make the newer of the two files right, and
    which one that is cannot be seen from either.
    """
    claude = pi.render_claude(label="app", version="1.2.3")
    agents = _render(template, defaults)
    for section in ("## Git Worktrees", "## Scratch Files"):
        assert section in agents
        assert section not in claude


def test_every_project_gets_a_claude_file(machine):
    _retire(machine)
    for project in machine["projects"]:
        text = (Path(project["path"]) / pi.CLAUDE_NAME).read_text(
            encoding="utf-8")
        assert f"@{AGENTS_NAME}" in text


def test_a_second_run_leaves_the_claude_file_alone(machine):
    _retire(machine)
    first = [(Path(p["path"]) / pi.CLAUDE_NAME).read_text(encoding="utf-8")
             for p in machine["projects"]]
    _retire(machine)
    second = [(Path(p["path"]) / pi.CLAUDE_NAME).read_text(encoding="utf-8")
              for p in machine["projects"]]
    assert first == second


def test_a_users_own_claude_file_is_not_touched(machine):
    """Left alone rather than merged into, and not a blocker either.

    Its whole content is one import line, so a second consent prompt per
    project would spend the operator's attention on the file that carries
    none of the conventions.
    """
    target = Path(machine["projects"][0]["path"]) / pi.CLAUDE_NAME
    target.write_text("# mine\n\nHands off.\n", encoding="utf-8")
    result = _retire(machine)
    assert target.read_text(encoding="utf-8") == "# mine\n\nHands off.\n"
    assert [o.state for o in result.outcomes] == [WRITTEN, WRITTEN]


def test_a_claude_file_with_a_managed_block_is_regenerated(machine):
    """The half the "leave it alone" rule must not swallow.

    A file this tool wrote is a file this tool keeps true. Only a file with
    no block of ours in it is somebody else's.
    """
    target = Path(machine["projects"][0]["path"]) / pi.CLAUDE_NAME
    target.write_text(
        "# mine\n\nkept\n\n"
        + pi.render_claude(label="app", version="0.0.1"),
        encoding="utf-8")
    _retire(machine)
    out = target.read_text(encoding="utf-8")
    assert "kept" in out, "content outside the block was destroyed"
    assert "0.0.1" not in out, "the stale block was not regenerated"
    assert f"@{AGENTS_NAME}" in out


def _declare(machine, mapping: dict, contracts=()) -> Path:
    """Give the first project a subproject declaration and the directories."""
    root = Path(machine["projects"][0]["path"])
    for owned in mapping.values():
        for path in owned:
            (root / path).mkdir(parents=True, exist_ok=True)
    declaration = root / ".operator" / "subprojects.json"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(json.dumps(
        {"subprojects": {name: {"owns": list(owned)}
                         for name, owned in mapping.items()},
         "contracts": list(contracts)}), encoding="utf-8")
    return root


def test_a_repository_that_declares_no_subprojects_gets_no_extra_files(machine):
    """Every repository, until somebody draws a boundary.

    The declaration being absent is not an error and not a prompt: it is the
    normal shape of a repository, and generation must be silent about it.
    """
    _retire(machine)
    root = Path(machine["projects"][0]["path"])
    assert [p.name for p in root.rglob(AGENTS_NAME)] == [AGENTS_NAME]


def test_each_declared_subproject_directory_gets_its_own_file(machine):
    root = _declare(machine, {"api": ["services/api"],
                              "web": ["clients/web"]},
                    contracts=["specs/contracts"])
    _retire(machine)
    api = (root / "services" / "api" / AGENTS_NAME).read_text(encoding="utf-8")
    web = (root / "clients" / "web" / AGENTS_NAME).read_text(encoding="utf-8")
    assert "`api` subproject" in api and "`services/api`" in api
    assert "`web` subproject" in web
    assert "`specs/contracts`" in api


def test_a_subproject_owning_two_trees_gets_a_file_in_each(machine):
    """A nested file only helps an agent editing near it.

    Both are generated in the same run from the same values, so this is not
    the duplication FR-9 forbids -- there is no way for them to disagree.
    """
    root = _declare(machine, {"api": ["services/api", "clients/api-sdk"]})
    _retire(machine)
    for owned in ("services/api", "clients/api-sdk"):
        text = (root / owned / AGENTS_NAME).read_text(encoding="utf-8")
        assert "`services/api`" in text and "`clients/api-sdk`" in text


def test_a_declared_directory_that_is_not_there_is_skipped(machine):
    """A declaration can name a path before anyone creates it.

    Creating the directory to hold the file would put the tool in the
    business of inventing the tree it was asked to describe.
    """
    root = Path(machine["projects"][0]["path"])
    (root / ".operator").mkdir(parents=True)
    (root / ".operator" / "subprojects.json").write_text(
        json.dumps({"subprojects": {"api": {"owns": ["services/api"]}}}),
        encoding="utf-8")
    result = _retire(machine)
    assert [o.state for o in result.outcomes] == [WRITTEN, WRITTEN]
    assert not (root / "services").exists()


def test_an_unreadable_declaration_fails_the_project(machine):
    """Not a silent skip.

    `operator ownership check` refuses on an unreadable declaration, so
    swallowing it here leaves a repository whose push gate is broken and
    whose generation reported success.
    """
    root = Path(machine["projects"][0]["path"])
    (root / ".operator").mkdir(parents=True)
    (root / ".operator" / "subprojects.json").write_text(
        "{not json", encoding="utf-8")
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert "subproject" in result.outcomes[0].detail.lower()


def test_a_users_own_subproject_file_is_not_touched(machine):
    root = _declare(machine, {"api": ["services/api"]})
    target = root / "services" / "api" / AGENTS_NAME
    target.write_text("# mine\n\nHands off.\n", encoding="utf-8")
    result = _retire(machine)
    assert target.read_text(encoding="utf-8") == "# mine\n\nHands off.\n"
    assert [o.state for o in result.outcomes] == [WRITTEN, WRITTEN]


def test_a_subproject_file_regenerates_and_keeps_what_is_outside_the_block(
        machine):
    root = _declare(machine, {"api": ["services/api"]})
    target = root / "services" / "api" / AGENTS_NAME
    target.write_text(
        "# mine\n\nkept\n\n"
        + pi.render_subproject(name="api", owns=["stale"], contracts=[],
                               version="0.0.1"),
        encoding="utf-8")
    _retire(machine)
    out = target.read_text(encoding="utf-8")
    assert "kept" in out, "content outside the block was destroyed"
    assert "0.0.1" not in out and "stale" not in out
    assert "`services/api`" in out


def test_a_declared_prefix_that_climbs_out_of_the_repository_is_refused(
        machine):
    """Found by review, and it wrote a real file outside a real checkout.

    `operator_ownership.normalize` drops `.` segments and empty ones but
    keeps `..`, which is correct for the check it was written for -- git
    never emits a `..` path, so the segment is inert there. It is not inert
    here: this is the first code that turns a declared prefix into a
    *destination*, and `root.joinpath("..", "..", "x")` is a write into
    somebody else's repository.

    Refused at the declaration, so every consumer gets it and the message
    names the file a human can edit.
    """
    root = Path(machine["projects"][0]["path"])
    (root / ".operator").mkdir(parents=True)
    (root / ".operator" / "subprojects.json").write_text(
        json.dumps({"subprojects": {"api": {"owns": ["../escaped"]}}}),
        encoding="utf-8")
    (root.parent / "escaped").mkdir(exist_ok=True)
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert not (root.parent / "escaped" / AGENTS_NAME).exists()


def test_each_climbing_path_guard_is_separately_falsifiable(tmp_path):
    """Mutation found this: the end-to-end tests could not tell them apart.

    Two guards refuse a climbing path -- the declaration reader and the
    resolved-containment check in `_place_subprojects`. Asserting only that
    generation failed means either one alone satisfies the test, so deleting
    the declaration refusal left the suite green. The containment check does
    stop the write, so nothing unsafe shipped; what was lost was the *message*
    naming `subprojects.json`, which is the whole reason for having the first
    guard at all.

    So this one goes at `read_declaration` directly, where only one guard can
    answer.
    """
    (tmp_path / ".operator").mkdir(parents=True)
    declaration = tmp_path / ".operator" / "subprojects.json"

    for payload in (
            {"subprojects": {"api": {"owns": ["../escaped"]}}},
            {"subprojects": {"api": {"owns": ["services/api"]}},
             "contracts": ["../elsewhere"]}):
        declaration.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(operator_ownership.OwnershipError) as caught:
            operator_ownership.read_declaration(tmp_path)
        assert ".." in str(caught.value)


def test_the_declaration_reader_admits_an_ordinary_path(tmp_path):
    """The control for the pair above. A reader that refused every path would
    pass both of them and ship a tool that can declare nothing."""
    (tmp_path / ".operator").mkdir(parents=True)
    (tmp_path / ".operator" / "subprojects.json").write_text(
        json.dumps({"subprojects": {"api": {"owns": ["services/api"]}},
                    "contracts": ["specs/contracts"]}),
        encoding="utf-8")
    declaration = operator_ownership.read_declaration(tmp_path)
    assert declaration is not None
    assert tuple(declaration.subprojects["api"]) == (("services", "api"),)


def test_a_contract_path_that_climbs_out_of_the_repository_is_refused(machine):
    """The same segment in the other list.

    A guard written on one of two loops over the same JSON is a guard the
    next edit walks around.
    """
    root = _declare(machine, {"api": ["services/api"]},
                    contracts=["../elsewhere"])
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert not (root / "services" / "api" / AGENTS_NAME).exists()


def test_a_symlinked_subproject_directory_that_escapes_is_refused(
        machine, tmp_path):
    """The half the declaration cannot express.

    `..` is refusable in the file. A directory that *is* a symlink or a
    junction pointing out of the tree is not: the declaration says
    `services/api` and means it, and only the resolved path knows better.
    Both checks are kept because each catches a case the other cannot.
    """
    root = Path(machine["projects"][0]["path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "services").mkdir(parents=True)
    try:
        (root / "services" / "api").symlink_to(outside,
                                               target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine will not create a directory symlink")
    (root / ".operator").mkdir(parents=True)
    (root / ".operator" / "subprojects.json").write_text(
        json.dumps({"subprojects": {"api": {"owns": ["services/api"]}}}),
        encoding="utf-8")
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert not (outside / AGENTS_NAME).exists()


def test_the_containment_check_admits_what_it_should():
    """The control. A guard that refuses everything is the same defect
    wearing a safer face -- it stops the writes it was meant to allow, and
    the suite above cannot tell the difference."""
    root = Path("/repo").resolve()
    assert pi._within(root, root)
    assert pi._within(root / "services" / "api", root)
    assert not pi._within(root.parent / "repo-two", root)
    assert not pi._within(root.parent, root)


def test_a_target_that_cannot_be_resolved_is_refused_not_skipped(
        machine, monkeypatch):
    """Caught by `test_resolve_conformance`, which is the point of having it.

    `Path.resolve` fails three ways -- `OSError` on a denial, `RuntimeError`
    on a symlink loop, `ValueError` on an embedded NUL -- and the first draft
    caught one of the three, so the other two left as tracebacks out of
    `operator projects`.

    The direction matters more than the coverage. `project_paths.resolved_str`
    exists for exactly this and is *wrong* here: its fallback is a lexical
    absolute path, which is less resolved than the truth, so a containment
    gate using it admits the target it could not check. A write gate that
    cannot see must say no.
    """
    root = _declare(machine, {"api": ["services/api"]})
    real_resolve = Path.resolve

    def exploding(self, *args, **kwargs):
        if self.name == "api":
            raise RuntimeError("symlink loop")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", exploding)
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert not (root / "services" / "api" / AGENTS_NAME).exists()


def test_only_the_root_file_is_given_the_validation_stub(machine):
    """Found by review: the stub was leaking into every new file.

    `compose` seeds it when it is creating a file from nothing, and both
    `CLAUDE.md` and the subproject files are usually created from nothing.
    `CLAUDE.md`'s entire content is one import line, and a subproject file is
    supposed to carry facts and nothing else -- neither is anybody's home for
    build commands. The seed is off by default now and one caller asks for it.
    """
    root = _declare(machine, {"api": ["services/api"]})
    _retire(machine)
    stub_heading = pi.VALIDATION_STUB.splitlines()[0]
    assert stub_heading in (root / AGENTS_NAME).read_text(encoding="utf-8")
    assert stub_heading not in (root / pi.CLAUDE_NAME).read_text(
        encoding="utf-8")
    assert stub_heading not in (
        root / "services" / "api" / AGENTS_NAME).read_text(encoding="utf-8")


def test_the_shipped_subproject_file_states_no_rules(machine):
    """FR-9 measured on the bytes that reach the disk.

    The generator's output and the file's content are not the same thing --
    `compose` sits between them, and it is what put the validation stub into
    subproject files in the first place. Checking the generator alone is how
    that went unnoticed.
    """
    root = _declare(machine, {"api": ["services/api"]})
    _retire(machine)
    shipped = (root / "services" / "api" / AGENTS_NAME).read_text(
        encoding="utf-8")
    found = [tell for tell in _DIRECTIVE_TELLS if tell in shipped.lower()]
    assert not found, f"the shipped subproject file gives directions: {found}"


def test_a_second_run_leaves_the_subproject_files_alone(machine):
    root = _declare(machine, {"api": ["services/api"]})
    target = root / "services" / "api" / AGENTS_NAME
    _retire(machine)
    first = target.read_text(encoding="utf-8")
    _retire(machine)
    assert target.read_text(encoding="utf-8") == first


# ---------------------------------------------------------------------------
# Writing and preserving
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "sub" / "AGENTS.md"
    pi.write_text_atomic(target, "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert [p.name for p in target.parent.iterdir()] == ["AGENTS.md"]


def test_atomic_write_reports_a_failure_as_an_instructions_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(InstructionsError):
        pi.write_text_atomic(blocker / "sub" / "AGENTS.md", "x")


def test_preserving_copies_the_bytes_and_names_them_by_digest(tmp_path):
    source = tmp_path / "copilot-instructions.md"
    source.write_bytes(b"the user's edited conventions")
    archive = tmp_path / "retired"
    landed = pi.preserve(source, archive)
    assert landed.parent == archive
    assert landed.read_bytes() == b"the user's edited conventions"
    digest = hashlib.sha256(b"the user's edited conventions").hexdigest()
    assert digest[:12] in landed.name
    assert landed.name.endswith(".md")


def test_preserving_the_same_bytes_twice_reuses_the_archive(tmp_path):
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    first = pi.preserve(source, archive)
    second = pi.preserve(source, archive)
    assert first == second
    assert len(list(archive.iterdir())) == 1


def test_the_same_bytes_a_second_apart_are_still_one_archive(tmp_path):
    """The version of the test above that does not depend on the clock.

    The one above passes whenever both calls land in the same second, which is
    almost always, so it went green on seven CI legs and eight local runs and
    failed on the eighth leg with a same-content pair one second apart. Naming
    the two instants is the whole point: reuse must be keyed on the digest,
    and the timestamp in the name must not get a vote.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    first = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 51, tzinfo=timezone.utc))
    second = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 52, tzinfo=timezone.utc))
    assert first == second
    assert first.name == "f-20260805T095751Z-0967115f2813.md"
    assert len(list(archive.iterdir())) == 1


def test_different_bytes_a_second_apart_are_two_archives(tmp_path):
    """The other direction, which reuse-by-digest must not break.

    Without this, keying on the digest and keying on nothing at all are
    indistinguishable: both make the test above pass.
    """
    source = tmp_path / "f.md"
    archive = tmp_path / "retired"
    source.write_bytes(b"first")
    one = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 51, tzinfo=timezone.utc))
    source.write_bytes(b"second")
    two = pi.preserve(
        source, archive, when=datetime(2026, 8, 5, 9, 57, 52, tzinfo=timezone.utc))
    assert one != two
    assert one.read_bytes() == b"first"
    assert two.read_bytes() == b"second"
    assert len(list(archive.iterdir())) == 2


def test_two_files_with_identical_bytes_get_their_own_archives(tmp_path):
    """An archive is named after the file it came from, not just its bytes.

    Two different files can hold the same bytes, and reuse keyed on the digest
    alone would hand back the first one's archive as the second one's
    preserved copy. No bytes would be lost -- they are the same bytes -- but
    ``retired/`` would carry no record that the second file was ever retired,
    and its only caller unlinks the original next.

    The two stems are the same length deliberately. With stems of different
    lengths the stamp-width check rejects the mismatch on its own, so a
    version of this test using ``a.md`` and ``bbbb.md`` passes even when the
    stem is not compared at all.
    """
    archive = tmp_path / "retired"
    one = tmp_path / "aaa.md"
    one.write_bytes(b"identical")
    two = tmp_path / "bbb.md"
    two.write_bytes(b"identical")
    kept_one = pi.preserve(one, archive)
    kept_two = pi.preserve(two, archive)
    assert kept_one != kept_two
    assert kept_one.name.startswith("aaa-")
    assert kept_two.name.startswith("bbb-")
    assert len(list(archive.iterdir())) == 2


def test_two_files_with_identical_bytes_and_different_suffixes_do_too(tmp_path):
    """The same argument for the other end of the name.

    The two suffixes are the same length deliberately, for the reason the
    equal-length stems above are: with ``.md`` against ``.txt`` the stamp
    check rejects the mismatch by itself, and the test passes without the
    suffix ever being compared. Two reviewers found that independently.
    """
    archive = tmp_path / "retired"
    md = tmp_path / "AGENTS.md"
    md.write_bytes(b"identical")
    py = tmp_path / "AGENTS.py"
    py.write_bytes(b"identical")
    kept_md = pi.preserve(md, archive)
    kept_py = pi.preserve(py, archive)
    assert kept_md != kept_py
    assert kept_md.name.endswith(".md")
    assert kept_py.name.endswith(".py")
    assert len(list(archive.iterdir())) == 2


def test_an_archive_that_no_longer_holds_its_own_bytes_is_refused(tmp_path):
    """A corrupted archive must not be handed back as a preserved copy.

    ``preserve``'s only caller unlinks the original next, so returning a file
    that no longer matches its digest would delete the last good copy and
    report success.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    landed = pi.preserve(source, archive)
    landed.write_bytes(b"corrupted after the fact")
    with pytest.raises(InstructionsError):
        pi.preserve(source, archive)


def test_a_stem_with_glob_characters_matches_only_itself(tmp_path):
    """The scan is not a glob, and a caller-controlled stem is why.

    ``Path.glob`` would read ``a?c`` as "any single character in the middle"
    and hand back the archive of ``abc.md`` as the preserved copy of
    ``a?c.md`` -- different bytes, reported as kept.

    The metacharacter has to be ``?`` and the decoy stem has to be the same
    length. ``[ab]`` -- the obvious choice, and the one this test used first
    -- cannot show the difference at all: a character class matches exactly
    one character, so any name a glob wrongly matched would have a
    three-characters-shorter stem and the stamp check would reject it on its
    own. That version passed against a glob implementation, and two reviewers
    said so independently.

    ``a?c.md`` is not a legal filename on Windows, so ``existing_archive`` is
    called directly. Nothing here needs the source to exist on disk: only its
    stem and suffix are read.
    """
    archive = tmp_path / "retired"
    decoy = tmp_path / "abc.md"
    decoy.write_bytes(b"the decoy's bytes")
    kept_decoy = pi.preserve(decoy, archive)
    assert kept_decoy.name.startswith("abc-")
    digest = hashlib.sha256(b"the decoy's bytes").hexdigest()
    assert pi.existing_archive(archive, "a?c.md", digest) is None
    assert pi.existing_archive(archive, "abc.md", digest) == kept_decoy


def test_a_symlink_is_never_accepted_as_a_preserved_copy(tmp_path):
    """The reviewers' finding, and the worst shape in this function.

    ``file_digest`` follows links, so a link in the archive directory reads
    back as whatever it points at. A link pointing at the source being
    retired digests as a perfect match -- it *is* the source -- so it would be
    returned as the preserved copy, and the caller unlinking the original
    would turn it into a dangling link. The bytes would be gone and the run
    would report success.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"the only copy")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"the only copy").hexdigest()
    link = archive / f"f-20260805T095751Z-{digest[:12]}.md"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    with pytest.raises(InstructionsError):
        pi.preserve(source, archive)
    assert source.read_bytes() == b"the only copy"


def test_a_directory_named_like_an_archive_is_refused(tmp_path):
    """The other non-regular candidate, and the one that needs no privileges.

    ``file_digest`` returns None for a directory, so this was already
    refused -- but by a check that cannot tell "not a file" from "wrong
    bytes", and the message said the bytes were wrong.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()
    (archive / f"f-20260805T095751Z-{digest[:12]}.md").mkdir()
    with pytest.raises(InstructionsError) as caught:
        pi.preserve(source, archive)
    assert "not a regular file" in str(caught.value)


def test_duplicates_already_on_disk_are_settled_on_deterministically(tmp_path):
    """Directories retired before the fix already hold the duplicate pairs.

    Reuse has to pick one, pick the same one every time, and not add a third.
    Sorting the names is what makes the choice stable, and because the stamp
    is fixed-width and zero-padded, sorted order is chronological: the
    earliest copy wins, on every platform.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    for stamp in ("20260805T095752Z", "20260805T095751Z", "20260805T095753Z"):
        (archive / f"f-{stamp}-{digest}.md").write_bytes(b"same")
    picked = {pi.preserve(source, archive).name for _ in range(3)}
    assert picked == {f"f-20260805T095751Z-{digest}.md"}
    assert len(list(archive.iterdir())) == 3


def test_the_stamp_pattern_matches_what_archive_name_writes():
    """The two halves of the naming scheme, pinned against each other.

    ``archive_name`` writes the stamp with ``strftime`` and
    ``existing_archive`` recognises it with a regex. Nothing else makes them
    agree, and if they stopped agreeing every archive would look unrecognised
    and reuse would silently stop working -- which is the bug this whole
    change is about, returned in a new spelling.
    """
    digest = "0" * 64
    built = pi.archive_name("f.md", digest, datetime(
        2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc))
    middle = built[len("f-"):-len(f"-{digest[:12]}.md")]
    assert pi._STAMP.fullmatch(middle)
    assert middle == "20261231T235959Z"


def test_sixteen_characters_of_anything_is_not_a_stamp(tmp_path):
    """Length alone is not the shape, and getting this wrong fails a preserve.

    A file whose middle is sixteen arbitrary characters would be taken for an
    archive, read back, and -- since it is not one -- refused. That refuses
    the whole preserve, so a stray file in the archive directory would stop
    the retire it was unrelated to. Sixteen was what the first version
    checked; a reviewer pointed out that it is not the same question.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    reference = pi.archive_name("f.md", "0" * 64, datetime(
        2026, 8, 5, 9, 57, 51, tzinfo=timezone.utc))
    stamp_length = len(reference) - len("f-") - len("-000000000000.md")
    middle = "notatimestamp!!!"
    assert len(middle) == stamp_length
    stray = archive / f"f-{middle}-{digest}.md"
    stray.write_bytes(b"nothing like the source")
    landed = pi.preserve(source, archive)
    assert landed != stray
    assert landed.read_bytes() == b"same"
    assert stray.read_bytes() == b"nothing like the source"


def test_a_name_with_no_room_for_a_stamp_is_not_an_archive(tmp_path):
    """Both ends can match at once when there is nothing between them.

    ``f-0967115f2813.md`` starts with the stem and ends with the digest and
    suffix, sharing characters between the two. There is no stamp in it, so
    it is not an archive -- and reading it back would refuse a preserve it
    has nothing to do with.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    stray = archive / f"f-{digest}.md"
    stray.write_bytes(b"nothing like the source")
    landed = pi.preserve(source, archive)
    assert landed != stray
    assert landed.read_bytes() == b"same"
    assert stray.read_bytes() == b"nothing like the source"


def test_an_unrelated_file_ending_the_same_way_is_not_an_archive(tmp_path):
    """The middle of the name has to be stamp-shaped to count.

    Matching on the two ends alone would accept any file that happens to
    start with the stem and end with the digest, and hand it back unread.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()
    digest = hashlib.sha256(b"same").hexdigest()[:12]
    impostor = archive / f"f-notatimestamp-{digest}.md"
    impostor.write_bytes(b"nothing like the source")
    landed = pi.preserve(source, archive)
    assert landed != impostor
    assert landed.read_bytes() == b"same"
    assert impostor.read_bytes() == b"nothing like the source"


def test_an_archive_directory_that_cannot_be_listed_is_an_error(tmp_path):
    """Not being able to look is not the same answer as nothing being there.

    Treating a failed listing as "no existing copy" would write a second
    archive of bytes already kept, and on a directory that stayed unreadable
    it would keep doing so on every run. The denial is injected rather than
    staged on disk because the errno a blocked listing produces differs by
    platform, and a test that passes through a different branch on Windows
    than on Linux is not evidence about either.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "retired"
    archive.mkdir()

    def denied(self):
        raise PermissionError(13, "Permission denied")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "iterdir", denied)
        with pytest.raises(InstructionsError) as caught:
            pi.preserve(source, archive)
    assert "Nothing was removed" in str(caught.value)


def test_an_absent_archive_directory_is_not_an_error(tmp_path):
    """The control for the test above: absent really is 'nothing there'.

    Every first preserve hits this, so mapping FileNotFoundError onto the
    refusal above would break the ordinary path rather than a rare one.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"same")
    archive = tmp_path / "never-created-yet"
    assert not archive.exists()
    landed = pi.preserve(source, archive)
    assert landed.read_bytes() == b"same"


def test_preserving_an_unreadable_source_raises(tmp_path):
    with pytest.raises(InstructionsError):
        pi.preserve(tmp_path / "missing.md", tmp_path / "retired")


def test_preserving_verifies_the_copy_by_reading_it_back(tmp_path, monkeypatch):
    """The read-back is what makes the promise a promise.

    Its only caller unlinks the original next, so a copy that was written and
    never checked is the same as no copy at all -- it just reads better in a
    log. Simulated here by corrupting the file between the write and the
    check, which is what a short write or a lying filesystem looks like from
    the outside.
    """
    source = tmp_path / "f.md"
    source.write_bytes(b"important")
    archive = tmp_path / "retired"
    real_digest = install_manifest.file_digest

    def corrupt(path):
        Path(path).write_bytes(b"truncated")
        monkeypatch.setattr(install_manifest, "file_digest", real_digest)
        return real_digest(path)

    monkeypatch.setattr(pi, "file_digest", corrupt)
    with pytest.raises(InstructionsError):
        pi.preserve(source, archive)


def test_user_scope_agents_files_are_found_and_never_touched(tmp_path):
    (tmp_path / ".copilot").mkdir()
    globals_ = tmp_path / ".copilot" / AGENTS_NAME
    globals_.write_text("theirs", encoding="utf-8")
    found = pi.user_scope_agents_files(tmp_path)
    assert found == [globals_]
    assert globals_.read_text(encoding="utf-8") == "theirs"


def test_no_user_scope_agents_file_is_no_finding(tmp_path):
    assert pi.user_scope_agents_files(tmp_path) == []


# ---------------------------------------------------------------------------
# Choosing where the conventions come from
# ---------------------------------------------------------------------------

@pytest.fixture
def source_pair(tmp_path):
    template = tmp_path / "templates" / "copilot-instructions.md"
    template.parent.mkdir(parents=True)
    template.write_text("# repository template\n", encoding="utf-8")
    deployed = tmp_path / "copilot" / "copilot-instructions.md"
    deployed.parent.mkdir(parents=True)
    return template, deployed


def test_an_untouched_deployment_takes_the_repository_wording(source_pair):
    template, deployed = source_pair
    deployed.write_text("# repository template\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest=install_manifest.file_digest(template))
    text, origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# repository template\n"
    assert "repository template" in origin


def test_an_edited_deployment_wins(source_pair):
    """The user's edits are the conventions this machine actually follows.

    Generating every project's ``AGENTS.md`` from the pristine template would
    delete a year of edits from the machine in the same operation that deletes
    the file holding them. The predicate is the manifest's -- the same one
    ``install_templates`` uses to decide it may not overwrite -- because a file
    precious enough not to clobber is precious enough not to discard.
    """
    template, deployed = source_pair
    deployed.write_text("# MY EDITS\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest="0" * 64)
    install_manifest.entry(manifest, pi.TEMPLATE_KEY)["sha256"] = "0" * 64
    text, origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# MY EDITS\n"
    assert "local edits" in origin


def test_an_untracked_deployment_wins(source_pair):
    """A machine that predates the manifest. Its file is precious by default."""
    template, deployed = source_pair
    deployed.write_text("# from an older setup\n", encoding="utf-8")
    text, _origin = pi.resolve_source(template, deployed,
                                      install_manifest.empty_manifest())
    assert text == "# from an older setup\n"


def test_a_missing_template_falls_back_to_the_deployed_copy(source_pair):
    template, deployed = source_pair
    template.unlink()
    deployed.write_text("# deployed\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest=install_manifest.file_digest(deployed))
    text, _origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# deployed\n"


def test_no_source_at_all_raises(source_pair):
    template, deployed = source_pair
    template.unlink()
    with pytest.raises(InstructionsError):
        pi.resolve_source(template, deployed, install_manifest.empty_manifest())


def test_an_empty_source_is_not_a_source(source_pair):
    """An empty template would render an ``AGENTS.md`` with a header and no
    conventions in it, into every repository, and then delete the file that
    had them."""
    template, deployed = source_pair
    template.write_text("", encoding="utf-8")
    deployed.write_text("# deployed\n", encoding="utf-8")
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, pi.TEMPLATE_KEY, deployed, kind="template",
                            digest=install_manifest.file_digest(template))
    text, _origin = pi.resolve_source(template, deployed, manifest)
    assert text == "# deployed\n"


# ---------------------------------------------------------------------------
# The retirement
# ---------------------------------------------------------------------------

SOURCE = (
    "# Conventions\n\nintro\n\n"
    "## Always On\n\nprose\n\n"
    f"## {pi.CONFIGURATION_SECTION}\n\nenrollment prose\n\n"
    "## Session Handoff Protocol\n\n"
    "*Enabled by feature flag: `session-handoff`*\n\nhandoff prose\n"
)


@pytest.fixture
def machine(tmp_path):
    """A home with a global instructions file and two registered projects.

    Every project carries a configuration with all the flags on. That is not
    scenery: since FR-8 the flags default *off* and rendering refuses a
    project that never chose, so a fixture without one would exercise the
    refusal in every test rather than the thing each test is about. It also
    keeps the section-visibility assertions below meaningful — a test that
    a disabled feature drops its section proves nothing if the baseline is
    everything-off.
    """
    home = tmp_path / "home"
    copilot = home / ".copilot"
    projects_root = copilot / "projects"
    projects_root.mkdir(parents=True)
    global_path = copilot / "copilot-instructions.md"
    global_path.write_text("the global conventions\n", encoding="utf-8")
    projects = []
    for index, name in enumerate(("alpha", "beta"), 1):
        guid = f"GUID-{index}"
        root = tmp_path / "repos" / name
        root.mkdir(parents=True)
        (projects_root / guid).mkdir()
        (projects_root / guid / project_features.CONFIG_NAME).write_text(
            json.dumps({"version": 1, "features": {
                slug: project_features.ON
                for slug in project_features.SLUGS
                if slug != project_features.TRACKED_BACKLOG}}),
            encoding="utf-8")
        projects.append({"guid": guid, "path": str(root), "label": name})
    return {
        "home": home,
        "global_path": global_path,
        "archive": copilot / pi.ARCHIVE_DIRNAME,
        "projects_root": projects_root,
        "projects": projects,
    }


def _retire(machine, **overrides):
    kwargs = dict(
        source=SOURCE,
        source_origin="a test",
        global_path=machine["global_path"],
        archive_dir=machine["archive"],
        projects_root=machine["projects_root"],
        home=machine["home"],
        version="9.9.9",
    )
    kwargs.update(overrides)
    return pi.retire(machine["projects"], **kwargs)


def test_every_project_gets_a_file_and_the_global_one_is_retired(machine):
    result = _retire(machine)
    assert [o.state for o in result.outcomes] == [WRITTEN, WRITTEN]
    for project in machine["projects"]:
        text = (Path(project["path"]) / AGENTS_NAME).read_text(encoding="utf-8")
        assert MANAGED_BEGIN in text and MANAGED_END in text
        assert "enrollment prose" not in text
    assert result.removed
    assert not machine["global_path"].exists()
    assert result.archived.read_text(encoding="utf-8") == "the global conventions\n"


def test_each_project_gets_only_the_sections_its_own_features_turned_on(machine):
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    config.write_text(json.dumps(
        {"version": 1, "features": {"session-handoff": "off"}}),
        encoding="utf-8")
    _retire(machine)
    alpha = (Path(machine["projects"][0]["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8")
    beta = (Path(machine["projects"][1]["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8")
    assert "handoff prose" not in alpha
    assert "handoff prose" in beta


def test_a_project_that_cannot_be_written_stops_the_removal(machine):
    """The contract this module exists for.

    A removal that went ahead anyway would leave the machine with the
    conventions in no place at all, which is the one failure the ordering is
    chosen to make impossible. The state it settles on instead is a duplicate.
    """
    blocked = Path(machine["projects"][1]["path"]) / AGENTS_NAME
    blocked.mkdir()                      # a directory where the file must go
    result = _retire(machine)
    assert result.blockers
    assert not result.removed
    assert machine["global_path"].exists()
    assert result.archived is None
    # ...and the project that *could* be written still was.
    assert (Path(machine["projects"][0]["path"]) / AGENTS_NAME).is_file()


def test_a_project_directory_that_is_not_here_stops_the_removal(machine):
    """An unplugged drive or an unsynced clone is a project that comes back.

    Removing the global file while it is away opens the gap on a delay.
    """
    machine["projects"].append({"guid": "GUID-3", "label": "gamma",
                                "path": str(machine["home"] / "not-here")})
    result = _retire(machine)
    assert [o.state for o in result.outcomes][-1] == MISSING
    assert not result.removed
    assert machine["global_path"].exists()


def test_allow_missing_is_the_deliberate_override(machine):
    machine["projects"].append({"guid": "GUID-3", "label": "gamma",
                                "path": str(machine["home"] / "not-here")})
    result = _retire(machine, allow_missing=True)
    assert result.removed
    assert not machine["global_path"].exists()


def test_an_existing_agents_file_is_not_touched_without_consent(machine):
    theirs = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    theirs.write_text("# their own conventions\n", encoding="utf-8")
    result = _retire(machine)                       # decide defaults to "no"
    assert result.outcomes[0].state == DECLINED
    assert theirs.read_text(encoding="utf-8") == "# their own conventions\n"
    assert not result.removed
    assert machine["global_path"].exists()


def test_consenting_appends_below_what_was_already_there(machine):
    theirs = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    theirs.write_text("# their own conventions\n", encoding="utf-8")
    result = _retire(machine, decide=lambda project, existing: True)
    assert result.outcomes[0].state == MERGED
    text = theirs.read_text(encoding="utf-8")
    assert text.startswith("# their own conventions\n")
    assert MANAGED_BEGIN in text
    assert result.removed


def test_the_consent_question_is_asked_only_for_files_we_did_not_write(machine):
    """A repository that already carries a managed block has already
    consented; asking again on every regeneration would train the answer."""
    asked = []
    _retire(machine, decide=lambda project, existing: asked.append(project) or True)
    assert asked == []
    second = _retire(machine, decide=lambda p, e: asked.append(p) or True)
    assert asked == []
    assert [o.state for o in second.outcomes] == [UNCHANGED, UNCHANGED]


def test_a_second_run_is_idempotent(machine):
    first = _retire(machine)
    assert first.removed
    before = {p["path"]: (Path(p["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8") for p in machine["projects"]}
    second = _retire(machine)
    assert second.removed
    assert [o.state for o in second.outcomes] == [UNCHANGED, UNCHANGED]
    after = {p["path"]: (Path(p["path"]) / AGENTS_NAME).read_text(
        encoding="utf-8") for p in machine["projects"]}
    assert before == after
    assert len(list(machine["archive"].iterdir())) == 1


# ---------------------------------------------------------------------------
# FR-7 -- a project's own content survives regeneration, byte for byte
#
# `test_a_second_run_is_idempotent` above is a weaker claim than it looks:
# it regenerates the *same* block, so a `_place_one` that ignored the
# existing file entirely and wrote the block alone would still pass it on
# the second run. What FR-7 promises is that content survives a block that
# genuinely changed -- a new toolkit version, a feature turned on, a section
# rewritten -- because that is the only regeneration anyone will ever run.
# These tests drive the whole path (`render` -> `compose` -> the atomic
# write), not `compose` on its own, since every one of those steps is
# somewhere the surrounding bytes could be dropped.
# ---------------------------------------------------------------------------

APPENDED_ABOVE = (
    "# alpha\n"
    "\n"
    "Build: `make check`. Run one test with `pytest -k name`.\n"
    "\n"
)

APPENDED_BELOW = (
    "\n"
    "## Notes we wrote ourselves\n"
    "\n"
    "Trailing whitespace matters here:   \n"
    "So do blank lines.\n"
    "\n"
    "\n"
    "And a tab\tin the middle.\n"
)


def _regenerate_around_project_content(machine, **second_run):
    """Place, append the project's own prose above and below, regenerate.

    Returns the block-stripped remainder before and after, plus the two
    managed blocks, so a caller can assert both halves: what changed, and
    what did not.
    """
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    written = target.read_text(encoding="utf-8")
    target.write_text(APPENDED_ABOVE + written.rstrip("\n") + "\n"
                      + APPENDED_BELOW, encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    result = _retire(machine, **second_run)
    after = target.read_text(encoding="utf-8")
    return before, after, result


def _split_out_the_block(text: str) -> "tuple[str, str, str]":
    """`text` as (above, the managed block, below)."""
    assert text.count(MANAGED_BEGIN) == 1, text
    assert text.count(MANAGED_END) == 1, text
    head, rest = text.split(MANAGED_BEGIN, 1)
    block, tail = rest.split(MANAGED_END, 1)
    return head, block, tail


def test_project_content_survives_a_block_that_actually_changed(machine):
    """The regeneration FR-7 is about: the toolkit version moved on.

    Everything outside the markers is compared byte for byte -- including
    the trailing spaces, the doubled blank line and the tab, because a
    "preserving" implementation that round-trips through a line list and
    rejoins with `\\n` loses exactly those and nothing else, and no test
    written with tidy fixture prose would see it.
    """
    before, after, result = _regenerate_around_project_content(
        machine, version="10.0.0")
    assert [o.state for o in result.outcomes] == [MERGED, MERGED]

    old_head, old_block, old_tail = _split_out_the_block(before)
    new_head, new_block, new_tail = _split_out_the_block(after)

    assert old_block != new_block, (
        "the block did not change, so this test proved only what the "
        "idempotence test already proves")
    assert "9.9.9" in old_block and "10.0.0" in new_block
    assert (new_head, new_tail) == (old_head, old_tail)
    assert new_head == APPENDED_ABOVE
    assert new_tail.endswith(APPENDED_BELOW)


def test_project_content_survives_a_feature_being_turned_off(machine):
    """The other axis: the block shrinks. Content below it must not be
    dragged up with the removed section."""
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    target.write_text(
        APPENDED_ABOVE + target.read_text(encoding="utf-8").rstrip("\n")
        + "\n" + APPENDED_BELOW, encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    config.write_text(json.dumps(
        {"version": 1, "features": {"session-handoff": "off"}}),
        encoding="utf-8")
    _retire(machine)
    after = target.read_text(encoding="utf-8")

    old_head, old_block, old_tail = _split_out_the_block(before)
    new_head, new_block, new_tail = _split_out_the_block(after)
    assert "handoff prose" in old_block
    assert "handoff prose" not in new_block
    assert (new_head, new_tail) == (old_head, old_tail)


def test_appended_content_that_quotes_the_markers_is_not_eaten(machine):
    """A project documenting the managed block writes these lines into a
    fenced sample. Read as live markers, the sample and everything between
    it and the real block is what `compose` replaces -- so the file loses
    the paragraph explaining the very thing that ate it."""
    _retire(machine)
    target = Path(machine["projects"][0]["path"]) / AGENTS_NAME
    documented = (
        target.read_text(encoding="utf-8").rstrip("\n") + "\n"
        + "\n## How the managed block is delimited\n\n"
        + "```markdown\n" + MANAGED_BEGIN + "\n...\n" + MANAGED_END
        + "\n```\n\nKeep your own notes outside it.\n")
    target.write_text(documented, encoding="utf-8")
    _retire(machine, version="10.0.0")
    after = target.read_text(encoding="utf-8")
    assert "Keep your own notes outside it." in after
    assert "## How the managed block is delimited" in after
    assert after.count(MANAGED_BEGIN) == 2
    assert "10.0.0" in after


def test_regeneration_reports_merged_not_written(machine):
    """The state is what a caller logs, and `written` over a file that
    already held someone's prose reads as "we made this file" -- which is
    the claim the whole preservation contract denies."""
    _, _, result = _regenerate_around_project_content(machine,
                                                      version="10.0.0")
    assert result.outcomes[0].state == MERGED
    assert result.outcomes[0].state != WRITTEN


def test_no_projects_at_all_refuses_to_remove_anything(machine):
    result = pi.retire(
        [], source=SOURCE, source_origin="a test",
        global_path=machine["global_path"], archive_dir=machine["archive"],
        projects_root=machine["projects_root"], home=machine["home"],
        version="9.9.9")
    assert not result.removed
    assert machine["global_path"].exists()
    assert result.problems


def test_a_failed_archive_leaves_the_original_alone(machine, monkeypatch):
    """Preservation is not best-effort. If the copy cannot be proven to exist,
    the original is what is left holding the conventions."""
    def refuse(*_a, **_k):
        raise InstructionsError("disk full")

    monkeypatch.setattr(pi, "preserve", refuse)
    result = _retire(machine)
    assert not result.removed
    assert machine["global_path"].exists()
    assert any("disk full" in p for p in result.problems)
    # Every project still got its file: the replacement happens first.
    for project in machine["projects"]:
        assert (Path(project["path"]) / AGENTS_NAME).is_file()


def test_an_unreadable_feature_configuration_fails_that_project(machine):
    """Rendering from invented defaults would write a confident document about
    choices nobody managed to read, and put it in the repository."""
    config = (machine["projects_root"] / "GUID-1"
              / project_features.CONFIG_NAME)
    config.write_text("{ not json", encoding="utf-8")
    result = _retire(machine)
    assert result.outcomes[0].state == FAILED
    assert not (Path(machine["projects"][0]["path"]) / AGENTS_NAME).exists()
    assert not result.removed
    assert machine["global_path"].exists()


def test_a_user_scope_agents_file_is_reported_and_left_alone(machine):
    theirs = machine["home"] / ".copilot" / AGENTS_NAME
    theirs.write_text("theirs\n", encoding="utf-8")
    result = _retire(machine)
    assert result.user_agents == [theirs]
    assert theirs.read_text(encoding="utf-8") == "theirs\n"
    assert result.removed


def test_an_already_absent_global_file_is_not_an_error(machine):
    machine["global_path"].unlink()
    result = _retire(machine)
    assert result.removed
    assert result.archived is None
    assert not result.problems


def test_the_archive_directory_is_never_pruned_by_this_module():
    """A promise, asserted against the source rather than by behaviour.

    ``superseded/`` grows only when a handoff went unread, and this directory
    grows only when a machine's conventions were replaced. Both are symptoms
    to read, not messes to clear -- and a reaper hidden inside the fix for an
    unwanted delete is the same bug wearing the fix's clothes.
    """
    source = (REPO / "project_instructions.py").read_text(encoding="utf-8")
    for destructive in ("rmtree", "shutil.rmtree", "os.removedirs"):
        assert destructive not in source
    unlinks = re.findall(r"\.unlink\(|os\.unlink\(|os\.remove\(", source)
    # One for the interrupted-temp-file cleanup in `write_text_atomic`, one in
    # `preserve`, and one for the global file itself. Anything more is a
    # deletion nobody argued for.
    assert len(unlinks) == 3, f"unexpected deletions: {unlinks}"


# ---------------------------------------------------------------------------
# Fences, markers and labels: the review round
#
# Every test below is a defect three adversarial reviewers found in code that
# already had 55 passing tests over it. They are kept apart from the sections
# above so the next reader can see what the first pass did not think of.
# ---------------------------------------------------------------------------

def test_a_four_tick_fence_is_not_closed_by_an_inner_three_tick_line():
    """CommonMark closes a fence on a run at least as long, not any run.

    A document about writing Markdown -- which this template is -- wraps
    triple-tick examples in a four-tick fence. Reading the inner ``` as the
    close puts the rest of the example back into the document as prose, and
    the examples here carry `## ` headings at column zero, so those fragments
    become sections with no gate marker on them. Content for a feature that
    is *off* is then emitted.
    """
    text = (
        "## Kept\n"
        "````markdown\n"
        "```python\n"
        "x = 1\n"
        "```\n"
        "## Not a heading\n"
        "````\n"
        "tail\n"
    )
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["Kept"]
    assert "## Not a heading" in sections[0].body


def test_the_four_tick_case_would_fail_a_three_character_fence_tracker():
    """The control for the test above.

    A tracker that stored only ``stripped[:3]`` closes on the inner ``` and
    produces a second section. Asserting that here means the test above is
    pinned to the fix rather than to an implementation that never had the bug.
    """
    text = (
        "## Kept\n````\n```\n## Not a heading\n```\n````\n"
    )
    naive = [line for line in text.splitlines() if line.startswith("## ")]
    assert len(naive) == 2, "the input must contain the trap being tested"


def test_a_tilde_fence_is_not_closed_by_backticks():
    text = "## One\n~~~\n```\n## Inside\n```\n~~~\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["One"]


def test_a_closing_run_may_be_longer_than_the_opening_one():
    text = "## One\n```\nbody\n`````\n## Two\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["One", "Two"]


def test_a_fence_line_with_an_info_string_does_not_close_a_fence():
    """```` ```python ```` opens; it never closes."""
    text = "## One\n```\n```python\n## Inside\n```\n## Two\n"
    _preamble, sections = pi.split_sections(text)
    assert [s.title for s in sections] == ["One", "Two"]


def test_the_real_template_still_splits_into_its_gated_sections(template):
    """The fence rewrite must not have changed the document it exists for."""
    _preamble, sections = pi.split_sections(template)
    slugs = [pi.gate_slug(s.body) for s in sections]
    assert set(s for s in slugs if s) == set(project_features.SLUGS)


def test_markers_quoted_in_a_code_sample_are_not_a_managed_block():
    """An AGENTS.md that documents this toolkit is not one it wrote.

    Treating the sample as a live block is not cosmetic: it makes
    ``managed_block_present`` true, which skips the consent prompt, and then
    ``compose`` replaces everything between the two sampled lines. That is
    the user's own file destroyed without being asked.
    """
    existing = (
        "# My conventions\n\n"
        "copilot-tools writes a block delimited like this:\n\n"
        "```\n"
        f"{pi.MANAGED_BEGIN}\n"
        "...generated conventions...\n"
        f"{pi.MANAGED_END}\n"
        "```\n\n"
        "Keep the rest.\n"
    )
    assert pi.managed_block_present(existing) is False
    out = pi.compose(existing, "MANAGED\n")
    assert "...generated conventions..." in out, "the sample was clobbered"
    assert out.startswith(existing.rstrip("\n"))
    assert out.rstrip("\n").endswith("MANAGED")


def test_a_marker_merely_mentioned_in_prose_is_not_a_delimiter():
    existing = (
        f"We do not use the {pi.MANAGED_BEGIN} marker here.\n"
    )
    assert pi.managed_block_present(existing) is False
    out = pi.compose(existing, "MANAGED\n")
    assert existing.rstrip("\n") in out


def test_a_real_marker_pair_is_still_found_and_replaced():
    """The control: the narrowing above must not have switched detection off."""
    existing = (
        "# Mine\n\n"
        f"{pi.MANAGED_BEGIN}\n"
        "OLD\n"
        f"{pi.MANAGED_END}\n\n"
        "# Also mine\n"
    )
    assert pi.managed_block_present(existing) is True
    out = pi.compose(existing, "NEW\n")
    assert "OLD" not in out
    assert "NEW" in out
    assert out.startswith("# Mine\n")
    assert out.endswith("# Also mine\n")


def test_a_marker_line_with_trailing_whitespace_still_counts():
    existing = (
        f"{pi.MANAGED_BEGIN}  \nOLD\n"
        f"  {pi.MANAGED_END}\t\n"
    )
    out = pi.compose(existing, "NEW\n")
    assert "OLD" not in out and "NEW" in out


def test_a_lone_begin_marker_is_refused_rather_than_appended_to():
    existing = f"# Mine\n\n{pi.MANAGED_BEGIN}\nOLD\n"
    with pytest.raises(pi.InstructionsError):
        pi.compose(existing, "NEW\n")


def test_a_windows_path_label_survives_a_posix_basename(tmp_path):
    """``Path("C:\\a\\b").name`` is the whole string on Linux.

    Catalog rows are written in the native form of the machine that made
    them, so a Windows row read on Linux must still label as ``b``. These
    assertions can only *fail* on a POSIX leg — on Windows ``pathlib`` agrees
    with ``ntpath`` for these inputs — which is why the source scan below
    exists as well.
    """
    assert pi._basename(r"C:\repos\my-app") == "my-app"
    assert pi._basename("/home/dev/my-app") == "my-app"
    assert pi._basename("/home/dev/my-app/") == "my-app"
    assert pi._basename(r"C:\repos\my-app\\") == "my-app"


def test_the_label_cases_would_catch_an_os_path_basename():
    """The control for the test above, on this platform's os.path."""
    import posixpath
    assert posixpath.basename(r"C:\repos\my-app") == r"C:\repos\my-app"


def test_no_path_string_is_split_with_the_running_platforms_syntax():
    """A scan, because the behavioural test above is blind on Windows.

    ``os.path`` is an alias for whichever of ``posixpath``/``ntpath`` is
    running, so it is the wrong tool for a string that may name the *other*
    platform's syntax — and every catalog row here is such a string. On
    Windows ``pathlib`` and ``ntpath`` agree, so a green local suite says
    nothing; this fires on every leg.

    It reads the parsed tree rather than the text, because the text also
    contains the *explanation* of why not to do this — a substring scan
    would trip on the docstring that documents the rule.
    """
    source = (Path(__file__).resolve().parent.parent
              / "project_instructions.py").read_text(encoding="utf-8")
    banned = {("os", "path", "basename"), ("os", "path", "dirname"),
              ("os", "path", "split"), ("posixpath", "basename")}
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            parts = _dotted(node)
            if parts in banned:
                found.add(".".join(parts))
    assert not found, (
        f"{sorted(found)} read the running platform's syntax; catalog paths "
        "may be written in the other one. Use ntpath.")
    assert "ntpath" in {alias.name for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.Import)
                        for alias in node.names}


def _dotted(node) -> tuple:
    """``os.path.basename`` -> ``("os", "path", "basename")``; else ``()``."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ()
    parts.append(node.id)
    return tuple(reversed(parts))


def test_the_scan_above_would_notice_the_wrong_spelling():
    """Positive control: a detector that matches nothing reports clean.

    The banned call is fed through the same tree walk, so this fails if the
    walk stops finding anything — which is the way that scan dies silently.
    """
    tree = ast.parse("import os\nname = os.path.basename(p)\n")
    hits = {".".join(_dotted(n)) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)}
    assert "os.path.basename" in hits


def test_the_scan_above_does_not_fire_on_the_portable_spelling():
    """Negative control: ``ntpath.basename`` must pass the same walk."""
    tree = ast.parse("import ntpath\nname = ntpath.basename(p)\n")
    hits = {".".join(_dotted(n)) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)}
    assert hits == {"ntpath.basename"}
