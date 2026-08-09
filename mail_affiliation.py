"""Where a message came from, and where it was delivered.

Operator mail had no idea what anyone was working on. `send_message` gates on
whether an instance *exists*, never on what it is doing, and the record it
writes carries `from`, `to`, `to_id`, `sent_at` and `text` -- nothing that
says which project either end belongs to. Five of the ten ordered instance
pairs on the machine this was written on are cross-project, and reconstructing
that fact required pattern-matching agent names, which is a guess.

This module answers the question the record could not. It is metadata and
only metadata: **nothing here may prevent, delay or alter a send.** That is
not an implementation convenience, it is the decision the 0025 review council
reached -- two of three seats rejected gating delivery on affiliation, because
no wrong outcome has ever been traced to a cross-project message while two of
the four cross-project threads measurably improved this repository. A refusal
built on an unknown affiliation would drop work the sender believed was sent,
which is this repository's own recurring defect class.

Three ideas are kept deliberately separate:

*Origin* is where the `operator send` process was standing. *Destination* is
where the receiving instance is bound. *Relationship* is what can be said
about the two together -- and it is tri-state, because "we do not know" is a
different claim from "they are the same", and rendering the first as the
second is the one failure mode this module must not have.
"""

from __future__ import annotations

import json
from pathlib import Path

import project_paths
from install_manifest import file_present

__all__ = [
    "SAME_PROJECT", "CROSS_PROJECT", "PROJECT_UNKNOWN",
    "UNPLACEABLE", "NO_LAUNCH_RECORD",
    "Affiliation", "describe_path", "describe_instance",
    "relationship", "relationship_label", "attach", "endpoints_of",
]

#: The three things that can be said about a pair of endpoints. `PROJECT_UNKNOWN`
#: is a first-class answer rather than a blank: a viewer that renders an
#: unknown affiliation as "same project" has invented the reassuring half of a
#: fact it does not have.
SAME_PROJECT = "same-project"
CROSS_PROJECT = "cross-project"
PROJECT_UNKNOWN = "project-unknown"

#: Why an endpoint has no project, beyond the reasons `project_paths` already
#: names (`catalog-missing`, `catalog-unreadable`, `no-entry`, `unusable-id`).
UNPLACEABLE = "unplaceable"
NO_LAUNCH_RECORD = "no-launch-record"

#: The keys the message record grows. Spelled once, here, because the writer
#: and every reader are in different files and a key name agreed by memory is
#: a key name that drifts.
ORIGIN_KEY = "origin"
DESTINATION_KEY = "delivered_to"


class Affiliation:
    """One endpoint: a directory, a project id, and why if there is none.

    ``project`` is non-empty exactly when ``status`` is ``"known"``. The
    failure reasons are carried rather than collapsed because they mean
    different things to whoever reads them later -- an unregistered checkout
    and an unreadable catalog produce the same blank and want opposite
    responses.
    """

    __slots__ = ("cwd", "project", "status", "detail")

    def __init__(self, cwd: str = "", project: str = "",
                 status: str = "", detail: str = "") -> None:
        self.cwd = cwd
        self.project = project
        self.status = status or ("known" if project else PROJECT_UNKNOWN)
        self.detail = detail

    def as_dict(self) -> dict:
        return {"cwd": self.cwd or None,
                "project_id": self.project or None,
                "status": self.status,
                "detail": self.detail or None}

    @classmethod
    def from_dict(cls, data) -> "Affiliation":
        """Read an endpoint back, tolerating everything.

        Called on records written by older versions, by a future version, and
        by a partially written file. Anything it cannot understand becomes an
        unknown endpoint, because the alternative -- raising while rendering a
        message -- would make a malformed field able to break delivery, and
        this module is not allowed to do that.
        """
        if not isinstance(data, dict):
            return cls(status=PROJECT_UNKNOWN)
        project = data.get("project_id") or ""
        return cls(cwd=str(data.get("cwd") or ""),
                   project=str(project),
                   status=str(data.get("status")
                              or ("known" if project else PROJECT_UNKNOWN)),
                   detail=str(data.get("detail") or ""))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Affiliation(cwd={self.cwd!r}, project={self.project!r}, "
                f"status={self.status!r})")


def describe_path(cwd) -> Affiliation:
    """The affiliation of whoever is standing in ``cwd``.

    Resolved through the *primary* checkout, so an agent working in
    `.worktrees/feat-x` is recorded as belonging to the project rather than to
    a directory that exists in order to be deleted. That is the whole reason
    `primary_repo_root` is called here and `Path.cwd()` is not used directly:
    a worktree path never matches a catalog row, so without it every agent
    following this repository's own worktree convention would be filed as
    unaffiliated -- the rule and the tooling disagreeing about the same fact.

    Never raises. A failure to place a directory is a blank field and a
    reason, because the caller is a send that must go through regardless.
    """
    if not cwd:
        return Affiliation(status=NO_LAUNCH_RECORD)
    text = str(cwd)
    try:
        root = project_paths.primary_repo_root(Path(text))
    except (OSError, ValueError):
        return Affiliation(cwd=text, status=UNPLACEABLE)
    try:
        found = project_paths.catalog_guid(root)
    except OSError as exc:
        return Affiliation(cwd=text,
                           status=project_paths.CATALOG_UNREADABLE,
                           detail=str(exc))
    if found.guid:
        return Affiliation(cwd=text, project=found.guid, status="known")
    return Affiliation(cwd=text, status=found.reason or PROJECT_UNKNOWN,
                       detail=found.detail)


def describe_instance(instance_id: str, restart_dir, read_tabs=None,
                      ) -> Affiliation:
    """The affiliation of the instance a message is addressed to.

    The launch record is read directly rather than through the operator's own
    lookup so that this module stays importable by tests without dragging in
    the whole CLI. ``read_tabs`` is the fallback the operator already uses and
    is injected, not imported, for the same reason.

    Deliberately tri-state about *absence*: an instance with no launch record
    is `no-launch-record`, and one whose record cannot be read is
    `unplaceable`. Collapsing those would let an unreadable state directory
    look exactly like an agent that has never been started.
    """
    if not instance_id:
        return Affiliation(status=NO_LAUNCH_RECORD)
    spec = Path(restart_dir) / f"{instance_id}.launch.json"
    present = file_present(spec)
    if present is None:
        return Affiliation(status=UNPLACEABLE, detail=str(spec))
    cwd = ""
    if present:
        try:
            cwd = json.loads(spec.read_text(encoding="utf-8")).get("cwd") or ""
        except ValueError:
            cwd = ""
        except OSError:
            return Affiliation(status=UNPLACEABLE, detail=str(spec))
    if not cwd and read_tabs is not None:
        try:
            tabs = read_tabs()
        except OSError:
            return Affiliation(status=UNPLACEABLE)
        if tabs is None:
            return Affiliation(status=UNPLACEABLE)
        entry = tabs.get(instance_id) or {}
        cwd = entry.get("cwd") or ""
    if not cwd:
        return Affiliation(status=NO_LAUNCH_RECORD)
    return describe_path(cwd)


def relationship(origin: Affiliation, destination: Affiliation) -> str:
    """What can be said about the pair.

    Only ``SAME_PROJECT`` and ``CROSS_PROJECT`` are claims. Everything else is
    ``PROJECT_UNKNOWN``, and the test is ``and`` rather than ``or`` on purpose:
    one known endpoint tells you nothing about the relationship, and the
    tempting shortcut -- "we know the sender's project and not the
    recipient's, so assume local" -- is exactly how an unknown becomes a
    reassuring lie.
    """
    if origin is None or destination is None:
        return PROJECT_UNKNOWN
    if origin.project and destination.project:
        return (SAME_PROJECT if origin.project == destination.project
                else CROSS_PROJECT)
    return PROJECT_UNKNOWN


def endpoints_of(msg: dict) -> tuple:
    """``(origin, destination)`` for a stored message.

    A message written before this existed has neither, and gets two unknown
    endpoints -- which is the truth about it. There is no migration and no
    inference: the 286 messages already on this machine are unknowable, and
    guessing their project from an agent's name is the guess the council ruled
    out by name.
    """
    if not isinstance(msg, dict):
        return (Affiliation(status=PROJECT_UNKNOWN),
                Affiliation(status=PROJECT_UNKNOWN))
    return (Affiliation.from_dict(msg.get(ORIGIN_KEY)),
            Affiliation.from_dict(msg.get(DESTINATION_KEY)))


def relationship_label(msg: dict) -> str:
    """The tri-state for a stored message, ready to render."""
    origin, destination = endpoints_of(msg)
    return relationship(origin, destination)


def attach(msg: dict, origin: Affiliation, destination: Affiliation) -> dict:
    """Add both endpoints to ``msg`` and return it.

    Mutates and returns the same object, matching `new_message`'s existing
    shape. Additive only: no existing key is touched, so a reader that has
    never heard of affiliation is unaffected.
    """
    msg[ORIGIN_KEY] = origin.as_dict()
    msg[DESTINATION_KEY] = destination.as_dict()
    return msg
