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

echo ""
echo "═══ Results: ${PASS} passed, ${FAIL} failed ═══"
[[ "$FAIL" -eq 0 ]]
