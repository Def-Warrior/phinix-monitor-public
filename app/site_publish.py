"""Publish a minimal domain snapshot to the owner's private hosted dashboard."""

import json
import os
from urllib.parse import urlsplit

import httpx

from app.domains import DomainStore


class SitePublishError(Exception):
    pass


def site_snapshot(db):
    status = db.cloud_status()
    if not status or status.get("focus") != "intigriti_domains":
        raise SitePublishError("domain_snapshot_unavailable")
    store = DomainStore(db)
    data = store.listings(limit=10000)
    if data["total"] != len(data["items"]):
        raise SitePublishError("site_snapshot_size_limit")
    provider = next(p for p in status["integrations"] if p["name"] == "intigriti")
    fields = ("id", "domain", "endpoints", "scope_url", "added_at", "checked_at", "baseline")
    return {
        "schema": 1,
        "updated_at": status["updated_at"],
        "status": provider["status"],
        "stats": store.stats(),
        "items": [{k: item[k] for k in fields} for item in data["items"]],
    }


async def publish_site(db, client):
    origin = os.environ.get("PHINIX_SITE_URL", "").rstrip("/")
    token = os.environ.get("PHINIX_SITE_TOKEN", "")
    if not origin and not token:
        return False
    url = urlsplit(origin)
    if (
        not token
        or url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".chatgpt.site")
        or url.username
        or url.password
        or url.port
        or url.path
        or url.query
        or url.fragment
    ):
        raise SitePublishError("invalid_site_configuration")
    payload = json.dumps(site_snapshot(db), separators=(",", ":")).encode()
    if len(payload) > 2_000_000:
        raise SitePublishError("site_snapshot_size_limit")
    try:
        response = await client.post(
            origin + "/api/ingest",
            content=payload,
            headers={
                "OAI-Sites-Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "X-Phinix-Update": "snapshot-v1",
            },
            follow_redirects=False,
            timeout=12,
        )
        if response.status_code != 200 or not isinstance(response.json().get("updated_at"), str):
            raise SitePublishError("site_update_rejected")
    except (httpx.TransportError, ValueError, AttributeError):
        raise SitePublishError("site_update_failed") from None
    return True
