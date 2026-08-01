"""Subprocess output survives bytes the locale codec cannot decode.

The companion static scan, ``test_subprocess_encoding_conformance.py``, checks
that every decoding call *names* an encoding and an error policy. This file
checks that the code behind those calls actually behaves when the decode is
hard, and that the guards added alongside them hold.

Two properties, and they fail in different directions:

**Decoding must not lose the answer.** ``text=True`` decodes with
``locale.getpreferredencoding(False)`` -- cp1252 on Windows -- while git, node
and PowerShell all emit UTF-8. Naming ``encoding="utf-8"`` is what makes the
round trip exact rather than luck, and ``errors="replace"`` is what keeps a
byte that is not valid UTF-8 *either* from killing the read.

**A read that failed must not read as a read that returned nothing.** This is
the shape the bug actually arrived in, and it is the one no ``returncode``
check can see. Measured on this repository, 2026-08-01, against a git
repository whose path contained U+0401::

    Exception in thread Thread-9 (_readerthread):
    UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 ...
    returncode: 0
    stdout is None: True

``subprocess.run`` returned normally with ``returncode == 0``. The failure was
on a reader thread, so the only trace of it in the parent was ``stdout is
None``, and every guard in this repository is spelled ``if proc.returncode !=
0``. The tests below drive that exact shape through each caller with a fake
``subprocess.run`` -- ``returncode=0``, ``stdout=None`` -- because it is the
combination that gets past the guard that exists.

Those fakes are not a substitute for running something: the round-trip tests
in this file spawn real child processes that emit real undecodable bytes, on
every platform, without needing the machine's locale to be cp1252.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import operator_runner
import project_paths
import setup_tools

ROOT = Path(__file__).resolve().parent.parent

#: UTF-8 ``d0 81``. 0x81 is one of the five bytes cp1252 leaves undefined, so
#: this character is undecodable there rather than merely wrong -- and a wrong
#: decode is not what this is about, a *raising* one is.
CYRILLIC_IO = "\u0401"

#: Not valid UTF-8 in any position: a continuation byte with no lead. Nothing
#: can decode this, which is what ``errors="replace"`` is for.
LONE_CONTINUATION = b"\x81"


def _completed(stdout=None, stderr=None, returncode=0):
    """The shape a failed decode leaves behind: exit 0, no stdout object."""
    return subprocess.CompletedProcess(
        args=["irrelevant"], returncode=returncode, stdout=stdout,
        stderr=stderr)


def _emit(data: bytes) -> list[str]:
    """A child that writes exactly ``data`` to its stdout, as raw bytes."""
    return [sys.executable, "-c",
            "import sys; sys.stdout.buffer.write(%r); sys.stdout.buffer.flush()"
            % (data,)]


# ── project_paths.primary_repo_root ─────────────────────────────
def test_repo_root_survives_a_path_the_locale_cannot_decode(tmp_path):
    """The end-to-end case, run for real rather than simulated.

    git prints the worktree path back as UTF-8 bytes. Under the old
    ``text=True`` this raised on Windows; naming the encoding makes the answer
    the same on every platform.

    The question is asked *from inside a linked worktree* for a reason. Asked
    from the primary checkout, the expected answer and the fallback are the
    same path -- ``primary_repo_root`` hands ``start`` back when it cannot get
    an answer -- so the assertion would hold with the decode still broken.
    Measured: with ``text=True`` restored, that version of this test passed.
    From a worktree the two differ, and only a real answer can produce the
    primary root.
    """
    root = tmp_path / f"repo{CYRILLIC_IO}name"
    try:
        root.mkdir()
    except (OSError, UnicodeError):  # pragma: no cover - filesystem-dependent
        pytest.skip("this filesystem will not hold the character under test")

    def git(*args):
        subprocess.run(["git", *args], cwd=str(root), check=True,
                       capture_output=True, encoding="utf-8",
                       errors="replace")

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Test")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "init")
    git("worktree", "add", ".worktrees/feat-x", "-b", "feat/x")
    worktree = root / ".worktrees" / "feat-x"

    found = project_paths.primary_repo_root(worktree)

    assert found.resolve() != worktree.resolve(), (
        "the worktree path came back unchanged, which is what this function "
        "returns when it could not get an answer at all")
    assert found.resolve() == root.resolve()
    assert CYRILLIC_IO in str(found), (
        f"the character was lost or mangled in {found!r}")


def test_repo_root_falls_back_when_the_output_could_not_be_read(tmp_path,
                                                                monkeypatch):
    """exit 0 with no stdout is git failing to answer, not a repo with no
    worktrees. The contract is to hand ``start`` back, and before the guard
    this raised ``AttributeError`` from ``None.splitlines()`` instead."""
    monkeypatch.setattr(project_paths.subprocess, "run",
                        lambda *a, **k: _completed(stdout=None))
    start = tmp_path / "somewhere"
    start.mkdir()

    assert project_paths.primary_repo_root(start) == start


def test_repo_root_still_reads_a_normal_answer(tmp_path, monkeypatch):
    """The control for the test above: with stdout present the guard must not
    fire, or "always fall back" would pass it just as well."""
    monkeypatch.setattr(
        project_paths.subprocess, "run",
        lambda *a, **k: _completed(stdout="worktree /elsewhere/primary\n"))
    start = tmp_path / "somewhere"
    start.mkdir()

    assert project_paths.primary_repo_root(start) == Path("/elsewhere/primary")


# ── setup_tools.capture ─────────────────────────────────────────
def test_capture_round_trips_non_ascii_utf8():
    """Exact, not approximate: the bytes go out as UTF-8 and come back as the
    same characters, whatever the machine's locale happens to be."""
    text = f"v1.0 {CYRILLIC_IO} \u2014 build"
    ok, out = setup_tools.capture(_emit(text.encode("utf-8")))

    assert ok
    assert out == text


def test_capture_replaces_bytes_nothing_can_decode():
    """The other half of the fix. ``encoding="utf-8"`` alone leaves the codec
    strict, so an invalid byte still kills the reader thread and still hands
    back ``stdout is None``; ``errors="replace"`` is what makes this a
    substitution rather than a loss."""
    ok, out = setup_tools.capture(_emit(b"before" + LONE_CONTINUATION + b"after"))

    assert ok, "an undecodable byte must not turn a successful run into a failure"
    assert out.startswith("before") and out.endswith("after")
    assert "\ufffd" in out, f"expected a replacement character in {out!r}"


def test_capture_reports_an_unreadable_stream_as_a_failure(monkeypatch):
    """Not as a command that succeeded and printed nothing.

    ``(True, "")`` is the answer that gets a missing tool recorded as present
    with no version, which is the collapse this repository's conventions name
    explicitly.
    """
    monkeypatch.setattr(setup_tools.subprocess, "run",
                        lambda *a, **k: _completed(stdout=None))

    assert setup_tools.capture(["anything"]) == (False, "")


def test_capture_reports_a_genuinely_empty_stream_as_success():
    """The control: a command that really does print nothing still succeeds,
    so the guard above cannot be "return False whenever stdout is falsy"."""
    ok, out = setup_tools.capture(_emit(b""))

    assert (ok, out) == (True, "")


def test_capture_still_reports_a_nonzero_exit(tmp_path):
    ok, _out = setup_tools.capture([sys.executable, "-c", "raise SystemExit(3)"])
    assert not ok


# ── operator_runner process-tree fallback ───────────────────────
def test_ps_fallback_survives_output_it_could_not_read(monkeypatch):
    """``_process_parents_posix`` exists so a failed ``/proc`` read has
    somewhere to go. Its ``except`` clause never listed ``AttributeError``, so
    an unreadable ``ps`` would have crashed the runner in the code path whose
    whole job is to keep it alive."""
    monkeypatch.setattr(Path, "iterdir",
                        lambda self: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr(operator_runner.subprocess, "run",
                        lambda *a, **k: _completed(stdout=None))

    assert operator_runner._process_parents_posix() == {}


def test_ps_fallback_still_parses_a_normal_answer(monkeypatch):
    """The control: "return {} always" must not pass the test above."""
    monkeypatch.setattr(Path, "iterdir",
                        lambda self: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr(
        operator_runner.subprocess, "run",
        lambda *a, **k: _completed(stdout="  1 0\n42 1\n"))

    assert operator_runner._process_parents_posix() == {1: 0, 42: 1}


# ── operator-ingest.py (legacy, dash-named) ─────────────────────
@pytest.fixture(scope="module")
def legacy_ingest():
    spec = importlib.util.spec_from_file_location(
        "legacy_operator_ingest_decoding", ROOT / "operator-ingest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_ingest_reads_utf8_log_bytes(legacy_ingest):
    """Its entire input is copilot's UTF-8 telemetry log, and its callers do
    ``out.strip()`` and ``json.loads(out)`` on whatever this returns."""
    text = f"cwd={CYRILLIC_IO}\u2014path"
    out, rc = legacy_ingest.run_cmd(_emit(text.encode("utf-8")))

    assert rc == 0
    assert out == text


def test_legacy_ingest_does_not_report_an_unreadable_read_as_success(
        legacy_ingest, monkeypatch):
    """``("", 0)`` would say "the command ran and found nothing", and every
    caller here treats an empty result as an absent measurement."""
    monkeypatch.setattr(legacy_ingest.subprocess, "run",
                        lambda *a, **k: _completed(stdout=None))
    out, rc = legacy_ingest.run_cmd(["anything"])

    assert out == ""
    assert rc != 0


def test_legacy_ingest_passes_through_a_real_empty_result(legacy_ingest):
    """The control: an empty stdout from a command that exited 0 keeps its 0.
    Without this, "return 1 when stdout is falsy" passes the test above."""
    out, rc = legacy_ingest.run_cmd(_emit(b""))

    assert (out, rc) == ("", 0)


# ── e2e_restart_loop.pid_alive ──────────────────────────────────
@pytest.fixture(scope="module")
def e2e():
    spec = importlib.util.spec_from_file_location(
        "e2e_restart_loop_for_decoding", ROOT / "e2e_restart_loop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pid_alive_does_not_declare_a_process_dead_it_could_not_check(
        e2e, monkeypatch):
    """"Alive" is the safe answer for an unknown. The caller that reads False
    stops waiting and starts deleting the temporary tree, and the assertion
    that reads it -- "old supervisor is gone" -- would pass without evidence.
    """
    monkeypatch.setattr(e2e.os, "name", "nt")
    monkeypatch.setattr(e2e.subprocess, "run",
                        lambda *a, **k: _completed(stdout=None))

    assert e2e.pid_alive(4321) is True


def test_pid_alive_reads_a_normal_tasklist_answer(e2e, monkeypatch):
    """The control for the polarity above, in both directions."""
    monkeypatch.setattr(e2e.os, "name", "nt")
    monkeypatch.setattr(
        e2e.subprocess, "run",
        lambda *a, **k: _completed(stdout="python.exe   4321 Console  1  9,000 K\n"))
    assert e2e.pid_alive(4321) is True

    monkeypatch.setattr(
        e2e.subprocess, "run",
        lambda *a, **k: _completed(stdout="INFO: No tasks are running.\n"))
    assert e2e.pid_alive(4321) is False


def test_pid_alive_still_answers_false_for_no_pid(e2e):
    assert e2e.pid_alive(None) is False
    assert e2e.pid_alive(0) is False


# ── the premise these tests rest on ─────────────────────────────
@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_locale_decoding_really_does_raise_on_a_reader_thread():
    """The mechanism, demonstrated rather than asserted from documentation.

    If this ever stops being true -- a future Python defaulting to UTF-8 mode,
    say -- the tests above are guarding a bug that can no longer happen, and
    that is worth knowing rather than discovering. It is skipped where the
    locale codec can decode the byte anyway, because there the premise simply
    does not apply.
    """
    import locale
    codec = locale.getpreferredencoding(False)
    try:
        LONE_CONTINUATION.decode(codec)
    except (UnicodeDecodeError, LookupError):
        pass
    else:
        pytest.skip(f"{codec} decodes {LONE_CONTINUATION!r} without complaint")

    proc = subprocess.run(_emit(LONE_CONTINUATION), capture_output=True,
                          text=True, timeout=60)  # decode-ok: this IS the bug

    assert proc.returncode == 0, "the child exited badly for some other reason"
    assert proc.stdout is None, (
        "the decode no longer fails silently on a reader thread; re-read the "
        "guards this file protects, they may be guarding nothing")


def test_the_named_encoding_is_what_makes_that_call_safe():
    """Same command, same bytes, with the fix applied."""
    proc = subprocess.run(_emit(LONE_CONTINUATION), capture_output=True,
                          encoding="utf-8", errors="replace", timeout=60)

    assert proc.returncode == 0
    assert proc.stdout == "\ufffd"
