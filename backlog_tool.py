"""The tracked backlog: parse, validate and render ``backlog/`` items.

Why this exists
---------------

Open work in a Copilot-tools project survives only in
``~/.copilot/projects/{guid}/next-session.md``, which is read-once and deleted
at session start. Closed work is answerable from ``git log``; open work was
answerable from nothing durable at all. It lived in a live agent's context and
was carried forward as one re-summarised sentence per session -- lossy by
construction, and nothing could detect the loss.

``backlog/`` is the fallback. One file per item, under version control, in the
repository the work belongs to.

One file per item, not one file
-------------------------------

``backlog/0007-entra-id-endpoint-drift.md`` -- zero-padded id, kebab slug.

This is the main design constraint, not a stylistic preference. Parallel agents
work in separate worktrees off the same ``main``. A single ``BACKLOG.md`` puts
every add and every close on the same lines of the same file, so every
concurrent pair conflicts. One file per item makes an add conflict-free by
construction, and makes a close read as a small diff against one file.

Why the front-matter reader is hand-rolled
------------------------------------------

Two reasons, and the second is the one that decided it.

PyYAML is declared in this project's ``dev`` extra, not its runtime
dependencies -- and ``backlog`` is a console script, so importing yaml here
would give a dependency-free toolkit its first runtime dependency, payable by
every user who installs without ``[dev]``. This repository already carries a
narrow hand-rolled TOML reader for a comparable reason (``tomllib`` is 3.11+
and the floor is 3.10), so the precedent is established.

The second reason is that YAML's comment rule is actively wrong for this data.
``title: Fix issue #42`` parses in YAML as the value ``Fix issue`` with ``#42``
discarded as a comment, because YAML strips ``#`` wherever whitespace precedes
it. Titles here name defects, and defects have numbers. So this reader does
*no* comment stripping: a value is the rest of its line. The visible
consequence is that an inline ``# open | closed | rejected`` left in a copied
template does not quietly parse as ``open`` -- it fails, loudly, naming the
file. That is the better failure, and ``backlog/README.md`` documents the
vocabulary instead of a comment doing it.

The parser is deliberately flat: no nesting, no multi-line values, no anchors.
Every field is one line. A format that cannot express structure cannot grow a
structural ambiguity for a hand-rolled reader to get wrong.

What stops it rotting
---------------------

A backlog nothing reads decays exactly like any other prose. ``check()`` is the
single owner of every rule, ``tests/test_backlog_conformance.py`` runs it
against the real directory, and ``backlog check`` runs the same code from a
terminal. There is one implementation of the vocabulary, one of the discovery
rule, and one of each check -- a second copy is the thing that drifts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from install_manifest import path_present
from project_paths import resolved_str

__all__ = [
    "STATUSES", "TERMINAL_STATUSES", "COMMIT_REQUIRED_STATUSES", "OPEN_STATUS",
    "NO_SPEC", "BACKLOG_DIRNAME", "EVIDENCE_HEADING", "REQUIRED_FIELDS",
    "KNOWN_FIELDS", "ITEM_FILENAME", "Item", "BacklogFormatError",
    "checkout_root", "item_paths", "split_front_matter", "parse_item", "load",
    "check", "render_html", "main",
]

#: The complete status vocabulary. This tuple is the only place the legal
#: values are written down; tests read it rather than repeating the literals,
#: because a test that spells its own copy of a vocabulary stops testing the
#: vocabulary the moment the two disagree.
STATUSES = ("open", "closed", "rejected")

#: The one status that means work is outstanding.
OPEN_STATUS = "open"

#: Statuses that end an item's life. Both require a ``closed`` date: a
#: rejection is a decision with a date, not an absence.
TERMINAL_STATUSES = ("closed", "rejected")

#: Statuses that require a resolvable ``commit``. Only ``closed`` does --
#: ``rejected`` means nothing shipped, so demanding a SHA for it would force
#: whoever rejects an item to invent one, and an invented SHA is worse than a
#: blank field because it looks like evidence.
COMMIT_REQUIRED_STATUSES = ("closed",)

#: The literal a ``spec`` field carries when an item has no spec-kit impact.
#: Required rather than allowing the field to be blank, so "this changes no
#: specification" is a decision somebody wrote down instead of a silence that
#: could equally mean nobody looked.
NO_SPEC = "none"

BACKLOG_DIRNAME = "backlog"
SPECS_DIRNAME = "specs"
EVIDENCE_HEADING = "## Evidence"

#: Item filenames: four-digit id, a kebab slug, ``.md``. ``README.md`` and any
#: other documentation in the directory does not match, so it is excluded by
#: the discovery rule itself rather than by a special case somewhere that a
#: later reader could fail to notice.
ITEM_FILENAME = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")

REQUIRED_FIELDS = ("id", "title", "status", "opened", "spec")
OPTIONAL_FIELDS = ("closed", "commit", "requirement")
KNOWN_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

_DELIMITER = "---"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(.*)$")

# A background supervisor with no console of its own would otherwise flash a
# console window for each git call on Windows. Same reasoning as
# ``project_paths._POPEN_KWARGS``; spelled again rather than imported because
# that name is private to that module.
_POPEN_KWARGS: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if platform.system() == "Windows" else {}
)


class BacklogFormatError(ValueError):
    """A backlog file could not be parsed at all."""


@dataclass(frozen=True)
class Item:
    """One parsed backlog item.

    ``front`` holds raw string values exactly as written. Typing happens in
    :func:`check`, so that a malformed value is reported as a named problem
    against a named file rather than raised as a bare ``ValueError`` from
    somewhere in the middle of a load.
    """

    path: Path
    front: dict
    body: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def status(self) -> str:
        return self.front.get("status", "")

    @property
    def title(self) -> str:
        return self.front.get("title", "")

    @property
    def filename_id(self) -> int:
        """The id encoded in the filename. Callers hold a path that matched
        :data:`ITEM_FILENAME`, so the match cannot be None here."""
        return int(ITEM_FILENAME.match(self.name).group(1))

    def section(self, heading: str) -> str:
        """The text under ``heading``, up to the next heading of any level.

        Returns the empty string when the heading is absent, which is the same
        answer as "present but empty" on purpose: both mean there is nothing
        written there, and both are equally not evidence.
        """
        lines = self.body.splitlines()
        out: list[str] = []
        collecting = False
        for line in lines:
            if line.strip() == heading:
                collecting = True
                continue
            if collecting and line.startswith("#"):
                break
            if collecting:
                out.append(line)
        return "\n".join(out).strip()


def checkout_root(start=None) -> Path:
    """The root of the working tree ``start`` belongs to.

    **This is the one place in this toolkit where ``git rev-parse
    --show-toplevel`` is the correct call, and using
    ``project_paths.primary_repo_root`` here would be the bug.**

    That function answers "which project is this", and it deliberately resolves
    a linked worktree back to the primary checkout so that a worktree cannot
    mint a second project identity. The backlog is not identity: it is tracked
    content, versioned on a branch. In a worktree the content that matters is
    the worktree's, and an agent adding an item on a feature branch must
    validate the items on that branch.

    Resolving to the primary checkout instead would validate whatever `main`
    happens to hold, so a malformed item added on a branch would pass its own
    branch's test run, and a well-formed one would be invisible to the tooling
    that just wrote it. Both were observed before this function existed.

    Falls back to ``start`` (or the cwd) when git is missing, the call fails,
    or the path is not inside a repository.
    """
    base = Path(start) if start is not None else Path.cwd()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(base), capture_output=True,
            encoding="utf-8", errors="replace", timeout=10,
            **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return base
    if proc.returncode != 0 or not proc.stdout:
        return base
    top = proc.stdout.strip()
    return Path(top) if top else base


def backlog_dir(repo_root=None) -> Path:
    """The backlog directory of the working tree ``repo_root`` belongs to."""
    root = Path(repo_root) if repo_root is not None else checkout_root()
    return root / BACKLOG_DIRNAME


def item_paths(directory) -> list:
    """Every item file in ``directory``, sorted by name.

    The single owner of "what is a backlog item". A second copy of this glob
    anywhere is how a file with an unexpected name gets excluded from every
    assertion while all of them keep passing green.

    Raises :class:`BacklogFormatError` when the directory exists but cannot be
    listed. Returning an empty list there would be the worst available answer:
    "no items" is what a *clean* backlog directory looks like to every rule
    downstream, so a permission denial would report the backlog as conforming
    at the moment it became unreadable.

    An entry that cannot be examined is *kept*, not skipped, for the same
    reason -- parsing it then fails and names it, where dropping it would
    leave no trace at all.
    """
    directory = Path(directory)
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BacklogFormatError(
            f"{directory} exists but cannot be listed: {exc}") from exc
    return sorted(
        (p for p in entries
         if ITEM_FILENAME.match(p.name) and path_present(p) is not False),
        key=lambda p: p.name,
    )


def split_front_matter(text: str) -> tuple:
    """Split ``text`` into its front-matter mapping and its body.

    Raises :class:`BacklogFormatError` when the document does not open with a
    ``---`` line or the block is never closed. Values are the rest of the line,
    stripped of surrounding whitespace and nothing else -- see the module
    docstring for why no comment stripping happens.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _DELIMITER:
        raise BacklogFormatError(
            "file does not begin with a '---' front-matter delimiter")
    front: dict = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == _DELIMITER:
            return front, "\n".join(lines[index + 1:]).strip()
        if not line.strip():
            continue
        match = _FIELD.match(line)
        if not match:
            raise BacklogFormatError(
                f"front-matter line {index + 1} is not 'key: value': {line!r}")
        key = match.group(1)
        if key in front:
            raise BacklogFormatError(f"duplicate front-matter key {key!r}")
        front[key] = match.group(2).strip()
    raise BacklogFormatError("front-matter block is never closed with '---'")


def parse_item(path) -> Item:
    """Parse one item file. Raises :class:`BacklogFormatError`."""
    path = Path(path)
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    return Item(path=path, front=front, body=body)


def load(directory) -> tuple:
    """Parse every item in ``directory``.

    Returns ``(items, problems)``. A file that cannot be parsed becomes a
    problem string rather than an exception, so one broken item reports itself
    by name instead of hiding every other item behind a traceback.
    """
    items: list = []
    problems: list = []
    try:
        paths = item_paths(directory)
    except BacklogFormatError as exc:
        return [], [str(exc)]
    for path in paths:
        try:
            items.append(parse_item(path))
        except (BacklogFormatError, OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name}: {exc}")
    return items, problems


def _commit_resolves(sha: str, repo_root) -> bool:
    """True when ``sha`` names a commit that exists in this repository.

    ``^{commit}`` is load-bearing: ``git cat-file -e`` alone succeeds for a
    blob or a tree, so without it any 40-hex object -- including one that is
    not a commit at all -- would certify a close.
    """
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=str(repo_root), capture_output=True,
            encoding="utf-8", errors="replace", timeout=30,
            **_POPEN_KWARGS,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def check(repo_root=None, *, resolve_commits: bool = True) -> list:
    """Every rule, in one place. Returns a list of problem strings.

    Empty means the backlog is well-formed. Never raises: a rule that crashes
    reports nothing, and reporting nothing is indistinguishable from passing.

    ``resolve_commits=False`` skips the git round trip, for callers rendering a
    view rather than gating a build.
    """
    root = Path(repo_root) if repo_root is not None else checkout_root()
    directory = root / BACKLOG_DIRNAME
    problems: list = []

    # R0. The directory exists and holds at least one item.
    #
    # Without this, deleting backlog/ turns every rule below into a loop over
    # an empty list, and the suite reports the backlog perfectly clean at the
    # exact moment it stopped existing. A guard whose subject can vanish has to
    # assert its subject is there.
    #
    # "Cannot tell" gets its own answer rather than sharing one with "absent":
    # a directory that is occupied but unexaminable is not a repository without
    # a backlog, and reporting it as one would be the same silence in a
    # different costume.
    present = path_present(directory)
    if present is False:
        return [f"{BACKLOG_DIRNAME}/ does not exist at {directory}"]
    if present is None:
        return [f"{BACKLOG_DIRNAME}/ at {directory} exists but cannot be "
                "examined, so its contents were never checked"]
    items, problems = load(directory)
    if not items:
        problems.append(
            f"{BACKLOG_DIRNAME}/ contains no items matching "
            f"{ITEM_FILENAME.pattern!r}; every rule below would pass vacuously")
        return problems

    seen_ids: dict = {}
    for item in items:
        name = item.name
        front = item.front

        # R1. Unknown or missing fields.
        for field in REQUIRED_FIELDS:
            if not front.get(field):
                problems.append(f"{name}: required field {field!r} is missing "
                                "or empty")
        for field in front:
            if field not in KNOWN_FIELDS:
                problems.append(
                    f"{name}: unknown front-matter field {field!r}; known "
                    f"fields are {', '.join(KNOWN_FIELDS)}")

        # R2. The id parses and matches the filename.
        raw_id = front.get("id", "")
        try:
            numeric_id = int(raw_id)
        except ValueError:
            problems.append(f"{name}: id {raw_id!r} is not an integer")
            numeric_id = None
        if numeric_id is not None:
            if numeric_id != item.filename_id:
                problems.append(
                    f"{name}: front-matter id {numeric_id} does not match the "
                    f"filename id {item.filename_id}")
            # R3. Ids are unique.
            if numeric_id in seen_ids:
                problems.append(
                    f"{name}: id {numeric_id} is already used by "
                    f"{seen_ids[numeric_id]}")
            else:
                seen_ids[numeric_id] = name

        # R4. The status is one of the legal values.
        status = front.get("status", "")
        if status and status not in STATUSES:
            problems.append(
                f"{name}: status {status!r} is not one of "
                f"{', '.join(STATUSES)}")

        # R5. Dates are well-formed.
        for field in ("opened", "closed"):
            value = front.get(field, "")
            if value and not _DATE.match(value):
                problems.append(
                    f"{name}: {field} {value!r} is not a YYYY-MM-DD date")

        # R6. Evidence is present and non-empty. An item with no evidence is a
        # rumour, and a backlog of rumours is worse than no backlog because it
        # costs a reader time to discover that.
        if not item.section(EVIDENCE_HEADING):
            problems.append(
                f"{name}: the {EVIDENCE_HEADING!r} section is missing or empty")

        # R7. Terminal items carry a closing date; open items carry neither a
        # closing date nor a commit.
        closed_on = front.get("closed", "")
        commit = front.get("commit", "")
        if status in TERMINAL_STATUSES and not closed_on:
            problems.append(
                f"{name}: status is {status!r} but no 'closed' date is set")
        if status == OPEN_STATUS:
            if closed_on:
                problems.append(
                    f"{name}: status is 'open' but a 'closed' date is set")
            if commit:
                problems.append(
                    f"{name}: status is 'open' but a 'commit' is set")

        # R8. A closed item names a commit, and that commit resolves here. A
        # close pointing at nothing is a claim that something shipped with
        # nothing behind it.
        if status in COMMIT_REQUIRED_STATUSES:
            if not commit:
                problems.append(
                    f"{name}: status is {status!r} but no 'commit' is named")
            elif resolve_commits and not _commit_resolves(commit, root):
                problems.append(
                    f"{name}: commit {commit!r} does not resolve to a commit "
                    "in this repository")

        # R9. The spec-kit mapping. Either a path that exists under specs/, or
        # the explicit literal 'none'.
        spec = front.get("spec", "")
        spec_path = None
        if spec and spec != NO_SPEC:
            candidate = root / spec
            posix = spec.replace(os.sep, "/")
            spec_here = path_present(candidate)
            if not posix.startswith(f"{SPECS_DIRNAME}/"):
                problems.append(
                    f"{name}: spec {spec!r} is not a path under "
                    f"{SPECS_DIRNAME}/ (use {NO_SPEC!r} when there is no "
                    "specification impact)")
            elif spec_here is False:
                problems.append(
                    f"{name}: spec {spec!r} does not exist at {candidate}")
            elif spec_here is None:
                problems.append(
                    f"{name}: spec {spec!r} at {candidate} cannot be examined")
            else:
                spec_path = candidate

        # R10. A named requirement must actually occur in the spec it names.
        # This is what makes the item-to-spec mapping load-bearing rather than
        # decorative: rename or delete the requirement and this goes red, which
        # is the whole point of recording it.
        requirement = front.get("requirement", "")
        if requirement:
            if spec == NO_SPEC or not spec:
                problems.append(
                    f"{name}: a 'requirement' is named but 'spec' is "
                    f"{spec or 'empty'!r}, so there is nothing to check it "
                    "against")
            elif spec_path is not None:
                try:
                    text = spec_path.read_text(encoding="utf-8",
                                               errors="replace")
                except OSError as exc:
                    problems.append(
                        f"{name}: spec {spec!r} could not be read, so "
                        f"requirement {requirement!r} was never checked: {exc}")
                else:
                    if requirement not in text:
                        problems.append(
                            f"{name}: requirement {requirement!r} does not "
                            f"appear in {spec}")

    return problems


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;",
                 '"': "&quot;", "'": "&#39;"}


def _escape(text: str) -> str:
    return "".join(_HTML_ESCAPES.get(ch, ch) for ch in text)


def _embed_json(payload) -> str:
    """Serialise ``payload`` for a ``<script>`` block it cannot escape from.

    ``</script>`` inside a JSON string ends the block as far as an HTML parser
    is concerned, no matter that JSON considers it ordinary text -- so the
    three characters that could begin such a sequence are emitted as ``\\uXXXX``
    escapes. JSON and JavaScript both read those back as the original
    characters, so nothing is lost, and no item title can break the page or
    inject markup into it.
    """
    return (json.dumps(payload, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def render_html(repo_root=None, *, resolve_commits: bool = True) -> str:
    """A self-contained HTML view of the backlog.

    Self-contained is the requirement, not a nicety. A page that fetched its
    items on load would work when served over HTTP and fail silently when
    opened from disk: browsers treat every ``file://`` document as an opaque
    origin, so ``fetch('0001-x.md')`` is refused by CORS and the page renders
    an empty backlog with no error a reader would see. Embedding the data at
    generation time removes the failure mode entirely, and generating on demand
    means the page cannot be stale the way a committed copy would be.
    """
    root = Path(repo_root) if repo_root is not None else checkout_root()
    items, parse_problems = load(root / BACKLOG_DIRNAME)
    problems = check(root, resolve_commits=resolve_commits)

    payload = {
        "repo": root.name,
        "root": str(root),
        "problems": problems,
        "items": [
            {
                "id": item.front.get("id", ""),
                "file": item.name,
                "title": item.title,
                "status": item.status,
                "opened": item.front.get("opened", ""),
                "closed": item.front.get("closed", ""),
                "commit": item.front.get("commit", ""),
                "spec": item.front.get("spec", ""),
                "requirement": item.front.get("requirement", ""),
                "body": item.body,
            }
            for item in items
        ],
        "statuses": list(STATUSES),
    }
    del parse_problems  # already folded into `problems` by check()

    return _PAGE.replace("__DATA__", _embed_json(payload))


_PAGE = """<!DOCTYPE html>
<meta charset="utf-8">
<title>Backlog</title>
<style>
 :root { color-scheme: light dark; }
 body { font: 15px/1.5 ui-sans-serif, system-ui, sans-serif;
        margin: 0 auto; max-width: 60rem; padding: 2rem 1rem; }
 h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
 .sub { opacity: .6; font-size: .85rem; margin-bottom: 1.5rem; }
 .problems { border-left: 3px solid #c0392b; background: #c0392b18;
             padding: .75rem 1rem; margin-bottom: 1.5rem; border-radius: 3px; }
 .problems ul { margin: .5rem 0 0; padding-left: 1.2rem; }
 .ok { border-left: 3px solid #27ae60; background: #27ae6018;
       padding: .6rem 1rem; margin-bottom: 1.5rem; border-radius: 3px; }
 nav { display: flex; gap: .5rem; margin-bottom: 1rem; flex-wrap: wrap; }
 button { font: inherit; padding: .3rem .8rem; border-radius: 999px;
          border: 1px solid currentColor; background: transparent;
          cursor: pointer; opacity: .55; }
 button.on { opacity: 1; font-weight: 600; }
 .item { border: 1px solid #8884; border-radius: 5px; margin-bottom: .6rem; }
 .head { display: flex; gap: .7rem; align-items: baseline; cursor: pointer;
         padding: .7rem .9rem; }
 .head:hover { background: #8881; }
 .id { font-variant-numeric: tabular-nums; opacity: .5; }
 .title { flex: 1; font-weight: 600; }
 .tag { font-size: .72rem; text-transform: uppercase; letter-spacing: .06em;
        padding: .12rem .5rem; border-radius: 999px; border: 1px solid; }
 .open { color: #d68910; } .closed { color: #27ae60; } .rejected { opacity: .5; }
 .meta { display: flex; gap: 1.2rem; flex-wrap: wrap; font-size: .8rem;
         opacity: .7; padding: 0 .9rem .5rem; }
 .body { padding: 0 .9rem 1rem; border-top: 1px solid #8884; margin-top: .3rem; }
 .body h4 { margin: 1rem 0 .3rem; font-size: .95rem; }
 .body p { margin: .3rem 0; white-space: pre-wrap; }
 .hidden { display: none; }
 code { background: #8882; padding: .05rem .3rem; border-radius: 3px; }
</style>
<body>
<h1>Backlog</h1>
<div class="sub" id="sub"></div>
<div id="health"></div>
<nav id="filters"></nav>
<div id="items"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const esc = s => { const d = document.createElement('div');
                   d.textContent = s; return d.innerHTML; };

document.getElementById('sub').textContent =
  D.repo + ' \\u2014 ' + D.items.length + ' item' +
  (D.items.length === 1 ? '' : 's') + ' \\u2014 ' + D.root;

const health = document.getElementById('health');
if (D.problems.length) {
  health.className = 'problems';
  health.innerHTML = '<strong>' + D.problems.length +
    ' conformance problem' + (D.problems.length === 1 ? '' : 's') +
    '</strong><ul>' +
    D.problems.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul>';
} else {
  health.className = 'ok';
  health.textContent = 'All conformance checks pass.';
}

// Markdown is shown, not interpreted: headings become headings and everything
// else is escaped text with its line breaks kept. Rendering it properly would
// mean shipping a parser, and an item body is prose in a table, not a document.
function bodyHtml(md) {
  return md.split(/\\n{2,}/).map(block => {
    const m = block.match(/^(#{1,6})\\s+(.*)$/s);
    if (m) {
      const rest = m[2].split('\\n');
      return '<h4>' + esc(rest.shift()) + '</h4>' +
             (rest.length ? '<p>' + esc(rest.join('\\n')) + '</p>' : '');
    }
    return '<p>' + esc(block) + '</p>';
  }).join('');
}

let active = 'all';
const counts = {all: D.items.length};
for (const s of D.statuses) counts[s] = D.items.filter(i => i.status === s).length;

const filters = document.getElementById('filters');
for (const key of ['all', ...D.statuses]) {
  const b = document.createElement('button');
  b.textContent = key + ' (' + (counts[key] || 0) + ')';
  b.onclick = () => { active = key; draw(); };
  b.dataset.key = key;
  filters.appendChild(b);
}

function draw() {
  for (const b of filters.children) b.className = b.dataset.key === active ? 'on' : '';
  const host = document.getElementById('items');
  host.innerHTML = '';
  const shown = D.items.filter(i => active === 'all' || i.status === active);
  if (!shown.length) {
    host.innerHTML = '<p style="opacity:.6">Nothing with this status.</p>';
    return;
  }
  for (const i of shown) {
    const el = document.createElement('div');
    el.className = 'item';
    const meta = [];
    meta.push('opened ' + esc(i.opened));
    if (i.closed) meta.push('closed ' + esc(i.closed));
    if (i.commit) meta.push('commit <code>' + esc(i.commit.slice(0, 12)) + '</code>');
    meta.push('spec <code>' + esc(i.spec) + '</code>');
    if (i.requirement) meta.push('requirement \\u201c' + esc(i.requirement) + '\\u201d');
    meta.push('<code>' + esc(i.file) + '</code>');
    el.innerHTML =
      '<div class="head"><span class="id">#' + esc(i.id) + '</span>' +
      '<span class="title">' + esc(i.title) + '</span>' +
      '<span class="tag ' + esc(i.status) + '">' + esc(i.status) + '</span></div>' +
      '<div class="meta">' + meta.map(m => '<span>' + m + '</span>').join('') + '</div>' +
      '<div class="body hidden">' + bodyHtml(i.body) + '</div>';
    const body = el.querySelector('.body');
    el.querySelector('.head').onclick = () => body.classList.toggle('hidden');
    host.appendChild(el);
  }
}
draw();
</script>
</body>
"""


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _default_html_path(root: Path) -> Path:
    """A stable, bookmarkable path outside the checkout.

    Outside is the point: this repository's test suite fails on stray untracked
    files in a checkout, and a generated view is exactly that. A stable name
    beats a fresh temp directory because a human can bookmark it once.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", root.name) or "repo"
    return Path(tempfile.gettempdir()) / f"backlog-{safe}.html"


def _cmd_list(args, root: Path) -> int:
    items, problems = load(root / BACKLOG_DIRNAME)
    for problem in problems:
        print(f"unparsed: {problem}", file=sys.stderr)
    wanted = args.status
    shown = [i for i in items if not wanted or i.status == wanted]
    if not shown:
        print("(no matching items)")
        return 0
    width = max(len(i.status) for i in shown)
    for item in shown:
        print(f"{item.filename_id:>4}  {item.status:<{width}}  {item.title}")
    return 0


def _cmd_show(args, root: Path) -> int:
    try:
        paths = item_paths(root / BACKLOG_DIRNAME)
    except BacklogFormatError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for item in paths:
        if int(ITEM_FILENAME.match(item.name).group(1)) == args.id:
            print(item.read_text(encoding="utf-8"))
            return 0
    print(f"no backlog item with id {args.id}", file=sys.stderr)
    return 1


def _cmd_check(args, root: Path) -> int:
    problems = check(root)
    if not problems:
        items, _ = load(root / BACKLOG_DIRNAME)
        count = len(items)
        print(f"backlog ok: {count} item{'' if count == 1 else 's'}")
        return 0
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{len(problems)} problem(s)", file=sys.stderr)
    return 1


def _cmd_html(args, root: Path) -> int:
    out = Path(args.out) if args.out else _default_html_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(root), encoding="utf-8")
    print(out)
    if args.open:
        try:
            webbrowser.open(Path(resolved_str(out)).as_uri())
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"wrote {out} but could not open a browser: {exc}",
                  file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="backlog",
        description="Read and validate the repository's tracked backlog.")
    parser.add_argument("-C", "--repo", default=None,
                        help="a path inside the repository (default: cwd)")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="one line per item")
    p_list.add_argument("--status", choices=STATUSES, default=None)
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="print one item in full")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=_cmd_show)

    sub.add_parser("check", help="validate every item").set_defaults(
        func=_cmd_check)

    p_html = sub.add_parser("html", help="write a self-contained HTML view")
    p_html.add_argument("--out", default=None)
    p_html.add_argument("--open", action="store_true",
                        help="open the page in a browser")
    p_html.set_defaults(func=_cmd_html)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["list"] + (["-C", args.repo] if args.repo
                                             else []))
    root = checkout_root(args.repo) if args.repo else checkout_root()
    return args.func(args, Path(root))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
