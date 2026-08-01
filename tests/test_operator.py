"""Tests for the operator CLI."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import copilot_operator as op


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Point all module-level state at a temp directory."""
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    return tmp_path


# ── instance identity ───────────────────────────────────────────
def test_instance_uses_safe_id_for_files():
    inst = op.Instance("my:proj")
    assert ":" not in inst.id
    assert inst.session == inst.id
    assert inst.display_name == "my:proj"


def test_distinct_names_get_distinct_state_files():
    a, b, c = op.Instance("a.b"), op.Instance("a:b"), op.Instance("a-b")
    paths = {a.state_file, b.state_file, c.state_file}
    assert len(paths) == 3


def test_simple_name_is_unchanged():
    assert op.Instance("frontend").id == "frontend"


# ── persisted state ─────────────────────────────────────────────
def test_state_roundtrip():
    inst = op.Instance("proj")
    inst.save_state(4, "2026-07-27T10:00:00Z", "3f2a9c1e-1111-2222-3333-444455556666")
    state = inst.load_state()
    assert state["SESSION_NUM"] == "4"
    assert state["RUN_STARTED"] == "2026-07-27T10:00:00Z"
    assert state["COPILOT_SESSION_ID"] == "3f2a9c1e-1111-2222-3333-444455556666"


def test_state_omits_blank_session_id():
    inst = op.Instance("proj")
    inst.save_state(1, "2026-07-27T10:00:00Z")
    assert "COPILOT_SESSION_ID" not in inst.state_file.read_text(encoding="utf-8")


def test_load_state_absent_returns_none():
    assert op.Instance("nothing").load_state() is None


def test_read_session_id_rejects_non_uuid():
    inst = op.Instance("proj")
    inst.session_file.write_text("not-a-uuid", encoding="utf-8")
    assert inst.read_session_id() == ""


def test_read_session_id_accepts_uuid():
    inst = op.Instance("proj")
    inst.session_file.write_text(
        "3f2a9c1e-1111-2222-3333-444455556666", encoding="utf-8")
    assert inst.read_session_id() == "3f2a9c1e-1111-2222-3333-444455556666"


# ── ownership ───────────────────────────────────────────────────
def test_claim_records_token_and_display_name():
    """An empty marker cannot prove which process owns a session."""
    inst = op.Instance("my.proj")
    inst.claim("tok123")
    owner = inst.ownership()
    assert owner["token"] == "tok123"
    assert owner["display_name"] == "my.proj"
    assert owner["session"] == inst.id


def test_ownership_none_when_unclaimed():
    assert op.Instance("unowned").ownership() is None


def test_managed_instances_lists_claimed(isolated_state):
    inst = op.Instance("alpha")
    inst.claim("t")
    found = op.managed_instances()
    assert inst.id in found
    assert found[inst.id]["display_name"] == "alpha"


def test_cleanup_removes_transient_files_but_keeps_state():
    inst = op.Instance("beta")
    inst.claim("t")
    inst.save_state(2, "2026-07-27T10:00:00Z")
    inst.restart_marker.touch()
    inst.pid_file.write_text("1", encoding="utf-8")
    inst.cleanup_files()
    assert not inst.managed_file.exists()
    assert not inst.restart_marker.exists()
    assert not inst.pid_file.exists()
    # State survives so a named loop can auto-continue after a restart.
    assert inst.state_file.exists()


# ── argument handling ───────────────────────────────────────────
@pytest.mark.parametrize("args,expected", [
    (["--agent", "anvil:anvil"], "anvil:anvil"),
    (["--agent=custom:x"], "custom:x"),
    (["--yolo"], "anvil:anvil"),
])
def test_extract_agent_from_args(args, expected):
    assert op.extract_agent_from_args(args) == expected


@pytest.mark.parametrize("args,expected", [
    (["--resume=abc"], True),
    (["--resume"], True),
    (["--continue"], True),
    (["--connect=x"], True),
    (["--yolo"], False),
    (["--resumearg"], False),
])
def test_args_have_explicit_session(args, expected):
    assert op.args_have_explicit_session(args) is expected


@pytest.mark.parametrize("args,expected", [
    (["--agent", "x"], True), (["--agent=x"], True), (["--model", "y"], False),
])
def test_has_agent_flag(args, expected):
    assert op.has_agent_flag(args) is expected


# ── extensions load only in experimental mode ───────────────────
@pytest.mark.parametrize("defaults", [
    ["--yolo"],
    [],
    ["--yolo", "--no-experimental"],
])
def test_with_experimental_always_adds_the_flag(defaults):
    """Unconditionally, and last, so anything appended after it wins.

    Deciding by inspecting the caller's arguments is what the first version of
    this did, and it could not tell a flag from a value: `-p --no-experimental`
    reads as a ruling and suppressed the injected flag.
    """
    assert op.with_experimental(defaults) == [*defaults, "--experimental"]


def _capture_launch_args(monkeypatch):
    """Record the argv the operator would hand to copilot."""
    seen = []

    def fake_start_session(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen.append(args)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", fake_start_session)
    monkeypatch.setattr(op, "handle_existing_session", lambda instance: None)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    return seen


def _run_single(monkeypatch, args, headless: bool = False):
    seen = _capture_launch_args(monkeypatch)
    monkeypatch.setattr(op.MUX, "attach", lambda session: None)
    monkeypatch.setattr(op.MUX, "has_session", lambda session: False)
    monkeypatch.setattr(op, "wait_for_exit", lambda instance, timeout=10: True)
    op.run_single_session(op.Instance("exp-single"), args, headless=headless)
    assert seen, "the session never launched, so nothing about its args was tested"
    return seen[0]


def test_single_session_launches_copilot_in_experimental_mode(monkeypatch):
    """Runtime extensions load only in experimental mode.

    Without the flag the CLI loads no extensions AND reports nothing about it,
    so `checkout-guard` is absent in exactly the shape of a guard that ran and
    found the checkout clean. Measured, not assumed: sessions on this machine
    ran over an hour with no guard in the shared primary checkout.
    """
    assert _run_single(monkeypatch, []).count("--experimental") == 1


def test_loop_mode_launches_copilot_in_experimental_mode(monkeypatch):
    seen = _capture_launch_args(monkeypatch)

    op.run_loop_mode(op.Instance("exp-loop"), ["--agent", "test:agent"], is_fresh=True)

    assert seen, "the loop never launched, so nothing about its args was tested"
    assert seen[0].count("--experimental") == 1


@pytest.mark.parametrize("mode", ["single", "loop"])
def test_an_explicit_no_experimental_comes_after_the_injected_flag(monkeypatch, mode):
    """Control: the operator supplies a default it does not force.

    The CLI resolves conflicting spellings last-wins -- measured against CLI
    1.0.77, both orders -- so the opt-out only survives if the user's argument
    is positioned after the injected one. Asserting on order rather than on
    absence is what makes this a real control now that injection is
    unconditional.
    """
    if mode == "single":
        launched = _run_single(monkeypatch, ["--no-experimental"])
    else:
        seen = _capture_launch_args(monkeypatch)
        op.run_loop_mode(op.Instance("exp-loop-off"),
                         ["--agent", "test:agent", "--no-experimental"], is_fresh=True)
        assert seen, "the loop never launched, so nothing about its args was tested"
        launched = seen[0]

    assert launched.count("--experimental") == 1
    assert launched.index("--experimental") < launched.index("--no-experimental")


@pytest.mark.parametrize("mode", ["single", "loop"])
@pytest.mark.parametrize("value_flag", ["-p", "-i", "--prompt"])
def test_a_ruling_shaped_option_value_does_not_suppress_the_flag(
        monkeypatch, mode, value_flag):
    """Regression: `-p --no-experimental` is a prompt, not a decision.

    The first version of this feature scanned every forwarded token, so a
    value that merely looked like a ruling silently cancelled the injected
    flag -- putting the session back in the guardless state with no signal,
    which is the exact failure this feature exists to abolish.
    """
    user_args = [value_flag, "--no-experimental"]
    if mode == "single":
        launched = _run_single(monkeypatch, user_args)
    else:
        seen = _capture_launch_args(monkeypatch)
        op.run_loop_mode(op.Instance("exp-loop-val"),
                         ["--agent", "test:agent", *user_args], is_fresh=True)
        assert seen, "the loop never launched, so nothing about its args was tested"
        launched = seen[0]

    assert launched.count("--experimental") == 1
    assert launched.index("--experimental") < launched.index(value_flag)


# ── operator.sh must default the same way ───────────────────────
#
# Everything above tests the Python operator. `operator.sh` is what actually
# runs on Linux and macOS, it received the same change, and CI checks it with
# `bash -n` only -- which proves it parses and says nothing about what it
# launches. So the flag could be dropped from the shell path while every job
# stayed green: an extension outage on two platforms, reported by nothing, in
# exactly the shape this change exists to abolish.
OPERATOR_SH = Path(__file__).resolve().parent.parent / "operator.sh"

# The interpreter macOS ships, and the only one on any CI leg that is bash
# 3.2. It is addressed by absolute path rather than looked up, because a
# lookup answers "a bash" and every claim these tests make needs "the bash a
# macOS user will actually run this under" -- see `_bash_executable`.
MACOS_SYSTEM_BASH = Path("/bin/bash")


def _bash_executable() -> str | None:
    """A bash that can actually run a script out of a native temp directory.

    On Windows, `bash` on PATH is `System32\\bash.exe` -- the WSL launcher.
    On this class of machine it cannot reach `/mnt/c` at all, and it has been
    observed exiting 0 for a script whose transfer had failed: a false OK,
    which is the one failure mode these tests exist to refuse. Git for Windows
    ships a real msys bash that takes native paths, so prefer it, and fall
    back to PATH only where PATH bash is the genuine article.

    On macOS, prefer `/bin/bash` explicitly. Everything these tests assert
    about bash 3.2 -- see `tests/test_operator_sh_bash32.py`, whose docstring
    calls the macOS runners "the bash 3.2 canary" -- rests on the interpreter
    chosen here being Apple's frozen 3.2, and `shutil.which` does not promise
    that: it promises the first `bash` on PATH. Homebrew's is 5.x and installs
    ahead of `/bin` on a developer's machine, and a runner image is free to do
    the same. That substitution changes nothing observable -- the suite stays
    green, because bash 5 runs everything 3.2 runs -- so the only execution
    coverage of 3.2 this repository has would leave without a failing test
    anywhere. Naming the path makes the choice a decision rather than a
    coincidence, and `test_macos_runs_these_tests_under_the_bash_apple_ships`
    makes its loss loud. The executability probe is not ceremony: a path that
    exists but cannot be run would be returned as "a bash that can actually
    run a script" and turn every test in that file into a spawn error, where
    falling through to PATH gets the suite a working interpreter instead.
    """
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        git_bash = program_files / "Git" / "bin" / "bash.exe"
        return str(git_bash) if git_bash.is_file() else None
    if (sys.platform == "darwin" and MACOS_SYSTEM_BASH.is_file()
            and os.access(MACOS_SYSTEM_BASH, os.X_OK)):
        return str(MACOS_SYSTEM_BASH)
    return shutil.which("bash")


def _bash_version(executable: str) -> tuple[int, int] | None:
    """``(major, minor)`` of `executable`, or ``None`` if it could not be asked.

    ``None`` means the question failed, and callers must not read it as any
    particular version: an interpreter that cannot be interrogated is not
    evidence of coverage, it is the absence of evidence.
    """
    try:
        proc = subprocess.run(
            [executable, "-c",
             'printf "%s %s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


bash = pytest.mark.skipif(_bash_executable() is None,
                          reason="no bash that can run a native-path script")


def _shell_function(name: str) -> str:
    """The body of a top-level ``name() {`` ... ``}`` function in operator.sh.

    Reads the real shipped source rather than a copy of it, so these tests
    cannot go on passing against a function the script no longer contains.
    """
    text = OPERATOR_SH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}$",
                      text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in operator.sh"
    return match.group(1)


@pytest.mark.parametrize("function", ["run_single_session", "run_loop_mode"])
def test_operator_sh_injects_experimental_ahead_of_the_user_args(function):
    """Both shell launch paths put the flag in, and put it in *first*.

    Order is the whole mechanism now that injection is unconditional. The CLI
    resolves conflicting spellings last-wins, so `--experimental` ahead of the
    user's arguments is what leaves a real `--no-experimental` able to win. The
    same line appended *after* them would silently make the opt-out
    unexpressible through the operator -- and would still satisfy any test that
    only asked whether the flag was present.

    Parametrised over both functions because they are separate code paths that
    have now been edited separately twice. Covering only the loop would leave a
    plain `operator` on macOS starting sessions with no extensions at all.
    """
    lines = [line for line in _shell_function(function).splitlines()
             if not line.strip().startswith("#")]

    # Every mention in live code, not just the ones that look like the
    # defaults array. A later `copilot_args+=("--experimental")` does not
    # contain `copilot_args=(`, so matching only the construction line would
    # let the flag be re-added *after* the user's arguments -- the precise
    # regression this test exists for -- while still reporting one injection.
    mentions = [i for i, line in enumerate(lines) if '"--experimental"' in line]
    assert len(mentions) == 1, (
        f"{function}() should mention --experimental exactly once, found "
        f"{[lines[i].strip() for i in mentions]}")

    injected = [i for i, line in enumerate(lines)
                if "copilot_args=(" in line and '"--experimental"' in line]
    assert len(injected) == 1, (
        f"{function}() should build copilot_args with --experimental exactly "
        f"once, found {[lines[i].strip() for i in injected]}")

    # Matched on `copilot_args+=(` plus a mention of the user's arguments
    # rather than on one exact spelling: the bash 3.2 guard makes the loop's
    # line `copilot_args+=(${user_args[@]+"${user_args[@]}"})`, and a matcher
    # pinned to the unguarded form would have reported "no forwarding at all"
    # for a line that forwards perfectly well.
    forwarded = [i for i, line in enumerate(lines)
                 if 'copilot_args+=("$@")' in line
                 or ("copilot_args+=(" in line and "user_args[@]" in line)]
    assert len(forwarded) == 1, (
        f"{function}() should append the user's arguments exactly once, "
        f"found {[lines[i].strip() for i in forwarded]}")

    assert injected[0] < forwarded[0], (
        f"{function}() adds --experimental after the user's arguments, so an "
        f"explicit --no-experimental could never win:\n"
        f"  {lines[injected[0]].strip()}\n  {lines[forwarded[0]].strip()}")


def _shell_helper_names() -> list[str]:
    """Every top-level function operator.sh defines.

    Used to stub the whole helper surface generically, so the probe below
    cannot go stale as the script grows: anything the code under test calls is
    by definition defined here. Enumerating instead of hand-listing also keeps
    it working on the bash 3.2 that ships with macOS, which has no
    `command_not_found_handle` to lean on.
    """
    names = re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{",
                       OPERATOR_SH.read_text(encoding="utf-8"), re.MULTILINE)
    assert names, "no functions found in operator.sh -- the extraction missed"
    return names


# Distinct from any exit status the script itself produces, so "the probe
# reached the launch" cannot be confused with "the probe fell over early".
_LAUNCHED = 7


def _shell_launch_argv(function: str, user_argv: list[str], tmp_path: Path) -> list[str]:
    """The argv `operator.sh` really launches with, taken at the call site.

    Runs the function's *entire* body against stubbed helpers, and captures
    what arrives at `generate_run_script` -- the point where the arguments
    stop being the operator's business and become the CLI's. An earlier
    version of this reconstructed the array from just the assignment lines,
    which was a restatement of the code rather than a run of it: any argv
    assembled further down was invisible to it. `run_loop_mode` already does
    exactly that, building `launch_args` from `copilot_args` inside the
    session loop, so the reconstruction was checking something the script does
    not launch with.

    `set -euo pipefail` matches operator.sh's own line 28, so the probe is no
    laxer than the script it is quoting.
    """
    stubs = "\n".join(f"{name}() {{ return 0; }}"
                      for name in _shell_helper_names() if name != function)
    script = tmp_path / "argv.sh"
    script.write_text(
        "set -euo pipefail\n"
        # Enough state for `set -u` to let the real body run.
        'INSTANCE_NAME=probe\nTMUX_SESSION=probe\nRUN_SCRIPT=run\n'
        'RESTART_DIR=.\nRESTART_MARKER=marker\nMAX_SESSIONS=1\n'
        'POLL_INTERVAL=1\nIS_FRESH=true\nIS_LOOP_MODE=false\n'
        'SCRIPT_PREAMBLE=""\nOPERATOR_RUN_STARTED=""\nCURRENT_SESSION_NUM=0\n'
        'CURRENT_COPILOT_SESSION_ID=""\n'
        f"{stubs}\n"
        # `seq` is not guaranteed in a minimal msys; the loop must not iterate
        # zero times and report a clean exit, which would test nothing.
        'seq() { local i=$1; while [ "$i" -le "$2" ]; do echo "$i"; i=$((i+1)); done; }\n'
        'extract_agent_from_args() { printf "%s\\n" "anvil:anvil"; }\n'
        f'generate_run_script() {{ printf "%s\\n" "$@"; exit {_LAUNCHED}; }}\n'
        f"{function}() {{\n{_shell_function(function)}}}\n"
        f'{function} "$@"\n',
        encoding="utf-8", newline="\n")
    proc = subprocess.run([_bash_executable(), "argv.sh", *user_argv], cwd=tmp_path,
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == _LAUNCHED, (
        f"{function}() never reached generate_run_script (exit "
        f"{proc.returncode}), so nothing about its launch args was tested:\n"
        f"{proc.stderr}")
    return proc.stdout.splitlines()


def _run_loop(monkeypatch, args: list[str]) -> list[str]:
    seen = _capture_launch_args(monkeypatch)
    op.run_loop_mode(op.Instance("exp-shape-loop"), list(args), is_fresh=True)
    assert seen, "the loop never launched, so nothing about its args was tested"
    return seen[0]


def _shell_dispatch(argv: list[str], tmp_path: Path) -> tuple[str, list[str]]:
    """Which session function `operator.sh` calls, and with what.

    Runs `main()`'s real body -- argument parsing and all -- and captures the
    hand-off to `run_single_session` / `run_loop_mode`. `_shell_launch_argv`
    above starts *inside* those functions, so the dispatch itself was the one
    part of the launch path with no coverage at all, and it is where a bare
    `operator` with no arguments of its own is decided.

    `tmux`, `sqlite3` and `python3` are stubbed as shell functions rather than
    installed: `command -v` finds a function, so main's dependency checks pass
    on a machine that has none of them.
    """
    stubs = "\n".join(f"{name}() {{ return 0; }}"
                      for name in _shell_helper_names() if name != "main")
    script = tmp_path / "dispatch.sh"
    script.write_text(
        "set -euo pipefail\n"
        'IS_FRESH=false\nSTATE_FILE=state\n'
        f"{stubs}\n"
        'tmux() { return 1; }\nsqlite3() { return 0; }\npython3() { return 0; }\n'
        'sanitize_session_name() { printf "%s\\n" "probe"; }\n'
        # `printf "%s\n" "$@"` with no arguments still prints one empty line,
        # which would read back as a forwarded empty string and make "no
        # arguments" indistinguishable from "one blank argument".
        f'run_single_session() {{ printf "single\\n"; [ "$#" -eq 0 ] || printf "%s\\n" "$@"; exit {_LAUNCHED}; }}\n'
        f'run_loop_mode() {{ printf "loop\\n"; [ "$#" -eq 0 ] || printf "%s\\n" "$@"; exit {_LAUNCHED}; }}\n'
        f"main() {{\n{_shell_function('main')}}}\n"
        'main "$@"\n',
        encoding="utf-8", newline="\n")
    proc = subprocess.run([_bash_executable(), "dispatch.sh", *argv], cwd=tmp_path,
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == _LAUNCHED, (
        f"main() never reached a session function (exit {proc.returncode}), so "
        f"nothing about its dispatch was tested:\n{proc.stderr}")
    lines = proc.stdout.splitlines()
    return lines[0], lines[1:]


@bash
@pytest.mark.parametrize("argv, expected_mode", [
    ([], "single"),
    (["--loop"], "loop"),
])
def test_operator_sh_starts_a_session_when_given_no_arguments_of_its_own(
        argv, expected_mode, tmp_path):
    """A bare `operator` and a bare `operator --loop` must actually start.

    This looks like it cannot fail, and on bash 4.4 and later it cannot. On
    the bash 3.2 that macOS still ships, an empty array is *unset* rather than
    set-and-empty, so the `"${copilot_args[@]}"` these two dispatch lines used
    to carry was an unbound-variable error under `set -u` -- not zero words.
    The plainest invocation the script has died before starting a session, on
    the platform `operator.sh` exists to serve, and nothing caught it because
    every existing shell test passed arguments.

    Parametrised over the two dispatch lines because they are separate
    expansions: fixing one and not the other would leave `operator --loop`
    broken while `operator` worked, which is exactly the kind of half-repair a
    single-case test blesses.
    """
    mode, forwarded = _shell_dispatch(argv, tmp_path)

    assert mode == expected_mode, (
        f"operator.sh {argv} started a {mode} session, expected {expected_mode}")
    assert forwarded == [], (
        f"operator.sh {argv} invented arguments the user did not pass: {forwarded}")


@bash
def test_operator_sh_forwards_its_unrecognised_arguments_to_the_session(tmp_path):
    """The other half of the guarded expansion: it must still pass things on.

    A guard written as `${a[@]:-}` instead of `${a[@]+"${a[@]}"}` would fix the
    empty case and quietly substitute an empty *word* -- so this asserts the
    non-empty case still arrives intact, and intact means unsplit: the second
    argument here contains a space precisely because an unquoted guard would
    tear it in two.
    """
    mode, forwarded = _shell_dispatch(
        ["--agent", "anvil:anvil", "--model", "claude opus"], tmp_path)

    assert mode == "single"
    assert forwarded == ["--agent", "anvil:anvil", "--model", "claude opus"], (
        f"operator.sh mangled the arguments it forwards: {forwarded}")


@bash
def test_both_operators_inject_identical_single_session_defaults(tmp_path,
                                                                 monkeypatch):
    """Not merely the same shape -- the same list, element for element.

    The shape test below is deliberately loose because it also covers loop
    mode, where the two legitimately differ (`operator.sh` resolves the agent
    name itself). Attached single session has no such licence: every injected
    argument is a decision about how the CLI behaves for the user, and there
    is no reason any of them should depend on which platform they are on.
    `--yolo` is why this test exists -- the Python operator injected it here
    and the shell operator did not, so the same command granted an agent
    blanket approval on Windows and not on Linux, for months, with nothing in
    either program that would ever have said so.

    Loose where the general case demands it, exact where exactness is
    available.
    """
    shell_argv = _shell_launch_argv("run_single_session", [], tmp_path)
    python_argv = _run_single(monkeypatch, [])

    assert shell_argv == python_argv, (
        "the two operators disagree about what a single session grants:\n"
        f"  operator.sh:          {shell_argv}\n"
        f"  copilot_operator.py:  {python_argv}")


@bash
def test_operator_sh_grants_blanket_approval_only_in_loop_mode(tmp_path):
    """The property itself, run rather than read.

    This is the invariant `run_single_session`'s headless branch has to not
    break: on the shell side, blanket approval belongs to loop mode and
    nowhere else. Asserting it directly is better than asserting the premise
    it used to rest on -- "operator.sh has no headless mode" is true of that
    script's vocabulary but not of its behaviour, since a single session whose
    `tmux attach` fails (no TTY: a wrapper, CI, a nested tmux) keeps running
    with nobody attached. That path grants no `--yolo`, and neither does
    `copilot_operator.py` on the same path, so the two still agree; what this
    refuses is the shell quietly starting to grant it somewhere else.
    """
    single = _shell_launch_argv("run_single_session", [], tmp_path)
    loop = _shell_launch_argv("run_loop_mode", [], tmp_path)

    assert "--yolo" not in single, single
    assert "--no-ask-user" not in single, single
    assert "--yolo" in loop, loop
    assert "--no-ask-user" in loop, loop


_ATTACHED = 9


@bash
def test_operator_sh_always_attaches_its_single_session(tmp_path):
    """Why the headless branch is Python-only, asserted as behaviour.

    Granting blanket approval when headless is defensible as a difference the
    shell does not have to match *because the shell cannot express the
    request*: it has no mode that deliberately launches a single session and
    leaves it running with nobody attached. This runs the shell function's
    real body and asserts it always reaches `tmux attach`. A reviewer was
    right that grepping for the word "headless" tests a word, not a behaviour
    -- a `--detached` mode (which is precisely the spelling
    `copilot_operator.py` accepts as a synonym) would sail past it.

    Note what this deliberately does NOT claim. The attach is best-effort
    (`|| true`), so it can fail and leave a live unattended session -- but
    that is an environment removing the terminal, not a mode, and
    `copilot_operator.py` does the identical thing there (`MUX.attach` returns
    a code rather than raising, and the next branch prints "Detached from
    copilot session."). Neither grants `--yolo` on that path, so it is a
    shared property. What would be a real divergence is the shell gaining a
    deliberate unattended launch, and that is what this refuses.
    """
    stubs = "\n".join(f"{name}() {{ return 0; }}"
                      for name in _shell_helper_names()
                      if name != "run_single_session")
    script = tmp_path / "attach.sh"
    script.write_text(
        "set -euo pipefail\n"
        'INSTANCE_NAME=probe\nTMUX_SESSION=probe\nRUN_SCRIPT=run\n'
        'RESTART_DIR=.\nOPERATOR_RUN_STARTED=""\nSCRIPT_PREAMBLE=""\n'
        f"{stubs}\n"
        # Shadow the real binary: a shell function wins over PATH lookup.
        # Exiting here cannot be swallowed by the `|| true` on the attach,
        # because `exit` leaves the shell rather than returning a status.
        'tmux() { if [ "${1:-}" = "attach" ]; then exit '
        f'{_ATTACHED}; fi; return 0; }}\n'
        f"run_single_session() {{\n{_shell_function('run_single_session')}}}\n"
        'run_single_session\n',
        encoding="utf-8", newline="\n")
    proc = subprocess.run([_bash_executable(), "attach.sh"], cwd=tmp_path,
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=60)
    assert proc.returncode == _ATTACHED, (
        "operator.sh's run_single_session did not reach `tmux attach` (exit "
        f"{proc.returncode}). If it has gained a way to launch a single "
        "session and leave it unattended, decide deliberately whether that "
        "mode grants --yolo and assert it against copilot_operator.py's "
        f"headless branch -- do not just delete this test.\n{proc.stderr}")


def test_operator_sh_does_not_mention_an_unattended_single_session_mode():
    """A tripwire, not a control -- and worth having as long as it is labelled.

    The two tests above are the real guarantees; this one runs even where no
    bash does, and catches the cheapest way the premise could rot: someone
    adding a deliberate unattended mode, or a comment planning one, in a CI
    lane where the behavioural probes are skipped rather than run. Both
    spellings, because `copilot_operator.py` accepts `--detached` as a synonym
    for `--headless` and a port would plausibly use either.
    """
    text = OPERATOR_SH.read_text(encoding="utf-8").lower()
    found = [word for word in ("headless", "--detached") if word in text]
    assert not found, (
        f"operator.sh now mentions {found}. If it has gained a way to launch "
        "a single session and leave it unattended, decide deliberately "
        "whether that mode grants --yolo and --no-ask-user, and assert it "
        "against copilot_operator.py's headless branch -- do not just delete "
        "this test.")


@bash
@pytest.mark.parametrize("mode", ["single", "loop"])
@pytest.mark.parametrize("user_argv", [
    ["--no-experimental"],
    # Values that merely look like a ruling. These are what broke the previous
    # implementation, and they are the cases most likely to be fixed in one
    # language and not the other.
    ["-p", "--no-experimental"],
    ["-i", "--no-experimental"],
    ["--", "--no-experimental"],
])
def test_both_operators_build_the_same_shaped_launch_argv(tmp_path, monkeypatch,
                                                          mode, user_argv):
    """The shell and Python operators must agree about the launch argv.

    Asserted as a shared property rather than a fixed list, because the two
    legitimately inject different defaults in loop mode -- `operator.sh`
    resolves the agent name itself. (In single session there is no such
    licence, and the argv is asserted identical above.) What must never differ
    in either mode is where `--experimental` sits relative to the user's own
    arguments, because that is what decides whether an opt-out is expressible
    at all.

    Running the shell's own source is the point: a Linux or macOS regression
    here is otherwise invisible to a suite that only ever drives the Python
    operator, and CI checks `operator.sh` with `bash -n`, which proves it
    parses and nothing whatever about what it passes.
    """
    if mode == "single":
        shell_argv = _shell_launch_argv("run_single_session", user_argv, tmp_path)
        python_argv = _run_single(monkeypatch, list(user_argv))
    else:
        shell_argv = _shell_launch_argv("run_loop_mode", user_argv, tmp_path)
        python_argv = _run_loop(monkeypatch, list(user_argv))

    for label, argv in (("operator.sh", shell_argv),
                        ("copilot_operator.py", python_argv)):
        assert argv.count("--experimental") == 1, f"{label}: {argv}"
        # The user's arguments survive intact, at the tail, in order.
        assert argv[-len(user_argv):] == user_argv, f"{label}: {argv}"
        # ...and the injected flag is in the head, so anything the user passed
        # is seen by the CLI later and therefore wins.
        assert "--experimental" in argv[:-len(user_argv)], f"{label}: {argv}"


# ── preamble ────────────────────────────────────────────────────
def test_preamble_is_platform_neutral():
    """The preamble is read by an agent that may be on Windows, so it must not
    prescribe a POSIX-only command such as `touch`."""
    text = op.build_preamble("anvil:anvil", op.Instance("proj"))
    assert "touch " not in text
    assert "handoff --instance proj" in text


def test_preamble_uses_display_name_not_internal_id():
    text = op.build_preamble("a:b", op.Instance("my.proj"))
    assert "my.proj" in text


def test_preamble_omits_crash_note_by_default():
    text = op.build_preamble("anvil:anvil", op.Instance("proj"))
    assert "crash" not in text.lower()


def test_preamble_includes_crash_note_when_requested():
    text = op.build_preamble("anvil:anvil", op.Instance("proj"), crash_recovery=True)
    assert "handoff file could not be found" in text
    assert "crash" in text.lower()


# ── project handoff file resolution ─────────────────────────────
def test_project_handoff_file_none_without_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "project_catalog_path", lambda: tmp_path / "missing.csv")
    assert op.project_handoff_file(tmp_path) is None


def test_project_handoff_file_resolves_from_catalog(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(f'"{project_root}",abc-123\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    result = op.project_handoff_file(project_root)

    assert result is not None
    assert result.name == "next-session.md"
    assert "abc-123" in str(result)


def test_project_handoff_file_none_when_not_in_catalog(tmp_path, monkeypatch):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text('"C:\\some\\other\\dir",xyz-999\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    assert op.project_handoff_file(project_root) is None


@pytest.mark.parametrize("guid", ["../../elsewhere", "..", "a/b", "a\\b"])
def test_project_handoff_file_never_resolves_outside_the_projects_root(
        tmp_path, monkeypatch, guid):
    """The reader must refuse the ids the writer refuses to create.

    Before this guard, a catalog id of `../../elsewhere` produced
    `~/.copilot/projects/../../elsewhere/next-session.md`, which resolves two
    levels above the projects root -- a file the operator would then report on
    and the next agent would read and delete.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(f'"{project_root}",{guid}\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    found = op.project_handoff_file(project_root)

    assert found is None, f"escaped to {found.resolve() if found else None}"


def test_project_handoff_file_rejects_a_windows_trailing_dot_id(tmp_path, monkeypatch):
    """`victim.` and `victim` are one directory on Windows.

    A plausible typo, not an attack: it would silently read and delete a
    different project's handoff.
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(f'"{project_root}",victim.\n', encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    assert op.project_handoff_file(project_root) is None


def test_project_handoff_file_reader_and_writer_agree_on_validity(tmp_path, monkeypatch):
    """One definition of a valid id, not two that drift apart."""
    import handoff_tool

    assert op.guid_is_usable is handoff_tool.guid_is_usable

    project_root = tmp_path / "proj"
    project_root.mkdir()
    catalog = tmp_path / "catalog.csv"
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)
    for guid in ("", "..", "../x", "victim.", "bad:stream", "CON"):
        catalog.write_text(f'"{project_root}",{guid}\n', encoding="utf-8")
        assert not handoff_tool.guid_is_usable(guid)
        assert op.project_handoff_file(project_root) is None, \
            f"reader accepted {guid!r} that the writer rejects"


def test_project_handoff_file_still_accepts_a_real_guid(tmp_path, monkeypatch):
    """The guard must not break the entries that work today."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    catalog = tmp_path / "catalog.csv"
    catalog.write_text(
        f'"{project_root}",a1b2c3d4-e5f6-7890-abcd-ef1234567890\n',
        encoding="utf-8")
    monkeypatch.setattr(op, "project_catalog_path", lambda: catalog)

    found = op.project_handoff_file(project_root)

    assert found is not None
    assert found.parent.name == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


# ── --yolo is granted only where nobody can be asked ─────────────
def test_attached_single_session_does_not_grant_yolo(monkeypatch):
    """Your terminal is attached, so you are there to answer.

    `--yolo` waives every approval prompt for the life of the session. Here
    the terminal goes straight back to the human who typed the command, so
    granting it buys nothing and spends the one control this mode still has.
    """
    argv = _run_single(monkeypatch, [])
    assert "--yolo" not in argv, argv
    assert "--no-ask-user" not in argv, argv


def test_headless_single_session_grants_yolo_and_no_ask_user(monkeypatch):
    """Nothing attaches, so there is nobody to answer anything.

    `operator join` is an invitation the user may never accept. Without these
    a headless session does not degrade to "more prompts" -- it blocks on the
    first question indefinitely, while looking exactly like a session doing
    long work: live process, live pane, no error anywhere. The lower-authority
    option is the one that fails silently here, which is why the ruling goes
    the other way from the attached case.

    Both flags, because they close different mouths: `--yolo` waives the
    approvals the CLI asks for before acting, `--no-ask-user` stops the agent
    asking a question of its own accord. Granting only the first leaves the
    identical hang reachable through `ask_user` -- which is why loop mode, the
    other unattended mode, has always injected both.
    """
    argv = _run_single(monkeypatch, [], headless=True)
    assert "--yolo" in argv, argv
    assert "--no-ask-user" in argv, argv


def test_headless_and_attached_single_sessions_do_not_agree_about_yolo(monkeypatch):
    """Asserted together so the two cannot quietly drift into one answer.

    Separately, either test can be "fixed" by editing it to match whichever
    behaviour someone changed first, and the pair would still be green. The
    distinction is the whole ruling: it is not that prompts are good or bad,
    it is that a prompt with nobody to answer it is a silent hang.
    """
    attached = _run_single(monkeypatch, [])
    headless = _run_single(monkeypatch, [], headless=True)
    for flag in ("--yolo", "--no-ask-user"):
        assert (flag in headless) and (flag not in attached), (
            f"{flag}\nattached={attached}\nheadless={headless}")


def test_an_attached_single_session_still_honours_a_user_asking_for_yolo(monkeypatch):
    """Not granting it by default is not the same as refusing it.

    It lands after the injected defaults, which is what makes it expressible
    at all -- the same last-wins ordering `--experimental` relies on.
    """
    argv = _run_single(monkeypatch, ["--yolo"])
    assert argv[-1] == "--yolo"


def test_loop_mode_still_grants_yolo(monkeypatch):
    """The asymmetry between the modes is the ruling, not an oversight.

    Both operators inject it here. If this ever fails, an unattended loop is
    one approval prompt away from stalling with nobody to notice.
    """
    assert "--yolo" in _run_loop(monkeypatch, [])


# ── launch spec ─────────────────────────────────────────────────
def test_launch_spec_roundtrip(tmp_path):
    inst = op.Instance("proj")
    argv = ["copilot", "--yolo", "-i", "a preamble with 'quotes' and \"more\""]
    path = op.write_launch_spec(inst, argv, tmp_path, 3)
    spec = json.loads(path.read_text(encoding="utf-8"))
    assert spec["argv"] == argv
    assert spec["session_num"] == 3
    assert spec["instance"] == inst.id


def test_runner_argv_is_a_list_not_a_shell_string(tmp_path):
    argv = op.runner_argv(tmp_path / "spec.json")
    assert isinstance(argv, list)
    assert argv[0] == op.sys.executable
    assert argv[1].endswith("operator_runner.py")


# ── reports ─────────────────────────────────────────────────────
def test_report_without_database_is_actionable(capsys):
    assert op.report_metrics("summary") == 1
    assert "No metrics database" in capsys.readouterr().out


def test_unknown_report_type_lists_valid_ones(capsys, isolated_state):
    op.METRICS_DB.write_bytes(b"")
    assert op.report_metrics("bogus") == 1
    out = capsys.readouterr().out
    for kind in ("summary", "sessions", "models", "projects", "costs"):
        assert kind in out


def test_table_renders_headers_and_rows():
    rendered = op._table([("a", 1), ("bb", 22)], ["name", "count"])
    assert "name" in rendered and "count" in rendered
    assert "bb" in rendered


def test_table_handles_no_rows():
    assert op._table([], ["a"]) == "(no data)"


# ── dispatch ────────────────────────────────────────────────────
def test_help_exits_zero(capsys):
    assert op.main(["help"]) == 0
    assert "operator" in capsys.readouterr().out


def test_version(capsys):
    assert op.main(["version"]) == 0
    assert op.__version__ in capsys.readouterr().out


def test_reserved_words_are_not_instance_names():
    for word in ("stop", "list", "report", "ingest", "help", "join", "reload"):
        assert word in op.RESERVED_WORDS


def test_stop_unknown_instance_reports_error(monkeypatch, capsys):
    monkeypatch.setattr(op.MUX, "available", lambda: False)
    assert op.stop_operator("ghost") == 1


def test_stop_with_nothing_running_is_success(monkeypatch, capsys):
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [])
    assert op.stop_operator() == 0
    assert "No running operator instances" in capsys.readouterr().out


def test_list_with_nothing_running(monkeypatch, capsys):
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [])
    assert op.list_instances() == 0
    assert "(none)" in capsys.readouterr().out


def test_list_excludes_foreign_sessions(monkeypatch, capsys):
    """A session the operator did not create must never be listed."""
    inst = op.Instance("mine")
    inst.claim("t")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id, "someone-elses"])
    op.list_instances()
    out = capsys.readouterr().out
    assert "mine" in out
    assert "someone-elses" not in out


def test_stop_all_ignores_foreign_sessions(monkeypatch):
    inst = op.Instance("mine")
    inst.claim("t")
    killed = []
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id, "foreign"])
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s))
    op.stop_operator()
    assert killed == [inst.id]


# ── loop supervisor PID tracking ────────────────────────────────
def test_pid_alive_true_for_current_process():
    assert op._pid_alive(os.getpid()) is True


def test_pid_alive_false_for_bogus_pid():
    assert op._pid_alive(999_999_999) is False


def test_running_loop_pid_none_without_file():
    inst = op.Instance("no-loop")
    assert op._running_loop_pid(inst) is None


def test_running_loop_pid_prunes_stale_entry():
    inst = op.Instance("stale-loop")
    inst.loop_pid_file.write_text("999999999", encoding="utf-8")
    assert op._running_loop_pid(inst) is None
    assert not inst.loop_pid_file.exists()


def test_running_loop_pid_returns_live_pid():
    inst = op.Instance("live-loop")
    inst.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    assert op._running_loop_pid(inst) == os.getpid()


# ── stop-loop / stop-session split ──────────────────────────────
def test_stop_loop_only_without_supervisor_errors():
    assert op.stop_loop_only("nothing-running") == 1


def test_stop_loop_only_requires_name():
    assert op.stop_loop_only(None) == 1


def test_stop_loop_only_touches_detach_marker_and_waits(monkeypatch):
    inst = op.Instance("has-loop")
    inst.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")

    calls = {"n": 0}

    def fake_running_pid(instance):
        calls["n"] += 1
        # Simulate the supervisor exiting after being asked to detach.
        if calls["n"] >= 2:
            return None
        return os.getpid()

    monkeypatch.setattr(op, "_running_loop_pid", fake_running_pid)
    monkeypatch.setattr(op.time, "sleep", lambda s: None)
    rc = op.stop_loop_only("has-loop")
    assert rc == 0
    assert inst.detach_marker.exists()  # unlinked by the supervisor itself, not us
    assert calls["n"] >= 2


def test_stop_session_only_requires_running_session(monkeypatch):
    monkeypatch.setattr(op.MUX, "has_session", lambda s: False)
    assert op.stop_session_only("ghost") == 1


def test_stop_session_only_refuses_foreign_session(monkeypatch, capsys):
    inst = op.Instance("foreign-owned")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(type(inst), "owns_live_session", lambda self: False)
    assert op.stop_session_only("foreign-owned") == 1
    assert "Refusing to stop it" in capsys.readouterr().err


def test_stop_session_only_kills_session_leaves_loop_state(monkeypatch):
    inst = op.Instance("owned-session")
    inst.claim("tok")
    inst.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    killed = []
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: killed.append(s) or True)

    rc = op.stop_session_only("owned-session")

    assert rc == 0
    assert killed == [inst.id]
    # A live supervisor is left in place — it owns relaunching, not us.
    assert inst.loop_pid_file.exists()


def test_stop_session_only_removes_tab_when_no_loop(monkeypatch, tmp_path):
    inst = op.Instance("no-loop-session")
    inst.claim("tok")
    op.register_tab(inst, False, [], tmp_path)
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "kill_session", lambda s: True)

    op.stop_session_only("no-loop-session")

    assert inst.id not in op.load_tabs()


# ── interactive menu ─────────────────────────────────────────────
def _feed(monkeypatch, *answers):
    """Answer successive prompts, then behave like a closed stdin."""
    pending = list(answers)

    def fake_input(prompt=""):
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def test_menu_exits_cleanly_on_blank_input(monkeypatch):
    _feed(monkeypatch, "")
    assert op.show_menu() == 0


def test_menu_dispatches_selected_action(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(op, "browse_instances",
                        lambda: called.__setitem__("n", 1) or 0)
    _feed(monkeypatch, "1", "")
    assert op.show_menu() == 0
    assert called["n"] == 1


def test_menu_returns_to_itself_after_an_action(monkeypatch):
    """One action is not the end of the program — the menu is a loop."""
    seen = {"n": 0}
    monkeypatch.setattr(op, "browse_instances",
                        lambda: seen.__setitem__("n", seen["n"] + 1) or 0)
    _feed(monkeypatch, "1", "1", "")
    assert op.show_menu() == 0
    assert seen["n"] == 2


def test_menu_rejects_out_of_range_choice(monkeypatch, capsys):
    _feed(monkeypatch, "999")
    assert op.show_menu() == 1
    assert "Out of range" in capsys.readouterr().err


# ── session browser ──────────────────────────────────────────────
@pytest.fixture
def running_loop(monkeypatch):
    """A managed instance with both a live session and a live supervisor."""
    inst = op.Instance("proj")
    inst.claim("tok")
    inst.save_state(7, "2026-07-30T09:00:00Z")
    inst.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id])
    monkeypatch.setattr(op.MUX, "has_session", lambda s: s == inst.id)
    return inst


@pytest.mark.parametrize("seconds,expected", [
    (45, "45s"), (90, "1m 30s"), (3660, "1h 1m"), (90000, "1d 1h"),
])
def test_elapsed_formatting(seconds, expected):
    assert op._fmt_elapsed(seconds) == expected


def test_snapshot_reports_loop_iteration_and_ownership(running_loop):
    snap = op.instance_snapshot(running_loop)
    assert snap["session_num"] == 7
    assert snap["loop_pid"] == os.getpid()
    assert snap["session_live"] is True
    assert snap["owned"] is True
    assert "#7" in op._status_label(snap)


def test_snapshot_survives_a_corrupt_state_file(running_loop):
    running_loop.state_file.write_text("SESSION_NUM=banana\n", encoding="utf-8")
    assert op.instance_snapshot(running_loop)["session_num"] == 0


def test_active_instances_includes_loop_with_no_live_session(monkeypatch):
    """Between sessions a loop has no session; it must stay actionable."""
    inst = op.Instance("between")
    inst.claim("tok")
    inst.loop_pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [])
    monkeypatch.setattr(op.MUX, "has_session", lambda s: False)
    assert [i.id for i in op.active_instances()] == [inst.id]


def test_pick_instance_returns_the_chosen_one(running_loop, monkeypatch):
    _feed(monkeypatch, "1")
    picked = op._pick_instance("pick")
    assert picked is not None and picked.id == running_loop.id


def test_pick_instance_cancels_on_blank(running_loop, monkeypatch):
    _feed(monkeypatch, "")
    assert op._pick_instance("pick") is None


def test_detail_view_shows_stats_then_joins(running_loop, monkeypatch, capsys):
    joined = {}

    def fake_join(name):
        joined["name"] = name
        return 0

    monkeypatch.setattr(op, "join_instance", fake_join)
    _feed(monkeypatch, "1")
    assert op.show_instance_detail(running_loop) == 0
    out = capsys.readouterr().out
    assert "Loop session" in out and "#7" in out
    assert "Running for" in out
    assert joined["name"] == "proj"


def test_detail_view_hides_loop_stop_when_no_loop_runs(monkeypatch, capsys):
    inst = op.Instance("solo")
    inst.claim("tok")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "list_sessions", lambda: [inst.id])
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    _feed(monkeypatch, "")
    assert op.show_instance_detail(inst) == 0
    out = capsys.readouterr().out
    assert "Stop the loop" not in out
    assert "Stop the session" in out


def test_detail_view_exits_when_nothing_is_running(monkeypatch, capsys):
    inst = op.Instance("gone")
    inst.claim("tok")
    monkeypatch.setattr(op.MUX, "available", lambda: True)
    monkeypatch.setattr(op.MUX, "has_session", lambda s: False)
    assert op.show_instance_detail(inst) == 0
    assert "no longer running" in capsys.readouterr().out


def test_detail_view_rereads_state_after_a_stop(running_loop, monkeypatch, capsys):
    """After stopping, the view must re-read state rather than re-offer
    actions against something that is already gone."""
    live = {"on": True}
    monkeypatch.setattr(op.MUX, "has_session", lambda s: live["on"])
    monkeypatch.setattr(op.MUX, "list_sessions",
                        lambda: [running_loop.id] if live["on"] else [])

    def fake_stop(name):
        live["on"] = False
        running_loop.loop_pid_file.unlink()
        return 0

    monkeypatch.setattr(op, "stop_operator", fake_stop)
    _feed(monkeypatch, "4")     # Stop everything (loop and session)
    assert op.show_instance_detail(running_loop) == 0
    assert "no longer running" in capsys.readouterr().out


def test_recorded_usage_is_silent_without_a_database():
    assert op._recorded_usage("/nowhere") == ""


def test_is_copilot_running_treats_dead_pane_as_stopped(monkeypatch):
    """Loop mode sets remain-on-exit, so has_session stays true after the
    program exits. Ignoring pane_dead lets the loop poll forever."""
    inst = op.Instance("proj")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda s: True)
    assert op.is_copilot_running(inst) is False


def test_is_copilot_running_true_while_alive(monkeypatch):
    inst = op.Instance("proj")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda s: False)
    assert op.is_copilot_running(inst) is True


def test_is_copilot_running_false_when_exit_marker_present(monkeypatch):
    inst = op.Instance("proj")
    inst.exit_file.write_text("0", encoding="utf-8")
    monkeypatch.setattr(op.MUX, "has_session", lambda s: True)
    monkeypatch.setattr(op.MUX, "pane_dead", lambda s: False)
    assert op.is_copilot_running(inst) is False


def test_reload_without_name_errors(capsys):
    assert op.reload_instance(None) == 1


def test_reload_rebuilds_preamble(tmp_path, isolated_state):
    inst = op.Instance("proj")
    op.write_launch_spec(
        inst, ["copilot", "--agent", "anvil:anvil", "-i", "old preamble"], tmp_path, 1)
    assert op.reload_instance("proj") == 0
    spec = json.loads(inst.spec_file.read_text(encoding="utf-8"))
    assert spec["argv"][-2] == "-i"
    assert "blanket human approval" in spec["argv"][-1]
    assert "--effort" in spec["argv"]
