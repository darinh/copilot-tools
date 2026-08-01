"""The README's Repository Structure tree must match the repository.

A structure diagram is the first thing a newcomer reads and the last thing
anyone updates. This repository's tree had silently drifted to omit ten tracked
top-level entries -- among them a whole directory (``extensions/``) and a
verification gate that CI runs on every push (``verify_cross_platform.py``).
Nothing failed, because a stale map fails only the reader.

Only the *top level* of the tree is enforced, and deliberately so. That is the
layer that claims to be a complete inventory, and it is the layer that drifts:
a new module lands at the root and the diagram does not grow a line for it.
Deeper levels are illustrative -- the tree annotates a few children of
``docs/`` and ``skills/`` to show what they are for, and enumerating every file
beneath them would turn a map into a duplicate of ``git ls-files`` that goes
red on any unrelated addition.

Both directions are checked. A tree missing an entry misinforms by omission; a
tree naming a file that was deleted misinforms by assertion, which is worse.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"

HEADING = "## Repository Structure"

# A top-level entry is a branch character in column zero; nested entries are
# indented behind a "│" or spaces, so the anchor is what excludes them. The
# name runs to the inline comment, which is separated by at least two spaces --
# capturing "not whitespace" instead would silently truncate a name containing
# one, reporting the README as wrong when it was right.
_TOP_LEVEL = re.compile(r"^(?:├──|└──)\s+(?P<name>.+?)(?:\s{2,}#.*)?$")

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _tree_block(text: str) -> str:
    """Return the fenced tree from the structure section.

    Scoped to the section and identified by content rather than position: the
    first fence after the heading would be the wrong one the day somebody adds
    a command example above the tree, and reading past the next ``##`` would
    let an unrelated section's fence stand in for a structure section that has
    lost its own.
    """
    _, sep, after = text.partition(HEADING)
    assert sep, f"README.md has no '{HEADING}' section"
    section = re.split(r"^## ", after, maxsplit=1, flags=re.MULTILINE)[0]
    trees = [block for block in _FENCE.findall(section) if _listed_top_level(block)]
    assert len(trees) == 1, (
        f"expected exactly one directory tree in the '{HEADING}' section, "
        f"found {len(trees)}"
    )
    return trees[0]


def _listed_top_level(block: str) -> set[str]:
    """Names the tree claims exist at the repository root, without trailing /."""
    return {
        match.group("name").strip().rstrip("/")
        for line in block.splitlines()
        if (match := _TOP_LEVEL.match(line))
    }


def _tracked_top_level() -> set[str]:
    """First path segment of every file git tracks: the real root inventory."""
    # ``.git`` is a directory in a primary checkout and a file in a worktree;
    # exists() covers both. Its absence means an unpacked sdist, where there is
    # nothing to compare against. A missing git binary is a different thing
    # entirely -- skipping there would quietly disarm the gate.
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout; no tracked file list to compare against")
    if shutil.which("git") is None:
        pytest.fail("git is required to verify the README structure tree")
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(REPO), capture_output=True, check=True,
    )
    # -z suppresses git's path quoting, so the bytes are the path verbatim.
    # Decoding them with the platform's preferred encoding, as text=True would,
    # mangles any non-ASCII name on a Windows runner.
    entries = {
        path.split("/", 1)[0]
        for path in proc.stdout.decode("utf-8", "surrogateescape").split("\0")
        if path
    }
    # An empty inventory is not "the repository is empty", it is "the question
    # was not answered". Left alone it would make the omission check below pass
    # against nothing at all -- a green gate reporting on a repository it never
    # managed to read.
    assert entries, (
        "git ls-files returned no tracked files in "
        f"{REPO}. The comparison below would be vacuous, so this is a failure "
        "of the check itself rather than a verdict on the README."
    )
    return entries


@pytest.fixture(scope="module")
def listed() -> set[str]:
    return _listed_top_level(_tree_block(README.read_text(encoding="utf-8")))


def test_tree_lists_every_tracked_top_level_entry(listed):
    missing = sorted(_tracked_top_level() - listed)
    assert not missing, (
        "README.md's Repository Structure tree omits tracked top-level "
        f"entries:\n  " + "\n  ".join(missing) + "\n"
        "Add a line for each, with a comment saying what it is for."
    )


def test_tree_lists_nothing_that_is_not_tracked(listed):
    stale = sorted(listed - _tracked_top_level())
    assert not stale, (
        "README.md's Repository Structure tree names top-level entries that "
        f"git does not track:\n  " + "\n  ".join(stale) + "\n"
        "They were renamed, deleted, or never committed. Remove or correct "
        "the lines."
    )


def test_the_parser_reads_only_the_top_level():
    """Guard the guard.

    If ``_TOP_LEVEL`` ever started matching indented lines, the tests above
    would demand that nested filenames appear at the root and the fix would be
    to wreck the tree. The name with a space in it is here because a "not
    whitespace" capture truncates it to ``a`` and reports the README as missing
    an entry it plainly lists.

    A parser that matched nothing at all needs no test of its own: it makes
    ``_tree_block`` find zero trees and take the whole module down in fixture
    setup.
    """
    block = (
        "copilot-tools/\n"
        "├── .github/\n"
        "│   ├── copilot-instructions.md    # nested, must be ignored\n"
        "│   └── workflows/ci.yml           # nested, must be ignored\n"
        "├── LICENSE                        # MIT\n"
        "├── a name with spaces.txt         # must survive intact\n"
        "├── extensions/\n"
        "└── docs/\n"
        "    └── operator.md                # nested, must be ignored\n"
    )
    assert _listed_top_level(block) == {
        ".github", "LICENSE", "a name with spaces.txt", "extensions", "docs",
    }


def test_the_tree_is_identified_by_content_not_by_position():
    """A fence added above the tree must not be mistaken for it."""
    readme = (
        f"{HEADING}\n\n"
        "Run this first:\n\n"
        "```bash\npython setup_tools.py --status\n```\n\n"
        "```\n"
        "copilot-tools/\n"
        "├── LICENSE                        # MIT\n"
        "└── docs/\n"
        "```\n\n"
        "## Versioning\n\n"
        "```\n"
        "├── not-the-tree.py                # a later section's fence\n"
        "```\n"
    )
    assert _listed_top_level(_tree_block(readme)) == {"LICENSE", "docs"}
