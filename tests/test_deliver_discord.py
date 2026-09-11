"""Story 4.3 — ``post_discord_text`` + ``webhook_id`` (raw ``httpx`` webhook POST).

An injected ``httpx.Client`` backed by ``httpx.MockTransport`` stands in for
Discord: a 204 returns the non-secret webhook id; a 401 / 404 and a transport
error chain a ``DeliveryError``; a malformed URL and an oversized ``content`` raise
``DeliveryError`` with the transport never touched; the request body is exactly
``{"content": ..., "allowed_mentions": {"parse": []}}`` — no ``embeds`` / ``files``,
every ping suppressed; unicode round-trips; and
``webhook_id`` extracts the numeric id, never the token.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import httpx
import pytest

from commishdesk.deliver import post_discord_text
from commishdesk.deliver.discord import webhook_id
from commishdesk.errors import CommishDeskError, DeliveryError

REPO_ROOT = Path(__file__).resolve().parent.parent

_GOOD_URL = "https://discord.com/api/webhooks/123456789012345678/aB_cD-3fToKeNtOkEn"
_GOOD_ID = "123456789012345678"

#: ``allowed_mentions`` with an empty ``parse`` list suppresses every ping.
_NO_PINGS = {"parse": []}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _recording_handler(status: int, sink: dict):
    def handle(request: httpx.Request) -> httpx.Response:
        sink["url"] = str(request.url)
        sink["method"] = request.method
        sink["headers"] = dict(request.headers)
        sink["body"] = request.content.decode("utf-8")
        return httpx.Response(status)

    return handle


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_204_returns_the_webhook_id_and_posts_exactly_content() -> None:
    sink: dict = {}
    client = _client(_recording_handler(204, sink))
    assert post_discord_text(_GOOD_URL, "Draft recap is up.", client=client) == _GOOD_ID
    assert sink["method"] == "POST"
    assert sink["url"] == _GOOD_URL
    assert json.loads(sink["body"]) == {
        "content": "Draft recap is up.",
        "allowed_mentions": _NO_PINGS,
    }
    # content + ping suppression only — no embed, no attachment, no identity override
    for forbidden in ("embeds", "file", "username", "avatar_url"):
        assert forbidden not in sink["body"]
    assert sink["headers"].get("content-type", "").startswith("application/json")
    assert "multipart/form-data" not in sink["headers"].get("content-type", "")


def test_any_2xx_is_accepted() -> None:
    sink: dict = {}
    assert (
        post_discord_text(_GOOD_URL, "ok", client=_client(_recording_handler(200, sink)))
        == _GOOD_ID
    )
    assert (
        post_discord_text(
            "https://canary.discordapp.com/api/webhooks/42/tok-tok",
            "ok",
            client=_client(_recording_handler(201, {})),
        )
        == "42"
    )


def test_called_twice_with_identical_args_posts_identical_bodies() -> None:
    bodies: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content.decode("utf-8"))
        return httpx.Response(204)

    client = _client(handle)
    post_discord_text(_GOOD_URL, "same", client=client)
    post_discord_text(_GOOD_URL, "same", client=client)
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0]) == {"content": "same", "allowed_mentions": _NO_PINGS}


def test_unicode_content_round_trips() -> None:
    sink: dict = {}
    payload = 'É — 🏈 "quote" ‏rtl'
    post_discord_text(_GOOD_URL, payload, client=_client(_recording_handler(204, sink)))
    assert json.loads(sink["body"]) == {
        "content": payload,
        "allowed_mentions": _NO_PINGS,
    }


def test_at_everyone_in_the_text_produces_no_allowed_mentions_parse_entry() -> None:
    sink: dict = {}
    post_discord_text(
        _GOOD_URL,
        "@everyone the draft recap is up <@&123> @here",
        client=_client(_recording_handler(204, sink)),
    )
    body = json.loads(sink["body"])
    assert body["allowed_mentions"] == {"parse": []}
    assert "@everyone" in body["content"]  # kept as literal text, just not pinged


# --------------------------------------------------------------------------- #
# Faults — every one chains a DeliveryError
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [401, 403, 404])
def test_rejected_webhook_chains_delivery_error(status: int) -> None:
    client = _client(lambda request: httpx.Response(status, json={"message": "no"}))
    with pytest.raises(DeliveryError) as excinfo:
        post_discord_text(_GOOD_URL, "hi", client=client)
    assert isinstance(excinfo.value, CommishDeskError)
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)
    assert str(status) in str(excinfo.value)
    assert "may be deleted or the URL mistyped" in str(excinfo.value)
    # the token is never in the message
    assert "aB_cD-3fToKeNtOkEn" not in str(excinfo.value)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_status_gets_retry_wording_not_a_misdiagnosis(status: int) -> None:
    client = _client(lambda request: httpx.Response(status, json={"message": "later"}))
    with pytest.raises(DeliveryError) as excinfo:
        post_discord_text(_GOOD_URL, "hi", client=client)
    assert isinstance(excinfo.value.__cause__, httpx.HTTPStatusError)
    assert "temporarily unavailable" in str(excinfo.value)
    assert "retry shortly" in str(excinfo.value)
    assert "deleted or the URL mistyped" not in str(excinfo.value)
    assert "aB_cD-3fToKeNtOkEn" not in str(excinfo.value)


def test_transport_failure_chains_delivery_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    with pytest.raises(DeliveryError) as excinfo:
        post_discord_text(_GOOD_URL, "hi", client=_client(boom))
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
    assert "aB_cD-3fToKeNtOkEn" not in str(excinfo.value)


def test_malformed_url_raises_before_any_network_call() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    with pytest.raises(DeliveryError, match="Discord webhook"):
        post_discord_text("https://example.com/hook", "hi", client=_client(handle))
    assert calls == 0


def test_url_shape_is_the_first_guard_checked() -> None:
    """A call wrong in more than one way surfaces the URL-shape error first (the
    I/O matrix treats URL shape as the first guard)."""
    with pytest.raises(DeliveryError, match="Discord webhook"):
        post_discord_text("https://example.com/hook", "x" * 5000)
    with pytest.raises(DeliveryError, match="Discord webhook"):
        post_discord_text("https://example.com/hook", "   ")


def test_empty_or_whitespace_content_raises_before_any_network_call() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    for blank in ("", "   ", "\n\t "):
        with pytest.raises(DeliveryError, match="empty"):
            post_discord_text(_GOOD_URL, blank, client=_client(handle))
    assert calls == 0


def test_non_discord_lookalike_host_is_rejected() -> None:
    for bad in (
        "https://discord.com/api/webhooks/abc/tok",  # non-numeric id
        "http://discord.com/api/webhooks/1/tok",  # not https
        "https://evil-discord.com/api/webhooks/1/tok",
        "https://discord.com/api/webhooks/1/",  # empty token
    ):
        with pytest.raises(DeliveryError):
            post_discord_text(bad, "hi", client=_client(lambda r: httpx.Response(204)))


def test_oversized_content_raises_before_any_network_call() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    with pytest.raises(DeliveryError, match="2000"):
        post_discord_text(_GOOD_URL, "x" * 2001, client=_client(handle))
    assert calls == 0
    # exactly 2000 is fine
    post_discord_text(_GOOD_URL, "x" * 2000, client=_client(handle))
    assert calls == 1


def test_injected_client_is_not_closed_by_the_call() -> None:
    client = _client(lambda request: httpx.Response(204))
    post_discord_text(_GOOD_URL, "hi", client=client)
    assert not client.is_closed
    client.close()


class _LenientResponse:
    """A response whose ``raise_for_status`` never raises — to prove the explicit
    ``is_success`` guard catches a non-2xx that a lenient client would let by."""

    def __init__(self, status: int) -> None:
        self.status_code = status
        self.is_success = 200 <= status < 300

    def raise_for_status(self) -> _LenientResponse:
        return self


class _StubClient:
    def __init__(self, status: int) -> None:
        self._status = status
        self.closed = False

    def post(self, url: str, *, json: object, timeout: float) -> _LenientResponse:
        return _LenientResponse(self._status)

    def close(self) -> None:
        self.closed = True


def test_non_2xx_that_slips_past_raise_for_status_is_a_delivery_error() -> None:
    with pytest.raises(DeliveryError, match="did not accept the post"):
        post_discord_text(_GOOD_URL, "hi", client=_StubClient(304))


def test_owned_client_is_built_and_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``verify-webhook`` calls with ``client=None``; the self-built client must be
    closed. Patch ``httpx.Client`` so the call never touches the network."""
    built: list[_StubClient] = []
    posted: dict = {}

    class _RecordingClient(_StubClient):
        def __init__(self, *a: object, **k: object) -> None:
            super().__init__(204)
            built.append(self)

        def post(self, url: str, *, json: object, timeout: float) -> _LenientResponse:
            posted["url"] = url
            posted["json"] = json
            return super().post(url, json=json, timeout=timeout)

    monkeypatch.setattr(httpx, "Client", _RecordingClient)
    assert post_discord_text(_GOOD_URL, "hi") == _GOOD_ID
    assert len(built) == 1 and built[0].closed is True
    assert posted["url"] == _GOOD_URL
    assert posted["json"] == {"content": "hi", "allowed_mentions": _NO_PINGS}


def test_a_rendered_summary_posts_through_unchanged() -> None:
    """End to end: a real ``render_discord_summary`` output goes out as the POST
    ``content`` byte for byte."""
    from commishdesk.facts.schema import DraftRecapFacts
    from commishdesk.narrate import render_draft_recap
    from commishdesk.render import render_discord_summary

    facts_path = (
        REPO_ROOT / "tests" / "fixtures" / "facts" / "expected-draft-recap-facts.json"
    )
    facts = DraftRecapFacts.model_validate(
        json.loads(facts_path.read_text(encoding="utf-8"))
    )
    summary = render_discord_summary(facts, recap=render_draft_recap(facts.narration))

    sink: dict = {}
    assert (
        post_discord_text(_GOOD_URL, summary, client=_client(_recording_handler(204, sink)))
        == _GOOD_ID
    )
    assert json.loads(sink["body"])["content"] == summary


# --------------------------------------------------------------------------- #
# webhook_id
# --------------------------------------------------------------------------- #


def test_webhook_id_extracts_the_numeric_id_never_the_token() -> None:
    assert webhook_id(_GOOD_URL) == "123456789012345678"
    assert webhook_id("https://canary.discordapp.com/api/webhooks/42/tok-tok") == "42"


def test_webhook_id_rejects_a_non_webhook_url() -> None:
    with pytest.raises(DeliveryError):
        webhook_id("https://example.com/hook")


# --------------------------------------------------------------------------- #
# AD-1 import fence
# --------------------------------------------------------------------------- #


def test_discord_deliver_module_imports_only_the_sanctioned_surface() -> None:
    tree = ast.parse(
        (REPO_ROOT / "commishdesk" / "deliver" / "discord.py").read_text(encoding="utf-8")
    )
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    external = roots - sys.stdlib_module_names
    assert external <= {"httpx", "commishdesk"}, external
