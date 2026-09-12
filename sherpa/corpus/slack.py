"""Slack Web API access for the corpus ingester.

The client is deliberately synchronous and narrow: ingest is a cron job with no
latency budget, and reactive sleep-and-retry on 429 is easier to keep inside
Slack's limits than concurrency would be.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Protocol

import httpx

API = "https://slack.com/api/"
AUTH_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed"}

# Errors that say "this will never work", as opposed to "try later". The
# Slackbot DM answers channel_not_found to conversations.history for the life
# of the workspace; retrying it every run and counting it as an outstanding
# fault is a health signal that can never reach green, which teaches everyone
# to ignore the one that matters. Lives here so the scheduler and any future
# caller classify the same way.
PERMANENT_ERRORS = {
    "channel_not_found",
    "is_archived",
    "not_in_channel",
    "method_not_supported_for_channel_type",
    "missing_scope",
    "restricted_action",
    "user_not_found",
}
MAX_RETRIES = 5


class SlackError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable

    @property
    def permanent(self) -> bool:
        """Whether another attempt could ever succeed."""
        return self.code in PERMANENT_ERRORS


class AuthError(SlackError):
    """The token is bad. Retrying identically will never work."""


class SlackClient(Protocol):
    def call(self, method: str, **params: Any) -> dict: ...

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]: ...


class HttpSlackClient:
    def __init__(self, token: str, *, transport: Any = None, sleep=time.sleep) -> None:
        self._token = token
        self._transport = transport or httpx.Client(timeout=30.0)
        self._sleep = sleep

    def call(self, method: str, **params: Any) -> dict:
        payload = {k: v for k, v in params.items() if v is not None}
        last_error_type = None
        last_status = None
        for attempt in range(MAX_RETRIES):
            response = self._transport.post(
                API + method,
                headers={"Authorization": f"Bearer {self._token}"},
                data=payload,
            )
            if response.status_code == 429:
                try:
                    delay = float(response.headers.get("Retry-After", "1"))
                except ValueError:
                    delay = 1.0
                self._sleep(delay)
                last_error_type = "ratelimit"
                continue
            if response.status_code < 200 or response.status_code >= 300:
                last_error_type = "http_error"
                last_status = response.status_code
                self._sleep(2.0 * (attempt + 1))
                continue
            body = response.json()
            if body.get("ok"):
                return body
            code = body.get("error", "unknown")
            if code in AUTH_ERRORS:
                raise AuthError(code)
            if code == "ratelimited":
                self._sleep(2.0 * (attempt + 1))
                last_error_type = "ratelimit"
                continue
            raise SlackError(code)
        if last_error_type == "http_error":
            raise SlackError(f"http_{last_status}", retryable=True)
        raise SlackError("ratelimited", retryable=True)

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]:
        cursor = None
        seen_cursors = set()
        while True:
            if cursor in seen_cursors:
                raise SlackError("pagination_cursor_repeat")
            seen_cursors.add(cursor)
            body = self.call(method, cursor=cursor, limit=200, **params)
            yield from body.get(key, [])
            cursor = body.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return


class FakeSlack:
    """Replays queued response bodies per method. Records every call made."""

    def __init__(self, pages: dict[str, list[dict]]) -> None:
        self._pages = {method: list(bodies) for method, bodies in pages.items()}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method: str, **params: Any) -> dict:
        """Execute a fake API call, replaying queued response bodies.

        The final queued response for each method is sticky — it replays on every
        subsequent call. This is intentional: Task 5's tests invoke the ingester
        twice against a single queued response and need it available both times.
        """
        self.calls.append((method, params))
        queue = self._pages.get(method)
        if not queue:
            raise SlackError("no_fake_response_queued")
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if not body.get("ok", True):
            code = body.get("error", "unknown")
            raise AuthError(code) if code in AUTH_ERRORS else SlackError(code)
        return body

    def paginate(self, method: str, key: str, **params: Any) -> Iterator[dict]:
        cursor = None
        seen_cursors = set()
        while True:
            if cursor in seen_cursors:
                raise SlackError("pagination_cursor_repeat")
            seen_cursors.add(cursor)
            body = self.call(method, cursor=cursor, limit=200, **params)
            yield from body.get(key, [])
            cursor = body.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                return
