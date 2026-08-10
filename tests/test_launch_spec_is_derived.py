"""The launch spec is derived, so nothing may ship a command that edits it.

`operator reload` rewrote `{id}.launch.json` and printed `✅ Launch spec
updated`. Nothing ever read what it wrote: `start_session` calls
`write_launch_spec` and hands the path it returns straight to `runner_argv`,
and that is the only `MUX.new_session` in the operator. Every launch therefore
regenerates the file immediately before the only thing that consumes it, so an
edit made at any other time is overwritten before anybody looks.

The command survived for two weeks because its only test asserted the file's
contents *after calling it* -- true of the write, and silent about whether the
write is ever used. This asserts the property that made it pointless, so the
next command that wants to hot-edit a derived artifact fails here with the
reason rather than shipping a green checkmark.

Static, because the thing being asserted is a fact about the code's shape: no
runtime test can prove that no future caller launches from a stale spec, but a
scan can show there is exactly one launch path and that it always rewrites.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OPERATOR = REPO / "copilot_operator.py"


def _functions(source: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Name) and func.id == name:
                found.append(call)
            elif isinstance(func, ast.Attribute) and func.attr == name:
                found.append(call)
    return found


def launch_sites(source: str) -> list[tuple[str, int]]:
    """``(function, lineno)`` for every ``MUX.new_session`` in the operator."""
    sites = []
    for func in _functions(source):
        for call in _calls(func, "new_session"):
            sites.append((func.name, call.lineno))
    return sites


def regenerating_launch_sites(source: str) -> list[tuple[str, int]]:
    """Launch sites that launch from the spec this function just wrote.

    Data flow, not proximity. The first draft asked only whether the enclosing
    function contained an earlier ``write_launch_spec`` call, which a reviewer
    showed accepts the exact regression it exists to refuse::

        def start(a, b):
            spec = write_launch_spec(a, args)
            MUX.new_session(b.session, cwd, runner_argv(b.spec_file))

    The write happens, the launch ignores it, and the scan reports the tree
    clean. So the returned name has to be the one handed to ``runner_argv``.
    """
    ok = []
    for func in _functions(source):
        bound: dict[str, int] = {}
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            if not _calls(node.value, "write_launch_spec") if node.value else True:
                continue
            for target in node.targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        bound[name.id] = node.lineno
        if not bound:
            continue
        for call in _calls(func, "new_session"):
            fed = set()
            for inner in _calls(call, "runner_argv"):
                for name in ast.walk(inner):
                    if isinstance(name, ast.Name):
                        fed.add(name.id)
            used = {n for n in fed if n in bound and call.lineno > bound[n]}
            if used:
                ok.append((func.name, call.lineno))
    return ok


def test_every_launch_regenerates_the_spec_it_launches_from():
    """The property that makes an on-disk edit of the spec meaningless.

    If this ever fails, a launch path exists that uses a spec somebody else
    wrote -- at which point editing the file on disk becomes meaningful and
    `reload` could be brought back. Until then it cannot be.
    """
    source = OPERATOR.read_text(encoding="utf-8")
    sites = launch_sites(source)
    assert sites, "found no launch site at all — this scan is measuring nothing"
    stale = sorted(set(sites) - set(regenerating_launch_sites(source)))
    assert stale == [], (
        "these launch a session without regenerating the launch spec first:\n  "
        + "\n  ".join(f"{fn}:{line}" for fn, line in stale)
    )


def test_nothing_writes_the_launch_spec_outside_the_launch_path():
    """A second writer is how `reload` came to exist in the first place."""
    source = OPERATOR.read_text(encoding="utf-8")
    writers = sorted({func.name for func in _functions(source)
                      if _calls(func, "write_launch_spec")})
    assert writers == ["start_session"], (
        f"the launch spec is written by {writers}. It is a derived artifact "
        f"regenerated at every launch, so anything else writing it produces a "
        f"file that is overwritten before it is read — which is exactly what "
        f"`operator reload` did while reporting success."
    )


def test_reload_does_not_claim_to_have_done_anything(capsys):
    """It printed a green tick for a write nothing consumed."""
    import copilot_operator as op

    assert op.reload_instance("anything") == 1
    captured = capsys.readouterr()
    assert "✅" not in captured.out + captured.err
    assert "restart-loop" in captured.err, \
        "refusing without naming the command that works is a dead end"


def test_reload_refuses_with_no_target_too(capsys):
    import copilot_operator as op

    assert op.reload_instance(None) == 1
    assert "restart-loop" in capsys.readouterr().err


# ── controls ────────────────────────────────────────────────────
GOOD = '''
def start_session(instance, args):
    spec = write_launch_spec(instance, args)
    MUX.new_session(instance.session, cwd, runner_argv(spec))
'''

BAD_NO_WRITE = '''
def resume_session(instance):
    MUX.new_session(instance.session, cwd, runner_argv(instance.spec_file))
'''

BAD_WRITE_AFTER = '''
def start_session(instance, args):
    MUX.new_session(instance.session, cwd, runner_argv(instance.spec_file))
    spec = write_launch_spec(instance, args)
'''

BAD_OTHER_FUNCTION = '''
def start_session(instance, args):
    spec = write_launch_spec(instance, args)
    MUX.new_session(instance.session, cwd, runner_argv(spec))


def resume_session(instance):
    MUX.new_session(instance.session, cwd, runner_argv(instance.spec_file))
'''


BAD_LAUNCHES_ANOTHER_SPEC = '''
def start(a, b):
    spec = write_launch_spec(a, args)
    MUX.new_session(b.session, cwd, runner_argv(b.spec_file))
'''


@pytest.mark.parametrize("source, expected", [
    pytest.param(GOOD, [], id="write then launch from what was written"),
    pytest.param(BAD_NO_WRITE, [("resume_session", 3)], id="launch with no write"),
    pytest.param(BAD_WRITE_AFTER, [("start_session", 3)], id="write after launch"),
    pytest.param(BAD_OTHER_FUNCTION, [("resume_session", 8)],
                 id="a second launch path that reuses the file"),
    pytest.param(BAD_LAUNCHES_ANOTHER_SPEC, [("start", 4)],
                 id="writes one spec and launches a different one"),
])
def test_the_scan_separates_regenerating_launches_from_stale_ones(source, expected):
    stale = sorted(set(launch_sites(source)) - set(regenerating_launch_sites(source)))
    assert stale == expected


def test_the_scan_would_notice_if_the_launch_site_vanished():
    """A scan that finds no launch site reports a clean tree.

    The assertion in the real test guards this, and this is its control.
    """
    assert launch_sites("def nothing():\n    pass\n") == []
