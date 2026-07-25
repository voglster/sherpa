"""Output-shape tests for the jira_issues AXI conversion.

Run: uv run --with pytest --with python-toon pytest tests/test_jira_issues.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

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
