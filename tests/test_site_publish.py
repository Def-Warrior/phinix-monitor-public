import json

import httpx
import pytest

from app.domains import DomainStore
from app.site_publish import SitePublishError, publish_site, site_snapshot


def seed(db):
    db.save_cloud_status(
        {
            "focus": "intigriti_domains",
            "integrations": [{"name": "intigriti", "status": "healthy"}],
            "private_extra": "must-not-be-published",
        }
    )
    return DomainStore(db)


async def test_site_publishing_is_optional(db, monkeypatch):
    monkeypatch.delenv("PHINIX_SITE_URL", raising=False)
    monkeypatch.delenv("PHINIX_SITE_TOKEN", raising=False)
    assert not await publish_site(db, None)


async def test_private_snapshot_upload_is_minimal_and_authenticated(db, monkeypatch):
    seed(db)
    monkeypatch.setenv("PHINIX_SITE_URL", "https://example.chatgpt.site")
    monkeypatch.setenv("PHINIX_SITE_TOKEN", "service-secret")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://example.chatgpt.site/api/ingest"
        assert request.headers["oai-sites-authorization"] == "Bearer service-secret"
        payload = json.loads(request.content)
        assert "must-not-be-published" not in request.content.decode()
        assert payload["stats"]["active_domains"] == 0
        return httpx.Response(200, json={"updated_at": payload["updated_at"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await publish_site(db, client)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.chatgpt.site",
        "https://evil.test",
        "https://a.chatgpt.site.evil.test",
        "https://a.chatgpt.site/path",
        "https://user@a.chatgpt.site",
    ],
)
async def test_site_token_never_sent_to_unapproved_origin(db, monkeypatch, origin):
    monkeypatch.setenv("PHINIX_SITE_URL", origin)
    monkeypatch.setenv("PHINIX_SITE_TOKEN", "service-secret")
    with pytest.raises(SitePublishError):
        await publish_site(db, None)


async def test_site_upload_does_not_follow_redirects(db, monkeypatch):
    seed(db)
    monkeypatch.setenv("PHINIX_SITE_URL", "https://example.chatgpt.site")
    monkeypatch.setenv("PHINIX_SITE_TOKEN", "service-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SitePublishError):
            await publish_site(db, client)
    assert len(requests) == 1


def test_site_snapshot_requires_domain_monitor(db):
    with pytest.raises(SitePublishError):
        site_snapshot(db)
