"""Staleness has to be legible to the agent, not only at the command line.

`operator list` learned to name a supervisor running older code (6d2385c),
which serves a human who went looking. The misinformation lands somewhere
else: 355 launches across the fleet were told "a handoff file could not be
found for this project" by supervisors running code from before that verdict
was decided per launch, and every one of those agents distrusted a perfectly
good handoff and re-derived its predecessor's work. None had any reason to go
and run `operator list`. This is backlog 0011's residue -- the only part of it
that 6d2385c did not already close.
"""
from __future__ import annotations

import builtins
import json
import re
from pathlib import Path

import pytest

import copilot_operator as op
import operator_session
import work_claims

#: Every verdict `loop_code_state` and `own_code_state` can return, and the
#: non-current subset the preamble must caveat.
#:
#: Named once and swept by `test_the_verdict_lists_here_name_every_constant`
#: rather than written out at each `parametrize`. A hand-written list is how a
#: new enum member acquires a silent wrong default: `CODE_MISMATCH` was added
#: to the module long after these sweeps were written, and nothing existing
#: would have failed had it been left out of them -- the sweeps would simply
#: have kept passing over a set that no longer described the code.
ALL_CODE_STATES = (op.CODE_CURRENT, op.CODE_STALE, op.CODE_UNKNOWN,
                   op.CODE_UNRECORDED, op.CODE_MISMATCH)
NON_CURRENT_CODE_STATES = tuple(s for s in ALL_CODE_STATES
                                if s != op.CODE_CURRENT)


def test_the_verdict_lists_here_name_every_constant():
    """The guard that makes the two sweeps below mean what they say.

    Discovers the constants by introspection instead of restating them, so
    adding a `CODE_*` to `copilot_operator` without adding it here fails
    rather than silently narrowing every parametrised sweep in this file.
    """
    declared = {v for k, v in vars(op).items()
                if k.startswith("CODE_") and isinstance(v, str)}

    assert declared == set(ALL_CODE_STATES)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    restart = tmp_path / "restart"
    restart.mkdir(parents=True)
    monkeypatch.setattr(op, "OPERATOR_HOME", tmp_path)
    monkeypatch.setattr(op, "RESTART_DIR", restart)
    monkeypatch.setattr(op, "LOG_FILE", tmp_path / "operator.log")
    monkeypatch.setattr(op, "METRICS_DB", tmp_path / "metrics.db")
    monkeypatch.setattr(op, "COPILOT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(op, "TABS_FILE", tmp_path / "tabs.json")
    monkeypatch.setattr(op, "POLL_INTERVAL", 0)
    monkeypatch.setattr(op, "LAUNCH_BACKOFF_BASE", 0)
    monkeypatch.setattr(op, "RESTART_PAUSE_SECONDS", 0)
    # The fingerprint is cached for the life of the process by design, so a
    # test that computed one would otherwise grade whatever the previous test
    # left behind.
    monkeypatch.setattr(op, "_RUNNING_CODE", None)
    return tmp_path


def _pretend_source(monkeypatch, path: Path, text: str) -> None:
    """Make ``path`` the only operator source this process 'imported'.

    Patching `_loaded_operator_sources` rather than hand-building
    `_RUNNING_CODE` keeps the real caching path under test: the fingerprint is
    computed from the file's actual bytes, exactly as it is at supervisor
    startup. A hand-built cache would test only `_compare_recorded_files` and
    would still pass if `own_code_state` stopped consulting the cache at all.
    """
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(op, "_loaded_operator_sources", lambda modules=None: [path])


# ── the self-check ──────────────────────────────────────────────
def test_own_code_state_is_current_when_the_tree_has_not_moved(tmp_path, monkeypatch):
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "x = 1\n")
    op.running_code_fingerprint()

    assert op.own_code_state() == (op.CODE_CURRENT, [])


def test_own_code_state_is_stale_after_the_loaded_file_changes(tmp_path, monkeypatch):
    """The control for the test above, and the behaviour the whole item is about.

    The fingerprint is taken first -- that is the supervisor importing its
    code -- and the edit lands afterwards, which is a developer fixing the
    operator while a supervisor is already running.
    """
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "x = 1\n")
    op.running_code_fingerprint()

    src.write_text("x = 2\n", encoding="utf-8")

    verdict, changed = op.own_code_state()
    assert verdict == op.CODE_STALE
    assert changed == [str(src)], \
        "the changed file has to be named; 'something changed' is not actionable"


def test_own_code_state_is_stale_when_the_loaded_file_is_deleted(tmp_path, monkeypatch):
    """Absence is a definite difference, not an unreadable one.

    A module the supervisor loaded that is no longer on disk has certainly
    changed, and must not be laundered into "cannot tell" -- which is the
    weaker verdict and would understate it.
    """
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "x = 1\n")
    op.running_code_fingerprint()

    src.unlink()

    assert op.own_code_state()[0] == op.CODE_STALE


def test_own_code_state_cannot_tell_when_the_file_cannot_be_read(tmp_path, monkeypatch):
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "x = 1\n")
    op.running_code_fingerprint()

    real_open = builtins.open

    def guard(file, *args, **kwargs):
        try:
            same = Path(file) == src
        except TypeError:
            same = False
        if same:
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard)

    assert op.own_code_state()[0] == op.CODE_UNKNOWN, \
        "a file nobody could examine cannot support a claim of currency"


def test_own_code_state_never_reports_unrecorded(tmp_path, monkeypatch):
    """`CODE_UNRECORDED` means "the supervisor wrote nothing down", which is
    unreachable here: the in-memory fingerprint always exists. Asserting it
    keeps a later refactor from routing the self-check back through the file
    and quietly reintroducing the failure below."""
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "x = 1\n")
    op.running_code_fingerprint()
    inst = op.Instance("no-record")
    assert not inst.loop_code_file.exists()

    assert op.own_code_state()[0] != op.CODE_UNRECORDED
    assert op.loop_code_state(inst)[0] == op.CODE_UNRECORDED, \
        "control: the on-disk reader does report the absent record"


def test_a_previous_supervisors_record_cannot_issue_an_all_clear(tmp_path, monkeypatch):
    """Why the self-check does not read the file `operator list` reads.

    `_save_loop_code` warns and carries on when it cannot write, by design --
    losing a verdict must never cost the running session. So a supervisor can
    be running while `loop_code` still describes a *previous* supervisor of
    the same instance. Compared against disk, that record reads `current`: a
    confident all-clear sourced from a process that no longer exists, in the
    one instrument built to stop exactly that.
    """
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "old\n")
    op.running_code_fingerprint()          # this supervisor loaded "old"

    src.write_text("new\n", encoding="utf-8")

    inst = op.Instance("orphaned-record")
    # A record left by a later supervisor, describing the bytes now on disk.
    inst.loop_code_file.write_text(json.dumps({
        "version": "1.4.0", "digest": "x",
        "files": [{"path": str(src), "sha256": op._digest_file(src)}],
    }), encoding="utf-8")

    assert op.loop_code_state(inst)[0] == op.CODE_CURRENT, \
        "control: the file-based reader is fooled, which is the point"
    assert op.own_code_state()[0] == op.CODE_STALE, \
        "the process that loaded the code must answer for it"


def test_both_readers_agree_when_given_the_same_entries(tmp_path, monkeypatch):
    """The two verdicts are quoted side by side -- `operator list` prints one
    and the preamble carries the other -- so they must not drift apart."""
    src = tmp_path / "fake_operator.py"
    _pretend_source(monkeypatch, src, "x = 1\n")
    op.running_code_fingerprint()
    src.write_text("x = 2\n", encoding="utf-8")

    inst = op.Instance("agreeing")
    inst.loop_code_file.write_text(
        json.dumps(op.running_code_fingerprint()), encoding="utf-8")

    assert op.loop_code_state(inst) == op.own_code_state()


# ── the preamble clause ─────────────────────────────────────────
def _preamble(**kwargs) -> str:
    return op.build_preamble("anvil:anvil", op.Instance("proj"), **kwargs)


def test_current_code_adds_no_caveat():
    """A caveat on every session is one that stops being read."""
    assert "CAUTION" not in _preamble()
    assert "CAUTION" not in _preamble(code_state=op.CODE_CURRENT)
    assert _preamble() == _preamble(code_state=op.CODE_CURRENT), \
        "the default must be the silent case, byte for byte"


def test_current_code_is_the_default_so_untouched_callers_are_unaffected():
    """`operator NAME` builds a preamble in the process that just imported the
    code, where staleness is impossible by construction."""
    assert "CAUTION" not in op.build_preamble("a:b", op.Instance("one-shot"))


@pytest.mark.parametrize("state", NON_CURRENT_CODE_STATES)
def test_every_non_current_verdict_reaches_the_agent(state):
    """Backlog 0011's question was about a launch that "cannot show it is
    running current code" -- which is three verdicts, not just the definite
    one. `unrecorded` in particular described four of the five live
    supervisors on this machine when the item was corrected.

    Four now. `CODE_MISMATCH` was added later, and a verdict added to the set
    without being added here is the classic way a new enum member acquires a
    silent wrong default -- which for this function would mean an agent
    launched by an undescribable supervisor being told nothing at all.
    """
    assert "CAUTION" in _preamble(code_state=state)


def test_a_mismatched_record_is_not_reported_as_an_absent_one():
    """The generic "cannot show" text says the supervisor "either recorded
    nothing [...] or that record could not be compared". Both are false for
    `CODE_MISMATCH`: a record was read and it names a different process.
    Reporting the two in the same words is the enum-extension defect -- a new
    member silently acquiring an existing branch's wrong prose, which
    `test_every_non_current_verdict_reaches_the_agent` cannot see because
    every branch says CAUTION."""
    mismatch = _preamble(code_state=op.CODE_MISMATCH)
    unknown = _preamble(code_state=op.CODE_UNKNOWN)

    assert "CANNOT SHOW" in mismatch
    assert "belongs to a different process" in mismatch
    assert "recorded nothing" not in mismatch
    assert "recorded nothing" in unknown
    assert "belongs to a different process" not in unknown


def test_stale_and_cannot_tell_are_not_reported_in_the_same_words():
    """An observed difference and an absence of evidence support different
    actions. Collapsing them overstates the second, and a notice that
    overstates is one that gets discounted."""
    stale = _preamble(code_state=op.CODE_STALE)
    unknown = _preamble(code_state=op.CODE_UNKNOWN)
    assert "OUT-OF-DATE" in stale
    assert "OUT-OF-DATE" not in unknown
    assert "CANNOT SHOW" in unknown
    assert "absence of evidence" in unknown


def test_the_stale_notice_names_the_command_that_fixes_it():
    """The sweep, not the single instance.

    Every supervisor on the machine imported its code at startup, so one
    operator change makes all of them stale at the same moment. A preamble
    naming only this agent's own instance describes a fix that leaves the
    other seven exactly as they were.
    """
    text = op.build_preamble("a:b", op.Instance("my.proj"), code_state=op.CODE_STALE)
    assert "operator restart-loop --all" in text
    assert "operator restart-loop my.proj" not in text, \
        "naming one instance understates a fault that is machine-wide"


def test_the_stale_notice_does_not_tell_the_agent_to_restart_unprompted():
    """A supervisor restart is a decision about the process the agent is
    running under. Naming the command is information; instructing an
    unattended agent to run it would have every session restart its own
    wrapper as a side effect of reading its preamble."""
    text = _preamble(code_state=op.CODE_STALE)
    assert "raise it with the human" in text


def test_the_notice_points_at_the_crash_claim_when_there_is_one():
    """Scoped to this preamble's own claims. "Some code is old" is not
    actionable; "the sentence above about your predecessor may be wrong" is."""
    with_crash = _preamble(crash_recovery=True, code_state=op.CODE_STALE)
    assert "the claim above that a handoff could not be found" in with_crash

    without = _preamble(crash_recovery=False, code_state=op.CODE_STALE)
    assert "the claim above that a handoff could not be found" not in without, \
        "there is no such claim to qualify when the note was not added"


@pytest.mark.parametrize("state", [op.CODE_STALE, op.CODE_UNKNOWN])
def test_the_notice_tells_the_agent_to_check_the_handoff_itself(state):
    """The remedy that would have saved the 355 launches: the file was on
    disk the whole time and nobody looked."""
    text = _preamble(crash_recovery=True, code_state=state)
    assert "check for a handoff file yourself" in text


# ── clause numbering ────────────────────────────────────────────
def test_the_crash_note_keeps_its_number_when_there_is_no_caveat():
    assert "(6) This session is being resumed" in _preamble(crash_recovery=True)


def test_the_caveat_takes_the_next_free_number():
    """Numbering is computed, not written twice. Hard-coding "(7)" would be
    wrong for the far commoner launch that has no crash note."""
    assert "(6) CAUTION" in _preamble(code_state=op.CODE_STALE)
    assert "(7) CAUTION" in _preamble(crash_recovery=True, code_state=op.CODE_STALE)
    assert "(6) This session is being resumed" in _preamble(
        crash_recovery=True, code_state=op.CODE_STALE), \
        "adding a clause must not renumber the one before it"


def test_no_clause_number_is_used_twice():
    text = _preamble(crash_recovery=True, code_state=op.CODE_STALE)
    numbers = [f"({n})" for n in range(1, 8)]
    assert [text.count(n) for n in numbers] == [1] * 7


def _resume_assignment():
    """A real RESUME assignment, so `describe` renders from the code under
    test rather than from a string this test invented."""
    claim = work_claims.Claim(item="0011", instance="proj",
                              worktree="C:/w/x", branch="fix/x")
    return operator_session.Assignment(kind=operator_session.RESUME,
                                       instance="proj", claim=claim)


def test_the_assignment_clause_takes_the_next_free_number_too():
    """The assignment clause was written as a literal "(7)" when it was the
    only optional clause after the crash note. It is not any more, and a
    literal is right for exactly one of the four combinations below.

    Each case names the number the assignment must actually carry, so a
    regression to a literal fails on three of them rather than being masked
    by the one it was written for.
    """
    described = operator_session.describe(_resume_assignment())
    assert described, "the fixture must produce a clause at all"

    cases = {
        (False, op.CODE_CURRENT): "(6)",
        (True, op.CODE_CURRENT): "(7)",
        (False, op.CODE_STALE): "(7)",
        (True, op.CODE_STALE): "(8)",
    }
    for (crash, state), expected in cases.items():
        text = _preamble(crash_recovery=crash, code_state=state,
                         assignment=_resume_assignment())
        assert f"{expected} {described}" in text, (
            f"crash_recovery={crash}, code_state={state} should number the "
            f"assignment {expected}")


def test_every_clause_number_is_unique_when_all_of_them_are_present():
    """The counter's whole job. Two clauses sharing a number is the failure
    a literal produces, and it is invisible to any test that only checks the
    clause it cares about is present somewhere."""
    text = _preamble(crash_recovery=True, code_state=op.CODE_STALE,
                     assignment=_resume_assignment())
    counts = [text.count(f"({n})") for n in range(1, 9)]
    assert counts == [1] * 8, f"clause numbers 1..8 appeared {counts} times"


def test_an_unassigned_session_is_charged_nothing_for_it():
    """Control. `describe` returns "" for NONE, and a clause number must not
    be spent on a clause that renders empty -- that would leave a gap in the
    numbering of every unassigned session, which is nearly all of them."""
    none = operator_session.Assignment(kind=operator_session.NONE,
                                       instance="proj")
    assert operator_session.describe(none) == ""
    assert _preamble(assignment=none) == _preamble(), \
        "an empty assignment must leave the preamble byte for byte unchanged"


# ── the numbering is contiguous, not merely unique ──────────────
def _clause_numbers(text: str) -> "list[int]":
    """Every "(n)" the preamble spends, in the order it spends them.

    Deliberately reads the numbers out of the rendered text rather than being
    told what to expect. A test that asks "is (7) present" can only find the
    numbers it already thought of, and the defect this file exists for was a
    number nobody thought to look for.
    """
    return [int(m) for m in re.findall(r"\((\d+)\)", text)]


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("state", ALL_CODE_STATES)
@pytest.mark.parametrize("assigned", [False, True])
def test_the_numbering_is_contiguous_from_one_in_every_combination(
        crash, state, assigned):
    """The property, over all sixteen combinations rather than the one or two
    a hand-written case list happens to name.

    Uniqueness is not enough on its own: a clause that consumes a number
    without rendering leaves 1..5 and 7 -- every number used once, and a gap
    where nobody looks. Contiguity catches both that and the duplicate, and
    it does so without the test needing to know how many clauses this call
    should have produced.
    """
    assignment = _resume_assignment() if assigned else None
    numbers = _clause_numbers(_preamble(crash_recovery=crash, code_state=state,
                                        assignment=assignment))
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"crash_recovery={crash}, code_state={state}, assigned={assigned} "
        f"rendered clause numbers {numbers}, which is not 1..n with no gaps "
        "or repeats")


def test_the_base_clause_count_matches_the_text():
    """`BASE_CLAUSES` is an assumption about prose held somewhere else.

    Nothing about the constant makes it true -- a wrong value is still a
    number, and every optional clause after it would be self-consistently
    wrong, which is exactly how the literal "(7)" survived. So it is counted
    off the rendered base preamble rather than asserted.
    """
    numbers = _clause_numbers(_preamble())
    assert numbers == list(range(1, op.BASE_CLAUSES + 1)), (
        f"the base preamble renders clauses {numbers}, but BASE_CLAUSES says "
        f"{op.BASE_CLAUSES}; adding or removing a numbered clause in the base "
        "text means updating the constant in the same edit")


def test_a_clause_that_renders_nothing_consumes_no_number():
    """The property the surviving mutant used to hide behind.

    Numbering off the collected list makes "consume a number without
    rendering" unrepresentable rather than merely untested -- there is no
    counter to bump. This asserts the consequence directly, at the boundary
    where an empty clause would show up: the preamble of a session that has
    the caveat but no assignment must not skip a number on the way past.
    """
    none = operator_session.Assignment(kind=operator_session.NONE,
                                       instance="proj")
    text = _preamble(crash_recovery=True, code_state=op.CODE_STALE,
                     assignment=none)
    assert _clause_numbers(text) == [1, 2, 3, 4, 5, 6, 7]
    assert not text.rstrip().endswith(")"), \
        "a trailing '(n)' with nothing after it is an empty clause"


def test_the_preamble_stays_platform_neutral_with_the_caveat():
    """The agent reading this may be on Windows; the existing guard on the
    base preamble has to keep holding once clauses are appended to it."""
    text = _preamble(crash_recovery=True, code_state=op.CODE_STALE)
    assert "touch " not in text
    assert "~/" not in text


# ── the loop wiring ─────────────────────────────────────────────
def _run_one_session(monkeypatch, tmp_path) -> str:
    """Drive exactly one launch of the real loop and return its preamble."""
    seen: list[str] = []

    def capture(instance, args, session_num, remain_on_exit=False, preamble=""):
        seen.append(preamble)
        instance.exit_file.write_text("0", encoding="utf-8")
        instance.stop_marker.touch()

    monkeypatch.setattr(op, "start_session", capture)
    monkeypatch.setattr(op, "show_run_summary", lambda run_started: None)
    workdir = tmp_path / "not-a-repo"
    workdir.mkdir(exist_ok=True)
    monkeypatch.chdir(workdir)

    op.run_loop_mode(op.Instance("wired"), ["--agent", "test:agent"], is_fresh=True)
    assert len(seen) == 1, f"expected one launch, got {len(seen)}"
    return seen[0]


def test_a_stale_supervisor_says_so_in_the_preamble_it_hands_over(
        monkeypatch, tmp_path):
    """End to end: the verdict has to survive the trip from the supervisor's
    launch loop into the text the agent is actually given. Every earlier test
    here calls `build_preamble` directly, so all of them would pass with the
    loop never wired up at all."""
    monkeypatch.setattr(op, "own_code_state", lambda: (op.CODE_STALE, ["/x.py"]))

    assert "CAUTION" in _run_one_session(monkeypatch, tmp_path)


def test_a_current_supervisor_hands_over_a_clean_preamble(monkeypatch, tmp_path):
    """The control. Without it the test above passes against an
    implementation that appends the caveat unconditionally."""
    monkeypatch.setattr(op, "own_code_state", lambda: (op.CODE_CURRENT, []))

    assert "CAUTION" not in _run_one_session(monkeypatch, tmp_path)


def test_a_failed_staleness_check_does_not_kill_an_unattended_loop(
        monkeypatch, tmp_path):
    """The loop outlives everything it calls. A staleness verdict is never
    worth ending a run that would otherwise have kept launching sessions."""
    def boom():
        raise RuntimeError("simulated")

    monkeypatch.setattr(op, "own_code_state", boom)

    text = _run_one_session(monkeypatch, tmp_path)
    assert "CANNOT SHOW" in text, \
        "a check that failed must degrade to 'cannot tell', never to 'current'"


def test_a_failed_check_degrades_to_unknown_rather_than_current():
    """Stated against the function directly as well, because the failure
    directions are not symmetric: 'unknown' costs a few unnecessary lines,
    while 'current' is a clean bill of health nobody checked -- byte-identical
    to the healthy case, so nothing downstream could ever catch it."""
    def boom():
        raise RuntimeError("simulated")

    original = op.own_code_state
    op.own_code_state = boom
    try:
        assert op._launch_code_state() == op.CODE_UNKNOWN
    finally:
        op.own_code_state = original


# ── why `operator reload` is not this ───────────────────────────
#
# `reload` re-generates an instance's launch spec from a fresh process, so it
# is a reasonable guess at the remedy for a stale supervisor. It is not, for
# three separate reasons, and the first two are established below rather than
# argued. The third is structural: `reload` is a *remedy*, and applying it
# requires already knowing the supervisor is behind -- which is precisely what
# nobody knew for 355 launches. Detection is not the same as cure, and the
# cure here remains `restart-loop`.


def test_reload_cannot_write_the_clause_that_was_wrong(tmp_path):
    """`reload_instance` builds its preamble with no crash-recovery argument,
    so a reloaded spec never contains that claim at all. It cannot correct a
    sentence it is structurally incapable of writing."""
    inst = op.Instance("reloaded-claim")
    op.write_launch_spec(
        inst, ["copilot", "--agent", "a:b", "-i", "old"], tmp_path, 1)

    assert op.reload_instance("reloaded-claim") == 0

    written = json.loads(inst.spec_file.read_text(encoding="utf-8"))["argv"][-1]
    assert "blanket human approval" in written, "control: reload did write a preamble"
    assert "handoff file could not be found" not in written


def test_a_loop_launch_overwrites_the_spec_that_reload_wrote(tmp_path, monkeypatch):
    """In loop mode `reload`'s work does not survive to be read.

    The supervisor builds its own preamble per launch and `start_session`
    rewrites the spec from it, so the next launch replaces whatever `reload`
    put there -- with text built by the same stale code. `reload` serves the
    non-loop path, where the spec is what the runner actually reads.
    """
    inst = op.Instance("reloaded-spec")
    op.write_launch_spec(
        inst, ["copilot", "--agent", "a:b", "-i", "old"], tmp_path, 1)
    assert op.reload_instance("reloaded-spec") == 0
    reloaded = json.loads(inst.spec_file.read_text(encoding="utf-8"))["argv"][-1]
    assert "blanket human approval" in reloaded, "control: reload did write a preamble"

    monkeypatch.setattr(op, "copilot_executable", lambda: "copilot")
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)
    monkeypatch.setattr(op.Instance, "copilot_pid", lambda self: 4321)
    monkeypatch.chdir(tmp_path)

    op.start_session(inst, ["--agent", "a:b"], 2, remain_on_exit=True,
                     preamble="built by the supervisor that is running")

    argv = json.loads(inst.spec_file.read_text(encoding="utf-8"))["argv"]
    assert "built by the supervisor that is running" in argv
    assert reloaded not in argv, \
        "the launch spec reload wrote was replaced, not read"
