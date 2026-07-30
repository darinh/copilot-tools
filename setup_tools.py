#!/usr/bin/env python3
"""Cross-platform setup for the copilot-tools toolkit.

Idempotent by design: rerunning detects what is already installed, never
replaces user-edited configuration without consent, and treats a failed
installation as fatal rather than continuing in a half-configured state.

Setup *provisions* rather than merely audits: a missing prerequisite is
installed with the platform's package manager instead of being reported back
to the user as homework. Only genuinely unautomatable cases (no package
manager at all, an install that fails) stop the run, and those say exactly
what to do by hand.

Windows notes
-------------
* Console scripts (``operator``, ``handoff``) come from installing the package,
  not from symlinks into ``~/.local/bin``, which has no Windows equivalent.
* Runtime extensions are linked with directory **junctions**, which unlike
  symlinks do not require Developer Mode or elevation. If a junction cannot be
  created the directory is copied and the copy is refreshed on later runs.
* Installers routinely extend PATH in the registry without touching the
  already-running process, so PATH is re-read after each install.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from operator_console import enable_utf8_output

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
REPO_ROOT = Path(__file__).resolve().parent
COPILOT_DIR = Path.home() / ".copilot"
OPERATOR_HOME = Path(os.environ.get("COPILOT_OPERATOR_HOME") or Path.home() / ".operator")
MIN_PYTHON = (3, 10)
# spec-kit's own floor is higher than this toolkit's.
MIN_SPEC_KIT_PYTHON = (3, 11)
SPEC_KIT_VERSION = os.environ.get("SPEC_KIT_VERSION", "v0.13.4")
PSMUX_VERSION = os.environ.get("PSMUX_VERSION", "3.3.7")
LOCAL_BIN = Path.home() / ".local" / "bin"


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


# ── running external commands ───────────────────────────────────
def run(cmd: list[str], *, quiet: bool = False) -> bool:
    """Run a command, echoing it first. True when it exits 0."""
    if not quiet:
        print(f"     $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=quiet, text=True)
    except OSError as exc:
        warn(f"could not run {cmd[0]}: {exc}")
        return False
    return proc.returncode == 0


def which(name: str) -> str | None:
    """shutil.which, but sees PATH changes made by installers mid-run."""
    return shutil.which(name)


def capture(cmd: list[str]) -> tuple[bool, str]:
    """Run a command for its output. Returns (succeeded, stdout)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return False, ""
    return proc.returncode == 0, proc.stdout or ""


# ── PATH maintenance ────────────────────────────────────────────
def _prepend_process_path(directory: Path) -> None:
    entry = str(directory)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if entry not in parts:
        os.environ["PATH"] = os.pathsep.join([entry] + parts)


def refresh_path() -> None:
    """Re-read PATH from the registry (Windows) so freshly installed tools
    are visible without restarting the shell.

    Windows installers write PATH to the registry and broadcast a settings
    change that already-running processes never see, so a tool installed
    moments ago is invisible to ``shutil.which`` until this runs.
    """
    if not IS_WINDOWS:
        return
    ps = which("powershell") or which("pwsh")
    if not ps:
        return
    script = (
        "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
        "[Environment]::GetEnvironmentVariable('Path','User')"
    )
    try:
        proc = subprocess.run([ps, "-NoProfile", "-Command", script],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    seen = {p for p in current if p}
    added = [p for p in proc.stdout.strip().split(os.pathsep)
             if p and p not in seen]
    if added:
        os.environ["PATH"] = os.pathsep.join(current + added)


def persist_user_path(directory: Path) -> None:
    """Add a directory to PATH for this process and for future shells."""
    _prepend_process_path(directory)
    if IS_WINDOWS:
        ps = which("powershell") or which("pwsh")
        if not ps:
            return
        # setx is deliberately avoided: it truncates PATH at 1024 characters.
        script = (
            "$d = $args[0]; "
            "$p = [Environment]::GetEnvironmentVariable('Path','User'); "
            "if (-not $p) { $p = '' }; "
            "if (($p -split ';') -notcontains $d) { "
            "  $new = if ($p) { $d + ';' + $p } else { $d }; "
            "  [Environment]::SetEnvironmentVariable('Path', $new, 'User') }"
        )
        run([ps, "-NoProfile", "-Command", script, str(directory)], quiet=True)
        info(f"Added {directory} to your user PATH (new shells pick it up)")
        return

    # POSIX: append to the profile the user's login shell actually reads.
    shell = Path(os.environ.get("SHELL", "")).name
    profile = {
        "zsh": Path.home() / ".zshrc",
        "bash": Path.home() / ".bashrc",
    }.get(shell, Path.home() / ".profile")
    line = f'export PATH="{directory}:$PATH"'
    try:
        existing = profile.read_text(encoding="utf-8") if profile.is_file() else ""
        if str(directory) not in existing:
            with open(profile, "a", encoding="utf-8") as fh:
                fh.write(f"\n# added by copilot-tools setup\n{line}\n")
            info(f"Added {directory} to PATH in {profile}")
    except OSError as exc:
        warn(f"Could not update {profile}: {exc}. Add manually: {line}")


# ── platform package managers ───────────────────────────────────
# Logical name -> package name per manager. A value of None means the manager
# cannot supply that tool and a dedicated installer is used instead.
PACKAGES: dict[str, dict[str, str | None]] = {
    "tmux":   {"brew": "tmux", "apt-get": "tmux", "dnf": "tmux",
               "pacman": "tmux", "zypper": "tmux", "apk": "tmux",
               "winget": None},
    "git":    {"brew": "git", "apt-get": "git", "dnf": "git",
               "pacman": "git", "zypper": "git", "apk": "git",
               "winget": "Git.Git"},
    "node":   {"brew": "node", "apt-get": "nodejs", "dnf": "nodejs",
               "pacman": "nodejs", "zypper": "nodejs", "apk": "nodejs",
               "winget": "OpenJS.NodeJS.LTS"},
    "npm":    {"brew": "node", "apt-get": "npm", "dnf": "npm",
               "pacman": "npm", "zypper": "npm", "apk": "npm",
               "winget": "OpenJS.NodeJS.LTS"},
}

LINUX_MANAGERS = (
    ("apt-get", ["install", "-y"]),
    ("dnf", ["install", "-y"]),
    ("pacman", ["-S", "--noconfirm"]),
    ("zypper", ["install", "-y"]),
    ("apk", ["add"]),
)

_APT_UPDATED = False


def _sudo_prefix() -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    sudo = which("sudo")
    return [sudo] if sudo else []


def detect_package_manager() -> str | None:
    if IS_WINDOWS:
        return "winget" if which("winget") else None
    if IS_MACOS:
        return "brew" if which("brew") else None
    for name, _ in LINUX_MANAGERS:
        if which(name):
            return name
    return None


def install_system_package(logical: str) -> bool:
    """Install a logical tool with the platform package manager."""
    global _APT_UPDATED
    manager = detect_package_manager()
    if not manager:
        warn(f"No supported package manager found to install '{logical}'.")
        return False
    package = PACKAGES.get(logical, {}).get(manager)
    if not package:
        return False

    exe = which(manager)
    if not exe:
        return False

    if manager == "winget":
        ok = run([exe, "install", "--id", package, "--exact", "--silent",
                  "--accept-package-agreements", "--accept-source-agreements",
                  "--disable-interactivity"])
    elif manager == "brew":
        ok = run([exe, "install", package])
    else:
        sudo = _sudo_prefix()
        if manager == "apt-get" and not _APT_UPDATED:
            run(sudo + [exe, "update"])
            _APT_UPDATED = True
        flags = next(f for n, f in LINUX_MANAGERS if n == manager)
        env_prefix = ["env", "DEBIAN_FRONTEND=noninteractive"] if manager == "apt-get" else []
        ok = run(sudo + env_prefix + [exe] + flags + [package])

    refresh_path()
    return ok


# ── individual prerequisites ────────────────────────────────────
def multiplexer_hint() -> str:
    if IS_WINDOWS:
        return "winget install --id marlocarlo.psmux"
    if IS_MACOS:
        return "brew install tmux"
    return "sudo apt install tmux   (or your distro's package manager)"


def find_multiplexer() -> str | None:
    return next((c for c in ("tmux", "psmux", "pmux") if which(c)), None)


def _install_psmux_from_release() -> bool:
    """Fetch psmux from its GitHub release into ~/.operator/bin.

    winget is not guaranteed to carry psmux, and a self-contained download
    keeps Windows setup working on machines where it doesn't.
    """
    url = (f"https://github.com/psmux/psmux/releases/download/"
           f"v{PSMUX_VERSION}/psmux-v{PSMUX_VERSION}-windows-x64.zip")
    dest = OPERATOR_HOME / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    print(f"     downloading {url}")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "psmux.zip"
            # Extract to a subdirectory so the archive itself is never one of
            # the "extracted" files copied to the destination.
            extracted = Path(tmp) / "extracted"
            extracted.mkdir()
            with urllib.request.urlopen(url, timeout=120) as resp:
                archive.write_bytes(resp.read())
            shutil.unpack_archive(str(archive), str(extracted))
            exe = next((p for p in extracted.rglob("psmux.exe")), None)
            if not exe:
                warn("psmux.exe was not found inside the downloaded archive.")
                return False
            for item in exe.parent.iterdir():
                if item.is_file():
                    shutil.copy2(item, dest / item.name)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        warn(f"psmux download failed: {exc}")
        return False
    persist_user_path(dest)
    return which("psmux") is not None


def ensure_multiplexer() -> bool:
    mux = find_multiplexer()
    if mux:
        info(f"multiplexer found: {mux} ({which(mux)})")
        return True

    print("  Installing a terminal multiplexer...")
    if IS_WINDOWS:
        if which("winget"):
            run([which("winget"), "install", "--id", "marlocarlo.psmux",
                 "--exact", "--silent", "--accept-package-agreements",
                 "--accept-source-agreements", "--disable-interactivity"])
            refresh_path()
        if not find_multiplexer():
            _install_psmux_from_release()
    else:
        install_system_package("tmux")

    mux = find_multiplexer()
    if mux:
        info(f"multiplexer installed: {mux} ({which(mux)})")
        return True
    err(f"Could not install a terminal multiplexer automatically. "
        f"Install it manually:\n       {multiplexer_hint()}")
    return False


def ensure_git() -> bool:
    if which("git"):
        info(f"git found: {which('git')}")
        return True
    print("  Installing git...")
    install_system_package("git")
    if which("git"):
        info(f"git installed: {which('git')}")
        return True
    err("Could not install git automatically. Install it from "
        "https://git-scm.com/downloads and re-run.")
    return False


def ensure_npm() -> str | None:
    npm = which("npm")
    if npm:
        return npm
    print("  Installing Node.js (needed for the Copilot CLI)...")
    install_system_package("node")
    if not which("npm"):
        install_system_package("npm")
    return which("npm")


def ensure_copilot() -> bool:
    if which("copilot"):
        info(f"copilot found: {which('copilot')}")
        return True

    print("  Installing the GitHub Copilot CLI...")
    npm = ensure_npm()
    if not npm:
        err("npm is unavailable, so the Copilot CLI cannot be installed. "
            "Install Node.js from https://nodejs.org and re-run.")
        return False
    run([npm, "install", "-g", "@github/copilot"])
    refresh_path()
    if not which("copilot"):
        # npm's global bin is frequently absent from PATH on a fresh install.
        ok, out = capture([npm, "prefix", "-g"])
        if ok and out.strip():
            root = Path(out.strip())
            bin_dir = root if IS_WINDOWS else root / "bin"
            if bin_dir.is_dir():
                persist_user_path(bin_dir)
    if which("copilot"):
        info(f"copilot installed: {which('copilot')}")
        return True
    err("Could not install the Copilot CLI automatically. Install it with:\n"
        "       npm install -g @github/copilot")
    return False


def ensure_uv() -> str | None:
    """Install Astral's uv, used to install the spec-kit CLI."""
    if which("uv"):
        info(f"uv found: {which('uv')}")
        return which("uv")

    print("  Installing uv...")
    if IS_WINDOWS:
        ps = which("powershell") or which("pwsh")
        if ps:
            run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "irm https://astral.sh/uv/install.ps1 | iex"])
    else:
        curl = which("curl")
        sh = which("sh")
        if curl and sh:
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "uv-install.sh"
                # Downloaded to a file first so a truncated transfer cannot be
                # executed as a half-written script.
                if run([curl, "-LsSf", "https://astral.sh/uv/install.sh",
                        "-o", str(script)]):
                    run([sh, str(script)])
    # uv installs itself into ~/.local/bin on every platform.
    if LOCAL_BIN.is_dir():
        persist_user_path(LOCAL_BIN)
    refresh_path()
    if which("uv"):
        info(f"uv installed: {which('uv')}")
        return which("uv")
    warn("Could not install uv automatically; skipping spec-kit.")
    return None


def ensure_specify() -> bool:
    """Install the spec-kit CLI (``specify``)."""
    print("\nInstalling spec-kit (specify)...")
    if which("specify"):
        ok, out = capture([which("specify"), "--version"])
        if ok:
            first = out.strip().splitlines()[0] if out.strip() else ""
            info(f"specify already installed: {first}" if first
                 else "specify already installed")
            return True
        warn("specify is present but failed its version check; reinstalling.")

    if sys.version_info < MIN_SPEC_KIT_PYTHON:
        warn(f"spec-kit needs Python {MIN_SPEC_KIT_PYTHON[0]}.{MIN_SPEC_KIT_PYTHON[1]}+ "
             f"(this interpreter is {sys.version_info.major}.{sys.version_info.minor}); "
             "skipping. uv will still pick a suitable Python if one is installed.")

    uv = ensure_uv()
    if not uv:
        return False
    ok = run([uv, "tool", "install", "--force", "specify-cli", "--from",
              f"git+https://github.com/github/spec-kit.git@{SPEC_KIT_VERSION}"])
    if LOCAL_BIN.is_dir():
        persist_user_path(LOCAL_BIN)
    refresh_path()
    if which("specify"):
        info(f"specify installed: {which('specify')}")
        return True
    warn("specify did not install cleanly. Install it manually with:\n"
         f"       uv tool install specify-cli --from "
         f"git+https://github.com/github/spec-kit.git@{SPEC_KIT_VERSION}")
    return ok


ANVIL_SOURCE = "burkeholland/anvil"


def ensure_anvil() -> None:
    print("\nInstalling the Anvil agent plugin...")
    copilot = which("copilot")
    if not copilot:
        warn("copilot is unavailable; skipping Anvil.")
        return
    # `copilot plugin ...` is the current CLI surface. The older
    # `copilot extensions list` / `copilot install <repo>` spellings are
    # rejected outright, and their failure used to be hidden behind a
    # 2>/dev/null, so Anvil silently never installed.
    ok, out = capture([copilot, "plugin", "list"])
    if ok and "anvil" in out.lower():
        info("Anvil already installed")
        return
    if run([copilot, "plugin", "install", ANVIL_SOURCE]):
        info(f"Installed Anvil from {ANVIL_SOURCE}")
    else:
        warn("Could not auto-install Anvil. Install manually:\n"
             f"       copilot plugin install {ANVIL_SOURCE}")


def ensure_roslyn_mcp(assume_yes: bool = False) -> None:
    print("\nChecking MCP servers...")
    if which("dotnet-roslyn-mcp"):
        info("dotnet-roslyn-mcp ready")
        return
    dotnet = which("dotnet")
    if not dotnet:
        warn("dotnet CLI not found — skipping the optional roslyn MCP server. "
             "Install the .NET SDK first if you want C# code intelligence.")
        return
    listing_ok, listing = capture([dotnet, "tool", "list", "-g"])
    if listing_ok and "roslyn-mcp" in listing:
        info("dotnet-roslyn-mcp ready")
        return
    if not ask("Install dotnet-roslyn-mcp as a global .NET tool?", assume_yes):
        warn("Skipped dotnet-roslyn-mcp")
        return
    if run([dotnet, "tool", "install", "-g", "dotnet-roslyn-mcp"]):
        info("Installed dotnet-roslyn-mcp")
    else:
        warn("dotnet-roslyn-mcp install failed")


def check_prerequisites() -> int:
    """Report how many required tools are still missing. Pure audit: it
    installs nothing, so it doubles as the post-provisioning verification."""
    print("Checking prerequisites...")
    missing = 0

    if sys.version_info < MIN_PYTHON:
        err(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(found {sys.version_info.major}.{sys.version_info.minor})")
        missing += 1
    else:
        info(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    mux = find_multiplexer()
    if mux:
        info(f"multiplexer found: {mux} ({which(mux)})")
    else:
        err(f"No terminal multiplexer found. Install it:\n       {multiplexer_hint()}")
        missing += 1

    for tool, hint in (
        ("git", "https://git-scm.com/downloads"),
        ("copilot", "https://docs.github.com/en/copilot/how-tos/copilot-cli"),
    ):
        path = which(tool)
        if path:
            info(f"{tool} found: {path}")
        else:
            err(f"{tool} not found. Install: {hint}")
            missing += 1

    # sqlite3 is deliberately NOT required: the toolkit uses Python's stdlib
    # sqlite3 module, so the standalone binary is unnecessary on every platform.
    return missing


def ensure_prerequisites() -> int:
    """Install every missing prerequisite, then report what is still absent.

    Setup exists to make the machine ready. Telling the user to go install
    things by hand is a last resort, not the first response.
    """
    print("Checking prerequisites...")
    missing = 0

    if sys.version_info < MIN_PYTHON:
        # The interpreter running this file cannot upgrade itself.
        err(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required "
            f"(found {sys.version_info.major}.{sys.version_info.minor}). "
            "Install a newer Python and re-run.")
        return 1
    info(f"Python {sys.version_info.major}.{sys.version_info.minor}")

    refresh_path()
    if not ensure_multiplexer():
        missing += 1
    if not ensure_git():
        missing += 1
    if not ensure_copilot():
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


def _dirs_match(a: Path, b: Path) -> bool:
    """True when two directory trees have identical file names and contents."""
    try:
        a_files = {p.relative_to(a) for p in a.rglob("*") if p.is_file()}
        b_files = {p.relative_to(b) for p in b.rglob("*") if p.is_file()}
    except OSError:
        return False
    if a_files != b_files:
        return False
    for rel in a_files:
        try:
            if (a / rel).read_bytes() != (b / rel).read_bytes():
                return False
        except OSError:
            return False
    return True


def _link_directory(src: Path, dest: Path, assume_yes: bool = False) -> str:
    """Link src -> dest, preferring a link and falling back to a copy.

    A real directory at ``dest`` may contain edits the user made in place, so it
    is never removed without consent.
    """
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
        if dest.is_dir() and _dirs_match(src, dest):
            return "already up to date"
        if not ask(f"{dest} exists and differs from the repository copy. Replace it?",
                   assume_yes):
            return "skipped (kept existing)"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return "replaced with a copy"

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


def install_extensions(assume_yes: bool = False) -> None:
    print("\nInstalling runtime extensions...")
    src_root = REPO_ROOT / "extensions"
    if not src_root.is_dir():
        warn("No extensions/ directory found — skipping")
        return
    dest_root = COPILOT_DIR / "extensions"
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in sorted(p for p in src_root.iterdir() if p.is_dir()):
        try:
            result = _link_directory(src, dest_root / src.name, assume_yes)
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
    parser.add_argument("--check-only", action="store_true",
                        help="Report missing prerequisites without installing anything")
    parser.add_argument("--no-install-prereqs", dest="install_prereqs",
                        action="store_false",
                        help="Do not install missing prerequisites automatically")
    parser.add_argument("--skip-optional", action="store_true",
                        help="Skip Anvil, spec-kit, and MCP server provisioning")
    args = parser.parse_args(argv)

    print("\n\u2550\u2550\u2550 Copilot Tools Setup \u2550\u2550\u2550\n")

    if args.check_only:
        missing = check_prerequisites()
        if missing:
            err(f"{missing} prerequisite(s) missing.")
            return 1
        info("All prerequisites present")
        return 0

    if args.install_prereqs:
        missing = ensure_prerequisites()
    else:
        missing = check_prerequisites()
    if missing:
        err(f"{missing} prerequisite(s) could not be installed automatically. "
            "See the instructions above, then re-run.")
        return 1

    scaffold_directories()

    if not args.skip_package:
        if not install_package(assume_yes=args.yes):
            return 1

    install_extensions(assume_yes=args.yes)
    install_templates(assume_yes=args.yes)

    if not args.skip_optional:
        ensure_anvil()
        ensure_specify()
        ensure_roslyn_mcp(assume_yes=args.yes)

    print("\n\u2550\u2550\u2550 Setup Complete \u2550\u2550\u2550\n")
    print("Next steps:")
    print("  1. Run: operator help")
    print("  2. Review ~/.copilot/copilot-instructions.md and customize")
    print("  3. Start a session: operator --agent=anvil:anvil --yolo")
    print("  4. Start an autonomous loop: operator --loop --name myproject")
    return 0


if __name__ == "__main__":
    sys.exit(main())
