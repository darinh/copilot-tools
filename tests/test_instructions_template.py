"""``templates/copilot-instructions.md`` must agree with the code it documents.

This file is deployed to ``~/.copilot/copilot-instructions.md`` and is read by
every agent on the machine at the start of every session. It is the one
artifact here whose *reader* is a language model, which is exactly why it can
rot without anyone noticing: a human skims a stale command line and retypes the
right one from memory, while an agent runs what it was told to run. A flag that
was renamed in ``handoff.sh`` a month ago costs one failed command in a human's
terminal and a whole session's continuity for an agent that hits it while
writing its handoff.

Nothing here checks prose, and nothing here checks that a section merely
exists. Each test pins a claim a shipped document makes about *behaviour
elsewhere* to the code that implements it:

* every ``operator`` command it spells, against the dispatcher's own vocabulary
* the ``operator send`` / ``reply`` flags, against ``copilot_operator.SEND_FLAGS``
* the handoff and restart-marker paths, against the functions that write them
* its gates, against ``project_features``
* the generated header's feature list, against the sections that survived gating

The document is allowed to say more than the code does. It is not allowed to
say something the code will not do.

It is also checked for what it must *not* say. Roughly 85% of this block was
deleted -- the pasted SQL, the hand-written handoff fallback, the catalog
format -- because each had become a command, and a procedure documented beside
the tool that performs it is a second implementation that cannot be kept in
step. A rule removed from a document is enforced by nothing unless something
watches the hole, so several tests here assert an absence and each one is
paired with a control proving the detector can fire.

The corresponding checks did not disappear with the prose. They moved to where
their subject went: the claim protocol to ``tests/test_work_cli.py`` and
``tests/test_work_claims.py``, the session log to ``tests/test_session_cli.py``,
the handoff file's layout to ``tests/test_handoff.py``. Deleting a rule and its
check in one commit is the failure this arrangement exists to prevent.

Known gap, stated rather than faked: the ``operator --loop --headless --name X
--agent Y`` launch line is not pinned. That parser has an ``else`` branch which
forwards anything it does not recognise to the Copilot CLI, so a renamed flag
fails silently rather than loudly -- and there is no seam to check it against
short of launching a session. A grep for the flag literals would look like
coverage and prove nothing, so it is not here.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

import backlog_tool
import copilot_operator
import handoff_tool
import project_features
import project_instructions as pi

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "copilot-instructions.md"
HANDOFF_SH = REPO / "handoff.sh"
OPERATOR_DOC = REPO / "docs" / "operator.md"
PEER_SKILL = REPO / "skills" / "peer-agents" / "SKILL.md"

# A fence may be indented, because several of them sit inside numbered lists.
# Matching only column zero loses every ``sql`` block in the parallel-agent
# protocol -- and loses it silently, as an empty list of blocks to check.
_FENCE_LINE = re.compile(r"^(?P<indent>[ \t]*)```(?P<info>[^\n]*)$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rendered(template) -> str:
    """The block as an agent receives it, every feature on.

    Several claims in this file are about the *generated* text and not the
    template: `render()` replaces the preamble and the whole enrollment
    section, so a check run against the template would be reading words that
    never reach a reader. Every feature is on because that is the only
    rendering in which every gated section is present to be checked -- and it
    is also the rendering the word budget binds on.
    """
    values = dict(project_features.resolved_values(None))
    for feature in project_features.FEATURES:
        values[feature.slug] = (feature.options[0].value
                                if len(feature.options) > 2 else "on")
    return pi.render(
        source=template, values=values, guid="0" * 8, project_path="/p",
        label="p", project_dir_path="/d", config_path="/d/features.json",
        version="0.0.0", platform=pi.WINDOWS)


_HEADING = re.compile(r"^(?P<hashes>#+) \S")


def _section(text: str, heading: str, level: int = 2) -> str:
    """The body of a heading, up to the next heading of that level or higher.

    Fence-aware, and that is not a refinement: the handoff section *shows* a
    file whose own headings are ``## Status`` and ``## Next Steps``. A plain
    split on ``^## `` ends the section in the middle of the example, so the
    very block this module exists to check falls outside the range it
    searches -- and the failure mode is an empty search, which reads as
    agreement.

    Scoping to a section is likewise load-bearing: the handoff command line and
    the operator command line are both fenced blocks full of ``--``-shaped
    tokens, and only position tells them apart.

    A duplicate heading is an error rather than a tie broken by position. The
    dangerous input for a parser like this is not a malformed document, it is
    a second *well-formed* thing that also matches: taking the first one
    leaves the check green while it guards a section nobody maintains.
    """
    marker = "#" * level + " " + heading
    lines, fenced, starts = text.splitlines(), False, []
    for i, line in enumerate(lines):
        if _FENCE_LINE.match(line):
            fenced = not fenced
        elif not fenced and line.strip() == marker:
            starts.append(i)
    assert len(starts) == 1, (
        f"expected exactly one '{marker}' heading in {TEMPLATE.name}, "
        f"found {len(starts)}"
    )

    fenced = False
    for i in range(starts[0] + 1, len(lines)):
        if _FENCE_LINE.match(lines[i]):
            fenced = not fenced
            continue
        match = _HEADING.match(lines[i])
        if not fenced and match and len(match.group("hashes")) <= level:
            return "\n".join(lines[starts[0] + 1:i])
    return "\n".join(lines[starts[0] + 1:])


def _blocks(body: str, info: str | None = None) -> list[str]:
    """Fenced blocks in ``body``, dedented, optionally filtered by info string."""
    out: list[str] = []
    open_info: str | None = None
    indent, buf = "", []
    for line in body.splitlines():
        match = _FENCE_LINE.match(line)
        if match and open_info is None:
            open_info, indent, buf = match.group("info").strip(), match.group("indent"), []
        elif match:
            if info is None or open_info == info:
                out.append("".join(
                    (line[len(indent):] if line.startswith(indent) else line.lstrip())
                    + "\n" for line in buf))
            open_info = None
        elif open_info is not None:
            buf.append(line)
    # An unclosed fence swallows the rest of the section into a block nobody
    # asked for, or drops it entirely. Either way the checks below go quiet.
    assert open_info is None, f"unclosed ``` fence (info string {open_info!r})"
    return out


def _inline_code(body: str) -> list[str]:
    """Single-backtick spans, which is where some documented SQL lives."""
    return [span.strip() for span in _INLINE_CODE.findall(body)]


# --------------------------------------------------------------------------
# The operator commands the document tells agents to run
# --------------------------------------------------------------------------
#
# This replaced four tests that pinned the literal `handoff --instance ...`
# line, and two that pinned `operator send` / `operator reply` flags. The
# document no longer spells any of those: writing a handoff is
# `operator session end`, and the procedures the flags belonged to moved into
# the tool and the skills (D10 -- the rule left the block in the same commit
# its check arrived).
#
# What replaces them is deliberately not six more literal-string tests. Those
# went stale in exactly one way: the document changed and the test kept
# passing against a command nobody typed any more. This one reads whatever
# `operator ...` the document currently spells and measures every one against
# the vocabulary the dispatcher actually uses, so it cannot be outlived by an
# edit to the document.
#
# It has already earned that: the first draft of the new block documented
# `operator work request|list|end`, and `end` is not a work verb. A literal
# test for the old commands would have passed while shipping it.

#: Groups with a closed verb vocabulary, read out of the dispatcher rather
#: than restated so this table cannot drift into a third copy.
#:
#: Groups absent from here take arguments, not verbs -- `operator inbox NAME`,
#: `operator send --from ...` -- so there is nothing to check the word after
#: them against. `test_every_group_with_a_verb_vocabulary_is_in_the_table`
#: below is what stops a *new* verb-taking group from landing in that silent
#: majority.
_VERB_SOURCES = {
    "session": lambda: copilot_operator.SESSION_VERBS,
    "work": lambda: copilot_operator.WORK_VERBS,
    "worktree": lambda: copilot_operator.WORKTREE_VERBS,
    "ownership": lambda: copilot_operator.OWNERSHIP_VERBS,
    "backlog": lambda: tuple(
        backlog_tool.build_parser()._subparsers._group_actions[0].choices),
}

#: Every document shipped to agents that spells `operator` commands.
#:
#: The block is not the only one any more, and that is the point of moving
#: procedure into skills: the commands went with it. A check that still read
#: only the template would report the whole corpus clean while a skill named a
#: verb that had been renamed.
_COMMAND_DOCS = [TEMPLATE] + sorted((REPO / "skills").glob("*/SKILL.md"))


def _documented_operator_commands(text: str) -> set[tuple[str, str]]:
    """``(group, verb)`` for every ``operator ...`` span in a document.

    Alternations are expanded, because the document writes
    ``operator session start|end`` and both halves are a command an agent will
    type. Reading only the first would leave the second unchecked while the
    test still reported the line as covered.
    """
    found: set[tuple[str, str]] = set()
    for span in _INLINE_CODE.findall(text):
        words = span.split()
        if not words or words[0] != "operator" or len(words) < 2:
            continue
        group = words[1]
        verbs = words[2].split("|") if len(words) > 2 else [""]
        for verb in verbs:
            # Only the slot immediately after the group can be a verb, and a
            # flag there means the group takes arguments instead.
            found.add((group, "" if verb.startswith("-") else verb))
    return found


@pytest.mark.parametrize(
    "path", _COMMAND_DOCS, ids=lambda p: p.parent.name + "/" + p.name)
def test_documents_only_name_operator_commands_that_exist(path):
    """Every ``operator`` command shipped to an agent is one the dispatcher answers to.

    An agent runs what it was told to run. A group or verb that does not exist
    costs it the command and, for `operator session end`, the whole session's
    continuity -- which is the failure this file was written for.

    This replaced four tests pinning the literal ``handoff --instance ...``
    line and two pinning ``operator send`` / ``operator reply`` flags. Those
    could go stale in one direction only: the document changes and the test
    keeps passing against a command nobody types any more. This one reads
    whatever the document currently spells, so an edit cannot outlive it.

    It has already earned that. The first draft of the new block documented
    ``operator work request|list|end``, and ``end`` is not a work verb; every
    literal test for the old commands passed on it.
    """
    documented = _documented_operator_commands(
        path.read_text(encoding="utf-8"))
    if path == TEMPLATE:
        assert documented, (
            "parsed no `operator ...` commands out of the block. It is now "
            "written around them, so finding none means this parser stopped "
            "matching, not that the document stopped naming them."
        )
    unknown = sorted({group for group, _ in documented}
                     - set(copilot_operator.SUBCOMMANDS))
    assert not unknown, (
        f"{path.name} names operator subcommands that copilot_operator does "
        "not dispatch:\n  " + "\n  ".join(unknown))

    bad = []
    for group, verb in sorted(documented):
        if group not in _VERB_SOURCES or not verb:
            continue
        verbs = _VERB_SOURCES[group]()
        if verb not in verbs:
            bad.append(f"operator {group} {verb} "
                       f"(accepted: {', '.join(verbs)})")
    assert not bad, (
        f"{path.name} tells agents to run commands the tool refuses with "
        "'Unknown subcommand':\n  " + "\n  ".join(bad))


def test_the_command_parser_expands_alternations_and_ignores_prose():
    """A control for the parser above, which is the only thing checking these documents.

    Both directions matter. If it stopped expanding ``a|b`` it would check the
    first verb and silently pass the second; if it started matching ordinary
    backticked prose it would report failures for words nobody typed.
    """
    found = _documented_operator_commands(
        "run `operator session start|end` after `operator projects`, but not "
        "`operator` alone, not `git worktree list`, and `operator work "
        "request --instance x` takes a flag not a verb")
    assert ("session", "start") in found
    assert ("session", "end") in found
    assert ("projects", "") in found
    assert ("work", "request") in found
    assert not any(group == "worktree" and verb == "list"
                   for group, verb in found), (
        "matched a `git worktree` span as an operator one")
    assert not any(verb.startswith("-") for _, verb in found)


def test_the_parser_would_report_a_command_that_does_not_exist():
    """The control that matters most: the check can fail.

    A parser that quietly matched nothing would report every document clean,
    which reads exactly like coverage. This is the shape that caught the real
    defect -- a plausible verb the tool does not accept.
    """
    found = _documented_operator_commands("`operator work request|list|end`")
    assert ("work", "end") in found
    assert "end" not in copilot_operator.WORK_VERBS


def test_every_group_with_a_verb_vocabulary_is_in_the_table():
    """``_VERB_SOURCES`` must not fall behind the dispatcher.

    Groups missing from the table are treated as taking arguments rather than
    verbs, and are therefore *not* checked. That silence is correct for
    ``inbox`` and wrong for a new verb-taking group -- and a new group is
    exactly when a freshly written command line is most likely to be wrong.
    Every ``*_VERBS`` tuple in the dispatcher must therefore be reachable
    from here.
    """
    declared = {name[:-len("_VERBS")].lower()
                for name in dir(copilot_operator)
                if name.endswith("_VERBS") and name.isupper()}
    missing = sorted(declared - set(_VERB_SOURCES))
    assert not missing, (
        "copilot_operator declares a verb vocabulary with no _VERB_SOURCES "
        "entry, so its verbs are documented and unchecked:\n  "
        + "\n  ".join(missing))
    for group, source in _VERB_SOURCES.items():
        assert group in copilot_operator.SUBCOMMANDS, (
            f"_VERB_SOURCES names '{group}', which the dispatcher does not "
            "answer to -- the table has outlived the command")
        assert source(), f"_VERB_SOURCES['{group}'] resolves to no verbs"


def test_every_skill_the_block_points_at_exists():
    """A pointer to a skill that is not installed is worse than no pointer.

    The block now buys its size by naming skills instead of restating them,
    which makes every one of those names load-bearing: an agent told to read
    the `worktrees` skill and finding none has been handed a dead end at the
    moment it stopped being told the procedure.
    """
    shipped = {p.parent.name for p in (REPO / "skills").glob("*/SKILL.md")}
    assert shipped, "no skills found -- this test would pass vacuously"
    named = {
        name for name in _INLINE_CODE.findall(
            TEMPLATE.read_text(encoding="utf-8"))
        if re.fullmatch(r"[a-z][a-z0-9-]*", name or "")
    } & shipped
    assert named, (
        "the block names no shipped skill at all, so this test is checking "
        "nothing. Either the pointers went or this parser stopped matching.")
    referenced = re.findall(
        r"`([a-z][a-z0-9-]*)` skill", TEMPLATE.read_text(encoding="utf-8"))
    missing = sorted(set(referenced) - shipped)
    assert not missing, (
        "the block points at skills that are not shipped in skills/:\n  "
        + "\n  ".join(missing))



# --------------------------------------------------------------------------
# The handoff file: where it lives and what is in it
# --------------------------------------------------------------------------
#
# These used to read the block's Session Handoff Protocol section, which
# showed the file's markdown layout, its path, and the banked slot. The block
# no longer shows any of it: writing a handoff is `operator session end`, and
# a layout an agent never has to type is not a rule it can break (FR-6).
#
# Deleting the checks with the prose would have been the D10 failure in
# miniature, so each one follows its claim to the document that still makes
# it. The paths are documented in `docs/operator.md`, which is where an agent
# is sent to look. The *layout* is not documented anywhere any more -- and it
# is the one that needed a document least, because the reader is the tool:
# `tests/test_handoff.py` pins `render()` against the parser that consumes it,
# at the seam where a drift actually costs something.


def _headings(markdown: str) -> list[str]:
    return [
        line.strip() for line in markdown.splitlines()
        if re.match(r"^#{1,2} \S", line)
    ]


def test_the_documented_handoff_path_is_the_one_the_tool_writes():
    """Where agents are sent to look must be where the tool writes.

    Documentation naming the wrong path is worse than none: the agent finds
    nothing, concludes its predecessor left nothing, and starts over.

    Built from the function the tool writes through, not from a retyped
    literal: a check against a hand-copied path is a check that the document
    agrees with the test author, which is the agreement least likely to be
    the one that breaks.
    """
    doc = OPERATOR_DOC.read_text(encoding="utf-8")
    written = handoff_tool.handoff_path(
        handoff_tool.project_dir("{guid}"), "{instance}")
    documented = "~/" + written.relative_to(Path.home()).as_posix()
    # The doc writes the placeholders in its own metasyntax, so compare the
    # shape rather than the literal: what must not drift is the directory
    # chain, which is the part the tool computes.
    shape = re.sub(r"\{[a-z]+\}", "[^/`]+", re.escape(documented)
                   .replace(r"\{", "{").replace(r"\}", "}"))
    assert re.search(shape, doc), (
        f"docs/operator.md never says where handoffs live; expected a path "
        f"shaped like {documented!r}")


def test_the_documented_banked_slot_is_the_one_the_tool_writes():
    """The other half of the same claim, and it rots separately.

    The path above is informational; this one is what the agent is told to go
    and read when a handoff warns it. Documentation that kept the old
    ``superseded/`` name here while the path above was updated would leave a
    green test guarding a directory that no longer exists -- which is exactly
    what this file's predecessor did.
    """
    doc = OPERATOR_DOC.read_text(encoding="utf-8")
    slot = handoff_tool.PREV_SUFFIX.lstrip(".")
    assert slot in doc, (
        "docs/operator.md never names the banked slot; expected a mention of "
        f"{handoff_tool.PREV_SUFFIX!r}")

    naming = [para for para in doc.split("\n\n") if slot in para]
    assert any(re.search(r"\bread\b|\bpick(ing|ed)? up\b", para, re.IGNORECASE)
               for para in naming), (
        f"docs/operator.md names {handoff_tool.PREV_SUFFIX!r} but never says "
        "to go and read it. The banked file is the tool's only way to hand "
        "back context a session dropped; documentation that mentions the slot "
        "without saying to open it leaves the file written and unread:\n  "
        + "\n  ".join(naming))


def test_the_block_no_longer_documents_a_hand_written_handoff():
    """The deletion has to stay deleted, and that is a check like any other.

    The section this replaced showed a manual fallback -- write
    ``next-session.md`` yourself, then ``touch`` the restart marker. It is the
    single most expensive thing an agent can be told to do here: a hand-written
    handoff overwrites an unread one, and the tool's preserve-then-publish step
    is the only thing that stops it.

    A rule deleted from the block is a rule nothing enforces, unless something
    watches the hole. This watches the hole.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    for banned in ("next-session.md", "restart/", "New-Item", "touch ~"):
        assert banned not in text, (
            f"the block spells {banned!r} again. Writing the handoff file or "
            "the restart marker by hand is what `operator session end` exists "
            "to prevent; documenting the path is documenting the workaround.")

# --------------------------------------------------------------------------
# The operator command lines
# --------------------------------------------------------------------------

def _long_flags(line: str) -> set[str]:
    """The long options a documented command line passes.

    ``--`` ends the options and is not one: the peer-agents skill documents
    ``operator send --from a --to b -- "--dash-leading text"`` precisely
    because a message may begin with a dash, and a reader that counted the
    separator would report the document as passing a flag no tool accepts.
    Everything after it is an argument however it is spelled.

    ``posix=False`` keeps the quotes on quoted values, which is what makes a
    flag distinguishable from a value that looks like one: under
    ``posix=True`` shlex strips them and ``--status "--not-a-flag"`` reports
    two flags.
    """
    flags: set[str] = set()
    for tok in shlex.split(line, posix=False):
        if tok == "--":
            break
        if tok.startswith("--"):
            flags.add(tok.split("=")[0])
    return flags


def test_the_flag_reader_stops_at_the_end_of_options_separator():
    """A control, because the helper above is what every flag check sees through.

    Both halves matter: a reader that swallowed ``--`` would report a
    non-existent flag on a line that is correct, and one that kept reading
    past it would treat a dash-leading *message* as a flag -- the exact case
    the separator is documented for.
    """
    assert _long_flags('operator send --from a --to b "hi"') == {"--from", "--to"}
    assert _long_flags(
        'operator send --from a --to b -- "--dash-leading text"'
    ) == {"--from", "--to"}
    assert _long_flags('handoff --status "--not-a-flag"') == {"--status"}
    assert _long_flags("operator work request --instance=x") == {"--instance"}


def test_documented_operator_send_flags_are_accepted():
    """The examples moved to the peer-agents skill; the check moved with them.

    Retargeting rather than deleting is the whole point of D10. The claim --
    "these flags exist and these are required" -- is still made, just in
    another document shipped to the same reader. A check left pointed at the
    document that stopped making it would have passed by finding nothing.
    """
    section = PEER_SKILL.read_text(encoding="utf-8")
    sends = [
        line.strip()
        for block in _blocks(section, info="bash")
        for line in block.splitlines()
        if line.strip().startswith("operator send ")
    ]
    assert sends, f"no 'operator send' example found in {PEER_SKILL.name}"
    for line in sends:
        # Each example is checked on its own. Pooling the flags of every
        # example would let two individually broken lines cover for each
        # other -- one passing only --from, the next only --to -- and the
        # union satisfies a requirement that neither line meets.
        documented = _long_flags(line)
        unknown = sorted(documented - set(copilot_operator.SEND_FLAGS))
        assert not unknown, (
            "this documented line passes flags that copilot_operator."
            f"SEND_FLAGS does not list: {unknown}\n  {line}"
        )
        assert {"--from", "--to"} <= documented, (
            "operator send requires --from and --to, but this documented "
            f"line passes only {sorted(documented)}:\n  {line}"
        )


def test_documented_operator_reply_flags_are_accepted():
    """The same check for `reply`, which the peer section now leads with.

    Worth its own test rather than a parametrisation of the one above: the two
    commands have different required flags, and `reply` is the one the
    document tells an agent to run *from a hint it was handed*, so a wrong
    flag here is followed rather than read.
    """
    section = PEER_SKILL.read_text(encoding="utf-8")
    replies = [
        line.strip()
        for block in _blocks(section, info="bash")
        for line in block.splitlines()
        if line.strip().startswith("operator reply ")
    ]
    assert replies, f"no 'operator reply' example found in {PEER_SKILL.name}"
    for line in replies:
        documented = _long_flags(line)
        unknown = sorted(documented - set(copilot_operator.REPLY_FLAGS))
        assert not unknown, (
            "this documented line passes flags that copilot_operator."
            f"REPLY_FLAGS does not list: {unknown}\n  {line}"
        )
        assert "--instance" in documented, (
            "operator reply cannot tell who is replying without --instance "
            "(or $OPERATOR_INSTANCE, which nothing in this system sets), so "
            f"a documented example must name it:\n  {line}"
        )


# --------------------------------------------------------------------------
# The procedures that left the block, and the hole they left behind
# --------------------------------------------------------------------------
#
# Four tests here used to *execute* the SQL the block told agents to paste --
# the `session_log` schema and the `todo_claims` claim/release protocol -- on
# the grounds that documented SQL an agent will run verbatim has to run. That
# was the right check for a document that shipped SQL.
#
# It does not ship SQL any more. `operator session start|end` and
# `operator work request|release|list` do that work in code, and their tests
# are `tests/test_session_cli.py`, `tests/test_work_cli.py` and
# `tests/test_work_claims.py` -- which check the same protocol at a seam
# where a defect costs a claim rather than a paragraph.
#
# The deletion was worth more than a shorter document. The block's pasted
# `session_log` schema was a *second, conflicting* definition of a table
# `operator_session.SESSION_SCHEMA` already owned: same name, different
# columns, and nothing to notice they disagreed. Two lists that disagree is
# the failure the block itself warns about, and it was doing it.
#
# What replaces those four is the check that the deletion stays deleted.
# A rule removed from a document is enforced by nothing unless something
# watches the hole.

_SQL_TELL = re.compile(
    r"\b(CREATE TABLE|INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM|BEGIN IMMEDIATE)\b",
    re.IGNORECASE)


def test_the_block_tells_nobody_to_paste_sql(template):
    """SQL in the block is a protocol with two owners and one of them is prose.

    Every statement it used to carry now has a command. Putting one back means
    an agent hand-writing a transaction against a schema the tool also writes
    -- which is how the block came to define `session_log` a second time, with
    different columns, and no way for either copy to notice.
    """
    hits = _SQL_TELL.findall(template)
    assert not hits, (
        "the block spells SQL again: " + ", ".join(sorted(set(hits))) + ".\n"
        "Whatever it does has a command -- `operator session start|end`, "
        "`operator work request|release|list`, `operator backlog ready`. If it "
        "genuinely has none, the missing command is the change to make, not "
        "the paragraph.")


def test_the_sql_detector_would_fire(template):
    """The control, because a detector that matches nothing reports clean.

    This is the exact shape the repository's own instructions call out: a
    guard that cannot fire reads identically to coverage. The positive case
    uses the real deleted text, so a rewrite of the pattern that stopped
    matching the thing it was written for fails here rather than in silence.
    """
    deleted = (
        "CREATE TABLE IF NOT EXISTS session_log (id INTEGER PRIMARY KEY);\n"
        "BEGIN IMMEDIATE;\n"
        "INSERT INTO todo_claims (todo_id, agent_id) VALUES ('a', 'b');\n"
        "UPDATE todos SET status = 'in_progress' WHERE id = 'a';\n"
        "DELETE FROM todo_claims WHERE todo_id = 'a';\n"
        "COMMIT;\n")
    assert len(set(_SQL_TELL.findall(deleted))) == 5
    assert not _SQL_TELL.findall(
        "update the spec, insert a section, and create a branch"), (
        "the detector matches ordinary prose; it would fire on any document")


def test_the_replacement_commands_have_their_own_tests():
    """The other half of D10: the check has to exist somewhere.

    Naming the files rather than describing them, because "it is covered
    elsewhere" is the sentence under which coverage disappears. If one of
    these is renamed, this goes red and somebody has to say where the protocol
    is checked now.
    """
    for name in ("test_session_cli.py", "test_work_cli.py",
                 "test_work_claims.py"):
        path = REPO / "tests" / name
        assert path.is_file(), (
            f"tests/{name} is gone. The claim and session protocols left the "
            "block on the promise that these check them in code; without it "
            "the protocol is documented nowhere and tested nowhere.")


# --------------------------------------------------------------------------
# The catalog and the restart marker
# --------------------------------------------------------------------------

def test_the_generated_block_forbids_writing_the_catalog(rendered):
    """Enrollment is done before an agent ever reads this file.

    The block used to show the catalog's CSV format, and a test here parsed
    the example with the real reader. Showing the format is showing the
    workaround: a hand-written row is how a project acquires a second id and
    splits its state in two. The generated section names the project instead
    and forbids the write, so what needs checking is that the prohibition
    survived generation -- it is the only part of this the agent still reads.
    """
    assert re.search(r"[Dd]o not offer to enroll", rendered), (
        "the generated block no longer forbids enrolling this directory. It "
        "is already registered; an agent that offers costs a duplicate id.")
    assert "catalog" in rendered.lower(), (
        "the prohibition no longer names the catalog, which is the file it is "
        "about")


def test_the_documented_restart_marker_path_is_where_the_operator_looks(
        monkeypatch):
    """Where the marker goes, checked against the code that watches for it.

    The block used to carry a hand-typed fallback for this; it does not any
    anymore, and `test_the_block_no_longer_documents_a_hand_written_handoff`
    keeps it that way. `docs/operator.md` is where the path is written down
    now, so that is where the claim is checked.

    Selected by the ``restart/`` chain rather than by a literal, so a path
    documented into the wrong directory stays in the sample and fails, rather
    than dropping out of it and leaving the test green.
    """
    monkeypatch.delenv("COPILOT_OPERATOR_HOME", raising=False)
    doc = OPERATOR_DOC.read_text(encoding="utf-8")
    watched = copilot_operator.operator_home() / "restart"
    documented = re.findall(r"`(~/\.[a-z]+/[a-z]+/)<id>`", doc)
    assert documented, (
        "docs/operator.md no longer writes the restart marker directory as an "
        "inline path, so nothing here checks it against operator_home()")
    for path in set(documented):
        actual = Path(path).expanduser()
        assert actual == watched, (
            f"docs/operator.md documents markers under {actual}, but the "
            f"operator watches {watched}")


# --------------------------------------------------------------------------
# Feature flags: the gates, and what the generated header says is on
# --------------------------------------------------------------------------
#
# The block used to carry its own table of features, and three tests checked
# the gates against it. The table was in the enrollment section, which
# `render()` replaces wholesale -- so it never shipped to a single agent, and
# the gates were being checked against a second copy of the vocabulary that
# only the template had.
#
# They are checked against `project_features` now, which is the code that
# owns the vocabulary and the code the renderer actually consults. That is
# strictly stronger: the old pairing could be made green by editing either
# side, and this one cannot.

_GATE = re.compile(r"^\*Enabled by feature flag: `(?P<slug>[a-z0-9-]+)`\*\s*$",
                   re.MULTILINE)
_ENABLED_LINE = re.compile(r"Enabled features: (?P<slugs>[^.]+)\.", re.DOTALL)


def _gated(text: str) -> list[str]:
    return _GATE.findall(text)


def _enabled_features_line(text: str) -> set[str]:
    """The slugs in a document's ``Enabled features:`` line."""
    matches = _ENABLED_LINE.findall(text)
    assert len(matches) == 1, (
        "expected exactly one 'Enabled features:' line, found "
        f"{len(matches)}. With more than one there is no way to tell which "
        "one describes the file.")
    return {slug.strip() for slug in re.split(r",\s*", matches[0])
            if slug.strip()}


def test_every_gate_names_a_real_feature(template):
    """A section nobody can turn on, or a flag that turns on nothing.

    Both directions are failures and they look nothing alike. A gate with no
    feature is a section that never ships and nobody notices; a feature with
    no gate is an option in `operator projects` that changes nothing an agent
    reads, which is worse -- it was chosen deliberately.
    """
    gated, known = set(_gated(template)), set(project_features.SLUGS)
    assert gated, "no '*Enabled by feature flag: `x`*' markers found"
    assert not gated - known, (
        "these sections are gated behind flags project_features does not "
        f"offer:\n  " + "\n  ".join(sorted(gated - known)))
    assert not known - gated, (
        "project_features offers flags with no section gated behind them:\n  "
        + "\n  ".join(sorted(known - gated)) + "\n"
        "Enabling one would change nothing an agent reads.")


def test_the_generated_header_lists_exactly_the_sections_that_shipped(rendered):
    """The header's feature list is a claim about the rest of the block.

    It is the first line an agent reads and the only summary it gets. A flag
    named there whose section was gated out sends the agent looking for a
    procedure that is not in the file -- and, being a summary, it is the last
    place anyone checks.
    """
    assert _enabled_features_line(rendered) == set(_gated(rendered)), (
        "the generated 'Enabled features:' line disagrees with the sections "
        "that survived gating:\n"
        f"  claimed:  {sorted(_enabled_features_line(rendered))}\n"
        f"  shipped:  {sorted(set(_gated(rendered)))}")


def test_a_gate_slug_that_names_no_feature_is_reported(template):
    """The control for the gate check, on the real document.

    Written as a mutation of what ships rather than of a synthetic string:
    a control over a hand-made document proves the assertion works, not that
    it is pointed at the file that matters.
    """
    broken = template.replace(
        "*Enabled by feature flag: `spec-driven`*",
        "*Enabled by feature flag: `telepathy`*", 1)
    assert broken != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError, match="does not offer"):
        test_every_gate_names_a_real_feature(broken)


def test_a_second_enabled_features_line_is_refused():
    """A second well-formed list is ambiguous; picking one is a coin toss."""
    with pytest.raises(AssertionError, match="exactly one"):
        _enabled_features_line(
            "Enabled features: telepathy.\n\nEnabled features: spec-driven.")


# --------------------------------------------------------------------------
# Guard the guards
# --------------------------------------------------------------------------

def test_the_document_stays_within_what_these_parsers_can_read(template):
    """State the parsers' preconditions instead of hoping they hold.

    ``_section`` and ``_blocks`` are two-state machines over backtick fences.
    They are wrong about tilde fences, about a fence nested inside a longer
    one, and about a heading hidden in an HTML comment -- and wrong in the
    dangerous direction, because each of those ends a section early and
    leaves the rest of it unexamined while every test still passes.

    Writing a full markdown parser to hold a 600-line document would be the
    wrong trade. Asserting the document does not contain those constructs
    costs three lines and converts a silent fail-open into a failure that
    names the construct and the parser that cannot read it.
    """
    assert "~~~" not in template, (
        "the document now uses tilde fences, which _section and _blocks do "
        "not recognise; they would read the fenced content as prose"
    )
    assert not re.search(r"^[ \t]*````", template, re.MULTILINE), (
        "the document now nests fences with four or more backticks; _blocks "
        "closes on the first ``` and would truncate the block"
    )
    for comment in re.findall(r"<!--.*?-->", template, re.DOTALL):
        assert not re.search(r"^#{1,6} ", comment, re.MULTILINE), (
            "a heading is commented out with HTML; _section does not skip "
            f"HTML comments and would end a section there:\n{comment}"
        )


def test_section_scoping_stops_at_the_next_section():
    text = "## A\n\nalpha\n\n## B\n\nbeta\n"
    assert _section(text, "A").strip() == "alpha"
    assert "beta" not in _section(text, "A")


def test_a_subsection_ends_at_the_next_heading_of_any_higher_level():
    text = "## A\n\n### Inner\n\nalpha\n\n### Other\n\nother\n\n## B\n\nbeta\n"
    assert _section(text, "Inner", level=3).strip() == "alpha"
    text = "## A\n\n### Inner\n\nalpha\n\n## B\n\nbeta\n"
    assert _section(text, "Inner", level=3).strip() == "alpha"


def test_a_second_well_formed_heading_is_refused_not_ranked():
    """The dangerous input is a decoy, not garbage.

    Taking the first of two matching headings leaves every test in this module
    green while it inspects a section nobody maintains -- it fails open and it
    fails silently, which is the worst combination a guard can have.
    """
    text = "## A\n\nalpha\n\n## A\n\nsecond\n\n## B\n\nbeta\n"
    with pytest.raises(AssertionError, match="exactly one"):
        _section(text, "A")


def test_section_scoping_ignores_headings_inside_fences():
    """The handoff section shows a file whose own headings are ``## ...``."""
    text = "## A\n\n```markdown\n## Status\nshown\n```\n\ntail\n\n## B\n\nbeta\n"
    body = _section(text, "A")
    assert "shown" in body and "tail" in body
    assert "beta" not in body


def test_a_heading_inside_a_fence_is_not_a_duplicate():
    """A fenced example naming the same heading must not count as a second one."""
    text = "## A\n\n```markdown\n## A\nshown\n```\n\n## B\n\nbeta\n"
    assert "shown" in _section(text, "A")


def test_fence_filtering_is_by_info_string():
    body = "```sql\nSELECT 1;\n```\n\n```bash\necho hi\n```\n"
    assert _blocks(body, info="sql") == ["SELECT 1;\n"]
    assert len(_blocks(body)) == 2


def test_indented_fences_are_found_and_dedented():
    """The parallel-agent SQL is indented under numbered list items."""
    body = "1. Do this:\n   ```sql\n   SELECT 1;\n   ```\n"
    assert _blocks(body, info="sql") == ["SELECT 1;\n"]


def test_an_unclosed_fence_is_an_error_not_an_empty_result():
    with pytest.raises(AssertionError, match="unclosed"):
        _blocks("```sql\nSELECT 1;\n")


def test_the_flag_reader_is_still_the_only_flag_reader():
    """``_documented_handoff_flags`` and ``_table_features`` are gone with their subjects.

    Both parsed things the block no longer contains -- a literal ``handoff``
    command line and a markdown table of features -- and a parser kept past
    its subject is worse than none: it is a helper the next person will point
    at something it was not written for. Their checks did not go with them:
    flags are read by ``_long_flags`` (controlled above) and features by
    ``project_features``.
    """
    for retired in ("_documented_handoff_flags", "_table_features"):
        assert retired not in globals(), (
            f"{retired} is back. If a document needs it again, it needs its "
            "controls again too.")


# Eight-four-four-four-twelve hex digits: what uuid.uuid4() prints, and what an
# agent told to "generate a GUID" will recognise as one it may reuse.
_LOOKS_LIKE_A_REAL_GUID = re.compile(
    r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")


def test_no_example_in_the_template_is_a_usable_guid(template):
    """Nothing in the template may look like a GUID somebody could paste.

    The template used to show the format of ``~/.operator/projects/catalog.csv``
    a few lines above an instruction to add an entry to it, and its reader is a
    model that does what it is told. A well-formed value next to a write
    instruction is a value that gets written -- and that file is the user's
    data, mapping every project on the machine, with nothing in this toolkit
    able to rebuild it. It has been destroyed once already.

    The catalog example is gone now and the block forbids the write outright,
    so this looks like a guard without a hazard. It is kept because the hazard
    is the *value*, not the section that carried it: a GUID reintroduced as an
    illustration anywhere in the file is pasteable again, and the section that
    made that obvious is exactly what was removed.

    The rendered block is deliberately not checked. It contains this project's
    real id, which is the whole point of generating it.
    """
    found = _LOOKS_LIKE_A_REAL_GUID.findall(template)
    assert not found, (
        f"{TEMPLATE.name} contains {found!r}, which a reader can paste into a "
        "real catalog. Any example id here must be malformed on purpose.")


def test_the_guid_detector_would_fire():
    """The control. A pattern that matched nothing would clear any document."""
    assert _LOOKS_LIKE_A_REAL_GUID.findall(
        "id: c48add2d-aedc-45e5-a562-946da32753ff")
    assert not _LOOKS_LIKE_A_REAL_GUID.findall("id: `{guid}` or 0000-not-a-guid")
