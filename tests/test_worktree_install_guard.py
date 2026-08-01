"""The build backend must refuse an editable install of a linked worktree.

``pip install -e`` records its *source directory* in the interpreter's import
path and points the console scripts at it. Every agent on this project works
in a worktree, and a worktree exists in order to be deleted, so an editable
install made from one is a machine-wide breakage armed to go off when someone
else finishes the branch. It has gone off twice.

The detector is in :mod:`worktree_guard_backend`. What is tested here is both
directions of every branch it has, because the expensive failure mode of this
particular guard is not a false refusal -- that is one environment variable on
a machine where somebody is watching a command they just typed -- but a false
*pass*, which is silent, machine-wide, and surfaces days later for a person
who did not cause it.

Two things get their own tests for reasons worth naming:

* **A submodule is not a worktree.** Both have a ``.git`` *file* rather than a
  directory, and the obvious detector ("``.git`` is a file") refuses both. A
  submodule is a durable checkout; refusing it would be a bug that only shows
  up in someone else's repository layout.
* **Real git, not only fixtures.** ``test_a_real_git_worktree_is_refused``
  builds an actual repository with ``git worktree add`` and runs the hook in a
  subprocess. A fixture written from my reading of the format cannot fail if
  my reading is wrong, which is the whole risk here -- git chooses the bytes,
  not this file.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import worktree_guard_backend as guard

REPO = Path(__file__).resolve().parent.parent

#: Every hook a PEP 517 frontend may call for an editable install. All three
#: are guarded, so which one a given frontend reaches first cannot matter.
EDITABLE_HOOKS = ("get_requires_for_build_editable",
                  "prepare_metadata_for_build_editable",
                  "build_editable")

#: The hooks that cannot produce an editable install. Delegated unguarded --
#: a wheel or an sdist copies files out of the worktree and records nothing
#: about where they came from.
COPYING_HOOKS = ("get_requires_for_build_wheel", "get_requires_for_build_sdist",
                 "prepare_metadata_for_build_wheel", "build_wheel",
                 "build_sdist")


def _checkout(root: Path, dot_git: str | None) -> Path:
    """A directory shaped like a checkout, with ``.git`` written as given.

    ``None`` means no ``.git`` at all -- an unpacked sdist or an export.
    """
    root.mkdir(parents=True, exist_ok=True)
    if dot_git is not None:
        (root / ".git").write_text(dot_git, encoding="utf-8")
    return root


def _worktree_gitdir(repo: Path, name: str) -> Path:
    """A git directory laid out the way git lays a linked worktree's out.

    The marker files are not decoration: ``commondir`` is what distinguishes a
    linked worktree from every other checkout with a ``.git`` file, and
    ``config`` is what corroborates that the path the refusal message points
    at is really a checkout. A fixture missing them cannot fail the code that
    reads them.
    """
    gitdir = repo / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return gitdir


# ── classify_checkout: the three verdicts ───────────────────────────────

def test_a_primary_checkout_has_a_git_directory(tmp_path):
    root = tmp_path / "primary"
    (root / ".git").mkdir(parents=True)
    assert guard.classify_checkout(root) == (guard.PRIMARY, "")


def test_a_tree_with_no_git_at_all_is_primary(tmp_path):
    """An unpacked sdist has no ``.git``. It is not disposable, and it is the
    shape a build from a released artifact has."""
    root = _checkout(tmp_path / "sdist", None)
    assert guard.classify_checkout(root) == (guard.PRIMARY, "")


def test_a_linked_worktree_is_detected(tmp_path):
    root = _checkout(tmp_path / "wt",
                     "gitdir: /repo/.git/worktrees/feat-x\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.WORKTREE
    assert detail.replace("\\", "/").endswith(".git/worktrees/feat-x")


def test_a_submodule_is_not_a_worktree(tmp_path):
    """The false positive the naive detector produces. A submodule's ``.git``
    is a file too; it points into ``modules/`` and it is durable."""
    root = _checkout(tmp_path / "sub", "gitdir: /super/.git/modules/libfoo\n")
    assert guard.classify_checkout(root) == (guard.PRIMARY, "")


def test_a_relative_gitdir_is_resolved_against_the_checkout(tmp_path):
    """git writes a relative ``gitdir:`` for a worktree added with relative
    paths. Read literally that path is meaningless from any other directory."""
    root = _checkout(tmp_path / "a" / "b" / "wt",
                     "gitdir: ../../.git/worktrees/feat-y\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.WORKTREE
    assert ".." not in detail
    assert Path(detail) == tmp_path / "a" / ".git" / "worktrees" / "feat-y"


def test_a_git_file_with_no_gitdir_line_is_unknown(tmp_path):
    root = _checkout(tmp_path / "odd", "this is not a git link\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.UNKNOWN
    assert "gitdir" in detail


def test_an_unrecognised_gitdir_target_is_unknown(tmp_path):
    root = _checkout(tmp_path / "odd", "gitdir: /somewhere/else\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.UNKNOWN
    assert "neither a worktree nor a submodule" in detail


def test_a_separate_git_dir_checkout_is_primary(tmp_path):
    """``git clone --separate-git-dir`` puts the git directory anywhere the
    user likes, so the checkout has a ``.git`` file pointing at a path in
    neither well-known location. It is durable, and refusing it would be this
    guard being wrong about somebody else's ordinary layout."""
    elsewhere = tmp_path / "var" / "git" / "copilot-tools.git"
    elsewhere.mkdir(parents=True)
    (elsewhere / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    root = _checkout(tmp_path / "work", f"gitdir: {elsewhere}\n")
    assert guard.classify_checkout(root) == (guard.PRIMARY, "")


def test_a_worktree_in_an_unusual_location_is_still_detected(tmp_path):
    """``commondir`` is git's own marker for a *linked* worktree, and it is
    what the path shape falls back on. A worktree whose git directory is not
    where this guard expects is still a worktree."""
    gitdir = tmp_path / "elsewhere" / "feat-x"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../../repo/.git\n", encoding="utf-8")
    root = _checkout(tmp_path / "wt", f"gitdir: {gitdir}\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.WORKTREE
    assert detail == str(gitdir)


def test_a_gitdir_that_is_not_there_at_all_is_unknown(tmp_path):
    """Neither marker can be read, so nothing is known -- and the guard says
    that rather than picking whichever answer is convenient."""
    root = _checkout(tmp_path / "odd",
                     f"gitdir: {tmp_path / 'gone' / 'missing'}\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.UNKNOWN
    assert "neither a worktree nor a submodule" in detail


def test_an_unreadable_git_file_is_unknown_not_primary(tmp_path, monkeypatch):
    """A read that failed must not be indistinguishable from a read that said
    'primary'. Folding the two is the bug class this repository keeps finding."""
    root = _checkout(tmp_path / "denied", "gitdir: /repo/.git/worktrees/z\n")
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if self.name == ".git":
            raise PermissionError(13, "denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.UNKNOWN
    assert "could not be read" in detail


def test_an_unexaminable_git_path_is_unknown_not_primary(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "denied", None)

    def denied(self):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "is_file", denied)
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.UNKNOWN
    assert "could not be examined" in detail


# ── primary_checkout_of ─────────────────────────────────────────────────

def test_the_primary_checkout_is_read_from_commondir(tmp_path):
    gitdir = _worktree_gitdir(tmp_path / "repo", "feat-x")
    assert guard.primary_checkout_of(gitdir) == tmp_path / "repo"


def test_the_primary_checkout_falls_back_to_position(tmp_path):
    """No ``commondir`` to read: the standard layout still gives the answer."""
    gitdir = _worktree_gitdir(tmp_path / "repo", "feat-x")
    (gitdir / "commondir").unlink()
    assert guard.primary_checkout_of(gitdir) == tmp_path / "repo"


def test_an_unrecognisable_layout_yields_no_guess(tmp_path):
    """A wrong path in a 'run this instead' line is worse than no line."""
    assert guard.primary_checkout_of(tmp_path / "nowhere" / "at" / "all") is None


def test_a_candidate_that_is_not_really_a_git_directory_yields_no_guess(tmp_path):
    """The positional fallback is a guess, and under a bind mount or a
    relative ``gitdir:`` it produces a path that is shaped right and is not a
    checkout. Shape alone must not be enough to print a command."""
    gitdir = tmp_path / "mount" / ".git" / "worktrees" / "feat-x"
    gitdir.mkdir(parents=True)          # no config, no HEAD: not a git dir
    assert guard.primary_checkout_of(gitdir) is None


# ── check_editable_source: the decision ─────────────────────────────────

def test_a_primary_checkout_is_allowed(tmp_path):
    root = tmp_path / "primary"
    (root / ".git").mkdir(parents=True)
    assert guard.check_editable_source(root) is None


def test_a_worktree_is_refused_with_a_usable_message(tmp_path, capsys):
    gitdir = _worktree_gitdir(tmp_path / "repo", "feat-x")
    root = _checkout(tmp_path / "repo" / ".worktrees" / "feat-x",
                     f"gitdir: {gitdir}\n")

    with pytest.raises(guard.EditableInstallFromWorktree) as excinfo:
        guard.check_editable_source(root)

    message = str(excinfo.value)
    # The remedy, not merely the complaint: the refusal has to leave the
    # reader able to do the thing they were trying to do.
    assert str(tmp_path / "repo") in message
    assert "pip install -e" in message
    assert guard.OVERRIDE_ENV in message
    # Printed too -- a frontend may render a backend exception as one line.
    assert guard.OVERRIDE_ENV in capsys.readouterr().err


def test_an_unclassifiable_checkout_is_refused_without_claiming_it_is_one(tmp_path):
    """The rule this project earned the hard way: a message written where its
    subject is not knowable may assert neither an outcome nor a cause."""
    root = _checkout(tmp_path / "odd", "gitdir: /somewhere/else\n")
    with pytest.raises(guard.EditableInstallFromWorktree) as excinfo:
        guard.check_editable_source(root)
    message = str(excinfo.value)
    assert "could not be classified" in message
    assert "refusing to build an editable install from a linked git worktree" \
        not in message.lower()


def test_the_override_allows_an_install_from_a_worktree(tmp_path, monkeypatch):
    root = _checkout(tmp_path / "wt", "gitdir: /repo/.git/worktrees/feat-x\n")
    monkeypatch.setenv(guard.OVERRIDE_ENV, "1")
    assert guard.check_editable_source(root) is None


@pytest.mark.parametrize("value", ["", "0", "false", "False", "no", "off",
                                   "  0  "])
def test_a_denial_is_not_consent(value, tmp_path, monkeypatch):
    """``bool("0")`` is ``True``, so the obvious truthiness test reads
    ``COPILOT_TOOLS_ALLOW_WORKTREE_INSTALL=0`` -- a script saying "leave the
    guard on" -- as permission to switch it off."""
    root = _checkout(tmp_path / "wt", "gitdir: /repo/.git/worktrees/feat-x\n")
    monkeypatch.setenv(guard.OVERRIDE_ENV, value)
    with pytest.raises(guard.EditableInstallFromWorktree):
        guard.check_editable_source(root)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "please"])
def test_anything_that_is_not_a_denial_is_consent(value, tmp_path, monkeypatch):
    """The negative control. A denial list that swallowed every value would
    pass the test above and disable the override entirely."""
    root = _checkout(tmp_path / "wt", "gitdir: /repo/.git/worktrees/feat-x\n")
    monkeypatch.setenv(guard.OVERRIDE_ENV, value)
    assert guard.check_editable_source(root) is None


def test_a_nul_byte_in_the_git_file_is_refused_not_crashed(tmp_path):
    """On 3.10 -- the floor this project supports -- ``Path`` raises
    ``ValueError`` on an embedded NUL. Unhandled inside a PEP 517 hook that
    reads as a broken package rather than as an unreadable checkout."""
    root = _checkout(tmp_path / "corrupt", "gitdir: /repo/\x00/worktrees/x\n")
    verdict, detail = guard.classify_checkout(root)
    assert verdict == guard.UNKNOWN
    assert "not a usable path" in detail


def test_a_nul_byte_in_commondir_yields_no_guess(tmp_path):
    gitdir = _worktree_gitdir(tmp_path / "repo", "feat-x")
    (gitdir / "commondir").write_text("..\x00/..\n", encoding="utf-8")
    assert guard.primary_checkout_of(gitdir) is None


def test_the_source_defaults_to_the_working_directory(tmp_path, monkeypatch):
    """PEP 517 runs every hook with cwd at the source root, and that is the
    only way the backend is ever told which directory it is building."""
    root = _checkout(tmp_path / "wt", "gitdir: /repo/.git/worktrees/feat-x\n")
    monkeypatch.chdir(root)
    with pytest.raises(guard.EditableInstallFromWorktree):
        guard.check_editable_source()


# ── the hooks ───────────────────────────────────────────────────────────

class _Recorder:
    """Stands in for ``setuptools.build_meta`` and raises like it would.

    Every hook returns a sentinel and records that it was called. Nothing here
    delegates to real setuptools, so a hook that was supposed to refuse and
    did not is caught by the recording rather than by a build succeeding.
    """

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name not in EDITABLE_HOOKS + COPYING_HOOKS:
            raise AttributeError(name)

        def hook(*args, **kwargs):
            self.calls.append(name)
            return f"delegated:{name}"

        return hook


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(guard, "_setuptools", lambda: rec)
    return rec


@pytest.mark.parametrize("hook", EDITABLE_HOOKS)
def test_every_editable_hook_refuses_a_worktree(hook, tmp_path, monkeypatch,
                                                recorder):
    root = _checkout(tmp_path / "wt", "gitdir: /repo/.git/worktrees/feat-x\n")
    monkeypatch.chdir(root)
    with pytest.raises(guard.EditableInstallFromWorktree):
        getattr(guard, hook)("arg")
    assert recorder.calls == [], f"{hook} delegated before refusing"


@pytest.mark.parametrize("hook", EDITABLE_HOOKS)
def test_every_editable_hook_delegates_from_a_primary_checkout(hook, tmp_path,
                                                               monkeypatch,
                                                               recorder):
    """The negative control. A guard that refused everything would pass every
    test above and break every install."""
    root = tmp_path / "primary"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    assert getattr(guard, hook)("arg") == f"delegated:{hook}"
    assert recorder.calls == [hook]


@pytest.mark.parametrize("hook", COPYING_HOOKS)
def test_a_copying_hook_is_never_blocked_by_a_worktree(hook, tmp_path,
                                                       monkeypatch, recorder):
    """A wheel or sdist built from a worktree contains copies and no reference
    to where they came from. Refusing those would be scope creep with a cost."""
    root = _checkout(tmp_path / "wt", "gitdir: /repo/.git/worktrees/feat-x\n")
    monkeypatch.chdir(root)
    assert getattr(guard, hook)("arg") == f"delegated:{hook}"
    assert recorder.calls == [hook]


def test_the_backend_exposes_every_hook_setuptools_does():
    """``pyproject.toml`` names this module as the build backend, so a hook
    setuptools offers and this module forgot is a capability the project
    silently loses -- and the frontend reports it as "backend does not support
    X", never as "the wrapper is incomplete"."""
    from setuptools import build_meta

    expected = {name for name in dir(build_meta)
                if name.startswith(("build_", "get_requires_", "prepare_"))
                and callable(getattr(build_meta, name))}
    missing = {name for name in expected if not callable(getattr(guard, name, None))}
    assert not missing, f"worktree_guard_backend does not re-export {missing}"


def test_the_module_imports_without_setuptools(tmp_path):
    """``setup_tools`` imports this module on a machine whose problem may be
    that packaging is missing, and the whole file is unreachable if importing
    it needs the thing that is not there yet."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, importlib.util\n"
         "sys.modules['setuptools'] = None\n"
         f"spec = importlib.util.spec_from_file_location('g', r'{REPO / 'worktree_guard_backend.py'}')\n"
         "m = importlib.util.module_from_spec(spec)\n"
         "spec.loader.exec_module(m)\n"
         "print(m.classify_checkout('.')[0])\n"],
        cwd=str(tmp_path), capture_output=True, encoding="utf-8",
        errors="replace")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == guard.PRIMARY


# ── the wiring, and real git ────────────────────────────────────────────

def test_pyproject_actually_names_this_backend():
    """Nothing else in the suite reads ``[build-system]``. A typo there is a
    project that cannot be installed at all, and the tests would all pass."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[build-system]", 1)[1].split("\n[", 1)[0]
    assert re.search(r'^build-backend\s*=\s*"worktree_guard_backend"\s*$',
                     block, re.M), block
    assert re.search(r'^backend-path\s*=\s*\["\."\]\s*$', block, re.M), block


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_real_git_worktree_is_refused(tmp_path):
    """git chooses the bytes in ``.git``, so a fixture cannot falsify a
    misreading of the format. This builds a real worktree and asks the hook."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

    def git(*args, cwd=repo):
        proc = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                              capture_output=True, encoding="utf-8",
                              errors="replace")
        assert proc.returncode == 0, f"git {args}: {proc.stderr}"
        return proc

    git("init", "-q")
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "init")
    worktree = tmp_path / "wt"
    git("worktree", "add", "-q", str(worktree), "-b", "feat-x")

    assert guard.classify_checkout(repo)[0] == guard.PRIMARY
    verdict, detail = guard.classify_checkout(worktree)
    assert verdict == guard.WORKTREE, detail
    assert guard.primary_checkout_of(Path(detail)) == repo

    with pytest.raises(guard.EditableInstallFromWorktree) as excinfo:
        guard.check_editable_source(worktree)
    assert str(repo) in str(excinfo.value)


# ── setup's half: do not ask for the install the backend would refuse ───

def test_setup_installs_the_repo_root_when_it_is_the_primary_checkout(monkeypatch):
    """The no-op branch, and by itself indistinguishable from the behaviour
    this replaced -- which is what makes it the control for the two tests
    below rather than evidence on its own."""
    import setup_tools

    monkeypatch.setattr(setup_tools.project_paths, "primary_repo_root",
                        lambda start: setup_tools.REPO_ROOT)
    assert setup_tools.install_source() == (setup_tools.REPO_ROOT, None)


def test_setup_redirects_a_worktree_to_the_primary_checkout(monkeypatch, tmp_path):
    import setup_tools

    primary = tmp_path / "primary"
    monkeypatch.setattr(setup_tools.project_paths, "primary_repo_root",
                        lambda start: primary)
    assert setup_tools.install_source() == (primary, setup_tools.REPO_ROOT)


def test_setup_installs_the_primary_checkout_and_says_so(monkeypatch, tmp_path,
                                                         capsys):
    """The negative control for the redirect is the message: an agent that is
    silently handed a different source directory learns nothing, and the whole
    reason this exists is that the mistake teaches nobody anything."""
    import setup_tools

    primary = tmp_path / "primary"
    installed = []
    monkeypatch.setattr(setup_tools.project_paths, "primary_repo_root",
                        lambda start: primary)
    monkeypatch.setattr(setup_tools.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup_tools, "pip_install",
                        lambda args: installed.append(args) or True)
    monkeypatch.setattr(setup_tools, "persist_user_path", lambda d: None)

    setup_tools.install_package(assume_yes=True)

    assert installed == [["-e", str(primary)]]
    out = capsys.readouterr().out
    assert str(setup_tools.REPO_ROOT) in out
    assert str(primary) in out
