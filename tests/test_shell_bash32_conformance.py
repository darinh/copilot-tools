"""Every shell script this repo ships has to run on the bash macOS ships.

`/bin/bash` on macOS is 3.2, frozen at the last GPLv2 release in 2007 and
never going to move. It is not a legacy configuration a user could upgrade out
of: on macOS it is the default interpreter, permanently.

`tests/test_operator_sh_bash32.py` already covers this by *executing* function
bodies, which is the stronger test — but it can only tell 3.2 from 5.x when a
3.2 interpreter is actually present, and that happens on exactly one leg of
CI. Everywhere else those tests are behavioural checks that pass identically
against code bash 3.2 would refuse to run.

That gap is not hypothetical, and this module exists because of what it hid.
`operator.sh` was converted off associative arrays; `handoff.sh` had the same
construct, in `resolve_instance`, and kept it. Nothing noticed, because the
tripwire that pinned the conversion read one file:

    OPERATOR_SH.read_text(...)

A rule enforced against one file is not a rule, it is that file's history. So
the scan below is over every first-party script, discovered rather than
listed, and covers the bash 4 feature set rather than the single construct
that happened to be found first.

**Why static, when this repo is sceptical of textual tests.** The convention
here is that pinning a *word* and calling it a behaviour is usually wrong. It
is exact in this case for the same reason the `-A` tripwire was: these are not
proxies for the incompatibility, they *are* the tokens bash 3.2 rejects. There
is no way to write an associative array that `declare -A` misses, and no way
to trip it without writing one. The one rule below that is a *convention*
rather than a token — that array expansions use the `${a[@]+"${a[@]}"}` guard
— is labelled as such where it is defined.

Every detector is exercised against source that must trip it
(`test_every_detector_fires_on_source_that_uses_the_feature`). Without that,
a detector broken into matching nothing reports every script clean, which is
the shape this module is here to prevent in the first place.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Vendored, not ours. `.specify/scripts/bash/` is regenerated wholesale by
# `specify init`, so a finding there cannot be fixed here -- it would come
# back on the next regeneration. It is excluded deliberately and named here
# so the exclusion stays visible; silently narrowing the population is the
# failure this module exists to catch.
VENDORED = (".specify",)
NOT_SOURCE = (".git", ".worktrees", "node_modules", "__pycache__")


def _first_party_scripts() -> list[Path]:
    # Filtered on the path *relative to the repo*, never the absolute one.
    # Every agent on this project works in `<repo>/.worktrees/<branch>/`, so
    # an absolute-path filter matches `.worktrees` for every script in the
    # tree, the population comes back empty, and an empty population passes
    # every "no script contains X" assertion in this file. That is not a
    # hypothetical: it is what this function did when it was first written,
    # and `test_the_scan_population_is_not_empty_and_holds_the_scripts_we_ship`
    # is the only reason it did not ship green and blind.
    return sorted(
        p for p in REPO.rglob("*.sh")
        if not any(part in VENDORED or part in NOT_SOURCE
                   for part in p.relative_to(REPO).parts)
    )


def _code_only(line: str) -> str:
    """`line` with any trailing comment removed, quotes respected.

    A `#` only starts a comment at the start of a word, which is what keeps
    `${#arr[@]}` and `$#` out of it, and never inside quotes, which is what
    keeps `grep '#'` out of it.
    """
    out: list[str] = []
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is not None:
            # Backslash escapes inside "..." only; inside '...' it is literal.
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(ch)
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
        i += 1
    return "".join(out)


# `${a[@]+"${a[@]}"}` -- the guarded expansion. The backreferences matter:
# `${a[@]+"${b[@]}"}` is a real bug (it guards on one array and expands
# another) and must not be accepted as a guard for either of them.
_GUARDED = re.compile(
    r"""\$\{([A-Za-z_]\w*)\[([@*])\]\+"?\$\{\1\[\2\]\}"?\}""")

# A bare value expansion of an array: `"${a[@]}"` / `${a[*]}`. `${#a[@]}` is
# NOT this -- the length operator is safe under `set -u` on every bash, which
# is why the name has to follow `{` immediately here.
_BARE_ARRAY = re.compile(r"\$\{[A-Za-z_]\w*\[[@*]\]\}")


def _unguarded_array_expansions(code: str) -> list[str]:
    return _BARE_ARRAY.findall(_GUARDED.sub("", code))


# (name, predicate over one line of code, why it is fatal on bash 3.2)
DETECTORS: tuple[tuple[str, object, str], ...] = (
    (
        "associative array",
        re.compile(r"\b(?:local|declare|typeset|readonly|export)\s+-[A-Za-z]*A").search,
        "bash 3.2 has no associative arrays: `declare: -A: invalid option`, "
        "which under `set -e` ends the run",
    ),
    (
        "nameref",
        re.compile(r"\b(?:local|declare|typeset)\s+-[A-Za-z]*n\b").search,
        "namerefs are bash 4.3",
    ),
    (
        "declare -g",
        re.compile(r"\b(?:declare|typeset)\s+-[A-Za-z]*g").search,
        "`declare -g` is bash 4.2",
    ),
    (
        "mapfile/readarray",
        re.compile(r"\b(?:mapfile|readarray)\b").search,
        "`mapfile`/`readarray` are bash 4.0",
    ),
    (
        "coproc",
        re.compile(r"\bcoproc\b").search,
        "`coproc` is bash 4.0",
    ),
    (
        "case modification",
        re.compile(r"\$\{[A-Za-z_]\w*(?:\[[^]]*\])?(?:\^|,)[^}]*\}").search,
        "`${v^^}` / `${v,,}` case modification is bash 4.0",
    ),
    (
        "parameter transformation",
        re.compile(r"\$\{[A-Za-z_]\w*(?:\[[^]]*\])?@[QEPAKakLUu]\}").search,
        "`${v@Q}` parameter transformation is bash 4.4",
    ),
    (
        "negative array index",
        re.compile(r"\$\{[A-Za-z_]\w*\[-").search,
        "negative array subscripts are bash 4.2",
    ),
    (
        "&>> redirect",
        re.compile(r"&>>").search,
        "`&>>` is bash 4.0",
    ),
    (
        "|& pipe",
        re.compile(r"\|&").search,
        "`|&` is bash 4.0",
    ),
    (
        "[[ -v ]]",
        re.compile(r"\[\[\s+-v\s").search,
        "`[[ -v var ]]` is bash 4.2",
    ),
    (
        "wait -n",
        re.compile(r"\bwait\s+-n\b").search,
        "`wait -n` is bash 4.3",
    ),
    (
        "globstar",
        re.compile(r"\bglobstar\b").search,
        "`globstar` is bash 4.0",
    ),
    (
        # The one convention rather than a token. Bare `"${a[@]}"` is valid
        # syntax on 3.2 and aborts only when the array is empty, so this is a
        # rule about how the array is written, not about what bash rejects.
        # It is enforced uniformly on purpose: which arrays can be reached
        # while empty is a fact about today's callers, and those change. The
        # last time someone hand-verified a list as non-empty, the same branch
        # had shortened that list from six elements to four a few hours
        # earlier.
        "unguarded array expansion",
        lambda code: bool(_unguarded_array_expansions(code)),
        'expanding an empty array as `"${a[@]}"` under `set -u` is an '
        'unbound-variable abort before bash 4.4; write '
        '`${a[@]+"${a[@]}"}` instead',
    ),
)


def _findings(text: str) -> list[tuple[int, str, str]]:
    """(line number, detector name, the offending line) for one script."""
    found = []
    for n, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        code = _code_only(raw)
        if not code.strip():
            continue
        for name, matches, _why in DETECTORS:
            if matches(code):
                found.append((n, name, raw.strip()))
    return found


def test_the_scan_population_is_not_empty_and_holds_the_scripts_we_ship():
    """The guard on the guard.

    Every assertion below is of the form "no script contains X", and an empty
    population satisfies all of them perfectly. `rglob` returning nothing --
    because the layout moved, because an exclusion grew a leading dot, because
    this file was copied somewhere without a repo above it -- would read as
    total compliance. So the population is asserted before it is used.
    """
    found = {p.relative_to(REPO).as_posix() for p in _first_party_scripts()}

    # Named individually rather than counted: a count survives a script being
    # dropped and another added, and it is the *drop* that silently retires
    # coverage.
    for expected in ("operator.sh", "handoff.sh", "setup.sh",
                     "diagnose-restart-deleter.sh"):
        assert expected in found, (
            f"{expected} is no longer being scanned for bash 3.2 "
            f"conformance. Scanned: {sorted(found)}")

    assert not any(p.startswith(".specify/") for p in found), (
        "the vendored spec-kit scripts are in the population; findings there "
        "cannot be fixed in this repo because `specify init` regenerates "
        f"them: {sorted(p for p in found if p.startswith('.specify/'))}")


@pytest.mark.parametrize(
    "script", _first_party_scripts(),
    ids=lambda p: p.relative_to(REPO).as_posix())
def test_shell_script_uses_no_bash_4_only_construct(script):
    """The rule itself, over one script.

    Parametrised per script so a failure names the file in the test id, and
    so a newly added script is covered the moment it lands rather than when
    someone remembers to add it to a list.
    """
    why = {name: reason for name, _m, reason in DETECTORS}
    findings = _findings(script.read_text(encoding="utf-8"))
    assert not findings, "\n".join(
        [f"{script.relative_to(REPO).as_posix()} will not run on the bash "
         f"macOS ships (3.2):"]
        + [f"  line {n}: {name} -- {why[name]}\n      {line}"
           for n, name, line in findings])


def test_every_detector_fires_on_source_that_uses_the_feature():
    """The positive control, and the reason to believe the scan above.

    A detector with a typo'd pattern matches nothing and reports every script
    clean -- indistinguishable, in a pass count, from a codebase that is
    actually clean. Each detector gets a line it must flag, and the check is
    that *its own* name comes back, not merely that something did: two
    detectors are near-neighbours (`declare -A` and `declare -g` differ by one
    letter) and "some detector fired" would let one cover for the other.
    """
    samples = {
        "associative array": 'local -A seen',
        "nameref": 'local -n ref="$1"',
        "declare -g": 'declare -g GLOBAL=1',
        "mapfile/readarray": 'mapfile -t lines < file',
        "coproc": 'coproc tail -f log',
        "case modification": 'echo "${name^^}"',
        "parameter transformation": 'echo "${name@Q}"',
        "negative array index": 'echo "${arr[-1]}"',
        "&>> redirect": 'run &>> log',
        "|& pipe": 'make |& tee log',
        "[[ -v ]]": 'if [[ -v FOO ]]; then :; fi',
        "wait -n": 'wait -n',
        "globstar": 'shopt -s globstar',
        "unguarded array expansion": 'for x in "${arr[@]}"; do :; done',
    }
    assert set(samples) == {name for name, _m, _w in DETECTORS}, (
        "a detector has no positive control, so nothing shows whether it "
        "works")

    for name, matches, _why in DETECTORS:
        line = samples[name]
        assert matches(_code_only(line)), (
            f"the {name!r} detector did not fire on {line!r}, so every "
            f"'no script uses this' assertion above is vacuous for it")


def test_the_detectors_accept_the_forms_that_are_actually_portable():
    """The negative control.

    A detector that matches everything also makes the scan meaningless -- it
    would fail loudly rather than silently, but only until someone deleted the
    rule to get CI green. These are the portable spellings that live in the
    scripts today and must stay accepted.
    """
    portable = [
        'local -a items=()',                       # indexed array, 3.2 has these
        'for x in ${arr[@]+"${arr[@]}"}; do :; done',   # the guarded expansion
        'if (( ${#arr[@]} > 0 )); then :; fi',     # length: safe under set -u
        'echo "$#"',
        'base="${base%.state}"',
        'value="${VAR:-default,with,commas}"',     # not case modification
        'printf "%s\\n" "$@"',
        'if [[ -n "${VAR+x}" ]]; then :; fi',      # the 3.2 way to ask "is set?"
        'cmd1 || cmd2 &',                          # not `|&`
    ]
    for line in portable:
        fired = [name for name, matches, _w in DETECTORS
                 if matches(_code_only(line))]
        assert not fired, f"{fired} rejected portable bash: {line!r}"


def test_comment_stripping_does_not_blind_the_scan():
    """`_code_only` is the one place a construct could hide.

    It is load-bearing in both directions: too eager and real code stops being
    scanned, too shy and every comment that *discusses* a construct fails the
    build. Both directions are asserted, because a stripper that returned ""
    for everything would satisfy the second half alone -- and would switch the
    entire module off.
    """
    # Still code, still scanned.
    assert _code_only('local -A seen  # a set').strip() == 'local -A seen'
    assert _code_only('echo "a # b"') == 'echo "a # b"'
    assert _code_only("grep '#' file") == "grep '#' file"
    assert _code_only('n=${#arr[@]}') == 'n=${#arr[@]}'
    assert _code_only('echo "$#"') == 'echo "$#"'

    # Comments, not code.
    assert _code_only('# local -A seen').strip() == ''
    assert _code_only('    # ${a[@]}').strip() == ''
    assert _code_only('code # local -A seen').strip() == 'code'

    # And the end-to-end consequence of both halves.
    assert _findings('# local -A seen\n') == []
    assert [f[1] for f in _findings('local -A seen  # trailing note\n')] == [
        "associative array"]


def test_the_guard_pattern_requires_the_same_array_on_both_sides():
    """`${a[@]+"${b[@]}"}` guards on one array and expands another.

    It is a plausible thing to produce by copy-paste, it aborts on 3.2 exactly
    when `b` is empty and `a` is not, and a guard pattern written without
    backreferences would wave it through -- while still passing every other
    test in this file.
    """
    assert _unguarded_array_expansions('f ${a[@]+"${a[@]}"}') == []
    assert _unguarded_array_expansions('f ${a[@]+"${b[@]}"}') == ['${b[@]}']
    # Same array, mismatched subscript: `${a[@]+...}` says nothing about
    # whether `${a[*]}` is safe to expand -- it is the same abort.
    assert _unguarded_array_expansions('f ${a[@]+"${a[*]}"}') == ['${a[*]}']
