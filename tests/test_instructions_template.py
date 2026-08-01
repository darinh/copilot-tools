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
exists. Each test pins a claim the document makes about *behaviour elsewhere*
to the code that implements it:

* the ``handoff`` flags it tells agents to type, against both implementations
* the handoff file layout it shows, against ``handoff_tool.render``
* the ``operator send`` flags, against ``copilot_operator.SEND_FLAGS``
* the SQL it tells agents to paste, by running it
* its own feature-flag table, against the gated sections it promises

The document is allowed to say more than the code does. It is not allowed to
say something the code will not do.
"""
from __future__ import annotations

import re
import shlex
import sqlite3
from pathlib import Path

import pytest

import copilot_operator
import handoff_tool

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "templates" / "copilot-instructions.md"
HANDOFF_SH = REPO / "handoff.sh"

# A fence may be indented, because several of them sit inside numbered lists.
# Matching only column zero loses every ``sql`` block in the parallel-agent
# protocol -- and loses it silently, as an empty list of blocks to check.
_FENCE_LINE = re.compile(r"^(?P<indent>[ \t]*)```(?P<info>[^\n]*)$")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


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
# The handoff command line
# --------------------------------------------------------------------------

def _documented_handoff_flags(text: str) -> set[str]:
    """Long options in the ``handoff`` command line the document tells agents to run.

    Found by content rather than by position: the section also contains a
    PowerShell block and a bash block for the manual fallback, and which one
    comes first is a formatting decision that must not decide what gets
    checked.
    """
    section = _section(text, "Session Handoff Protocol")
    lines = [
        line.strip()
        for block in _blocks(section)
        for line in block.splitlines()
        if line.strip().startswith("handoff ")
    ]
    assert len(lines) == 1, (
        "expected exactly one 'handoff ...' command line in the Session "
        f"Handoff Protocol section, found {len(lines)}"
    )
    # posix=False keeps the quotes on quoted values, which is what makes a
    # flag distinguishable from a value that looks like one: under posix=True
    # shlex strips them and `--status "--not-a-flag"` reports two flags.
    return {tok.split("=")[0] for tok in shlex.split(lines[0], posix=False)
            if tok.startswith("--")}


def test_documented_handoff_flags_exist_in_the_python_tool(template):
    documented = _documented_handoff_flags(template)
    assert documented, "parsed no flags out of the documented handoff command"
    accepted = {
        opt
        for action in handoff_tool.build_parser()._actions
        for opt in action.option_strings
    }
    unknown = sorted(documented - accepted)
    assert not unknown, (
        "templates/copilot-instructions.md tells agents to pass flags that "
        f"handoff_tool.py does not accept:\n  " + "\n  ".join(unknown) + "\n"
        "Either the flag was renamed in the tool or invented in the docs. An "
        "agent following this document verbatim gets an argparse error and "
        "loses its handoff."
    )


def test_documented_handoff_flags_exist_in_the_shell_tool(template):
    """``handoff.sh`` is what actually runs on Linux and macOS.

    The two implementations are separate hand-written parsers, so a flag can
    exist in one and not the other -- and the document promises the command
    "takes the same arguments on every platform".
    """
    script = HANDOFF_SH.read_text(encoding="utf-8")
    missing = sorted(
        flag for flag in _documented_handoff_flags(template)
        # The space-separated and ``=``-joined spellings are separate case
        # arms; requiring both is deliberate, since the document's own example
        # uses the space form and agents copy the ``=`` form from habit.
        if f"{flag})" not in script or f"{flag}=*)" not in script
    )
    assert not missing, (
        "handoff.sh does not handle both spellings of flags that "
        "templates/copilot-instructions.md documents:\n  "
        + "\n  ".join(missing)
    )


def test_the_required_handoff_flags_are_in_the_documented_command(template):
    """``handoff`` refuses to run without ``--status`` and ``--next``.

    A documented invocation that omits a required flag fails on first use, and
    an agent writing a handoff is by definition at the end of its context and
    least able to debug it.
    """
    documented = _documented_handoff_flags(template)
    for required in ("--status", "--next"):
        assert required in documented, (
            f"handoff_tool.main() exits with 'Missing required: {required}', "
            "but the documented command line does not pass it"
        )


# --------------------------------------------------------------------------
# The handoff file layout
# --------------------------------------------------------------------------

def _headings(markdown: str) -> list[str]:
    return [
        line.strip() for line in markdown.splitlines()
        if re.match(r"^#{1,2} \S", line)
    ]


def test_documented_handoff_layout_matches_what_the_tool_writes(template):
    """The shown file format must be the file the tool produces.

    The next session reads this file by section. A heading that drifts -- a
    renamed ``## Next Steps``, an extra section shown but never written -- is
    not a cosmetic difference: it is a promise to the reader about where to
    look, and the reader is a model that will believe it.
    """
    shown = _blocks(_section(template, "Session Handoff Protocol"), info="markdown")
    assert len(shown) == 1, (
        f"expected one markdown block showing the handoff file, found {len(shown)}"
    )
    written = handoff_tool.render(
        status="s", in_progress="p", next_steps="n", context="c", prompt="q",
    )
    assert _headings(shown[0]) == _headings(written), (
        "the handoff file format in templates/copilot-instructions.md no "
        "longer matches handoff_tool.render():\n"
        f"  documented: {_headings(shown[0])}\n"
        f"  written:    {_headings(written)}"
    )


# --------------------------------------------------------------------------
# The operator command lines
# --------------------------------------------------------------------------

def test_documented_operator_send_flags_are_accepted(template):
    section = _section(template, "Operator — Parallel Agents")
    sends = [
        line.strip()
        for block in _blocks(section, info="bash")
        for line in block.splitlines()
        if line.strip().startswith("operator send ")
    ]
    assert sends, "no 'operator send' example found in the Operator section"
    documented = {
        tok.split("=")[0]
        for line in sends
        # The example's message is an unclosed-quote-free placeholder, but a
        # future one need not be; posix=True would raise on an odd quote.
        for tok in shlex.split(line, posix=False)
        if tok.startswith("--")
    }
    unknown = sorted(documented - set(copilot_operator.SEND_FLAGS))
    assert not unknown, (
        "the documented 'operator send' line passes flags that "
        f"copilot_operator.SEND_FLAGS does not list:\n  " + "\n  ".join(unknown)
    )
    # The document states these two are required; the CLI enforces it.
    assert {"--from", "--to"} <= documented, (
        "operator send requires --from and --to, but the documented example "
        f"passes only {sorted(documented)}"
    )


# --------------------------------------------------------------------------
# The SQL agents are told to paste
# --------------------------------------------------------------------------

# Substituted before execution. The first group is the document's own
# ``{placeholder}`` metasyntax; the second is the bare ``N``/``M`` stand-ins in
# the session_log update, which read as column names to SQLite. A block that
# still contains a brace after substitution fails loudly below rather than
# silently skipping, so a newly introduced placeholder is a prompt to extend
# this map, not a hole in the check.
_SQL_PLACEHOLDERS = {
    "{agent_id}": "agent-1",
    "{todo_id}": "ready",
    "{done_or_blocked}": "done",
    "{branch}": "main",
    "{what you are working on}": "pinning the instructions template",
    "{shas}": "deadbeef",
    "{files}": "tests/test_instructions_template.py",
    "{notes}": "none",
    "{id}": "1",
    "tests_before = N": "tests_before = 0",
    "tests_after = M": "tests_after = 0",
}

# Mirrors the shape of the session database's built-in tables, which the
# template's SQL joins against but does not define. Only the columns its
# statements actually name are here -- this is a stand for the template's SQL
# to run against, not a specification of the real schema.
_SCRATCH_SCHEMA = """
CREATE TABLE todos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE todo_deps (
    todo_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (todo_id, depends_on)
);
"""


def _sql_blocks(text: str, heading: str) -> list[str]:
    blocks = _blocks(_section(text, heading), info="sql")
    assert blocks, f"no ```sql blocks in the '{heading}' section"
    return blocks


def _substitute(block: str) -> str:
    for placeholder, value in _SQL_PLACEHOLDERS.items():
        block = block.replace(placeholder, value)
    assert "{" not in block, (
        "a placeholder in this SQL block has no entry in _SQL_PLACEHOLDERS, "
        "so it cannot be executed:\n" + block
    )
    return block


def _run(conn: sqlite3.Connection, block: str) -> None:
    """Execute a documented block after substituting its placeholders.

    ``executescript`` rather than ``execute`` because several blocks are whole
    transactions -- ``BEGIN IMMEDIATE``, two statements, ``COMMIT`` -- and
    running them any other way would be verifying a rearrangement of the
    document rather than the document.
    """
    conn.executescript(_substitute(block))


@pytest.fixture
def scratch() -> sqlite3.Connection:
    # isolation_level=None: the claiming protocol issues its own BEGIN
    # IMMEDIATE, which the driver's implicit transaction would collide with.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_SCRATCH_SCHEMA)
    yield conn
    conn.close()


def test_the_session_log_sql_runs(template, scratch):
    """Every agent pastes this at session start and again at session end.

    A typo here is discovered by an agent mid-session, in a document it has
    been told is authoritative -- which is the worst place to discover one,
    because the agent's next move is to work around it and carry on.

    The INSERT and UPDATE are inline code spans rather than fenced blocks, so
    they are collected separately. Taking only the fenced DDL would leave the
    two statements that actually run every session unchecked, while still
    reporting a passing test about "the session_log SQL".
    """
    section = _section(template, "Session History")
    for block in _sql_blocks(template, "Session History"):
        _run(scratch, block)
    statements = [span for span in _inline_code(section)
                  if span.upper().startswith(("INSERT ", "UPDATE "))]
    assert len(statements) == 2, (
        "expected the documented INSERT and UPDATE for session_log, found "
        f"{len(statements)}"
    )
    for statement in statements:
        _run(scratch, statement)
    rows = scratch.execute(
        "SELECT branch, status, tests_after FROM session_log").fetchall()
    assert rows == [("main", "completed", 0)], (
        "the documented INSERT and UPDATE ran, but did not leave the row the "
        f"document describes: {rows}"
    )


def test_the_todo_claims_sql_runs_and_claims(template, scratch):
    """The parallel-agent protocol, executed as written rather than retyped.

    ``tests/test-todo-claims.sh`` covers the protocol's semantics against its
    own copy of these statements. That is the thing this cannot verify and
    vice versa: a correct protocol is no help if the text agents actually
    paste has drifted from it.
    """
    blocks = _sql_blocks(template, "Parallel Agents")
    # 'foundation' is in_progress and stays that way, so 'blocked-by-dep' is
    # still blocked at the end of the test. Making it depend on the todo the
    # protocol completes would unblock it, and the final "nothing is ready"
    # assertion would then be measuring the fixture rather than the SQL.
    scratch.executescript(
        "INSERT INTO todos (id, title, status) VALUES "
        "('foundation', 'Foundation', 'in_progress'),"
        "('blocked-by-dep', 'Blocked', 'pending'),"
        "('ready', 'Ready work', 'pending');"
        "INSERT INTO todo_deps (todo_id, depends_on) VALUES "
        "('blocked-by-dep', 'foundation');"
    )
    ready = [block for block in blocks if block.lstrip().upper().startswith("SELECT")]
    assert len(ready) == 1, f"expected one ready-work SELECT, found {len(ready)}"
    query = _substitute(ready[0]).rstrip().rstrip(";")
    # The claims table has to exist before the ready-work query can name it,
    # so the DDL runs first and the rest of the protocol in document order.
    ddl = [block for block in blocks if block.lstrip().upper().startswith("CREATE")]
    assert ddl, "the Parallel Agents section documents no CREATE TABLE"
    for block in ddl:
        _run(scratch, block)

    # Before anything is claimed the query must *discriminate*: 'foundation'
    # is not pending and 'blocked-by-dep' has an unfinished dependency. A
    # query that selected everything, or nothing, would satisfy the after
    # check below just as well.
    assert [row[0] for row in scratch.execute(query)] == ["ready"], (
        "the documented ready-work query does not select exactly the pending, "
        "unclaimed, dependency-satisfied todo"
    )

    for block in blocks:
        if block not in ddl:
            _run(scratch, block)

    assert scratch.execute(query).fetchall() == [], (
        "after the documented claim and release ran, the ready-work query "
        "still offers work -- a second agent would pick up a finished or "
        "blocked todo"
    )
    assert scratch.execute(
        "SELECT status FROM todos WHERE id = 'ready'").fetchone() == ("done",), (
        "the documented claim and release transactions ran without error but "
        "did not move the todo through in_progress to done"
    )
    assert scratch.execute("SELECT COUNT(*) FROM todo_claims").fetchone() == (0,), (
        "the documented release transaction left the claim behind, which "
        "would deadlock the todo against every future agent"
    )


# --------------------------------------------------------------------------
# The feature-flag table
# --------------------------------------------------------------------------

_GATE = re.compile(r"^\*Enabled by feature flag: `(?P<slug>[a-z0-9-]+)`\*\s*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\| \*\*(?P<name>[^*]+)\*\* \|", re.MULTILINE)
_ENABLED_LINE = re.compile(r"Enabled features: (?P<slugs>[^.]+)\.", re.DOTALL)


def _gated(text: str) -> list[str]:
    return _GATE.findall(text)


def _table_features(text: str) -> list[str]:
    """Feature names from the one table under ``### Feature Selection``.

    Scoped to the subsection, not to the whole configuration section: a
    ``| **Bold** |`` row elsewhere would be read as a feature nobody offers,
    and the gate would then be reporting on a table of its own invention.
    """
    return [m.group("name").strip()
            for m in _TABLE_ROW.finditer(_section(text, "Feature Selection", level=3))]


def _enabled_features_line(text: str) -> set[str]:
    """The slugs in the example per-project file's ``Enabled features:`` line."""
    section = _section(text, "What to write in a per-project file", level=3)
    matches = _ENABLED_LINE.findall(section)
    assert len(matches) == 1, (
        "expected exactly one 'Enabled features:' line in the example "
        f"per-project file, found {len(matches)}. With more than one there is "
        "no way to tell which the next agent will copy."
    )
    return {slug.strip() for slug in re.split(r",\s*", matches[0]) if slug.strip()}


def test_every_gated_section_is_offered_in_the_feature_table(template):
    """A section nobody can turn on, or an option that turns on nothing.

    The table is what an agent reads when registering a new project; the gates
    are what it reads when deciding whether a section applies to the project
    it is in. Either one alone is unfalsifiable, so they are checked against
    each other by matching each slug to the row it abbreviates.
    """
    slugs, features = _gated(template), _table_features(template)
    assert slugs, "no '*Enabled by feature flag: `x`*' markers found"
    assert features, "no feature rows found in the Feature Selection table"

    # A slug abbreviates a row name: its words are a prefix of the name's
    # words ('spec-driven' for 'Spec-Driven Development'). Prefix rather than
    # equality because the table names are prose and the slugs are terse, and
    # demanding they match exactly would be a rule about wording rather than
    # about coherence.
    unmatched = [
        slug for slug in slugs
        if not any(
            re.split(r"[ -]", name.lower())[:len(slug.split("-"))] == slug.split("-")
            for name in features
        )
    ]
    assert not unmatched, (
        "these sections are gated behind feature flags that the Feature "
        f"Selection table does not offer:\n  " + "\n  ".join(unmatched) + "\n"
        "An agent reading the table would never enable them."
    )

    unclaimed = [
        name for name in features
        if not any(
            re.split(r"[ -]", name.lower())[:len(slug.split("-"))] == slug.split("-")
            for slug in slugs
        )
    ]
    assert not unclaimed, (
        "the Feature Selection table offers features with no section gated "
        f"behind them:\n  " + "\n  ".join(unclaimed) + "\n"
        "Enabling one would change nothing an agent reads."
    )


def test_the_example_project_file_lists_exactly_the_real_flags(template):
    """The per-project file's ``Enabled features:`` line is copied verbatim.

    It is the one place the slugs appear as a list, so it is the one place a
    retired flag survives -- every per-project file written from this example
    inherits whatever it says.
    """
    listed, gated = _enabled_features_line(template), set(_gated(template))
    assert listed == gated, (
        "the example per-project file's feature list disagrees with the "
        "sections that are actually gated:\n"
        f"  only in the example: {sorted(listed - gated)}\n"
        f"  only gated:          {sorted(gated - listed)}"
    )


# --------------------------------------------------------------------------
# Guard the guards
# --------------------------------------------------------------------------

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


def test_a_second_enabled_features_line_is_refused(template):
    """A second well-formed list is ambiguous; picking one is a coin toss."""
    doubled = template.replace(
        "Enabled features: session-handoff",
        "Enabled features: telepathy.\n\nEnabled features: session-handoff",
        1,
    )
    assert doubled != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError, match="exactly one"):
        _enabled_features_line(doubled)


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


def test_the_flag_parser_finds_flags_and_ignores_their_values():
    """``--status "--next is not a flag here"`` must yield one flag, not two."""
    text = (
        "## Session Handoff Protocol\n\n"
        "```\n"
        'handoff --instance x --status "--not-a-flag" --next=later\n'
        "```\n\n"
        "## Next Section\n"
    )
    assert _documented_handoff_flags(text) == {"--instance", "--status", "--next"}


def test_a_gate_slug_must_be_matched_by_a_table_row(template):
    """The coherence check fails when the two halves disagree.

    Without this, a regex that silently stopped matching would leave both
    directions comparing empty sets and reporting agreement.
    """
    broken = template.replace(
        "*Enabled by feature flag: `session-handoff`*",
        "*Enabled by feature flag: `telepathy`*",
    )
    assert broken != template, "the substitution found nothing to replace"
    with pytest.raises(AssertionError, match="telepathy"):
        test_every_gated_section_is_offered_in_the_feature_table(broken)
