"""APNs-via-relay delivery (app/push.py) against a fake relay: what prunes a
device token and what must not, the BadDeviceToken environment retry, and
alert truncation for Apple's payload limit. No network — the relay is an
httpx.MockTransport."""

import json
import logging

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

TOPIC = "com.jworthington.colloqui"
SENT = {"status": "sent", "response_ms": 12}


def gone(reason: str) -> httpx.Response:
    return httpx.Response(410, json={"detail": reason, "reason": reason})


@pytest_asyncio.fixture
async def relay(monkeypatch, make_user):
    """A configured relay whose answers the test scripts. `relay.respond` maps a
    request's JSON payload to an httpx.Response; every payload received lands in
    `relay.requests`."""
    from app import push
    from app.config import settings
    from app.db import SessionLocal
    from app.models import DeviceToken

    monkeypatch.setattr(settings, "push_relay_url", "http://relay:8000")
    monkeypatch.setattr(settings, "push_relay_api_key", "relaykey")
    monkeypatch.setattr(settings, "apns_topic", TOPIC)

    class Relay:
        def __init__(self):
            self.requests: list[dict] = []
            self.raw: list[bytes] = []
            self.respond = lambda payload: httpx.Response(200, json=SENT)
            self.user_id = None

        def _handle(self, request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "http://relay:8000/notify"
            assert request.headers["X-API-Key"] == "relaykey"
            self.raw.append(request.content)
            payload = json.loads(request.content)
            self.requests.append(payload)
            return self.respond(payload)

        async def add_token(self, token: str, environment: str = "production"):
            async with SessionLocal() as db:
                db.add(DeviceToken(token=token, user_id=self.user_id,
                                   platform="ios", environment=environment))
                await db.commit()

        async def tokens(self) -> dict[str, str]:
            async with SessionLocal() as db:
                rows = (await db.scalars(select(DeviceToken).where(
                    DeviceToken.user_id == self.user_id))).all()
            return {t.token: t.environment for t in rows}

        async def deliver(self, title="hi", body="there", data=None, badge=1) -> int:
            return await push._deliver(self.user_id, title, body, data, badge)

    r = Relay()
    _, r.user_id = await make_user("carol")
    client = httpx.AsyncClient(transport=httpx.MockTransport(r._handle))
    monkeypatch.setattr(push, "_client", client)
    yield r
    await client.aclose()


# ---- what prunes a token, and what must not --------------------------------

async def test_sent_is_counted_and_keeps_the_token(relay):
    await relay.add_token("LIVE")
    assert await relay.deliver() == 1
    assert len(relay.requests) == 1
    assert await relay.tokens() == {"LIVE": "production"}


@pytest.mark.parametrize("reason", ["Unregistered", "DeviceTokenNotForTopic"])
async def test_410_with_a_fatal_reason_prunes_immediately(relay, reason):
    await relay.add_token("GONE")
    await relay.add_token("LIVE")
    relay.respond = lambda p: gone(reason) if p["device_token"] == "GONE" else httpx.Response(200, json=SENT)
    assert await relay.deliver() == 1
    assert len(relay.requests) == 2  # one per token — no retry for these reasons
    assert await relay.tokens() == {"LIVE": "production"}


async def test_422_echoing_a_reason_word_does_not_prune(relay):
    # The relay's validation errors echo the request back. A message that just
    # SAYS "Unregistered" must never be able to delete a live token.
    await relay.add_token("LIVE")
    text = "Heads up: the domain shows as Unregistered / BadDeviceToken"

    def respond(p):
        return httpx.Response(422, json={"detail": [{
            "type": "string_too_long", "loc": ["body", "body"],
            "msg": "String should have at most 10 characters", "input": p["body"],
        }]})

    relay.respond = respond
    assert await relay.deliver(body=text) == 0
    assert relay.requests[0]["body"] == text
    assert len(relay.requests) == 1
    assert await relay.tokens() == {"LIVE": "production"}


@pytest.mark.parametrize("response", [
    httpx.Response(502, json={"detail": "APNs delivery failed"}),
    httpx.Response(413, json={"detail": "Payload too large for APNs"}),
    # A string detail that merely mentions a reason (say, echoed message text).
    httpx.Response(400, json={"detail": "Rejected: 'is the domain Unregistered?'"}),
    # Right word, wrong status: only a 410 may prune.
    httpx.Response(502, json={"detail": "Unregistered", "reason": "Unregistered"}),
    # Right status, but the reason is not one of the three (or is only in detail).
    httpx.Response(410, json={"detail": "Unregistered", "reason": "ExpiredProviderToken"}),
    httpx.Response(410, json={"detail": "Unregistered"}),
    httpx.Response(410, json={"detail": "x", "reason": "Unregistered (probably)"}),
    httpx.Response(410, content=b"gone"),
], ids=["502", "413", "400-string-echo", "502-reason", "410-other", "410-detail-only", "410-inexact", "410-not-json"])
async def test_other_failures_do_not_prune(relay, response):
    await relay.add_token("LIVE")
    relay.respond = lambda p: response
    assert await relay.deliver() == 0
    assert len(relay.requests) == 1
    assert await relay.tokens() == {"LIVE": "production"}


async def test_429_warns_with_retry_after_and_does_not_prune(relay, caplog):
    await relay.add_token("LIVE")
    relay.respond = lambda p: httpx.Response(
        429, json={"detail": "Rate limit exceeded"}, headers={"Retry-After": "30"})
    with caplog.at_level(logging.WARNING, logger="push"):
        assert await relay.deliver() == 0
    assert len(relay.requests) == 1  # no retry loop
    assert await relay.tokens() == {"LIVE": "production"}
    warnings = [r for r in caplog.records if r.name == "push" and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "429" in warnings[0].getMessage() and "30" in warnings[0].getMessage()


async def test_a_422_does_not_put_the_message_text_in_the_logs(relay, caplog):
    await relay.add_token("LIVE")
    relay.respond = lambda p: httpx.Response(422, json={"detail": [{"input": p["body"]}]})
    with caplog.at_level(logging.DEBUG, logger="push"):
        await relay.deliver(body="the launch code is 0000")
    assert "launch code" not in caplog.text


# ---- BadDeviceToken: try the other environment before giving up -------------

@pytest.mark.parametrize("stored, actual", [("production", "sandbox"), ("sandbox", "production")])
async def test_bad_device_token_retries_the_other_environment_and_corrects_it(relay, stored, actual):
    await relay.add_token("MIXEDUP", stored)
    relay.respond = lambda p: (
        httpx.Response(200, json=SENT) if p["sandbox"] == (actual == "sandbox")
        else gone("BadDeviceToken"))

    assert await relay.deliver(data={"channel_id": "c1"}) == 1  # counted as sent

    assert [r["sandbox"] for r in relay.requests] == [stored == "sandbox", actual == "sandbox"]
    first, second = relay.requests
    assert {**first, "sandbox": None} == {**second, "sandbox": None}  # same push otherwise
    assert await relay.tokens() == {"MIXEDUP": actual}  # kept, environment corrected

    # Next push goes straight to the right environment: one request, no retry.
    relay.requests.clear()
    assert await relay.deliver() == 1
    assert [r["sandbox"] for r in relay.requests] == [actual == "sandbox"]


@pytest.mark.parametrize("second", ["BadDeviceToken", "Unregistered"])
async def test_bad_device_token_in_both_environments_prunes(relay, second):
    await relay.add_token("DEAD", "production")
    relay.respond = lambda p: gone(second if p["sandbox"] else "BadDeviceToken")
    assert await relay.deliver() == 0
    assert [r["sandbox"] for r in relay.requests] == [False, True]  # exactly two
    assert await relay.tokens() == {}


async def test_inconclusive_retry_keeps_the_token_and_its_environment(relay):
    await relay.add_token("MAYBE", "production")
    relay.respond = lambda p: (
        httpx.Response(502, json={"detail": "APNs delivery failed"}) if p["sandbox"]
        else gone("BadDeviceToken"))
    assert await relay.deliver() == 0
    assert len(relay.requests) == 2
    assert await relay.tokens() == {"MAYBE": "production"}


async def test_retry_is_per_token_and_leaves_other_devices_alone(relay):
    await relay.add_token("PHONE", "production")
    await relay.add_token("XCODE", "production")  # really a sandbox token

    def respond(p):
        if p["device_token"] == "XCODE" and not p["sandbox"]:
            return gone("BadDeviceToken")
        return httpx.Response(200, json=SENT)

    relay.respond = respond
    assert await relay.deliver() == 2
    per_token = [r["device_token"] for r in relay.requests]
    assert per_token.count("PHONE") == 1 and per_token.count("XCODE") == 2
    assert await relay.tokens() == {"PHONE": "production", "XCODE": "sandbox"}


# ---- long messages still notify --------------------------------------------

DATA = {
    "channel_id": "3b1f0b7e-5d0a-4c55-9a53-0e8d1c1f7a11",
    "root_id": "9f6f3c52-4a5e-4d0b-8b3e-7f2f1f0c9d22",
    "message_id": "0a4f6f1e-2f6b-4b7c-9d8e-6c5b4a392817",
    "skipped": None,
}
DATA_SENT = {k: v for k, v in DATA.items() if v is not None}


def apns_bytes(p: dict) -> int:
    """Size of the APNs payload the relay builds from this request — it
    serializes with json.dumps' default (ASCII-escaped) encoding and checks
    that against Apple's 4096."""
    aps = {"alert": {"title": p["title"], "body": p["body"]}, "sound": "default",
           "badge": p["badge"], "mutable-content": 1}
    return len(json.dumps({"aps": aps, **p.get("custom_data", {})}).encode())


def alert_bytes(p: dict) -> int:
    parts = {"title": p["title"], "body": p["body"], "custom_data": p.get("custom_data", {})}
    return max(len(json.dumps(parts).encode()),
               len(json.dumps(parts, ensure_ascii=False).encode()))


@pytest.mark.parametrize("message", [
    "x" * 10_000,
    "word " * 2_000,
    "😀" * 2_000,
    "漢字かな" * 2_500,
    "👨‍👩‍👧‍👦" * 500,          # ZWJ families: must not end on a dangling joiner
    "🇨🇦🇫🇷" * 1_000,
], ids=["10k-ascii", "10k-words", "2k-emoji", "10k-cjk", "zwj-families", "flags"])
async def test_long_message_is_truncated_to_fit(relay, message):
    await relay.add_token("LIVE")
    title = "Carol in #general"
    assert await relay.deliver(title=title, body=message, data=DATA, badge=3) == 1

    (sent,) = relay.requests
    relay.raw[0].decode("utf-8")  # the request itself is valid UTF-8
    body = sent["body"]
    body.encode("utf-8")  # no lone surrogates / split characters
    assert body.endswith("…")
    assert len(body) <= 300
    assert len(body) > 100  # truncated, not gutted
    assert not body[:-1].endswith(("‍", " "))
    assert message.startswith(body[:-1])  # a clean prefix of the original
    assert alert_bytes(sent) <= 3500
    assert apns_bytes(sent) <= 4096
    # Only the alert body changed.
    assert sent["title"] == title
    assert sent["custom_data"] == DATA_SENT
    assert sent["badge"] == 3


@pytest.mark.parametrize("message", [
    "there",
    "Lunch at 12? 🍜 — see you at 渋谷",
    "x" * 300,                # exactly at the cap: not touched
    "line one\nline two  ",   # trailing whitespace is the sender's business
], ids=["short", "mixed", "at-cap", "whitespace"])
async def test_short_message_is_sent_byte_for_byte(relay, message):
    await relay.add_token("LIVE")
    await relay.deliver(title="Carol", body=message, data=DATA)
    (sent,) = relay.requests
    assert sent["body"] == message
    assert sent["body"].encode() == message.encode()
    assert sent["title"] == "Carol"
    assert sent["custom_data"] == DATA_SENT
    # And the exact bytes are on the wire (httpx sends raw UTF-8, not \u escapes).
    assert json.dumps(message, ensure_ascii=False).encode() in relay.raw[0]


async def test_an_oversized_title_cannot_sink_the_push(relay):
    await relay.add_token("LIVE")
    await relay.deliver(title="😀" * 500, body="😀" * 2_000, data=DATA)
    (sent,) = relay.requests
    assert len(sent["title"]) <= 100 and sent["title"].endswith("…")
    assert sent["body"].endswith("…")
    assert alert_bytes(sent) <= 3500
    assert apns_bytes(sent) <= 4096
