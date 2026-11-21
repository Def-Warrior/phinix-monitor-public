from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from app.domains import DomainStore
from app.main import create_app
from app.service import MonitoringService
from app.supabase_viewer import SupabaseViewer


async def test_edge_feed_revision_heartbeat_and_fail_closed(settings, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://abcdefghijklmnopqrst.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test_secret")
    now = datetime.now(timezone.utc).isoformat()
    runtime = {
        "enabled": True,
        "configured": True,
        "feed_revision": "revision-one",
        "last_poll": now,
        "status": "healthy",
    }
    feed = {
        "schema": 2,
        "updated_at": now,
        "stats": {"active_domains": 1, "sent_alerts": 1, "pending_alerts": 0},
        "notifications": [],
        "items": [
            {
                "id": "addition-1",
                "program_id": "program-1",
                "domain": "example.com",
                "endpoints": ["https://example.com"],
                "scope_url": "https://app.intigriti.com/researcher/dashboard",
                "added_at": now,
                "checked_at": now,
            }
        ],
    }
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if "phinix_runtime" in request.url.path:
            return httpx.Response(200, json=[runtime])
        assert "phinix_feed" in request.url.path
        return httpx.Response(200, json=[{"payload": feed}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        viewer = SupabaseViewer(client)
        try:
            db = await viewer.refresh()
            assert DomainStore(db).listings()["items"][0]["domain"] == "example.com"
            settings.snapshot_mode = True
            status = MonitoringService(db, settings, client).status()
            assert status["mode"] == "supabase_edge" and status["poll_interval_seconds"] == 6
            assert not status["snapshot_stale"]
            runtime["status"] = "rate_limited"
            viewer.next_check = 0
            assert await viewer.refresh() is db
            assert db.cloud_status()["integrations"][0]["status"] == "rate_limited"
            assert sum("phinix_feed" in r for r in requests) == 1
            runtime["feed_revision"] = "revision-two"
            feed["schema"] = 999
            viewer.next_check = 0
            assert await viewer.refresh() is db
            assert viewer.failed and viewer.state.revision == "revision-one"
            runtime["enabled"] = False
            feed["schema"] = 2
            viewer.next_check = 0
            updated = await viewer.refresh()
            assert updated.cloud_status()["integrations"][0]["status"] == "paused"
            restarted = SupabaseViewer(client)
            try:
                paused_db = await restarted.refresh()
                assert DomainStore(paused_db).listings()["total"] == 1
                assert paused_db.cloud_status()["integrations"][0]["status"] == "paused"
            finally:
                restarted.close()
            assert not any("checkpoint" in r for r in requests)
        finally:
            viewer.close()


def test_health_updates_without_domain_changes(settings, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://abcdefghijklmnopqrst.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test_secret")
    now = datetime.now(timezone.utc).isoformat()
    runtime = {
        "enabled": True,
        "configured": True,
        "last_poll": now,
        "status": "healthy",
        "feed_revision": "unchanged-revision",
    }
    feed = {
        "schema": 2,
        "updated_at": now,
        "items": [],
        "notifications": [],
        "stats": {"active_domains": 0, "sent_alerts": 31, "pending_alerts": 0, "failed_alerts": 7},
    }

    def handle(request):
        if "phinix_runtime" in request.url.path:
            return httpx.Response(200, json=[runtime])
        return httpx.Response(200, json=[{"payload": feed}])

    settings.snapshot_mode = True
    settings.supabase_snapshot = True
    settings.discord_channel_name = "not-know"
    app = create_app(settings, transport=httpx.MockTransport(handle))
    with TestClient(app) as client:
        client.post("/api/login", json={"token": settings.admin_token.get_secret_value()})
        first = client.get("/api/updates").json()
        assert first["monitor_status"] == "healthy"
        runtime["status"] = "rate_limited"
        app.state.remote.next_check = 0
        second = client.get("/api/updates").json()
        assert first["revision"] == second["revision"]
        assert second["monitor_status"] == "rate_limited"
        assert client.get("/api/notifications/status").json()["counts"]["failed"] == 7
        runtime["status"] = "healthy"
        runtime["last_sent_at"] = now
        runtime["last_source_delivery_ms"] = 8000
        runtime["last_api_seen_delivery_ms"] = 1500
        feed["stats"]["restricted_programs"] = 1
        runtime["feed_revision"] = "timing-and-restriction"
        app.state.remote.next_check = 0
        status = client.get("/api/status").json()
        assert "1 program(s) unavailable" in status["integrations"][0]["reason"]
        assert status["delivery_timing"]["source_to_discord_ms"] == 8000
        assert status["delivery_timing"]["api_seen_to_discord_ms"] == 1500
        assert status["discord"]["channel_label"] == "not-know"
