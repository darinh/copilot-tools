"""Conformance rules for ``backlog/``.

A backlog nothing reads decays exactly like any other prose, so every rule
lives in :func:`backlog_tool.check` and this module runs it against the real
directory. The rules themselves are not restated here -- a test that spells its
own copy of a rule stops testing the rule the moment the two disagree, which is
the failure this repository has already paid for once in
``test_git_identity.py`` (a duplicated workflow glob let a ``.yaml`` file
escape every assertion while all of them stayed green).

What *is* here is a control for every rule, in both directions:

* a **positive control** that violates the rule and asserts it is reported --
  a detector that cannot fire reports a clean tree, which reads exactly like
  success; and
* a **negative control** that asserts the correct spelling still passes --
  without which "reject everything" would score full marks.

Every positive control mutates a known-good document through :func:`mutate`,
which **asserts the text actually changed**. A ``str.replace`` whose pattern
does not match returns the input unaltered, so a mutation test written against
a stale literal would validate a pristine file and pass -- reporting a working
guard where there is none. That assertion is the difference between proving a
rule fires and assuming it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

import backlog_tool
import project_features
from backlog_tool import (
    ACTIVE_STATUSES,
    APPROVED_STATUSES,
    BACKLOG_DIRNAME,
    COMMIT_REQUIRED_STATUSES,
    EVIDENCE_HEADING,
    ITEM_FILENAME,
    KNOWN_FIELDS,
    NO_SPEC,
    OPEN_STATUS,
    PROPOSED_STATUS,
    REQUIRED_FIELDS,
    STATUSES,
    TERMINAL_STATUSES,
)

#: The working tree under test. ``checkout_root`` rather than
#: ``primary_repo_root``: on a feature branch the backlog that matters is this
#: branch's, not whatever ``main`` happens to hold.
REPO = backlog_tool.checkout_root(Path(__file__).resolve().parent)

ITEM_NAME = "0001-a-known-good-item.md"

#: A minimal document that satisfies every rule. Each positive control below
#: breaks exactly one thing about it, so a failure names the rule it broke.
GOOD = """---
id: 1
title: A known good item
status: open
opened: 2026-08-04
spec: none
---

## Evidence

Measured on 2026-08-04, and reproducible.

## Why it matters

It is the fixture every control in this module mutates.
"""


def mutate(text: str, old: str, new: str) -> str:
    """``text`` with ``old`` replaced by ``new``, proven to have changed.

    The assertion is the entire point. ``str.replace`` is silent when its
    pattern is absent, so a control whose literal has drifted out of date
    quietly feeds the *unmutated* document to the checker, watches it pass, and
    reports that as the rule being enforced.
    """
    out = text.replace(old, new)
    assert out != text, (
        f"mutation {old!r} -> {new!r} changed nothing; this control would "
        "have validated an unmutated document and passed regardless of the rule")
    return out


def write_backlog(root: Path, text: str = GOOD, name: str = ITEM_NAME,
                  extra: "dict | None" = None) -> Path:
    """Lay out a backlog under ``root`` and confirm it reached the disk."""
    directory = root / BACKLOG_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, (
        f"{path} does not hold what was just written to it")
    for other_name, other_text in (extra or {}).items():
        other = directory / other_name
        other.write_text(other_text, encoding="utf-8")
        assert other.read_text(encoding="utf-8") == other_text
    return path


def problems(root: Path, **kwargs) -> list:
    return backlog_tool.check(root, **kwargs)


def reported(root: Path, needle: str, **kwargs) -> bool:
    return any(needle in p for p in problems(root, **kwargs))


# ---------------------------------------------------------------------------
# The real directory
# ---------------------------------------------------------------------------

def _stand_down_reason(backend: str, source: str) -> "str | None":
    """Why ``backlog/`` should not be demanded of this project, or ``None``.

    A pure function of the two facts, so it can be exercised for every backend
    without arranging a machine to be in that state -- and so the decision
    lives in one place that a control can score.
    """
    if backend == project_features.BACKLOG_FOLDER:
        return None
    return (f"this project's tracked-backlog backend is {backend!r} "
            f"(from {source}), so backlog/ is not where its open work lives")


def _require_folder_backend() -> None:
    """Stand aside for a project that keeps its open work somewhere else.

    ``tracked-backlog`` is a choice of one backend, not a toggle: a project may
    keep open work in ``backlog/``, in GitHub Issues, or nowhere. The three
    assertions below demand a conforming ``backlog/`` directory, and demanding
    one of a project that deliberately chose GitHub Issues would be this
    repository's rules enforced on somebody who declined them.

    The predicate is deliberately one-sided --
    :func:`project_features.tracked_backlog_backend` answers ``folder`` for
    every uncertainty, including no catalog at all, which is what CI has. So
    this can only step aside because a person configured it to, never because
    a file was missing.

    That is not sufficient on its own, and a mutation proved it: inverting the
    comparison in :func:`_stand_down_reason` turns all three assertions into
    skips, and a suite reporting ``59 passed, 4 skipped`` reads exactly like a
    suite reporting ``62 passed, 1 skipped``. A guard that can be switched off
    without going red is not a guard, so
    :func:`test_the_enforcement_is_switched_on_in_this_repository` asserts this
    gate lets *this* repository through, and fails loudly when it does not.
    """
    reason = _stand_down_reason(*project_features.tracked_backlog_backend(REPO))
    if reason:
        pytest.skip(reason)


def test_the_enforcement_is_switched_on_in_this_repository():
    """The premise the three assertions below rest on.

    ``pytest.skip`` raises, and a skip raised inside a test body skips that
    test rather than failing it -- so the outcome is caught and asserted on
    instead. Anything that makes the gate stand down here, an inverted
    comparison or a configuration nobody meant to leave behind, arrives as a
    red test naming the reason rather than as three skips in a count nobody
    reads.
    """
    outcome = []
    try:
        _require_folder_backend()
    except BaseException as exc:          # noqa: BLE001 - Skipped is one of these
        outcome.append(exc)
    assert not outcome, (
        f"the backlog enforcement below has stood down: {outcome[0]}")


@pytest.mark.parametrize("backend", ["github-issues", "none"])
def test_another_backend_stands_the_enforcement_down(backend):
    """Positive control: the gate must be able to open."""
    assert _stand_down_reason(backend, "a source") is not None


def test_the_folder_backend_keeps_the_enforcement_on():
    """Negative control: the gate must not open for everything."""
    assert _stand_down_reason(project_features.BACKLOG_FOLDER, "a source") is None


def test_the_stand_down_reason_names_the_backend_and_where_it_came_from():
    """A guard that disappears silently is worse than one never written."""
    reason = _stand_down_reason("github-issues", "/some/features.json")
    assert "github-issues" in reason and "/some/features.json" in reason


def test_the_repositorys_own_backlog_conforms():
    """The assertion this module exists for. Everything else is a control."""
    _require_folder_backend()
    found = backlog_tool.check(REPO)
    assert found == [], "backlog/ violates its own rules:\n" + "\n".join(found)


def test_the_repository_actually_has_a_backlog_to_check():
    """A premise assertion.

    Every rule below iterates over the items it finds, so an empty directory
    satisfies all of them. Without this, deleting ``backlog/`` would turn the
    test above green rather than red.
    """
    _require_folder_backend()
    items = backlog_tool.item_paths(REPO / BACKLOG_DIRNAME)
    assert items, f"no items under {REPO / BACKLOG_DIRNAME}"


def test_every_item_in_the_repository_names_a_spec_or_says_there_is_none():
    """The spec-kit tie, asserted against the real directory.

    ``check`` enforces this too; asserting it here as well means the mapping
    cannot be quietly abandoned by relaxing one rule in the checker.
    """
    _require_folder_backend()
    items, parse_problems = backlog_tool.load(REPO / BACKLOG_DIRNAME)
    assert not parse_problems, parse_problems
    for item in items:
        spec = item.front.get("spec", "")
        assert spec, f"{item.name}: no spec field"
        if spec != NO_SPEC:
            assert (REPO / spec).is_file(), f"{item.name}: {spec} is missing"


# ---------------------------------------------------------------------------
# R0 -- the subject of every other rule must exist
# ---------------------------------------------------------------------------

def test_a_missing_backlog_directory_is_reported(tmp_path):
    assert reported(tmp_path, "does not exist")


def test_an_empty_backlog_directory_is_reported(tmp_path):
    """The failure that would make every rule below pass vacuously."""
    (tmp_path / BACKLOG_DIRNAME).mkdir()
    assert reported(tmp_path, "would pass vacuously")


def test_a_known_good_backlog_is_not_reported(tmp_path):
    """Negative control for the whole module.

    Without this, a checker that returned a problem for every input would
    satisfy every positive control in this file.
    """
    write_backlog(tmp_path)
    assert problems(tmp_path) == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_documentation_in_the_backlog_directory_is_not_treated_as_an_item(tmp_path):
    write_backlog(tmp_path, extra={"README.md": "# Backlog\n\nnot an item\n"})
    assert problems(tmp_path) == []
    names = [p.name for p in backlog_tool.item_paths(tmp_path / BACKLOG_DIRNAME)]
    assert names == [ITEM_NAME]


def test_the_discovery_pattern_requires_a_four_digit_id():
    """Positive control for the pattern itself.

    A pattern that matched everything would silently pull README.md into the
    item set; one that matched nothing would empty it. Both read as clean.
    """
    assert ITEM_FILENAME.match("0001-a-slug.md")
    assert not ITEM_FILENAME.match("1-a-slug.md")
    assert not ITEM_FILENAME.match("README.md")
    assert not ITEM_FILENAME.match("0001-a-slug.txt")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_a_file_with_no_front_matter_is_reported(tmp_path):
    write_backlog(tmp_path, "## Evidence\n\nno front matter at all\n")
    assert reported(tmp_path, "front-matter delimiter")


def test_an_unterminated_front_matter_block_is_reported(tmp_path):
    """Built by truncation rather than replacement.

    Deleting the closing ``---`` from the full document does not reach this
    rule: the body's ``## Evidence`` line is then read as front matter and
    fails as a malformed field first. Truncating after the fields is what
    actually leaves the block open.
    """
    fields = GOOD.split("---\n", 2)[1]
    text = "---\n" + fields
    assert text != GOOD, "truncation changed nothing"
    write_backlog(tmp_path, text)
    assert reported(tmp_path, "never closed")


def test_a_duplicated_front_matter_key_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "spec: none", "spec: none\nspec: none"))
    assert reported(tmp_path, "duplicate front-matter key")


def test_a_front_matter_line_that_is_not_a_field_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "spec: none", "spec: none\nnonsense"))
    assert reported(tmp_path, "is not 'key: value'")


def test_a_value_keeps_everything_after_the_colon(tmp_path):
    """Negative control for the deliberate absence of comment stripping.

    YAML would read ``title: Fix issue #42`` as ``Fix issue`` and discard the
    number. Item titles name defects, and defects have numbers.
    """
    path = write_backlog(
        tmp_path, mutate(GOOD, "title: A known good item",
                         "title: Fix issue #42 in the parser"))
    item = backlog_tool.parse_item(path)
    assert item.title == "Fix issue #42 in the parser"
    assert problems(tmp_path) == []


# ---------------------------------------------------------------------------
# Fields, ids, dates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_a_missing_required_field_is_reported(tmp_path, field):
    """Driven by the module's own list, so a field added there is covered."""
    line = next(ln for ln in GOOD.splitlines() if ln.startswith(f"{field}:"))
    write_backlog(tmp_path, mutate(GOOD, line + "\n", ""))
    assert reported(tmp_path, f"required field {field!r}")


def test_an_unknown_front_matter_field_is_reported(tmp_path):
    """A typo'd field name would otherwise be accepted in silence."""
    unknown = "stauts"
    assert unknown not in KNOWN_FIELDS
    write_backlog(tmp_path, mutate(GOOD, "spec: none",
                                   f"spec: none\n{unknown}: open"))
    assert reported(tmp_path, "unknown front-matter field")


def test_an_id_that_disagrees_with_the_filename_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "id: 1", "id: 2"))
    assert reported(tmp_path, "does not match the filename id")


def test_an_id_that_is_not_a_number_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "id: 1", "id: one"))
    assert reported(tmp_path, "is not an integer")


def test_a_duplicate_id_is_reported(tmp_path):
    """Two files, same id. Neither is wrong on its own."""
    write_backlog(tmp_path)
    second = mutate(GOOD, "title: A known good item", "title: A colliding item")
    (tmp_path / BACKLOG_DIRNAME / "0001-a-colliding-item.md").write_text(
        second, encoding="utf-8")
    assert reported(tmp_path, "is already used by")


def test_a_malformed_date_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "opened: 2026-08-04",
                                   "opened: 4th August"))
    assert reported(tmp_path, "is not a YYYY-MM-DD date")


# ---------------------------------------------------------------------------
# Status vocabulary -- read from one place, never respelled here
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", STATUSES)
def test_every_legal_status_is_accepted(tmp_path, status):
    """Negative control for the vocabulary.

    Parametrised over ``STATUSES`` rather than a list written out here: a copy
    of the vocabulary in this file would keep passing after the real one
    changed, which is how a rule quietly stops covering a value.
    """
    if status in COMMIT_REQUIRED_STATUSES:
        pytest.skip("covered by the git-backed commit controls below")
    text = GOOD if status == OPEN_STATUS else mutate(
        GOOD, f"status: {OPEN_STATUS}", f"status: {status}")
    if status in TERMINAL_STATUSES:
        text = mutate(text, "opened: 2026-08-04",
                      "opened: 2026-08-04\nclosed: 2026-08-05")
    write_backlog(tmp_path, text)
    assert problems(tmp_path) == []


def test_a_status_outside_the_vocabulary_is_reported(tmp_path):
    invented = "wontfix"
    assert invented not in STATUSES, "pick a status the vocabulary rejects"
    write_backlog(tmp_path, mutate(GOOD, f"status: {OPEN_STATUS}",
                                   f"status: {invented}"))
    assert reported(tmp_path, "is not one of")


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def test_an_item_with_no_evidence_section_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, EVIDENCE_HEADING, "## Background"))
    assert reported(tmp_path, "section is missing or empty")


def test_an_item_with_an_empty_evidence_section_is_reported(tmp_path):
    """Present but blank is the same as absent: neither is evidence."""
    write_backlog(tmp_path, mutate(
        GOOD, "## Evidence\n\nMeasured on 2026-08-04, and reproducible.\n",
        "## Evidence\n\n"))
    assert reported(tmp_path, "section is missing or empty")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_a_terminal_item_with_no_closing_date_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, f"status: {OPEN_STATUS}",
                                   "status: rejected"))
    assert reported(tmp_path, "no 'closed' date is set")


def test_an_open_item_carrying_a_closing_date_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "opened: 2026-08-04",
                                   "opened: 2026-08-04\nclosed: 2026-08-05"))
    assert reported(tmp_path, "but a 'closed' date is set")


def test_an_open_item_carrying_a_commit_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "opened: 2026-08-04",
                                   "opened: 2026-08-04\ncommit: " + "0" * 40))
    assert reported(tmp_path, "but a 'commit' is set")


def test_a_rejected_item_needs_no_commit(tmp_path):
    """Negative control. Nothing shipped, so demanding a SHA would force
    whoever rejects an item to invent one."""
    assert "rejected" not in COMMIT_REQUIRED_STATUSES
    text = mutate(GOOD, f"status: {OPEN_STATUS}", "status: rejected")
    text = mutate(text, "opened: 2026-08-04",
                  "opened: 2026-08-04\nclosed: 2026-08-05")
    write_backlog(tmp_path, text)
    assert problems(tmp_path) == []


# ---------------------------------------------------------------------------
# Closing commits -- against a real repository, in both directions
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def git_repo(tmp_path_factory):
    """A throwaway repository with one commit, for the commit-resolution rule.

    A temp directory that is *not* a repository would make ``git cat-file``
    fail for every SHA, so the rule would appear to fire for the wrong reason
    and a genuinely resolvable SHA would never be tested.
    """
    root = tmp_path_factory.mktemp("backlog-git")
    env = ["-c", "user.email=test@example.invalid", "-c", "user.name=test"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                   capture_output=True, encoding="utf-8", errors="replace")
    subprocess.run(["git", *env, "commit", "-q", "--allow-empty", "-m", "seed"],
                   cwd=root, check=True, capture_output=True,
                   encoding="utf-8", errors="replace")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, encoding="utf-8",
                          errors="replace").stdout.strip()
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root,
                          check=True, capture_output=True, encoding="utf-8",
                          errors="replace").stdout.strip()
    assert len(head) == 40 and len(tree) == 40 and head != tree
    return root, head, tree


def _closed_item(sha: str) -> str:
    text = mutate(GOOD, f"status: {OPEN_STATUS}", "status: closed")
    return mutate(text, "opened: 2026-08-04",
                  f"opened: 2026-08-04\nclosed: 2026-08-05\ncommit: {sha}")


def test_a_closed_item_naming_a_real_commit_is_accepted(git_repo):
    """Negative control: the rule must not reject a correct close."""
    root, head, _ = git_repo
    write_backlog(root, _closed_item(head))
    assert problems(root) == []


def test_a_closed_item_with_no_commit_is_reported(git_repo):
    root, head, _ = git_repo
    text = mutate(_closed_item(head), f"commit: {head}\n", "")
    write_backlog(root, text)
    assert reported(root, "no 'commit' is named")


def test_a_closed_item_naming_a_commit_that_does_not_exist_is_reported(git_repo):
    root, head, _ = git_repo
    write_backlog(root, _closed_item("0" * 40))
    assert reported(root, "does not resolve to a commit")


def test_a_closed_item_naming_a_tree_instead_of_a_commit_is_reported(git_repo):
    """Proves the ``^{commit}`` suffix in the resolver is load-bearing.

    ``git cat-file -e`` alone succeeds for any object that exists, so without
    the suffix a tree or blob SHA would certify a close -- a close pointing at
    something that is not a commit, reported as verified.
    """
    root, _, tree = git_repo
    write_backlog(root, _closed_item(tree))
    assert reported(root, "does not resolve to a commit")


# ---------------------------------------------------------------------------
# The spec-kit mapping
# ---------------------------------------------------------------------------

def _with_spec(tmp_path: Path, spec_body: str, spec_line: str,
               requirement: "str | None" = None) -> None:
    spec = tmp_path / "specs" / "007-a-feature" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(spec_body, encoding="utf-8")
    text = mutate(GOOD, "spec: none", spec_line)
    if requirement is not None:
        text = mutate(text, spec_line, f"{spec_line}\nrequirement: {requirement}")
    write_backlog(tmp_path, text)


SPEC_BODY = "# Feature\n\n### User Story 4 - Something observable\n\nProse.\n"
SPEC_LINE = "spec: specs/007-a-feature/spec.md"


def test_an_item_naming_an_existing_spec_is_accepted(tmp_path):
    _with_spec(tmp_path, SPEC_BODY, SPEC_LINE)
    assert problems(tmp_path) == []


def test_an_item_naming_a_spec_that_does_not_exist_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(GOOD, "spec: none",
                                   "spec: specs/999-not-here/spec.md"))
    assert reported(tmp_path, "does not exist at")


def test_a_spec_path_outside_the_specs_directory_is_reported(tmp_path):
    """``none`` is the way to say there is no spec, not an arbitrary path."""
    (tmp_path / "notes.md").write_text("# notes\n", encoding="utf-8")
    write_backlog(tmp_path, mutate(GOOD, "spec: none", "spec: notes.md"))
    assert reported(tmp_path, f"is not a path under")


def test_a_requirement_present_in_its_spec_is_accepted(tmp_path):
    _with_spec(tmp_path, SPEC_BODY, SPEC_LINE,
               requirement="User Story 4 - Something observable")
    assert problems(tmp_path) == []


def test_a_requirement_absent_from_its_spec_is_reported(tmp_path):
    """The rule that makes the mapping load-bearing.

    Rename or delete a requirement and the item pointing at it goes red, so
    somebody has to look at the item again. Without this the ``spec`` field is
    decorative: it would keep naming a document while saying nothing true
    about it.
    """
    _with_spec(tmp_path, SPEC_BODY, SPEC_LINE,
               requirement="User Story 9 - Renamed away")
    assert reported(tmp_path, "does not appear in")


def test_a_requirement_with_no_spec_to_check_it_against_is_reported(tmp_path):
    write_backlog(tmp_path, mutate(
        GOOD, "spec: none", "spec: none\nrequirement: something"))
    assert reported(tmp_path, "there is nothing to check it against")


# ---------------------------------------------------------------------------
# The rendered view
# ---------------------------------------------------------------------------

def test_the_html_view_embeds_its_data_rather_than_fetching_it(tmp_path):
    """The page must work from ``file://``.

    Browsers treat every ``file://`` document as an opaque origin, so a
    ``fetch`` of a sibling file is refused by CORS and the page renders an
    empty backlog with no error a reader would see -- a viewer that silently
    shows nothing is worse than no viewer.
    """
    write_backlog(tmp_path)
    page = backlog_tool.render_html(tmp_path, resolve_commits=False)
    assert "fetch(" not in page
    assert "XMLHttpRequest" not in page
    assert "A known good item" in page


def test_the_html_view_reports_conformance_problems(tmp_path):
    """The viewer must not render a broken backlog as though it were fine."""
    write_backlog(tmp_path, mutate(GOOD, "id: 1", "id: 2"))
    page = backlog_tool.render_html(tmp_path, resolve_commits=False)
    payload = json.loads(_embedded_json(page))
    assert payload["problems"], "a malformed backlog rendered with no problems"


def test_an_item_title_cannot_break_out_of_the_embedded_data(tmp_path):
    """``</script>`` in a title ends the block as far as HTML is concerned.

    JSON escaping alone does not prevent this: the sequence is ordinary text to
    a JSON encoder. If it survived into the page, an item title would be able
    to inject markup into the viewer.
    """
    hostile = "</script><img src=x onerror=alert(1)>"
    write_backlog(tmp_path, mutate(GOOD, "title: A known good item",
                                   f"title: {hostile}"))
    page = backlog_tool.render_html(tmp_path, resolve_commits=False)
    assert "</script><img" not in page
    payload = json.loads(_embedded_json(page))
    assert payload["items"][0]["title"] == hostile, (
        "escaping must be reversible: the title has to survive intact")


def _embedded_json(page: str) -> str:
    match = re.search(
        r'<script type="application/json" id="data">(.*?)</script>',
        page, re.S)
    assert match, "the page no longer carries an embedded data block"
    return match.group(1)


# ---------------------------------------------------------------------------
# The approval gate -- the vocabulary that expresses it
# ---------------------------------------------------------------------------

def test_the_vocabulary_can_express_filed_but_not_approved():
    """A premise, not a rule.

    The gate is "an agent works only what the product owner approved". That
    sentence is unenforceable until the data can tell "somebody wrote this
    down" apart from "somebody decided it should be built", so every control
    below rests on this value existing and meaning neither approved nor
    finished.
    """
    assert PROPOSED_STATUS in STATUSES
    assert PROPOSED_STATUS not in APPROVED_STATUSES
    assert PROPOSED_STATUS not in TERMINAL_STATUSES
    assert OPEN_STATUS in APPROVED_STATUSES
    assert set(ACTIVE_STATUSES) | set(TERMINAL_STATUSES) == set(STATUSES), (
        "every status must be either live or terminal; one that is neither "
        "falls through R7 entirely and can carry any combination of fields")


def _proposed(text: str = GOOD) -> str:
    return mutate(text, f"status: {OPEN_STATUS}", f"status: {PROPOSED_STATUS}")


def _with_blocks(text: str, target: "int | str") -> str:
    return mutate(text, "spec: none", f"blocks: {target}\nspec: none")


def _status_variant(status: str, text: str = GOOD) -> str:
    """``text`` re-stated with ``status``, plus the fields that status needs."""
    out = mutate(text, f"status: {OPEN_STATUS}", f"status: {status}")
    if status in TERMINAL_STATUSES:
        out = mutate(out, "opened: 2026-08-04",
                     "opened: 2026-08-04\nclosed: 2026-08-05")
    return out


def _other(item_id: int, slug: str, status: str = OPEN_STATUS) -> "tuple":
    """A second well-formed item, as ``(filename, text)``.

    Refuses id 1 rather than quietly producing a no-op mutation: ``GOOD``
    already carries it, so ``mutate`` would have nothing to change and the
    caller would be handed a duplicate of the fixture under a second name.
    """
    assert item_id != 1, "id 1 is the fixture's; a second item needs its own"
    text = mutate(GOOD, "id: 1", f"id: {item_id}")
    text = mutate(text, "title: A known good item", f"title: Item {item_id}")
    if status != OPEN_STATUS:
        text = _status_variant(status, text)
    return f"{item_id:04d}-{slug}.md", text


def _loaded(root: Path) -> list:
    items, parse_problems = backlog_tool.load(root / BACKLOG_DIRNAME)
    assert not parse_problems, parse_problems
    return items


def _reason(root: Path, item_id: int) -> "str | None":
    items = _loaded(root)
    index = backlog_tool.by_filename_id(items)
    item = index[item_id]
    return backlog_tool.why_not_workable(item, index)


def test_an_approved_item_is_workable(tmp_path):
    """Negative control for the gate.

    Without it, a gate that refused everything would satisfy every positive
    control below while making the queue permanently empty -- which reads as a
    quiet backlog rather than a broken one.
    """
    write_backlog(tmp_path)
    assert _reason(tmp_path, 1) is None
    assert [i.filename_id for i in backlog_tool.workable(_loaded(tmp_path))] == [1]


def test_an_item_awaiting_approval_is_not_workable(tmp_path):
    write_backlog(tmp_path, _proposed())
    reason = _reason(tmp_path, 1)
    assert reason and "awaiting approval" in reason, reason
    assert backlog_tool.workable(_loaded(tmp_path)) == []


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_a_finished_item_is_not_workable(tmp_path, status):
    write_backlog(tmp_path, _status_variant(status))
    reason = _reason(tmp_path, 1)
    assert reason and status in reason, reason


def test_the_escape_hatch_makes_a_blocking_item_workable(tmp_path):
    """The lawful move for an agent that finds a defect mid-task.

    This is the negative control for R12 below, and the more important half of
    the pair: a gate with no legal way through does not stop an agent, it
    makes the agent approve its own item and carry on -- which repeals the
    gate while appearing to honour it, and leaves nothing behind that says so.
    """
    blocker, blocker_text = _other(2, "a-blocking-defect")
    blocker_text = _with_blocks(_proposed(blocker_text), 1)
    write_backlog(tmp_path, extra={blocker: blocker_text})
    assert problems(tmp_path) == []
    assert _reason(tmp_path, 2) is None
    assert sorted(i.filename_id
                  for i in backlog_tool.workable(_loaded(tmp_path))) == [1, 2]


def test_the_escape_hatch_cannot_be_bootstrapped_from_an_unapproved_item(tmp_path):
    """Two moves would otherwise repeal the gate: file A, then file B
    blocking A, and B is workable on an authority A never had."""
    write_backlog(tmp_path, _proposed())
    blocker, blocker_text = _other(2, "a-blocking-defect")
    write_backlog(tmp_path, _proposed(),
                  extra={blocker: _with_blocks(_proposed(blocker_text), 1)})
    reason = _reason(tmp_path, 2)
    assert reason and "rather than approved" in reason, reason
    assert backlog_tool.workable(_loaded(tmp_path)) == []


def test_the_escape_hatch_expires_when_the_item_it_blocks_is_finished(tmp_path):
    """Nothing is being unblocked once the blocked item is closed, so the
    exception lapses and the item goes back to needing approval."""
    finished_text = _status_variant("rejected")
    blocker, blocker_text = _other(2, "a-blocking-defect")
    write_backlog(tmp_path, finished_text,
                  extra={blocker: _with_blocks(_proposed(blocker_text), 1)})
    reason = _reason(tmp_path, 2)
    assert reason and "#1" in reason and "rejected" in reason, reason


def test_the_refusal_says_which_item_it_names(tmp_path):
    """Assert the reason, not the boolean.

    A refusal is the most multiplexed answer this function has -- five
    different situations produce one -- and a test that accepted any of them
    would pass with the gate wired to the wrong field.
    """
    blocker, blocker_text = _other(2, "a-blocking-defect")
    write_backlog(tmp_path, GOOD,
                  extra={blocker: _with_blocks(_proposed(blocker_text), 9)})
    reason = _reason(tmp_path, 2)
    assert reason and "9" in reason and "does not exist" in reason, reason


# ---------------------------------------------------------------------------
# R11 / R12 -- the 'blocks' reference, checked at rest
# ---------------------------------------------------------------------------

def test_a_blocks_reference_to_an_approved_item_is_accepted(tmp_path):
    """Negative control for both rules below."""
    blocker, blocker_text = _other(2, "a-blocking-defect")
    write_backlog(tmp_path, extra={blocker: _with_blocks(blocker_text, 1)})
    assert problems(tmp_path) == []


def test_a_blocks_reference_that_is_not_a_number_is_reported(tmp_path):
    write_backlog(tmp_path, _with_blocks(GOOD, "the parser item"))
    assert reported(tmp_path, "is not an integer item id")


def test_an_item_that_blocks_itself_is_reported(tmp_path):
    """Otherwise an item is its own authority, which is no authority."""
    write_backlog(tmp_path, _with_blocks(GOOD, 1))
    assert reported(tmp_path, "names the item itself")


def test_a_blocks_reference_to_a_missing_item_is_reported(tmp_path):
    write_backlog(tmp_path, _with_blocks(GOOD, 4242))
    assert reported(tmp_path, "does not exist")


def test_a_blocks_reference_to_an_unapproved_item_is_reported(tmp_path):
    blocker, blocker_text = _other(2, "a-blocking-defect")
    write_backlog(tmp_path, _proposed(),
                  extra={blocker: _with_blocks(blocker_text, 1)})
    assert reported(tmp_path, "cannot be the authority")


@pytest.mark.parametrize("field,value", [("closed", "2026-08-05"),
                                         ("commit", "0" * 40)])
def test_an_item_awaiting_approval_carries_no_closing_fields(tmp_path, field,
                                                             value):
    """R7 covers every live status, not just ``open``.

    Written against ``ACTIVE_STATUSES`` rather than the literal so that a
    status added to that tuple is covered the day it is added, rather than the
    day somebody remembers this file.
    """
    text = mutate(_proposed(), "opened: 2026-08-04",
                  f"opened: 2026-08-04\n{field}: {value}")
    write_backlog(tmp_path, text)
    assert reported(tmp_path, f"{PROPOSED_STATUS!r} but a {field!r}")
