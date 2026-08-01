"""Setup can now say whether the extensions it deployed will actually run.

Two independent questions, and until now neither was asked anywhere:

1. Is the CLI in a mode that loads extensions **at all**? It only does so in
   experimental mode, and the CLI persists the last spelling it was given into
   a settings file this toolkit never writes. On the machine this was written
   for, that value sat at ``false`` while agent sessions ran for over an hour
   with no ``checkout-guard`` in the shared checkout it exists to protect. Not
   one artifact was wrong. `--status` would have reported a perfect machine.

2. Could a particular deployed extension load if it were tried? A junction to
   a directory with no ``extension.mjs``, or an ``extension.mjs`` that does not
   parse, is classified ``CURRENT`` by the install manifest, because a link has
   no independent content to compare.

The failure both questions guard against is silent by construction: an
extension that never loaded cannot report its own absence, so a session in
which nothing ran looks exactly like a session in which everything ran and
found nothing to say. Every assertion below therefore checks that a *failed or
impossible* probe stays distinguishable from a passing one, rather than merely
that the happy path returns something.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import install_manifest  # noqa: E402
import setup_tools  # noqa: E402
from conftest import denied  # noqa: E402

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

VALID = "export const x = 1;\n"
BROKEN = "this is not ((( valid javascript\n"


def _extension(root: Path, name: str, source: str | None = VALID) -> Path:
    dest = root / name
    dest.mkdir(parents=True, exist_ok=True)
    if source is not None:
        (dest / "extension.mjs").write_text(source, encoding="utf-8")
    return dest


# ── the gap being closed ─────────────────────────────────────────
def test_the_manifest_calls_a_gutted_extension_current(tmp_path):
    """The premise, asserted against code this change does not touch.

    Without this, the probe below could be testing a problem that does not
    exist. ``classify`` is asked about a *linked* extension whose directory
    holds no entrypoint at all, which is the exact shape a broken junction
    leaves behind, and it answers ``CURRENT`` -- "up to date" -- because a link
    has no content of its own to compare. That is a true statement about the
    bytes and a useless one about whether anything will run.
    """
    dest = _extension(tmp_path, "gutted", source=None)
    manifest = install_manifest.empty_manifest()
    install_manifest.record(manifest, "extensions/gutted", dest,
                            kind="extension", linked=True, digest=None)

    state = install_manifest.classify(manifest, "extensions/gutted", dest, None)

    assert state == install_manifest.CURRENT
    assert install_manifest.needs_update(
        [install_manifest.ArtifactStatus("extensions/gutted", "extension",
                                         dest, state, None)]) is False


# ── question 1: does the CLI load extensions at all ──────────────
def _settings(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(body, encoding="utf-8")
    return path


def test_experimental_true_is_the_only_enabled_answer(tmp_path):
    settings = _settings(tmp_path, json.dumps({"experimental": True}))
    assert setup_tools.extension_mode(settings).state == setup_tools.ENABLED


def test_experimental_false_is_reported_as_nothing_loading(tmp_path):
    """The state the machine was actually in, and the one worth naming."""
    settings = _settings(tmp_path, json.dumps({"experimental": False}))
    mode = setup_tools.extension_mode(settings)
    assert mode.state == setup_tools.DISABLED
    assert "--experimental" in mode.detail


@pytest.mark.parametrize("body, why", [
    ("{}", "an absent key is not a recorded false"),
    (json.dumps({"experimental": "true"}), "a string is not a boolean"),
    (json.dumps({"experimental": None}), "null is not a boolean"),
    (json.dumps(["experimental"]), "a list is not a settings object"),
    ("{not json at all", "unparsable JSON says nothing about the setting"),
    ("", "an empty file says nothing about the setting"),
])
def test_anything_but_a_recorded_boolean_is_undetermined(tmp_path, body, why):
    """Never a verdict from a failed read.

    ``copilot --help`` documents ``--experimental`` and ``--no-experimental``
    and no default, so an absent key genuinely cannot be resolved from here.
    Guessing it would produce exactly the false all-clear this module exists to
    prevent.
    """
    mode = setup_tools.extension_mode(_settings(tmp_path, body))
    assert mode.state == setup_tools.UNDETERMINED, why
    assert mode.detail, "an undetermined answer must say what stopped it"


def test_a_missing_settings_file_is_undetermined_not_disabled(tmp_path):
    mode = setup_tools.extension_mode(tmp_path / "nothing-here.json")
    assert mode.state == setup_tools.UNDETERMINED
    assert mode.state != setup_tools.DISABLED


def test_an_unreadable_settings_file_is_undetermined(tmp_path, monkeypatch):
    """A permission denial is a failed read, not a recorded answer."""
    settings = _settings(tmp_path, json.dumps({"experimental": True}))
    with denied(monkeypatch, settings):
        mode = setup_tools.extension_mode(settings)
    assert mode.state == setup_tools.UNDETERMINED
    assert str(settings) in mode.detail


def test_the_default_settings_path_follows_the_configured_home(tmp_path, monkeypatch):
    """Resolved per call, so a redirected home is never answered about the real one.

    Fixed at import, every test on this machine -- and every test on CI --
    would silently be reading the developer's own settings file, and the
    answer would change under them when another session flipped it.
    """
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", tmp_path)
    _settings(tmp_path, json.dumps({"experimental": False}))
    assert setup_tools.extension_mode().state == setup_tools.DISABLED


# ── question 2: could this extension load ────────────────────────
@node
def test_a_parsable_entrypoint_is_loadable(tmp_path):
    health = setup_tools.extension_health(_extension(tmp_path, "good"))
    assert health.state == setup_tools.LOADABLE
    assert health.broken is False
    assert health.key == "good"


@node
def test_a_directory_without_an_entrypoint_cannot_load(tmp_path):
    """The case the install manifest calls "up to date"."""
    health = setup_tools.extension_health(_extension(tmp_path, "gutted", source=None))
    assert health.state == setup_tools.NO_ENTRYPOINT
    assert health.broken is True


@node
def test_an_unparsable_entrypoint_is_reported_with_its_reason(tmp_path):
    """Assert the message, not the exit status.

    ``node`` exits 1 for a syntax error, and the CLI's own extension host also
    exits 1 when an extension is denied permission. The code is shared by
    causes that need different fixes, so the reason has to survive into the
    report.
    """
    health = setup_tools.extension_health(_extension(tmp_path, "bad", source=BROKEN))
    assert health.state == setup_tools.UNPARSABLE
    assert health.broken is True
    assert "SyntaxError" in health.detail


@node
def test_check_rejects_broken_javascript(tmp_path):
    """Negative control for the probe itself.

    Every ``LOADABLE`` above is worthless unless ``node --check`` is known to
    reject something. A node shim that always exited 0 would leave the whole
    module permanently, invisibly green.
    """
    broken = tmp_path / "broken.mjs"
    broken.write_text(BROKEN, encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(broken)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert "SyntaxError" in proc.stderr


def test_without_node_the_answer_is_unchecked_not_loadable(tmp_path, monkeypatch):
    """No node means no verdict -- in particular, not a favourable one.

    ``shutil`` is patched rather than ``setup_tools.which`` because the module
    under test is the thing being graded: a double installed on it can be
    satisfied by an implementation that never consults it.
    """
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    health = setup_tools.extension_health(_extension(tmp_path, "good"))
    assert health.state == setup_tools.UNCHECKED
    assert health.state != setup_tools.LOADABLE
    assert health.broken is False, "not being able to look is not a defect found"
    assert health.detail


def test_an_unreadable_entrypoint_is_unchecked_not_missing(tmp_path, monkeypatch):
    """EACCES on the entrypoint must not read as "there is no entrypoint"."""
    dest = _extension(tmp_path, "locked")
    with denied(monkeypatch, dest / "extension.mjs"):
        health = setup_tools.extension_health(dest)
    assert health.state == setup_tools.UNCHECKED
    assert health.state != setup_tools.NO_ENTRYPOINT
    assert health.broken is False


def test_a_node_that_cannot_be_run_is_unchecked(tmp_path, monkeypatch):
    """A failed launch is not a failed parse.

    ``capture`` was not reused inside the probe for exactly this reason: it
    reports "could not start node" and "node rejected the file" as the same
    ``(False, "")``.
    """
    dest = _extension(tmp_path, "good")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/node")

    def unlaunchable(*_a, **_k):
        raise OSError("Exec format error")

    monkeypatch.setattr(subprocess, "run", unlaunchable)
    health = setup_tools.extension_health(dest)
    assert health.state == setup_tools.UNCHECKED
    assert health.broken is False


def test_a_hung_node_is_unchecked_rather_than_hanging_setup(tmp_path, monkeypatch):
    dest = _extension(tmp_path, "good")
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/node")

    def wedged(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["node"], timeout=60)

    monkeypatch.setattr(subprocess, "run", wedged)
    assert setup_tools.extension_health(dest).state == setup_tools.UNCHECKED


def test_the_reason_line_survives_node_stderr(tmp_path):
    """The error is the fifth line of seven, between a caret and a banner."""
    stderr = ("/tmp/bad.mjs:1\n"
              "this is not ((( valid\n"
              "     ^^\n"
              "\n"
              "SyntaxError: Unexpected identifier 'is'\n"
              "    at checkSyntax (node:internal/main/check_syntax:72:5)\n"
              "\n"
              "Node.js v24.18.0\n")
    assert setup_tools._syntax_error_line(stderr) == (
        "SyntaxError: Unexpected identifier 'is'")


def test_a_reason_is_given_even_when_node_says_nothing():
    """Silence from the probe must not become silence in the report."""
    assert setup_tools._syntax_error_line("")
    assert setup_tools._syntax_error_line("something unrecognised")


# ── the report ───────────────────────────────────────────────────
def _status(key: str, state: str, dest: Path, kind: str = "extension"):
    return install_manifest.ArtifactStatus(key, kind, dest, state, None)


@node
def test_the_report_skips_what_was_never_installed(tmp_path):
    """An absent extension is one fault, and the artifact table already has it."""
    missing = tmp_path / "not-installed"
    good = _extension(tmp_path, "good")
    report = setup_tools.extension_report([
        _status("extensions/not-installed", install_manifest.ABSENT, missing),
        _status("extensions/good", install_manifest.CURRENT, good),
        _status("skills/demo", install_manifest.CURRENT, tmp_path, kind="skill"),
    ])
    assert [item.key for item in report] == ["extensions/good"]


@node
def test_the_report_probes_an_extension_the_manifest_calls_current(tmp_path):
    """End to end over the gap: manifest says CURRENT, the probe says broken."""
    gutted = _extension(tmp_path, "gutted", source=None)
    report = setup_tools.extension_report(
        [_status("extensions/gutted", install_manifest.CURRENT, gutted)])
    assert [item.state for item in report] == [setup_tools.NO_ENTRYPOINT]


@pytest.fixture()
def deployed(tmp_path, monkeypatch):
    """A whole miniature install: templates, a skill, and one extension.

    Everything is deployed and current, so any non-zero exit code below comes
    from the extension questions and not from something else being stale.
    """
    repo = tmp_path / "repo"
    (repo / "templates").mkdir(parents=True)
    (repo / "templates" / "copilot-instructions.md").write_text("v1", encoding="utf-8")
    (repo / "templates" / "mcp-config.json").write_text("{}", encoding="utf-8")
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "skills" / "demo" / "SKILL.md").write_text("v1", encoding="utf-8")
    _extension(repo / "extensions", "demo")
    home = tmp_path / "copilot"
    operator_home = tmp_path / "operator"
    monkeypatch.setattr(setup_tools, "REPO_ROOT", repo)
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", home)
    monkeypatch.setattr(setup_tools, "OPERATOR_HOME", operator_home)
    manifest = install_manifest.empty_manifest()
    setup_tools.install_templates(assume_yes=True, manifest=manifest)
    setup_tools.install_skills(assume_yes=True, manifest=manifest)
    setup_tools.install_extensions(assume_yes=True, manifest=manifest)
    manifest["package_version"] = setup_tools.TOOLKIT_VERSION
    install_manifest.save(operator_home, manifest)
    (home / "settings.json").write_text(json.dumps({"experimental": True}),
                                        encoding="utf-8")
    return repo, home, operator_home


@node
def test_status_is_clean_when_extensions_load(deployed, capsys):
    code = setup_tools.report_status()
    out = capsys.readouterr().out
    assert "experimental mode is on" in out
    assert code == 0


@node
def test_status_refuses_to_call_an_inert_machine_healthy(deployed, capsys):
    """The headline. Every artifact current, and nothing will run.

    This is the exact state this repository was in while three agents worked in
    a shared checkout with no guard, and the old report described it as a
    machine with nothing to do.
    """
    _repo, home, _operator = deployed
    (home / "settings.json").write_text(json.dumps({"experimental": False}),
                                        encoding="utf-8")

    code = setup_tools.report_status()
    out = capsys.readouterr().out

    assert "experimental mode is OFF" in out
    assert "inert" in out
    assert code == 1


@node
def test_status_does_not_call_an_undetermined_machine_broken(deployed, capsys):
    """No settings file at all: say so, but do not report a defect found."""
    _repo, home, _operator = deployed
    (home / "settings.json").unlink()

    code = setup_tools.report_status()
    out = capsys.readouterr().out

    assert "could not tell" in out
    assert code == 0


@node
def test_status_names_an_extension_that_cannot_load(deployed, capsys):
    _repo, home, _operator = deployed
    (home / "extensions" / "demo" / "extension.mjs").write_text(
        BROKEN, encoding="utf-8")

    code = setup_tools.report_status()
    out = capsys.readouterr().out

    assert "does not parse" in out
    assert "cannot announce its own absence" in out
    assert code == 1


@node
def test_installing_into_a_disabled_cli_says_so(tmp_path, monkeypatch, capsys):
    """Seven extensions installed into a CLI that loads none of them is a
    successful-looking run with no effect at all. Setup is the last place in
    the sequence that can mention it -- the CLI will not."""
    repo = tmp_path / "repo"
    _extension(repo / "extensions", "demo")
    home = tmp_path / "copilot"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"experimental": False}),
                                        encoding="utf-8")
    monkeypatch.setattr(setup_tools, "REPO_ROOT", repo)
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", home)

    setup_tools.install_extensions(assume_yes=True,
                                   manifest=install_manifest.empty_manifest())

    out = capsys.readouterr().out
    assert "experimental mode is OFF" in out
    assert "none of it will load" in out


@node
def test_installing_a_broken_extension_is_not_reported_as_success(
        tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    _extension(repo / "extensions", "demo", source=BROKEN)
    home = tmp_path / "copilot"
    monkeypatch.setattr(setup_tools, "REPO_ROOT", repo)
    monkeypatch.setattr(setup_tools, "COPILOT_DIR", home)

    setup_tools.install_extensions(assume_yes=True,
                                   manifest=install_manifest.empty_manifest())

    assert "does not parse" in capsys.readouterr().out


# ── wording ──────────────────────────────────────────────────────
@pytest.mark.parametrize("state", [setup_tools.ENABLED, setup_tools.DISABLED,
                                   setup_tools.UNDETERMINED])
def test_every_mode_has_prose(state):
    """A raw state name reaching the user is a report nobody can act on."""
    described = setup_tools.describe_mode(state)
    assert described != state
    assert " " in described


@pytest.mark.parametrize("state", [setup_tools.LOADABLE, setup_tools.NO_ENTRYPOINT,
                                   setup_tools.UNPARSABLE, setup_tools.UNCHECKED])
def test_every_health_state_has_prose(state):
    described = setup_tools.describe_health(state)
    assert described != state
    assert described


def test_the_two_vocabularies_do_not_overlap():
    """Mode and health answer different questions and must not be confusable.

    A shared token would let a reader carry a conclusion about one across to
    the other -- "unchecked" mode, "disabled" extension -- and both mistakes
    resolve toward believing more than was measured.
    """
    modes = {setup_tools.ENABLED, setup_tools.DISABLED, setup_tools.UNDETERMINED}
    healths = {setup_tools.LOADABLE, setup_tools.NO_ENTRYPOINT,
               setup_tools.UNPARSABLE, setup_tools.UNCHECKED}
    assert modes.isdisjoint(healths)
