#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# copilot-tools setup — Configure your environment for the full
# Copilot CLI power-user toolkit.
#
# Usage: ./setup.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COPILOT_DIR="${HOME}/.copilot"
LOCAL_BIN="${HOME}/.local/bin"
SPEC_KIT_VERSION="${SPEC_KIT_VERSION:-v0.13.4}"

# ── Helpers ─────────────────────────────────────────────────────
info()  { echo "  ✅ $*"; }
warn()  { echo "  ⚠️  $*"; }
err()   { echo "  ❌ $*" >&2; }
ask()   { read -rp "  → $1 [y/N] " ans; [[ "$ans" =~ ^[Yy] ]]; }

check_cmd() {
    if command -v "$1" &>/dev/null; then
        info "$1 found: $(command -v "$1")"
        return 0
    else
        err "$1 not found"
        return 1
    fi
}

copy_template() {
    local src="$1" dest="$2" label="$3"
    if [[ -f "$dest" ]]; then
        if ask "$label already exists at $dest. Overwrite?"; then
            cp "$src" "$dest"
            info "Updated $label"
        else
            warn "Skipped $label (kept existing)"
        fi
    else
        cp "$src" "$dest"
        info "Installed $label"
    fi
}

echo ""
echo "═══ Copilot Tools Setup ═══"
echo ""

# ── Step 1: Prerequisites ──────────────────────────────────────
echo "Checking prerequisites..."
missing=0
for cmd in tmux sqlite3 python3 git; do
    check_cmd "$cmd" || (( missing++ )) || true
done

if ! check_cmd copilot; then
    err "GitHub Copilot CLI is required. Install from: https://docs.github.com/en/copilot/github-copilot-in-the-cli"
    (( missing++ )) || true
fi

if (( missing > 0 )); then
    err "$missing prerequisite(s) missing. Install them and re-run."
    exit 1
fi
echo ""

# ── Step 2: Directory scaffolding ──────────────────────────────
echo "Setting up directories..."
mkdir -p "$LOCAL_BIN"
mkdir -p "${COPILOT_DIR}/restart"
mkdir -p "${COPILOT_DIR}/projects"
mkdir -p "${COPILOT_DIR}/logs"
info "Created ~/.copilot/ directories"
echo ""

# ── Step 3: Operator symlink ──────────────────────────────────
echo "Installing operator..."
if [[ -L "${LOCAL_BIN}/operator" ]]; then
    current_target=$(readlink -f "${LOCAL_BIN}/operator")
    expected_target=$(readlink -f "${SCRIPT_DIR}/operator.sh")
    if [[ "$current_target" == "$expected_target" ]]; then
        info "operator symlink already correct"
    else
        ln -sf "${SCRIPT_DIR}/operator.sh" "${LOCAL_BIN}/operator"
        info "Updated operator symlink → ${SCRIPT_DIR}/operator.sh"
    fi
else
    ln -sf "${SCRIPT_DIR}/operator.sh" "${LOCAL_BIN}/operator"
    info "Created operator symlink → ${SCRIPT_DIR}/operator.sh"
fi

# Ensure ~/.local/bin is on PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$LOCAL_BIN"; then
    warn "~/.local/bin is not on your PATH. Add to your shell profile:"
    echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# Handoff script symlink
if [[ -L "${LOCAL_BIN}/handoff" ]]; then
    current_target=$(readlink -f "${LOCAL_BIN}/handoff")
    expected_target=$(readlink -f "${SCRIPT_DIR}/handoff.sh")
    if [[ "$current_target" == "$expected_target" ]]; then
        info "handoff symlink already correct"
    else
        ln -sf "${SCRIPT_DIR}/handoff.sh" "${LOCAL_BIN}/handoff"
        info "Updated handoff symlink → ${SCRIPT_DIR}/handoff.sh"
    fi
else
    ln -sf "${SCRIPT_DIR}/handoff.sh" "${LOCAL_BIN}/handoff"
    info "Created handoff symlink → ${SCRIPT_DIR}/handoff.sh"
fi
echo ""

# ── Step 4: Anvil plugin ──────────────────────────────────────
echo "Installing Anvil agent plugin..."
if copilot extensions list 2>/dev/null | grep -q "anvil"; then
    info "Anvil already installed"
else
    if copilot install burkeholland/anvil 2>/dev/null; then
        info "Installed Anvil from burkeholland/anvil"
    else
        warn "Could not auto-install Anvil. Install manually:"
        echo "       copilot install burkeholland/anvil"
    fi
fi
echo ""



# ── Step 4b: Runtime Extensions ──────────────────────────────
echo "Installing runtime extensions..."
EXTENSIONS_DIR="${COPILOT_DIR}/extensions"
mkdir -p "$EXTENSIONS_DIR"
if [[ -d "${SCRIPT_DIR}/extensions" ]]; then
    shopt -s nullglob
    for ext_dir in "${SCRIPT_DIR}/extensions/"*/; do
        ext_name=$(basename "$ext_dir")
        target="${EXTENSIONS_DIR}/${ext_name}"
        src="${ext_dir%/}"
        if [[ -L "$target" ]]; then
            if [[ "$(readlink -f "$target")" == "$(readlink -f "$src")" ]]; then
                info "Extension '$ext_name' symlink already correct"
                continue
            fi
            rm "$target"
        elif [[ -e "$target" ]]; then
            warn "Extension '$ext_name' exists at $target as a real directory — skipping (remove it to symlink)"
            continue
        fi
        ln -s "$src" "$target"
        info "Linked extension '$ext_name' → $src"
    done
    shopt -u nullglob
else
    warn "No extensions/ directory found in copilot-tools — skipping"
fi
echo ""

# ── Step 5: MCP Servers ──────────────────────────────────────
echo "Checking MCP servers..."

# dotnet-roslyn-mcp
if command -v dotnet-roslyn-mcp &>/dev/null || command -v dotnet &>/dev/null && dotnet tool list -g 2>/dev/null | grep -q "roslyn-mcp"; then
    info "dotnet-roslyn-mcp ready"
else
    if command -v dotnet &>/dev/null; then
        if ask "Install dotnet-roslyn-mcp as a global .NET tool?"; then
            dotnet tool install -g dotnet-roslyn-mcp && info "Installed dotnet-roslyn-mcp" || warn "Install failed"
        fi
    else
        warn "dotnet CLI not found — cannot install roslyn-mcp. Install .NET SDK first."
    fi
fi
echo ""

# ── Step 5b: Spec-kit CLI ─────────────────────────────────────
echo "Checking spec-kit (specify)..."
if command -v specify &>/dev/null; then
    info "specify already installed: $(specify --version 2>&1 | head -1)"
else
    # Require Python >= 3.11
    python_ver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
    python_major=$(echo "$python_ver" | cut -d. -f1)
    python_minor=$(echo "$python_ver" | cut -d. -f2)
    if [[ "$python_major" -lt 3 ]] || { [[ "$python_major" -eq 3 ]] && [[ "$python_minor" -lt 11 ]]; }; then
        err "spec-kit requires Python >= 3.11 (found ${python_ver}). Upgrade Python and re-run."
        exit 1
    fi
    info "Python ${python_ver} OK"

    # Bootstrap uv via Astral's official installer when absent
    if ! command -v uv &>/dev/null; then
        info "uv not found — bootstrapping via Astral installer..."
        if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
            err "Failed to download or run the Astral uv installer."
            exit 1
        fi
        export PATH="${HOME}/.local/bin:${PATH}"
        if ! command -v uv &>/dev/null; then
            err "uv installer ran but 'uv' is still not on PATH. Add ~/.local/bin to PATH and re-run."
            exit 1
        fi
        info "uv bootstrapped"
    else
        info "uv found: $(command -v uv)"
        export PATH="${HOME}/.local/bin:${PATH}"
    fi

    # Install specify-cli from the pinned GitHub tag
    info "Installing specify-cli ${SPEC_KIT_VERSION} via uv..."
    if ! uv tool install specify-cli --from "git+https://github.com/github/spec-kit.git@${SPEC_KIT_VERSION}"; then
        err "uv tool install failed for specify-cli ${SPEC_KIT_VERSION}."
        exit 1
    fi

    # Verify specify is callable
    if ! command -v specify &>/dev/null; then
        err "'specify' is not callable after installation. Ensure ~/.local/bin is on PATH and re-run."
        exit 1
    fi
    info "specify installed: $(specify --version 2>&1 | head -1)"
fi
echo ""

# ── Step 6: Templates ────────────────────────────────────────
echo "Installing templates..."
copy_template \
    "${SCRIPT_DIR}/templates/mcp-config.json" \
    "${COPILOT_DIR}/mcp-config.json" \
    "MCP config"

copy_template \
    "${SCRIPT_DIR}/templates/copilot-instructions.md" \
    "${COPILOT_DIR}/copilot-instructions.md" \
    "Copilot instructions"
echo ""

# ── Step 7: Code Intelligence skill ─────────────────────────
echo "Installing skills..."
echo "  The code-intelligence skill should be copied into each project's"
echo "  .github/skills/ directory. Example:"
echo ""
echo "    cp -r ${SCRIPT_DIR}/skills/code-intelligence your-project/.github/skills/"
echo ""

# ── Done ─────────────────────────────────────────────────────
echo "═══ Setup Complete ═══"
echo ""
echo "Next steps:"
echo "  1. Run: operator help"
echo "  2. Copy code-intelligence skill into your project (see above)"
echo "  3. Review ~/.copilot/copilot-instructions.md and customize"
echo "  4. Start a session: operator --agent=anvil:anvil --yolo"
echo "  5. Start an autonomous loop: operator --loop --name myproject"
echo ""
