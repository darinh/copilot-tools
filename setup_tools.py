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
import time
import urllib.error
import urllib.request
from pathlib import Path

from operator_console import enable_utf8_output
from copilot_tools_version import __version__ as TOOLKIT_VERSION
import install_manifest

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
def _clean_env() -> dict[str, str]:
    """Environment for spawned shells, with inherited poison removed.

    A parent process (pwsh 7, a venv launcher, an IDE terminal) may export a
    PSModulePath that doesn't include Windows PowerShell's own module
    directory. A powershell.exe child then inherits it and fails to load even
    built-in modules -- "the module could not be loaded" from
    Microsoft.PowerShell.Security is the usual symptom. Dropping the variable
    lets each shell rebuild its own default.
    """
    env = dict(os.environ)
    env.pop("PSModulePath", None)
    return env


def run(cmd: list[str], *, quiet: bool = False) -> bool:
    """Run a command, echoing it first. True when it exits 0."""
    if not quiet:
        print(f"     $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=quiet, text=True,
                              env=_clean_env())
    except OSError as exc:
        warn(f"could not run {cmd[0]}: {exc}")
        return False
    return proc.returncode == 0


def which(name: str) -> str | None:
    """shutil.which, but sees PATH changes made by installers mid-run."""
    return shutil.which(name)


def capture(cmd: list[str], timeout: float | None = None) -> tuple[bool, str]:
    """Run a command for its output. Returns (succeeded, stdout).

    ``timeout`` guards the version probes: a wedged tool should cost setup a
    few seconds, not hang it.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=_clean_env(), timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return proc.returncode == 0, proc.stdout or ""


def powershell() -> str | None:
    """A PowerShell that can actually start. pwsh is preferred: it is the
    less restricted of the two on locked-down machines, where invoking
    powershell.exe can fail outright with 'Access is denied'."""
    for name in ("pwsh", "powershell"):
        exe = which(name)
        if not exe:
            continue
        ok, _ = capture([exe, "-NoProfile", "-Command", "exit 0"])
        if ok:
            return exe
    return None


# ── PATH maintenance ────────────────────────────────────────────
_WIN_USER_ENV = r"Environment"
_WIN_MACHINE_ENV = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


def _prepend_process_path(directory: Path) -> None:
    entry = str(directory)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if entry.lower() not in {p.lower() for p in parts}:
        os.environ["PATH"] = os.pathsep.join([entry] + parts)


def _read_registry_path(root, subkey: str) -> tuple[str, int]:
    """Return the raw (unexpanded) Path value and its registry type."""
    import winreg

    try:
        with winreg.OpenKey(root, subkey) as key:
            value, kind = winreg.QueryValueEx(key, "Path")
            return value or "", kind
    except OSError:
        return "", winreg.REG_EXPAND_SZ


def refresh_path() -> None:
    """Re-read PATH from the registry (Windows) so freshly installed tools
    are visible without restarting the shell.

    Windows installers write PATH to the registry and broadcast a settings
    change that already-running processes never see, so a tool installed
    moments ago is invisible to ``shutil.which`` until this runs.

    Read straight from the registry rather than by shelling out: on locked
    down machines ``powershell.exe`` can be blocked outright, and a PATH
    refresh that silently does nothing is worse than none at all -- it makes
    already-installed tools look missing and triggers pointless reinstalls.
    """
    if not IS_WINDOWS:
        return
    import winreg

    merged: list[str] = []
    seen: set[str] = set()
    current = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    registry = []
    for root, subkey in ((winreg.HKEY_LOCAL_MACHINE, _WIN_MACHINE_ENV),
                         (winreg.HKEY_CURRENT_USER, _WIN_USER_ENV)):
        raw, _ = _read_registry_path(root, subkey)
        registry.extend(os.path.expandvars(p) for p in raw.split(os.pathsep) if p)

    for entry in current + registry:
        key = entry.rstrip("\\").lower()
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    os.environ["PATH"] = os.pathsep.join(merged)


def _broadcast_environment_change() -> None:
    """Tell running shells the environment changed. Best effort."""
    try:
        import ctypes

        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0,
            ctypes.c_wchar_p("Environment"), SMTO_ABORTIFHUNG, 2000, None)
    except Exception:
        pass


def persist_user_path(directory: Path) -> None:
    """Add a directory to PATH for this process and for future shells."""
    _prepend_process_path(directory)
    if IS_WINDOWS:
        import winreg

        entry = str(directory)
        raw, kind = _read_registry_path(winreg.HKEY_CURRENT_USER, _WIN_USER_ENV)
        existing = [p for p in raw.split(os.pathsep) if p]
        expanded = {os.path.expandvars(p).rstrip("\\").lower() for p in existing}
        if entry.rstrip("\\").lower() in expanded:
            return
        new_value = os.pathsep.join([entry] + existing) if existing else entry
        # setx is deliberately avoided: it truncates PATH at 1024 characters.
        # An unexpanded %VAR% anywhere in the value must stay REG_EXPAND_SZ or
        # every such entry silently stops resolving.
        if "%" in new_value:
            kind = winreg.REG_EXPAND_SZ
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_USER_ENV, 0,
                                winreg.KEY_READ | winreg.KEY_WRITE) as key:
                winreg.SetValueEx(key, "Path", 0, kind, new_value)
        except OSError as exc:
            warn(f"Could not add {directory} to your user PATH: {exc}")
            return
        _broadcast_environment_change()
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
        existing_text = profile.read_text(encoding="utf-8") if profile.is_file() else ""
        if str(directory) not in existing_text:
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
    # Debian and its derivatives ship python3 without pip; everywhere else it
    # comes with the interpreter, so there is nothing to install.
    "pip":    {"brew": None, "apt-get": "python3-pip", "dnf": "python3-pip",
               "pacman": "python-pip", "zypper": "python3-pip",
               "apk": "py3-pip", "winget": None},
    "venv":   {"brew": None, "apt-get": "python3-venv", "dnf": None,
               "pacman": None, "zypper": None, "apk": None, "winget": None},
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


def _sudo_usable(sudo: str) -> bool:
    """True when sudo will not block on a password prompt.

    Without this a non-interactive run (CI, a backgrounded operator loop)
    stalls for minutes on sudo's password retries before failing anyway.
    """
    if run([sudo, "-n", "true"], quiet=True):
        return True
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


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
        if sudo and not _sudo_usable(sudo[0]):
            warn(f"Installing '{package}' needs root, and sudo would block "
                 "waiting for a password here. Skipping.")
            return False
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


def _python_scripts_dir() -> Path:
    """Where console scripts from this interpreter's pip land."""
    import sysconfig

    for key in ("scripts", "purelib"):
        path = sysconfig.get_path(key)
        if key == "scripts" and path:
            return Path(path)
    return Path(sys.executable).parent


def _user_scripts_dir() -> Path:
    """Where console scripts from a `pip install --user` land."""
    import sysconfig

    try:
        path = sysconfig.get_path("scripts", f"{os.name}_user")
    except KeyError:
        path = None
    return Path(path) if path else LOCAL_BIN


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def have_pip() -> bool:
    return run([sys.executable, "-m", "pip", "--version"], quiet=True)


def ensure_pip() -> bool:
    """Make `python -m pip` work for the interpreter running this script.

    Debian and Ubuntu ship python3 with pip removed, so the toolkit cannot
    install itself out of the box there. Telling the user to go and install
    pip is not setup's job — setup's job is to put it there.
    """
    if have_pip():
        return True

    print("  Installing pip...")

    # 1. stdlib bootstrap: no network, no package manager, no privileges.
    run([sys.executable, "-m", "ensurepip", "--upgrade"], quiet=True)
    if have_pip():
        info("pip installed (ensurepip)")
        return True

    # 2. the distro's own package. Debian also splits out ensurepip's
    #    payload, which is what makes venv creation fail later.
    if not IS_WINDOWS and not IS_MACOS:
        install_system_package("pip")
        install_system_package("venv")
        if have_pip():
            info("pip installed (system package)")
            return True

    # 3. PyPA's bootstrap script, fetched to a file and run by path.
    if _install_pip_from_bootstrap():
        info("pip installed (get-pip.py)")
        return True

    err("Could not install pip for this interpreter automatically:\n"
        f"       {sys.executable}\n"
        "       Install your distribution's python3-pip package and re-run.")
    return False


def _install_pip_from_bootstrap() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "get-pip.py"
        try:
            with urllib.request.urlopen("https://bootstrap.pypa.io/get-pip.py",
                                        timeout=120) as resp:
                script.write_bytes(resp.read())
        except (OSError, urllib.error.URLError) as exc:
            warn(f"Could not download get-pip.py: {exc}")
            return False
        for extra in ([], ["--user"], ["--user", "--break-system-packages"]):
            if in_virtualenv() and extra:
                break
            run([sys.executable, str(script)] + extra)
            if have_pip():
                return True
    return False


def pip_install(args: list[str]) -> bool:
    """`pip install`, coping with interpreters that refuse to be modified.

    Distro Pythons are marked externally managed (PEP 668), so a plain
    install aborts with a wall of text telling the user to use a venv.
    Retrying with --user writes to the user's own site-packages, which is
    precisely what that marker exists to steer people towards, so it is
    tried before the blunt --break-system-packages.
    """
    if not ensure_pip():
        return False

    attempts: list[list[str]] = [[]]
    if not in_virtualenv():
        attempts += [["--user"], ["--user", "--break-system-packages"]]

    output = ""
    for extra in attempts:
        cmd = [sys.executable, "-m", "pip", "install"] + extra + args
        print(f"     $ {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  env=_clean_env())
        except OSError as exc:
            warn(f"could not run pip: {exc}")
            return False
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            if "--user" in extra:
                persist_user_path(_user_scripts_dir())
                _prepend_process_path(_user_scripts_dir())
            return True
        if "externally-managed-environment" not in output:
            break

    print(output.strip())
    return False


def ensure_uv() -> str | None:
    """Install Astral's uv, used to install the spec-kit CLI.

    Three independent routes, because each fails on some machines: a package
    manager, PyPI (this interpreter already has pip, which is how the toolkit
    itself was installed), and finally Astral's own installer script.
    """
    if which("uv"):
        info(f"uv found: {which('uv')}")
        return which("uv")

    print("  Installing uv...")

    if IS_WINDOWS and which("winget"):
        run([which("winget"), "install", "--id", "astral-sh.uv", "--exact",
             "--silent", "--accept-package-agreements",
             "--accept-source-agreements", "--disable-interactivity"])
        refresh_path()
    elif IS_MACOS and which("brew"):
        run([which("brew"), "install", "uv"])

    if not which("uv"):
        # uv publishes wheels to PyPI, so this needs no shell, no execution
        # policy, and no network scripts piped into an interpreter.
        pip_install(["--upgrade", "uv"])
        scripts = _python_scripts_dir()
        if scripts.is_dir():
            _prepend_process_path(scripts)
            if which("uv"):
                persist_user_path(scripts)

    if not which("uv"):
        _install_uv_from_astral_script()

    # uv installs itself into ~/.local/bin on every platform.
    if LOCAL_BIN.is_dir():
        persist_user_path(LOCAL_BIN)
    refresh_path()
    if which("uv"):
        info(f"uv installed: {which('uv')}")
        return which("uv")
    warn("Could not install uv automatically; skipping spec-kit. "
         "Install it manually from https://docs.astral.sh/uv/")
    return None


def _install_uv_from_astral_script() -> None:
    """Last resort: Astral's official installer, downloaded to a file first.

    The documented one-liners pipe the script straight into a shell. That is
    exactly what breaks on machines whose PowerShell cannot load its own
    modules, and a truncated download would be executed as a half-written
    script, so the script is always fetched to disk and run by path.
    """
    with tempfile.TemporaryDirectory() as tmp:
        if IS_WINDOWS:
            ps = powershell()
            if not ps:
                return
            script = Path(tmp) / "uv-install.ps1"
            try:
                with urllib.request.urlopen("https://astral.sh/uv/install.ps1",
                                            timeout=120) as resp:
                    script.write_bytes(resp.read())
            except (OSError, urllib.error.URLError) as exc:
                warn(f"Could not download the uv installer: {exc}")
                return
            run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(script)])
            return

        curl, sh = which("curl"), which("sh")
        if not (curl and sh):
            return
        script = Path(tmp) / "uv-install.sh"
        if run([curl, "-LsSf", "https://astral.sh/uv/install.sh",
                "-o", str(script)]):
            run([sh, str(script)])


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
    if not pip_install(["-e", str(REPO_ROOT)]):
        err("pip install failed. Setup cannot continue — the console scripts "
            "would be missing and the toolkit would be unusable.")
        return False
    info("Package installed")

    if not shutil.which("operator"):
        for candidate in (_python_scripts_dir(), _user_scripts_dir()):
            if (candidate / ("operator.exe" if IS_WINDOWS else "operator")).exists():
                _prepend_process_path(candidate)
                persist_user_path(candidate)
                break
    if not shutil.which("operator"):
        scripts_dir = _python_scripts_dir()
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


def _present(path: Path) -> bool:
    """True when anything occupies ``path``, including something unreadable.

    The tri-state answer comes from :func:`install_manifest.path_present`; this
    folds "cannot tell" into *present* because every caller here is asking
    whether the way is clear to create or rename something onto that name. The
    cost of being wrong is asymmetric: treating an occupied path as free
    overwrites whatever was there, while treating a free path as occupied costs
    an ``os.replace`` that fails loudly and changes nothing.

    ``lstat``-based, so a link is seen without being followed — a link with a
    deleted target still occupies the name.
    """
    return install_manifest.path_present(path) is not False


def _link_directory(src: Path, dest: Path, assume_yes: bool = False,
                    may_replace: bool = False) -> str:
    """Link src -> dest, preferring a link and falling back to a copy.

    Anything already at ``dest`` may be the user's: a real directory can hold
    edits made in place, and a link can point at a working copy they maintain
    somewhere else. Neither is removed without consent — unless ``may_replace``
    says the manifest has already proved the contents are the ones setup wrote.

    A link is only self-evidently ours when it already resolves to ``src``; a
    link anywhere else carries exactly as much of the user's intent as a
    directory full of edits, and destroying it silently loses the one piece of
    information it holds — where they pointed it.
    """
    if _is_link(dest):
        try:
            if Path(os.path.realpath(dest)) == src.resolve():
                return "already linked"
        except OSError:
            pass
        if not may_replace and not ask(
                f"{dest} is a link to {_link_target(dest) or 'somewhere else'} "
                "rather than the repository copy. Replace it?", assume_yes):
            return "skipped (kept existing)"
        _remove_dest(dest)
    elif _present(dest):
        if dest.is_dir() and _dirs_match(src, dest):
            return "already up to date"
        if not may_replace and not ask(
                f"{dest} exists and differs from the repository copy. Replace it?",
                assume_yes):
            return "skipped (kept existing)"
        _remove_dest(dest)
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


def _link_target(path: Path) -> str | None:
    """Where ``path`` points, or None when it is not a link at all.

    ``os.readlink`` is the one call that answers this for both kinds of
    Windows reparse point and for POSIX symlinks, so every link question in
    this module is asked through here rather than re-derived.
    """
    try:
        return os.readlink(str(path))
    except OSError:
        return None


def _is_junction(path: Path) -> bool:
    return _link_target(path) is not None


def _is_link(path: Path) -> bool:
    """True when ``path`` is a symlink or a Windows junction.

    ``Path.is_symlink`` returns False for a junction (verified on Windows), so
    the junction probe is not redundant. Neither test requires the link to
    resolve, which is deliberate: a link whose target has been deleted is still
    a link, and treating it as absent leaves it to be discovered by whatever
    tries to write over it.

    A path that cannot be examined answers False, and the answer is a guess —
    ``Path.is_symlink`` re-raises a permission denial on every interpreter this
    project supports (verified on 3.11 and 3.12), so without the ``except``
    one unreadable artifact aborts the whole setup run. False is the harmless
    guess here because every caller uses True only to take *more* care; a
    caller that must not guess about a path asks
    :func:`install_manifest.path_present`, which keeps "cannot tell" as its own
    answer.
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    return IS_WINDOWS and _is_junction(path)


def _remove_dest(path: Path) -> None:
    """Remove whatever is at ``path``, never following a link out of it.

    ``shutil.rmtree`` is a silent no-op on a junction and on a symlink to a
    directory — it removes nothing and, with ``ignore_errors``, says nothing —
    so using it to clear a link leaves the link in place and makes the caller's
    replacement fail later for no visible reason. ``unlink`` removes the link
    itself and leaves the target alone, which is what is wanted in every case
    here.

    Failures raise rather than being swallowed. ``ignore_errors=True`` turns a
    single locked file on Windows into a half-deleted directory that still
    reports success, and the copy that follows then fails on a destination
    which was supposed to be gone — destroying the user's data and aborting
    the run for reasons neither of them can see. A caller that cannot clear a
    destination needs to be told while it can still leave the original alone.
    """
    if _is_link(path):
        try:
            path.unlink()
        except OSError:
            # Some link kinds are directories to the API that refuses unlink.
            os.rmdir(path)
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _discard(path: Path) -> None:
    """Best-effort cleanup of scratch this module created itself.

    The distinction from :func:`_remove_dest` is whose data it is: nothing here
    was ever the user's, so failing to remove it is untidy rather than
    dangerous, and reporting it would bury the real error that led here.
    """
    try:
        _remove_dest(path)
    except OSError:
        pass


#: Windows rename failures that say "someone else has this open *right now*"
#: rather than "you may not do this": ``ERROR_ACCESS_DENIED`` and
#: ``ERROR_SHARING_VIOLATION``. A scanner opening a directory reports the first.
_RENAME_RETRY_WINERRORS = frozenset({5, 32})
_RENAME_RETRY_ATTEMPTS = 5
_RENAME_RETRY_DELAY = 0.1


def _transient_rename_error(exc: OSError) -> bool:
    """True when ``exc`` is a Windows lock that is worth waiting out.

    POSIX is excluded deliberately rather than incidentally. ``EACCES`` on a
    rename there means the permissions do not allow it, which no amount of
    waiting changes, so retrying would convert a clear refusal into a slow one.
    On Windows the same numeric error is returned for a third party holding a
    handle, which is a state that ends on its own.
    """
    if not IS_WINDOWS:
        return False
    return getattr(exc, "winerror", None) in _RENAME_RETRY_WINERRORS


def _replace_retrying(src: Path, dest: Path) -> None:
    """``os.replace`` that waits out a transient Windows lock.

    Only for renames whose source is scratch this module wrote moments ago.
    ``shutil.copytree`` finishes and the antivirus scanner that woke up to read
    the new files still has them open, so the very next rename fails with
    ``ERROR_ACCESS_DENIED`` — an error about someone else's handle, not about
    the caller's permissions or the state of the destination.

    Bounded on purpose: this narrows the "stop when something is open" rule in
    :func:`_replace_tree` to "stop when something is *still* open half a second
    later", and a lock that outlives the retries still raises the original
    error. A rename is atomic, so a failed attempt moved nothing and the next
    one starts from the same state — which is what makes retrying safe here and
    would not be true of a copy.
    """
    for remaining in range(_RENAME_RETRY_ATTEMPTS - 1, -1, -1):
        try:
            os.replace(src, dest)
            return
        except OSError as exc:
            if not remaining or not _transient_rename_error(exc):
                raise
            time.sleep(_RENAME_RETRY_DELAY)


def _replace_tree(staged: Path, dest: Path, *, expect_absent: bool = False) -> None:
    """Move ``staged`` onto ``dest``, keeping ``dest`` recoverable throughout.

    The old copy is renamed aside rather than deleted, because deletion is the
    step that cannot be taken back. ``shutil.rmtree`` walking a tree that turns
    out to hold a locked file removes everything up to it and then raises,
    which leaves the user with neither their old copy nor a new one — a fix for
    "the destination was not cleared" that destroys more than the bug it
    replaced. A rename either happens or does not, and on Windows it is also
    the operation that fails when something inside is still open, which is
    precisely when stopping is the right answer.

    That rule holds for moving the user's copy aside — a handle on *their*
    tree is exactly the signal to leave it alone, so that rename is not
    retried. It does not hold for moving scratch this module wrote seconds ago
    onto a name nothing occupies: there the only handle that can exist belongs
    to a scanner reacting to setup's own writes, the destination has already
    been vacated, and refusing means a skill silently fails to install for a
    lock that was over before the message was printed. Those two renames go
    through :func:`_replace_retrying`, which stops on anything that outlasts a
    short wait.

    ``expect_absent`` says the caller never asked the user about ``dest``,
    because at the moment it classified there was nothing there to ask about.
    Consent is scoped to the state it was given for: if something has appeared
    since — the CLI has been observed recreating directories under
    ``~/.copilot`` — replacing it silently spends an authorisation nobody
    granted, and the aside copy is discarded on success, so the thing that
    appeared would be gone. Refusing costs a reinstall; the alternative costs
    whatever was there.

    If the swap fails the old copy is renamed back. If *that* fails too, or if
    the process dies between the two renames, the only copy is left under the
    aside name — which :func:`_reconcile_scratch` restores before anything else
    looks at the destination.
    """
    previous = dest.with_name(f".{dest.name}.previous")
    _discard(previous)
    moved = False
    if _present(dest):
        if expect_absent:
            raise FileExistsError(
                f"{dest} appeared after setup found it absent; nothing was "
                "replaced")
        os.replace(dest, previous)
        moved = True
    try:
        _replace_retrying(staged, dest)
    except OSError:
        if moved:
            _replace_retrying(previous, dest)
        raise
    _discard(previous)


def _reconcile_scratch(dest: Path) -> None:
    """Clear anything a half-finished install left beside ``dest``.

    Two reasons this is not merely tidiness. A swap that failed between its
    renames — or a process killed outright, which no ``except`` clause can
    cover — leaves the user's only copy under the aside name, and putting it
    back is the difference between a skill that reappears and one that is gone
    until somebody notices. And the scratch names are directories holding a
    ``SKILL.md``, so leaving them in the skills directory invites the CLI to
    load a half-written copy as a skill in its own right.

    The staged copy is read before it is discarded, because its mere existence
    answers a question nothing else can. :func:`_replace_tree` renames it *onto*
    the destination, so a swap that finished leaves none behind. Finding one
    means the swap did not finish — and then something at ``dest`` is not the
    install this scratch belongs to, whoever put it there, which makes the
    aside copy still the user's only one. Discarding it on the strength of
    "well, something is at the destination" is how both copies are lost.
    """
    staged = dest.with_name(f".{dest.name}.installing")
    swap_unfinished = install_manifest.path_present(staged) is not False
    previous = dest.with_name(f".{dest.name}.previous")
    if install_manifest.path_present(previous) is not True:
        # Absent, or there but unexaminable — either way there is nothing here
        # that can be safely moved back.
        _discard(staged)
        return
    dest_present = install_manifest.path_present(dest)
    if dest_present is None:
        # Something may or may not be at the destination. Restoring over it
        # could destroy a finished install, and discarding the aside copy could
        # destroy the only one. Leave both and let the caller report.
        return
    if not dest_present:
        try:
            _replace_retrying(previous, dest)
        except OSError:
            # The aside copy is the only one; leaving it named oddly beats
            # discarding it because it could not be renamed back.
            return
        _discard(staged)
        return
    if swap_unfinished:
        # Both copies survive, litter and all. Tidiness is the thing being
        # traded away here, and it is the cheaper of the two.
        return
    _discard(previous)
    _discard(staged)


#: Files copied from ``templates/`` into ``~/.copilot``.
TEMPLATE_ARTIFACTS = (
    ("mcp-config.json", "mcp-config.json", "MCP config"),
    ("copilot-instructions.md", "copilot-instructions.md", "Copilot instructions"),
)


def _skill_sources() -> list[Path]:
    root = REPO_ROOT / "skills"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file())


def _extension_sources() -> list[Path]:
    root = REPO_ROOT / "extensions"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def deployed_artifacts() -> list[tuple[str, str, Path, Path]]:
    """Every ``(key, kind, source, dest)`` setup copies out of this repository.

    One definition shared by the installers and ``--status`` so a report can
    never describe a different set of files than the one setup writes.
    """
    items: list[tuple[str, str, Path, Path]] = []
    for src_name, dest_name, _label in TEMPLATE_ARTIFACTS:
        items.append((f"templates/{src_name}", "template",
                      REPO_ROOT / "templates" / src_name, COPILOT_DIR / dest_name))
    for src in _skill_sources():
        items.append((f"skills/{src.name}", "skill",
                      src, COPILOT_DIR / "skills" / src.name))
    for src in _extension_sources():
        items.append((f"extensions/{src.name}", "extension",
                      src, COPILOT_DIR / "extensions" / src.name))
    return items


def detect_tool_versions() -> dict[str, str]:
    """Versions of the external tools this toolkit drives.

    Recorded for diagnosis only — nothing branches on these. When a machine
    misbehaves, knowing which Copilot CLI and multiplexer it was set up against
    is usually the first question.
    """
    versions: dict[str, str] = {
        "python": platform.python_version(),
        "copilot-tools": TOOLKIT_VERSION,
    }
    probes = [("git", ["git", "--version"]), ("copilot", ["copilot", "--version"])]
    mux = find_multiplexer()
    if mux:
        probes.append((mux, [mux, "-V"]))
    for name, cmd in probes:
        if not which(cmd[0]):
            continue
        ok, out = capture(cmd, timeout=20)
        if ok and out.strip():
            versions[name] = out.strip().splitlines()[0].strip()
    return versions


def _warn_unreadable(label: str, dest: Path) -> None:
    """Say why an artifact was left alone, in one wording everywhere.

    Silence here would be the worst outcome of the conservative choice: a
    machine that quietly stops receiving updates and never says so.
    """
    warn(f"{label} at {dest} exists but could not be examined "
         "(permission, a lock, or an unreachable mount) — not touching it")


def _resolve_overwrite(state: str, label: str, dest: Path, assume_yes: bool) -> bool:
    """Decide whether to write over an existing artifact.

    The manifest is what makes this more than a byte comparison: ``STALE``
    means the bytes on disk are the bytes setup wrote, so the user has nothing
    invested in them and the update is not a question worth asking.

    ``UNREADABLE`` is refused before ``assume_yes`` is consulted. ``--yes``
    answers questions about known contents; it is not consent to overwrite
    something nobody could look at.
    """
    if state == install_manifest.UNREADABLE:
        _warn_unreadable(label, dest)
        return False
    if state == install_manifest.STALE:
        info(f"{label}: updating (your copy was unmodified)")
        return True
    if state == install_manifest.MODIFIED:
        return ask(f"{label} at {dest} has local edits. Overwrite them?", assume_yes)
    if state == install_manifest.UNTRACKED:
        return ask(f"{label} at {dest} differs and was not installed by setup. "
                   "Overwrite?", assume_yes)
    return True


def install_extensions(assume_yes: bool = False, manifest: dict | None = None) -> None:
    print("\nInstalling runtime extensions...")
    sources = _extension_sources()
    if not sources:
        warn("No extensions/ directory found — skipping")
        return
    manifest = install_manifest.empty_manifest() if manifest is None else manifest
    dest_root = COPILOT_DIR / "extensions"
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in sources:
        dest = dest_root / src.name
        key = f"extensions/{src.name}"
        state = install_manifest.classify(manifest, key, dest,
                                          install_manifest.tree_digest(src))
        if state == install_manifest.UNREADABLE:
            # _link_directory reads an unexaminable destination as occupied and
            # goes on to ask `dest.is_dir()`, which raises rather than answers
            # on a permission denial — aborting the whole run over one
            # extension. Nothing here is worth finding out that way.
            _warn_unreadable(f"extension '{src.name}'", dest)
            continue
        try:
            result = _link_directory(src, dest, assume_yes,
                                     may_replace=install_manifest.may_overwrite(state))
            info(f"extension '{src.name}': {result}")
        except OSError as exc:
            warn(f"extension '{src.name}': {exc}")
            continue
        if result.startswith("skipped"):
            continue
        linked = "link" in result or "junction" in result
        install_manifest.record(
            manifest, key, dest, kind="extension", linked=linked,
            digest=None if linked else install_manifest.tree_digest(dest),
        )


def install_templates(assume_yes: bool = False, manifest: dict | None = None) -> None:
    """Copy the template files into ``~/.copilot``.

    The copy never goes *through* a link. ``shutil.copyfile`` opens the
    destination for writing, which follows a symlink and lands the repository's
    copy in the link's target — a path outside ``~/.copilot`` that the user
    chose and setup was never offered. Consent to overwrite ``dest`` is consent
    about ``dest``, so the link is removed and a real file is written in its
    place, which is what the other two installers already do by renaming the
    destination aside. Every other write here is inside the directory setup
    owns; this was the one that could reach out of it.
    """
    print("\nInstalling templates...")
    COPILOT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = install_manifest.empty_manifest() if manifest is None else manifest
    for src_name, dest_name, label in TEMPLATE_ARTIFACTS:
        src = REPO_ROOT / "templates" / src_name
        dest = COPILOT_DIR / dest_name
        key = f"templates/{src_name}"
        if not src.is_file():
            warn(f"{label}: source missing ({src})")
            continue
        source_digest = install_manifest.file_digest(src)
        state = install_manifest.classify(manifest, key, dest, source_digest)
        if state == install_manifest.CURRENT:
            info(f"{label} already up to date")
            install_manifest.record(manifest, key, dest, kind="template",
                                    digest=source_digest)
            continue
        if not _resolve_overwrite(state, label, dest, assume_yes):
            warn(f"Skipped {label} (kept existing)")
            continue
        if _is_link(dest):
            try:
                _remove_dest(dest)
            except OSError as exc:
                warn(f"{label}: not installed ({exc})")
                continue
        shutil.copyfile(src, dest)
        install_manifest.record(manifest, key, dest, kind="template",
                                digest=source_digest)
        info(f"Installed {label}")


def install_skills(assume_yes: bool = False, manifest: dict | None = None) -> None:
    """Install this repo's skills for the user, not for one project.

    Skills go to ``~/.copilot/skills/<name>/`` so they are available in every
    project on the machine. A project-level copy under ``.github/skills/``
    would only reach agents working in that one repository, and the operator
    skill is specifically about work that spans projects.

    The whole tree is copied, not just its top-level files. A skill that
    bundles ``reference/`` or ``scripts/`` is one artifact, and half of it is
    not a working skill; a partial copy would also never match the digest the
    manifest compares against, so setup would re-copy it on every run forever
    while still reporting it as installed.

    The copy is staged beside the destination and swapped in, and the copy it
    replaces is renamed aside rather than deleted, so nothing that goes wrong
    part-way through can leave the user with no skill at all.
    """
    skills = _skill_sources()
    if not skills:
        return

    print("\nInstalling skills...")
    manifest = install_manifest.empty_manifest() if manifest is None else manifest
    dest_root = COPILOT_DIR / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in skills:
        dest = dest_root / src.name
        _reconcile_scratch(dest)
        key = f"skills/{src.name}"
        label = f"skill '{src.name}'"
        source_digest = install_manifest.tree_digest(src)
        state = install_manifest.classify(manifest, key, dest, source_digest)
        if state == install_manifest.UNREADABLE:
            # Asked before the link question below, which cannot be answered
            # about a path nobody can examine and would only guess at.
            _warn_unreadable(label, dest)
            continue
        if _is_link(dest) and not (install_manifest.entry(manifest, key) or {}).get("linked"):
            # Every digest here is taken *through* the link, so it describes
            # whatever the user pointed at, not the destination — it cannot
            # show that setup wrote what is there, and STALE would otherwise
            # license deleting a link setup never created.
            state = install_manifest.UNTRACKED
        if state == install_manifest.CURRENT:
            info(f"{label} already up to date")
            install_manifest.record(manifest, key, dest, kind="skill",
                                    digest=source_digest)
            continue
        if not _resolve_overwrite(state, label, dest, assume_yes):
            warn(f"Skipped {label} (kept existing)")
            continue
        staged = dest.with_name(f".{src.name}.installing")
        try:
            _discard(staged)
            shutil.copytree(src, staged)
            _replace_tree(staged, dest,
                          expect_absent=state == install_manifest.ABSENT)
        except OSError as exc:
            warn(f"{label}: not installed ({exc})")
            _reconcile_scratch(dest)
            continue
        install_manifest.record(manifest, key, dest, kind="skill",
                                digest=install_manifest.tree_digest(dest))
        info(f"Installed {label}")


def report_status() -> int:
    """Print what is installed against what this checkout would install.

    Answers the question a second machine has after ``git pull``: is anything
    stale, and can it be updated without losing local edits?
    """
    manifest = install_manifest.load(OPERATOR_HOME)
    installed = manifest.get("package_version")
    print(f"copilot-tools {TOOLKIT_VERSION} (this checkout)")
    if installed is None:
        warn("No install manifest found — setup has not run since manifests "
             "were introduced.")
    elif install_manifest.is_older(installed, TOOLKIT_VERSION):
        warn(f"Installed version {installed} is older — run setup to update.")
    elif install_manifest.is_older(TOOLKIT_VERSION, installed):
        warn(f"Installed version {installed} is NEWER than this checkout. "
             "Pull before running setup.")
    else:
        info(f"Installed version {installed} is current")
    print(f"\nManifest: {install_manifest.manifest_path(OPERATOR_HOME)}")

    report = install_manifest.status(manifest, deployed_artifacts())
    if not report:
        return 0
    width = max(len(item.key) for item in report)
    print("\nDeployed artifacts:")
    for item in report:
        version = item.installed_version or "—"
        print(f"  {item.key.ljust(width)}  {version:>7}  "
              f"{install_manifest.describe(item.state)}")

    pending = install_manifest.pending_migrations(installed, TOOLKIT_VERSION)
    if pending:
        print("\nUpgrade steps setup would run:")
        for source, target, _func in pending:
            print(f"  {source} -> {target}")

    tools = manifest.get("tools") or {}
    if tools:
        print("\nTool versions at last setup:")
        for name in sorted(tools):
            print(f"  {name}: {tools[name]}")

    if install_manifest.needs_update(report):
        print("\nRun setup to bring these up to date.")
        return 1
    return 0


def apply_upgrades(manifest: dict, assume_yes: bool = False) -> None:
    """Run any upgrade functions between the installed and current versions.

    Skipped entirely on a machine with nothing deployed: there is no old state
    to migrate, and running every historical upgrade against an empty
    ``~/.copilot`` would be noise at best.
    """
    installed = manifest.get("package_version")
    fresh = not any(_present(dest) for _key, _kind, _src, dest in deployed_artifacts())
    pending = install_manifest.pending_migrations(installed, TOOLKIT_VERSION)
    if fresh or not pending:
        return
    print(f"\nUpgrading installed files from {installed or 'an untracked version'} "
          f"to {TOOLKIT_VERSION}...")
    ctx = install_manifest.MigrationContext(
        copilot_dir=COPILOT_DIR,
        operator_home=OPERATOR_HOME,
        repo_root=REPO_ROOT,
        manifest=manifest,
        from_version=installed,
        to_version=TOOLKIT_VERSION,
        assume_yes=assume_yes,
        log=lambda message: info(message),
    )
    install_manifest.run_migrations(ctx)


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
    parser.add_argument("--status", action="store_true",
                        help="Report installed versions and whether an update is needed")
    parser.add_argument("--no-install-prereqs", dest="install_prereqs",
                        action="store_false",
                        help="Do not install missing prerequisites automatically")
    parser.add_argument("--skip-optional", action="store_true",
                        help="Skip Anvil, spec-kit, and MCP server provisioning")
    args = parser.parse_args(argv)

    print("\n\u2550\u2550\u2550 Copilot Tools Setup \u2550\u2550\u2550\n")

    if args.status:
        return report_status()

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

    manifest = install_manifest.load(OPERATOR_HOME)
    apply_upgrades(manifest, assume_yes=args.yes)

    if not args.skip_package:
        if not install_package(assume_yes=args.yes):
            return 1

    install_extensions(assume_yes=args.yes, manifest=manifest)
    install_templates(assume_yes=args.yes, manifest=manifest)
    install_skills(assume_yes=args.yes, manifest=manifest)

    if not args.skip_optional:
        ensure_anvil()
        ensure_specify()
        ensure_roslyn_mcp(assume_yes=args.yes)

    manifest["package_version"] = TOOLKIT_VERSION
    manifest["tools"] = detect_tool_versions()
    try:
        install_manifest.save(OPERATOR_HOME, manifest)
        info(f"Recorded install manifest ({TOOLKIT_VERSION})")
    except OSError as exc:
        # The manifest is bookkeeping. Losing it costs prompts on the next run,
        # so it must never be the reason a successful setup reports failure.
        warn(f"Could not write install manifest: {exc}")

    print("\n\u2550\u2550\u2550 Setup Complete \u2550\u2550\u2550\n")
    print("Next steps:")
    print("  1. Run: operator help")
    print("  2. Review ~/.copilot/copilot-instructions.md and customize")
    print("  3. Start a session: operator --agent=anvil:anvil --yolo")
    print("  4. Start an autonomous loop: operator --loop --name myproject")
    return 0


if __name__ == "__main__":
    sys.exit(main())
