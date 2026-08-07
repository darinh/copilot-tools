#!/usr/bin/env python3
"""Which subproject a branch is allowed to have touched.

Three things isolate one agent's work from another's in a shared repository,
in increasing order of strength: the instruction you gave it, which is a
request and not a sandbox; its work-item claim, which stops a second agent
taking the same item but says nothing about files; and this, which is the
only one that can fail a build.

The check is the one in the spec, unchanged: `git diff --name-only
main...HEAD` must be a subset of the paths owned by exactly one subproject.
Everything below is about the ways that sentence goes wrong when written
carelessly.

**Paths here are repository-relative and pure syntax.** `git diff
--name-only` emits forward slashes on every platform, including Windows, so
nothing in this module touches `os.path`, `Path.resolve`, or the filesystem.
`os.path` is an alias for whichever syntax is *running*, and this data names
git's, which is a third thing. A declaration hand-written with backslashes is
normalised on the way in rather than compared as-is; refusing it would be
defensible, but a monorepo where the check silently owns nothing because
somebody typed a Windows separator is not.

**Containment is by segment, never by string prefix.** `services/api` does
not own `services/api-v2/main.py`, and `startswith` says it does. This
repository has already paid for that comparison once, in worktree removal
(`operator_worktree._is_inside`), where the same mistake would have removed
somebody else's checkout.

**An unreadable declaration is not an empty one.** A failed read that
resolves to "no rules" reports every branch clean, which is this
repository's most expensive defect class and the reason `project_features`
distinguishes never-written from unreadable. So does this: no declaration at
all means the repository is not a monorepo and there is nothing to check,
and that is a *different answer* from one nobody could open.

**An empty `owns` list owns nothing, and refuses everything.** The tempting
reading -- a subproject that has not said what it owns may touch anything --
inverts the rule at exactly the moment it is least specified.

**Contract paths are refused even to the subproject that also owns them.**
They are shared, they are the interface between subprojects, and an agent
that needs one changed is an agent that needs to stop and ask. Opting in is
explicit and per-branch, so the refusal is recoverable without being
routine.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "OwnershipError", "DECLARATION_NAME", "DECLARATION_VERSION",
    "Declaration", "Verdict", "NO_DECLARATION", "OWNED", "UNOWNED",
    "CONTRACT", "AMBIGUOUS", "UNKNOWN_SUBPROJECT", "NOTHING_CHANGED",
    "normalize", "contains", "read_declaration", "owners_of", "check",
]

#: Tracked, and at the repository root, because the rule is a fact about the
#: repository rather than about one clone of it. A declaration in
#: ``.git/info`` or in the per-project directory would be invisible to CI,
#: which is the one place this check has teeth.
DECLARATION_NAME = ".operator/subprojects.json"

DECLARATION_VERSION = 1

#: Verdict codes. Strings rather than an enum so a caller can print one and
#: a test can assert on it without importing this module's namespace.
NO_DECLARATION = "no-declaration"
NOTHING_CHANGED = "nothing-changed"
OWNED = "owned"
UNOWNED = "unowned"
CONTRACT = "contract"
AMBIGUOUS = "ambiguous"
UNKNOWN_SUBPROJECT = "unknown-subproject"

#: Verdicts a push may proceed on. Written as an allow-list rather than a
#: deny-list: a verdict added later is refused until somebody decides it is
#: safe, which is the direction a gate should fail in.
PASSING = frozenset({NO_DECLARATION, NOTHING_CHANGED, OWNED})


class OwnershipError(RuntimeError):
    """The declaration exists and could not be read or understood."""


def normalize(path: str) -> tuple:
    """A repository-relative path as a tuple of segments.

    Accepts either separator, drops `.` segments and empty ones, and lowers
    nothing -- git is case-sensitive about tracked paths on every platform,
    and folding case here would let `SERVICES/api` pass as `services/api` on
    a branch where git considers them two different files.

    A leading `/` is dropped rather than refused: `/services/api` in a
    declaration means the repository root, which is the only root there is.
    """
    text = str(path).replace("\\", "/")
    return tuple(part for part in text.split("/") if part and part != ".")


def contains(prefix: tuple, path: tuple) -> bool:
    """Whether `path` is `prefix` or sits beneath it, segment-wise.

    `("services", "api")` does not contain `("services", "api-v2", "x")`.
    A string comparison says it does, and the failure is silent in the
    permissive direction: a branch touching a sibling directory passes.

    An empty prefix contains everything, which is what `""` in a declaration
    would mean. Callers that must not grant that -- an empty `owns` list --
    reject it before getting here rather than making this function lie.
    """
    return path[:len(prefix)] == prefix


@dataclass(frozen=True)
class Declaration:
    """What the repository says about who owns what."""

    #: subproject name -> tuple of owned prefixes, each a segment tuple.
    subprojects: dict = field(default_factory=dict)
    #: Prefixes no subproject work may touch without opting in.
    contracts: tuple = ()
    #: Where it was read from, for a message that names the file to edit.
    source: str = ""

    def names(self) -> tuple:
        return tuple(sorted(self.subprojects))


@dataclass(frozen=True)
class Verdict:
    """The answer, and enough of the evidence to argue with it."""

    code: str
    ok: bool
    subproject: str = ""
    offending: tuple = ()
    candidates: tuple = ()
    detail: str = ""


def read_declaration(root, name: str = DECLARATION_NAME):
    """The declaration at `root`, or `None` when there is not one.

    `None` means *absent*, and nothing else. A file that exists and cannot
    be read, or does not parse, or is the wrong shape, raises -- because
    collapsing those into `None` makes the check report every branch clean
    at the moment it stopped being able to tell.
    """
    path = Path(root)
    for part in normalize(name):
        path = path / part
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OwnershipError(
            f"Cannot read the subproject declaration {path}: {exc}. "
            f"Refusing rather than reporting the branch clean.") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise OwnershipError(
            f"The subproject declaration {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise OwnershipError(
            f"The subproject declaration {path} holds "
            f"{type(data).__name__}, not an object.")
    if "subprojects" not in data:
        raise OwnershipError(
            f"The subproject declaration {path} has no 'subprojects' object. "
            f"An empty one is spelled \"subprojects\": {{}} and means no "
            f"subproject may change anything; a file without the key was not "
            f"written by this tool.")
    subprojects = data["subprojects"]
    if not isinstance(subprojects, dict):
        raise OwnershipError(
            f"The subproject declaration {path} has 'subprojects' as "
            f"{type(subprojects).__name__}, not an object.")
    owned = {}
    for name_, entry in subprojects.items():
        if not isinstance(entry, dict):
            raise OwnershipError(
                f"Subproject {name_!r} in {path} is "
                f"{type(entry).__name__}, not an object.")
        paths = entry.get("owns")
        if not isinstance(paths, list):
            raise OwnershipError(
                f"Subproject {name_!r} in {path} has 'owns' as "
                f"{type(paths).__name__}, not a list.")
        prefixes = []
        for item in paths:
            if not isinstance(item, str):
                raise OwnershipError(
                    f"Subproject {name_!r} in {path} owns "
                    f"{type(item).__name__}, not a string.")
            segments = normalize(item)
            if not segments:
                raise OwnershipError(
                    f"Subproject {name_!r} in {path} owns {item!r}, which "
                    f"names the repository root. A subproject that owns "
                    f"everything is the check switched off; delete the "
                    f"subproject instead, so it says so.")
            if ".." in segments:
                raise OwnershipError(
                    f"Subproject {name_!r} in {path} owns {item!r}, which "
                    f"climbs out of the repository. Refused here rather than "
                    f"anywhere downstream: this declaration is also what "
                    f"decides where `operator projects` writes a subproject's "
                    f"instructions file, and a prefix that escapes the root "
                    f"is a write into a directory nobody in this repository "
                    f"named.")
            prefixes.append(segments)
        owned[name_] = tuple(prefixes)
    contracts = data.get("contracts", [])
    if not isinstance(contracts, list):
        raise OwnershipError(
            f"The subproject declaration {path} has 'contracts' as "
            f"{type(contracts).__name__}, not a list.")
    shared = []
    for item in contracts:
        if not isinstance(item, str):
            raise OwnershipError(
                f"A contract path in {path} is {type(item).__name__}, "
                f"not a string.")
        segments = normalize(item)
        if not segments:
            raise OwnershipError(
                f"A contract path in {path} names the repository root, so "
                f"every change would be a contract change.")
        if ".." in segments:
            raise OwnershipError(
                f"A contract path in {path} climbs out of the repository: "
                f"{item!r}. A contract is a path inside this repository that "
                f"more than one subproject depends on; one outside it is not "
                f"a thing this check can see, let alone protect.")
        shared.append(segments)
    return Declaration(subprojects=owned, contracts=tuple(shared),
                       source=str(path))


def owners_of(declaration: Declaration, path: tuple) -> tuple:
    """Every subproject whose owned paths contain `path`, sorted.

    More than one is possible and is not an error here: nested declarations
    (`apps/` and `apps/web/`) are a reasonable thing to write, and it is the
    *branch* that has to resolve to one subproject, not each file.
    """
    return tuple(sorted(
        name for name, prefixes in declaration.subprojects.items()
        if any(contains(prefix, path) for prefix in prefixes)))


def check(declaration, changed, *, subproject: "str | None" = None,
          allow_contracts: bool = False) -> Verdict:
    """Whether a branch that changed `changed` may be pushed.

    `declaration` is `None` when the repository has no declaration, which
    means it is not a monorepo and there is nothing here to enforce. That is
    a pass, and it is reported under its own code rather than as `OWNED`, so
    "the check does not apply" never reads as "the check ran and approved".

    `subproject` names the subproject the branch is claimed to be working.
    Left out, it is inferred: the branch must resolve to exactly one. Given,
    it is checked -- an agent that says it is working `api` and has touched
    `apps/web` is refused even though `web` owns that path, because the
    declared intent is evidence and disagreeing with it is the finding.
    """
    paths = tuple(normalize(p) for p in changed)
    paths = tuple(p for p in paths if p)
    if declaration is None:
        return Verdict(NO_DECLARATION, True, detail=(
            "No subproject declaration, so this repository declares no "
            "ownership boundaries and there is nothing to check."))
    if subproject is not None and subproject not in declaration.subprojects:
        return Verdict(UNKNOWN_SUBPROJECT, False, subproject=subproject,
                       candidates=declaration.names(), detail=(
                           f"{declaration.source} does not declare a "
                           f"subproject named {subproject!r}."))
    if not paths:
        return Verdict(NOTHING_CHANGED, True, subproject=subproject or "",
                       detail="The branch changes no files.")
    if not allow_contracts:
        touched = tuple(sorted(
            "/".join(p) for p in paths
            if any(contains(c, p) for c in declaration.contracts)))
        if touched:
            return Verdict(CONTRACT, False, subproject=subproject or "",
                           offending=touched, detail=(
                               "Contract paths are shared between "
                               "subprojects. Changing one is a contract "
                               "change: stop and ask, or opt in explicitly."))
    if subproject is None:
        common = None
        for path in paths:
            owners = set(owners_of(declaration, path))
            common = owners if common is None else (common & owners)
        common = common or set()
        if not common:
            unowned = tuple(sorted(
                "/".join(p) for p in paths
                if not owners_of(declaration, p)))
            return Verdict(
                UNOWNED if unowned else AMBIGUOUS, False,
                offending=unowned, candidates=declaration.names(),
                detail=("No single subproject owns every changed path."
                        if not unowned else
                        f"{declaration.source} gives no subproject these "
                        f"paths."))
        if len(common) > 1:
            return Verdict(AMBIGUOUS, False, candidates=tuple(sorted(common)),
                           detail=(
                               "More than one subproject owns every changed "
                               "path, so the branch does not identify one. "
                               "Name it explicitly."))
        return Verdict(OWNED, True, subproject=common.pop())
    prefixes = declaration.subprojects[subproject]
    outside = tuple(sorted(
        "/".join(p) for p in paths
        if not any(contains(prefix, p) for prefix in prefixes)))
    if outside:
        return Verdict(UNOWNED, False, subproject=subproject,
                       offending=outside,
                       candidates=declaration.names(), detail=(
                           f"Subproject {subproject!r} does not own these "
                           f"paths. Work that genuinely needs them is a "
                           f"contract change: stop and ask."))
    return Verdict(OWNED, True, subproject=subproject)
