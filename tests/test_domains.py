import json
import time
from unittest.mock import Mock

import httpx
import pytest

from app.config import Settings
from app.discord import Dispatcher, build_message
from app.domains import DomainStore, current_scope, domain_name, poll_domains, scope_domains
from app.github_runner import dashboard, run_cycle
from app.providers import Adapter, ProviderError
from app.service import MonitoringService
from tests.test_github_runner import CaptureState

P = "a21b0012-18e0-4ea3-9d8b-329bfb1f2146"
OLD = "b21b0012-18e0-4ea3-9d8b-329bfb1f2146"
NEW = "c21b0012-18e0-4ea3-9d8b-329bfb1f2146"
LATER = "d21b0012-18e0-4ea3-9d8b-329bfb1f2146"
URL = "https://app.intigriti.com/programs/example/test/detail"


def cfg(settings):
    return Settings(
        **{
            **settings.model_dump(),
            "intigriti_enabled": True,
            "intigriti_token": "test-secret",
            "intigriti_domains_only": True,
            "intigriti_collections": "activities",
        }
    )


def asset(endpoint, tier=4, kind=1):
    return {
        "id": P,
        "endpoint": endpoint,
        "type": {"id": kind, "value": {1: "Url", 7: "Wildcard", 2: "Android"}[kind]},
        "tier": {"id": tier, "value": {1: "No Bounty", 4: "Tier 1", 5: "Out Of Scope"}[tier]},
    }


def version(identity, endpoints):
    return {
        "id": identity,
        "createdAt": int(time.time()) - 100,
        "content": [asset(e, kind=7 if e.startswith("*.") else 1) for e in endpoints],
    }


def change(created=None, after=NEW, before=OLD):
    return {
        "programId": P,
        "activity": {"fromVersionId": before, "toVersionId": after},
        "createdAt": created if created is not None else int(time.time()) - 100,
        "type": {"id": 1, "value": "New domains version added"},
        "following": False,
    }


def detail(endpoints, opened=True):
    return {
        "id": P,
        "domains": version(NEW, endpoints),
        "status": {"id": 3 if opened else 4, "value": "Open" if opened else "Closed"},
        "webLinks": {"detail": URL},
    }


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://Api.Example.com/path", "api.example.com"),
        ("*.Example.com", "*.example.com"),
        ("https://example.com/path/*", "example.com"),
        ("https://127.0.0.1", None),
        ("10.0.0.0/8", None),
        ("localhost", None),
        ("https://user:pass@example.com", None),
        ("example.com\n@everyone", None),
        ("ftp://example.com", None),
        ("foo*.example.com", None),
    ],
)
def test_domain_extraction_preserves_boundaries(endpoint, expected):
    assert domain_name(endpoint) == expected


def test_scope_excludes_out_of_scope_apps_and_unknown_tiers():
    data = version(OLD, [])
    data["content"] = [
        asset("https://allowed.example/path"),
        asset("*.allowed.example", kind=7),
        asset("excluded.example", tier=5),
        asset("android.example", kind=2),
        asset("free.example", tier=1),
    ]
    result = scope_domains(data, OLD)
    assert set(result) == {"allowed.example", "*.allowed.example", "free.example"}
    assert result["allowed.example"] == ["https://allowed.example/path"]
    data["content"][0]["tier"]["value"] = "Unknown tier"
    assert "allowed.example" not in scope_domains(data)


@pytest.mark.parametrize("bad", [None, {}, "missing"])
def test_unavailable_previous_scope_is_not_empty_scope(bad):
    with pytest.raises(ProviderError):
        scope_domains({"id": OLD, "content": bad})
    with pytest.raises(ProviderError):
        scope_domains(version(OLD, []), NEW)


def test_closed_program_has_no_current_testing_domains():
    assert current_scope(detail(["example.com"], False), P)[0] == {}


async def test_version_comparison_baseline_new_addition_and_dedup(db, settings):
    settings = cfg(settings)
    db.ensure_collection("intigriti", "domains")
    records = [change()]
    versions = {
        OLD: version(OLD, ["existing.example"]),
        NEW: version(NEW, ["existing.example", "new.example"]),
    }
    current = detail(["existing.example", "new.example"])
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.intigriti.com"
        assert request.headers["authorization"] == "Bearer test-secret"
        path = request.url.path
        if path.endswith("/activities"):
            return httpx.Response(200, json={"records": records, "maxCount": len(records)})
        if "/domains/" in path:
            return httpx.Response(200, json={"domains": versions[path.rsplit("/", 1)[-1]]})
        return httpx.Response(200, json=current)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = Adapter("intigriti", settings, client)
        await poll_domains(adapter, db, settings)
        store = DomainStore(db)
        assert [r["domain"] for r in store.listings()["items"]] == ["new.example", "existing.example"]
        assert store.listings()["items"][-1]["baseline"]
        assert db.notification_status()["counts"]["pending"] == 0  # Existing history is silent.
        versions[LATER] = version(LATER, ["existing.example", "new.example", "fresh.example"])
        current["domains"] = versions[LATER]
        records.insert(0, change(int(time.time()) + 1, LATER, NEW))
        await poll_domains(adapter, db, settings)
        await poll_domains(adapter, db, settings)
        assert [r["domain"] for r in store.listings()["items"]] == [
            "fresh.example",
            "new.example",
            "existing.example",
        ]
        assert db.notification_status()["counts"]["pending"] == 1
        row = db.claim("test")
        message = build_message(json.loads(row["payload"]))
        assert message["embeds"][0]["title"] == "fresh.example"
        assert message["embeds"][0]["url"] == URL
        assert "program_activity" not in json.dumps(message)
        assert message["allowed_mentions"]["parse"] == []
        assert not any(r.url.host.endswith("example") for r in requests)


def test_removed_or_currently_out_of_scope_domains_are_hidden(db, settings):
    store = DomainStore(db)
    store.ingest([change()])
    job = store.pending(1)[0]
    store.finish(job, {"a.example", "gone.example"}, {"a.example": ["a.example"]}, URL, "open", cfg(settings))
    assert [r["domain"] for r in store.listings()["items"]] == ["a.example"]
    follow = change(int(time.time()) + 1, LATER, NEW)
    store.ingest([follow])
    store.finish(store.pending(1)[0], set(), {}, URL, "not_open", cfg(settings))
    assert store.listings()["total"] == 0
    assert db.notification_status()["counts"]["pending"] == 0


def test_same_domain_new_path_and_reward_changes_create_no_addition():
    old = scope_domains(version(OLD, ["https://a.example/old"]))
    new = scope_domains(version(NEW, ["https://a.example/new"]))
    assert set(new) - set(old) == set()


def test_same_scope_versions_do_not_duplicate_when_activity_timestamp_changes(db):
    store = DomainStore(db)
    store.ingest([change(100), change(101)])
    assert len(store.pending(10)) == 1


def test_same_domain_in_two_programs_keeps_separate_scope_references(db, settings):
    store = DomainStore(db)
    second = change()
    second["programId"] = LATER
    store.ingest([change(), second])
    for job in store.pending(10):
        store.finish(job, set(), {"shared.example": ["shared.example"]}, URL, "open", cfg(settings))
    assert store.listings()["total"] == 2


async def test_alert_for_domain_removed_before_delivery_is_suppressed(db, settings):
    store = DomainStore(db)
    store.ingest([])
    store.ingest([change(int(time.time()) + 1)])
    settings = cfg(settings)
    store.finish(
        store.pending(1)[0], {"new.example"}, {"new.example": ["new.example"]}, URL, "open", settings
    )
    assert db.notification_status()["counts"]["pending"] == 1
    store.unavailable(P)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("Removed scope must not alert"))
    ) as client:
        assert await Dispatcher(db, settings, client).once()
    assert db.notification_status()["counts"]["sent"] == 0


def test_source_version_commit_is_atomic(db, settings, monkeypatch):
    store = DomainStore(db)
    store.ingest([])
    store.ingest([change(int(time.time()) + 1)])
    job = store.pending(1)[0]
    monkeypatch.setattr(db, "_queue", Mock(side_effect=RuntimeError("queue failure")))
    with pytest.raises(RuntimeError):
        store.finish(job, {"a.example"}, {"a.example": ["a.example"]}, URL, "open", cfg(settings))
    assert store.listings()["total"] == 0
    assert store.pending(1)[0]["id"] == job["id"]


async def test_delayed_alert_uses_latest_scope_constraints(db, settings):
    store = DomainStore(db)
    store.ingest([])
    store.ingest([change(int(time.time()) + 1)])
    settings = cfg(settings)
    store.finish(
        store.pending(1)[0], {"new.example"}, {"new.example": ["new.example"]}, URL, "open", settings
    )
    with db.connection(True) as conn:
        conn.execute(
            "UPDATE scope_programs SET domains_json=? WHERE program_id=?",
            (json.dumps({"new.example": ["https://new.example:8443/allowed"]}), P),
        )
    messages = []

    def handler(request):
        messages.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "accepted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Dispatcher(db, settings, client).once()
    assert "https://new.example:8443/allowed" in messages[0]["embeds"][0]["description"]
    assert db.notification_status()["counts"]["sent"] == 1


def test_domain_notification_preserves_port_only_constraint():
    message = build_message(
        {
            "kind": "domain_added",
            "domain": "a.example",
            "endpoints": ["https://a.example:8443"],
            "scope_url": URL,
            "occurred_at": "2026-11-14T00:00:00Z",
        }
    )
    assert "https://a.example:8443" in message["embeds"][0]["description"]


async def test_generic_notifications_and_connection_tests_are_suppressed(db, settings, event):
    settings = cfg(settings)
    settings.notify_history = True
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    db.queue_test()
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requests.append(r))) as client:
        dispatcher = Dispatcher(db, settings, client)
        assert await dispatcher.once()
        assert await dispatcher.once()
    assert not requests
    with db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE state='suppressed'").fetchone()[0] == 2
    settings.intigriti_enabled = False
    service = MonitoringService(db, settings, None)
    await run_cycle(service, CaptureState(), send_test=True)
    assert db.notification_status()["counts"]["pending"] == 0
    assert "Intigriti testing domains" in dashboard(service, "completed")
    assert "Recent records" not in dashboard(service, "completed")


async def test_individual_scope_permission_failure_does_not_pause_whole_provider(db, settings):
    settings = cfg(settings)
    db.ensure_collection("intigriti", "domains")

    def handler(request):
        if request.url.path.endswith("/activities"):
            return httpx.Response(200, json={"maxCount": 1, "records": [change()]})
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await poll_domains(Adapter("intigriti", settings, client), db, settings)
    assert DomainStore(db).stats()["failed_changes"] == 1
    assert db.provider_state("intigriti")["cooldown_until"] == 0


@pytest.mark.parametrize(
    "domain,endpoints,url",
    [
        ("@everyone", ["@everyone"], URL),
        ("a.example", ["b.example"], URL),
        ("a.example", ["a.example"], "https://evil.example"),
    ],
)
def test_domain_notification_rejects_unsafe_payloads(domain, endpoints, url):
    with pytest.raises(ValueError):
        build_message(
            {
                "kind": "domain_added",
                "domain": domain,
                "endpoints": endpoints,
                "scope_url": url,
                "occurred_at": "2026-11-14T00:00:00Z",
            }
        )
