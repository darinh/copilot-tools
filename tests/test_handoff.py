"""Tests for the handoff tool."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import handoff_tool as ho


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".copilot" / "projects").mkdir(parents=True)
    restart = tmp_path / "operator" / "restart"
    restart.mkdir(parents=True)
    catalog = home / ".copilot" / "projects" / "catalog.csv"
    monkeypatch.setattr(ho, "CATALOG", catalog)
    monkeypatch.setattr(ho, "state_dir", lambda: restart)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    project = tmp_path / "proj"
    project.mkdir()
    return {"home": home, "catalog": catalog, "restart": restart, "project": project}


# ── rendering ───────────────────────────────────────────────────
def test_render_includes_required_sections():
    out = ho.render("did it", "", "next up", "", "")
    assert "# Session Handoff" in out
    assert "## Status" in out and "did it" in out
    assert "## Next Steps" in out and "next up" in out


def test_render_omits_empty_optional_sections():
    out = ho.render("s", "", "n", "", "")
    for heading in ("## In Progress", "## Context", "## Prompt"):
        assert heading not in out


def test_render_includes_supplied_optional_sections():
    out = ho.render("s", "wip", "n", "ctx", "prompt text")
    for heading in ("## In Progress", "## Context", "## Prompt"):
        assert heading in out


# ── path handling ───────────────────────────────────────────────
def test_same_or_within_matches_self_and_children(tmp_path):
    assert ho.same_or_within(str(tmp_path), str(tmp_path))
    assert ho.same_or_within(str(tmp_path / "a" / "b"), str(tmp_path))


def test_same_or_within_rejects_sibling_prefix(tmp_path):
    """A raw string prefix would wrongly match /srv/app2 against /srv/app."""
    parent = tmp_path / "app"
    sibling = tmp_path / "app2"
    parent.mkdir()
    sibling.mkdir()
    assert not ho.same_or_within(str(sibling), str(parent))


def test_normalize_is_case_insensitive_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(ho, "IS_WINDOWS", True)
    a = ho.normalize(str(tmp_path))
    assert a == a.lower()


# ── catalog ─────────────────────────────────────────────────────
def test_resolve_guid_finds_entry(env):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",abc-123\n', encoding="utf-8")
    assert ho.resolve_guid(env["project"]) == "abc-123"


def test_resolve_guid_tolerates_unquoted_entries(env):
    env["catalog"].write_text(
        f'{env["project"].resolve()},def-456\n', encoding="utf-8")
    assert ho.resolve_guid(env["project"]) == "def-456"


def test_resolve_guid_missing_entry_exits(env):
    env["catalog"].write_text('"/somewhere/else",zzz\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        ho.resolve_guid(env["project"])


def test_resolve_guid_missing_catalog_exits(env):
    with pytest.raises(SystemExit):
        ho.resolve_guid(env["project"])


# ── end to end ──────────────────────────────────────────────────
def test_handoff_writes_file_and_marker(env, monkeypatch, capsys):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-1\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)

    rc = ho.main([
        "--instance", "proj",
        "--status", "finished the thing",
        "--next", "do the next thing",
        "--context", "watch out for X",
        "--project-root", str(env["project"]),
    ])
    assert rc == 0

    handoff = env["home"] / ".copilot" / "projects" / "guid-1" / "next-session.md"
    body = handoff.read_text(encoding="utf-8")
    assert "finished the thing" in body
    assert "watch out for X" in body
    assert (env["restart"] / "proj").exists()


def test_handoff_uses_safe_instance_id_for_marker(env, monkeypatch):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-2\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    ho.main([
        "--instance", "my:proj",
        "--status", "s", "--next", "n",
        "--project-root", str(env["project"]),
    ])
    markers = [p.name for p in env["restart"].iterdir()]
    assert markers and all(":" not in m for m in markers)


def test_missing_status_exits(env):
    with pytest.raises(SystemExit):
        ho.main(["--next", "n", "--project-root", str(env["project"])])


def test_missing_next_exits(env):
    with pytest.raises(SystemExit):
        ho.main(["--status", "s", "--project-root", str(env["project"])])


def test_non_running_instance_warns_but_still_writes(env, monkeypatch, capsys):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-3\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: True)
    monkeypatch.setattr(ho.Mux, "has_session", lambda self, s: False)
    assert ho.main([
        "--instance", "ghost", "--status", "s", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0
    assert "Warning" in capsys.readouterr().err
    handoff = env["home"] / ".copilot" / "projects" / "guid-3" / "next-session.md"
    assert handoff.exists()


def test_equals_form_arguments_accepted(env, monkeypatch):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-4\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    assert ho.main([
        "--instance=proj", "--status=s", "--next=n",
        f"--project-root={env['project']}",
    ]) == 0


def test_handoff_body_is_utf8(env, monkeypatch):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-5\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    ho.main([
        "--instance", "proj",
        "--status", "caf\u00e9 \u4e2d\u6587 \U0001f600",
        "--next", "n",
        "--project-root", str(env["project"]),
    ])
    handoff = env["home"] / ".copilot" / "projects" / "guid-5" / "next-session.md"
    assert "caf\u00e9" in handoff.read_text(encoding="utf-8")


def test_uninferrable_instance_exits(env, monkeypatch):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-6\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: True)
    monkeypatch.setattr(ho.Mux, "list_sessions", lambda self: [])
    with pytest.raises(SystemExit):
        ho.main(["--status", "s", "--next", "n",
                 "--project-root", str(env["project"])])


def test_infer_instance_matches_by_cwd(env, monkeypatch):
    (env["restart"] / "alpha.managed").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "list_sessions", lambda self: ["alpha", "beta"])
    monkeypatch.setattr(
        ho.Mux, "pane_current_path",
        lambda self, s: str(env["project"]) if s == "alpha" else "/elsewhere",
    )
    assert ho.infer_instance(env["project"], ho.Mux()) == "alpha"
