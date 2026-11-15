import copy
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.config import Settings
from app.discord import build_message
from app.providers import Adapter, ProviderError
from app.service import MonitoringService

PROGRAM_ID = "a21b0012-18e0-4ea3-9d8b-329bfb1f2146"


def program():
    return {
        "id": PROGRAM_ID,
        "handle": "example",
        "name": "Example program",
        "following": False,
        "minBounty": {"value": 100},
        "maxBounty": {"value": 1000},
        "confidentialityLevel": {"id": 1, "value": "Public"},
        "status": {"id": 1, "value": "Open"},
        "type": {"id": 1, "value": "Bug bounty"},
        "webLinks": {"detail": "https://app.intigriti.com/programs/example/example/detail"},
    }


def activity():
    return {
        "programId": PROGRAM_ID,
        "activity": {"fromStatus": 1, "toStatus": 2},
        "type": {"id": 3, "value": "New program status available"},
        "createdAt": 1794583531,
        "following": False,
    }


def configured(settings):
    return Settings(**{**settings.model_dump(), "intigriti_enabled": True, "intigriti_token": "test-token"})


async def test_intigriti_auth_paths_pagination_and_window(settings):
    settings = configured(settings)
    settings.page_size = 2
    settings.intigriti_activity_window = 3
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.intigriti.com"
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.url.path == "/external/researcher/v1/programs/activities"
        records = []
        for i in range(int(request.url.params["limit"])):
            item = activity()
            item["createdAt"] -= i + int(request.url.params["offset"])
            records.append(item)
        return httpx.Response(200, json={"maxCount": 100, "records": records})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Adapter("intigriti", settings, client)
        events, cursor = await adapter.fetch("activities", 0)
        assert len(events) == 2 and cursor == 2
        events, cursor = await adapter.fetch("activities", cursor)
        assert len(events) == 1 and cursor is None
        assert calls[-1].url.params["limit"] == "1"
        assert adapter.path("programs") == "/v1/programs"
        with pytest.raises(ProviderError):
            adapter.path("payouts")


def test_activity_identity_timestamp_and_following(settings):
    adapter = Adapter("intigriti", configured(settings), None)
    record = activity()
    original = adapter.normalize("activities", record)
    assert original.occurred_at == datetime.fromtimestamp(1794583531, timezone.utc)
    record["following"] = True
    assert adapter.normalize("activities", record).fingerprint() == original.fingerprint()
    record["activity"]["toStatus"] = 3
    assert adapter.normalize("activities", record).external_id != original.external_id


def test_program_bounty_change_is_a_revision(settings):
    adapter = Adapter("intigriti", configured(settings), None)
    record = program()
    original = adapter.normalize("programs", record)
    record["following"] = True
    assert adapter.normalize("programs", record).fingerprint() == original.fingerprint()
    record["maxBounty"]["value"] = 2000
    changed = adapter.normalize("programs", record)
    assert changed.external_id == original.external_id
    assert changed.fingerprint() != original.fingerprint()


@pytest.mark.parametrize(
    "change",
    [
        {"createdAt": True},
        {"createdAt": 1794583531000},
        {"programId": "bad"},
        {"activity": []},
        {"following": "false"},
        {"type": {"id": "3", "value": "changed"}},
    ],
)
async def test_invalid_activity_preserves_page(settings, change):
    record = {**activity(), **change}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"maxCount": 1, "records": [record]})
        )
    ) as client:
        with pytest.raises(ProviderError, match="schema_error"):
            await Adapter("intigriti", configured(settings), client).fetch("activities", 0)


@pytest.mark.parametrize("total", [-1, True, "2", None, 0])
def test_invalid_total_rejected(settings, total):
    with pytest.raises(ProviderError):
        Adapter("intigriti", configured(settings), None).next_cursor("programs", {"maxCount": total}, 0, 1)


def test_program_url_cannot_escape_provider(settings):
    record = program()
    record["webLinks"]["detail"] = "https://evil.example/collect"
    with pytest.raises(ValueError):
        Adapter("intigriti", configured(settings), None).normalize("programs", record)


@pytest.mark.parametrize(
    "target,valid",
    [
        ("/programs/example/example/detail", True),
        ("/programs/example org/example/detail", True),
        ("https://evil.example", False),
        ("//evil.example", False),
        ("/programs/../account", False),
    ],
)
def test_official_program_login_redirect(settings, target, valid):
    from urllib.parse import quote, urlencode

    record = program()
    record["webLinks"]["detail"] = "https://app.intigriti.com/researcher/login?" + urlencode(
        {"redirect": target}
    )
    adapter = Adapter("intigriti", configured(settings), None)
    if valid:
        assert adapter.normalize("programs", record).url == "https://app.intigriti.com" + quote(
            target, safe="/"
        )
    else:
        with pytest.raises(ProviderError):
            adapter.normalize("programs", record)


async def test_live_service_baseline_repeat_and_new_activity(db, settings):
    settings = configured(settings)
    current = [activity()]

    def handler(request):
        records = current if request.url.path.endswith("activities") else [program()]
        return httpx.Response(200, json={"maxCount": len(records), "records": records})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = MonitoringService(db, settings, client)
        adapter = service.adapters["intigriti"]
        for collection in adapter.collections:
            db.ensure_collection("intigriti", collection)
        await service.poll_once(adapter)
        await service.poll_once(adapter)
        assert db.snapshot()["event_count"] == 2
        assert db.notification_status()["counts"]["pending"] == 0
        new = copy.deepcopy(activity())
        new["createdAt"] += 60
        current.insert(0, new)
        await service.poll_once(adapter)
        assert db.notification_status()["counts"]["pending"] == 1
        integration = next(i for i in service.status()["integrations"] if i["name"] == "intigriti")
        assert integration["status"] == "healthy" and integration["reason"] is None


def test_existing_provider_fingerprint_unchanged(event):
    previous = event.model_dump(mode="json", exclude={"source_fingerprint"})
    assert event.payload() == json.dumps(previous, sort_keys=True, separators=(",", ":"))


def test_intigriti_auth_alert_supported():
    message = build_message({"kind": "operational", "provider": "intigriti", "code": "auth_error"})
    assert "intigriti" in json.dumps(message).lower()
