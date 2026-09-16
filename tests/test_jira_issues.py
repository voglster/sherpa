"""Output-shape tests for the jira_issues AXI conversion.

Run: uv run --with pytest --with python-toon pytest tests/test_jira_issues.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
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
    def __init__(self, responses, whoami=None):
        self._responses = list(responses)
        self._whoami = whoami
        self.calls = 0
        self.get_calls = 0

    def post(self, url, json=None):
        response = self._responses[self.calls]
        self.calls += 1
        return response

    def get(self, url, params=None):
        self.get_calls += 1
        if isinstance(self._whoami, Exception):
            raise self._whoami
        if self._whoami is None:
            raise AssertionError(f"unexpected GET {url}")
        return self._whoami


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


# --- markdown -> ADF marks ---


def marks_of(adf: dict, text: str) -> list[str]:
    """Mark types on the first text node matching `text`, in document order."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("text") == text:
                yield [m["type"] for m in node.get("marks", [])]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    return next(walk(adf), [])


def href_of(adf: dict, text: str) -> str | None:
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and node.get("text") == text:
                for mark in node.get("marks", []):
                    if mark["type"] == "link":
                        yield mark["attrs"]["href"]
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    return next(walk(adf), None)


def test_bold_wrapped_inline_code_emits_code_alone():
    adf = jira_issues._md_to_adf("**`Foo`**")

    assert marks_of(adf, "Foo") == ["code"]


def test_code_inside_a_bolded_bullet_lead_in_emits_code_alone():
    adf = jira_issues._md_to_adf("- **`PendingJobIndexMiddleware` fails open.**")

    assert marks_of(adf, "PendingJobIndexMiddleware") == ["code"]
    assert marks_of(adf, " fails open.") == ["strong"]


def test_linked_inline_code_keeps_the_link_and_drops_the_bold():
    """ADF forbids code+strong but explicitly permits code+link, so the href survives."""
    adf = jira_issues._md_to_adf("[**`config.py`**](http://x/y)")

    assert sorted(marks_of(adf, "config.py")) == ["code", "link"]
    assert href_of(adf, "config.py") == "http://x/y"


def test_non_code_marks_still_combine():
    adf = jira_issues._md_to_adf("***both***")

    assert sorted(marks_of(adf, "both")) == ["em", "strong"]


def test_no_text_node_ever_carries_code_with_another_mark():
    source = "**`a`** and *`b`* and ~~`c`~~ and [`d`](http://e/f)"

    adf = jira_issues._md_to_adf(source)

    for name in ("a", "b", "c"):
        assert marks_of(adf, name) == ["code"]
    assert sorted(marks_of(adf, "d")) == ["code", "link"]


# --- markdown tables ---


TABLE_MD = "| Env | Host |\n| --- | ---: |\n| prod | `a.example` |\n|  | **b** |\n"


def only_table(markdown: str) -> dict:
    nodes = [n for n in jira_issues._md_to_adf(markdown)["content"] if n["type"] == "table"]
    assert len(nodes) == 1
    return nodes[0]


def test_a_pipe_table_becomes_a_real_table_node():
    table = only_table(TABLE_MD)

    assert [row["type"] for row in table["content"]] == ["tableRow"] * 3


def test_the_first_row_becomes_header_cells():
    header, first_body, _ = only_table(TABLE_MD)["content"]

    assert [c["type"] for c in header["content"]] == ["tableHeader", "tableHeader"]
    assert [c["type"] for c in first_body["content"]] == ["tableCell", "tableCell"]


def test_cell_content_is_a_paragraph_carrying_inline_marks():
    body_row = only_table(TABLE_MD)["content"][1]

    cell = body_row["content"][1]
    assert cell["content"][0]["type"] == "paragraph"
    assert cell["content"][0]["content"][0]["marks"] == [{"type": "code"}]


def test_an_empty_cell_holds_no_empty_text_node():
    last_row = only_table(TABLE_MD)["content"][2]

    assert last_row["content"][0]["content"] == [{"type": "paragraph"}]


def test_a_rendered_table_is_a_valid_document():
    assert jira_issues.adf_problems(jira_issues._md_to_adf(TABLE_MD)) == []


def test_a_table_survives_the_round_trip_back_to_markdown():
    rendered = jira_issues._adf_to_text(jira_issues._md_to_adf(TABLE_MD))

    assert rendered.splitlines() == [
        "| Env | Host |",
        "| --- | --- |",
        "| prod | `a.example` |",
        "|  | **b** |",
    ]


def test_text_around_a_table_still_renders_as_paragraphs():
    types = [n["type"] for n in jira_issues._md_to_adf(f"Intro\n\n{TABLE_MD}\nOutro\n")["content"]]

    assert types == ["paragraph", "table", "paragraph"]


# --- ADF validation (what --dry-run checks) ---


def test_validation_passes_a_clean_document():
    assert jira_issues.adf_problems(jira_issues._md_to_adf("plain **text**")) == []


def test_validation_accepts_code_with_a_link():
    ok = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Foo", "marks": [
                {"type": "code"}, {"type": "link", "attrs": {"href": "http://x/y"}}]}]}]}

    assert jira_issues.adf_problems(ok) == []


def test_validation_catches_an_exclusive_mark_collision():
    bad = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Foo",
             "marks": [{"type": "code"}, {"type": "strong"}]}]}]}

    problems = jira_issues.adf_problems(bad)

    assert len(problems) == 1
    assert "Foo" in problems[0] and "code" in problems[0]


def test_validation_catches_an_unsupported_node_type():
    bad = {"type": "doc", "version": 1, "content": [{"type": "flowchart", "content": []}]}

    assert any("flowchart" in p for p in jira_issues.adf_problems(bad))


def test_validated_adf_returns_the_document_when_clean():
    assert jira_issues.validated_adf("**bold** and `code`")["type"] == "doc"


def test_validated_adf_refuses_an_invalid_document(monkeypatch, capsys):
    bad = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Foo",
             "marks": [{"type": "code"}, {"type": "strong"}]}]}]}
    monkeypatch.setattr(jira_issues, "_md_to_adf", lambda text: bad)

    with pytest.raises(SystemExit) as exit_info:
        jira_issues.validated_adf("anything")

    assert exit_info.value.code == 2
    assert "Foo" in capsys.readouterr().err


AUTHED = _FakeResponse(200, {"emailAddress": "jane@example.com"})
ANON = _FakeResponse(401, text="Client must be authenticated to access this resource.")
EMPTY_PAGE = {"issues": [], "isLast": True}


def test_unauthenticated_empty_page_fails_instead_of_reporting_zero_matches():
    """Jira answers an anonymous search with 200 and an empty list. Reporting
    that as a legitimately empty result is the bug this guards."""
    client = _FakeClient([_FakeResponse(200, EMPTY_PAGE)], whoami=ANON)
    with pytest.raises(SystemExit) as excinfo:
        jira_issues._search_execute(client, "project = KB", 20)
    assert excinfo.value.code == 2


def test_authenticated_empty_page_is_still_a_real_empty_result():
    client = _FakeClient([_FakeResponse(200, EMPTY_PAGE)], whoami=AUTHED)
    issues, total, total_is_exact = jira_issues._search_execute(client, "project = KB", 20)
    assert (issues, total, total_is_exact) == ([], 0, True)


def test_forbidden_is_treated_as_an_auth_failure_too():
    client = _FakeClient([_FakeResponse(200, EMPTY_PAGE)],
                         whoami=_FakeResponse(403, text="Forbidden"))
    with pytest.raises(SystemExit):
        jira_issues._search_execute(client, "project = KB", 20)


def test_unreachable_auth_probe_does_not_fail_a_legitimate_empty_result():
    """A probe that cannot complete must not turn an empty search into an error."""
    client = _FakeClient([_FakeResponse(200, EMPTY_PAGE)],
                         whoami=httpx.ConnectError("boom"))
    issues, total, _ = jira_issues._search_execute(client, "project = KB", 20)
    assert (issues, total) == ([], 0)


def test_non_empty_page_never_pays_for_the_auth_probe():
    client = _FakeClient([_FakeResponse(200, {"issues": [ISSUE], "isLast": True})])
    jira_issues._search_execute(client, "project = KB", 20)
    assert client.get_calls == 0
