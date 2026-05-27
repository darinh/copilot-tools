#!/usr/bin/env bash
# Fixture test for lib/migrate-operator-state.sh covering:
#   1. happy-path full migration
#   2. partial-failure case (one mv fails) — legacy catalog must NOT be removed
#   3. idempotency (re-run after happy path is a no-op)
set -u

FX="/tmp/migfx-$$"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MIGRATE="$SCRIPT_DIR/migrate-operator-state.sh"
rm -rf "$FX"; mkdir -p "$FX"
export HOME="$FX"
export COPILOT_OPERATOR_HOME="$FX/.operator"

GUID1='11111111-2222-3333-4444-555555555555'
GUID2='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'

setup_legacy() {
    rm -rf "$FX/.copilot" "$FX/.operator"
    mkdir -p "$FX/.copilot/projects/$GUID1"
    echo 'data1' > "$FX/.copilot/projects/$GUID1/state.txt"
    mkdir -p "$FX/.copilot/projects/$GUID2"
    echo 'data2' > "$FX/.copilot/projects/$GUID2/state.txt"
    cat > "$FX/.copilot/projects/catalog.csv" <<CSV
"/some/path/one",$GUID1
"/some/path/two",$GUID2
CSV
}

echo "=== TEST 1: happy path full migration ==="
setup_legacy
bash "$MIGRATE"
rc=$?
echo "-> exit code: $rc (expect 0)"
test "$rc" = "0" || { echo "FAIL"; exit 1; }
test -f "$FX/.operator/projects/$GUID1/state.txt" || { echo "FAIL: GUID1 not moved"; exit 1; }
test -f "$FX/.operator/projects/$GUID2/state.txt" || { echo "FAIL: GUID2 not moved"; exit 1; }
test -f "$FX/.operator/projects/catalog.csv" || { echo "FAIL: catalog not merged"; exit 1; }
test ! -f "$FX/.copilot/projects/catalog.csv" || { echo "FAIL: legacy catalog still present"; exit 1; }
echo "OK"
echo ""

echo "=== TEST 2: partial-failure (one mv blocked) ==="
setup_legacy
# Block GUID2 move by pre-occupying destination with a NON-directory at the
# exact target path. mv refuses to overwrite a non-dir with a dir.
# But the migration script's existence check uses [[ -e ]] and SKIPS if it
# exists, so this would be "skipped" not "failed." We need to actually trigger
# the mv-failure branch.
#
# Strategy: chattr/chmod won't work on DrvFs. Instead, monkey-patch mv via
# PATH shadowing — drop a `mv` shim that returns failure for one specific path.
SHIM_DIR="$FX/shim"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/mv" <<EOSHIM
#!/usr/bin/env bash
# Fixture shim: fail mv when target contains "$GUID2", succeed otherwise.
for arg in "\$@"; do
    if [[ "\$arg" == *"$GUID2"* ]]; then
        echo "fixture-shim: simulated failure for $GUID2" >&2
        exit 1
    fi
done
exec /usr/bin/mv "\$@"
EOSHIM
chmod +x "$SHIM_DIR/mv"
PATH="$SHIM_DIR:$PATH" bash "$MIGRATE"
rc=$?
echo "-> exit code: $rc (expect 2 = partial failure)"
test "$rc" = "2" || { echo "FAIL: expected exit 2"; exit 1; }
test -f "$FX/.operator/projects/$GUID1/state.txt" || { echo "FAIL: GUID1 should have moved"; exit 1; }
test -f "$FX/.copilot/projects/$GUID2/state.txt" || { echo "FAIL: GUID2 should be stranded in legacy"; exit 1; }
test -f "$FX/.copilot/projects/catalog.csv" || { echo "FAIL: legacy catalog must NOT be removed when moves fail"; exit 1; }
# Catalog-filter assertions: the new catalog must NOT advertise GUID2 (whose
# state is still in the legacy dir). handoff.sh would otherwise short-circuit
# on a new-catalog hit and miss the legacy fallback.
grep -q "$GUID1" "$FX/.operator/projects/catalog.csv" || { echo "FAIL: GUID1 row missing from new catalog"; exit 1; }
if grep -q "$GUID2" "$FX/.operator/projects/catalog.csv"; then
    echo "FAIL: GUID2 row leaked into new catalog despite failed move"
    cat "$FX/.operator/projects/catalog.csv"
    exit 1
fi
echo "OK — GUID1 moved+merged, GUID2 held back, legacy catalog preserved"
echo ""

echo "=== TEST 3: idempotency on re-run after happy path ==="
setup_legacy
bash "$MIGRATE" >/dev/null
bash "$MIGRATE"
rc=$?
echo "-> exit code: $rc (expect 0 — no-op)"
test "$rc" = "0" || { echo "FAIL"; exit 1; }
echo "OK"
echo ""

echo "=== TEST 4: catalog-only entry (no legacy dir) merges regardless ==="
rm -rf "$FX/.copilot" "$FX/.operator"
mkdir -p "$FX/.copilot/projects"
GUID3='99999999-8888-7777-6666-555544443333'
cat > "$FX/.copilot/projects/catalog.csv" <<CSV
"/some/orphan/path",$GUID3
CSV
bash "$MIGRATE"
rc=$?
echo "-> exit code: $rc (expect 0)"
test "$rc" = "0" || { echo "FAIL"; exit 1; }
grep -q "$GUID3" "$FX/.operator/projects/catalog.csv" || { echo "FAIL: catalog-only row not merged"; exit 1; }
echo "OK"
echo ""

rm -rf "$FX"
echo "=== ALL TESTS PASSED ==="
