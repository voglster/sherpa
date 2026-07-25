"""Output-shape tests for the jira_issues AXI conversion.

Run: uv run --with pytest --with python-toon pytest tests/test_jira_issues.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "jira_issues", Path(__file__).resolve().parent.parent / "tools" / "jira_issues.py"
)
jira_issues = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(jira_issues)

ISSUE = {
    "key": "KB-1",
    "fields": {
        "summary": "Fix auth bug",
        "status": {"name": "Open"},
        "assignee": {"displayName": "Jane Doe"},
        "description": "y" * 4000,
    },
}


def test_default_row_stays_within_the_axi_field_budget():
    assert set(jira_issues.search_rows([ISSUE])[0]) == {"key", "summary", "status"}


def test_long_descriptions_never_reach_list_output():
    assert "y" * 100 not in str(jira_issues.search_rows([ISSUE]))


def test_payload_reports_the_true_total_not_the_page_size():
    payload = jira_issues.search_payload([ISSUE], total=847)
    assert payload["count"] == "1 of 847 total"


def test_empty_result_states_the_zero_explicitly():
    payload = jira_issues.search_payload([], total=0)
    assert "0" in str(payload["issues"])
    assert payload.get("help")


def test_hints_use_placeholders_rather_than_guessed_values():
    hints = jira_issues.search_payload([ISSUE], total=1)["help"]
    assert any("<" in hint for hint in hints)


def test_exact_total_omits_the_approximate_marker():
    payload = jira_issues.search_payload([ISSUE], total=1, total_is_exact=True)
    assert "total_is_approximate" not in payload


def test_inexact_total_is_flagged_approximate():
    payload = jira_issues.search_payload([ISSUE], total=847, total_is_exact=False)
    assert payload["total_is_approximate"] is True


class _FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("response body is not valid JSON")
        return self._body


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, url, json=None):
        response = self._responses[self.calls]
        self.calls += 1
        return response


def test_isLast_true_reports_exact_total_without_a_second_call():
    client = _FakeClient([_FakeResponse(200, {"issues": [ISSUE], "isLast": True})])
    issues, total, total_is_exact = jira_issues._search_execute(client, "project = KB", 20)
    assert issues == [ISSUE]
    assert total == 1
    assert total_is_exact is True
    assert client.calls == 1


def test_garbage_count_response_does_not_break_the_search():
    client = _FakeClient([
        _FakeResponse(200, {"issues": [ISSUE], "isLast": False}),
        _FakeResponse(200, body=None, text="<html>not json</html>"),
    ])
    issues, total, total_is_exact = jira_issues._search_execute(client, "project = KB", 20)
    assert issues == [ISSUE]
    assert total == 1
    assert total_is_exact is False


def test_count_call_network_failure_does_not_break_the_search():
    import httpx

    class _RaisingClient(_FakeClient):
        def post(self, url, json=None):
            if self.calls == 0:
                return super().post(url, json)
            self.calls += 1
            raise httpx.ConnectError("connection refused")

    client = _RaisingClient([_FakeResponse(200, {"issues": [ISSUE], "isLast": False})])
    issues, total, total_is_exact = jira_issues._search_execute(client, "project = KB", 20)
    assert issues == [ISSUE]
    assert total == 1
    assert total_is_exact is False


def test_missing_secret_reports_on_both_channels_and_exits_two(capsys, monkeypatch):
    monkeypatch.setattr(jira_issues, "_load_vault", dict)
    with pytest.raises(SystemExit) as exit_info:
        jira_issues._load_secret_axi("JIRA_API_TOKEN")
    captured = capsys.readouterr()
    assert captured.err == "MISSING_SECRET: JIRA_API_TOKEN\n"
    assert captured.out == (
        "error: missing secret JIRA_API_TOKEN\n"
        "help: sherpa vault_manager set JIRA_API_TOKEN <value>\n"
    )
    assert exit_info.value.code == 2


def test_unconverted_subcommands_share_the_missing_secret_exit_code(capsys, monkeypatch):
    monkeypatch.setattr(jira_issues, "_load_vault", dict)
    with pytest.raises(SystemExit) as exit_info:
        jira_issues._load_secret("JIRA_API_TOKEN")
    assert capsys.readouterr().err == "MISSING_SECRET: JIRA_API_TOKEN\n"
    assert exit_info.value.code == 2
