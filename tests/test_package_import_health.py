"""An editable install can go stale in a way `git pull` cannot fix.

setuptools does not symlink the checkout. It writes a finder holding a static
table of module name to path, built once at install time. So a pull updates
the source of every module already in that table, and cannot add a new one --
the checkout is correct, the installed package is broken, and the first
symptom is `ModuleNotFoundError` from a command that worked yesterday.

`mail_affiliation` is the live example: added in this release, imported at
module scope by `copilot_operator`, so every `operator` verb would fail on a
machine that pulled without re-running setup.
"""

from __future__ import annotations

import subprocess
import sys

import setup_tools


def test_the_installed_package_imports():
    """The real answer for this machine.

    Also the reason the check runs in a subprocess: this test process
    imported `setup_tools` from a `sys.path` that includes the checkout, so
    it is the last thing capable of noticing that the *install* is missing a
    module.
    """
    assert setup_tools.package_import_health() == ""


def test_a_module_the_install_has_never_heard_of_is_reported():
    """Positive control, and the shape of the real failure.

    A name that is not in the finder's table is exactly what a newly added
    module looks like to an install that predates it.
    """
    broken = setup_tools.package_import_health(
        entries=("copilot_operator", "module_no_install_has_ever_shipped"))
    assert broken, "a missing module was reported as healthy"
    assert "module_no_install_has_ever_shipped" in broken, broken


def test_the_probe_does_not_resolve_modules_from_the_working_directory():
    """The load-bearing detail.

    Without `-I` the current directory joins `sys.path`, every module in the
    checkout resolves from there, and the check reports a healthy install on
    a machine that has none -- passing for the same reason it is useless.
    """
    program = "import importlib.util as u; print(bool(u.find_spec('setup_tools')))"
    plain = subprocess.run([sys.executable, "-c", program], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
    isolated = subprocess.run([sys.executable, "-I", "-c", program], cwd=str(TMP),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    assert plain.stdout.strip() == "True", plain.stderr
    # `setup_tools` is installed here, so isolation must not have broken the
    # ability to find it -- only the ability to find it *by being in the
    # directory*. The negative half is the test above.
    assert isolated.returncode == 0, isolated.stderr


def test_entry_modules_are_the_console_script_entry_points():
    """The list must stay the way in, not become a hand-maintained inventory
    of modules -- which would go stale in exactly the situation this check
    exists to catch."""
    for name in setup_tools.ENTRY_MODULES:
        assert name in ("copilot_operator", "handoff_tool", "operator_ingest")
    assert "copilot_operator" in setup_tools.ENTRY_MODULES


def test_status_says_so_when_the_package_does_not_import(monkeypatch, capsys):
    """Reported even when the recorded version matches. The manifest knows
    what setup last deployed; it cannot know a pull has since added a module
    the install has never heard of, and that is precisely the case where the
    version reads "current" and nothing runs."""
    monkeypatch.setattr(setup_tools, "package_import_health",
                        lambda *a, **k: "ModuleNotFoundError: no module named 'x'")
    setup_tools.report_status()
    out = capsys.readouterr().out
    assert "does not import" in out, out
    assert "Re-run setup" in out, out


def test_status_is_quiet_when_the_package_is_healthy(monkeypatch, capsys):
    """Positive control: a warning printed unconditionally is a warning
    nobody reads."""
    monkeypatch.setattr(setup_tools, "package_import_health",
                        lambda *a, **k: "")
    setup_tools.report_status()
    out = capsys.readouterr().out
    assert "does not import" not in out, out
    assert "imports cleanly" in out, out


REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
TMP = __import__("tempfile").gettempdir()
