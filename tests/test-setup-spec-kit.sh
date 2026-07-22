#!/usr/bin/env bash
# tests/test-setup-spec-kit.sh
#
# Self-contained tests for the spec-kit installation section of setup.sh.
# Uses an isolated HOME and stubbed external commands. No network access.
#
# Usage: bash tests/test-setup-spec-kit.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SETUP_SH="${REPO_ROOT}/setup.sh"

PASS=0
FAIL=0

CLEANUP_DIRS=()
cleanup_all() {
    if [[ "${#CLEANUP_DIRS[@]}" -gt 0 ]]; then
        rm -rf "${CLEANUP_DIRS[@]}"
    fi
}
trap cleanup_all EXIT

pass() { echo "  PASS: $*"; PASS=$(( PASS + 1 )); }
fail() { echo "  FAIL: $*" >&2; FAIL=$(( FAIL + 1 )); }

assert_eq() {
    local label="$1" actual="$2" expected="$3"
    if [[ "$actual" == "$expected" ]]; then
        pass "$label"
    else
        fail "$label (expected '$expected', got '$actual')"
    fi
}

# build_env — populate an isolated HOME and stub-bin directory.
#
# $1  base dir (must already exist)
# $2  specify mode: "absent" | "present"
# $3  uv mode:     "present" (records calls, creates specify on install)
#                  "broken"  (records calls, does NOT create specify)
build_env() {
    local base="$1" specify_mode="$2" uv_mode="$3"
    local stub_bin="${base}/stub-bin"
    local fake_home="${base}/home"
    local call_log="${base}/calls.log"

    mkdir -p "$stub_bin" "${fake_home}/.local/bin"
    touch "$call_log"

    # ── Minimal prereq stubs ──────────────────────────────────
    for cmd in tmux sqlite3 git; do
        printf '#!/usr/bin/env bash\nexit 0\n' > "${stub_bin}/${cmd}"
        chmod +x "${stub_bin}/${cmd}"
    done

    # python3: reports version 3.11 for -c invocations
    cat > "${stub_bin}/python3" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
    -c) echo "3.11" ;;
    --version) echo "Python 3.11.0" ;;
esac
exit 0
STUB
    chmod +x "${stub_bin}/python3"

    # copilot: exits 0 for all subcommands (extensions list, install, etc.)
    printf '#!/usr/bin/env bash\nexit 0\n' > "${stub_bin}/copilot"
    chmod +x "${stub_bin}/copilot"

    # dotnet-roslyn-mcp: present so the Roslyn check skips the install prompt
    printf '#!/usr/bin/env bash\nexit 0\n' > "${stub_bin}/dotnet-roslyn-mcp"
    chmod +x "${stub_bin}/dotnet-roslyn-mcp"

    # ── specify stub ─────────────────────────────────────────
    if [[ "$specify_mode" == "present" ]]; then
        cat > "${stub_bin}/specify" <<'STUB'
#!/usr/bin/env bash
echo "specify 0.13.4"
exit 0
STUB
        chmod +x "${stub_bin}/specify"
    fi

    # ── uv stub ──────────────────────────────────────────────
    case "$uv_mode" in
        present)
            # Records calls; on 'uv tool install' creates a specify stub
            cat > "${stub_bin}/uv" <<STUB
#!/usr/bin/env bash
echo "uv \$*" >> "${call_log}"
if [[ "\${1:-}" == "tool" && "\${2:-}" == "install" ]]; then
    printf '#!/usr/bin/env bash\\necho "specify 0.13.4"\\nexit 0\\n' \\
        > "${fake_home}/.local/bin/specify"
    chmod +x "${fake_home}/.local/bin/specify"
fi
exit 0
STUB
            chmod +x "${stub_bin}/uv"
            ;;
        broken)
            # Records calls; does NOT create specify (simulates broken install)
            cat > "${stub_bin}/uv" <<STUB
#!/usr/bin/env bash
echo "uv \$*" >> "${call_log}"
exit 0
STUB
            chmod +x "${stub_bin}/uv"
            ;;
    esac
}

# run_setup — execute setup.sh in the isolated environment, return its exit code.
# Use a minimal PATH so real ~/.local/bin (and any pre-installed specify) is excluded.
run_setup() {
    local fake_home="$1" stub_bin="$2"
    local actual_exit=0
    HOME="$fake_home" \
    PATH="${stub_bin}:${fake_home}/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    bash "$SETUP_SH" </dev/null >"${fake_home}/setup.out" 2>&1 || actual_exit=$?
    echo "$actual_exit"
}

# ────────────────────────────────────────────────────────────────
echo ""
echo "═══ spec-kit setup tests ═══"
echo ""

# ── Test 1: specify absent, uv present ───────────────────────
# Expect: exactly one 'uv tool install' call with pinned version;
#         specify callable afterward; setup exits 0.
echo "Test 1: missing specify → uv install → callable specify"
t1_base="$(mktemp -d)"
CLEANUP_DIRS+=("$t1_base")

build_env "$t1_base" "absent" "present"
t1_exit=$(run_setup "${t1_base}/home" "${t1_base}/stub-bin")
assert_eq "T1: setup exits 0" "$t1_exit" "0"

t1_calls=$(grep -c "uv tool install" "${t1_base}/calls.log" 2>/dev/null || true)
assert_eq "T1: uv tool install called exactly once" "${t1_calls:-0}" "1"

if grep -q "v0.13.4" "${t1_base}/calls.log" 2>/dev/null; then
    pass "T1: pinned version v0.13.4 used in uv call"
else
    fail "T1: pinned version v0.13.4 not found in uv call log"
fi

if grep -q "spec-kit.git" "${t1_base}/calls.log" 2>/dev/null; then
    pass "T1: spec-kit repository URL referenced in uv call"
else
    fail "T1: spec-kit repository URL not found in uv call log"
fi

if [[ -x "${t1_base}/home/.local/bin/specify" ]]; then
    pass "T1: specify is callable after install"
else
    fail "T1: specify not callable after install"
fi

echo ""

# ── Test 2: specify already present ──────────────────────────
# Expect: no uv calls; version reported; setup exits 0.
echo "Test 2: existing specify → skip install"
t2_base="$(mktemp -d)"
CLEANUP_DIRS+=("$t2_base")

build_env "$t2_base" "present" "present"
t2_exit=$(run_setup "${t2_base}/home" "${t2_base}/stub-bin")
assert_eq "T2: setup exits 0" "$t2_exit" "0"

t2_calls=$(grep -c "uv tool install" "${t2_base}/calls.log" 2>/dev/null || true)
assert_eq "T2: uv tool install not called" "${t2_calls:-0}" "0"

if grep -q "specify" "${t2_base}/home/setup.out" 2>/dev/null; then
    pass "T2: existing specify version reported in output"
else
    fail "T2: no mention of existing specify in setup output"
fi

echo ""

# ── Test 3: broken install (uv succeeds but specify not created) ──
# Expect: setup exits non-zero; error references specify.
echo "Test 3: broken install → setup fails"
t3_base="$(mktemp -d)"
CLEANUP_DIRS+=("$t3_base")

build_env "$t3_base" "absent" "broken"
t3_exit=$(run_setup "${t3_base}/home" "${t3_base}/stub-bin")

if [[ "$t3_exit" -ne 0 ]]; then
    pass "T3: setup exits non-zero on broken install"
else
    fail "T3: setup unexpectedly succeeded despite broken install"
fi

if grep -qi "specify" "${t3_base}/home/setup.out" 2>/dev/null; then
    pass "T3: failure output references specify"
else
    fail "T3: expected error message about specify not found in output"
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "═══ Results: ${PASS} passed, ${FAIL} failed ═══"
echo ""

[[ "$FAIL" -eq 0 ]]
