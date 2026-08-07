"""Every command a skill names must exist, and every skill must be shipped.

A skill is loaded at the moment the agent has decided to act, so a command it
names wrong is not a documentation defect the reader shrugs at -- it is an
instruction the agent will follow. The ETH Zurich measurement behind
`docs/rationale.md` is the reason to care about the direction: a tool named in
context is used ~1.6 times per instance versus under 0.01 when unnamed. Naming
a command that does not exist is therefore *more* expensive than saying
nothing, because the agent will try it.

The scan deliberately covers the templates and the repository's own `AGENTS.md`
as well as `skills/`. Those are the same class of artifact -- text an agent acts
on without checking -- and a rule enforced against one file is not a rule, it is
that file's history.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import copilot_operator

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"

#: `operator <word>`, where the word looks like a subcommand rather than prose.
#: Long options and placeholders are excluded here rather than filtered later,
#: so that `operator --loop --headless` and `operator <your-instance>` do not
#: have to be special-cased as false subcommands downstream.
_INVOCATION = re.compile(r"\boperator\s+([a-z][a-z0-9-]*)")

#: Words that follow `operator` in ordinary English rather than naming a
#: subcommand. Kept short and explicit: every entry is a licence to miss a real
#: defect, so a new one needs a reason, not a convenience.
_PROSE_AFTER_OPERATOR = frozenset({
    "agent", "agents", "assigns", "assigned", "commands", "confirms",
    "does", "instance", "is", "restart", "session", "sessions", "starts",
    "to",
})


def _skill_files() -> list[Path]:
    return sorted(SKILLS.glob("*/SKILL.md"))


def _documents() -> list[Path]:
    """Everything an agent is expected to act on without verifying."""
    docs = _skill_files()
    docs.append(REPO / "templates" / "copilot-instructions.md")
    agents = REPO / "AGENTS.md"
    if agents.is_file():
        docs.append(agents)
    return docs


def named_commands(text: str) -> set[str]:
    """Subcommands named in ``text``.

    ``operator restart-loop`` is a real subcommand and ``operator restart the
    loop`` is not, so `restart` is prose while `restart-loop` is not -- the
    hyphenated form is matched first by the pattern's greedy character class,
    which is why the two do not collide.
    """
    found = set()
    for word in _INVOCATION.findall(text):
        if word in _PROSE_AFTER_OPERATOR:
            continue
        found.add(word)
    return found


def test_there_are_skills_to_check():
    """Without this, deleting `skills/` turns every rule below into a loop
    over an empty list and the suite reports clean at the moment the skills
    stopped existing."""
    assert len(_skill_files()) >= 5


@pytest.mark.parametrize("path", _skill_files(), ids=lambda p: p.parent.name)
def test_a_skill_declares_the_name_of_its_directory(path: Path):
    """A skill whose front matter disagrees with its directory is loaded under
    one name and referred to under another."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} has no front matter"
    front = text.split("---", 2)[1]
    assert f"name: {path.parent.name}\n" in front, \
        f"{path} declares a name other than its directory"
    assert re.search(r"^description: \S", front, re.MULTILINE), \
        f"{path} has no description, so nothing tells an agent when to load it"


@pytest.mark.parametrize("path", _documents(),
                         ids=lambda p: str(p.relative_to(REPO)).replace("\\", "/"))
def test_every_operator_command_named_in_a_document_exists(path: Path):
    named = named_commands(path.read_text(encoding="utf-8"))
    unknown = sorted(named - set(copilot_operator.SUBCOMMANDS))
    assert not unknown, (
        f"{path.relative_to(REPO)} names operator subcommands that do not "
        f"exist: {unknown}. An agent that reads this will run them.")


def test_the_scan_finds_the_commands_it_is_meant_to_be_checking():
    """A positive control for the parametrised test above.

    Without it, a pattern that matched nothing would report every document
    clean, which reads exactly like coverage. Asserting on specific commands
    rather than a count also means a regex that matched only the first
    invocation per file would fail here.
    """
    peer = (SKILLS / "peer-agents" / "SKILL.md").read_text(encoding="utf-8")
    named = named_commands(peer)
    assert {"send", "reply", "inbox", "list", "join", "stop"} <= named

    trees = (SKILLS / "worktrees" / "SKILL.md").read_text(encoding="utf-8")
    assert "worktree" in named_commands(trees)

    items = (SKILLS / "backlog" / "SKILL.md").read_text(encoding="utf-8")
    assert "backlog" in named_commands(items)


def test_the_scan_fires_on_a_command_that_does_not_exist():
    """The negative control. A detector that cannot fire is not a detector."""
    named = named_commands("Run `operator teleport --now` to finish.")
    assert "teleport" in named
    assert "teleport" not in copilot_operator.SUBCOMMANDS


def test_prose_after_operator_is_not_mistaken_for_a_command():
    """The exclusion list must not be doing its job by accident."""
    assert named_commands("operator assigns your worktree") == set()
    assert named_commands("the operator does not recognise") == set()
    # ...but a real command spelled the same way in prose is still caught.
    assert named_commands("operator restart-loop scripts") == {"restart-loop"}


def test_every_skill_path_referenced_in_the_docs_exists():
    """A `skills/<name>` path that no longer resolves is a pointer to nothing.

    Stated as a path rule rather than a ban on the retired name, because the
    ban would have to be relaxed for prose that *describes* the retirement --
    and a rule with a prose exemption stops being checkable. A path is either
    there or it is not.
    """
    pattern = re.compile(r"skills/([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![-a-z0-9*])")
    offenders = []
    for path in [*_documents(), REPO / "README.md",
                 *sorted((REPO / "docs").glob("*.md"))]:
        if not path.is_file():
            continue
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            for name in pattern.findall(line):
                if name in {"demo"}:      # fixtures in prose about testing
                    continue
                if not (SKILLS / name).is_dir():
                    offenders.append(
                        f"{path.relative_to(REPO)}:{number}: skills/{name}")
    assert not offenders, "\n".join(offenders)


def test_the_scan_fires_on_a_skill_path_that_does_not_exist():
    """The negative control for the rule above, plus the two shapes the
    pattern must *not* treat as paths: a glob standing for a family of
    skills, and a name whose hyphen is the start of one."""
    pattern = re.compile(r"skills/([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?![-a-z0-9*])")
    assert pattern.findall("see skills/operator-agents/SKILL.md") == \
        ["operator-agents"]
    assert not (SKILLS / "operator-agents").is_dir()

    assert pattern.findall("`skills/operator-backlog-*` file items") == []
    assert pattern.findall(".specify/skills/speckit-*") == []


def test_the_instructions_point_at_the_current_peer_skill():
    """D7 retired `operator-agents`; the always-loaded file must name the one
    that exists. A directive pointing at a skill that will not load is worse
    than no directive, because the agent stops looking once it has one."""
    for path in (REPO / "templates" / "copilot-instructions.md",
                 REPO / "AGENTS.md"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "`peer-agents` skill" in text, \
            f"{path.relative_to(REPO)} does not name the peer-agents skill"
        assert "`operator-agents` skill" not in text, \
            f"{path.relative_to(REPO)} still directs agents to the retired skill"
