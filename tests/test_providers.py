import gzip
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.providers import Adapter, ProviderError, retry_after
from app.service import MonitoringService
from tests.conftest import h1_record


@pytest.mark.parametrize("valid", [True, False])
async def test_streamed_compression_decoded_once(settings, valid):
    raw = gzip.compress(json.dumps({"data": [h1_record()], "links": {"next": None}}).encode())
    if not valid:
        raw = b"invalid gzip data"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-length": str(len(raw))},
                stream=httpx.ByteStream(raw),
            )
        )
    ) as client:
        if valid:
            events, cursor = await Adapter("hackerone", settings, client).fetch("hacktivity", 0)
            assert len(events) == 1 and cursor is None
        else:
            with pytest.raises(ProviderError, match="schema_error"):
                await Adapter("hackerone", settings, client).fetch("hacktivity", 0)


async def test_hackerone_normalization_and_auth(settings):
    settings.hackerone_username = "test-user"
    # Rebuild through validation so secrets remain SecretStr.
    settings = type(settings)(
        **{**settings.model_dump(), "hackerone_username": "test-user", "hackerone_token": "test-token"}
    )

    def handler(request):
        assert request.url.host == "api.hackerone.com"
        assert request.url.path == "/v1/hackers/hacktivity"
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"data": [h1_record()], "links": {"next": None}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, cursor = await Adapter("hackerone", settings, client).fetch("hacktivity", 0)
    assert cursor is None and events[0].program == "example"
    assert events[0].state == "resolved"


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, "auth_error"),
        (403, "auth_error"),
        (429, "rate_limited"),
        (302, "redirect_rejected"),
        (404, "request_error"),
        (500, "upstream_unavailable"),
        (408, "upstream_unavailable"),
    ],
)
async def test_http_outcomes(settings, code, expected):
    settings.max_retries = 0
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(code, headers={"Retry-After": "90", "Location": "https://evil.test"})
        )
    ) as client:
        with pytest.raises(ProviderError) as exc:
            await Adapter("hackerone", settings, client).fetch("hacktivity", 0)
        assert exc.value.code == expected
        if code == 429:
            assert exc.value.delay == 90


@pytest.mark.parametrize("retries", [0, 1, 3])
async def test_provider_attempts_initial_plus_n(settings, monkeypatch, retries):
    calls = []
    settings.max_retries = retries
    monkeypatch.setattr("app.providers.asyncio.sleep", AsyncMock())

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError):
            await Adapter("hackerone", settings, client).fetch("hacktivity", 0)
    assert len(calls) == retries + 1


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"id": None, "attributes": {}}]},
        {"data": "wrong"},
        {"data": [h1_record(), h1_record()]},
        {"data": [h1_record()], "links": {"next": "https://evil.test/v1/hackers/hacktivity?page[number]=2"}},
    ],
)
async def test_invalid_page_is_rejected(settings, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ProviderError):
            await Adapter("hackerone", settings, client).fetch("hacktivity", 0)


async def test_hackerone_pagination_preserves_page(settings):
    body = {
        "data": [h1_record()],
        "links": {"next": "https://api.hackerone.com/v1/hackers/hacktivity?page%5Bnumber%5D=8"},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    ) as client:
        _, cursor = await Adapter("hackerone", settings, client).fetch("hacktivity", 7)
    assert cursor == 8


def test_bugcrowd_documented_flat_submission(settings):
    adapter = Adapter("bugcrowd", settings, None)
    event = adapter.normalize(
        "submissions",
        {
            "id": "abc-123",
            "engagement_id": "eng-1",
            "title": "Issue",
            "state": "triaged",
            "severity": "p2_severe",
            "needs_action": False,
            "last_activity_at": None,
        },
    )
    assert event.severity == "high" and event.occurred_at is None
    assert event.url == "https://hacker-api.bugcrowd.com/v1/submissions/abc-123"


def test_bugcrowd_engagement_and_offset(settings):
    settings.page_size = 1
    adapter = Adapter("bugcrowd", settings, None)
    event = adapter.normalize(
        "engagements",
        {
            "id": "abc-123",
            "type": "engagement",
            "attributes": {"name": "Program", "state": "in_progress", "created_at": "2026-01-01T00:00:00Z"},
        },
    )
    assert event.title == "Program"
    assert (
        adapter.next_cursor(
            "engagements", {"pagination": {"offset": 3, "limit": 1, "total_count": 5, "has_more": True}}, 3, 1
        )
        == 4
    )


@pytest.mark.parametrize(
    "next_link",
    [
        "https://api.hackerone.com/v1/hackers/hacktivity?page[number]=1",
        "https://api.hackerone.com:444/v1/hackers/hacktivity?page[number]=2",
        "https://api.hackerone.com/v1/me?page[number]=2",
    ],
)
def test_pagination_cannot_loop_or_escape(settings, next_link):
    with pytest.raises(ProviderError):
        Adapter("hackerone", settings, None).next_cursor("hacktivity", {"links": {"next": next_link}}, 1, 1)


def test_retry_after_seconds_date_and_json():
    assert retry_after(httpx.Response(429, json={"retry_after": 2.5})) == 2.5
    assert retry_after(httpx.Response(429, headers={"Retry-After": "nan"})) == 60
    assert retry_after(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"})) > 60


async def test_failed_sibling_does_not_discard_success(db, settings, event):
    db.ensure_collection("hackerone", "reports")

    class FakeAdapter:
        name = "hackerone"
        collections = ["hacktivity", "reports"]

        async def fetch(self, collection, cursor):
            if collection == "reports":
                raise ProviderError("schema_error")
            return [event], None

    service = MonitoringService(db, settings, None)
    await service.poll_once(FakeAdapter())
    assert db.snapshot()["event_count"] == 1
    assert db.checkpoint("hackerone", "hacktivity")["baseline_done"]
    assert db.checkpoint("hackerone", "reports")["cursor"] == 0
    assert db.checkpoint("hackerone", "reports")["error_code"] == "schema_error"


async def test_auth_pause_prevents_sibling_requests(db, settings):
    calls = []

    class FakeAdapter:
        name = "hackerone"
        collections = ["hacktivity", "reports"]

        async def fetch(self, collection, cursor):
            calls.append(collection)
            raise ProviderError("auth_error", 3600)

    service = MonitoringService(db, settings, None)
    await service.poll_once(FakeAdapter())
    await service.poll_once(FakeAdapter())
    assert calls == ["hacktivity"]


async def test_supervisor_recovers_unexpected_exit(db, settings, monkeypatch):
    service = MonitoringService(db, settings, None)
    service.worker_health["test"] = {"state": "starting", "restarts": 0, "error_code": None}
    calls = 0

    async def worker():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Secret should never be logged")
        service.stopping = True

    monkeypatch.setattr("app.service.backoff", lambda n: 0)
    await service.supervise("test", worker)
    assert calls == 2 and service.worker_health["test"]["restarts"] == 1
