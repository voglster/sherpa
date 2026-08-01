"""Unit tests for the litellm_keys tool.

Run: uv run --with pytest --with python-toon==0.1.3 pytest tests/test_litellm_keys.py -v
No network required — all proxy calls and sinks are stubbed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "litellm_keys", Path(__file__).resolve().parent.parent / "tools" / "litellm_keys.py"
)
litellm_keys = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(litellm_keys)


def test_home_view_reports_proxy_and_master_status(capsys, tmp_path, monkeypatch):
    vault = tmp_path / "vault.json"
    vault.write_text(json.dumps({"LITELLM_API_URL": "https://llm.example"}))
    monkeypatch.setattr(litellm_keys, "VAULT_PATH", vault)
    litellm_keys._home()
    out = capsys.readouterr().out
    assert "proxy: https://llm.example" in out
    assert "master_key: MISSING" in out
    assert "bin:" in out


def test_derive_var_slugifies_and_uppercases():
    assert litellm_keys._derive_var("my key!") == "LITELLM_KEY_MY_KEY"
    assert litellm_keys._derive_var("prod-2026") == "LITELLM_KEY_PROD_2026"


def test_mask_shows_only_last_four():
    assert litellm_keys._mask("sk-abcdEFGHwTDg") == "sk-…wTDg"
    assert "abcdEFGH" not in litellm_keys._mask("sk-abcdEFGHwTDg")


def test_env_upsert_appends_when_absent():
    assert litellm_keys._env_upsert("", "FOO", "bar") == "FOO=bar\n"
    assert litellm_keys._env_upsert("A=1\n", "FOO", "bar") == "A=1\nFOO=bar\n"


def test_env_upsert_replaces_in_place_and_preserves_others():
    text = "A=1\nFOO=old\nB=2\n"
    assert litellm_keys._env_upsert(text, "FOO", "new") == "A=1\nFOO=new\nB=2\n"


def test_env_upsert_matches_exported_var():
    assert litellm_keys._env_upsert("export FOO=old\n", "FOO", "new") == "FOO=new\n"


def test_vault_set_merges_and_persists(tmp_path):
    p = tmp_path / "vault.json"
    p.write_text(json.dumps({"EXISTING": "1"}))
    litellm_keys._vault_set("LITELLM_KEY_X", "sk-secret", path=p)
    data = json.loads(p.read_text())
    assert data == {"EXISTING": "1", "LITELLM_KEY_X": "sk-secret"}


def test_op_template_puts_secret_in_concealed_field():
    tpl = litellm_keys._op_template("T", "sk-secret", "a note", "https://llm.example")
    assert tpl["category"] == "API_CREDENTIAL"
    cred = next(f for f in tpl["fields"] if f["id"] == "credential")
    assert cred["type"] == "CONCEALED"
    assert cred["value"] == "sk-secret"
    assert any(f["id"] == "notesPlain" and f["value"] == "a note" for f in tpl["fields"])


def test_resolve_matches_alias_then_token(monkeypatch):
    records = [
        {"key_alias": "prod", "token": "aaa", "key_name": "sk-...aaaa"},
        {"key_alias": "dev", "token": "bbbccc", "key_name": "sk-...bccc"},
    ]
    monkeypatch.setattr(litellm_keys, "_list_keys", lambda url, key, size=200: records)
    assert litellm_keys._resolve("u", "k", "prod")["token"] == "aaa"
    assert litellm_keys._resolve("u", "k", "bbbccc")["key_alias"] == "dev"
    assert litellm_keys._resolve("u", "k", "missing") is None


def test_generate_masks_secret_and_records_destinations(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("https://llm.example", "sk-master"))
    monkeypatch.setattr(
        litellm_keys, "_generate",
        lambda url, key, alias, models, note: {
            "key": "sk-realSECRETvalue", "token": "tok123", "models": models,
        },
    )
    vault = tmp_path / "vault.json"
    monkeypatch.setattr(litellm_keys, "VAULT_PATH", vault)

    args = argparse.Namespace(
        command="generate", name="prod", models="gpt-4o", note="",
        vault=None, env_file=None, var=None, op=None, clip=False,
        reveal=False, json=False,
    )
    litellm_keys.cmd_generate(args)
    out = capsys.readouterr().out
    assert "sk-realSECRETvalue" not in out
    assert "sk-…alue" in out
    assert json.loads(vault.read_text())["LITELLM_KEY_PROD"] == "sk-realSECRETvalue"


def test_list_summarizes_and_reports_totals(capsys, monkeypatch):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(
        litellm_keys, "_list_keys",
        lambda url, key, size=200: [
            {"key_alias": "prod", "models": ["gpt-4o"], "spend": 1.5, "created_at": "2026-08-01T10:00:00Z"},
        ],
    )
    litellm_keys.cmd_list(argparse.Namespace(command="list", fields="", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == "1 of 1 total"
    assert payload["keys"][0]["alias"] == "prod"


def test_info_not_found_is_definitive(capsys, monkeypatch):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(litellm_keys, "_resolve", lambda url, key, ident: None)
    litellm_keys.cmd_info(argparse.Namespace(command="info", ident="ghost", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ident": "ghost", "found": False}


def test_delete_missing_key_is_noop(capsys, monkeypatch):
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(litellm_keys, "_resolve", lambda url, key, ident: None)
    litellm_keys.cmd_delete(argparse.Namespace(command="delete", ident="ghost", json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted"] is False


def test_delete_resolves_then_deletes_by_token(capsys, monkeypatch):
    calls = {}
    monkeypatch.setattr(litellm_keys, "_config", lambda: ("u", "k"))
    monkeypatch.setattr(litellm_keys, "_resolve", lambda url, key, ident: {"token": "tok9", "key_alias": "prod"})
    monkeypatch.setattr(litellm_keys, "_delete", lambda url, key, token: calls.setdefault("token", token))
    litellm_keys.cmd_delete(argparse.Namespace(command="delete", ident="prod", json=True))
    assert calls["token"] == "tok9"
    assert json.loads(capsys.readouterr().out)["deleted"] is True
