"""Tests for the install manifest: hashing, classification, and upgrades."""
from __future__ import annotations

import json

import pytest

import install_manifest as im


# ── hashing ──────────────────────────────────────────────────────
def test_file_digest_matches_known_sha256(tmp_path):
    """The digest must be plain SHA-256 so a human can verify it with
    Get-FileHash or sha256sum."""
    path = tmp_path / "f.txt"
    path.write_bytes(b"abc")
    expected = ("ba7816bf8f01cfea414140de5dae2223"
                "b00361a396177a9cb410ff61f20015ad")
    assert im.file_digest(path) == expected


def test_file_digest_of_missing_file_is_none(tmp_path):
    assert im.file_digest(tmp_path / "nope") is None


def test_tree_digest_is_stable_across_identical_trees(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "sub").mkdir(parents=True)
        (root / "one.md").write_text("1", encoding="utf-8")
        (root / "sub" / "two.md").write_text("2", encoding="utf-8")
    assert im.tree_digest(a) == im.tree_digest(b)


def test_tree_digest_changes_when_content_changes(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    (root / "a.md").write_text("one", encoding="utf-8")
    before = im.tree_digest(root)
    (root / "a.md").write_text("two", encoding="utf-8")
    assert im.tree_digest(root) != before


def test_tree_digest_changes_when_a_file_is_renamed(tmp_path):
    """Paths are folded into the hash, so a rename is a change even when the
    bytes are identical."""
    root = tmp_path / "s"
    root.mkdir()
    (root / "a.md").write_text("same", encoding="utf-8")
    before = im.tree_digest(root)
    (root / "a.md").rename(root / "b.md")
    assert im.tree_digest(root) != before


def test_tree_digest_notices_an_added_file(tmp_path):
    root = tmp_path / "s"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    before = im.tree_digest(root)
    (root / "b.md").write_text("y", encoding="utf-8")
    assert im.tree_digest(root) != before


def test_tree_digest_of_a_non_directory_is_none(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("x", encoding="utf-8")
    assert im.tree_digest(path) is None


def test_digest_for_dispatches_on_type(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    d = tmp_path / "d"
    d.mkdir()
    (d / "f.txt").write_text("x", encoding="utf-8")
    assert im.digest_for(f) == im.file_digest(f)
    assert im.digest_for(d) == im.tree_digest(d)
    assert im.digest_for(tmp_path / "missing") is None


# ── version ordering ─────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("1.0.0", (1, 0, 0)),
    ("1.10.2", (1, 10, 2)),
    ("2", (2,)),
    ("1.2.3rc1", (1, 2, 3)),
])
def test_parse_version(text, expected):
    assert im.parse_version(text) == expected


def test_unknown_version_sorts_below_everything():
    """A machine with no manifest predates every release, so it must compare
    as older or its upgrades would be skipped."""
    assert im.parse_version(None) == (0,)
    assert im.is_older(None, "1.0.0") is True
    assert im.is_older("garbage", "0.0.1") is True


def test_version_ordering_is_numeric_not_lexical():
    assert im.is_older("1.9.0", "1.10.0") is True
    assert im.is_older("1.10.0", "1.9.0") is False


# ── manifest file ────────────────────────────────────────────────
def test_save_then_load_round_trips(tmp_path):
    manifest = im.empty_manifest()
    manifest["package_version"] = "1.1.0"
    im.record(manifest, "templates/x.md", tmp_path / "x.md",
              kind="template", digest="abc")
    im.save(tmp_path, manifest)
    loaded = im.load(tmp_path)
    assert loaded["package_version"] == "1.1.0"
    assert loaded["artifacts"]["templates/x.md"]["sha256"] == "abc"


def test_save_stamps_updated_at(tmp_path):
    manifest = im.empty_manifest()
    im.save(tmp_path, manifest)
    assert im.load(tmp_path)["updated_at"]


def test_missing_manifest_loads_as_empty(tmp_path):
    assert im.load(tmp_path)["artifacts"] == {}
    assert im.load(tmp_path)["package_version"] is None


def test_corrupt_manifest_loads_as_empty(tmp_path):
    """Losing the record must cost prompts, never data: an unreadable manifest
    degrades to the same conservative behaviour as no manifest at all."""
    im.manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert im.load(tmp_path) == im.empty_manifest()


def test_manifest_holding_a_json_list_loads_as_empty(tmp_path):
    im.manifest_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    assert im.load(tmp_path) == im.empty_manifest()


def test_manifest_with_wrong_field_types_is_repaired(tmp_path):
    im.manifest_path(tmp_path).write_text(
        json.dumps({"artifacts": "nope", "tools": 5}), encoding="utf-8")
    loaded = im.load(tmp_path)
    assert loaded["artifacts"] == {}
    assert loaded["tools"] == {}


def test_save_creates_the_directory(tmp_path):
    home = tmp_path / "deep" / "operator"
    im.save(home, im.empty_manifest())
    assert im.manifest_path(home).is_file()


def test_manifest_lives_outside_the_copilot_directory(tmp_path):
    """The Copilot CLI owns ~/.copilot and deletes subdirectories there on
    startup, so the record of what we installed is kept in the operator home."""
    assert im.manifest_path(tmp_path).parent == tmp_path
    assert ".copilot" not in im.MANIFEST_NAME


# ── classification ───────────────────────────────────────────────
@pytest.fixture()
def pair(tmp_path):
    source = tmp_path / "src.md"
    source.write_text("v1", encoding="utf-8")
    dest = tmp_path / "dest.md"
    return source, dest


def test_absent_when_nothing_is_deployed(pair):
    source, dest = pair
    assert im.classify(im.empty_manifest(), "k", dest,
                       im.file_digest(source)) == im.ABSENT


def test_current_when_deployed_matches_source(pair):
    source, dest = pair
    dest.write_text("v1", encoding="utf-8")
    assert im.classify(im.empty_manifest(), "k", dest,
                       im.file_digest(source)) == im.CURRENT


def test_untracked_when_it_differs_and_we_never_recorded_it(pair):
    source, dest = pair
    dest.write_text("something else", encoding="utf-8")
    assert im.classify(im.empty_manifest(), "k", dest,
                       im.file_digest(source)) == im.UNTRACKED


def test_stale_when_deployed_matches_what_we_wrote(pair):
    """The repository moved on but the user never touched their copy."""
    source, dest = pair
    dest.write_text("v1", encoding="utf-8")
    manifest = im.empty_manifest()
    im.record(manifest, "k", dest, kind="template", digest=im.file_digest(dest))
    source.write_text("v2", encoding="utf-8")
    assert im.classify(manifest, "k", dest, im.file_digest(source)) == im.STALE


def test_modified_when_deployed_differs_from_what_we_wrote(pair):
    source, dest = pair
    dest.write_text("v1", encoding="utf-8")
    manifest = im.empty_manifest()
    im.record(manifest, "k", dest, kind="template", digest=im.file_digest(dest))
    dest.write_text("MY EDITS", encoding="utf-8")
    source.write_text("v2", encoding="utf-8")
    assert im.classify(manifest, "k", dest, im.file_digest(source)) == im.MODIFIED


def test_a_linked_artifact_is_always_current(tmp_path):
    """A junction points at the repository, so it has no content of its own to
    compare and can never drift."""
    dest = tmp_path / "ext"
    dest.mkdir()
    manifest = im.empty_manifest()
    im.record(manifest, "extensions/x", dest, kind="extension",
              digest=None, linked=True)
    assert im.classify(manifest, "extensions/x", dest, "whatever") == im.CURRENT


def test_only_stale_may_be_overwritten_silently():
    assert im.may_overwrite(im.STALE) is True
    for state in (im.MODIFIED, im.UNTRACKED, im.ABSENT, im.CURRENT):
        assert im.may_overwrite(state) is False


def test_every_state_has_a_description():
    for state in (im.ABSENT, im.CURRENT, im.STALE, im.MODIFIED, im.UNTRACKED):
        assert im.describe(state) != state


# ── upgrade strategies ───────────────────────────────────────────
def _namespace(calls):
    def upgrade_v1_0_0_to_v1_1_0(ctx):
        calls.append("1.1.0")

    def upgrade_v1_1_0_to_v1_2_0(ctx):
        calls.append("1.2.0")

    def upgrade_v1_2_0_to_v2_0_0(ctx):
        calls.append("2.0.0")

    def not_an_upgrade(ctx):
        calls.append("nope")

    return locals()


def test_discovery_reads_the_transition_from_the_name():
    found = im.discover_migrations(_namespace([]))
    assert [(a, b) for a, b, _ in found] == [
        ("1.0.0", "1.1.0"), ("1.1.0", "1.2.0"), ("1.2.0", "2.0.0")]


def test_functions_not_named_for_a_transition_are_ignored():
    names = [f.__name__ for _a, _b, f in im.discover_migrations(_namespace([]))]
    assert "not_an_upgrade" not in names


def test_only_migrations_above_the_installed_version_are_pending():
    pending = im.pending_migrations("1.1.0", "2.0.0", _namespace([]))
    assert [b for _a, b, _f in pending] == ["1.2.0", "2.0.0"]


def test_migrations_above_the_target_version_are_not_run():
    pending = im.pending_migrations("1.0.0", "1.2.0", _namespace([]))
    assert [b for _a, b, _f in pending] == ["1.1.0", "1.2.0"]


def test_a_multi_version_jump_runs_every_step_in_order():
    calls = []
    ctx = im.MigrationContext(**_ctx_kwargs(from_version="1.0.0", to_version="2.0.0"))
    im.run_migrations(ctx, _namespace(calls))
    assert calls == ["1.1.0", "1.2.0", "2.0.0"]


def test_an_unknown_installed_version_runs_everything():
    """A machine that predates the manifest is exactly the machine with old
    state to migrate, so it must not be treated as up to date."""
    pending = im.pending_migrations(None, "2.0.0", _namespace([]))
    assert len(pending) == 3


def test_nothing_is_pending_when_already_current():
    assert im.pending_migrations("2.0.0", "2.0.0", _namespace([])) == []


def _ctx_kwargs(**overrides):
    from pathlib import Path
    kwargs = dict(
        copilot_dir=Path("."), operator_home=Path("."), repo_root=Path("."),
        manifest=im.empty_manifest(), from_version="1.0.0", to_version="2.0.0",
        log=lambda _m: None,
    )
    kwargs.update(overrides)
    return kwargs


def test_a_failing_migration_does_not_stop_the_others():
    """Aborting halfway would leave the machine worse off than before, so a bad
    upgrade is reported and skipped."""
    calls = []

    def upgrade_v1_0_0_to_v1_1_0(ctx):
        raise RuntimeError("boom")

    def upgrade_v1_1_0_to_v1_2_0(ctx):
        calls.append("1.2.0")

    ctx = im.MigrationContext(**_ctx_kwargs())
    ran = im.run_migrations(ctx, locals())
    assert calls == ["1.2.0"]
    assert ran == ["upgrade_v1_1_0_to_v1_2_0"]
    assert any("failed" in note for note in ctx.notes)


def test_a_migration_receives_the_paths_it_needs(tmp_path):
    seen = {}

    def upgrade_v1_0_0_to_v1_1_0(ctx):
        seen["copilot"] = ctx.copilot_dir
        seen["from"] = ctx.from_version

    ctx = im.MigrationContext(**_ctx_kwargs(copilot_dir=tmp_path))
    im.run_migrations(ctx, locals())
    assert seen == {"copilot": tmp_path, "from": "1.0.0"}


def test_shipped_migrations_are_all_well_named():
    """Guards the naming convention: a typo'd upgrade function would silently
    never run."""
    for source, target, func in im.discover_migrations():
        assert im.parse_version(source) < im.parse_version(target), func.__name__


# ── reporting ────────────────────────────────────────────────────
def test_status_reports_each_artifact(tmp_path):
    source = tmp_path / "src.md"
    source.write_text("v2", encoding="utf-8")
    dest = tmp_path / "dest.md"
    dest.write_text("v1", encoding="utf-8")
    manifest = im.empty_manifest()
    im.record(manifest, "templates/src.md", dest, kind="template",
              digest=im.file_digest(dest), version="1.0.0")
    report = im.status(manifest, [("templates/src.md", "template", source, dest)])
    assert report[0].state == im.STALE
    assert report[0].installed_version == "1.0.0"


def test_needs_update_is_false_when_everything_is_current(tmp_path):
    source = tmp_path / "src.md"
    source.write_text("v1", encoding="utf-8")
    dest = tmp_path / "dest.md"
    dest.write_text("v1", encoding="utf-8")
    report = im.status(im.empty_manifest(),
                       [("k", "template", source, dest)])
    assert im.needs_update(report) is False


def test_needs_update_is_true_when_something_is_missing(tmp_path):
    source = tmp_path / "src.md"
    source.write_text("v1", encoding="utf-8")
    report = im.status(im.empty_manifest(),
                       [("k", "template", source, tmp_path / "gone.md")])
    assert im.needs_update(report) is True
