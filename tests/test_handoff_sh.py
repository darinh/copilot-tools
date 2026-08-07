"""`handoff.sh`'s instance inference, run for real.

`resolve_instance` is reached on every `handoff` invoked without
`--instance`, which is how the operator's own restart protocol calls it. Until
this file existed it had no test at all, and it contained `local -A
managed_sessions` -- an associative array, which the bash macOS ships (3.2,
frozen at the last GPLv2 release) does not have. There it is `declare: -A:
invalid option`, and under the script's `set -e` that ends the run.

What kept it hidden is worth more than the bug. The abort happened inside

    instance=$(resolve_instance "$project_root") || die "Cannot infer instance."

so it arrived as a non-zero exit status -- indistinguishable from the honest
"no operator session matches this directory" the same line produces. On macOS
the inference had never worked once, and it failed in the words of a feature
politely declining to guess. A read that fails has to stay distinguishable
from a read that returned nothing; here it did not, all the way to the user.

These tests run the real function bodies through whatever bash is present, so
on the macOS runners they are a bash 3.2 conformance check and everywhere else
they are a behavioural check on the set logic that replaced the associative
array. The static half -- catching a bash 4 construct on platforms whose bash
is 5.x and cannot object -- is `test_shell_bash32_conformance.py`.
"""
import re
import subprocess

import pytest

from test_operator import OPERATOR_SH, _bash_executable, bash

HANDOFF_SH = OPERATOR_SH.parent / "handoff.sh"


def _shell_function(path, name: str) -> str:
    """The body of a top-level ``name() {`` ... ``}`` function in `path`.

    Reads the shipped source rather than a copy, so these tests cannot go on
    passing against a function the script no longer contains.
    """
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}$",
                      text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in {path.name}"
    return match.group(1)


def _harness(tmp_path, restart_dir, sessions, cwd_of, argv=()):
    """A script that calls the real `resolve_instance` with tmux stubbed out.

    `cwd_of` maps a session name to the working directory tmux should report
    for it. The default is the project root itself, expressed as
    ``$(cd ... && pwd)`` *inside the script* rather than as a Python-side
    string: on msys bash `pwd` prints `/c/Users/...` while Python prints
    `C:/Users/...`, and a stub that returned the Python spelling would make
    every session look like it belonged to a different project -- so every
    test would agree there were no matches, for the wrong reason.
    """
    cases = "".join(
        f'            {name}) printf "%s\\n" "{path}" ;;\n'
        for name, path in cwd_of.items())
    script = (
        "set -euo pipefail\n"
        f'RESTART_DIR="{restart_dir.as_posix()}"\n'
        f'NORM=$(cd "{tmp_path.as_posix()}" && pwd)\n'
        "tmux() {\n"
        '    case "$1" in\n'
        "        list-sessions) printf '%s\\n' "
        + " ".join(sessions) + " ;;\n"
        '        display-message)\n'
        '            case "$3" in\n'
        f"{cases}"
        '            *) printf "%s\\n" "$NORM" ;;\n'
        "            esac ;;\n"
        "    esac\n"
        "}\n"
        f"in_list() {{\n{_shell_function(HANDOFF_SH, 'in_list')}}}\n"
        f"resolve_instance() {{\n{_shell_function(HANDOFF_SH, 'resolve_instance')}}}\n"
        f'resolve_instance "{tmp_path.as_posix()}"\n'
    )
    path = tmp_path / "probe.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([_bash_executable(), "probe.sh", *argv],
                          cwd=tmp_path, capture_output=True, text=True,
                          # handoff.sh prints box-drawing characters; on
                          # Windows the default decoding is cp1252, which dies
                          # on them inside a reader thread and surfaces as an
                          # unrelated TypeError on `None`.
                          encoding="utf-8", errors="replace", timeout=60)


def _markers(tmp_path, *names):
    restart = tmp_path / "restart"
    restart.mkdir(exist_ok=True)
    for name in names:
        (restart / name).write_text("", encoding="utf-8")
    return restart


@bash
def test_resolve_instance_picks_the_managed_session_in_this_project(tmp_path):
    """The whole function, on the path that has to work.

    Three live tmux sessions, and only one of them is the answer, for two
    independent reasons: `gamma` is running but has no operator marker, and
    `beta` has a marker but is working in a different directory. Both
    exclusions are in the same run, so a `resolve_instance` that ignored the
    marker set and one that ignored the directory check cannot both hide.
    """
    restart = _markers(tmp_path, "alpha.managed", "beta.state")
    proc = _harness(tmp_path, restart,
                    sessions=["alpha", "gamma", "beta"],
                    cwd_of={"beta": "/some/other/project"})

    assert proc.returncode == 0, (
        f"resolve_instance() did not survive this bash or found nothing:\n"
        f"{proc.stderr}")
    assert proc.stdout.strip() == "alpha", (
        f"expected the marked session in this directory; got "
        f"{proc.stdout!r}\n{proc.stderr}")


@bash
def test_resolve_instance_declines_quietly_when_nothing_is_managed(tmp_path):
    """No markers at all -- the state of a machine with no operator running.

    This is the empty-array path: `managed_sessions` and `matches` are both
    empty, and expanding an empty array as `"${a[@]}"` under `set -u` is an
    abort on every bash before 4.4. The assertion is on stderr rather than on
    the exit status, because the status is 1 either way -- 1 is what this
    function returns when it legitimately finds nothing, and it is also what
    the shell returns when it dies. Asserting the code alone would accept the
    crash as the answer, which is precisely the confusion that let the
    associative array live in here for as long as it did.
    """
    restart = _markers(tmp_path)
    proc = _harness(tmp_path, restart, sessions=["alpha", "gamma"], cwd_of={})

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stderr.strip() == "", (
        f"resolve_instance() found nothing, but it also said something to "
        f"stderr, which a clean 'no match' does not do:\n{proc.stderr}")
    assert proc.stdout.strip() == "", proc.stdout

    # The positive control, in the same test: an empty answer proves nothing
    # on its own, because a function that had been broken into always failing
    # would produce it too.
    restart = _markers(tmp_path, "alpha.managed")
    ok = _harness(tmp_path, restart, sessions=["alpha", "gamma"], cwd_of={})
    assert ok.returncode == 0 and ok.stdout.strip() == "alpha", (
        f"the control did not resolve: {ok.stdout!r} {ok.stderr!r}")


@bash
def test_resolve_instance_refuses_to_choose_between_two_and_names_both(tmp_path):
    """Two candidates: the branch that expands `matches` to list them.

    Reported rather than guessed at, because picking one would write a
    session's handoff into another session's project.
    """
    restart = _markers(tmp_path, "alpha.managed", "beta.managed")
    proc = _harness(tmp_path, restart, sessions=["alpha", "beta"], cwd_of={})

    assert proc.returncode == 1, proc.stdout
    assert proc.stdout.strip() == "", (
        f"an ambiguous result was printed as if it were an answer: "
        f"{proc.stdout!r}")
    assert "alpha" in proc.stderr and "beta" in proc.stderr, (
        f"both candidates have to be named or the user cannot pick one:\n"
        f"{proc.stderr}")


@bash
def test_resolve_instance_matches_session_names_exactly(tmp_path):
    """The near-misses the associative array used to rule out for free.

    A hash lookup cannot mistake `alpha` for `alpha-two`. The indexed-array
    scan that replaced it can, if it is ever rewritten as a substring test --
    which is what the equivalent code in operator.sh did on its first attempt.
    Here the cost of a false positive is that `handoff` writes to the wrong
    project's next-session.md.
    """
    restart = _markers(tmp_path, "alpha.managed")
    proc = _harness(tmp_path, restart,
                    sessions=["alpha-two", "alph", "xalphax"], cwd_of={})

    assert proc.returncode == 1, (
        f"a session whose name merely contains 'alpha' was accepted as the "
        f"marked instance 'alpha': {proc.stdout!r}")
    assert proc.stdout.strip() == "", proc.stdout


def test_handoff_and_operator_agree_on_what_membership_means():
    """The two copies of `in_list`, held to each other.

    `operator.sh` and `handoff.sh` are installed independently and neither
    sources the other, so the duplication is deliberate. What is not
    deliberate is drift: the newline-joined encoding this function replaced
    was fixed in one script while the other kept an associative array, and
    the whole reason that survived is that nothing compared the two files.
    If these ever need to differ, say why here rather than deleting the test.
    """
    theirs = _shell_function(OPERATOR_SH, "in_list").rstrip()
    ours = _shell_function(HANDOFF_SH, "in_list").rstrip()
    assert ours == theirs, (
        "handoff.sh's in_list has drifted from operator.sh's:\n"
        f"--- operator.sh ---\n{theirs}\n--- handoff.sh ---\n{ours}")


# -- the instance name has to be a filename ----------------------

def _addressable(tmp_path, name: str):
    """The real ``addressable_instance_id`` body, on one name.

    `die` is stubbed rather than sourced so the probe can distinguish a
    refusal from a crash: the script's own `die` exits 1, and so does almost
    every other way a bash script can fail under `set -e`.
    """
    script = (
        "set -euo pipefail\n"
        'die() { printf "REFUSED: %s\\n" "$*" >&2; exit 3; }\n'
        f"addressable_instance_id() {{\n"
        f"{_shell_function(HANDOFF_SH, 'addressable_instance_id')}}}\n"
        'addressable_instance_id "$1"\n'
        'printf "%s\\n" "$instance_id"\n'
    )
    path = tmp_path / "probe.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([_bash_executable(), "probe.sh", name],
                          cwd=tmp_path, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)


@bash
@pytest.mark.parametrize("name", [
    "../../elsewhere/steal",   # escapes the project's handoff directory
    "a/b",                     # ditto, one level
    "a.b",                     # safe_instance_id would append a digest
    "a:b",                     # ditto, and would collide with `a.b` without it
    "-lead",                   # outer dashes are stripped by sanitize_name
    "trail-",
    "",
    "con",                     # a Windows device name gets a digest suffix
    "COM1",
    "a-b-69f664",              # already shaped like a generated digest
])
def test_a_name_this_script_cannot_address_is_refused_not_guessed(
        tmp_path, name):
    """Refusing loudly beats writing a real handoff where nothing reads.

    Every name here is one `safe_instance_id` would return *changed*, so the
    installed `handoff` and `operator` address it by a different filename than
    a literal interpolation would. The first two are worse than a mismatch:
    they leave the project's handoff directory entirely.
    """
    proc = _addressable(tmp_path, name)

    assert proc.returncode == 3, (
        f"{name!r} was not refused; the script would write to a path "
        f"`operator` does not read. stdout={proc.stdout!r}")
    assert "REFUSED" in proc.stderr


@bash
@pytest.mark.parametrize("name", ["copilot-tools", "agent-academy", "x9",
                                  "a-b-1234567"])
def test_an_ordinary_instance_name_is_passed_through_unchanged(tmp_path, name):
    """The other half, and the one that would make the guard useless if wrong.

    A refusal that fired on the names `operator` actually generates would
    break every handoff on the platforms this script exists for, so the
    accepted set is pinned rather than left to the refusal tests to imply.
    `a-b-1234567` is one character too long to be the digest suffix, which is
    the boundary the pattern is most likely to get wrong.
    """
    proc = _addressable(tmp_path, name)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == name


@bash
def test_the_script_addresses_the_handoff_by_the_validated_id(tmp_path):
    """The guard is worth nothing if the paths are still built from the raw name.

    A source check rather than a run, because reaching the write path needs a
    catalog, a guid and a mux; what can go wrong here is an interpolation left
    behind, and that is visible in the text.
    """
    text = HANDOFF_SH.read_text(encoding="utf-8")
    after = text[text.index('addressable_instance_id "$instance"'):]
    leaked = [line.strip() for line in after.splitlines()
              if "${instance}" in line and "echo" not in line
              and "printf" not in line]
    assert not leaked, (
        "these lines below the guard still interpolate the unvalidated "
        "name:\n  " + "\n  ".join(leaked))
