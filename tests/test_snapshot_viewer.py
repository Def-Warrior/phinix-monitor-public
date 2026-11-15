import json

import httpx

from app.config import Settings
from app.discord import build_message
from app.github_runner import run_cycle
from app.providers import Adapter
from app.service import MonitoringService
from app.storage import Database
from tests.test_github_runner import CaptureState
from tests.test_intigriti import activity, configured, program


def test_activity_links_resolve_to_program_webpage_without_changing_source(db, settings):
    adapter = Adapter("intigriti", configured(settings), None)
    for collection, record in [("programs", program()), ("activities", activity())]:
        db.ensure_collection("intigriti", collection)
        db.commit_page("intigriti", collection, [adapter.normalize(collection, record)], None, settings)
    row = next(e for e in db.events()["items"] if e["collection"] == "activities")
    assert row["url"].startswith("https://api.intigriti.com/")
    assert row["browser_url"] == program()["webLinks"]["detail"]
    assert row["revision"] == 1
    assert db.notification_status()["counts"]["pending"] == 0


def test_missing_program_and_bugcrowd_use_browser_dashboards(db, settings):
    adapter = Adapter("intigriti", configured(settings), None)
    event = adapter.normalize("activities", activity())
    db.ensure_collection("intigriti", "activities")
    db.commit_page("intigriti", "activities", [event], None, settings)
    assert db.events()["items"][0]["browser_url"] == "https://app.intigriti.com/researcher/dashboard"
    assert (
        build_message({"kind": "new", "event": event.model_dump(mode="json")}, True)["embeds"][0]["url"]
        == "https://app.intigriti.com/researcher/dashboard"
    )
    payload = {
        "provider": "bugcrowd",
        "collection": "submissions",
        "external_id": "1",
        "title": "Issue",
        "url": "https://hacker-api.bugcrowd.com/v1/submissions/1",
    }
    with db.connection() as conn:
        assert Database.browser_link(conn, payload)[0] == "https://bugcrowd.com/dashboard"
    assert (
        build_message({"kind": "new", "event": payload}, True)["embeds"][0]["url"]
        == "https://bugcrowd.com/dashboard"
    )


async def test_cloud_status_persisted_after_delivery_and_viewer_never_polls(db, settings):
    cloud = configured(settings)
    # The live adapter isn't needed for this delivery/state test.
    cloud.intigriti_collections = "activities"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(200, json={"maxCount": 1, "records": [activity()]})
                if r.method == "GET"
                else httpx.Response(200)
            )
        )
    ) as client:
        await run_cycle(MonitoringService(db, cloud, client), CaptureState(), send_test=True)
    saved = db.cloud_status()
    assert saved["notifications"]["counts"]["sent"] == 1
    assert "test-token" not in json.dumps(saved) and "webhooks/" not in json.dumps(saved)
    cfg = Settings(**{**cloud.model_dump(), "snapshot_mode": True})
    viewer = MonitoringService(db, cfg, None)
    await viewer.start()
    assert not viewer.adapters and not viewer.tasks and not cfg.delivery_enabled
    status = viewer.status()
    assert status["mode"] == "github_snapshot"
    assert status["integrations"][-1]["enabled"]
    assert status["integrations"][-1]["status"] == "healthy"
    assert status["discord"]["enabled"] and not status["local_delivery_enabled"]
    assert not status["snapshot_stale"]


def test_snapshot_refresh_keeps_working_copy_on_failure(db, settings, tmp_path):
    source = MonitoringService(db, settings, None)
    db.save_cloud_status(source.status())
    manifest = tmp_path / "viewer.json"
    cfg = Settings(**{**settings.model_dump(), "snapshot_mode": True, "snapshot_manifest_path": manifest})
    viewer = MonitoringService(db, cfg, None)
    newer = tmp_path / "newer.db"
    db.backup(newer)
    manifest.write_text(json.dumps({"database": str(newer)}))
    viewer.refresh_snapshot()
    assert viewer.db.path == newer and not viewer.snapshot_refresh_error
    manifest.write_text("broken")
    viewer.refresh_snapshot()
    assert viewer.db.path == newer and viewer.status()["snapshot_refresh_error"]
    manifest.write_text(json.dumps({"database": str(tmp_path.parent / "outside.db")}))
    viewer.refresh_snapshot()
    assert viewer.db.path == newer and viewer.snapshot_refresh_error


def test_stale_snapshot_is_visible(db, settings):
    status = MonitoringService(db, settings, None).status()
    status["updated_at"] = "2000-01-01T00:00:00+00:00"
    with db.connection(True) as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES ('cloud_status',?)", (json.dumps(status),))
    cfg = Settings(**{**settings.model_dump(), "snapshot_mode": True})
    assert MonitoringService(db, cfg, None).status()["snapshot_stale"]
