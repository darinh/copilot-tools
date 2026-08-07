"""Tests for the handoff tool."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import handoff_tool as ho


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".operator" / "projects").mkdir(parents=True)
    restart = tmp_path / "operator" / "restart"
    restart.mkdir(parents=True)
    catalog = home / ".operator" / "projects" / "catalog.csv"
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


# ── authorship ──────────────────────────────────────────────────
def test_render_stamps_the_authoring_instance_above_the_status():
    """A first-person document has to say whose person it is.

    Everything under `## Status` is written as "my worktree", "I claimed
    this" -- so the reader must meet the author before the content, exactly
    as with a notice.
    """
    out = ho.render("s", "", "n", "", "", instance="peer-1")
    assert "peer-1" in out
    assert out.index("# Session Handoff") < out.index("peer-1") < out.index("## Status")


def test_render_without_an_instance_is_byte_identical_to_before():
    """The stamp is opt-in, and an absent one leaves no trace.

    Paired with the notice control above for the same reason: a stamp
    rendered as an empty string would put a stray blank line into every
    handoff written by a caller that does not know the parameter exists.

    The prefix is asserted absent as well as the two renderings being equal.
    Equality alone is unfalsifiable for this property -- a stamp emitted
    unconditionally appears on *both* sides and the comparison still holds,
    which mutation testing demonstrated.
    """
    assert ho.render("s", "wip", "n", "ctx", "p", instance="") == \
        ho.render("s", "wip", "n", "ctx", "p")
    assert ho.AUTHOR_PREFIX not in ho.render("s", "wip", "n", "ctx", "p")


def test_a_name_wrapped_in_backticks_survives_containing_them():
    """`--instance` is free text, and `str.strip` is greedy.

    A name that begins or ends with a backtick -- or with a space -- must not
    come back short: every consumer of the stamp compares it to a live
    instance name, so a silently truncated one attributes the handoff to an
    agent that does not exist, while looking exactly like a successful read.

    Two independent reviewers found this on the first draft, which used a
    blanket `strip("`")`. It is delimiter removal, not normalisation.
    """
    for name in ("`x", "x`", "`x`", "``", " padded ", "\ttabbed"):
        assert ho.authoring_instance(
            ho.render("s", "", "n", "", "", instance=name)) == name


def test_authoring_instance_reads_back_what_render_wrote():
    """Round-trip, including the instance name that broke `operator send`.

    A live instance on this box is literally named `a,b`. Any stamp format
    that cannot survive a comma is a format that silently drops the one
    agent hardest to attribute.
    """
    for name in ("x", "copilot-tools", "a,b", "agent 7"):
        assert ho.authoring_instance(
            ho.render("s", "", "n", "", "", instance=name)) == name


def test_authoring_instance_says_nothing_for_an_unstamped_handoff():
    """Every handoff written before this change is unattributed.

    `None` is "does not say", and must not be confused with a name -- the
    caller's warning offers both causes on exactly this input.
    """
    assert ho.authoring_instance(
        "# Session Handoff\n\n## Status\nolder than the stamp\n") is None
    assert ho.authoring_instance("") is None


def test_a_stamp_in_the_body_is_not_authorship():
    """A handoff quoting this mechanism must not be able to re-attribute itself.

    Handoffs on this project routinely quote the tooling they describe, and
    a scan over the whole document would let `## Context` prose overwrite the
    header's answer. This is the repo's own string-literal-silences-the-scan
    defect, aimed the other way.
    """
    forged = ho.render("s", "", "n", ho.author_line("impostor"), "",
                       instance="real")
    assert ho.authoring_instance(forged) == "real"

    # And with no header stamp at all, a body line is still not an answer.
    assert ho.authoring_instance(
        "# Session Handoff\n\n## Status\n" + ho.author_line("impostor")
    ) is None


def test_the_author_stamp_is_not_mistakable_for_handoff_prose():
    """The parse anchors on this prefix, so it has to be unlikely by accident."""
    assert ho.author_line("x").startswith(ho.AUTHOR_PREFIX)
    assert "operator instance" in ho.AUTHOR_PREFIX


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
    shared = env["home"] / ".operator" / "projects" / "next-session.md"
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
    handoff = (env["home"] / ".operator" / "projects" / "guid-atomic"
               / "handoff" / "proj.md")
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

    handoff = env["home"] / ".operator" / "projects" / "guid-1" / "handoff" / "proj.md"
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
    handoff = env["home"] / ".operator" / "projects" / "guid-3" / "handoff" / "ghost.md"
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
    handoff = env["home"] / ".operator" / "projects" / "guid-5" / "handoff" / "proj.md"
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




# -- one writer per instance -------------------------------------
def _publish(env, monkeypatch, guid, instance, status="s"):
    """Run a real handoff for `instance` into `guid`, and return its project dir."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",{guid}\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    assert ho.main([
        "--instance", instance, "--status", status, "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0
    return env["home"] / ".operator" / "projects" / guid


def test_two_instances_do_not_share_a_mailbox(env, monkeypatch):
    """The defect this re-keying exists to remove.

    Under project keying these two writes were the same file, so the second
    agent's handoff destroyed the first's and the tool needed a lock, an
    archive and several hundred words of prose to make that survivable. Keyed
    by instance they are two files and there is nothing to arbitrate.

    Asserted on content rather than on paths, so it stays honest if the layout
    moves again: the question is whether each agent's own words are waiting
    for its own next session.
    """
    project_dir = _publish(env, monkeypatch, "guid-70", "peer-x",
                           status="peer-x's context")
    _publish(env, monkeypatch, "guid-70", "peer-y", status="peer-y's context")

    x = (project_dir / "handoff" / "peer-x.md").read_text(encoding="utf-8")
    y = (project_dir / "handoff" / "peer-y.md").read_text(encoding="utf-8")
    assert "peer-x's context" in x and "peer-y's context" not in x
    assert "peer-y's context" in y and "peer-x's context" not in y


def test_a_peer_publishing_leaves_no_warning_and_no_archive(env, monkeypatch, capsys):
    """The ordinary two-agent case is now silent, because nothing happened.

    A warning here would be the old behaviour surviving the re-key: it fired
    because a peer's write collided, and after the re-key a peer's write
    cannot collide. Kept as a test because a `bank_prior_handoff` wired to the
    project directory rather than the instance file would pass every content
    assertion above while warning on every peer handoff.
    """
    project_dir = _publish(env, monkeypatch, "guid-71", "peer-x")
    capsys.readouterr()
    _publish(env, monkeypatch, "guid-71", "peer-y")

    assert "unread handoff" not in capsys.readouterr().err
    assert sorted(p.name for p in (project_dir / "handoff").iterdir()) == \
        ["peer-x.md", "peer-y.md"]


def test_an_instance_overwriting_its_own_unread_handoff_banks_it(env, monkeypatch, capsys):
    """The one collision the re-key does not remove.

    The reader deletes the file, so a handoff still sitting here when the same
    instance writes its next one means a session ended without picking up what
    the one before it left. That is worth keeping and worth saying.
    """
    project_dir = _publish(env, monkeypatch, "guid-72", "solo",
                           status="the first session's context")
    capsys.readouterr()
    _publish(env, monkeypatch, "guid-72", "solo",
             status="the second session's context")

    handoff_dir = project_dir / "handoff"
    assert "the second session's context" in \
        (handoff_dir / "solo.md").read_text(encoding="utf-8")
    assert "the first session's context" in \
        (handoff_dir / "solo.prev.md").read_text(encoding="utf-8")
    err = capsys.readouterr().err
    assert "unread handoff" in err and "solo" in err


def test_the_banked_slot_is_one_slot_and_not_an_archive(env, monkeypatch):
    """Bounded on purpose, and the bound is asserted rather than assumed.

    The unbounded archive this replaces needed a promise never to prune it,
    because under project keying every file in there might have been somebody's
    only copy. Here a second consecutive miss means the read side is broken,
    and keeping the older of two undelivered handoffs does not fix that.
    """
    project_dir = _publish(env, monkeypatch, "guid-73", "solo", status="first")
    for status in ("second", "third"):
        _publish(env, monkeypatch, "guid-73", "solo", status=status)

    handoff_dir = project_dir / "handoff"
    assert sorted(p.name for p in handoff_dir.iterdir()) == \
        ["solo.md", "solo.prev.md"]
    assert "third" in (handoff_dir / "solo.md").read_text(encoding="utf-8")
    assert "second" in (handoff_dir / "solo.prev.md").read_text(encoding="utf-8")


def test_the_ordinary_path_leaves_nothing_behind(env, monkeypatch):
    """No prev slot, no warning, no noise when nobody missed a handoff.

    Names nothing from the change, so a `bank_prior_handoff` that fired
    unconditionally would break this while passing every test above.
    """
    project_dir = _publish(env, monkeypatch, "guid-74", "proj")
    assert [p.name for p in (project_dir / "handoff").iterdir()] == ["proj.md"]


def test_a_handoff_that_cannot_be_moved_aside_is_not_overwritten(env, monkeypatch):
    """Refusing beats destroying. The predecessor survives a failed handoff."""
    project_dir = _publish(env, monkeypatch, "guid-75", "solo",
                           status="PRECIOUS")
    handoff = project_dir / "handoff" / "solo.md"

    def refuse(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(ho.os, "replace", refuse)
    with pytest.raises(SystemExit):
        ho.main([
            "--instance", "solo", "--status", "s", "--next", "n",
            "--project-root", str(env["project"]),
        ])
    assert "PRECIOUS" in handoff.read_text(encoding="utf-8")


# -- authorship, end to end --------------------------------------
def test_the_published_handoff_names_the_instance_that_wrote_it(env, monkeypatch):
    """The stamp, asserted through `main` rather than through `render`.

    `render` taking the parameter proves nothing if `main` never passes it,
    and `main` is where the instance name is actually known.
    """
    project_dir = _publish(env, monkeypatch, "guid-90", "copilot-tools")
    published = (project_dir / "handoff" / "copilot-tools.md").read_text(
        encoding="utf-8")
    assert ho.authoring_instance(published) == "copilot-tools"


def test_the_stamp_and_the_filename_agree(env, monkeypatch):
    """They are derived from one name at one moment, and must stay so.

    The stamp survives a file being copied out of its directory, which is the
    only reason it is still here -- and a stamp that disagreed with the
    filename would be worse than no stamp at all, because the migration below
    routes on it.
    """
    project_dir = _publish(env, monkeypatch, "guid-95", "my:proj")
    written = list((project_dir / "handoff").iterdir())
    assert len(written) == 1
    stamped = ho.authoring_instance(written[0].read_text(encoding="utf-8"))
    assert ho.safe_instance_id(stamped) == written[0].stem
