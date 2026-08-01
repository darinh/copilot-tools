#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Functional tests for setup.sh's legacy-migration logic (Steps 1-3).
# Runs entirely inside a throwaway sandbox (fake HOME + fake checkout) so it
# never touches the real user's ~/.local/bin or installs anything real.
#
# Usage: bash tests/test_setup_sh.sh
# ═══════════════════════════════════════════════════════════════════
set -uo pipefail

REAL_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_ROOT="$(mktemp -d)"
trap 'rm -rf "$SANDBOX_ROOT"' EXIT
# Mirrors LEGACY_BACKUP_SUFFIX in setup.sh.
LEGACY_BAK=".copilot-tools-legacy-bak"

PASS=0
FAIL=0

pass() { echo "  ✅ PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL+1)); }

# Build one fresh fake checkout + fake HOME per scenario so nothing leaks
# between tests. Returns paths via the SCENARIO_HOME / SCENARIO_CHECKOUT
# globals.
new_scenario() {
    local name="$1"
    SCENARIO_HOME="${SANDBOX_ROOT}/${name}/home"
    SCENARIO_CHECKOUT="${SANDBOX_ROOT}/${name}/checkout"
    mkdir -p "$SCENARIO_HOME" "$SCENARIO_CHECKOUT"

    cp "${REAL_SCRIPT_DIR}/setup.sh" "${SCENARIO_CHECKOUT}/setup.sh"
    chmod +x "${SCENARIO_CHECKOUT}/setup.sh"

    printf '#!/usr/bin/env bash\necho "fake legacy operator: $*"\n' > "${SCENARIO_CHECKOUT}/operator.sh"
    printf '#!/usr/bin/env bash\necho "fake legacy handoff: $*"\n' > "${SCENARIO_CHECKOUT}/handoff.sh"
    chmod +x "${SCENARIO_CHECKOUT}/operator.sh" "${SCENARIO_CHECKOUT}/handoff.sh"
}

# A stub setup_tools.py standing in for the real Python installer.
# $1 = "succeed", "fail", or "sleep" (sleeps before installing, to give a test
# time to send SIGINT/SIGTERM mid-install).
# Deliberately mirrors real pip's console-script installer semantics: it
# unlinks whatever is at the destination first (os.remove, which does NOT
# follow a symlink into its target) and then creates a brand-new regular
# file -- it does NOT open-and-write "through" an existing symlink. This
# matters because a naive stub that just does `open(path, "w")` would leave
# a foreign symlink's pointer untouched while mutating its target's content,
# which is not how real pip behaves and would mask the exact bug this test
# suite exists to catch (an unrelated command being clobbered).
write_stub_setup_tools() {
    local mode="$1"
    cat > "${SCENARIO_CHECKOUT}/setup_tools.py" <<PYEOF
import os
import sys
import stat
import time

mode = "${mode}"
if mode == "fail":
    print("stub setup_tools.py: simulating failure")
    sys.exit(1)
if mode == "sleep":
    # Publish our pid so the test can derive the real process group to signal
    # instead of guessing, and can wait for the install to actually be in
    # flight instead of sleeping a fixed, racy interval.
    with open("${SANDBOX_ROOT}/stub_started", "w") as f:
        f.write(str(os.getpid()))
    print("stub setup_tools.py: sleeping to simulate a slow install...")
    sys.stdout.flush()
    time.sleep(20)

local_bin = os.path.join(os.environ["HOME"], ".local", "bin")
os.makedirs(local_bin, exist_ok=True)
for name in ("operator", "handoff"):
    path = os.path.join(local_bin, name)
    if os.path.lexists(path):
        os.remove(path)
    with open(path, "w") as f:
        f.write("#!/usr/bin/env bash\necho fake python %s\n" % name)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
print("stub setup_tools.py: simulated install OK")
PYEOF
}

run_setup() {
    HOME="$SCENARIO_HOME" PATH="${SCENARIO_HOME}/.local/bin:${PATH}" \
        bash "${SCENARIO_CHECKOUT}/setup.sh" --yes >"${SANDBOX_ROOT}/last_output.log" 2>&1
    echo $?
}

# Same sandbox as run_setup, but with the arguments under test, and with an
# optional EXTRA_PATH entry for the scenarios that turn on whether some *other*
# operator/handoff is resolvable on PATH.
run_setup_with() {
    HOME="$SCENARIO_HOME" \
        PATH="${SCENARIO_HOME}/.local/bin:${EXTRA_PATH:+${EXTRA_PATH}:}${PATH}" \
        bash "${SCENARIO_CHECKOUT}/setup.sh" "$@" >"${SANDBOX_ROOT}/last_output.log" 2>&1
    echo $?
}

# A stub standing in for setup_tools.py in a QUERY mode (--status/--check-only/
# --help): it reports and exits, and installs nothing. $1 is the exit code --
# report_status() returns 1 for a machine that is merely out of date or whose
# extensions are inert, which is a report and not a failure.
write_query_stub() {
    cat > "${SCENARIO_CHECKOUT}/setup_tools.py" <<PYEOF
import sys
print("stub setup_tools.py: reporting only, installing nothing")
sys.exit(${1})
PYEOF
}

# Both legacy symlinks, as a machine mid-migration has them.
link_legacy() {
    mkdir -p "${SCENARIO_HOME}/.local/bin"
    ln -s "${SCENARIO_CHECKOUT}/operator.sh" "${SCENARIO_HOME}/.local/bin/operator"
    ln -s "${SCENARIO_CHECKOUT}/handoff.sh" "${SCENARIO_HOME}/.local/bin/handoff"
}

# True only if both legacy symlinks are exactly as link_legacy left them and no
# backup file is lying around -- i.e. nothing touched them at all.
legacy_links_untouched() {
    [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
        && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/operator")" == "${SCENARIO_CHECKOUT}/operator.sh" ]] \
        && [[ -L "${SCENARIO_HOME}/.local/bin/handoff" ]] \
        && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/handoff")" == "${SCENARIO_CHECKOUT}/handoff.sh" ]] \
        && [[ -z "$(find "${SCENARIO_HOME}/.local/bin" -name '*bak*' 2>/dev/null)" ]]
}

echo "═══ setup.sh functional tests ═══"
echo ""

# ── Scenario 1: legacy symlink -> this checkout, install succeeds ──
new_scenario "scenario1"
mkdir -p "${SCENARIO_HOME}/.local/bin"
ln -s "${SCENARIO_CHECKOUT}/operator.sh" "${SCENARIO_HOME}/.local/bin/operator"
ln -s "${SCENARIO_CHECKOUT}/handoff.sh" "${SCENARIO_HOME}/.local/bin/handoff"
write_stub_setup_tools "succeed"
status=$(run_setup)
if [[ "$status" == "0" ]] \
    && [[ ! -L "${SCENARIO_HOME}/.local/bin/operator" || ! "$(readlink "${SCENARIO_HOME}/.local/bin/operator" 2>/dev/null)" == *operator.sh ]] \
    && [[ ! -e "${SCENARIO_HOME}/.local/bin/operator.copilot-tools-legacy-bak" ]] \
    && grep -q "fake python operator" "${SCENARIO_HOME}/.local/bin/operator" \
    && grep -q "fake python handoff" "${SCENARIO_HOME}/.local/bin/handoff"; then
    pass "successful migration replaces legacy symlinks and cleans up backups"
else
    fail "successful migration replaces legacy symlinks and cleans up backups"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 2: legacy symlink -> this checkout, install FAILS -> rollback ──
new_scenario "scenario2"
mkdir -p "${SCENARIO_HOME}/.local/bin"
ln -s "${SCENARIO_CHECKOUT}/operator.sh" "${SCENARIO_HOME}/.local/bin/operator"
ln -s "${SCENARIO_CHECKOUT}/handoff.sh" "${SCENARIO_HOME}/.local/bin/handoff"
write_stub_setup_tools "fail"
status=$(run_setup)
if [[ "$status" != "0" ]] \
    && [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
    && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/operator")" == "${SCENARIO_CHECKOUT}/operator.sh" ]] \
    && [[ -L "${SCENARIO_HOME}/.local/bin/handoff" ]] \
    && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/handoff")" == "${SCENARIO_CHECKOUT}/handoff.sh" ]] \
    && [[ ! -e "${SCENARIO_HOME}/.local/bin/operator.copilot-tools-legacy-bak" ]]; then
    pass "failed install rolls back legacy symlinks and exits non-zero"
else
    fail "failed install rolls back legacy symlinks and exits non-zero"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 3: symlink points somewhere else entirely -> backed up, never
# deleted, since a real `pip install` would otherwise silently clobber it ──
new_scenario "scenario3"
mkdir -p "${SCENARIO_HOME}/.local/bin" "${SCENARIO_HOME}/other"
printf '#!/usr/bin/env bash\necho "unrelated tool"\n' > "${SCENARIO_HOME}/other/operator"
chmod +x "${SCENARIO_HOME}/other/operator"
ln -s "${SCENARIO_HOME}/other/operator" "${SCENARIO_HOME}/.local/bin/operator"
write_stub_setup_tools "succeed"
status=$(run_setup)
backup="${SCENARIO_HOME}/.local/bin/operator.copilot-tools-preexisting-bak"
if [[ "$status" == "0" ]] \
    && [[ -L "$backup" ]] \
    && [[ "$(readlink "$backup")" == "${SCENARIO_HOME}/other/operator" ]] \
    && [[ ! -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
    && grep -q "fake python operator" "${SCENARIO_HOME}/.local/bin/operator" \
    && grep -qi "preexisting-bak\|moved existing" "${SANDBOX_ROOT}/last_output.log"; then
    pass "unrelated symlink with the same name is preserved as a backup, not silently overwritten"
else
    fail "unrelated symlink with the same name is preserved as a backup, not silently overwritten"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 3b: a plain (non-symlink) foreign file with the same name is
# also backed up, not silently overwritten ──
new_scenario "scenario3b"
mkdir -p "${SCENARIO_HOME}/.local/bin"
printf '#!/usr/bin/env bash\necho "some other operator command entirely"\n' > "${SCENARIO_HOME}/.local/bin/operator"
chmod +x "${SCENARIO_HOME}/.local/bin/operator"
write_stub_setup_tools "succeed"
status=$(run_setup)
backup="${SCENARIO_HOME}/.local/bin/operator.copilot-tools-preexisting-bak"
if [[ "$status" == "0" ]] \
    && [[ -f "$backup" ]] \
    && grep -q "some other operator command entirely" "$backup" \
    && grep -q "fake python operator" "${SCENARIO_HOME}/.local/bin/operator"; then
    pass "unrelated plain file with the same name is preserved as a backup, not silently overwritten"
else
    fail "unrelated plain file with the same name is preserved as a backup, not silently overwritten"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 4: broken symlink -> removed with a warning, install proceeds ──
new_scenario "scenario4"
mkdir -p "${SCENARIO_HOME}/.local/bin"
ln -s "${SCENARIO_HOME}/does/not/exist" "${SCENARIO_HOME}/.local/bin/operator"
write_stub_setup_tools "succeed"
status=$(run_setup)
if [[ "$status" == "0" ]] \
    && [[ ! -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
    && grep -q "fake python operator" "${SCENARIO_HOME}/.local/bin/operator" \
    && grep -qi "broken symlink" "${SANDBOX_ROOT}/last_output.log"; then
    pass "broken legacy symlink is removed with a warning and install proceeds"
else
    fail "broken legacy symlink is removed with a warning and install proceeds"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 5: no legacy symlinks at all -> plain first-time install ──
new_scenario "scenario5"
write_stub_setup_tools "succeed"
status=$(run_setup)
if [[ "$status" == "0" ]] \
    && grep -q "fake python operator" "${SCENARIO_HOME}/.local/bin/operator" \
    && grep -qi "Nothing at.*local/bin" "${SANDBOX_ROOT}/last_output.log"; then
    pass "first-time install with no legacy symlinks works cleanly"
else
    fail "first-time install with no legacy symlinks works cleanly"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 6: install succeeds but new binaries don't resolve on PATH ──
# (simulates pip installing somewhere not on PATH) -> rollback + non-zero exit
new_scenario "scenario6"
mkdir -p "${SCENARIO_HOME}/.local/bin"
ln -s "${SCENARIO_CHECKOUT}/operator.sh" "${SCENARIO_HOME}/.local/bin/operator"
cat > "${SCENARIO_CHECKOUT}/setup_tools.py" <<'PYEOF'
import os
# Simulate installing to a directory that is NOT on PATH.
elsewhere = os.path.join(os.environ["HOME"], "not-on-path")
os.makedirs(elsewhere, exist_ok=True)
with open(os.path.join(elsewhere, "operator"), "w") as f:
    f.write("#!/usr/bin/env bash\necho unreachable\n")
print("stub: installed operator to a directory that is not on PATH")
PYEOF
status=$(run_setup)
if [[ "$status" != "0" ]] \
    && [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
    && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/operator")" == "${SCENARIO_CHECKOUT}/operator.sh" ]]; then
    pass "install that doesn't resolve on PATH triggers rollback"
else
    fail "install that doesn't resolve on PATH triggers rollback"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 7: Ctrl-C (SIGINT) mid-install -> trap restores the legacy
# symlinks rather than leaving neither the old nor the new command in place ──
new_scenario "scenario7"
mkdir -p "${SCENARIO_HOME}/.local/bin"
ln -s "${SCENARIO_CHECKOUT}/operator.sh" "${SCENARIO_HOME}/.local/bin/operator"
ln -s "${SCENARIO_CHECKOUT}/handoff.sh" "${SCENARIO_HOME}/.local/bin/handoff"
write_stub_setup_tools "sleep"
rm -f "${SANDBOX_ROOT}/stub_started"
# Job control (set -m) is required here, not cosmetic: a non-interactive shell
# sets SIGINT to SIG_IGN for any job it starts with '&', and SIG_IGN is
# inherited across both fork and exec -- so without monitor mode neither
# setup.sh's trap nor its python child can ever observe a Ctrl-C (bash also
# refuses to trap a signal that was ignored on entry). Monitor mode both
# leaves SIGINT deliverable and puts the job in its own process group, which
# is what makes signalling that group behave exactly like a terminal's Ctrl-C.
# setsid is deliberately NOT used: under monitor mode the job is already a
# group leader, so setsid would fork and $! would be its short-lived pid
# rather than the shell whose trap we are testing.
set -m
HOME="$SCENARIO_HOME" PATH="${SCENARIO_HOME}/.local/bin:${PATH}" \
    bash "${SCENARIO_CHECKOUT}/setup.sh" --yes >"${SANDBOX_ROOT}/last_output.log" 2>&1 &
setup_pid=$!
set +m
# Wait for the install to actually be in flight rather than sleeping a fixed
# interval, so the test cannot race ahead of (or behind) the real work.
stub_pid=""
for _ in $(seq 1 100); do
    if [[ -s "${SANDBOX_ROOT}/stub_started" ]]; then
        stub_pid="$(cat "${SANDBOX_ROOT}/stub_started")"
        break
    fi
    sleep 0.2
done
if [[ -z "$stub_pid" ]]; then
    fail "Ctrl-C mid-install restores the legacy symlinks instead of stranding the user"
    echo "  (stub install never started; cannot exercise the interrupt path)"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
else
    # Signal the whole process group, exactly as a terminal does.
    pgid="$(ps -o pgid= -p "$stub_pid" 2>/dev/null | tr -d ' ')"
    kill -INT "-${pgid:-$setup_pid}" 2>/dev/null
    wait "$setup_pid" 2>/dev/null
    interrupted_rc=$?
    if [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
        && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/operator")" == "${SCENARIO_CHECKOUT}/operator.sh" ]] \
        && [[ -L "${SCENARIO_HOME}/.local/bin/handoff" ]] \
        && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/handoff")" == "${SCENARIO_CHECKOUT}/handoff.sh" ]] \
        && [[ ! -e "${SCENARIO_HOME}/.local/bin/operator.copilot-tools-legacy-bak" ]] \
        && [[ "$interrupted_rc" -ne 0 ]]; then
        pass "Ctrl-C mid-install restores the legacy symlinks instead of stranding the user"
    else
        fail "Ctrl-C mid-install restores the legacy symlinks instead of stranding the user"
        echo "  (exit status was ${interrupted_rc})"
        echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
    fi
fi

# ═══════════════════════════════════════════════════════════════════
# Query-only modes (--status / --check-only / --help).
#
# These ask setup_tools.py a question and install nothing, so none of the
# migration machinery above should run for them. Every one of these scenarios
# is a measured pre-fix behaviour, not a hypothetical:
#   * --status whose report exited 1 was relabelled "Python setup failed"
#     and rolled back, when nothing had failed and nothing was being installed;
#   * --status whose report exited 0, on a machine with an `operator` further
#     along PATH, DELETED ~/.local/bin/operator outright and reported a
#     successful migration;
#   * --help exited 1 after printing two false "does not resolve on PATH"
#     errors.
# ═══════════════════════════════════════════════════════════════════

# ── Scenario 8: --status whose report exits non-zero is forwarded, not
# relabelled as a setup failure, and touches nothing ──
new_scenario "scenario8"
link_legacy
write_query_stub 1
EXTRA_PATH="" status=$(run_setup_with --status)
if [[ "$status" == "1" ]] \
    && legacy_links_untouched \
    && ! grep -q "Python setup failed" "${SANDBOX_ROOT}/last_output.log" \
    && ! grep -q "Set aside" "${SANDBOX_ROOT}/last_output.log"; then
    pass "--status forwards a non-zero report verbatim and never migrates"
else
    fail "--status forwards a non-zero report verbatim and never migrates"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 9: --status on a machine that also has `operator` elsewhere on
# PATH must not delete ~/.local/bin/{operator,handoff} ──
# This is the destructive case: `command -v operator` answered "yes" from the
# other directory, which setup.sh read as "the install succeeded" and used to
# justify deleting the legacy backup -- the only remaining copy.
new_scenario "scenario9"
link_legacy
mkdir -p "${SCENARIO_HOME}/elsewhere"
printf '#!/usr/bin/env bash\necho other operator\n' > "${SCENARIO_HOME}/elsewhere/operator"
printf '#!/usr/bin/env bash\necho other handoff\n' > "${SCENARIO_HOME}/elsewhere/handoff"
chmod +x "${SCENARIO_HOME}/elsewhere/operator" "${SCENARIO_HOME}/elsewhere/handoff"
write_query_stub 0
EXTRA_PATH="${SCENARIO_HOME}/elsewhere" status=$(run_setup_with --status)
if [[ "$status" == "0" ]] \
    && legacy_links_untouched \
    && ! grep -q "Migrated" "${SANDBOX_ROOT}/last_output.log"; then
    pass "--status does not delete commands when another one is on PATH"
else
    fail "--status does not delete commands when another one is on PATH"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 10: --help answers and exits 0 without inventing PATH errors ──
new_scenario "scenario10"
link_legacy
write_query_stub 0
EXTRA_PATH="" status=$(run_setup_with --help)
if [[ "$status" == "0" ]] \
    && legacy_links_untouched \
    && ! grep -q "does not resolve on PATH" "${SANDBOX_ROOT}/last_output.log"; then
    pass "--help reports without migrating or inventing PATH errors"
else
    fail "--help reports without migrating or inventing PATH errors"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 11: --check-only gets the same treatment as --status ──
new_scenario "scenario11"
link_legacy
write_query_stub 1
EXTRA_PATH="" status=$(run_setup_with --check-only)
if [[ "$status" == "1" ]] \
    && legacy_links_untouched \
    && ! grep -q "Python setup failed" "${SANDBOX_ROOT}/last_output.log"; then
    pass "--check-only forwards its report verbatim and never migrates"
else
    fail "--check-only forwards its report verbatim and never migrates"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 12: an INSTALL that installs nothing at ~/.local/bin rolls back,
# even though another `operator` resolves on PATH ──
# The flag list in scenario 8-11 only covers the query modes known today. This
# covers the mechanism underneath them: finalization must turn on "did this
# install put a command where the original was set aside", not on the narrower
# "does some command by that name resolve on PATH". Reachable in install mode
# too -- e.g. --skip-package deploys extensions and templates but runs no pip,
# so it creates no console scripts.
new_scenario "scenario12"
link_legacy
mkdir -p "${SCENARIO_HOME}/elsewhere"
printf '#!/usr/bin/env bash\necho other operator\n' > "${SCENARIO_HOME}/elsewhere/operator"
printf '#!/usr/bin/env bash\necho other handoff\n' > "${SCENARIO_HOME}/elsewhere/handoff"
chmod +x "${SCENARIO_HOME}/elsewhere/operator" "${SCENARIO_HOME}/elsewhere/handoff"
cat > "${SCENARIO_CHECKOUT}/setup_tools.py" <<'PYEOF'
print("stub setup_tools.py: an install that creates no console scripts")
PYEOF
EXTRA_PATH="${SCENARIO_HOME}/elsewhere" status=$(run_setup_with --yes)
if [[ "$status" != "0" ]] \
    && [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
    && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/operator")" == "${SCENARIO_CHECKOUT}/operator.sh" ]] \
    && [[ -L "${SCENARIO_HOME}/.local/bin/handoff" ]] \
    && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/handoff")" == "${SCENARIO_CHECKOUT}/handoff.sh" ]] \
    && ! grep -q "Migrated" "${SANDBOX_ROOT}/last_output.log"; then
    pass "an install that creates no console scripts rolls back instead of deleting them"
else
    fail "an install that creates no console scripts rolls back instead of deleting them"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
fi

# ── Scenario 13: Ctrl-C DURING the stash, before the install starts ──
# Scenario 7 covers an interrupt once the install is running. This covers the
# earlier interval: after the first `mv` has moved a command aside, but before
# the stashing step has finished. The trap used to be armed only after BOTH
# links were stashed, so a signal in between killed the script with the
# operator already renamed to a .bak and nothing left at ~/.local/bin/operator
# -- the user's only copy, orphaned under a name they have no reason to look
# for, with no error saying so.
#
# The window is made deterministic rather than raced: `canon` shells out to
# python twice per symlink, so a fake python3 that sleeps for exactly that
# probe (and execs the real one for everything else) turns a millisecond gap
# into seconds. The test waits for the operator backup to appear -- proof the
# mv has happened -- and only then signals.
#
# What this covers, precisely: that the trap is armed before the first rename.
# What it does NOT cover: that the backup path is published before the rename
# rather than after it. A reviewer caught the overclaim, and a control settled
# it -- move the publish back into the caller but leave the trap early, and
# this scenario still passes, because the signal lands during the SECOND
# link's canon probe, by which time the first link's path has been recorded
# either way. That half of the fix is not observable from a test: the gap it
# closes is between two adjacent bash statements with no subprocess in
# between, so there is nothing to widen and no portable way to deliver a
# signal inside it. It is argued in the comments at the publish site instead.
new_scenario "scenario13"
mkdir -p "${SCENARIO_HOME}/.local/bin" "${SCENARIO_HOME}/fakebin"
REAL_PYTHON="$(command -v python3 || command -v python)"
cat > "${SCENARIO_HOME}/fakebin/python3" <<FAKEEOF
#!/usr/bin/env bash
# Slow ONLY for canon()'s realpath probe: find_python's version check and the
# installer itself must stay fast, or this stops being a test of the stash
# window and becomes a test of the timeout.
for a in "\$@"; do
    case "\$a" in
        *realpath*) sleep 2 ;;
    esac
done
exec "${REAL_PYTHON}" "\$@"
FAKEEOF
chmod +x "${SCENARIO_HOME}/fakebin/python3"
link_legacy
write_stub_setup_tools "succeed"
set -m
HOME="$SCENARIO_HOME" PATH="${SCENARIO_HOME}/fakebin:${SCENARIO_HOME}/.local/bin:${PATH}" \
    bash "${SCENARIO_CHECKOUT}/setup.sh" --yes >"${SANDBOX_ROOT}/last_output.log" 2>&1 &
stash_pid=$!
set +m
operator_bak="${SCENARIO_HOME}/.local/bin/operator${LEGACY_BAK}"
stashed=""
for _ in $(seq 1 200); do
    if [[ -L "$operator_bak" || -e "$operator_bak" ]]; then stashed=yes; break; fi
    # Stop waiting if the script exited -- otherwise a setup.sh that never
    # stashed at all would be reported as a timeout rather than as the
    # different failure it is.
    kill -0 "$stash_pid" 2>/dev/null || break
    sleep 0.05
done
if [[ -z "$stashed" ]]; then
    fail "Ctrl-C during the stash restores the command instead of orphaning it"
    echo "  (the operator backup never appeared; the stash window was never entered)"
    echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
else
    pgid="$(ps -o pgid= -p "$stash_pid" 2>/dev/null | tr -d ' ')"
    kill -INT "-${pgid:-$stash_pid}" 2>/dev/null
    wait "$stash_pid" 2>/dev/null
    stash_rc=$?
    if [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]] \
        && [[ "$(readlink "${SCENARIO_HOME}/.local/bin/operator")" == "${SCENARIO_CHECKOUT}/operator.sh" ]] \
        && [[ ! -e "$operator_bak" ]] \
        && [[ -L "${SCENARIO_HOME}/.local/bin/handoff" ]] \
        && [[ "$stash_rc" -ne 0 ]]; then
        pass "Ctrl-C during the stash restores the command instead of orphaning it"
    else
        fail "Ctrl-C during the stash restores the command instead of orphaning it"
        echo "  (exit status was ${stash_rc}; operator is $(
            if [[ -L "${SCENARIO_HOME}/.local/bin/operator" ]]; then echo "a symlink"
            elif [[ -e "${SCENARIO_HOME}/.local/bin/operator" ]]; then echo "a regular file"
            else echo "MISSING -- orphaned at ${operator_bak}"; fi))"
        echo "----- output -----"; cat "${SANDBOX_ROOT}/last_output.log"; echo "------------------"
    fi
fi

echo ""
echo "═══ Results: ${PASS} passed, ${FAIL} failed ═══"
[[ "$FAIL" -eq 0 ]]