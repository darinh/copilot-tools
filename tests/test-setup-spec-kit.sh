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
# $2  specify mode: "absent" | "present" | "broken"
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
        cat > "${fake_home}/.local/bin/specify" <<'STUB'
#!/usr/bin/env bash
echo "specify 0.13.4"
exit 0
STUB
        chmod +x "${fake_home}/.local/bin/specify"
    elif [[ "$specify_mode" == "broken" ]]; then
        cat > "${fake_home}/.local/bin/specify" <<'STUB'
#!/usr/bin/env bash
echo "broken specify" >&2
exit 1
STUB
        chmod +x "${fake_home}/.local/bin/specify"
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
    local fake_home="$1" stub_bin="$2" spec_kit_version="${3:-}"
    local actual_exit=0
    if [[ -n "$spec_kit_version" ]]; then
        SPEC_KIT_VERSION="$spec_kit_version" \
        HOME="$fake_home" \
        PATH="${stub_bin}:/usr/local/bin:/usr/bin:/bin" \
        bash "$SETUP_SH" </dev/null >"${fake_home}/setup.out" 2>&1 || actual_exit=$?
    else
        HOME="$fake_home" \
        PATH="${stub_bin}:/usr/local/bin:/usr/bin:/bin" \
        bash "$SETUP_SH" </dev/null >"${fake_home}/setup.out" 2>&1 || actual_exit=$?
    fi
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

# ── Test 4: both specify and uv absent → bootstrap uv via curl → install specify ──
# Expect: curl called once; exactly one 'uv tool install' with pinned version;
#         specify callable; setup exits 0. No real network calls.
echo "Test 4: no specify, no uv → bootstrap uv via curl → install specify"
t4_base="$(mktemp -d)"
CLEANUP_DIRS+=("$t4_base")

t4_home="${t4_base}/home"
t4_stub="${t4_base}/stub-bin"
t4_curl_log="${t4_base}/curl-calls.log"
mkdir -p "$t4_stub" "${t4_home}/.local/bin"
touch "$t4_curl_log"

# Standard prereq stubs (same minimal set as build_env)
for _cmd in tmux sqlite3 git; do
    printf '#!/usr/bin/env bash\nexit 0\n' > "${t4_stub}/${_cmd}"
    chmod +x "${t4_stub}/${_cmd}"
done
cat > "${t4_stub}/python3" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
    -c) echo "3.11" ;;
    --version) echo "Python 3.11.0" ;;
esac
exit 0
STUB
chmod +x "${t4_stub}/python3"
printf '#!/usr/bin/env bash\nexit 0\n' > "${t4_stub}/copilot"
chmod +x "${t4_stub}/copilot"
printf '#!/usr/bin/env bash\nexit 0\n' > "${t4_stub}/dotnet-roslyn-mcp"
chmod +x "${t4_stub}/dotnet-roslyn-mcp"

# uv stub: records calls; on 'tool install' creates a specify stub.
# Written to a shared file; the fake installer will copy it into ~/.local/bin.
cat > "${t4_base}/uv-stub.sh" <<'UVSTUB'
#!/usr/bin/env bash
echo "uv $*" >> "${HOME}/.local/uv-calls.log"
if [[ "${1:-}" == "tool" && "${2:-}" == "install" ]]; then
    mkdir -p "${HOME}/.local/bin"
    printf '#!/usr/bin/env bash\necho "specify 0.13.4"\nexit 0\n' \
        > "${HOME}/.local/bin/specify"
    chmod +x "${HOME}/.local/bin/specify"
fi
exit 0
UVSTUB
chmod +x "${t4_base}/uv-stub.sh"

# Fake installer: places uv-stub.sh at ~/.local/bin/uv when executed by sh.
# ${t4_base} is expanded at write time (known path); ${HOME} expands at run time.
cat > "${t4_base}/fake-installer.sh" <<INSTALLER
#!/usr/bin/env bash
mkdir -p "\${HOME}/.local/bin"
cp "${t4_base}/uv-stub.sh" "\${HOME}/.local/bin/uv"
chmod +x "\${HOME}/.local/bin/uv"
INSTALLER
chmod +x "${t4_base}/fake-installer.sh"

# curl stub: records the call and copies the fake installer to the -o target.
cat > "${t4_stub}/curl" <<CURLSTUB
#!/usr/bin/env bash
echo "curl \$*" >> "${t4_curl_log}"
prev=""
for arg in "\$@"; do
    if [[ "\$prev" == "-o" ]]; then
        cp "${t4_base}/fake-installer.sh" "\$arg"
        break
    fi
    prev="\$arg"
done
exit 0
CURLSTUB
chmod +x "${t4_stub}/curl"

t4_exit=$(run_setup "$t4_home" "$t4_stub")
assert_eq "T4: setup exits 0" "$t4_exit" "0"

if grep -q "curl" "$t4_curl_log" 2>/dev/null; then
    pass "T4: curl called to download uv installer"
else
    fail "T4: curl not called"
fi

t4_uv_calls=$(grep -c "uv tool install" "${t4_home}/.local/uv-calls.log" 2>/dev/null || true)
assert_eq "T4: uv tool install called exactly once after bootstrap" "${t4_uv_calls:-0}" "1"

if grep -q "v0.13.4" "${t4_home}/.local/uv-calls.log" 2>/dev/null; then
    pass "T4: pinned version v0.13.4 used after bootstrap"
else
    fail "T4: pinned version v0.13.4 not found in uv call log"
fi

if [[ -x "${t4_home}/.local/bin/specify" ]]; then
    pass "T4: specify callable after bootstrap install"
else
    fail "T4: specify not callable after bootstrap install"
fi

echo ""

# ── Test 5: version override ─────────────────────────────────
# Expect: SPEC_KIT_VERSION controls the pinned GitHub tag.
echo "Test 5: SPEC_KIT_VERSION overrides the default pin"
t5_base="$(mktemp -d)"
CLEANUP_DIRS+=("$t5_base")

build_env "$t5_base" "absent" "present"
t5_exit=$(run_setup "${t5_base}/home" "${t5_base}/stub-bin" "v9.9.9")
assert_eq "T5: setup exits 0" "$t5_exit" "0"

if grep -q "spec-kit.git@v9.9.9" "${t5_base}/calls.log" 2>/dev/null; then
    pass "T5: configured spec-kit version used in uv call"
else
    fail "T5: configured spec-kit version not found in uv call log"
fi

echo ""

# ── Test 6: broken existing specify ──────────────────────────
# Expect: setup fails explicitly and does not try to reinstall over the shim.
echo "Test 6: broken existing specify → explicit failure"
t6_base="$(mktemp -d)"
CLEANUP_DIRS+=("$t6_base")

build_env "$t6_base" "broken" "present"
t6_exit=$(run_setup "${t6_base}/home" "${t6_base}/stub-bin")

if [[ "$t6_exit" -ne 0 ]]; then
    pass "T6: setup exits non-zero for broken existing specify"
else
    fail "T6: setup accepted a broken existing specify"
fi

t6_calls=$(grep -c "uv tool install" "${t6_base}/calls.log" 2>/dev/null || true)
assert_eq "T6: setup does not reinstall over broken specify" "${t6_calls:-0}" "0"

if grep -q "failed its version check" "${t6_base}/home/setup.out" 2>/dev/null; then
    pass "T6: failure explains the broken specify command"
else
    fail "T6: broken specify failure was not actionable"
fi

echo ""

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "═══ Results: ${PASS} passed, ${FAIL} failed ═══"
echo ""

[[ "$FAIL" -eq 0 ]]
