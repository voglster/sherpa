"""Tests for the piper webhook tool.

Run: uv run --with pytest --with httpx pytest tests/test_piper.py -v
No network required — httpx.post and the vault are both stubbed.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "piper", Path(__file__).resolve().parent.parent / "tools" / "piper.py"
)
piper = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(piper)

SECRET_URL = "https://hooks.example.test/secret-path-abc123"
SECRET_AUTH = "Bearer super-secret-token-xyz"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    path = tmp_path / "vault.json"
    monkeypatch.setattr(piper, "VAULT_PATH", path)

    def write(**keys):
        path.write_text(json.dumps(keys))

    return write


@pytest.fixture
def webhook(monkeypatch):
    calls = []
    outcome = {"status": 200, "raise": None}

    def fake_post(url, *, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if outcome["raise"] is not None:
            raise outcome["raise"]
        return httpx.Response(outcome["status"], request=httpx.Request("POST", url))

    monkeypatch.setattr(piper.httpx, "post", fake_post)
    return calls, outcome


def run(argv, capsys):
    code = 0
    try:
        piper.main(argv)
    except SystemExit as exit_:
        code = exit_.code
    out, err = capsys.readouterr()
    return code, out, err


def assert_no_secrets(*streams):
    for stream in streams:
        assert SECRET_URL not in stream
        assert "secret-path-abc123" not in stream
        assert "super-secret-token-xyz" not in stream


def test_posts_the_documented_payload_with_the_vault_auth_header(vault, webhook, capsys):
    vault(PIPER_WEBHOOK_URL=SECRET_URL, PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    calls, _ = webhook

    code, out, err = run(["shipped KB-1", "--session", "kb-1", "--host", "desk", "--kind", "done", "--json"], capsys)

    assert code == 0
    [call] = calls
    assert call["url"] == SECRET_URL
    assert call["headers"] == {"Authorization": SECRET_AUTH}
    assert call["timeout"] <= 5
    payload = call["json"]
    assert set(payload) == {"source", "host", "session", "kind", "message", "ts"}
    assert payload | {"ts": None} == {
        "source": "sherpa", "host": "desk", "session": "kb-1",
        "kind": "done", "message": "shipped KB-1", "ts": None,
    }
    assert json.loads(out) == {"sent": True, "status": 200, "kind": "done", "session": "kb-1"}
    assert_no_secrets(out, err)


def test_missing_keys_names_each_key_and_how_to_set_it(vault, webhook, capsys):
    vault(PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    calls, _ = webhook

    code, out, err = run(["hello"], capsys)

    assert code == 2
    assert calls == []
    assert "MISSING_SECRET: PIPER_WEBHOOK_URL" in err
    assert "PIPER_WEBHOOK_AUTH" not in err
    assert "sherpa vault_manager set PIPER_WEBHOOK_URL" in out
    assert_no_secrets(out, err)


def test_no_vault_file_lists_both_keys(vault, webhook, capsys):
    code, out, err = run(["hello"], capsys)

    assert code == 2
    assert "MISSING_SECRET: PIPER_WEBHOOK_URL" in err
    assert "MISSING_SECRET: PIPER_WEBHOOK_AUTH" in err


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectTimeout("timed out to " + SECRET_URL), httpx.ConnectError("refused " + SECRET_URL)],
)
def test_unreachable_webhook_warns_without_leaking_url_and_exits_zero(vault, webhook, capsys, error):
    vault(PIPER_WEBHOOK_URL=SECRET_URL, PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    _, outcome = webhook
    outcome["raise"] = error

    code, out, err = run(["hello", "--json"], capsys)

    assert code == 0
    assert "warning: piper webhook unreachable" in err
    assert json.loads(out)["sent"] is False
    assert_no_secrets(out, err)


def test_rejected_post_warns_with_status_and_exits_zero(vault, webhook, capsys):
    vault(PIPER_WEBHOOK_URL=SECRET_URL, PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    _, outcome = webhook
    outcome["status"] = 401

    code, out, err = run(["hello", "--json"], capsys)

    assert code == 0
    assert "HTTP 401" in err
    assert json.loads(out) | {"session": None} == {"sent": False, "status": 401, "kind": "fyi", "session": None}
    assert_no_secrets(out, err)


def test_long_multiline_messages_are_flattened_and_clipped(vault, webhook, capsys):
    vault(PIPER_WEBHOOK_URL=SECRET_URL, PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    calls, _ = webhook

    run(["line one\nline two " + "x" * 2000, "--session", "s"], capsys)

    message = calls[0]["json"]["message"]
    assert "\n" not in message
    assert message.startswith("line one line two")
    assert len(message) == piper.MAX_MESSAGE_CHARS


def test_session_defaults_to_none_outside_tmux(vault, webhook, capsys, monkeypatch):
    vault(PIPER_WEBHOOK_URL=SECRET_URL, PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    monkeypatch.delenv("TMUX", raising=False)
    calls, _ = webhook

    run(["hello"], capsys)

    assert calls[0]["json"]["session"] is None


def test_unknown_kind_is_rejected_before_any_post(vault, webhook, capsys):
    vault(PIPER_WEBHOOK_URL=SECRET_URL, PIPER_WEBHOOK_AUTH=SECRET_AUTH)
    calls, _ = webhook

    code, _, _ = run(["hello", "--kind", "panic"], capsys)

    assert code == 2
    assert calls == []
