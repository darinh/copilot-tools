"""`operator.sh` has to run on the bash macOS actually ships, which is 3.2.

Apple froze /bin/bash at the last GPLv2 release in 2007 and is never going to
unfreeze it, so "bash 3.2" is not a legacy configuration a user could upgrade
out of -- on macOS it is the default interpreter, permanently. Two constructs
this script used are bash 4 features:

  * associative arrays (`local -A`), which 3.2 does not have at all: the line
    is `declare: -A: invalid option`, and under `set -e` that ends the run;
  * expanding an empty array as `"${a[@]}"` under `set -u`, which aborts with
    "unbound variable" before 4.4.

Both were invisible for the same reason: CI only ever *executed* this script
on ubuntu, where bash is 5.x, so the macOS jobs ran the Python tests against a
shell they never started. The second one was found when a test began running
these functions through the macOS runner's own bash and went red instantly.
The first was found by reading outward from it, and had been sitting in
`operator list` and `operator stop` -- two of the three commands this script
has -- the whole time.

These tests run the real function bodies through whatever bash is present, so
on the macOS runners they are a bash 3.2 conformance check, and everywhere
else they are still a behavioural check on the set logic that replaced the
associative arrays. That replacement is the part worth testing on every
platform: an associative array cannot mistake `alpha` for a member when only
`alpha-one` was added, and a string of names joined by newlines very much can.
"""
import subprocess

import pytest

from test_operator import OPERATOR_SH, _bash_executable, _shell_function, bash


def _run(script: str, tmp_path, *argv: str) -> subprocess.CompletedProcess:
    """Run `script` under the real bash, from a native temp directory."""
    path = tmp_path / "probe.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([_bash_executable(), "probe.sh", *argv],
                          cwd=tmp_path, capture_output=True, text=True,
                          # operator.sh prints box-drawing characters, and on
                          # Windows the default decoding is cp1252, which dies
                          # on them -- inside a reader thread, so it surfaces
                          # as an unrelated TypeError on `None`.
                          encoding="utf-8", errors="replace",
                          timeout=60)


@bash
def test_in_list_answers_membership_and_not_substring(tmp_path):
    """Exact membership, against the near-misses a cheaper encoding allows.

    The first version of this joined the names with newlines and asked whether
    the string contained one. That is what these cases are aimed at: a
    substring of a member, a member split by an embedded newline, and a query
    holding glob metacharacters. Every case runs in one script and reports its
    own answer, so a false positive and a false negative are both visible in a
    single failure -- and the members that must be found sit alongside the
    near-misses that must not be, which is what stops this passing against an
    `in_list` broken into always returning false.
    """
    script = (
        "set -euo pipefail\n"
        f"in_list() {{\n{_shell_function('in_list')}}}\n"
        "s=(alpha-one beta 'star*name' 'two words' $'multi\\nline')\n"
        'check() { if in_list "$1" "${s[@]}"; then printf "IN|%s\\n" "$2"; '
        'else printf "OUT|%s\\n" "$2"; fi; }\n'
        "check alpha-one alpha-one\n"
        "check beta beta\n"
        "check 'star*name' 'star*name'\n"
        "check 'two words' 'two words'\n"
        "check $'multi\\nline' multiline-whole\n"
        "check alpha alpha\n"
        "check one one\n"
        "check eta eta\n"
        "check '*' star\n"
        "check 'star?name' 'star?name'\n"
        "check multi multi-half\n"
        "check line line-half\n"
        "check '' empty\n"
    )
    proc = _run(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    answers = {}
    for line in proc.stdout.splitlines():
        if "|" in line:
            verdict, name = line.split("|", 1)
            answers[name] = verdict
    assert len(answers) == 13, f"expected every query to answer: {proc.stdout}"

    # Real members. If these are OUT the membership test is simply broken, and
    # the absences below would pass for the wrong reason.
    assert answers["alpha-one"] == "IN"
    assert answers["beta"] == "IN"
    assert answers["star*name"] == "IN"
    assert answers["two words"] == "IN"
    assert answers["multiline-whole"] == "IN"

    # Near misses: substrings of a real member at each position -- prefix,
    # suffix, interior.
    assert answers["alpha"] == "OUT"
    assert answers["one"] == "OUT"
    assert answers["eta"] == "OUT"

    # Either half of a member containing a newline. A newline-delimited set
    # answers IN to both of these, because one member has become two -- and a
    # marker filename may contain a newline, so this is reachable rather than
    # theoretical. `stop_operator` hands every name it calls a member to
    # `tmux kill-session`.
    assert answers["multi-half"] == "OUT"
    assert answers["line-half"] == "OUT"

    # Glob metacharacters in the *query* must be literal. Unquoted, `*` would
    # match every member and report that any name is managed.
    assert answers["star"] == "OUT"
    assert answers["star?name"] == "OUT"

    assert answers["empty"] == "OUT"


@bash
def test_in_list_says_no_to_an_empty_list(tmp_path):
    """The list is empty whenever no markers exist, which is the common case
    on a machine with no operator running -- and expanding an empty array is
    the other bash 3.2 abort, so this is a conformance check too."""
    script = (
        "set -euo pipefail\n"
        f"in_list() {{\n{_shell_function('in_list')}}}\n"
        "s=()\n"
        'if in_list alpha ${s[@]+"${s[@]}"}; then echo IN; else echo OUT; fi\n'
        "s=(alpha)\n"
        'if in_list alpha ${s[@]+"${s[@]}"}; then echo IN; else echo OUT; fi\n'
    )
    proc = _run(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    # The second line is the control: the same call, one member added, must
    # flip. Without it "OUT" proves nothing -- a function that never matches
    # anything would satisfy the first line perfectly.
    assert proc.stdout.split() == ["OUT", "IN"], proc.stdout


@bash
def test_list_instances_shows_managed_sessions_and_hides_the_rest(tmp_path):
    """`operator list`, run for real.

    On the macOS runners this is the bash 3.2 canary: before the conversion
    the body died on `local -A managed_sessions` at its fourth line, so the
    command printed a header and an error. Everywhere else it is a test that
    the marker-file set still selects the right sessions.
    """
    restart_dir = tmp_path / "restart"
    restart_dir.mkdir()
    (restart_dir / "alpha.managed").write_text("", encoding="utf-8")
    (restart_dir / "beta.state").write_text("", encoding="utf-8")

    script = (
        "set -euo pipefail\n"
        f'RESTART_DIR="{restart_dir.as_posix()}"\n'
        'tmux() { printf "%s\\n" "alpha: 1 windows" "gamma: 1 windows" '
        '"beta: 2 windows"; }\n'
        f"in_list() {{\n{_shell_function('in_list')}}}\n"
        f"list_instances() {{\n{_shell_function('list_instances')}}}\n"
        "list_instances\n"
    )
    proc = _run(script, tmp_path)
    assert proc.returncode == 0, (
        f"list_instances() did not survive this bash:\n{proc.stderr}")

    assert "alpha: 1 windows" in proc.stdout, proc.stdout
    assert "beta: 2 windows" in proc.stdout, proc.stdout
    # gamma is a live tmux session with no operator marker. Listing it would
    # mean `operator stop` offers to kill sessions the operator never started.
    assert "gamma" not in proc.stdout, proc.stdout
    assert "(none)" not in proc.stdout, proc.stdout


@bash
def test_stop_operator_kills_only_the_sessions_it_manages(tmp_path):
    """`operator stop` with no target, run for real.

    Same bash 3.2 canary as above, on the command where getting the set wrong
    is destructive rather than merely wrong: every name this reports is a name
    it passes to `tmux kill-session`.
    """
    restart_dir = tmp_path / "restart"
    restart_dir.mkdir()
    (restart_dir / "alpha.managed").write_text("", encoding="utf-8")
    (restart_dir / "beta.state").write_text("", encoding="utf-8")

    script = (
        "set -euo pipefail\n"
        f'RESTART_DIR="{restart_dir.as_posix()}"\n'
        'log() { printf "%s\\n" "$*"; }\n'
        'sanitize_session_name() { printf "%s\\n" "$1"; }\n'
        'tmux() {\n'
        '    case "$1" in\n'
        '        list-sessions) printf "%s\\n" alpha gamma beta ;;\n'
        '        kill-session) printf "KILLED %s\\n" "$3" ;;\n'
        '    esac\n'
        '}\n'
        f"in_list() {{\n{_shell_function('in_list')}}}\n"
        f"stop_operator() {{\n{_shell_function('stop_operator')}}}\n"
        "stop_operator\n"
    )
    proc = _run(script, tmp_path)
    assert proc.returncode == 0, (
        f"stop_operator() did not survive this bash:\n{proc.stderr}")

    killed = sorted(line.split(None, 1)[1] for line in proc.stdout.splitlines()
                    if line.startswith("KILLED "))
    assert killed == ["alpha", "beta"], (
        f"stop_operator killed {killed}; it must kill both marked sessions "
        f"and leave the unmarked one alone.\n{proc.stdout}")


@bash
def test_generate_run_script_survives_and_quotes_an_empty_argument_list(tmp_path):
    """The innermost consumer of the argv array, run with nothing in it.

    `generate_run_script` does `local copilot_args=("$@")`, so it is empty
    exactly when its caller passed nothing -- and on bash 3.2 an empty array
    expanded as `"${a[@]}"` under `set -u` is an unbound-variable abort, not
    zero words. Every caller today builds the array from a non-empty defaults
    list, which is a fact about those lists and not a property of this
    function: cb10f72 shortened one of them from six elements to four.

    The non-empty case is asserted in the same test, because a
    `generate_run_script` that had been broken into writing nothing at all
    would satisfy the empty case perfectly.
    """
    def run(*argv: str) -> str:
        out = tmp_path / "run.sh"
        script = (
            "set -euo pipefail\n"
            f'RUN_SCRIPT="{out.as_posix()}"\n'
            'SCRIPT_PREAMBLE=""\n'
            f"generate_run_script() {{\n{_shell_function('generate_run_script')}}}\n"
            'generate_run_script "$@"\n'
        )
        proc = _run(script, tmp_path, *argv)
        assert proc.returncode == 0, (
            f"generate_run_script({list(argv)}) did not survive this bash:\n"
            f"{proc.stderr}")
        return out.read_text(encoding="utf-8")

    assert run().splitlines()[-1] == "exec copilot"

    # The positive control, and the reason the guard has to preserve quoting
    # rather than merely avoid the abort: an unquoted expansion would word-split
    # this argument into two and launch with a prompt the user did not write.
    launched = run("--yolo", "-p", "two words").splitlines()[-1]
    assert launched.startswith("exec copilot --yolo -p ")
    assert launched.endswith("two\\ words") or launched.endswith("'two words'"), launched


def test_operator_sh_uses_no_bash_4_only_declarations():
    """Moved, not deleted -- see `tests/test_shell_bash32_conformance.py`.

    This test read one file, and that was the whole defect: `operator.sh` was
    converted off associative arrays while `handoff.sh` kept one in
    `resolve_instance`, and the tripwire that was supposed to prevent exactly
    that had `OPERATOR_SH` baked into it. A rule enforced against a single
    file is not a rule, it is that file's history.

    Its replacement scans every first-party shell script, discovered rather
    than listed, and covers the bash 4 feature set rather than the one
    construct that happened to be found first -- so `operator.sh` and `-A`
    remain covered, along with five more scripts and thirteen more
    constructs. Kept here as a signpost because the reason this moved is
    easier to lose than the assertion was.
    """
    pytest.importorskip("test_shell_bash32_conformance")
    from test_shell_bash32_conformance import (_findings,
                                               _first_party_scripts)

    scanned = [p.name for p in _first_party_scripts()]
    assert OPERATOR_SH.name in scanned, (
        f"operator.sh is no longer in the conformance scan: {scanned}")
    assert not _findings(OPERATOR_SH.read_text(encoding="utf-8"))
