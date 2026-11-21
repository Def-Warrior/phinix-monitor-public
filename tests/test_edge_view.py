from datetime import datetime, timezone

import httpx

from app.domains import DomainStore
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
            assert not any("checkpoint" in r for r in requests)
        finally:
            viewer.close()
