#!/usr/bin/env python3
"""The feature vocabulary a project can be configured with, and its storage.

Until this module existed the vocabulary lived in exactly one place: a Markdown
table inside ``templates/copilot-instructions.md``, and prose sections marked
``*Enabled by feature flag: `x`*`` beneath it. Nothing read either. Turning a
feature off meant an agent editing prose in a file no tool parses, so a
project whose ``Enabled features:`` line disagreed with the sections beneath it
could not be detected by anything, and a human could not see or change the
configuration without an agent.

So this module is the **single owner** of two things:

* :data:`FEATURES` -- what features exist, what they are called, what values
  each may take. ``tests/test_project_features.py`` pins the deployed template
  against it in both directions, so the table and the gated sections cannot
  drift from the vocabulary the menu offers. That check is the whole point: if
  the menu enumerates features and the template enumerates features, the two
  lists *will* disagree, and the disagreement surfaces as a menu that silently
  cannot toggle something. This repository has already paid for one duplicated
  enumeration -- ``test_workflow_discovery_conformance.py`` exists because a
  second copy of a glob let a ``.yaml`` workflow escape every assertion in the
  suite while every assertion stayed green.

* ``features.json`` in the per-project directory -- the machine-readable record
  of what one project chose. The prose stays; it is what an *agent* reads. This
  is what a *tool* reads, and the two are kept in agreement by writing the
  prose from here rather than by hoping.

Not every feature is a boolean. A tracked backlog is a choice of one backend
out of three -- a directory in the repository, GitHub Issues, or none at all --
and a toggle list cannot express that. Rather than carry two kinds of feature
with two renderers, every feature here is a choice among named options and a
flag is the case where those options happen to be ``on`` and ``off``. One
representation means one menu code path and one validator, and
:attr:`Feature.is_flag` still gives a caller the boolean reading when that is
what it wants.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from project_paths import catalog_guid, primary_repo_root, project_dir

__all__ = [
    "Option", "Feature", "FEATURES", "FEATURES_BY_SLUG", "SLUGS",
    "ON", "OFF", "BACKLOG_FOLDER", "BACKLOG_GITHUB_ISSUES", "BACKLOG_NONE",
    "TRACKED_BACKLOG", "CONFIG_NAME", "CONFIG_VERSION", "FeatureConfigError",
    "config_path", "read_config", "write_config", "resolved_values", "stored_values",
    "unknown_entries", "is_enabled", "enabled_slugs", "enabled_features_line",
    "describe_value", "tracked_backlog_backend",
]


@dataclass(frozen=True)
class Option:
    """One value a feature may take, and how to say it to a human."""

    value: str
    label: str


@dataclass(frozen=True)
class Feature:
    """One configurable convention.

    ``off_value`` is named explicitly rather than inferred. For a flag it is
    ``"off"`` and inferring it would be safe; for ``tracked-backlog`` it is
    ``"none"``, and any rule that guessed -- "the last option", "the falsy
    one" -- would be a rule that quietly changes meaning the first time
    somebody reorders the options or adds a fourth backend. What "this project
    has the feature turned off" means is a property of the feature, so the
    feature says it.
    """

    slug: str
    name: str
    description: str
    options: tuple[Option, ...]
    default: str
    off_value: str

    def __post_init__(self) -> None:
        values = [opt.value for opt in self.options]
        if len(values) != len(set(values)):
            raise ValueError(f"{self.slug}: duplicate option values {values}")
        for field, value in (("default", self.default),
                             ("off_value", self.off_value)):
            if value not in values:
                raise ValueError(
                    f"{self.slug}: {field} {value!r} is not one of {values}")

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(opt.value for opt in self.options)

    @property
    def is_flag(self) -> bool:
        """True when this feature is an on/off toggle rather than a choice.

        Derived rather than declared, so it cannot disagree with the options.
        """
        return self.values == (ON, OFF)

    def label_for(self, value: str) -> str:
        for opt in self.options:
            if opt.value == value:
                return opt.label
        return value

    def accepts(self, value) -> bool:
        return isinstance(value, str) and value in self.values


ON = "on"
OFF = "off"

BACKLOG_FOLDER = "folder"
BACKLOG_GITHUB_ISSUES = "github-issues"
BACKLOG_NONE = "none"

#: The slug of the one feature that is a choice rather than a flag. Named so
#: that callers asking "which backlog backend did this project pick?" -- the
#: enforcement tests, chiefly -- do not spell the string a second time.
TRACKED_BACKLOG = "tracked-backlog"

_FLAG = (Option(ON, "on"), Option(OFF, "off"))


FEATURES: tuple[Feature, ...] = (
    Feature(
        slug="session-handoff",
        name="Session Handoff",
        description="Per-instance handoff files for cross-session continuity",
        options=_FLAG, default=ON, off_value=OFF,
    ),
    Feature(
        slug="session-history",
        name="Session History",
        description="SQL `session_log` table for audit trail",
        options=_FLAG, default=ON, off_value=OFF,
    ),
    Feature(
        slug="spec-driven",
        name="Spec-Driven Development",
        description=("Spec as source of truth. Uses GitHub spec-kit. "
                     "Location: `.specify/` and `specs/`."),
        options=_FLAG, default=ON, off_value=OFF,
    ),
    Feature(
        slug="parallel-agents",
        name="Parallel Agents",
        description="SQL-coordinated parallel task execution via `todo_claims`.",
        options=_FLAG, default=ON, off_value=OFF,
    ),
    Feature(
        slug="operator-agents",
        name="Operator Agents",
        description="Peer Copilot sessions via `operator`, and mail between them",
        options=_FLAG, default=ON, off_value=OFF,
    ),
    Feature(
        slug="branching-strategy",
        name="Branching Strategy",
        description=("Feature branches in worktrees, merged to `main`, "
                     "conventional commits"),
        options=_FLAG, default=ON, off_value=OFF,
    ),
    Feature(
        slug=TRACKED_BACKLOG,
        name="Tracked Backlog",
        description="Where open work is recorded",
        options=(
            Option(BACKLOG_FOLDER,
                   "`backlog/` in the repo, one file per item, enforced by tests"),
            Option(BACKLOG_GITHUB_ISSUES, "GitHub Issues"),
            Option(BACKLOG_NONE, "no tracked backlog"),
        ),
        default=BACKLOG_FOLDER,
        off_value=BACKLOG_NONE,
    ),
)

FEATURES_BY_SLUG: dict[str, Feature] = {f.slug: f for f in FEATURES}
SLUGS: tuple[str, ...] = tuple(f.slug for f in FEATURES)

if len(FEATURES_BY_SLUG) != len(FEATURES):        # pragma: no cover - import guard
    raise RuntimeError("duplicate feature slug in FEATURES")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

#: The per-project file. Beside the handoff and the check-in watermark, and
#: outside the repository for the same reason both of those are: which
#: conventions a project opted into is a fact about this machine's setup, not
#: content every clone should carry.
CONFIG_NAME = "features.json"

#: Bumped only if the *shape* of the document changes. Adding a feature does
#: not change the shape -- an older reader meeting a newer slug is handled by
#: :func:`unknown_entries` rather than by refusing the file.
CONFIG_VERSION = 1


class FeatureConfigError(RuntimeError):
    """The configuration could not be read, understood or written."""


def config_path(guid: str) -> Path:
    """Where ``guid``'s feature configuration lives."""
    return project_dir(guid) / CONFIG_NAME


def read_config(path) -> "dict | None":
    """The stored document, or ``None`` when there has never been one.

    ``None`` means *never written*, and nothing else. A file that exists and
    cannot be read raises, because collapsing the two leads opposite ways: an
    absent configuration correctly resolves to the defaults, and an unreadable
    one doing the same would report every feature at its default -- which is a
    complete, confident answer about a project whose actual choices nobody
    managed to look at. Reporting "spec-driven is on" because the file was
    unreadable is worse than reporting nothing, and the menu would then offer
    to write those invented defaults back over the real file.

    That is this repository's most expensive defect class, and it has produced
    its four worst findings: a failed ``iterdir`` that became an empty
    population, a failed pane lookup that removed a peer from a list, a failed
    spec read that became "unrecorded", an empty lock file that became "stale".
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FeatureConfigError(
            f"Cannot read the feature configuration {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise FeatureConfigError(
            f"The feature configuration {path} is not valid JSON: {exc}. "
            "Delete it to fall back to the defaults.") from exc
    if not isinstance(data, dict):
        raise FeatureConfigError(
            f"The feature configuration {path} holds "
            f"{type(data).__name__}, not an object.")
    # An absent ``features`` key is refused for the same reason a non-object
    # one is. ``write_config`` always emits it, so a document without it was
    # not written by this tool -- it is a different JSON file that happens to
    # be sitting at this path, or one somebody hand-edited into a shape with
    # no meaning here. Defaulting it to ``{}`` would treat "this is not our
    # file" as "our file, with nothing set in it", and the screen would then
    # report every feature at its default and offer to write those invented
    # values over it. ``.get(key, {})`` is how that particular collapse gets
    # written by accident: the sibling check below refuses ``"features": []``
    # loudly, and the two are the same malformation.
    if "features" not in data:
        raise FeatureConfigError(
            f"The feature configuration {path} has no 'features' object. "
            "Delete it to fall back to the defaults.")
    features = data["features"]
    if not isinstance(features, dict):
        raise FeatureConfigError(
            f"The feature configuration {path} has 'features' as "
            f"{type(features).__name__}, not an object.")
    # Values are checked here, not at the point of use. A JSON document is not
    # a schema: ``{"spec-driven": true}`` is a perfectly good object, and a
    # caller comparing it against ``"on"`` would silently read it as off.
    for slug, value in features.items():
        feature = FEATURES_BY_SLUG.get(slug)
        if feature is None:
            # A slug this build does not know is not an error -- it is what a
            # configuration written by a newer toolkit looks like. It is
            # reported by `unknown_entries` and preserved by `write_config`.
            continue
        if not feature.accepts(value):
            raise FeatureConfigError(
                f"The feature configuration {path} sets {slug!r} to "
                f"{value!r}, which is not one of {list(feature.values)}.")
    return data


def stored_values(document: "dict | None") -> dict:
    """The ``features`` mapping out of a document, defaulting to empty."""
    if not document:
        return {}
    features = document.get("features", {})
    return dict(features) if isinstance(features, dict) else {}


def resolved_values(document: "dict | None") -> dict:
    """Every known feature's value, with the defaults filled in.

    The result always has exactly :data:`SLUGS` as its keys, so no caller has
    to decide what an absent entry means -- an absent entry means the default,
    and it means that in one place rather than at every read site.

    A stored value this build would not accept also resolves to the default.
    That is unreachable through :func:`read_config`, which refuses such a
    document outright; it is here so that a hand-built dict cannot put a value
    outside the vocabulary into a caller that will compare it against one.
    """
    stored = stored_values(document)
    resolved = {}
    for feature in FEATURES:
        value = stored.get(feature.slug)
        resolved[feature.slug] = value if feature.accepts(value) else feature.default
    return resolved


def unknown_entries(document: "dict | None") -> tuple[str, ...]:
    """Slugs in the document that this build has no feature for.

    Surfaced rather than dropped so a downgrade is visible. They are also
    preserved by :func:`write_config`; an older operator writing a newer
    project's configuration must not silently delete the settings it does not
    understand.
    """
    return tuple(sorted(s for s in stored_values(document)
                        if s not in FEATURES_BY_SLUG))


def write_config(path, values: dict, *, document: "dict | None" = None) -> dict:
    """Record ``values`` for the known features. Raises on an invalid one.

    ``document`` is whatever :func:`read_config` last returned for this path.
    Entries in it whose slugs this build does not recognise are carried
    through untouched -- as are any that have appeared in the file since, so a
    setting made by a newer toolkit between that read and this write survives
    a build that has no name for it. A file that has become unreadable in the
    meantime is refused rather than overwritten.

    Written to a sibling temp file, flushed to the disk itself, and moved into
    place, so an interrupted write leaves the previous configuration intact
    rather than a truncated file that :func:`read_config` would then refuse --
    which would turn a Ctrl-C into a project whose configuration cannot be
    read at all.
    """
    path = Path(path)
    for slug, value in values.items():
        feature = FEATURES_BY_SLUG.get(slug)
        if feature is None:
            raise FeatureConfigError(f"No such feature: {slug!r}")
        if not feature.accepts(value):
            raise FeatureConfigError(
                f"{slug!r} cannot be {value!r}; "
                f"it takes one of {list(feature.values)}.")
    current = resolved_values(document)
    # Settings this build has no name for are carried from the file as it is
    # *now*, not only from the document the caller read. Between that read and
    # this write the user can install a newer toolkit, or a second operator can
    # change something -- and this build, which by definition cannot see what
    # those slugs mean, would write a payload with them missing. A downgrade
    # has to arrive as a conflict, never as a gap: a silently dropped setting
    # is indistinguishable from one that was never made.
    #
    # A file that has become unreadable is refused rather than overwritten.
    # Rewriting it would destroy choices nobody managed to look at, which is
    # exactly what the menu refuses to do one layer up.
    latest = read_config(path)
    merged: dict = {}
    for source in (document, latest):
        merged.update({s: v for s, v in stored_values(source).items()
                       if s not in FEATURES_BY_SLUG})
    merged.update({f.slug: values.get(f.slug, current[f.slug])
                   for f in FEATURES})
    payload = {"version": CONFIG_VERSION, "features": merged}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd: "int | None" = None
        tmp: "str | None" = None
        placed = False
        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".features-",
                                       suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fd = None               # ``fdopen`` owns the descriptor now
                fh.write(text)
                # Closing flushes this process's buffers to the OS, and no
                # further. ``os.replace`` then commits a directory entry that
                # can outlive the data it points at, so a power loss here
                # leaves a file that is present, empty, and refused by
                # ``read_config`` -- the settings destroyed by the write that
                # was meant to preserve them.
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            placed = True
        finally:
            # ``finally`` rather than ``except OSError``: a Ctrl-C landing
            # between the write and the replace is not an OSError, and it
            # would leave a .features-*.json behind in the project directory
            # with nothing that ever cleans it up. ``mkstemp`` is inside the
            # ``try`` for the same reason -- an interrupt arriving between it
            # returning and the block being entered is the one interleaving
            # that strands a file no cleanup can still see.
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp is not None and not placed:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError as exc:
        raise FeatureConfigError(
            f"Cannot write the feature configuration {path}: {exc}") from exc
    return payload


# --------------------------------------------------------------------------
# Reading a resolved configuration
# --------------------------------------------------------------------------

def is_enabled(values: dict, slug: str) -> bool:
    """Whether ``slug`` is on, for the boolean reading of a choice.

    A choice is "enabled" when it is anything other than its ``off_value``, so
    a project on GitHub Issues has a tracked backlog and a project on ``none``
    does not.
    """
    feature = FEATURES_BY_SLUG.get(slug)
    if feature is None:
        raise KeyError(slug)
    return values.get(slug, feature.default) != feature.off_value


def enabled_slugs(values: dict) -> tuple[str, ...]:
    """The enabled features, in declaration order."""
    return tuple(f.slug for f in FEATURES if is_enabled(values, f.slug))


def enabled_features_line(values: dict) -> str:
    """The ``Enabled features:`` line for a per-project instructions file.

    The prose an agent reads is generated from the same values the menu wrote,
    which is what stops the two from drifting. Written here rather than by the
    caller so that every writer of that line produces the same one.
    """
    enabled = enabled_slugs(values)
    return f"Enabled features: {', '.join(enabled) if enabled else 'none'}."


def describe_value(slug: str, value: str) -> str:
    """A human label for one feature's current value."""
    feature = FEATURES_BY_SLUG.get(slug)
    if feature is None:
        raise KeyError(slug)
    return feature.label_for(value)


def tracked_backlog_backend(repo_root=None) -> "tuple[str, str]":
    """``(backend, source)`` for the project ``repo_root`` belongs to.

    The one caller that matters is the enforcement in
    ``tests/test_backlog_conformance.py``: a project that chose GitHub Issues
    or no backlog at all must not have a ``backlog/`` directory demanded of
    it. ``source`` describes where the answer came from, so a test that steps
    aside can say *why* instead of merely vanishing.

    **Every uncertainty resolves to the default**, which is the enforcing
    answer. There is no catalog in CI, so a predicate that answered "unknown"
    -- or that treated an unreadable configuration as a reason to stand down
    -- would silently retire three real guards on all eight legs while every
    one of them stayed green. That is this repository's worst failure shape: a
    check that reads as evidence and is not. Standing down has to be something
    a person deliberately configured, never something a missing file achieved.

    Keyed on the *primary* checkout, so a worktree gets its project's answer
    rather than looking like an unregistered directory -- which, under the
    rule above, would have been the enforcing answer anyway, but for the wrong
    reason and only by luck.
    """
    default = FEATURES_BY_SLUG[TRACKED_BACKLOG].default
    root = primary_repo_root(repo_root)
    found = catalog_guid(root)
    if found.guid is None:
        return default, f"no project configuration ({found.reason})"
    path = config_path(found.guid)
    try:
        document = read_config(path)
    except FeatureConfigError as exc:
        return default, f"{path} could not be read ({exc})"
    if document is None:
        return default, f"{path} has never been written"
    return resolved_values(document)[TRACKED_BACKLOG], str(path)
