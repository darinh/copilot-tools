"""Tests for the session-backend abstraction."""
from __future__ import annotations

import subprocess

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
    """Records invocations and replays scripted results."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        verb = cmd[1] if len(cmd) > 1 else ""
        out, rc = self.results.get(verb, ("", 0))
        if callable(out):
            out, rc = out(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


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
