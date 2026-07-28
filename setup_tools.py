#!/usr/bin/env python3
"""Cross-platform setup for the copilot-tools toolkit.

Idempotent by design: rerunning detects what is already installed, never
replaces user-edited configuration without consent, and treats a failed
installation as fatal rather than continuing in a half-configured state.

Windows notes
-------------
* Console scripts (``operator``, ``handoff``) come from installing the package,
  not from symlinks into ``~/.local/bin``, which has no Windows equivalent.
* Runtime extensions are linked with directory **junctions**, which unlike
  symlinks do not require Developer Mode or elevation. If a junction cannot be
  created the directory is copied and the copy is refreshed on later runs.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from operator_console import enable_utf8_output

IS_WINDOWS = platform.system() == "Windows"
REPO_ROOT = Path(__file__).resolve().parent
COPILOT_DIR = Path.home() / ".copilot"
OPERATOR_HOME = Path(os.environ.get("COPILOT_OPERATOR_HOME") or Path.home() / ".operator")
MIN_PYTHON = (3, 10)


def info(msg: str) -> None:
    print(f"  \u2705 {msg}")


def warn(msg: str) -> None:
    print(f"  \u26a0\ufe0f  {msg}")


def err(msg: str) -> None:
    print(f"  \u274c {msg}", file=sys.stderr)


def ask(question: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"  \u2192 {question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ── prerequisites ───────────────────────────────────────────────
def multiplexer_hint() -> str:
    if IS_WINDOWS:
        return "winget install --id marlocarlo.psmux"
    if platform.system() == "Darwin":
        return "brew install tmux"
    return "sudo apt install tmux   (or your distro's package manager)"


def check_prerequisites() -> int:
    print("Checking prerequisites...")
    missing = 0

    if sys.version_info < MIN_PYTHON:
        err(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(found {sys.version_info.major}.{sys.version_info.minor})")
        missing += 1
    else:
        info(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    mux = next((c for c in ("tmux", "psmux", "pmux") if shutil.which(c)), None)
    if mux:
        info(f"multiplexer found: {mux} ({shutil.which(mux)})")
    else:
        err(f"No terminal multiplexer found. Install it:\n       {multiplexer_hint()}")
        missing += 1

    for tool, hint in (
        ("git", "https://git-scm.com/downloads"),
        ("copilot", "https://docs.github.com/en/copilot/how-tos/copilot-cli"),
    ):
        path = shutil.which(tool)
        if path:
            info(f"{tool} found: {path}")
        else:
            err(f"{tool} not found. Install: {hint}")
            missing += 1

    # sqlite3 is deliberately NOT required: the toolkit uses Python's stdlib
    # sqlite3 module, so the standalone binary is unnecessary on every platform.
    return missing


# ── installation ────────────────────────────────────────────────
def install_package(assume_yes: bool = False) -> bool:
    print("\nInstalling console scripts (operator, handoff)...")
    already = shutil.which("operator") and shutil.which("handoff")
    if already:
        info("operator and handoff already on PATH")
        if not ask("Reinstall the package anyway?", assume_yes=False):
            return True
    cmd = [sys.executable, "-m", "pip", "install", "-e", str(REPO_ROOT)]
    print(f"  Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        err("pip install failed. Setup cannot continue — the console scripts "
            "would be missing and the toolkit would be unusable.")
        return False
    info("Package installed")

    if not shutil.which("operator"):
        scripts_dir = Path(sys.executable).parent
        if IS_WINDOWS:
            scripts_dir = scripts_dir / "Scripts"
        warn("'operator' is not on PATH yet. Add this directory to PATH:")
        print(f"       {scripts_dir}")
        if IS_WINDOWS:
            print('       PowerShell: $env:PATH = "' + str(scripts_dir) + ';$env:PATH"')
        else:
            print(f'       bash: export PATH="{scripts_dir}:$PATH"')
    else:
        info(f"operator resolves to {shutil.which('operator')}")
    return True


def _link_directory(src: Path, dest: Path) -> str:
    """Link src -> dest, preferring a link and falling back to a copy."""
    if dest.is_symlink() or (IS_WINDOWS and dest.is_dir() and _is_junction(dest)):
        try:
            if Path(os.path.realpath(dest)) == src.resolve():
                return "already linked"
        except OSError:
            pass
        try:
            dest.unlink()
        except OSError:
            shutil.rmtree(dest, ignore_errors=True)
    elif dest.exists():
        # A real directory: refresh the copy rather than clobbering blindly.
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return "copied (refreshed)"

    if IS_WINDOWS:
        # Junctions need neither Developer Mode nor elevation.
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dest), str(src)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return "junction created"
        shutil.copytree(src, dest)
        return "copied (junction unavailable)"

    os.symlink(src, dest)
    return "symlink created"


def _is_junction(path: Path) -> bool:
    try:
        return bool(os.readlink(str(path)))
    except OSError:
        return False


def install_extensions() -> None:
    print("\nInstalling runtime extensions...")
    src_root = REPO_ROOT / "extensions"
    if not src_root.is_dir():
        warn("No extensions/ directory found — skipping")
        return
    dest_root = COPILOT_DIR / "extensions"
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in sorted(p for p in src_root.iterdir() if p.is_dir()):
        try:
            result = _link_directory(src, dest_root / src.name)
            info(f"extension '{src.name}': {result}")
        except OSError as exc:
            warn(f"extension '{src.name}': {exc}")


def install_templates(assume_yes: bool = False) -> None:
    print("\nInstalling templates...")
    COPILOT_DIR.mkdir(parents=True, exist_ok=True)
    for src_name, dest_name, label in (
        ("mcp-config.json", "mcp-config.json", "MCP config"),
        ("copilot-instructions.md", "copilot-instructions.md", "Copilot instructions"),
    ):
        src = REPO_ROOT / "templates" / src_name
        dest = COPILOT_DIR / dest_name
        if not src.is_file():
            warn(f"{label}: source missing ({src})")
            continue
        if dest.exists():
            if src.read_bytes() == dest.read_bytes():
                info(f"{label} already up to date")
                continue
            if not ask(f"{label} exists at {dest} and differs. Overwrite?", assume_yes):
                warn(f"Skipped {label} (kept existing)")
                continue
        shutil.copyfile(src, dest)
        info(f"Installed {label}")


def scaffold_directories() -> None:
    print("\nSetting up directories...")
    for path in (COPILOT_DIR / "projects", COPILOT_DIR / "logs",
                 OPERATOR_HOME / "restart", OPERATOR_HOME / "backups"):
        path.mkdir(parents=True, exist_ok=True)
    info(f"Created {COPILOT_DIR} and {OPERATOR_HOME} directories")


def main(argv: list[str] | None = None) -> int:
    enable_utf8_output()
    parser = argparse.ArgumentParser(
        prog="setup_tools",
        description="Configure your environment for the copilot-tools toolkit.",
    )
    parser.add_argument("--yes", action="store_true",
                        help="Assume yes for overwrite prompts")
    parser.add_argument("--skip-package", action="store_true",
                        help="Do not run pip install")
    args = parser.parse_args(argv)

    print("\n\u2550\u2550\u2550 Copilot Tools Setup \u2550\u2550\u2550\n")

    missing = check_prerequisites()
    if missing:
        err(f"{missing} prerequisite(s) missing. Install them and re-run.")
        return 1

    scaffold_directories()

    if not args.skip_package:
        if not install_package(assume_yes=args.yes):
            return 1

    install_extensions()
    install_templates(assume_yes=args.yes)

    print("\n\u2550\u2550\u2550 Setup Complete \u2550\u2550\u2550\n")
    print("Next steps:")
    print("  1. Run: operator help")
    print("  2. Review ~/.copilot/copilot-instructions.md and customize")
    print("  3. Start a session: operator --agent=anvil:anvil --yolo")
    print("  4. Start an autonomous loop: operator --loop --name myproject")
    return 0


if __name__ == "__main__":
    sys.exit(main())
