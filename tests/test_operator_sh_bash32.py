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
def test_set_contains_answers_membership_and_not_substring(tmp_path):
    """The set is a string now, so the failure mode is a substring match.

    Every case runs in one script and reports its own answer, so a false
    positive and a false negative are both visible in a single failure --
    and the members that must be found sit alongside the near-misses that
    must not be, which is what stops this passing against a `set_contains`
    that has been broken into always returning false.
    """
    script = (
        "set -euo pipefail\n"
        f"set_contains() {{\n{_shell_function('set_contains')}}}\n"
        "s=\"\"\n"
        "for m in alpha-one beta 'star*name'; do s+=\"$m\"$'\\n'; done\n"
        'check() { if set_contains "$s" "$1"; then printf "IN|%s\\n" "$1"; '
        'else printf "OUT|%s\\n" "$1"; fi; }\n'
        "check alpha-one\n"
        "check beta\n"
        "check 'star*name'\n"
        "check alpha\n"
        "check one\n"
        "check eta\n"
        "check '*'\n"
        "check 'star?name'\n"
        "check ''\n"
    )
    proc = _run(script, tmp_path)
    assert proc.returncode == 0, proc.stderr
    # Delimited rather than whitespace-split: one of the queries is the empty
    # string, and splitting that row on whitespace loses the field entirely.
    answers = {}
    for line in proc.stdout.splitlines():
        if "|" in line:
            verdict, name = line.split("|", 1)
            answers[name] = verdict
    assert len(answers) == 9, f"expected every query to answer: {proc.stdout}"

    # Real members. If these are OUT the set is simply broken, and the
    # absences below would pass for the wrong reason.
    assert answers["alpha-one"] == "IN"
    assert answers["beta"] == "IN"
    assert answers["star*name"] == "IN"

    # Near misses. Each is a substring of a real member, at a different
    # position -- prefix, suffix, interior -- because a membership test built
    # on `case`/`[[` patterns can get the anchors right at one end only.
    assert answers["alpha"] == "OUT"
    assert answers["one"] == "OUT"
    assert answers["eta"] == "OUT"

    # Glob metacharacters in the *query* must be literal. Unquoted, `*` would
    # match every member and report that any name is managed -- which on the
    # `operator stop` path means killing sessions the operator does not own.
    assert answers["*"] == "OUT"
    assert answers["star?name"] == "OUT"


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
        f"set_contains() {{\n{_shell_function('set_contains')}}}\n"
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
        f"set_contains() {{\n{_shell_function('set_contains')}}}\n"
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


def test_operator_sh_uses_no_bash_4_only_declarations():
    """A tripwire, and deliberately a textual one.

    This is the shape of test that is usually wrong -- pinning a *word* and
    calling it a behaviour. It is exact here for one reason: `-A` is not a
    proxy for the incompatibility, it *is* the token bash 3.2 rejects. There
    is no way to write an associative array that this misses and no way to
    trip it without writing one.

    It earns its place because the third associative array lived in
    `start_copilot_in_tmux`'s recovery branch, which only runs when RESTART_DIR
    is deleted between startup and launch. The tests above run the two commands
    whose bodies can be reached; this covers the one whose body, in practice,
    cannot.
    """
    offenders = [
        (n, line.strip())
        for n, line in enumerate(OPERATOR_SH.read_text(encoding="utf-8").splitlines(), 1)
        if ("local -A" in line or "declare -A" in line)
        and not line.strip().startswith("#")
    ]
    assert not offenders, (
        "operator.sh declares an associative array, which is a bash 4 feature "
        f"and `declare: -A: invalid option` on the macOS default bash: {offenders}")
