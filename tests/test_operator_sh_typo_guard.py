"""`operator.sh` must refuse a mistyped subcommand, exactly as the Python CLI does.

Backlog item 8 -- "a mistyped operator subcommand starts a session instead of
erroring" -- was fixed in `copilot_operator.py` and closed. It was fixed in
*one of the two programs that answer the word `operator`*. `operator.sh` is the
one Linux, WSL and macOS users run, it is "superseded, still maintained" rather
than dead (see `tests/test_legacy_bash_status.py`), and it still fell straight
through: `operator ls` matched no `case` arm, named no running tmux session,
and so reached the launch path, where `ls` became a copilot *prompt* and a real
session started under the current directory's name.

That is the same defect the item describes, on the platform the item never
looked at, and it is the shape this repository already has a rule about:
a rule enforced against one file is not a rule, it is that file's history.

The tests here are therefore mostly *parity* tests. The interesting failure is
not "the guard is absent" -- that is one assertion -- but "the two guards have
drifted", because two hand-maintained copies of a predicate is exactly how the
Python operator's own `RESERVED_WORDS` came to be missing `send` and `inbox`.
`test_the_shell_guard_agrees_with_the_python_guard` is the load-bearing one:
it runs both predicates over a corpus built by mutating every subcommand, so a
divergence in the edit-distance rule shows up as a specific word rather than
as a judgement call.

Two traps from the Python round are deliberately avoided here, because both
produced tests that passed while testing nothing:

- **Argument shape.** A pass-through case written as the single token
  ``["refactor the parser"]`` is a shape no shell ever produces. The 19-char
  head resembles no subcommand, so the test stayed green while the real,
  split argv was being refused. Every pass-through case below is split the
  way a shell splits it.
- **Length-property inputs.** The claim "a word two or more characters longer
  than every subcommand is never refused" was first tested with inputs +5 and
  +6 characters long, which the length bail rejects instantly -- so widening
  the edit threshold left the whole population green. The inputs here are
  exactly +2.
"""

from __future__ import annotations

import re
import string
import subprocess
from pathlib import Path

import pytest

import copilot_operator as op
from test_operator import (OPERATOR_SH, _bash_executable, _shell_function,
                           _shell_helper_names, bash)

# Distinct from any status the script itself produces, so "the probe reached a
# session function" cannot be confused with "the probe fell over early". Shared
# with `tests/test_operator.py`, which uses the same value for the same reason.
_LAUNCHED = 7

#: Functions the probe must run for real. Everything else in `operator.sh` is
#: stubbed out.
#:
#: This list is the entire reason the first version of this probe reported no
#: refusals at all. `_shell_helper_names()` enumerates *every* top-level
#: function, and the existing `_shell_dispatch` helper stubs all of them but
#: `main` -- so `subcommand_suggestions` was replaced by `return 0`, printed
#: nothing, and the guard silently could not fire. The probe was measuring its
#: own stub. A test built on that helper would have passed against an
#: `operator.sh` with the guard deleted.
_REAL = ("main", "subcommand_suggestions", "one_edit_apart", "is_reserved_word")

#: Constants the real functions read. Extracted from the shipped script rather
#: than restated, so a test cannot go on passing against a value the script no
#: longer holds.
_CONSTANTS = ("SUBCOMMANDS", "RESERVED_WORDS", "MIN_PREFIX_LENGTH",
              "SUBCOMMAND_ALIAS_WORDS", "SUBCOMMAND_ALIAS_TARGETS")


def _script_text() -> str:
    return OPERATOR_SH.read_text(encoding="utf-8")


def _assignment(name: str) -> str:
    """The single top-level `name=...` line, as the script spells it."""
    match = re.search(rf"^{re.escape(name)}=.*$", _script_text(), re.MULTILINE)
    assert match, f"{name}= not found in operator.sh"
    return match.group(0)


def subcommands() -> tuple[str, ...]:
    match = re.match(r'^SUBCOMMANDS="([^"]*)"$', _assignment("SUBCOMMANDS"))
    assert match, "SUBCOMMANDS is no longer a plain double-quoted word list"
    words = tuple(match.group(1).split())
    assert words, "SUBCOMMANDS is empty, so every guard test below is vacuous"
    return words


def aliases() -> dict[str, str]:
    def array(name: str) -> list[str]:
        match = re.match(rf"^{name}=\((.*)\)$", _assignment(name))
        assert match, f"{name} is no longer a plain indexed array literal"
        return match.group(1).split()

    words = array("SUBCOMMAND_ALIAS_WORDS")
    targets = array("SUBCOMMAND_ALIAS_TARGETS")
    # Two parallel arrays stand in for the associative array bash 3.2 does not
    # have, and nothing in the shell notices if they stop lining up: a short
    # TARGETS list makes `${SUBCOMMAND_ALIAS_TARGETS[$i]}` an unbound variable
    # under `set -u`, which aborts the whole script inside a command
    # substitution whose output is then read as "no suggestions".
    assert len(words) == len(targets), (
        f"the alias arrays have drifted out of step: {len(words)} words, "
        f"{len(targets)} targets")
    assert len(set(words)) == len(words), f"duplicate alias words: {words}"
    return dict(zip(words, targets))


def min_prefix_length() -> int:
    match = re.match(r"^MIN_PREFIX_LENGTH=(\d+)$", _assignment("MIN_PREFIX_LENGTH"))
    assert match, "MIN_PREFIX_LENGTH is no longer a plain integer"
    return int(match.group(1))


def _probe_script() -> str:
    """A runnable `operator.sh` reduced to its dispatch and its typo guard."""
    stubs = "\n".join(f"{name}() {{ return 0; }}"
                      for name in _shell_helper_names() if name not in _REAL)
    real = "".join(f"{name}() {{\n{_shell_function(name)}}}\n" for name in _REAL)
    constants = "\n".join(_assignment(name) for name in _CONSTANTS)
    return (
        # Matches operator.sh's own line 28, so the probe is no laxer than the
        # script it is quoting -- and `set -u` in particular is what turns an
        # unguarded empty-array expansion into the abort this repo cares about.
        "set -euo pipefail\n"
        "IS_FRESH=false\nSTATE_FILE=state\n"
        f"{constants}\n{stubs}\n"
        # `command -v` finds a shell function, so main's dependency checks pass
        # on a machine with none of these installed. `tmux` returns 1 so that
        # `has-session` always says no: the positional shortcut must fall
        # through to the guard rather than attaching to something.
        "tmux() { return 1; }\nsqlite3() { return 0; }\npython3() { return 0; }\n"
        'sanitize_session_name() { printf "%s\\n" "probe"; }\n'
        # `printf "%s\n" "$@"` with no arguments still prints one empty line,
        # which would read back as a forwarded empty string and make "no
        # arguments" indistinguishable from "one blank argument".
        f'run_single_session() {{ printf "single\\n"; [ "$#" -eq 0 ] || printf "%s\\n" "$@"; exit {_LAUNCHED}; }}\n'
        f'run_loop_mode() {{ printf "loop\\n"; [ "$#" -eq 0 ] || printf "%s\\n" "$@"; exit {_LAUNCHED}; }}\n'
        f'{real}main "$@"\n')


def _dispatch(argv: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "dispatch.sh"
    script.write_text(_probe_script(), encoding="utf-8", newline="\n")
    return subprocess.run([_bash_executable(), "dispatch.sh", *argv], cwd=tmp_path,
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)


def _launched(proc: subprocess.CompletedProcess) -> tuple[str, list[str]] | None:
    """`(mode, forwarded_args)` if a session was started, else None."""
    if proc.returncode != _LAUNCHED:
        return None
    lines = proc.stdout.splitlines()
    return lines[0], lines[1:]


# ── The defect itself ───────────────────────────────────────────

@bash
@pytest.mark.parametrize("word, expected", [
    ("ls", "list"),            # the reported case, and no edit distance reaches it
    ("dir", "list"),           # cmd.exe
    ("ps", "list"),
    ("kill", "stop"),
    ("sto", "stop"),           # a truncation
    ("rep", "report"),
    ("hepl", "help"),          # transposition
    ("jion", "join"),
    ("reprot", "report"),
    ("relaod", "reload"),
    ("ingset", "ingest"),
    ("lst", "list"),           # deletion
    ("listt", "list"),         # insertion
    ("LS", "list"),            # the case arms are case-sensitive; the guard is not
    ("Stop", "stop"),
])
def test_a_mistyped_subcommand_is_refused_rather_than_run(word, expected, tmp_path):
    """The whole point: a typo produces a message, not a state change.

    Asserted on all three of exit status, the suggestion, and *the absence of a
    launch*. The last one is the one that matters and the easiest to leave out:
    a guard that prints the right advice and then starts a session anyway would
    satisfy a test that only read stderr, and would be the original defect with
    a friendlier banner on it.
    """
    proc = _dispatch([word], tmp_path)
    assert _launched(proc) is None, (
        f"operator.sh {word} still started a session: {proc.stdout!r}")
    assert proc.returncode == 1, (
        f"expected a refusal (exit 1), got {proc.returncode}: {proc.stderr!r}")
    assert f"Unknown subcommand: operator {word}" in proc.stderr
    assert f"`operator {expected}`" in proc.stderr, (
        f"operator.sh suggested something else for {word}: {proc.stderr!r}")
    # The escape hatch has to be in the message, because the guard is willing
    # to be wrong: `lint` and `end` are ordinary words one keystroke from a
    # subcommand, and somebody who meant them needs to be told how to say so
    # in the message that refused them, not in the documentation.
    assert f"`operator --name NAME {word}`" in proc.stderr, (
        f"the refusal does not name the way past it: {proc.stderr!r}")


@bash
def test_the_refusal_precedes_the_dependency_checks(tmp_path):
    """`operator ls` on a box without tmux still says `ls` is spelled `list`.

    Ordering, not politeness. A typo is answerable without a multiplexer, a
    database or an interpreter, and answering it with "Error: tmux is required"
    names neither the thing the user got wrong nor the thing they wanted --
    while also being the last thing they see, since main exits there.

    The dependencies are made to look absent by overriding `command` itself
    rather than by deleting the stubs. Deleting them makes the outcome depend
    on what happens to be installed on the machine running the tests: this box
    has a real `tmux`, so the first draft's control tripped on `sqlite3`
    instead, and on a box with all three it would not have tripped at all --
    the control would have passed by launching, which is the one result that
    proves nothing.
    """
    blinded = _probe_script().replace(
        "tmux() { return 1; }\n",
        "tmux() { return 1; }\ncommand() { return 1; }\n")
    assert "command() { return 1; }" in blinded, "the injection missed"
    (tmp_path / "blind.sh").write_text(blinded, encoding="utf-8", newline="\n")

    def run(word):
        return subprocess.run([_bash_executable(), "blind.sh", word], cwd=tmp_path,
                              capture_output=True, encoding="utf-8",
                              errors="replace", timeout=120)

    typo = run("ls")
    assert "`operator list`" in typo.stderr, (
        f"the dependency check answered first: {typo.stderr!r}")

    # Positive control: with the same blinding, a word the guard does *not*
    # claim must reach the dependency check and be refused there. Without this
    # the test above passes just as well against a probe that was never
    # blinded, and asserts nothing about ordering.
    control = run("definitely-not-a-subcommand")
    assert "is required but not found" in control.stderr, (
        "the dependency checks were not actually reached, so the ordering "
        f"assertion above was vacuous: {control.stderr!r}")


# ── What must keep working ──────────────────────────────────────

@bash
@pytest.mark.parametrize("argv", [
    # Split the way a shell splits them. A prompt passed as one token is a
    # shape no shell produces, and it is the shape that let the Python round's
    # pass-through test stay green while the real argv was refused.
    ["refactor", "the", "parser"],
    ["fix", "the", "login", "bug"],
    ["read", "the", "spec", "and", "summarise", "it"],
    ["myproject"],          # the documented quick-join
    ["copilot-tools"],
    ["prism"],
    ["book-translator"],
    ["list-view"],          # longer than the subcommand it resembles
    ["report-gen"],
    ["hello"],
    ["test"],
    ["read"],
    ["in"], ["re"], ["he"], ["me"],   # two letters: below MIN_PREFIX_LENGTH
    ["--loop"],
    ["--agent", "anvil:anvil", "--yolo"],
    ["-v"],
    [],
])
def test_an_ordinary_invocation_still_reaches_a_session(argv, tmp_path):
    """`operator [copilot-args...]` is documented, so every word this guard
    claims wrongly is a working invocation taken away.

    The forwarded arguments are asserted, not just the fact of a launch: a
    guard that consumed the first word and started a session with the rest
    would still "reach a session".
    """
    proc = _dispatch(argv, tmp_path)
    launched = _launched(proc)
    assert launched is not None, (
        f"operator.sh refused {argv}, which is an ordinary invocation:\n"
        f"{proc.stderr}")
    mode, forwarded = launched
    expected_mode = "loop" if "--loop" in argv else "single"
    assert mode == expected_mode, f"{argv} started a {mode} session"
    assert forwarded == [a for a in argv if a != "--loop"], (
        f"operator.sh mangled the arguments it forwards: {forwarded}")


@bash
@pytest.mark.parametrize("word", subcommands())
def test_a_real_subcommand_is_never_refused(word, tmp_path):
    """A correctly spelled subcommand must never reach the guard.

    Every `case` arm exits today, so this cannot happen by that route -- but
    that is a fact about seven handlers rather than a property of the dispatch,
    and the failure it would produce is absurd enough to be worth pinning: the
    guard refusing the exact command it had just run, suggesting the word the
    user had spelled correctly. `stop` is the live example, because its arm
    does not exit at the arm; it exits inside `stop_operator`.
    """
    proc = _dispatch([word], tmp_path)
    assert "Unknown subcommand" not in proc.stderr, (
        f"operator.sh refused its own subcommand {word!r}: {proc.stderr!r}")


@bash
@pytest.mark.parametrize("word", sorted(
    {c + suffix for c in subcommands() for suffix in ("xy", "ed", "er", "-1")}
    | {"listen", "helper", "joined", "reloaded", "ingested", "reported"}))
def test_a_word_two_characters_longer_is_never_refused(word, tmp_path):
    """The whole safety argument for instance names, stated as a property.

    A prefix is never longer than what it prefixes, and one edit changes a
    length by at most one, so nothing two or more characters longer than every
    subcommand can be refused. Almost every real project name has that shape.

    The inputs are exactly +2 on purpose. The Python round's version of this
    test used +5 and +6, which the length bail rejects before any comparison
    happens -- so the population was green no matter how wide the edit
    threshold got, and the property it claimed to test was untested.
    """
    longest = max(len(c) for c in subcommands())
    assert any(len(word) == len(c) + 2 for c in subcommands()), (
        f"{word!r} is not exactly two characters longer than any subcommand, "
        "so it does not exercise the property this test is named for")
    assert len(word) <= longest + 2
    proc = _dispatch([word], tmp_path)
    assert _launched(proc) is not None, (
        f"operator.sh refused {word!r}, which is two characters longer than "
        f"the subcommand it resembles:\n{proc.stderr}")


# ── Parity with the Python guard ────────────────────────────────

def _corpus() -> list[str]:
    """Every one-edit neighbour and every prefix of every subcommand.

    Generated rather than listed, so the population cannot quietly stop
    covering a rule. Deletions, insertions, substitutions and transpositions
    are the four edits `one_edit_apart` claims to model; prefixes are the other
    rule; the aliases and the hand-written tail are the words no generator
    reaches.
    """
    words: set[str] = set()
    for c in subcommands():
        words.add(c)
        words.update(c[:i] for i in range(len(c) + 1))
        words.update(c[:i] + c[i + 1:] for i in range(len(c)))
        words.update(c[:i] + c[i + 1] + c[i] + c[i + 2:] for i in range(len(c) - 1))
        for i in range(len(c)):
            words.update(c[:i] + ch + c[i + 1:] for ch in string.ascii_lowercase)
        for i in range(len(c) + 1):
            words.update(c[:i] + ch + c[i:] for ch in string.ascii_lowercase)
    words.update(aliases())
    words.update(["myproject", "copilot-tools", "refactor", "hello", "test", "read",
                  "lint", "end", "prism", "list-view", "report-gen", "operator",
                  "in", "re", "he", "me", "x", "ab", "abc", "LIST", "Stop", "LS"])
    return sorted(words)


def _python_reference(word: str) -> list[str]:
    """`_subcommand_suggestions` from the Python operator, over the *shell's*
    subcommand set.

    Reusing `op._one_edit_apart` rather than reimplementing it is the point:
    this compares the two edit-distance implementations against each other.
    Only the candidate set is substituted, because the two programs genuinely
    implement different subcommands and the shell must not suggest words it
    does not answer.
    """
    word = word.lower()
    if not word:
        return []
    matches = [c for c in subcommands()
               if (len(word) >= min_prefix_length() and c.startswith(word))
               or op._one_edit_apart(word, c)]
    if not matches and word in aliases():
        matches = [aliases()[word]]
    return matches


@bash
def test_the_shell_guard_agrees_with_the_python_guard(tmp_path):
    """Two implementations of one predicate, run against each other.

    This is the test that would catch drift, and drift is the realistic future
    failure -- not the guard being deleted. It is one test over ~2000 inputs
    rather than 2000 tests because the batch runs in one bash process; a
    per-word parametrisation spawned a process each and took minutes.

    A mismatch is reported as a specific word with both answers, so the failure
    names the rule that diverged instead of asking the reader to bisect.
    """
    corpus = _corpus()
    script = tmp_path / "guard.sh"
    script.write_text(
        "set -euo pipefail\n"
        + "\n".join(_assignment(name) for name in _CONSTANTS) + "\n"
        + f"one_edit_apart() {{\n{_shell_function('one_edit_apart')}}}\n"
        + f"subcommand_suggestions() {{\n{_shell_function('subcommand_suggestions')}}}\n"
        + 'while IFS= read -r w; do\n'
          '    printf "%s\\t" "$w"\n'
          '    printf "%s " $(subcommand_suggestions "$w")\n'
          '    printf "\\n"\n'
          'done\n',
        encoding="utf-8", newline="\n")

    # Bytes, not text. `subprocess` with `encoding=` wraps stdin in a
    # TextIOWrapper whose default newline translation turns every "\n" into
    # "\r\n" on Windows, so bash reads every word with a trailing CR, matches
    # nothing, and the comparison reports the shell guard as universally
    # broken. The first run of this probe did exactly that -- 1967 of 1998
    # "mismatches", none of them real.
    proc = subprocess.run([_bash_executable(), "guard.sh"], cwd=tmp_path,
                          input=("\n".join(corpus) + "\n").encode("utf-8"),
                          capture_output=True, timeout=600)
    stdout = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, (
        f"the shell guard aborted: {proc.stderr.decode('utf-8', 'replace')[-2000:]}")
    assert "\r" not in stdout, (
        "a CR reached the shell guard, so every answer below is about the "
        "wrong word")

    answers: dict[str, list[str]] = {}
    for line in stdout.split("\n"):
        if "\t" not in line:
            continue
        word, _, rest = line.partition("\t")
        answers[word] = rest.split()

    missing = [w for w in corpus if w not in answers]
    assert not missing, f"the probe never answered for {missing[:10]}"

    mismatches = [(w, answers[w], _python_reference(w))
                  for w in corpus if answers[w] != _python_reference(w)]
    assert not mismatches, (
        f"{len(mismatches)} of {len(corpus)} words are judged differently by "
        f"operator.sh and copilot_operator.py:\n" + "\n".join(
            f"  {w!r}: operator.sh={got} copilot_operator={want}"
            for w, got, want in mismatches[:20]))

    # Positive control. Without it, a probe that answered `[]` for everything
    # and a reference that did the same would agree perfectly, and this test
    # would be a very slow way of asserting nothing.
    assert answers["ls"] == ["list"], answers["ls"]
    assert any(answers[w] for w in corpus), "the shell guard matched nothing at all"
    assert sum(1 for w in corpus if answers[w]) > 50, (
        "the shell guard matched almost nothing, so agreement proves little")


# ── The two lists that must not drift ───────────────────────────

def _case_arms() -> set[str]:
    """The words `main`'s dispatch `case` actually answers.

    Read out of the script so that adding an arm without adding it to
    SUBCOMMANDS fails here, rather than becoming a subcommand the typo guard
    treats as an instance name.
    """
    body = _shell_function("main")
    match = re.search(r"case \"\$\{1:-\}\" in\n(.*?)^    esac$",
                      body, re.MULTILINE | re.DOTALL)
    assert match, "main()'s dispatch case is no longer where this test looks"
    arms: set[str] = set()
    for line in match.group(1).splitlines():
        arm = re.match(r"^        ([^\s()]+)\)$", line)
        if arm:
            arms.update(w for w in arm.group(1).split("|")
                        # `-h`, `--help` and `-?` are flag spellings of `help`,
                        # not subcommands: the guard never sees a word starting
                        # with a dash.
                        if not w.startswith("-") and not w.startswith("\\-"))
    assert arms, "no case arms were extracted, so this test proves nothing"
    return arms


def test_the_subcommand_list_matches_the_dispatch():
    """SUBCOMMANDS is what `main` answers -- checked, not asserted in a comment.

    The Python operator kept a hand-maintained second copy of this and it had
    already drifted: `send` and `inbox` were dispatched and missing from the
    reserved set. Nothing broke, because both were matched before the shortcut
    was reached, and that silence is what would have let the next omission be
    a real one.
    """
    assert set(subcommands()) == _case_arms(), (
        "operator.sh's SUBCOMMANDS and its dispatch have drifted:\n"
        f"  only in SUBCOMMANDS: {sorted(set(subcommands()) - _case_arms())}\n"
        f"  only in the case:    {sorted(_case_arms() - set(subcommands()))}")


def test_reserved_words_are_derived_from_the_subcommand_list():
    """One list, not two. This is the mechanical half of the test above."""
    assert _assignment("RESERVED_WORDS") == 'RESERVED_WORDS="$SUBCOMMANDS"', (
        "RESERVED_WORDS has become a second hand-maintained copy of the "
        f"subcommand list: {_assignment('RESERVED_WORDS')}")


def test_the_two_guards_share_the_constants_they_are_meant_to_share():
    """The parity test above cannot see a changed constant, so this does.

    `_python_reference` reads MIN_PREFIX_LENGTH and the alias table *out of the
    shell*, deliberately -- it exists to compare the two edit-distance
    implementations, not the two configurations. The cost is that halving
    MIN_PREFIX_LENGTH changes both sides of that comparison identically and
    produces no mismatch at all, so the one hole it leaves is closed here.

    `SUBCOMMANDS` is exempt and must stay exempt: the two programs really do
    implement different subcommands, and requiring the shell to list `send`
    would make it suggest a word it answers by starting a session.
    """
    assert min_prefix_length() == op.MIN_PREFIX_LENGTH, (
        f"operator.sh uses MIN_PREFIX_LENGTH={min_prefix_length()}, "
        f"copilot_operator.py uses {op.MIN_PREFIX_LENGTH}")

    expected = {word: target for word, target in op.SUBCOMMAND_ALIASES.items()
                if target in subcommands()}
    assert aliases() == expected, (
        "the two alias tables have drifted:\n"
        f"  operator.sh:        {aliases()}\n"
        f"  copilot_operator.py (restricted to what operator.sh implements): "
        f"{expected}")
    # An alias dropped for the *right* reason -- its target is Python-only --
    # must be visible as such, rather than as an alias someone forgot.
    dropped = {w: t for w, t in op.SUBCOMMAND_ALIASES.items()
               if t not in subcommands()}
    assert not dropped or all(t not in subcommands() for t in dropped.values())


def test_every_alias_points_at_a_subcommand_this_script_implements():
    """An alias naming a word `operator.sh` does not answer is worse than none.

    The Python operator has `send`, `inbox`, `restart-loop` and more. Pointing
    a reader here at one of those would answer their typing mistake with a
    word that *this* script also handles by starting a session -- the defect,
    reintroduced through its own fix.
    """
    unknown = {word: target for word, target in aliases().items()
               if target not in subcommands()}
    assert not unknown, (
        f"these aliases name words operator.sh does not implement: {unknown}")


def test_no_alias_is_reachable_by_the_distance_rules():
    """The alias table is for words no edit distance can reach.

    An entry that the prefix or edit rule already matches is dead weight that
    reads as intent, and it hides a real question: if `stauts` is meant to
    reach `list`, the alias table cannot do it, because aliases are matched
    exactly and `status` is not a subcommand here.
    """
    redundant = {word: _python_reference(word) for word in aliases()
                 if [c for c in subcommands()
                     if (len(word) >= min_prefix_length() and c.startswith(word))
                     or op._one_edit_apart(word, c)]}
    assert not redundant, (
        f"these aliases are already matched by the distance rules: {redundant}")
