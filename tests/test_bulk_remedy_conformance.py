"""A remedy printed once per instance must have a form that does all of them.

`operator list` named eight stale supervisors and printed eight
`operator restart-loop NAME` lines. Every one of those supervisors went stale
on the same operator change -- they each import their code once, at startup --
so the fault was always machine-wide and the remedy was always per-instance.
A remedy you apply by hand once per row is a remedy you apply to some of the
rows, and the ones you miss look exactly like the ones you fixed.

This is a static scan rather than an execution test for the same reason the
bash 3.2 conformance scan is: the defect is visible in the shape of the code,
and reproducing it at runtime needs a machine with several stale supervisors
on it, which no unit test has.

It is deliberately not scoped to `copilot_operator.py`. The rule this replaced
was applied to whichever notice somebody happened to be looking at, which is
how two of the three staleness blocks kept their per-instance remedy through
the change that fixed the first one. A rule enforced against one block is not
a rule, it is that block's history.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Subcommands that are genuinely about one named instance and have no sensible
#: sweep, so printing them per instance is the right shape. `join` attaches a
#: terminal, which is one at a time by construction; `forget` and `stop` take
#: an instance because doing them to everything is a decision nobody should be
#: nudged into by a listing.
SINGULAR = frozenset({"join", "forget", "stop", "stop-session", "stop-loop",
                      "worktree", "session", "work", "send", "reply", "inbox",
                      "backlog"})

_REMEDY = re.compile(r"\boperator\s+([a-z][a-z-]*)\b")


def first_party() -> list[Path]:
    return sorted(p for p in REPO.glob("*.py"))


def _literal_prefix(node: ast.JoinedStr) -> str:
    """The f-string's text up to its first interpolation."""
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            break
    return "".join(parts)


def _interpolated_names(node: ast.JoinedStr) -> set[str]:
    names: set[str] = set()
    for value in ast.walk(node):
        if isinstance(value, ast.Name):
            names.add(value.id)
    return names


def _loop_targets(node: ast.For) -> set[str]:
    return {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}


def per_instance_remedies(source: str) -> list[tuple[int, str]]:
    """``(lineno, subcommand)`` for every operator command printed in a loop.

    A command whose text interpolates the loop variable is being printed once
    per item, which is the shape this scan exists to refuse.
    """
    found: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for loop in ast.walk(tree):
        if not isinstance(loop, ast.For):
            continue
        targets = _loop_targets(loop)
        if not targets:
            continue
        for call in ast.walk(loop):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "print"):
                continue
            for arg in call.args:
                if not isinstance(arg, ast.JoinedStr):
                    continue
                match = _REMEDY.search(_literal_prefix(arg))
                if not match:
                    continue
                if not (_interpolated_names(arg) & targets):
                    continue
                if match.group(1) in SINGULAR:
                    continue
                found.append((arg.lineno, match.group(1)))
    return found


def test_no_remedy_is_printed_once_per_instance():
    offenders = []
    for path in first_party():
        for lineno, command in per_instance_remedies(
                path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}:{lineno}: operator {command} <name>")
    assert offenders == [], (
        "these print one command per instance:\n  " + "\n  ".join(offenders)
        + "\n\nIf the condition can affect several instances at once -- and a "
        "supervisor's loaded code always can, because they all import it at "
        "startup -- name the instances and print one command that fixes all "
        "of them. Add the subcommand to SINGULAR if it is genuinely one at a "
        "time."
    )


# ── controls ────────────────────────────────────────────────────
#
# A detector that matches nothing reports the whole tree clean, which reads
# exactly like success. Both directions are asserted.
POSITIVE = [
    pytest.param(
        'for name in stale:\n'
        '    print(f"    operator restart-loop {name}")\n',
        id="the exact shape this scan was written for",
    ),
    pytest.param(
        'for n in names:\n'
        '    print(f"  Fix it with: operator restart-loop {n} --now")\n',
        id="command embedded mid-sentence",
    ),
    pytest.param(
        'for snap in snaps:\n'
        '    if snap:\n'
        '        print(f"    operator reload {snap}")\n',
        id="nested inside a branch",
    ),
    pytest.param(
        'for name in stale:\n'
        '    print(f"    operator restart-loop {name.strip()}")\n',
        id="interpolation is an attribute call on the loop variable",
    ),
]

NEGATIVE = [
    pytest.param(
        'for name in stale:\n'
        '    print(f"    {name}")\n'
        'print("    operator restart-loop --all")\n',
        id="names listed, remedy printed once -- the fixed shape",
    ),
    pytest.param(
        'print(f"    operator join {instance.display_name}")\n',
        id="not in a loop at all",
    ),
    pytest.param(
        'for name in names:\n'
        '    print(f"    operator join {name}")\n',
        id="a subcommand that really is one at a time",
    ),
    pytest.param(
        'for name in names:\n'
        '    print(f"    {name} is stale")\n',
        id="no command in the line",
    ),
    pytest.param(
        'for name in names:\n'
        '    print("    operator restart-loop --all")\n',
        id="a sweep printed inside a loop interpolates nothing",
    ),
]


@pytest.mark.parametrize("source", POSITIVE)
def test_the_scan_fires_on_a_per_instance_remedy(source):
    assert per_instance_remedies(source), \
        "the detector did not fire on a case it exists to catch"


@pytest.mark.parametrize("source", NEGATIVE)
def test_the_scan_is_silent_on_the_correct_shape(source):
    assert per_instance_remedies(source) == [], \
        "the detector fired on a shape that is not the defect"


def test_the_scan_reads_more_than_one_file():
    """The rule it replaced was enforced against whichever block was in view."""
    assert len(first_party()) > 5


def test_singular_names_only_real_subcommands():
    """An exemption for a command that does not exist exempts nothing, and
    would go on reading like a considered decision."""
    source = (REPO / "copilot_operator.py").read_text(encoding="utf-8")
    for name in SINGULAR:
        assert f'"{name}"' in source, (
            f"SINGULAR exempts {name!r}, which is not a subcommand this "
            f"operator has"
        )
