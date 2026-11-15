import base64
import gzip
import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cloud_errors import CloudStateError
from app.config import Settings
from app.main import create_app
from app.service import MonitoringService
from app.supabase_state import SupabaseState
from app.supabase_viewer import SupabaseViewer

URL = "https://abcdefghijklmnopqrst.supabase.co"
KEY = "sb_secret_test_not_real"


def checkpoint(db, settings, tmp_path):
    settings.intigriti_domains_only = True
    db.save_cloud_status(MonitoringService(db, settings, None).status())
    copy = tmp_path / "source.db"
    db.backup(copy)
    raw = copy.read_bytes()
    return {
        "revision": str(uuid4()),
        "body": base64.b64encode(gzip.compress(raw)).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_count": len(raw),
    }


async def test_roundtrip_conflict_and_reload(db, settings, tmp_path):
    row = checkpoint(db, settings, tmp_path)
    calls = []

    def handle(request):
        nonlocal row
        assert request.headers["apikey"] == KEY
        assert "authorization" not in request.headers
        if request.method == "GET":
            return httpx.Response(200, json=[row])
        value = json.loads(request.content)
        calls.append(value)
        if value["expected_revision"] != row["revision"]:
            return httpx.Response(409)
        row = {
            "revision": value["next_revision"],
            "body": value["snapshot_body"],
            "sha256": value["snapshot_sha256"],
            "byte_count": value["snapshot_bytes"],
        }
        return httpx.Response(200, json=row["revision"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        one, two = SupabaseState(client, URL, KEY), SupabaseState(client, URL, KEY)
        assert await one.load(tmp_path / "one.db")
        await two.load(tmp_path / "two.db")
        assert not await one.load(tmp_path / "unchanged.db")
        await one.save(db)
        with pytest.raises(CloudStateError, match="conflict"):
            await two.save(db)
        assert calls[0]["domain_feed"]["schema"] == 1
        assert KEY not in json.dumps(calls)
        assert await two.load(tmp_path / "new.db")


@pytest.mark.parametrize("damage", ["digest", "size", "encoding", "gzip", "revision", "database"])
async def test_invalid_remote_snapshot_fails_closed(db, settings, tmp_path, damage):
    row = checkpoint(db, settings, tmp_path)
    if damage == "digest":
        row["sha256"] = "0" * 64
    elif damage == "size":
        row["byte_count"] = 26_000_000
    elif damage == "encoding":
        row["body"] = "not!base64"
    elif damage == "gzip":
        row["body"] = base64.b64encode(b"invalid gzip").decode()
    elif damage == "revision":
        row["revision"] = "invalid"
    else:
        raw = b"SQLite format 3\0" + b"bad" * 1024
        row.update(
            body=base64.b64encode(gzip.compress(raw)).decode(),
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_count=len(raw),
        )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[row]))
    ) as client:
        state = SupabaseState(client, URL, KEY)
        with pytest.raises(CloudStateError, match="invalid_supabase_state"):
            await state.load(tmp_path / "corrupt.db")
        assert not state.loaded and state.revision is None


async def test_missing_state_requires_explicit_initialization(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    ) as client:
        state = SupabaseState(client, URL, KEY)
        with pytest.raises(CloudStateError, match="manual_initialization"):
            await state.load(tmp_path / "missing.db")
        assert not state.loaded
        assert not await state.load(tmp_path / "first.db", initialize=True)
        assert state.loaded and not (tmp_path / "first.db").exists()


async def test_viewer_retains_last_good_snapshot(db, settings, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", KEY)
    row = checkpoint(db, settings, tmp_path)
    failed = False
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(503) if failed else httpx.Response(200, json=[row])
        )
    ) as client:
        viewer = SupabaseViewer(client)
        try:
            first = await viewer.refresh()
            failed = True
            viewer.next_check = 0
            assert await viewer.refresh() is first
            assert viewer.failed
            failed = False
            viewer.next_check = 0
            assert await viewer.refresh() is first
            assert not viewer.failed
        finally:
            viewer.close()


def test_web_signin_required_and_no_background_workers(db, settings, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", KEY)
    row = checkpoint(db, settings, tmp_path)
    cfg = Settings(**{**settings.model_dump(), "snapshot_mode": True, "supabase_snapshot": True})
    app = create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[row])))
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/domains").status_code == 401
        assert client.get("/api/status").status_code == 401
        assert (
            client.post(
                "/api/login",
                json={"token": cfg.admin_token.get_secret_value()},
                headers={"Origin": "https://foreign.example"},
            ).status_code
            == 403
        )
        assert (
            client.post("/api/login", json={"token": cfg.admin_token.get_secret_value()}).status_code == 200
        )
        assert client.get("/api/domains").status_code == 200
        assert not app.state.service.tasks and not app.state.service.adapters
        assert not cfg.delivery_enabled
        assert client.post("/api/notifications/test").status_code == 403
        assert KEY not in client.get("/").text


@pytest.mark.parametrize(
    "url", ["http://example.com", URL + "/path", URL + "@evil.example", "https://evil.example"]
)
def test_reject_non_supabase_destination(url):
    with pytest.raises(CloudStateError, match="configuration"):
        SupabaseState(None, url, KEY)
