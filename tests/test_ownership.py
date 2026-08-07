"""Subproject path ownership: the only isolation that can fail a build.

The check itself is one line of spec -- `git diff --name-only main...HEAD`
must be a subset of one subproject's owned paths -- so everything here is
about the ways that line goes wrong. Each of the five doctrines in
`operator_ownership`'s docstring has a test that fails if it is abandoned,
because a permissive ownership check is indistinguishable from no ownership
check until the day two agents overwrite each other.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import operator_ownership as own


# ── path normalisation: git's syntax, not the platform's ────────
@pytest.mark.parametrize("text,expected", [
    ("services/api/main.py", ("services", "api", "main.py")),
    ("services\\api\\main.py", ("services", "api", "main.py")),
    ("/services/api", ("services", "api")),
    ("services/api/", ("services", "api")),
    ("./services/api", ("services", "api")),
    ("services//api", ("services", "api")),
    ("", ()),
    ("/", ()),
    (".", ()),
])
def test_a_path_is_read_as_segments_in_either_syntax(text, expected) -> None:
    """`git diff --name-only` emits forward slashes on every platform, so
    `os.path` -- which is whichever syntax is *running* -- is the wrong tool
    for both halves. A declaration hand-written with backslashes normalises
    rather than silently owning nothing."""
    assert own.normalize(text) == expected


def test_case_is_not_folded() -> None:
    """git is case-sensitive about tracked paths on every platform. Folding
    here lets `SERVICES/api` pass as `services/api` on a branch where git
    considers them two different files."""
    assert own.normalize("Services/API") != own.normalize("services/api")


# ── containment: the sibling-prefix trap ────────────────────────
def test_a_sibling_sharing_a_name_prefix_is_not_contained() -> None:
    """`startswith` says `services/api-v2/main.py` is under `services/api`,
    and the failure is silent in the permissive direction. The same
    comparison in `operator_worktree._is_inside` would have removed somebody
    else's checkout."""
    prefix = own.normalize("services/api")
    assert own.contains(prefix, own.normalize("services/api/main.py"))
    assert own.contains(prefix, own.normalize("services/api"))
    assert not own.contains(prefix, own.normalize("services/api-v2/main.py"))
    assert not own.contains(prefix, own.normalize("services/apiary"))


def test_an_empty_prefix_contains_everything() -> None:
    """True, and the reason `read_declaration` refuses to build one: a
    subproject owning the repository root is the check switched off."""
    assert own.contains((), own.normalize("anything/at/all"))


# ── reading the declaration ─────────────────────────────────────
def _write(root: Path, payload) -> Path:
    path = root / ".operator" / "subprojects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def test_no_declaration_reads_as_absent_not_as_empty(tmp_path: Path) -> None:
    assert own.read_declaration(tmp_path) is None


def test_a_declaration_is_read(tmp_path: Path) -> None:
    _write(tmp_path, {"subprojects": {
        "api": {"owns": ["services/api", "specs/api"]},
        "web": {"owns": ["apps/web"]}}})
    got = own.read_declaration(tmp_path)
    assert got.names() == ("api", "web")
    assert got.subprojects["api"] == (("services", "api"), ("specs", "api"))
    assert got.contracts == ()


def test_contracts_are_read(tmp_path: Path) -> None:
    _write(tmp_path, {"subprojects": {"api": {"owns": ["services/api"]}},
                      "contracts": ["specs/contracts"]})
    assert own.read_declaration(tmp_path).contracts == (
        ("specs", "contracts"),)


@pytest.mark.parametrize("payload,fragment", [
    ("{not json", "not valid JSON"),
    ([], "not an object"),
    ({}, "no 'subprojects' object"),
    ({"subprojects": []}, "not an object"),
    ({"subprojects": {"api": []}}, "not an object"),
    ({"subprojects": {"api": {}}}, "'owns' as NoneType"),
    ({"subprojects": {"api": {"owns": "services/api"}}}, "not a list"),
    ({"subprojects": {"api": {"owns": [7]}}}, "not a string"),
    ({"subprojects": {"api": {"owns": ["/"]}}}, "repository root"),
    ({"subprojects": {"api": {"owns": [""]}}}, "repository root"),
    ({"subprojects": {}, "contracts": "x"}, "not a list"),
    ({"subprojects": {}, "contracts": [7]}, "not a string"),
    ({"subprojects": {}, "contracts": ["/"]}, "repository root"),
])
def test_a_malformed_declaration_raises_rather_than_permitting(
        tmp_path: Path, payload, fragment: str) -> None:
    """Every one of these, collapsed to an empty declaration, reports every
    branch clean. That is this repository's most expensive defect class: a
    failed read becoming a confident empty answer."""
    _write(tmp_path, payload)
    with pytest.raises(own.OwnershipError) as exc:
        own.read_declaration(tmp_path)
    assert fragment in str(exc.value)


def test_an_unreadable_declaration_raises(tmp_path: Path, monkeypatch) -> None:
    path = _write(tmp_path, {"subprojects": {}})
    real = Path.read_text

    def refuse(self, *args, **kw):
        if self == path:
            raise OSError("permission denied")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(own.OwnershipError) as exc:
        own.read_declaration(tmp_path)
    monkeypatch.undo()
    assert "Refusing rather than reporting the branch clean" in str(exc.value)


def test_a_subproject_owning_the_root_is_refused(tmp_path: Path) -> None:
    """Not a style objection. `"owns": ["/"]` passes every branch, reads as
    a configured check, and is indistinguishable from one in a log."""
    _write(tmp_path, {"subprojects": {"api": {"owns": ["."]}}})
    with pytest.raises(own.OwnershipError):
        own.read_declaration(tmp_path)


# ── the check ───────────────────────────────────────────────────
@pytest.fixture
def declared(tmp_path: Path):
    _write(tmp_path, {
        "subprojects": {
            "api": {"owns": ["services/api", "specs/api"]},
            "web": {"owns": ["apps/web"]},
            "empty": {"owns": []},
        },
        "contracts": ["specs/contracts"]})
    return own.read_declaration(tmp_path)


def test_no_declaration_passes_under_its_own_code() -> None:
    """"The check does not apply" must never read as "the check ran and
    approved". They are the same boolean and different facts."""
    got = own.check(None, ["anything"])
    assert got.ok is True
    assert got.code == own.NO_DECLARATION
    assert got.code != own.OWNED


def test_a_branch_inside_one_subproject_is_owned(declared) -> None:
    got = own.check(declared, ["services/api/main.py", "specs/api/spec.md"])
    assert (got.ok, got.code, got.subproject) == (True, own.OWNED, "api")


def test_a_branch_that_leaves_its_subproject_is_refused(declared) -> None:
    got = own.check(declared, ["services/api/main.py", "apps/web/index.ts"],
                    subproject="api")
    assert got.ok is False
    assert got.code == own.UNOWNED
    assert got.offending == ("apps/web/index.ts",)


def test_a_declared_subproject_is_believed_over_the_paths(declared) -> None:
    """`web` owns `apps/web`, and an agent that said it was working `api`
    has still gone somewhere it did not say it was going. The declared
    intent is evidence, and disagreeing with it is the finding."""
    got = own.check(declared, ["apps/web/index.ts"], subproject="api")
    assert (got.ok, got.code) == (False, own.UNOWNED)


def test_a_branch_spanning_two_subprojects_is_refused(declared) -> None:
    got = own.check(declared, ["services/api/main.py", "apps/web/index.ts"])
    assert got.ok is False
    assert got.code in (own.UNOWNED, own.AMBIGUOUS)


def test_a_path_no_subproject_owns_is_refused(declared) -> None:
    got = own.check(declared, ["services/api/main.py", "tools/build.sh"])
    assert got.ok is False
    assert got.code == own.UNOWNED
    assert got.offending == ("tools/build.sh",)


def test_a_subproject_that_owns_nothing_owns_nothing(declared) -> None:
    """The tempting reading -- a subproject that has not said what it owns
    may touch anything -- inverts the rule exactly where it is least
    specified."""
    got = own.check(declared, ["services/api/main.py"], subproject="empty")
    assert (got.ok, got.code) == (False, own.UNOWNED)


def test_an_undeclared_subproject_is_refused_not_ignored(declared) -> None:
    got = own.check(declared, ["services/api/main.py"], subproject="mobile")
    assert got.ok is False
    assert got.code == own.UNKNOWN_SUBPROJECT
    assert got.candidates == ("api", "empty", "web")


def test_a_branch_changing_nothing_passes_under_its_own_code(declared) -> None:
    got = own.check(declared, [])
    assert (got.ok, got.code) == (True, own.NOTHING_CHANGED)


def test_a_sibling_directory_is_not_owned(declared) -> None:
    """The whole point of segment comparison, driven through `check`. A
    string prefix passes this and nothing else in the suite would notice."""
    got = own.check(declared, ["services/api-v2/main.py"], subproject="api")
    assert (got.ok, got.code) == (False, own.UNOWNED)


def test_an_ancestor_of_an_owned_directory_is_not_owned() -> None:
    """Containment runs one way. `services/api` owns what is beneath it,
    never what is above it -- a root-level file named `services` belongs to
    no subproject at all.

    Truncating the prefix to the path's length instead of comparing at the
    prefix's length reads the relation backwards, and it is wrong in both
    directions at once: a subproject is handed a path outside every
    boundary the declaration draws, and a path above a contract directory
    is refused as a contract change nobody made. Mutation found this; no
    test in the suite did."""
    owned = own.normalize("services/api")
    assert not own.contains(owned, own.normalize("services"))
    assert not own.contains(owned, own.normalize(""))
    assert own.contains(owned, own.normalize("services/api"))
    assert own.contains(owned, own.normalize("services/api/deep/x.py"))


# ── contracts ───────────────────────────────────────────────────
def test_a_contract_change_is_refused_by_default(declared) -> None:
    got = own.check(declared, ["specs/contracts/api.md"], subproject="api")
    assert got.ok is False
    assert got.code == own.CONTRACT
    assert got.offending == ("specs/contracts/api.md",)


def test_a_contract_change_is_refused_even_to_an_owner(tmp_path: Path) -> None:
    """A subproject whose owned paths contain a contract path still may not
    change it. Contracts are the interface between subprojects; an agent
    that needs one changed is an agent that needs to stop and ask."""
    _write(tmp_path, {"subprojects": {"api": {"owns": ["specs"]}},
                      "contracts": ["specs/contracts"]})
    declaration = own.read_declaration(tmp_path)
    got = own.check(declaration, ["specs/contracts/api.md"], subproject="api")
    assert (got.ok, got.code) == (False, own.CONTRACT)


def test_a_contract_change_passes_when_opted_into(declared) -> None:
    got = own.check(declared, ["specs/contracts/api.md",
                               "services/api/main.py"],
                    subproject="api", allow_contracts=True)
    assert (got.ok, got.code) == (False, own.UNOWNED)
    got = own.check(declared, ["specs/api/spec.md", "specs/contracts/api.md"],
                    subproject="api", allow_contracts=True)
    assert got.code == own.UNOWNED


def test_opting_in_does_not_also_grant_ownership(declared) -> None:
    """`--allow-contracts` waives one rule. A caller that read it as
    "anything goes" would have the contract path pass *and* the ownership
    check skipped, which is the failure that makes a flag dangerous."""
    got = own.check(declared, ["specs/contracts/api.md"], subproject="api",
                    allow_contracts=True)
    assert (got.ok, got.code) == (False, own.UNOWNED)


def test_a_contract_path_owned_by_the_subproject_passes_when_opted_in(
        tmp_path: Path) -> None:
    _write(tmp_path, {"subprojects": {"api": {"owns": ["specs"]}},
                      "contracts": ["specs/contracts"]})
    declaration = own.read_declaration(tmp_path)
    got = own.check(declaration, ["specs/contracts/api.md"],
                    subproject="api", allow_contracts=True)
    assert (got.ok, got.code) == (True, own.OWNED)


# ── nested declarations ─────────────────────────────────────────
def test_a_nested_declaration_resolves_to_the_specific_one(
        tmp_path: Path) -> None:
    """`apps` and `apps/web` are both reasonable to declare. A file under
    `apps/web` has two owners, so the *branch* has to pick -- which is why
    ambiguity is a branch-level verdict and not a per-file error."""
    _write(tmp_path, {"subprojects": {
        "shell": {"owns": ["apps"]},
        "web": {"owns": ["apps/web"]}}})
    declaration = own.read_declaration(tmp_path)
    assert own.owners_of(declaration,
                         own.normalize("apps/web/i.ts")) == ("shell", "web")
    got = own.check(declaration, ["apps/web/i.ts"])
    assert (got.ok, got.code) == (False, own.AMBIGUOUS)
    assert got.candidates == ("shell", "web")
    named = own.check(declaration, ["apps/web/i.ts"], subproject="web")
    assert (named.ok, named.code) == (True, own.OWNED)


def test_ambiguity_is_reported_as_ambiguity_not_as_ownership(
        tmp_path: Path) -> None:
    """Picking the first owner would pass the branch and record the wrong
    subproject, which is worse than refusing: the log then says the check
    ran and approved a subproject nobody chose."""
    _write(tmp_path, {"subprojects": {
        "a": {"owns": ["src"]}, "b": {"owns": ["src"]}}})
    got = own.check(own.read_declaration(tmp_path), ["src/x.py"])
    assert got.code == own.AMBIGUOUS
    assert got.ok is False


# ── the gate itself ─────────────────────────────────────────────
def test_passing_is_an_allow_list() -> None:
    """A verdict added later is refused until somebody decides it is safe.
    A deny-list would pass it by default, which is the wrong direction for
    the only check that can fail a build."""
    assert own.PASSING == frozenset(
        {own.NO_DECLARATION, own.NOTHING_CHANGED, own.OWNED})
    for code in (own.UNOWNED, own.CONTRACT, own.AMBIGUOUS,
                 own.UNKNOWN_SUBPROJECT):
        assert code not in own.PASSING


def test_every_verdict_code_agrees_with_its_ok_flag(declared) -> None:
    """`ok` and `code` are two spellings of one answer, and a caller may
    read either. They must not be able to disagree."""
    cases = [
        own.check(None, ["x"]),
        own.check(declared, []),
        own.check(declared, ["services/api/main.py"], subproject="api"),
        own.check(declared, ["tools/x"], subproject="api"),
        own.check(declared, ["specs/contracts/x"], subproject="api"),
        own.check(declared, ["services/api/a", "apps/web/b"]),
        own.check(declared, ["services/api/a"], subproject="nope"),
    ]
    seen = {verdict.code for verdict in cases}
    assert len(seen) >= 6, seen
    for verdict in cases:
        assert verdict.ok is (verdict.code in own.PASSING), verdict


# ── the CLI, where the exit code is the whole interface ─────────
def _repo(tmp_path: Path, declaration=None) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", "a@b.invalid")
    _git(root, "config", "user.name", "A")
    for rel in ("services/api/main.py", "apps/web/i.ts",
                "specs/contracts/api.md"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n", encoding="utf-8")
    if declaration is not None:
        _write(root, declaration)
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "first")
    _git(root, "checkout", "--quiet", "-b", "feat/x")
    return root


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout


def _touch(repo: Path, rel: str, message: str) -> None:
    (repo / rel).write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)


def _run(repo: Path, *args: str):
    return subprocess.run(
        [sys.executable, str(REPO / "copilot_operator.py"), "ownership",
         *args],
        cwd=str(repo), capture_output=True, encoding="utf-8",
        errors="replace", timeout=180)


REPO = Path(__file__).resolve().parent.parent

DECLARED = {"subprojects": {"api": {"owns": ["services/api"]},
                            "web": {"owns": ["apps/web"]}},
            "contracts": ["specs/contracts"]}


def test_cli_allows_a_branch_inside_one_subproject(tmp_path: Path) -> None:
    repo = _repo(tmp_path, DECLARED)
    _touch(repo, "services/api/main.py", "api")
    proc = _run(repo, "check")
    assert proc.returncode == 0, proc.stderr
    assert "owned" in proc.stdout


def test_cli_refuses_a_branch_that_left_its_subproject(tmp_path: Path) -> None:
    repo = _repo(tmp_path, DECLARED)
    _touch(repo, "services/api/main.py", "api")
    _touch(repo, "apps/web/i.ts", "web")
    proc = _run(repo, "check", "--project", "api")
    assert proc.returncode == 1
    assert "apps/web/i.ts" in proc.stderr


def test_cli_refuses_a_contract_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path, DECLARED)
    _touch(repo, "specs/contracts/api.md", "contract")
    proc = _run(repo, "check", "--project", "api", "--json")
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["code"] == "contract"


def test_cli_exits_2_when_the_declaration_will_not_parse(
        tmp_path: Path) -> None:
    """Distinct from 1, and that is the point. A hook that reads "the
    declaration would not parse" as "this branch is fine" is the failure
    this check exists to prevent, so an author who wants it has to write
    `|| true` where a reviewer can see it."""
    repo = _repo(tmp_path, "{ not json")
    _touch(repo, "services/api/main.py", "api")
    proc = _run(repo, "check")
    assert proc.returncode == 2
    assert "not valid JSON" in proc.stderr


def test_cli_exits_2_when_git_cannot_answer(tmp_path: Path) -> None:
    """`[]` would mean "this branch changed nothing", which passes. The two
    must not share a return value."""
    repo = _repo(tmp_path, DECLARED)
    _touch(repo, "apps/web/i.ts", "web")
    proc = _run(repo, "check", "--against", "no-such-ref")
    assert proc.returncode == 2
    assert "Refusing rather than reporting it clean" in proc.stderr


def test_cli_passes_a_repository_with_no_declaration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _touch(repo, "apps/web/i.ts", "anything")
    proc = _run(repo, "check")
    assert proc.returncode == 0
    assert "no-declaration" in proc.stdout


def test_cli_compares_against_the_merge_base(tmp_path: Path) -> None:
    """`main...HEAD` -- three dots. With two, a branch that has not been
    rebased is blamed for everything that landed on `main` behind it, and
    the gate refuses work nobody on this branch did."""
    repo = _repo(tmp_path, DECLARED)
    _touch(repo, "services/api/main.py", "mine")
    _git(repo, "checkout", "--quiet", "main")
    _touch(repo, "apps/web/i.ts", "somebody else's")
    _git(repo, "checkout", "--quiet", "feat/x")
    proc = _run(repo, "check", "--project", "api")
    assert proc.returncode == 0, proc.stdout + proc.stderr
