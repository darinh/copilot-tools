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
    (json.dumps({"experimental": "true"}), "a string is not a boolean"),
    (json.dumps({"experimental": None}), "null is not a boolean"),
    (json.dumps(["experimental"]), "a list is not a settings object"),
    ("{not json at all", "unparsable JSON says nothing about the setting"),
    ("", "an empty file says nothing about the setting"),
])
def test_anything_but_a_recorded_boolean_is_undetermined(tmp_path, body, why):
    """Never a verdict from a failed read.

    Note what is *not* in this list any more: an absent key. That case moved
    to DISABLED once it was measured, and the distinction the list now draws
    is the one that survives: a file that cannot be read leaves the setting
    unknown, while a file that reads cleanly and records nothing leaves it
    unset -- and unset was measured to load nothing. Not knowing what the file
    says is a different thing from knowing it says nothing.

    Guessing at the first of those would produce exactly the false all-clear
    this module exists to prevent.
    """
    mode = setup_tools.extension_mode(_settings(tmp_path, body))
    assert mode.state == setup_tools.UNDETERMINED, why
    assert mode.detail, "an undetermined answer must say what stopped it"


@pytest.mark.parametrize("body, why", [
    ("{}", "a settings file that records nothing leaves the setting unset"),
    (json.dumps({"model": "gpt-5", "showReasoning": True}),
     "other keys present and this one absent is the common real case"),
])
def test_an_absent_key_is_off_because_that_was_measured(tmp_path, body, why):
    """The steady state of a fresh machine, and the reason this changed.

    Measured on CLI 1.0.77: with no ``experimental`` key recorded, a probe
    extension's module body never evaluated, while the same seeded settings
    plus ``--experimental`` loaded it. The negative has a matched positive
    differing only in the flag, which is what rules out "the harness broke the
    loader" as an equally good explanation.

    The finding that forced the code change was not the yes/no but that the
    CLI **writes nothing** when given no flag. An unset key is therefore not a
    startup transient that resolves on first run -- it persists until someone
    passes a spelling explicitly. Reporting the most common inert
    configuration as "could not tell" meant this report was at its most
    equivocal exactly where it was most needed.
    """
    mode = setup_tools.extension_mode(_settings(tmp_path, body))
    assert mode.state == setup_tools.DISABLED, why
    # The remedy has to be in the detail: a verdict of OFF with no way out is
    # a complaint, not a report.
    assert "--experimental" in mode.detail
    # Provenance, asserted rather than trusted to survive editing. The claim
    # is measured, not documented, and a future reader re-measuring after a
    # CLI update needs to know which version it was measured against.
    assert "measured" in mode.detail


def test_a_missing_settings_file_is_off_not_undetermined(tmp_path):
    """No file at all is the same unset setting as a file with no key.

    This was UNDETERMINED until the absent case was measured, on the reasoning
    that a fresh machine should not be failed for a file the CLI has not
    written yet. The measurement inverted it: a fresh machine genuinely loads
    no extensions, so "could not tell" was not caution, it was the true answer
    withheld.
    """
    mode = setup_tools.extension_mode(tmp_path / "nothing-here.json")
    assert mode.state == setup_tools.DISABLED
    # The reason is asserted, not just the state. Deleting the absence branch
    # entirely leaves `read_text` raising FileNotFoundError into the same
    # `except OSError` and returning UNDETERMINED -- so this assertion is what
    # distinguishes "reported absent" from "fell through to the read failure",
    # which are different states with the same name for the same file.
    assert "does not exist" in mode.detail
    assert "--experimental" in mode.detail


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


@node
def test_check_does_not_read_the_files_the_entrypoint_imports(tmp_path):
    """The premise of the whole multi-file probe, asserted against node.

    If this ever fails — if some future node parses imports — checking every
    module becomes redundant rather than load-bearing, and whoever sees this
    break should know that before deciding what to delete.
    """
    dest = _extension(tmp_path, "two-file",
                      "import { x } from './guard.mjs';\nexport const y = x;\n")
    (dest / "guard.mjs").write_text(BROKEN, encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(dest / "extension.mjs")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        "node --check now follows imports; the per-module loop can be revisited")


@node
def test_a_broken_imported_module_is_unparsable(tmp_path):
    """checkout-guard's decisions all live in the file it imports.

    ``extension.mjs`` is hook wiring around ``import "./guard.mjs"``. Checking
    only the entrypoint calls this extension healthy while the half that does
    the work cannot parse — the outage the module was written to catch, moved
    one file to the left.
    """
    dest = _extension(tmp_path, "guarded",
                      "import { x } from './guard.mjs';\nexport const y = x;\n")
    (dest / "guard.mjs").write_text(BROKEN, encoding="utf-8")

    health = setup_tools.extension_health(dest)

    assert health.state == setup_tools.UNPARSABLE
    assert health.broken
    assert "guard.mjs" in health.detail, \
        "the reader needs the file named; it is not the one they assume"


@node
def test_a_sound_multi_file_extension_is_loadable(tmp_path):
    """The positive half: checking every module must not condemn a healthy one."""
    dest = _extension(tmp_path, "sound",
                      "import { x } from './guard.mjs';\nexport const y = x;\n")
    (dest / "guard.mjs").write_text("export const x = 1;\n", encoding="utf-8")
    (dest / "nested").mkdir()
    (dest / "nested" / "helper.mjs").write_text("export const z = 2;\n",
                                                encoding="utf-8")

    assert setup_tools.extension_health(dest).state == setup_tools.LOADABLE


@node
def test_vendored_dependencies_are_not_ours_to_condemn(tmp_path):
    """One unparsable file inside node_modules must not fail the extension.

    Those files are not written here, are resolved by the CLI rather than by
    us, and may never be imported at all. Failing on them would make the
    check fire on healthy machines, which is the fastest way to teach someone
    to ignore it.
    """
    dest = _extension(tmp_path, "vendored")
    junk = dest / "node_modules" / "left-pad"
    junk.mkdir(parents=True)
    (junk / "index.mjs").write_text(BROKEN, encoding="utf-8")

    assert setup_tools.extension_health(dest).state == setup_tools.LOADABLE


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
    assert setup_tools._syntax_error_line("") == "node --check gave no reason"
    assert setup_tools._syntax_error_line("something unrecognised") \
        == "something unrecognised"


def test_the_error_line_wins_over_a_source_line_that_imitates_one():
    """node echoes the offending source before diagnosing it.

    A file with a line that *begins* ``SyntaxError:`` — a template literal, a
    thrown message, a fixture like the ones in this very file — is reproduced
    in the excerpt above node's own verdict. Taking the first match would
    quote the file back at the reader as though it were the diagnosis.
    """
    stderr = ("/tmp/x.mjs:3\n"
              "SyntaxError: not the real one`);\n"
              "^\n\n"
              "SyntaxError: Unexpected end of input\n"
              "    at checkSyntax (node:internal/main/check_syntax:69:3)\n\n"
              "Node.js v24.18.0\n")
    assert setup_tools._syntax_error_line(stderr) \
        == "SyntaxError: Unexpected end of input"


def test_deeply_nested_settings_are_undetermined_and_do_not_crash(tmp_path):
    """`json.loads` raises RecursionError, which is not a ValueError.

    It is the one malformed input that escapes the read as an exception
    instead of arriving as a state, which would take the whole status command
    down with it — a failed read that does not merely lie but refuses to
    return at all.
    """
    settings = _settings(
        tmp_path, '{"experimental":' + "[" * 20000 + "]" * 20000 + "}")
    mode = setup_tools.extension_mode(settings)
    assert mode.state == setup_tools.UNDETERMINED
    assert "could not be read" in mode.detail


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
def test_status_calls_a_machine_with_no_settings_file_inert(deployed, capsys):
    """No settings file at all: the fresh-machine case, and it is a finding.

    This test used to assert the opposite -- "could not tell", exit 0 -- on the
    reasoning that a machine where the CLI had never run should not be failed
    for a file the CLI had not written yet. That was the wrong shape of
    caution. An unset setting was measured to load nothing, and the CLI never
    writes the key on its own, so a fresh machine is not *pending* an answer,
    it is inert and staying that way. Exit 0 there meant the report was
    quietest about the single most common broken configuration.

    Kept as a distinct test from the ``experimental: false`` case above
    because they reach DISABLED down different branches, and a report that
    handles one is not thereby shown to handle the other.
    """
    _repo, home, _operator = deployed
    (home / "settings.json").unlink()

    code = setup_tools.report_status()
    out = capsys.readouterr().out

    assert "experimental mode is OFF" in out
    assert "inert" in out
    assert code == 1
    # The remedy and the provenance both have to survive into the output the
    # user actually sees, not just into the ExtensionMode object. Asserting
    # the state alone leaves the wording free to drift away from it.
    assert "--experimental" in out
    assert "measured" in out
    # And it must not still be hedging: the old wording claimed the answer was
    # unavailable, which is now false and would be the more expensive kind of
    # wrong, because it reads as care.
    assert "could not be determined" not in out


@node
def test_status_does_not_call_an_undetermined_machine_broken(deployed, capsys):
    """A failed read still must not be reported as a defect found.

    The absent-key and absent-file cases moved to DISABLED, and they used to
    be what covered this branch. They are gone from it now, so the branch
    needs a cause that is genuinely unknown rather than merely unset: a
    recorded value that is not a boolean says the file was read fine and still
    settles nothing.

    Without this replacement, retiring those two cases would have silently
    dropped the only coverage of the report's most important restraint --
    which is the same shape of loss as the bug the restraint exists to
    prevent.
    """
    _repo, home, _operator = deployed
    (home / "settings.json").write_text(json.dumps({"experimental": "true"}),
                                        encoding="utf-8")

    code = setup_tools.report_status()
    out = capsys.readouterr().out

    assert "could not tell" in out
    assert code == 0
    assert "inert and silent" not in out, \
        "a failed read was reported as a verdict about the extensions"
    assert "could not be determined" in out


def test_a_disabled_machine_is_told_plainly_that_nothing_runs(deployed, capsys):
    """The counterpart: where the answer IS known, hedging would be its own lie."""
    _repo, home, _operator = deployed
    (home / "settings.json").write_text(json.dumps({"experimental": False}),
                                        encoding="utf-8")

    code = setup_tools.report_status()
    out = capsys.readouterr().out

    assert "inert and silent" in out
    assert "could not be determined" not in out
    assert code == 1


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
