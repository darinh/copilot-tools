"""Tests for the handoff tool."""
from __future__ import annotations

import os
import stat
import sys
import threading
import time
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


def test_render_places_a_notice_under_the_title_and_above_the_status():
    """Position is the whole of its usefulness.

    Above the title it would stop the file being a handoff document to
    anything keying on the header; below `## Status` a reader meets the
    content before the caveat that qualifies it.
    """
    out = ho.render("s", "", "n", "", "", notice="> NOTE")
    assert out.index("# Session Handoff") < out.index("> NOTE") < out.index("## Status")


def test_render_without_a_notice_is_byte_identical_to_before():
    """No notice must mean no trace of one -- not even a blank line."""
    assert ho.render("s", "wip", "n", "ctx", "p", notice="") == \
        ho.render("s", "wip", "n", "ctx", "p")
    assert ho.render("s", "", "n", "", "") == \
        "# Session Handoff\n\n## Status\ns\n\n## Next Steps\nn\n"


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


# ── preserving an unread handoff ────────────────────────────────
def test_preserve_does_nothing_when_no_handoff_is_waiting(tmp_path):
    """The normal case: the reader consumed the last one, so the slot is free.

    Counterpart to the tests below -- a guard that archived unconditionally
    would satisfy every one of them while filling `superseded/` with copies of
    files nobody was about to lose.
    """
    target = tmp_path / "next-session.md"
    assert ho.preserve_prior_handoff(target) is None
    assert list(tmp_path.iterdir()) == []


def test_preserve_copies_an_unread_handoff_verbatim(tmp_path):
    target = tmp_path / "next-session.md"
    target.write_text("# Session Handoff\n\ncaf\u00e9 \U0001f600", encoding="utf-8")

    saved = ho.preserve_prior_handoff(target)

    assert saved is not None
    assert saved.parent == tmp_path / ho.SUPERSEDED_DIRNAME
    assert saved.read_text(encoding="utf-8") == "# Session Handoff\n\ncaf\u00e9 \U0001f600"


def test_preserve_leaves_the_original_in_place(tmp_path):
    """Copy, never move: the source must survive until the copy exists.

    A move would open a window in which the only copy of the handoff is in
    flight, which is the failure this function exists to prevent.
    """
    target = tmp_path / "next-session.md"
    target.write_text("ORIGINAL", encoding="utf-8")
    ho.preserve_prior_handoff(target)
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


def test_preserve_skips_an_empty_handoff(tmp_path):
    target = tmp_path / "next-session.md"
    target.write_text("   \n\n", encoding="utf-8")
    assert ho.preserve_prior_handoff(target) is None
    assert not (tmp_path / ho.SUPERSEDED_DIRNAME).exists()


def test_preserve_never_writes_over_an_existing_archive(tmp_path, monkeypatch):
    """O_EXCL, not truncate. An archiver that clobbers archives is not one."""
    target = tmp_path / "next-session.md"
    target.write_text("SECOND", encoding="utf-8")
    archive_dir = tmp_path / ho.SUPERSEDED_DIRNAME
    archive_dir.mkdir()
    (archive_dir / "taken.md").write_text("FIRST", encoding="utf-8")

    names = iter(["taken.md", "free.md"])
    monkeypatch.setattr(ho, "_superseded_name", lambda p: next(names))

    saved = ho.preserve_prior_handoff(target)

    assert (archive_dir / "taken.md").read_text(encoding="utf-8") == "FIRST"
    assert saved == archive_dir / "free.md"
    assert saved.read_text(encoding="utf-8") == "SECOND"


def test_preserve_gives_up_rather_than_reusing_a_name(tmp_path, monkeypatch):
    target = tmp_path / "next-session.md"
    target.write_text("SECOND", encoding="utf-8")
    archive_dir = tmp_path / ho.SUPERSEDED_DIRNAME
    archive_dir.mkdir()
    (archive_dir / "only.md").write_text("FIRST", encoding="utf-8")
    monkeypatch.setattr(ho, "_superseded_name", lambda p: "only.md")

    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert (archive_dir / "only.md").read_text(encoding="utf-8") == "FIRST"
    assert target.read_text(encoding="utf-8") == "SECOND"


def test_preserve_refuses_a_path_it_cannot_examine(tmp_path, monkeypatch):
    """Unreadable is not absent, and absent is what licenses the overwrite."""
    target = tmp_path / "next-session.md"
    target.write_text("UNREACHABLE", encoding="utf-8")

    def denied(path, *a, **kw):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(ho.os, "lstat", denied)
    monkeypatch.setattr(ho, "path_present", lambda p: None)
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert target.read_text(encoding="utf-8") == "UNREACHABLE"


def test_preserve_refuses_a_handoff_it_cannot_read(tmp_path, monkeypatch):
    target = tmp_path / "next-session.md"
    target.write_text("UNREADABLE", encoding="utf-8")
    real_open = ho.os.open

    def denied(path, *a, **kw):
        if Path(path) == target:
            raise PermissionError(13, "denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(ho.os, "open", denied)
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert not (tmp_path / ho.SUPERSEDED_DIRNAME).exists()
    assert target.read_text(encoding="utf-8") == "UNREADABLE"


def test_preserve_rechecks_the_type_on_the_descriptor_it_reads(
        tmp_path, monkeypatch):
    """The file measured by `lstat` can be a fifo by the time it is opened.

    `read_bytes` re-opens by name, so the check would have described a file
    that is no longer there. The type is therefore re-established on the open
    descriptor, which nothing can substitute.
    """
    target = tmp_path / "next-session.md"
    target.write_text("was a regular file", encoding="utf-8")

    class FifoStat:
        st_mode = stat.S_IFIFO | 0o644
        st_size = 0
        st_ino = 0
        st_dev = 0

    monkeypatch.setattr(ho.os, "fstat", lambda fd: FifoStat())
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert not (tmp_path / ho.SUPERSEDED_DIRNAME).exists()


def test_preserve_refuses_a_file_that_grows_past_the_limit_while_read(
        tmp_path, monkeypatch):
    """The size can change between the fstat and the read."""
    target = tmp_path / "next-session.md"
    target.write_text("x" * 500, encoding="utf-8")

    class SmallStat:
        st_mode = stat.S_IFREG | 0o644
        st_size = 1
        st_ino = 0
        st_dev = 0

    monkeypatch.setattr(ho, "MAX_PRESERVE_BYTES", 10)
    monkeypatch.setattr(ho.os, "fstat", lambda fd: SmallStat())
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert not (tmp_path / ho.SUPERSEDED_DIRNAME).exists()


def test_preserve_treats_a_vanished_file_as_nothing_to_save(tmp_path, monkeypatch):
    """The reader can consume the handoff between the probe and the stat.

    `None` is also what the ordinary absent path returns, so the return value
    alone cannot say which branch ran -- and if this patch ever stopped
    reaching the name `preserve_prior_handoff` actually calls, the real probe
    would answer False for this missing file and the assertion would pass on
    the wrong branch, testing nothing. So the stand-in records its own calls:
    it is consulted only if the patch is live, and once it answers True the
    absent branch is closed, leaving the vanished-between-probe-and-stat race
    as the only way back to `None`.

    Spying on `ho.os.lstat` instead does *not* work, and the near-miss is worth
    keeping: `ho.os` is the one shared `os` module, so a real `path_present` --
    exactly the case this needs to catch -- calls `lstat` itself and populates
    the spy on the way to the wrong branch.
    """
    target = tmp_path / "next-session.md"
    consulted = []
    monkeypatch.setattr(ho, "path_present",
                        lambda p: (consulted.append(Path(p)), True)[1])

    assert ho.preserve_prior_handoff(target) is None
    assert target in consulted, \
        "the stand-in probe was never called, so the real one answered " \
        "'absent' and the race this test is named for was not exercised"


def test_a_probe_that_says_absent_is_the_one_preserve_consults(tmp_path, monkeypatch):
    """Control for the test above, through the same call.

    Both tests patch `ho.path_present`; this one drives it the other way with a
    handoff that really is sitting there. A live patch means no archive. A
    patch that misses the name preserve consults means the real probe sees the
    file, the copy happens, and this fails -- which is what makes the sibling's
    `is None` mean something.
    """
    target = tmp_path / "next-session.md"
    target.write_text("UNREAD", encoding="utf-8")
    monkeypatch.setattr(ho, "path_present", lambda p: False)

    assert ho.preserve_prior_handoff(target) is None
    assert not (tmp_path / ho.SUPERSEDED_DIRNAME).exists(), \
        "the patched probe was not the one preserve_prior_handoff calls"


def test_preserve_refuses_a_directory(tmp_path):
    target = tmp_path / "next-session.md"
    target.mkdir()
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert target.is_dir()


def test_preserve_refuses_something_that_is_not_a_regular_file(tmp_path, monkeypatch):
    """A fifo would hang the process forever; a device node would be destroyed."""
    target = tmp_path / "next-session.md"
    target.write_text("x", encoding="utf-8")
    real_lstat = ho.os.lstat

    class FakeStat:
        st_mode = stat.S_IFIFO | 0o644
        st_size = 0

    monkeypatch.setattr(ho.os, "lstat",
                        lambda p, *a, **kw: FakeStat()
                        if Path(p) == target else real_lstat(p, *a, **kw))
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)


def test_preserve_refuses_an_implausibly_large_handoff(tmp_path, monkeypatch):
    target = tmp_path / "next-session.md"
    target.write_text("x" * 200, encoding="utf-8")
    monkeypatch.setattr(ho, "MAX_PRESERVE_BYTES", 10)
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert target.read_text(encoding="utf-8") == "x" * 200


def test_a_refusal_to_overwrite_still_banks_this_sessions_handoff(
        env, monkeypatch):
    """Protecting the dead session must not be paid for with the live one.

    When the predecessor cannot be preserved the tool gives up rather than
    replacing it -- but it writes this session's words down first, so the
    operator ends up with two files that both exist instead of being told
    which one was destroyed on its behalf.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-17\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-17"
    project_dir.mkdir(parents=True)
    prior = project_dir / "next-session.md"
    prior.write_text("UNREADABLE PREDECESSOR", encoding="utf-8")
    real_open = ho.os.open

    def denied(path, *a, **kw):
        if Path(path) == prior:
            raise PermissionError(13, "denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(ho.os, "open", denied)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "LIVE SESSION WORDS",
                 "--next", "n", "--project-root", str(env["project"])])

    assert prior.read_text(encoding="utf-8") == "UNREADABLE PREDECESSOR"
    banked = [p.read_text(encoding="utf-8")
              for p in (project_dir / ho.SUPERSEDED_DIRNAME).iterdir()]
    assert any("LIVE SESSION WORDS" in text for text in banked), \
        "the live session's handoff was lost to protect the dead one"


def test_a_refusal_that_cannot_bank_prints_the_handoff_instead(
        env, monkeypatch, capsys):
    """Last resort: stderr is still somewhere the words exist."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-18\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-18"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md").write_text("PRIOR", encoding="utf-8")

    def refuse(handoff_file):
        raise ho.PreserveError("nope")

    def no_bank(handoff_file, payload):
        raise OSError("no room")

    monkeypatch.setattr(ho, "preserve_prior_handoff", refuse)
    monkeypatch.setattr(ho, "_archive", no_bank)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "ONLY COPY LEFT",
                 "--next", "n", "--project-root", str(env["project"])])
    assert "ONLY COPY LEFT" in capsys.readouterr().err


def test_preserve_refuses_a_file_swapped_under_the_descriptor(
        tmp_path, monkeypatch):
    """O_NOFOLLOW is 0 on Windows, so identity is re-checked on the descriptor."""
    target = tmp_path / "next-session.md"
    target.write_text("ORIGINAL", encoding="utf-8")

    class OtherFile:
        st_mode = stat.S_IFREG | 0o644
        st_size = 8
        st_ino = 999999
        st_dev = 7

    monkeypatch.setattr(ho.os, "fstat", lambda fd: OtherFile())
    monkeypatch.setattr(ho.os, "lstat", lambda p, *a, **kw: _RealFile())
    with pytest.raises(ho.PreserveError):
        ho.preserve_prior_handoff(target)
    assert not (tmp_path / ho.SUPERSEDED_DIRNAME).exists()
    assert target.read_text(encoding="utf-8") == "ORIGINAL"


class _RealFile:
    st_mode = stat.S_IFREG | 0o644
    st_size = 8
    st_ino = 111111
    st_dev = 7


def test_an_unknown_file_index_is_not_read_as_a_match(tmp_path):
    """Windows can report zero. An unanswered question is not a yes."""
    class Zero:
        st_ino = 0
        st_dev = 0

    class Known:
        st_ino = 42
        st_dev = 1

    assert ho._swapped(Zero(), Known()) is False
    assert ho._swapped(Known(), Zero()) is False
    assert ho._swapped(Known(), Known()) is False


def test_preserve_records_a_symlink_and_removes_it(tmp_path):
    """The link's target survives; the link does not, and says so.

    The link has to go before the publish, not during it: on Windows a symlink
    to a directory is a directory entry and `os.replace` refuses to replace
    one, which would kill the handoff to protect a target that was never in
    danger. The target is never opened -- it could be a fifo, or a terabyte.
    """
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("TARGET CONTENT", encoding="utf-8")
    target = tmp_path / "next-session.md"
    try:
        target.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    saved = ho.preserve_prior_handoff(target)

    assert saved is not None
    body = saved.read_text(encoding="utf-8")
    assert "elsewhere.md" in body
    assert "TARGET CONTENT" not in body
    assert elsewhere.read_text(encoding="utf-8") == "TARGET CONTENT"
    assert not target.is_symlink(), "the link must be gone before the publish"


def test_preserve_removes_a_symlink_that_points_at_a_directory(tmp_path):
    """The Windows case: a directory symlink is a directory entry.

    `MoveFileEx` refuses to replace one, so leaving it in place would make the
    publish die with a bare access-denied and lose the live session's context.
    It also does not come off with `unlink` there -- it needs `rmdir`.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("KEEP", encoding="utf-8")
    target = tmp_path / "next-session.md"
    try:
        target.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    saved = ho.preserve_prior_handoff(target)

    assert saved is not None
    assert not target.is_symlink(), "the directory link must be gone"
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "KEEP"


def test_handoff_over_a_symlink_still_publishes(env, monkeypatch):
    """End to end: the symlink case must not become a failed handoff."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-14\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-14"
    project_dir.mkdir(parents=True)
    elsewhere = env["home"] / "elsewhere"
    elsewhere.mkdir()
    try:
        (project_dir / "next-session.md").symlink_to(
            elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")

    assert ho.main([
        "--instance", "proj", "--status", "published", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0
    handoff = project_dir / "next-session.md"
    assert not handoff.is_symlink()
    assert "published" in handoff.read_text(encoding="utf-8")
    assert elsewhere.is_dir()


def test_preserve_survives_a_symlink_target_that_is_not_utf8(tmp_path):
    """POSIX link targets are bytes; Python hands them back as surrogates.

    Encoding those strictly raises UnicodeEncodeError, which is not an OSError
    and would leave the tool as a traceback rather than a message.
    """
    if os.name == "nt":
        pytest.skip("Windows paths are text")
    target = tmp_path / "next-session.md"
    try:
        os.symlink(os.fsdecode(b"/tmp/\xff\xferaw"), target)
    except (OSError, NotImplementedError, UnicodeError):
        pytest.skip("symlinks not permitted here")

    saved = ho.preserve_prior_handoff(target)

    assert saved is not None
    assert saved.read_bytes()



# ── serialising concurrent handoffs ─────────────────────────────
def test_lock_is_exclusive_while_held(tmp_path, monkeypatch):
    """Two agents, one project. The second must not walk into the first.

    Preserving without serialising only changes which session is destroyed:
    A copies the predecessor aside, B copies the same predecessor aside and
    publishes, A publishes over B, and B was never archived because it did not
    exist when A looked.
    """
    handoff = tmp_path / "next-session.md"
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.1)
    with ho.handoff_lock(handoff) as first:
        assert first is True
        with ho.handoff_lock(handoff) as second:
            assert second is False


def test_lock_is_released_for_the_next_writer(tmp_path):
    handoff = tmp_path / "next-session.md"
    with ho.handoff_lock(handoff) as first:
        assert first is True
    with ho.handoff_lock(handoff) as second:
        assert second is True
    assert list(tmp_path.iterdir()) == []


def test_lock_is_released_when_the_handoff_fails(tmp_path):
    """`die` raises SystemExit from inside the block -- the lock must not stay."""
    handoff = tmp_path / "next-session.md"
    with pytest.raises(SystemExit):
        with ho.handoff_lock(handoff):
            sys.exit(1)
    assert list(tmp_path.iterdir()) == []


def test_stale_lock_is_reclaimed(tmp_path, monkeypatch):
    """A process that died between taking the lock and dropping it."""
    handoff = tmp_path / "next-session.md"
    lock = tmp_path / "next-session.md.lock"
    lock.write_text("999999", encoding="utf-8")
    old = time.time() - (ho.LOCK_STALE_SECONDS + 60)
    os.utime(lock, (old, old))
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 5.0)
    with ho.handoff_lock(handoff) as acquired:
        assert acquired is True


def test_lock_does_not_spin_forever_on_an_unremovable_stale_lock(
        tmp_path, monkeypatch):
    handoff = tmp_path / "next-session.md"
    lock = tmp_path / "next-session.md.lock"
    lock.write_text("999999", encoding="utf-8")
    old = time.time() - (ho.LOCK_STALE_SECONDS + 60)
    os.utime(lock, (old, old))

    def refuse(self, *a, **kw):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "unlink", refuse)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.2)
    started = time.monotonic()
    with ho.handoff_lock(handoff) as acquired:
        assert acquired is False
    assert time.monotonic() - started < 5


def test_lock_release_does_not_remove_someone_elses_lock(tmp_path):
    """The reclaim can hand the lock on while the first holder is still inside.

    A holder that unlinks on the way out without checking would then delete a
    lock its successor is relying on, and a third writer would walk in.
    """
    handoff = tmp_path / "next-session.md"
    lock = tmp_path / "next-session.md.lock"
    with ho.handoff_lock(handoff) as acquired:
        assert acquired is True
        lock.write_text("someone-elses-token 4242 now\n", encoding="utf-8")
    assert lock.exists(), "released a lock this process no longer owned"
    assert "someone-elses-token" in lock.read_text(encoding="utf-8")


def test_lock_is_not_left_behind_when_its_metadata_cannot_be_written(
        tmp_path, monkeypatch):
    """A lock nobody can prove ownership of would block writers until it aged out."""
    handoff = tmp_path / "next-session.md"

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(ho.os, "fdopen", boom)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.1)
    with ho.handoff_lock(handoff) as acquired:
        assert acquired is False
    assert list(tmp_path.iterdir()) == [], "stranded lock left behind"


def test_handoff_banks_a_spare_copy_when_it_cannot_take_the_lock(
        env, monkeypatch):
    """The unlocked path is the one where another writer can still overwrite us.

    So the words go on disk before the race can have them: whatever happens to
    `next-session.md`, this session's context exists somewhere.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-15\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-15"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md.lock").write_text("held", encoding="utf-8")

    assert ho.main([
        "--instance", "proj", "--status", "CONTENDED CONTEXT", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    banked = [p.read_text(encoding="utf-8")
              for p in (project_dir / ho.SUPERSEDED_DIRNAME).iterdir()]
    assert any("CONTENDED CONTEXT" in text for text in banked)


def test_an_unserialised_publish_says_so_in_the_file_it_publishes(
        env, monkeypatch):
    """The reader is the party the stderr warning never reaches.

    A handoff written without the lock may be replaced by the concurrent
    writer moments later, so `next-session.md` cannot be trusted to be the
    newest one -- and until this notice existed, nothing in it said so. The
    session that caused the race sees a warning on stderr and then ends; the
    session that has to act on the consequence reads only the file.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-30\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-30"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md.lock").write_text("held", encoding="utf-8")

    assert ho.main([
        "--instance", "proj", "--status", "RACED CONTEXT", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    published = (project_dir / "next-session.md").read_text(encoding="utf-8")
    assert "RACED CONTEXT" in published
    assert ho.NOTICE_UNSERIALISED in published
    # The published file is not the banked copy and must not claim to be one:
    # a reader told "this may never have reached next-session.md" by the very
    # file at next-session.md learns nothing it can act on.
    assert ho.NOTICE_BANKED_UNSERIALISED not in published


def test_the_banked_copy_says_it_may_never_have_been_published(
        env, monkeypatch):
    """A file in `superseded/` cannot otherwise be told apart from a predecessor.

    Both arrive by the same route and are named by the same scheme. Only one
    of them may be *newer* than the handoff beside it, and that is exactly the
    thing a reader has to know before deciding what to pick up.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-31\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-31"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md.lock").write_text("held", encoding="utf-8")

    assert ho.main([
        "--instance", "proj", "--status", "BANKED CONTEXT", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    banked = [p.read_text(encoding="utf-8")
              for p in (project_dir / ho.SUPERSEDED_DIRNAME).iterdir()]
    assert any("BANKED CONTEXT" in text and ho.NOTICE_BANKED_UNSERIALISED in text
               for text in banked)
    assert not any(ho.NOTICE_UNSERIALISED in text for text in banked)


def test_a_handoff_that_was_never_published_says_so_in_its_banked_copy(
        env, monkeypatch):
    """This copy is not a loser of a race -- it is the only copy there is.

    The predecessor could not be preserved, so it was not replaced: the words
    banked here never reached `next-session.md` at all, and the file sitting
    there is strictly older than this one.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-32\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-32"
    project_dir.mkdir(parents=True)
    prior = project_dir / "next-session.md"
    prior.write_text("UNREADABLE PREDECESSOR", encoding="utf-8")

    def refuse(handoff_file):
        raise ho.PreserveError("cannot preserve")

    monkeypatch.setattr(ho, "preserve_prior_handoff", refuse)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "NEVER PUBLISHED",
                 "--next", "n", "--project-root", str(env["project"])])

    assert prior.read_text(encoding="utf-8") == "UNREADABLE PREDECESSOR"
    banked = [p.read_text(encoding="utf-8")
              for p in (project_dir / ho.SUPERSEDED_DIRNAME).iterdir()]
    assert any("NEVER PUBLISHED" in text and ho.NOTICE_BANKED_UNPUBLISHED in text
               for text in banked)


def test_an_uncontended_handoff_carries_no_notice(env, monkeypatch):
    """The control that stops the notice becoming furniture.

    A banner printed on every handoff is one a reader learns to skip, and then
    it is worth nothing on the one occasion it is true. The ordinary path --
    lock taken, nothing waiting -- must produce the same bytes it always did.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-33\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    assert ho.main([
        "--instance", "proj", "--status", "ordinary", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    project_dir = env["home"] / ".copilot" / "projects" / "guid-33"
    published = (project_dir / "next-session.md").read_text(encoding="utf-8")
    assert published == ho.render("ordinary", "", "n", "", "")
    for notice in (ho.NOTICE_UNSERIALISED, ho.NOTICE_BANKED_UNSERIALISED,
                   ho.NOTICE_BANKED_UNPUBLISHED):
        assert notice not in published


def test_the_three_notices_are_pairwise_distinct():
    """Otherwise every assertion above proves less than it appears to.

    Three tests each pin "this file carries notice X and not notice Y". If two
    of the notices were the same string -- a copy-paste away -- those
    assertions would contradict each other and one of them would be asserting
    a tautology. Identity is the property they all rest on, so it is asserted
    once, directly.
    """
    notices = [ho.NOTICE_UNSERIALISED, ho.NOTICE_BANKED_UNSERIALISED,
               ho.NOTICE_BANKED_UNPUBLISHED]
    assert len(set(notices)) == 3
    for notice in notices:
        # A notice a reader can mistake for handoff prose is not a notice.
        assert notice.startswith("> ")
        assert notice.strip()


def test_a_preserved_predecessor_is_never_stamped(env, monkeypatch):
    """The notice describes *this* write. Somebody else's bytes are evidence.

    `preserve_prior_handoff` archives a predecessor verbatim, and a stamp
    added there would be this tool editing a file whose whole point is that it
    was not altered.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-34\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-34"
    project_dir.mkdir(parents=True)
    original = "# Session Handoff\n\nthe predecessor's own words"
    (project_dir / "next-session.md").write_text(original, encoding="utf-8")

    assert ho.main([
        "--instance", "proj", "--status", "successor", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    archived = list((project_dir / ho.SUPERSEDED_DIRNAME).iterdir())
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == original


def test_the_published_notice_does_not_depend_on_the_spare_copy(
        env, monkeypatch):
    """It is chosen before the bank is attempted, so it cannot report one.

    Adversarial review found the first version of this: `published` was set to
    a notice reading "a copy of this handoff was banked in `superseded/`" and
    then the bank raised, leaving `next-session.md` pointing a reader at a
    copy that does not exist -- and, worse, at a `superseded/` whose only
    occupant is the preserved predecessor, which is *older*. The notice now
    claims nothing about the spare, which is the only phrasing that is true on
    both branches.
    """
    def publish(guid, bank_works):
        env["catalog"].write_text(
            f'"{env["project"].resolve()}",{guid}\n', encoding="utf-8")
        project_dir = env["home"] / ".copilot" / "projects" / guid
        project_dir.mkdir(parents=True)
        (project_dir / "next-session.md.lock").write_text(
            "held", encoding="utf-8")
        real_archive = ho._archive
        if not bank_works:
            monkeypatch.setattr(
                ho, "_archive",
                lambda f, p: (_ for _ in ()).throw(OSError("no room")))
        try:
            assert ho.main([
                "--instance", "proj", "--status", "SAME WORDS", "--next", "n",
                "--project-root", str(env["project"]),
            ]) == 0
        finally:
            monkeypatch.setattr(ho, "_archive", real_archive)
        return (project_dir / "next-session.md").read_text(encoding="utf-8")

    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    banked = publish("guid-35", bank_works=True)
    unbanked = publish("guid-36", bank_works=False)

    assert banked == unbanked, (
        "the published handoff differs by whether the spare copy succeeded, "
        "so it is making a claim about the spare that one of the two cases "
        "will falsify")
    # And the literal false statement, named, because "they are equal" would
    # also be satisfied by both of them being wrong in the same way.
    assert "was banked" not in ho.NOTICE_UNSERIALISED


def test_the_banked_notice_does_not_claim_the_publish_happened(
        env, monkeypatch):
    """It is written before the publish is attempted, and the publish can abort.

    Lock held *and* the predecessor unpreservable: the spare is banked, then
    `preserve_prior_handoff` refuses and the tool dies without publishing. A
    banked copy asserting "this session published unserialised" is then simply
    false -- and it contradicts the second copy banked moments later on the
    same invocation, which correctly says the handoff was never published.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-37\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-37"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md.lock").write_text("held", encoding="utf-8")
    prior = project_dir / "next-session.md"
    prior.write_text("PREDECESSOR", encoding="utf-8")

    def refuse(handoff_file):
        raise ho.PreserveError("cannot preserve")

    monkeypatch.setattr(ho, "preserve_prior_handoff", refuse)
    with pytest.raises(SystemExit):
        ho.main(["--instance", "proj", "--status", "ABANDONED PUBLISH",
                 "--next", "n", "--project-root", str(env["project"])])

    # The publish never happened, so nothing may say that it did.
    assert prior.read_text(encoding="utf-8") == "PREDECESSOR"
    banked = [p.read_text(encoding="utf-8")
              for p in (project_dir / ho.SUPERSEDED_DIRNAME).iterdir()]
    assert len(banked) == 2, \
        "both banks should have run on this path: insurance, then the refusal"
    assert all("ABANDONED PUBLISH" in text for text in banked)
    assert any(ho.NOTICE_BANKED_UNSERIALISED in text for text in banked)
    assert any(ho.NOTICE_BANKED_UNPUBLISHED in text for text in banked)
    assert "this session published" not in ho.NOTICE_BANKED_UNSERIALISED


def test_a_failed_spare_copy_does_not_stop_the_handoff(env, monkeypatch):
    """Insurance is a second chance, never a reason to lose the first one."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-16\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-16"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md.lock").write_text("held", encoding="utf-8")

    def boom(handoff_file, payload):
        raise OSError("no room")

    monkeypatch.setattr(ho, "_archive", boom)
    assert ho.main([
        "--instance", "proj", "--status", "written regardless", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0
    assert "written regardless" in (
        project_dir / "next-session.md").read_text(encoding="utf-8")


def test_handoff_still_writes_when_the_lock_cannot_be_taken(env, monkeypatch):
    """Deliberate policy, pinned so nobody "fixes" it into a refusal.

    A handoff that refuses to run because a lock is held discards the context
    of the session that is running now -- a certain loss, to avoid a race whose
    remaining window is the few microseconds between this process's own
    preserve and its own rename.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-11\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.05)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-11"
    project_dir.mkdir(parents=True)
    (project_dir / "next-session.md.lock").write_text("1", encoding="utf-8")

    assert ho.main([
        "--instance", "proj", "--status", "written anyway", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0
    assert "written anyway" in (
        project_dir / "next-session.md").read_text(encoding="utf-8")


def test_handoff_leaves_no_lock_behind(env, monkeypatch):
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-12\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    ho.main([
        "--instance", "proj", "--status", "s", "--next", "n",
        "--project-root", str(env["project"]),
    ])
    project_dir = env["home"] / ".copilot" / "projects" / "guid-12"
    assert [p.name for p in project_dir.iterdir()] == ["next-session.md"]


def test_a_second_writer_cannot_enter_the_section_while_one_is_open(
        env, monkeypatch):
    """The interleaving itself, driven from two threads.

    The first handoff stops between preserving and publishing. The second must
    not get past the lock in that window; if it did, the first would then
    publish over it and the second's context would exist in neither the handoff
    nor `superseded/`.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-13\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-13"
    project_dir.mkdir(parents=True)
    handoff = project_dir / "next-session.md"
    handoff.write_text("PREDECESSOR", encoding="utf-8")

    holding = threading.Event()
    let_go = threading.Event()
    real_write_atomic = ho.write_atomic
    first = {"done": False}

    def paused_write(path, text):
        if not first["done"]:
            first["done"] = True
            holding.set()
            let_go.wait(timeout=10)
        return real_write_atomic(path, text)

    monkeypatch.setattr(ho, "write_atomic", paused_write)

    def run_first():
        ho.main(["--instance", "proj", "--status", "FIRST", "--next", "n",
                 "--project-root", str(env["project"])])

    worker = threading.Thread(target=run_first, daemon=True)
    worker.start()
    assert holding.wait(timeout=10), "first handoff never reached the write"

    second_got_in = threading.Event()
    rerun_got_in = threading.Event()
    contender_failed = []

    def run_second(got_in):
        try:
            with ho.handoff_lock(handoff) as acquired:
                if acquired:
                    got_in.set()
        except BaseException as exc:              # pragma: no cover - reported
            contender_failed.append(exc)

    monkeypatch.setattr(ho, "LOCK_WAIT_SECONDS", 0.3)
    contender = threading.Thread(target=run_second, args=(second_got_in,),
                                 daemon=True)
    contender.start()
    contender.join(timeout=10)
    assert not contender.is_alive(), \
        "the contender is still running, so its verdict is not in yet"
    assert not contender_failed, \
        f"the contender never reached the lock: {contender_failed!r}"
    assert not second_got_in.is_set(), \
        "a second handoff entered the critical section while one was open"

    let_go.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "the first handoff never finished"

    # The control, through the same call: an unset event is also what a
    # contender that crashed on the way to the lock leaves behind, and what a
    # thread that never ran leaves behind. Re-running the identical body with
    # the section now free must set it. Its own event, because the one above
    # could be set late by the first contender, and a positive the subject's
    # own failure can author is not a control.
    rerun = threading.Thread(target=run_second, args=(rerun_got_in,),
                             daemon=True)
    rerun.start()
    rerun.join(timeout=10)
    assert not rerun.is_alive(), "the re-run contender is still running"
    assert not contender_failed, \
        f"the contender never reached the lock: {contender_failed!r}"
    assert rerun_got_in.is_set(), \
        "the contender cannot take the lock even when it is free, so its " \
        "earlier failure to take it proves nothing"

    assert "FIRST" in handoff.read_text(encoding="utf-8")
    archived = [p.read_text(encoding="utf-8")
                for p in (project_dir / ho.SUPERSEDED_DIRNAME).iterdir()]
    assert any("PREDECESSOR" in text for text in archived)


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


def test_handoff_does_not_destroy_an_unread_predecessor(env, monkeypatch):
    """The property, stated without reference to how it is achieved.

    Every other test here names `preserve_prior_handoff` or
    `SUPERSEDED_DIRNAME`, so against the code before the fix they all fail with
    `AttributeError` -- which proves only that a symbol is new. This one talks
    to `main` alone and asks the question that actually matters: after a second
    handoff, is the first one still somewhere? Before the fix it is not, and
    this fails on the behaviour rather than on a missing name.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-10\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-10"
    project_dir.mkdir(parents=True)
    handoff = project_dir / "next-session.md"
    handoff.write_text("# Session Handoff\n\nthe first agent's context",
                       encoding="utf-8")

    assert ho.main([
        "--instance", "proj",
        "--status", "the second agent's context",
        "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    assert "the second agent's context" in handoff.read_text(encoding="utf-8")
    surviving = [p.read_text(encoding="utf-8", errors="replace")
                 for p in project_dir.rglob("*") if p.is_file()]
    assert any("the first agent's context" in text for text in surviving), \
        "the unread handoff was destroyed"


def test_handoff_preserves_an_unread_predecessor(env, monkeypatch, capsys):
    """Two handoffs, no reader in between -- and neither one is lost."""
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-7\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-7"
    project_dir.mkdir(parents=True)
    handoff = project_dir / "next-session.md"
    handoff.write_text("# Session Handoff\n\nthe first agent's context",
                       encoding="utf-8")

    rc = ho.main([
        "--instance", "proj",
        "--status", "the second agent's context",
        "--next", "n",
        "--project-root", str(env["project"]),
    ])

    assert rc == 0
    assert "the second agent's context" in handoff.read_text(encoding="utf-8")
    archives = list((project_dir / ho.SUPERSEDED_DIRNAME).iterdir())
    assert len(archives) == 1
    assert "the first agent's context" in archives[0].read_text(encoding="utf-8")
    assert "had not been read" in capsys.readouterr().err


def test_handoff_leaves_no_archive_when_nothing_was_waiting(env, monkeypatch):
    """The common path stays clean -- no directory, no warning, no noise.

    Named nothing from the fix, so it holds before and after it: an archiver
    that fired unconditionally would break this while passing every test that
    asserts preservation.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-8\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)

    assert ho.main([
        "--instance", "proj", "--status", "s", "--next", "n",
        "--project-root", str(env["project"]),
    ]) == 0

    project_dir = env["home"] / ".copilot" / "projects" / "guid-8"
    assert [p.name for p in project_dir.iterdir()] == ["next-session.md"]


def test_no_handoff_is_lost_when_several_pile_up_unread(env, monkeypatch):
    """Four handoffs, nobody reading, and all four are still there afterwards.

    The guarantee is append-only, and one supersession cannot show that: a
    preserve step that archived the predecessor and then cleared the archives
    it found from *earlier* rounds would pass every single-round test here
    while losing everything but the most recent pair. The claim documented in
    ``docs/operator.md`` and in the deployed instructions -- that nothing ever
    prunes this directory, so agents must not either -- is a promise about
    repetition, so it is checked by repeating.

    Named after the property rather than the mechanism, and it reads the whole
    project directory rather than ``SUPERSEDED_DIRNAME``, so it stays honest if
    the archive ever moves: the question is whether the context still exists,
    not where it was filed.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-11\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-11"
    project_dir.mkdir(parents=True)

    contexts = [f"context of agent {n}" for n in range(4)]
    for context in contexts:
        assert ho.main([
            "--instance", "proj",
            "--status", context,
            "--next", "n",
            "--project-root", str(env["project"]),
        ]) == 0

    surviving = [p.read_text(encoding="utf-8", errors="replace")
                 for p in project_dir.rglob("*") if p.is_file()]
    for context in contexts:
        assert any(context in text for text in surviving), \
            f"{context!r} was pruned: the archive is not append-only"


def test_handoff_refuses_rather_than_destroying_what_it_cannot_preserve(
        env, monkeypatch):
    """No archive, no overwrite. The predecessor survives a failed handoff.

    The mock denies any *new* directory under the project dir rather than one
    named by the fix, so before the fix it simply never fires -- and the
    assertion then fails because the handoff was overwritten, which is the
    behaviour under test.
    """
    env["catalog"].write_text(
        f'"{env["project"].resolve()}",guid-9\n', encoding="utf-8")
    monkeypatch.setattr(ho.Mux, "available", lambda self: False)
    project_dir = env["home"] / ".copilot" / "projects" / "guid-9"
    project_dir.mkdir(parents=True)
    handoff = project_dir / "next-session.md"
    handoff.write_text("PRECIOUS", encoding="utf-8")

    real_mkdir = Path.mkdir

    def refuse(self, *a, **kw):
        if self != project_dir and project_dir in self.parents:
            raise PermissionError(13, "denied")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", refuse)
    with pytest.raises(SystemExit):
        ho.main([
            "--instance", "proj", "--status", "s", "--next", "n",
            "--project-root", str(env["project"]),
        ])
    assert handoff.read_text(encoding="utf-8") == "PRECIOUS"
