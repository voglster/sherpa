"""Slack client: pagination, 429 backoff, auth error classification.

Run: uv run --with pytest --with httpx pytest tests/test_corpus_slack.py -v
No network — every response is stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sherpa.corpus.slack import AuthError, FakeSlack, HttpSlackClient, SlackError


class StubResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class StubTransport:
    """Returns queued responses in order, recording each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, data=None):
        self.requests.append((url, data))
        return self.responses.pop(0)


def test_paginate_follows_the_cursor_and_stops_when_empty():
    transport = StubTransport([
        StubResponse({
            "ok": True,
            "messages": [{"ts": "1.0"}, {"ts": "2.0"}],
            "response_metadata": {"next_cursor": "abc"},
        }),
        StubResponse({"ok": True, "messages": [{"ts": "3.0"}], "response_metadata": {}}),
    ])
    client = HttpSlackClient("xoxp-x", transport=transport)
    got = list(client.paginate("conversations.history", "messages", channel="C1"))
    assert [m["ts"] for m in got] == ["1.0", "2.0", "3.0"]
    assert transport.requests[1][1]["cursor"] == "abc"


def test_rate_limit_sleeps_for_retry_after_then_retries():
    transport = StubTransport([
        StubResponse({"ok": False, "error": "ratelimited"}, status_code=429, headers={"Retry-After": "3"}),
        StubResponse({"ok": True, "messages": []}),
    ])
    slept = []
    client = HttpSlackClient("xoxp-x", transport=transport, sleep=slept.append)
    client.call("conversations.history", channel="C1")
    assert slept == [3.0]


def test_invalid_auth_raises_auth_error():
    transport = StubTransport([StubResponse({"ok": False, "error": "invalid_auth"})])
    client = HttpSlackClient("xoxp-x", transport=transport)
    with pytest.raises(AuthError) as caught:
        client.call("auth.test")
    assert caught.value.code == "invalid_auth"


def test_other_api_errors_raise_slack_error():
    transport = StubTransport([StubResponse({"ok": False, "error": "channel_not_found"})])
    client = HttpSlackClient("xoxp-x", transport=transport)
    with pytest.raises(SlackError) as caught:
        client.call("conversations.history", channel="C9")
    assert caught.value.code == "channel_not_found"
    assert not isinstance(caught.value, AuthError)


def test_fake_slack_paginates_its_queued_pages():
    fake = FakeSlack({"conversations.history": [
        {"ok": True, "messages": [{"ts": "1.0"}], "response_metadata": {"next_cursor": "n"}},
        {"ok": True, "messages": [{"ts": "2.0"}]},
    ]})
    assert [m["ts"] for m in fake.paginate("conversations.history", "messages", channel="C1")] == ["1.0", "2.0"]


def test_repeated_cursor_raises_instead_of_hanging():
    """Malformed pagination (same cursor twice) raises rather than looping forever."""
    transport = StubTransport([
        StubResponse({
            "ok": True,
            "messages": [{"ts": "1.0"}],
            "response_metadata": {"next_cursor": "stuck"},
        }),
        StubResponse({
            "ok": True,
            "messages": [{"ts": "2.0"}],
            "response_metadata": {"next_cursor": "stuck"},  # Same cursor repeats
        }),
    ])
    client = HttpSlackClient("xoxp-x", transport=transport)
    with pytest.raises(SlackError) as caught:
        list(client.paginate("conversations.history", "messages", channel="C1"))
    assert caught.value.code == "pagination_cursor_repeat"


def test_non_numeric_retry_after_falls_back_to_default():
    """Invalid Retry-After header is ignored; uses default delay."""
    transport = StubTransport([
        StubResponse(
            {"ok": False, "error": "ratelimited"},
            status_code=429,
            headers={"Retry-After": "not-a-number"}
        ),
        StubResponse({"ok": True, "messages": []}),
    ])
    slept = []
    client = HttpSlackClient("xoxp-x", transport=transport, sleep=slept.append)
    client.call("conversations.history", channel="C1")
    assert slept == [1.0]  # Falls back to default


def test_5xx_response_retries_then_raises_retryable():
    """Non-2xx HTTP status (other than 429) retries and raises with retryable=True."""
    transport = StubTransport([
        StubResponse({"error": "internal_error"}, status_code=500),
        StubResponse({"error": "service_unavailable"}, status_code=503),
        StubResponse({"error": "bad_gateway"}, status_code=502),
        StubResponse({"error": "too_many_requests"}, status_code=503),
        StubResponse({"error": "something_bad"}, status_code=500),
    ])
    client = HttpSlackClient("xoxp-x", transport=transport)
    with pytest.raises(SlackError) as caught:
        client.call("conversations.history", channel="C1")
    assert caught.value.retryable is True
    assert caught.value.code == "ratelimited"
    # Verify all 5 attempts were made
    assert len(transport.requests) == 5


def test_fake_slack_paginate_includes_limit_200():
    """FakeSlack.paginate calls with limit=200 matching HttpSlackClient."""
    fake = FakeSlack({"conversations.history": [
        {"ok": True, "messages": [{"ts": "1.0"}], "response_metadata": {"next_cursor": ""}},
    ]})
    list(fake.paginate("conversations.history", "messages", channel="C1"))
    # Verify the call included limit=200
    assert len(fake.calls) == 1
    method, params = fake.calls[0]
    assert params.get("limit") == 200
    assert params.get("channel") == "C1"
