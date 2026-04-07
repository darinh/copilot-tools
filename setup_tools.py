#!/usr/bin/env python3
"""copilot-tools setup — Configure your environment for the full
Copilot CLI power-user toolkit. Cross-platform (Linux, macOS, Windows).

Usage: python setup_tools.py
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
COPILOT_DIR = Path.home() / ".copilot"
IS_WINDOWS = platform.system() == 'Windows'


def info(msg):
    print(f"  ✅ {msg}")


def warn(msg):
    print(f"  ⚠️  {msg}")


def err(msg):
    print(f"  ❌ {msg}", file=sys.stderr)


def ask(prompt):
    try:
        ans = input(f"  → {prompt} [y/N] ").strip()
        return ans.lower().startswith('y')
    except (EOFError, KeyboardInterrupt):
        return False


def check_cmd(name):
    path = shutil.which(name)
    if path:
        info(f"{name} found: {path}")
        return True
    err(f"{name} not found")
    return False


def copy_template(src, dest, label):
    if dest.exists():
        if ask(f"{label} already exists at {dest}. Overwrite?"):
            shutil.copy2(src, dest)
            info(f"Updated {label}")
        else:
            warn(f"Skipped {label} (kept existing)")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        info(f"Installed {label}")


def main():
    print()
    print("═══ Copilot Tools Setup ═══")
    print()

    # ── Step 1: Prerequisites ──────────────────────────────────
    print("Checking prerequisites...")
    missing = 0

    # tmux or psmux
    if IS_WINDOWS:
        if not (check_cmd('tmux') or check_cmd('psmux') or check_cmd('pmux')):
            err("psmux not found. Install: winget install psmux")
            missing += 1
    else:
        if not check_cmd('tmux'):
            missing += 1

    if not check_cmd('python3') and not check_cmd('python'):
        missing += 1

    if not check_cmd('git'):
        missing += 1

    if not check_cmd('copilot'):
        err("GitHub Copilot CLI is required. Install from: "
            "https://docs.github.com/en/copilot/github-copilot-in-the-cli")
        missing += 1

    if missing > 0:
        err(f"{missing} prerequisite(s) missing. Install them and re-run.")
        sys.exit(1)
    print()

    # ── Step 2: Directory scaffolding ──────────────────────────
    print("Setting up directories...")
    for d in [COPILOT_DIR / 'restart', COPILOT_DIR / 'projects', COPILOT_DIR / 'logs']:
        d.mkdir(parents=True, exist_ok=True)
    info("Created ~/.copilot/ directories")
    print()

    # ── Step 3: Install operator ───────────────────────────────
    print("Installing operator...")

    # Try pip install (preferred — works cross-platform)
    if shutil.which('pip3') or shutil.which('pip'):
        pip_cmd = 'pip3' if shutil.which('pip3') else 'pip'
        if ask("Install copilot-operator command via pip (recommended)?"):
            r = subprocess.run([pip_cmd, 'install', '-e', str(SCRIPT_DIR)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                info("Installed copilot-operator command via pip")
            else:
                warn(f"pip install failed: {r.stderr.strip()}")
                warn("You can run directly: python copilot_operator.py")
        else:
            warn("Skipped pip install")
    elif not IS_WINDOWS:
        # Fallback: symlink on Unix
        local_bin = Path.home() / '.local' / 'bin'
        local_bin.mkdir(parents=True, exist_ok=True)
        link = local_bin / 'operator'
        target = SCRIPT_DIR / 'operator.sh'
        if link.is_symlink() or link.exists():
            if link.resolve() == target.resolve():
                info("operator symlink already correct")
            else:
                link.unlink()
                link.symlink_to(target)
                info(f"Updated operator symlink → {target}")
        else:
            link.symlink_to(target)
            info(f"Created operator symlink → {target}")

        if str(local_bin) not in os.environ.get('PATH', ''):
            warn("~/.local/bin is not on your PATH. Add to your shell profile:")
            print(f'       export PATH="$HOME/.local/bin:$PATH"')
    print()

    # ── Step 4: Anvil plugin ──────────────────────────────────
    print("Installing Anvil agent plugin...")
    if shutil.which('copilot'):
        r = subprocess.run(['copilot', 'extensions', 'list'],
                           capture_output=True, text=True)
        if 'anvil' in r.stdout:
            info("Anvil already installed")
        else:
            r2 = subprocess.run(['copilot', 'install', 'burkeholland/anvil'],
                                capture_output=True, text=True)
            if r2.returncode == 0:
                info("Installed Anvil from burkeholland/anvil")
            else:
                warn("Could not auto-install Anvil. Install manually:")
                print("       copilot install burkeholland/anvil")
    print()

    # ── Step 5: MCP Servers ──────────────────────────────────
    print("Checking MCP servers...")
    if check_cmd('codebase-memory-mcp'):
        info("codebase-memory-mcp ready")
    else:
        warn("codebase-memory-mcp not found. Install the Go binary from your team's distribution.")

    if shutil.which('dotnet'):
        r = subprocess.run(['dotnet', 'tool', 'list', '-g'],
                           capture_output=True, text=True)
        if 'roslyn-mcp' in r.stdout:
            info("dotnet-roslyn-mcp ready")
        elif ask("Install dotnet-roslyn-mcp as a global .NET tool?"):
            r2 = subprocess.run(['dotnet', 'tool', 'install', '-g', 'dotnet-roslyn-mcp'],
                                capture_output=True, text=True)
            if r2.returncode == 0:
                info("Installed dotnet-roslyn-mcp")
            else:
                warn("Install failed")
    print()

    # ── Step 6: Templates ────────────────────────────────────
    print("Installing templates...")
    copy_template(
        SCRIPT_DIR / 'templates' / 'mcp-config.json',
        COPILOT_DIR / 'mcp-config.json',
        'MCP config')
    copy_template(
        SCRIPT_DIR / 'templates' / 'copilot-instructions.md',
        COPILOT_DIR / 'copilot-instructions.md',
        'Copilot instructions')
    print()

    # ── Step 7: Code Intelligence skill ─────────────────────
    print("Installing skills...")
    print("  The code-intelligence skill should be copied into each project's")
    print("  .github/skills/ directory. Example:")
    print()
    print(f"    cp -r {SCRIPT_DIR / 'skills' / 'code-intelligence'} your-project/.github/skills/")
    print()

    # ── Done ─────────────────────────────────────────────────
    print("═══ Setup Complete ═══")
    print()
    print("Next steps:")
    if shutil.which('copilot-operator'):
        print("  1. Run: copilot-operator help")
    else:
        print(f"  1. Run: python {SCRIPT_DIR / 'copilot_operator.py'} help")
    print("  2. Copy code-intelligence skill into your project (see above)")
    print("  3. Review ~/.copilot/copilot-instructions.md and customize")
    if IS_WINDOWS:
        print("  4. Start a session: copilot-operator --agent=anvil:anvil --yolo")
    else:
        print("  4. Start a session: operator --agent=anvil:anvil --yolo")
    print()


if __name__ == '__main__':
    main()
