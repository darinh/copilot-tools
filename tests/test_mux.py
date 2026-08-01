"""Tests for the session-backend abstraction."""
from __future__ import annotations

import subprocess
import time

import pytest

import operator_mux
from operator_mux import (
    Mux,
    MuxNotFoundError,
    MuxSessionError,
    safe_instance_id,
    sanitize_name,
)


class FakeRunner:
    """Records invocations and replays scripted results.

    A scripted result is ``(stdout, returncode)``, or a callable receiving the
    command and returning ``(stdout, returncode)`` or
    ``(stdout, returncode, stderr)`` when a test needs to drive stderr.
    """

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        verb = cmd[1] if len(cmd) > 1 else ""
        result = self.results.get(verb, ("", 0))
        if callable(result[0]):
            result = result[0](cmd)
        out, rc = result[0], result[1]
        err = result[2] if len(result) > 2 else ""
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    def count(self, verb):
        return sum(1 for cmd in self.calls if len(cmd) > 1 and cmd[1] == verb)


@pytest.fixture
def no_sleep(monkeypatch):
    """Keep retry tests instant while recording that a backoff happened.

    Patched on the stdlib module rather than on ``operator_mux`` so the test
    still exercises real behavior if the implementation stops importing time.
    """
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    return slept


# ── naming ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain", "plain"),
        ("my.proj", "my-proj"),
        ("my:proj", "my-proj"),
        ("a/b", "a-b"),
        ('bad"name', "bad-name"),
        ("with*star", "with-star"),
        ("pipe|name", "pipe-name"),
    ],
)
def test_sanitize_replaces_unsafe_characters(raw, expected):
    assert sanitize_name(raw) == expected


def test_sanitize_never_returns_empty():
    assert sanitize_name("...") == "instance"


def test_safe_id_is_stable_for_already_safe_names():
    assert safe_instance_id("frontend") == "frontend"


def test_safe_id_disambiguates_names_that_sanitize_alike():
    """'a.b', 'a:b' and 'a-b' must not collapse onto one set of state files."""
    ids = {safe_instance_id(n) for n in ("a.b", "a:b", "a-b")}
    assert len(ids) == 3


def test_safe_id_does_not_collide_with_a_literal_digest_name():
    """A user naming an instance exactly like a generated id must not collide
    with the instance that generated it."""
    generated = safe_instance_id("a.b")
    assert safe_instance_id(generated) != generated


def test_safe_id_avoids_windows_reserved_device_names():
    for reserved in ("CON", "nul", "COM1", "LPT9"):
        assert safe_instance_id(reserved).lower() != reserved.lower()


def test_safe_id_is_deterministic():
    assert safe_instance_id("my.proj") == safe_instance_id("my.proj")


# ── discovery ───────────────────────────────────────────────────
def test_probe_order_prefers_tmux(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return f"/usr/bin/{name}" if name in ("tmux", "psmux") else None

    monkeypatch.setattr(operator_mux.shutil, "which", fake_which)
    assert Mux().binary == "tmux"
    assert seen[0] == "tmux"


def test_probe_falls_back_to_psmux(monkeypatch):
    monkeypatch.setattr(
        operator_mux.shutil, "which",
        lambda n: "C:/psmux.exe" if n == "psmux" else None,
    )
    assert Mux().binary == "psmux"


def test_missing_backend_names_platform_install_command(monkeypatch):
    monkeypatch.setattr(operator_mux.shutil, "which", lambda n: None)
    monkeypatch.delenv("COPILOT_OPERATOR_MUX", raising=False)
    monkeypatch.setattr(operator_mux.platform, "system", lambda: "Windows")
    with pytest.raises(MuxNotFoundError) as exc:
        _ = Mux().binary
    assert "winget install" in str(exc.value)
    assert "psmux" in str(exc.value)


def test_env_override_selects_backend(monkeypatch):
    monkeypatch.setenv("COPILOT_OPERATOR_MUX", "mymux")
    assert Mux().binary == "mymux"


# ── session verbs ───────────────────────────────────────────────
def test_list_sessions_treats_empty_output_as_empty(monkeypatch):
    """psmux exits 0 with no output when no server runs; tmux exits 1."""
    mux = Mux(binary="tmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run",
        FakeRunner({"list-sessions": ("", 0)}),
    )
    assert mux.list_sessions() == []


def test_list_sessions_parses_names(monkeypatch):
    mux = Mux(binary="tmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run",
        FakeRunner({"list-sessions": ("alpha\nbeta\n", 0)}),
    )
    assert mux.list_sessions() == ["alpha", "beta"]


def test_new_session_detects_silent_failure(monkeypatch):
    """psmux can report success while creating nothing. That must raise."""
    mux = Mux(binary="psmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run",
        FakeRunner({"new-session": ("", 0), "has-session": ("", 1)}),
    )
    with pytest.raises(MuxSessionError, match="silent-failure"):
        mux.new_session("bad", "/tmp", ["prog"])


def test_new_session_succeeds_when_session_appears(monkeypatch):
    mux = Mux(binary="tmux")
    state = {"created": False}

    def has(cmd):
        return ("", 0 if state["created"] else 1)

    def create(cmd):
        state["created"] = True
        return ("", 0)

    monkeypatch.setattr(
        operator_mux.subprocess, "run",
        FakeRunner({"new-session": (create, 0), "has-session": (has, 0)}),
    )
    mux.new_session("good", "/tmp", ["prog", "--flag"])


def test_new_session_passes_argv_after_double_dash(monkeypatch):
    """argv must reach the backend unparsed so quoting survives."""
    mux = Mux(binary="tmux")
    state = {"created": False}
    runner = FakeRunner({
        "new-session": (lambda cmd: (state.__setitem__("created", True), ("", 0))[1], 0),
        "has-session": (lambda cmd: ("", 0 if state["created"] else 1), 0),
    })
    monkeypatch.setattr(operator_mux.subprocess, "run", runner)
    mux.new_session("s", "/tmp", ["copilot", "-i", "a b 'c' \"d\""])
    create_cmd = next(c for c in runner.calls if c[1] == "new-session")
    assert "--" in create_cmd
    assert create_cmd[create_cmd.index("--") + 1 :] == [
        "copilot", "-i", "a b 'c' \"d\""
    ]


def test_new_session_refuses_to_clobber_existing(monkeypatch):
    mux = Mux(binary="tmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run", FakeRunner({"has-session": ("", 0)})
    )
    with pytest.raises(MuxSessionError, match="already exists"):
        mux.new_session("taken", "/tmp", ["prog"])


# ── server-shutdown race ────────────────────────────────────────
def test_new_session_retries_when_the_server_was_shutting_down(monkeypatch, no_sleep):
    """Killing the last session takes the server with it.

    A create issued during that shutdown window fails for a reason that has
    nothing to do with the request, and the next attempt starts a fresh
    server. Surfacing it would break a loop restart for no reason.
    """
    mux = Mux(binary="tmux")
    state = {"attempts": 0, "created": False}

    def create(cmd):
        state["attempts"] += 1
        if state["attempts"] == 1:
            return ("", 1, "server exited unexpectedly")
        state["created"] = True
        return ("", 0)

    runner = FakeRunner({
        "new-session": (create, 0),
        "has-session": (lambda cmd: ("", 0 if state["created"] else 1), 0),
    })
    monkeypatch.setattr(operator_mux.subprocess, "run", runner)

    mux.new_session("good", "/tmp", ["prog"])

    assert runner.count("new-session") == 2
    assert no_sleep, "retry must back off rather than spin"


def test_new_session_adopts_a_session_the_dying_server_created(monkeypatch, no_sleep):
    """The create may land even though the client lost the server.

    Retrying then collides with the name it just made, so an existing session
    is adopted instead.
    """
    mux = Mux(binary="tmux")
    state = {"created": False}

    def create(cmd):
        state["created"] = True
        return ("", 1, "lost server")

    runner = FakeRunner({
        "new-session": (create, 0),
        "has-session": (lambda cmd: ("", 0 if state["created"] else 1), 0),
    })
    monkeypatch.setattr(operator_mux.subprocess, "run", runner)

    mux.new_session("adopted", "/tmp", ["prog"])

    assert runner.count("new-session") == 1


def test_new_session_does_not_retry_a_real_failure(monkeypatch, no_sleep):
    """Only server-lifecycle errors are transient. Bad requests stay loud."""
    mux = Mux(binary="tmux")
    runner = FakeRunner({
        "new-session": (lambda cmd: ("", 1, "bad session name: nope"), 0),
        "has-session": ("", 1),
    })
    monkeypatch.setattr(operator_mux.subprocess, "run", runner)

    with pytest.raises(MuxSessionError, match="bad session name"):
        mux.new_session("nope", "/tmp", ["prog"])

    assert runner.count("new-session") == 1
    assert not no_sleep, "a real failure must be reported without delay"


def test_new_session_gives_up_after_the_retry_budget(monkeypatch, no_sleep):
    """A backend that never recovers must still fail, and say why."""
    mux = Mux(binary="tmux")
    runner = FakeRunner({
        "new-session": (lambda cmd: ("", 1, "server exited unexpectedly"), 0),
        "has-session": ("", 1),
    })
    monkeypatch.setattr(operator_mux.subprocess, "run", runner)

    with pytest.raises(MuxSessionError, match="server exited unexpectedly"):
        mux.new_session("doomed", "/tmp", ["prog"])

    assert runner.count("new-session") == operator_mux._NEW_SESSION_ATTEMPTS


def test_pane_dead_maps_to_boolean(monkeypatch):
    mux = Mux(binary="tmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run", FakeRunner({"display-message": ("1", 0)})
    )
    assert mux.pane_dead("s") is True
    monkeypatch.setattr(
        operator_mux.subprocess, "run", FakeRunner({"display-message": ("0", 0)})
    )
    assert mux.pane_dead("s") is False


def test_pane_pid_returns_int_or_none(monkeypatch):
    mux = Mux(binary="tmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run", FakeRunner({"display-message": ("4242", 0)})
    )
    assert mux.pane_pid("s") == 4242
    monkeypatch.setattr(
        operator_mux.subprocess, "run", FakeRunner({"display-message": ("", 1)})
    )
    assert mux.pane_pid("s") is None


def test_kill_session_is_idempotent(monkeypatch):
    mux = Mux(binary="tmux")
    monkeypatch.setattr(
        operator_mux.subprocess, "run", FakeRunner({"has-session": ("", 1)})
    )
    assert mux.kill_session("gone") is False


# ── Windows console-window suppression ──────────────────────────
#
# Regression tests for the "a python window pops up and stays open" bug.
# A console-less parent (the `operator --loop` supervisor) makes Windows
# allocate a fresh *visible* console for any console child it spawns.
# CREATE_NO_WINDOW suppresses that, but it also rebinds the child's std
# handles to that new hidden console -- so it must NOT be used for the
# interactive attach path, which has to inherit the user's real terminal.


class FlagRecorder:
    """Captures the kwargs each subprocess.run call was made with."""

    def __init__(self, results=None):
        self.results = results or {}
        self.kwargs = []

    def __call__(self, cmd, **kwargs):
        self.kwargs.append(kwargs)
        verb = cmd[1] if len(cmd) > 1 else ""
        out, rc = self.results.get(verb, ("", 0))
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


def test_captured_calls_pass_no_window_flag(monkeypatch):
    """Control-plane calls must actually forward _POPEN_KWARGS.

    A sentinel is injected rather than asserting the real constant's value,
    so this tests the *wiring* (which is what regresses) on every platform
    instead of re-deriving the implementation's own platform branch.
    """
    mux = Mux(binary="tmux")
    monkeypatch.setattr(operator_mux, "_POPEN_KWARGS", {"creationflags": 0xABCD})
    rec = FlagRecorder({"has-session": ("", 0)})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    mux.has_session("s")
    assert len(rec.kwargs) == 1
    assert rec.kwargs[0]["creationflags"] == 0xABCD


def test_captured_calls_use_explicit_pipes(monkeypatch):
    """CREATE_NO_WINDOW rebinds std handles, so capture must be explicit.

    Without capture_output the child would write into the new hidden
    console and its output would be silently lost.
    """
    mux = Mux(binary="tmux")
    rec = FlagRecorder({"list-sessions": ("a\n", 0)})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    mux.list_sessions()
    assert rec.kwargs[0]["capture_output"] is True


def test_attach_never_passes_creationflags(monkeypatch):
    """attach() must inherit the real console or the user sees a dead prompt."""
    mux = Mux(binary="tmux")
    monkeypatch.setattr(operator_mux, "_POPEN_KWARGS", {"creationflags": 0xABCD})
    rec = FlagRecorder({"attach": ("", 0)})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    mux.attach("s")
    assert len(rec.kwargs) == 1
    assert "creationflags" not in rec.kwargs[0]
    assert "capture_output" not in rec.kwargs[0]


# ── send-keys literalness ───────────────────────────────────────
#
# Regression tests for message injection. Verified on psmux 3.3.7: without
# `-l` the backend looks up every whitespace-separated token in the string as
# a key name, so a message containing the word "Enter" submits early and the
# rest is lost, and one containing "C-c" delivers Ctrl-C to the pane.
def test_send_keys_is_literal_and_sends_enter_separately(monkeypatch):
    mux = Mux(binary="tmux")
    rec = FakeRunner({})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    mux.send_keys("sess", "hello Enter C-c world")
    assert rec.calls == [
        ["tmux", "send-keys", "-t", "sess", "-l", "hello Enter C-c world"],
        ["tmux", "send-keys", "-t", "sess", "Enter"],
    ]


def test_send_keys_without_enter_sends_only_the_text(monkeypatch):
    mux = Mux(binary="tmux")
    rec = FakeRunner({})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    mux.send_keys("sess", "no newline", enter=False)
    assert rec.calls == [["tmux", "send-keys", "-t", "sess", "-l", "no newline"]]


def test_send_keys_can_still_send_key_names_when_asked(monkeypatch):
    """The non-literal path stays available for real key names."""
    mux = Mux(binary="tmux")
    rec = FakeRunner({})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    mux.send_keys("sess", "C-c", literal=False, enter=False)
    assert rec.calls == [["tmux", "send-keys", "-t", "sess", "C-c"]]


# ── send-keys failure reporting ─────────────────────────────────
#
# `send_keys` is how an agent-to-agent message reaches a live session. Its
# caller queues the message instead when this raises, so a swallowed failure
# is not a cosmetic loss: the message is filed as delivered and nobody ever
# sees it. Every other state-changing verb here already reports failure.
def test_send_keys_raises_when_the_backend_reports_failure(monkeypatch):
    mux = Mux(binary="tmux")
    rec = FakeRunner({"send-keys": ("", 1, "no such session: sess")})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    with pytest.raises(operator_mux.MuxSessionError) as excinfo:
        mux.send_keys("sess", "hello")
    assert "sess" in str(excinfo.value)
    assert "no such session" in str(excinfo.value)


def test_send_keys_raises_when_only_the_enter_keystroke_fails(monkeypatch):
    """Text typed but never submitted has not been delivered. The session dying
    between the two calls is exactly the window this covers."""
    def result(cmd):
        return ("", 0) if "-l" in cmd else ("", 1, "session ended")

    mux = Mux(binary="tmux")
    rec = FakeRunner({"send-keys": (result, 0)})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    with pytest.raises(operator_mux.MuxSessionError):
        mux.send_keys("sess", "hello")


def test_send_keys_does_not_send_enter_after_the_text_failed(monkeypatch):
    """Submitting an empty line into a live agent session would be a stray
    keystroke in someone's UI."""
    mux = Mux(binary="tmux")
    rec = FakeRunner({"send-keys": ("", 1)})
    monkeypatch.setattr(operator_mux.subprocess, "run", rec)
    with pytest.raises(operator_mux.MuxSessionError):
        mux.send_keys("sess", "hello")
    assert rec.calls == [["tmux", "send-keys", "-t", "sess", "-l", "hello"]]
