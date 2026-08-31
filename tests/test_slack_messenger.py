"""Unit tests for slack_messenger's delivery path (message vs. file upload).

Run: uv run --with pytest --with httpx pytest tests/test_slack_messenger.py -v
No network required — every Slack call is stubbed.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "slack_messenger", Path(__file__).resolve().parent.parent / "tools" / "slack_messenger.py"
)
slack_messenger = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(slack_messenger)


class FakeResponse:
    status_code = 200


class FakeClient:
    """Stands in for httpx.AsyncClient; records raw uploads to the reserved URL."""

    def __init__(self):
        self.uploads = []

    async def post(self, url, headers=None, files=None):
        self.uploads.append((url, files["file"][0], files["file"][1]))
        return FakeResponse()


@pytest.fixture
def slack(monkeypatch):
    """Stub the Slack API surface and record every call the tool makes."""
    calls = {"get": [], "post": []}

    async def fake_get(client, headers, url, params=None):
        calls["get"].append((url, params))
        return {"upload_url": "https://files.slack/upload/1", "file_id": "F123"}

    async def fake_post(client, headers, url, payload):
        calls["post"].append((url, payload))
        return {
            "ts": "1774551827.458609",
            "files": [{"id": "F123", "permalink": "https://slack/files/F123",
                       "shares": {"public": {"C1": [{"ts": "1774551827.458609"}]}}}],
        }

    monkeypatch.setattr(slack_messenger, "_slack_get", fake_get)
    monkeypatch.setattr(slack_messenger, "_slack_post", fake_post)
    monkeypatch.setattr(slack_messenger, "_linkify_refs", lambda text: text)

    async def no_mentions(client, headers, text):
        return text

    monkeypatch.setattr(slack_messenger, "_linkify_mentions", no_mentions)
    return calls


def deliver(client, args):
    return asyncio.run(slack_messenger._deliver(client, {}, "C1", args))


def make_args(**overrides):
    defaults = {"text": None, "file": None, "stdin": False, "thread": None,
                "blocks": None, "attach": None, "title": None, "no_upload_fallback": False}
    return argparse.Namespace(**{**defaults, **overrides})


def test_plain_text_posts_a_message(slack):
    result = deliver(FakeClient(), make_args(text="hello"))

    assert result == {"ts": "1774551827.458609"}
    url, payload = slack["post"][0]
    assert url.endswith("chat.postMessage")
    assert payload == {"channel": "C1", "text": "hello"}


def test_escape_sequences_in_text_become_real_whitespace(slack):
    deliver(FakeClient(), make_args(text=r"line one\nline two\ttabbed"))

    assert slack["post"][0][1]["text"] == "line one\nline two\ttabbed"


def test_doubled_backslash_keeps_a_literal_escape_sequence(slack):
    deliver(FakeClient(), make_args(text=r"regex is \\n and \d"))

    assert slack["post"][0][1]["text"] == r"regex is \n and \d"


def test_file_text_keeps_backslash_n_literal(slack, tmp_path):
    snippet = tmp_path / "snippet.txt"
    snippet.write_text(r"print('a\nb')")

    deliver(FakeClient(), make_args(file=str(snippet)))

    assert slack["post"][0][1]["text"] == r"print('a\nb')"


def test_attachment_is_uploaded_with_text_as_initial_comment(slack, tmp_path):
    doc = tmp_path / "SKILL.md"
    doc.write_text("# skill\n")
    client = FakeClient()

    result = deliver(client, make_args(text="here you go", attach=[str(doc)], thread="123.456"))

    assert slack["get"][0][1] == {"filename": "SKILL.md", "length": len("# skill\n")}
    assert client.uploads == [("https://files.slack/upload/1", "SKILL.md", b"# skill\n")]
    url, payload = slack["post"][0]
    assert url.endswith("files.completeUploadExternal")
    assert payload == {
        "files": [{"id": "F123", "title": "SKILL.md"}],
        "channel_id": "C1",
        "initial_comment": "here you go",
        "thread_ts": "123.456",
    }
    assert result["files"] == [{"id": "F123", "permalink": "https://slack/files/F123"}]
    assert result["ts"] == "1774551827.458609"


def test_title_overrides_the_filename(slack, tmp_path):
    doc = tmp_path / "report.md"
    doc.write_text("body")

    deliver(FakeClient(), make_args(attach=[str(doc)], title="Weekly report"))

    assert slack["post"][0][1]["files"] == [{"id": "F123", "title": "Weekly report"}]
    assert "initial_comment" not in slack["post"][0][1]


def test_multiple_attachments_upload_and_complete_together(slack, tmp_path):
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    client = FakeClient()

    deliver(client, make_args(attach=[str(first), str(second)]))

    assert [name for _, name, _ in client.uploads] == ["a.png", "b.png"]
    assert len(slack["post"][0][1]["files"]) == 2


def test_over_limit_text_uploads_instead_of_being_truncated(slack, capsys):
    long_text = "x" * (slack_messenger.SLACK_TEXT_LIMIT + 1)
    client = FakeClient()

    deliver(client, make_args(text=long_text))

    url, payload = slack["post"][0]
    assert url.endswith("files.completeUploadExternal")
    assert client.uploads[0][2] == long_text.encode()
    assert "initial_comment" not in payload
    assert "over Slack's" in capsys.readouterr().err


def test_no_upload_fallback_sends_over_limit_text_as_is(slack):
    long_text = "x" * (slack_messenger.SLACK_TEXT_LIMIT + 1)

    deliver(FakeClient(), make_args(text=long_text, no_upload_fallback=True))

    url, payload = slack["post"][0]
    assert url.endswith("chat.postMessage")
    assert payload["text"] == long_text


def test_blocks_with_attach_is_rejected(slack, tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("body")

    with pytest.raises(SystemExit) as exit_info:
        deliver(FakeClient(), make_args(attach=[str(doc)], blocks="/tmp/blocks.json"))

    assert exit_info.value.code == 1


def test_missing_attachment_fails_before_any_slack_call(slack, tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        deliver(FakeClient(), make_args(attach=[str(tmp_path / "nope.md")]))

    assert exit_info.value.code == 1
    assert slack["get"] == [] and slack["post"] == []


def test_empty_attachment_is_rejected(slack, tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("")

    with pytest.raises(SystemExit) as exit_info:
        deliver(FakeClient(), make_args(attach=[str(empty)]))

    assert exit_info.value.code == 2


@pytest.fixture
def vault(monkeypatch, tmp_path):
    path = tmp_path / "vault.json"
    path.write_text('{"JIRA_URL": "https://acme.atlassian.net/"}')
    monkeypatch.setattr(slack_messenger, "VAULT_PATH", path)
    return path


def test_jira_key_becomes_a_link(vault):
    assert slack_messenger._linkify_refs("see KB-123 please") == (
        "see <https://acme.atlassian.net/browse/KB-123|KB-123> please"
    )


def test_bare_pr_url_gets_a_short_label(vault):
    assert slack_messenger._linkify_refs("review https://github.com/acme/api/pull/1715 today") == (
        "review <https://github.com/acme/api/pull/1715|#1715> today"
    )


def test_pr_url_keeps_trailing_punctuation_outside_the_link(vault):
    assert slack_messenger._linkify_refs("ship https://github.com/acme/api/pull/7.") == (
        "ship <https://github.com/acme/api/pull/7|#7>."
    )


def test_shorthand_expands_to_a_pr_link(vault):
    assert slack_messenger._linkify_refs("acme/api#42 is ready") == (
        "<https://github.com/acme/api/pull/42|#42> is ready"
    )


def test_labels_are_repo_qualified_when_the_message_spans_repos(vault):
    assert slack_messenger._linkify_refs("acme/api#42 and https://github.com/acme/web/pull/9") == (
        "<https://github.com/acme/api/pull/42|acme/api#42> and "
        "<https://github.com/acme/web/pull/9|acme/web#9>"
    )


def test_existing_slack_link_is_left_alone(vault):
    text = "<https://github.com/acme/api/pull/1715|the diff> for KB-9"
    assert slack_messenger._linkify_refs(text) == (
        "<https://github.com/acme/api/pull/1715|the diff> for "
        "<https://acme.atlassian.net/browse/KB-9|KB-9>"
    )


def test_linkifying_twice_changes_nothing(vault):
    text = "KB-1 https://github.com/acme/api/pull/2 acme/api#3"
    once = slack_messenger._linkify_refs(text)
    assert slack_messenger._linkify_refs(once) == once


def test_non_pr_github_urls_are_untouched(vault):
    for url in (
        "https://github.com/acme/api/issues/12",
        "https://github.com/acme/api/pull/12/files",
        "https://github.com/acme/api/commit/abc123",
    ):
        assert slack_messenger._linkify_refs(url) == url


def test_jira_keys_are_left_bare_without_a_configured_jira_url(monkeypatch, tmp_path):
    empty = tmp_path / "vault.json"
    empty.write_text("{}")
    monkeypatch.setattr(slack_messenger, "VAULT_PATH", empty)

    assert slack_messenger._linkify_refs("KB-5 and acme/api#6") == (
        "KB-5 and <https://github.com/acme/api/pull/6|#6>"
    )
