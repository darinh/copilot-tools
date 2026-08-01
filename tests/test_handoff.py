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


# ── guid validation ─────────────────────────────────────────────
@pytest.mark.parametrize("guid", [
    "abc-123", "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "x", "..dots",
])
def test_guid_is_usable_accepts_a_plain_directory_name(guid):
    assert ho.guid_is_usable(guid)

@pytest.mark.parametrize("guid", [
    "", "   ".strip(), ".", "..", "../..", "a/b", "a\\b", "/abs", "\\abs",
    "...", "victim.", "victim ", " victim", "a.", "bad:stream", "q?x", "a*b",
    'a"b', "a|b", "a<b", "a>b", "a\x00b", "a\nb",
    "CON", "con", "NUL", "com1", "LPT9", "CON.txt",
])
def test_guid_is_usable_rejects_anything_that_is_not_one_component(guid):
    assert not ho.guid_is_usable(guid)


def test_guid_is_usable_rejects_a_trailing_dot_that_windows_would_strip(tmp_path):
    """`victim.` and `victim` are one directory on Windows.

    Accepting it would let a malformed catalog row overwrite a *different*
    project's handoff -- the same clobbering the blank-id guard prevents.
    """
    assert not ho.guid_is_usable("victim.")
    (tmp_path / "victim").mkdir()
    if ho.IS_WINDOWS:
        assert (tmp_path / "victim.").resolve() == (tmp_path / "victim").resolve()


def test_an_id_windows_cannot_create_is_rejected_before_mkdir(env, monkeypatch):
    """`bad:stream` used to reach mkdir and raise an uncaught OSError."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}","bad:stream"\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "s", "--next", "n",
                 "--project-root", str(env["project"])])


def test_unwritable_project_dir_dies_instead_of_raising(env, monkeypatch, capsys):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-ro\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)

    def boom(self, *a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "s", "--next", "n",
                 "--project-root", str(env["project"])])
    assert "Cannot create" in capsys.readouterr().err


@pytest.mark.parametrize("line", ['"{path}",\n', '{path},\n', '"{path}","" \n'])
def test_resolve_guid_rejects_a_blank_id(env, line):
    """A blank id used to resolve to the shared projects root itself."""
    env["catalog"].write_text(
        line.format(path=env["project"].resolve()), encoding="utf-8")
    with pytest.raises(SystemExit):
        ho.resolve_guid(env["project"])


def test_resolve_guid_rejects_an_id_that_escapes_the_projects_root(env):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}","../../elsewhere"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        ho.resolve_guid(env["project"])


def test_resolve_guid_keeps_the_actionable_message_for_a_missing_entry(env, capsys):
    """The no-match error tells the user the exact line to add. Keep it."""
    env["catalog"].write_text('"/somewhere/else",zzz\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        ho.resolve_guid(env["project"])
    err = capsys.readouterr().err
    assert "No catalog entry for" in err
    assert str(env["project"].resolve()) in err


def test_blank_id_never_writes_to_the_shared_projects_root(env, monkeypatch):
    """The whole point of the guard: one project must not clobber all of them."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    shared = env["home"] / ".copilot" / "projects" / "next-session.md"
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "s", "--next", "n",
                 "--project-root", str(env["project"])])
    assert not shared.exists()


def test_handoff_is_never_written_directly_to_its_final_path(env, monkeypatch):
    """Behavioural proof of atomicity, naming no new helper.

    Writing straight to `next-session.md` is what made a torn handoff possible.
    This forbids exactly that: the destination must be produced by renaming a
    completed file over it, so a write aimed at the final path is a failure.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-atomic\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    handoff = (env["home"] / ".copilot" / "projects" / "guid-atomic"
               / "next-session.md")
    real_write_text = Path.write_text

    def refuse_direct_writes(self, data, *a, **kw):
        # Compared resolved: a `==` on unresolved paths would silently stop
        # guarding anything the moment the implementation normalised its path.
        if self.resolve() == handoff.resolve():
            raise AssertionError(f"wrote directly to the destination: {self}")
        return real_write_text(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", refuse_direct_writes)
    assert ho.main(["--instance", "proj", "--status", "the status",
                    "--next", "n", "--project-root", str(env["project"])]) == 0
    assert "the status" in handoff.read_text(encoding="utf-8")


# ── atomic write ────────────────────────────────────────────────
def test_write_atomic_creates_the_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "next-session.md"
    ho.write_atomic(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert list(tmp_path.iterdir()) == [target]


def test_write_atomic_replaces_existing_content(tmp_path):
    target = tmp_path / "next-session.md"
    target.write_text("old and much longer than the replacement",
                      encoding="utf-8")
    ho.write_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [target]


def test_write_atomic_is_utf8(tmp_path):
    target = tmp_path / "next-session.md"
    ho.write_atomic(target, "caf\u00e9 \u4e2d\u6587 \U0001f600")
    assert target.read_text(encoding="utf-8").startswith("caf\u00e9")


def test_write_atomic_never_exposes_a_partial_file(tmp_path, monkeypatch):
    """The property that matters: a reader sees all of one version or the other.

    The destination is inspected from inside the content write, which is the
    only window in which a direct ``write_text`` would have left a truncated
    handoff visible.
    """
    target = tmp_path / "next-session.md"
    target.write_text("OLD", encoding="utf-8")
    seen = []
    real_write_text = Path.write_text

    def spy(self, data, *a, **kw):
        seen.append(target.read_text(encoding="utf-8"))
        return real_write_text(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", spy)
    ho.write_atomic(target, "NEW")
    assert seen == ["OLD"], "destination changed before the rename"
    assert target.read_text(encoding="utf-8") == "NEW"


def test_write_atomic_keeps_the_old_file_when_the_rename_fails(tmp_path, monkeypatch):
    target = tmp_path / "next-session.md"
    target.write_text("OLD", encoding="utf-8")

    def boom(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(ho.os, "replace", boom)
    with pytest.raises(SystemExit):
        ho.write_atomic(target, "NEW")
    assert target.read_text(encoding="utf-8") == "OLD"
    assert list(tmp_path.iterdir()) == [target], "temp file left behind"


def test_write_atomic_cleans_up_when_the_content_write_fails(tmp_path, monkeypatch):
    target = tmp_path / "next-session.md"
    real_write_text = Path.write_text

    def boom(self, data, *a, **kw):
        real_write_text(self, "half", *a, **kw)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(SystemExit):
        ho.write_atomic(target, "NEW")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [], "temp file left behind"


def test_write_atomic_temp_names_do_not_collide_between_writers(tmp_path, monkeypatch):
    """Two agents can hand off from one project at the same moment.

    A pid would be only nearly unique -- it repeats across containers sharing a
    mounted home, which is exactly where two agents collide -- so the temp name
    must be random rather than derived from the process.
    """
    target = tmp_path / "next-session.md"
    names = []
    real_write_text = Path.write_text

    def spy(self, data, *a, **kw):
        names.append(self.name)
        return real_write_text(self, data, *a, **kw)

    monkeypatch.setattr(Path, "write_text", spy)
    monkeypatch.setattr(ho.os, "getpid", lambda: 111)
    ho.write_atomic(target, "a")
    ho.write_atomic(target, "b")
    assert names[0] != names[1], "temp name is not unique per writer"
    assert all(n.endswith(".tmp") for n in names)
    assert target.read_text(encoding="utf-8") == "b"


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
