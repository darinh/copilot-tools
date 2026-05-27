#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# copilot-tools setup — Configure your environment for the full
# Copilot CLI power-user toolkit.
#
# Usage: ./setup.sh
#
# Idempotent: safe to re-run after a `git pull`. Use `./upgrade.sh`
# for a one-step pull-and-resync.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COPILOT_DIR="${HOME}/.copilot"
OPERATOR_HOME="${COPILOT_OPERATOR_HOME:-${HOME}/.operator}"
LOCAL_BIN="${HOME}/.local/bin"
TEMPLATE_MANIFEST="${OPERATOR_HOME}/.template-manifest"

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

# Hash a file with whichever sha256 tool is available. Echoes the hex digest
# or an empty string if the file doesn't exist / nothing is installed.
file_sha256() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$f" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$f" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$f" | awk '{print $NF}'
    else
        echo ""
    fi
}

# Smart template install. Tracks the hash of the version we last shipped in
# $TEMPLATE_MANIFEST. On re-run:
#   • Destination missing  → install fresh.
#   • Dest matches shipped → no-op silent (user hasn't customized).
#   • Dest matches NEW src → no-op silent (already up to date).
#   • Dest != shipped AND != new src → user has local edits, prompt.
#   • Dest == shipped AND != new src → auto-upgrade (unmodified user).
copy_template() {
    local src="$1" dest="$2" label="$3"
    mkdir -p "$(dirname "$dest")"
    mkdir -p "$(dirname "$TEMPLATE_MANIFEST")"
    touch "$TEMPLATE_MANIFEST"

    # Hash the EFFECTIVE output (what we'd actually write to disk), not the
    # raw source. setup.sh has no BeforeWrite mutation today, so effective ==
    # source for now, but mirroring setup.ps1's logic means adding a Linux-
    # specific header later (or any future mutator) won't silently break the
    # auto-upgrade check by causing dest_hash to never match shipped_hash.
    local src_hash dest_hash shipped_hash effective_hash
    src_hash=$(file_sha256 "$src")
    dest_hash=$(file_sha256 "$dest")
    shipped_hash=$(awk -v k="$dest" -F'  ' '$2==k {print $1; exit}' "$TEMPLATE_MANIFEST")
    # Today the effective output IS the source byte-for-byte. If a mutator is
    # ever added, hash the post-mutation bytes here instead.
    effective_hash="$src_hash"

    if [[ ! -f "$dest" ]]; then
        cp "$src" "$dest"
        info "Installed $label"
    elif [[ -n "$effective_hash" && "$dest_hash" == "$effective_hash" ]]; then
        : # already up to date — silent
    elif [[ -n "$shipped_hash" && "$dest_hash" == "$shipped_hash" ]]; then
        cp "$src" "$dest"
        info "Auto-upgraded $label (no local edits detected)"
    else
        if ask "$label has local edits AND a newer version ships. Overwrite (current saved as .bak)?"; then
            cp "$dest" "${dest}.bak"
            cp "$src" "$dest"
            info "Updated $label (previous saved to ${dest}.bak)"
        else
            warn "Skipped $label (kept your version)"
        fi
    fi

    # Record the effective hash so future runs can tell "unmodified" from
    # "user-edited" even when the source file changes between releases.
    if [[ -n "$effective_hash" ]]; then
        local tmp
        tmp=$(mktemp "${TEMPLATE_MANIFEST}.XXXXXX")
        awk -v k="$dest" '$2!=k' "$TEMPLATE_MANIFEST" > "$tmp" 2>/dev/null || true
        echo "${effective_hash}  ${dest}" >> "$tmp"
        mv "$tmp" "$TEMPLATE_MANIFEST"
    fi
}

# Returns 0 if $SCRIPT_DIR lives on a Windows mount (DrvFs / 9P), 1 otherwise.
# Primary check: filesystem type via stat (works regardless of automount root,
# so users who set [automount] root = / in /etc/wsl.conf are still detected).
# Fallback: regex match on the default /mnt/<drive>/ prefix for systems where
# stat -f is unavailable. We use this to decide whether to symlink-into-PATH
# (safe on Linux fs) or to drop a wrapper script (works around DrvFs not
# honoring +x reliably and avoids fragile cross-fs symlinks).
script_dir_on_windows_mount() {
    local fstype
    if command -v stat >/dev/null 2>&1; then
        # GNU stat: -f -c %T prints filesystem type ("drvfs", "9p", "ext2/ext3", etc.)
        # BSD stat / BusyBox: misinterprets these flags or returns a hex magic
        # number. We classify only what we recognize; anything else falls through
        # to the regex check below, so unfamiliar stat output never regresses
        # Alpine/BSD users on default /mnt/<drive>/ paths.
        fstype=$(stat -f -c %T "$SCRIPT_DIR" 2>/dev/null)
        case "$fstype" in
            drvfs|9p|cifs|smb*|ntfs|exfat|vfat|msdos) return 0 ;;
            ext2*|ext3*|ext4*|btrfs|xfs|zfs|tmpfs|overlay*|reiserfs|f2fs) return 1 ;;
            *) ;;  # unknown / empty / non-GNU stat output — try the regex.
        esac
    fi
    # Stat unavailable or didn't return a known type — fall back to the path prefix.
    [[ "$SCRIPT_DIR" =~ ^/mnt/[a-z]/ ]]
}

# Install one bin entry. On a normal Linux fs: symlink. On a /mnt/c-style WSL
# DrvFs mount: write a tiny wrapper script that execs bash with the absolute
# path. The wrapper avoids DrvFs executable-bit quirks and survives Windows-
# side `git pull` operations that may strip the +x bit on the linked target.
install_bin_entry() {
    local name="$1" target_script="$2"
    local link="${LOCAL_BIN}/${name}"

    if script_dir_on_windows_mount; then
        cat > "$link" <<WRAPPER
#!/usr/bin/env bash
exec bash "${target_script}" "\$@"
WRAPPER
        chmod +x "$link"
        info "Installed wrapper ${name} → ${target_script} (NTFS-safe)"
        return 0
    fi

    if [[ -L "$link" ]]; then
        local current expected
        current=$(readlink -f "$link")
        expected=$(readlink -f "$target_script")
        if [[ "$current" == "$expected" ]]; then
            info "${name} symlink already correct"
            return 0
        fi
    fi
    ln -sf "$target_script" "$link"
    info "Installed ${name} symlink → ${target_script}"
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
mkdir -p "$OPERATOR_HOME"
mkdir -p "${OPERATOR_HOME}/restart"
mkdir -p "${OPERATOR_HOME}/projects"
# ~/.copilot/extensions and ~/.copilot/logs are CLI-owned paths; create the
# extensions one because we install into it, but leave logs alone (the CLI
# creates it on first run).
mkdir -p "${COPILOT_DIR}/extensions"
info "Created ~/.operator/ + ~/.copilot/extensions/ directories"
echo ""

# ── Step 2b: Migrate legacy operator state ────────────────────
# Move anything that lived under ~/.copilot/projects/ (legacy location) into
# ~/.operator/projects/. Refuses gracefully if operator instances are still
# running so handoffs in flight aren't lost.
if [[ -f "${SCRIPT_DIR}/lib/migrate-operator-state.sh" ]]; then
    echo "Checking for legacy operator state to migrate..."
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/lib/migrate-operator-state.sh"
    if ! migrate_operator_state; then
        err "Legacy state migration was refused or partially failed. Resolve the messages above and re-run."
        exit 1
    fi
    echo ""
fi

# ── Step 3: Operator + handoff into PATH ──────────────────────
echo "Installing operator + handoff into ${LOCAL_BIN}..."
install_bin_entry operator "${SCRIPT_DIR}/operator.sh"
install_bin_entry handoff  "${SCRIPT_DIR}/handoff.sh"

# Ensure ~/.local/bin is on PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$LOCAL_BIN"; then
    warn "~/.local/bin is not on your PATH. Add to your shell profile:"
    echo "       export PATH=\"\$HOME/.local/bin:\$PATH\""
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

# codebase-memory-mcp
if check_cmd codebase-memory-mcp; then
    info "codebase-memory-mcp ready"
else
    warn "codebase-memory-mcp not found. Install the Go binary from your team's distribution."
fi

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
