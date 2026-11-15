import asyncio
import json
import re
import uuid
from urllib.parse import urlsplit

import httpx

from app.models import Event
from app.providers import backoff, retry_after


def safe_text(value, limit):
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    value = value.replace("@", "@\u200b")
    return re.sub(r"([\\*_~`|<>\[\]()])", r"\\\1", value)[:limit]


def build_message(payload, include_details=False):
    kind = payload["kind"]
    if kind == "test":
        embed = {
            "title": "Phinix connection verified",
            "description": "Your monitoring service can deliver alerts to this channel.",
            "color": 5763719,
        }
    elif kind == "operational":
        provider = payload["provider"]
        if provider not in {"hackerone", "bugcrowd", "intigriti"} or payload["code"] != "auth_error":
            raise ValueError("Invalid operational event")
        embed = {
            "title": "Provider authentication needs attention",
            "description": f"{provider}: check the configured read-only credentials and scopes. Polling is paused until the cooldown ends.",
            "color": 16753920,
        }
    elif kind == "domain_added":
        from app.domains import domain_name

        name = payload["domain"]
        endpoints = payload["endpoints"]
        if domain_name(name) != name or not isinstance(endpoints, list) or not endpoints:
            raise ValueError("Invalid domain notification")
        if any(domain_name(endpoint) != name for endpoint in endpoints):
            raise ValueError("Mismatched domain endpoint")
        reference = Event(
            provider="intigriti",
            collection="programs",
            external_id="scope",
            title="Scope",
            url=payload["scope_url"],
            occurred_at=payload["occurred_at"],
        )
        if urlsplit(reference.url).hostname not in {"app.intigriti.com", "intigriti.com"}:
            raise ValueError("Invalid scope link")
        constrained = [
            e
            for e in endpoints
            if urlsplit(e if "://" in e else "//" + e).path not in {"", "/"}
            or urlsplit(e if "://" in e else "//" + e).query
            or urlsplit(e if "://" in e else "//" + e).port is not None
        ]
        description = "New in-scope domain."
        if constrained:
            description += "\nListed scope: " + ", ".join(safe_text(e, 1800) for e in constrained)
            description = description[:3500] + ("… See linked scope." if len(description) > 3500 else "")
        embed = {
            "title": safe_text(name, 256),
            "description": description,
            "url": reference.url,
            "color": 5763719,
            "timestamp": reference.occurred_at.isoformat(),
            "footer": {"text": "Phinix · Intigriti domain added"},
        }
    elif kind in {"new", "changed"}:
        event = Event.model_validate(payload["event"])
        embed = {
            "title": f"{event.provider.title()} · {kind} {event.category}",
            "description": "An authorized record changed. Open the protected Phinix dashboard to review it.",
            "color": {"critical": 15548997, "high": 16753920, "medium": 16705372}.get(
                event.severity, 5793266
            ),
            "footer": {"text": "Phinix · bug bounty intelligence"},
        }
        if include_details:
            browser_url = event.url
            if urlsplit(browser_url).hostname == "api.intigriti.com":
                browser_url = "https://app.intigriti.com/researcher/dashboard"
            elif urlsplit(browser_url).hostname == "hacker-api.bugcrowd.com":
                browser_url = "https://bugcrowd.com/dashboard"
            embed.update(
                title=safe_text(event.title, 240),
                url=browser_url,
                description=safe_text(f"{kind.title()} · {event.program} · {event.state}", 1000),
                fields=[
                    {"name": "Severity", "value": safe_text(event.severity, 50), "inline": True},
                    {"name": "Source", "value": event.provider, "inline": True},
                ],
            )
        if event.occurred_at:
            embed["timestamp"] = event.occurred_at.isoformat()
    else:
        raise ValueError("Unknown notification type")
    return {
        "username": "Phinix",
        "allowed_mentions": {"parse": [], "users": [], "roles": [], "replied_user": False},
        "embeds": [embed],
    }


class Dispatcher:
    def __init__(self, db, settings, client):
        self.db, self.settings, self.client = db, settings, client
        self.owner = uuid.uuid4().hex

    async def once(self):
        if not self.settings.delivery_enabled:
            return False
        row = self.db.claim(self.owner, lease_seconds=120)
        if row is None:
            return False
        try:
            data = json.loads(row["payload"])
            suppress = self.settings.intigriti_domains_only and data.get("kind") not in {
                "domain_added",
                "operational",
            }
            if self.settings.intigriti_domains_only and data.get("kind") == "domain_added":
                with self.db.connection() as db:
                    current = db.execute(
                        "SELECT domains_json FROM scope_programs WHERE program_id=? AND status='open'",
                        (data.get("program_id"),),
                    ).fetchone()
                current_domains = json.loads(current[0]) if current else {}
                suppress = data.get("domain") not in current_domains
                if not suppress:
                    data["endpoints"] = current_domains[data["domain"]]
            if suppress:
                with self.db.connection(True) as db:
                    db.execute(
                        "UPDATE outbox SET state='suppressed',owner=NULL,lease_until=NULL,error_code='domain_only_mode' "
                        "WHERE id=? AND owner=? AND state='sending'",
                        (row["id"], self.owner),
                    )
                return True
            payload = build_message(data, self.settings.discord_include_details)
        except (ValueError, KeyError, TypeError):
            self.db.delivery_failed(
                row, self.owner, "invalid_payload", 0, self.settings.max_retries, permanent=True
            )
            return True
        try:
            # No GET or status response ever contains the secret webhook URL.
            response = await self.client.post(
                self.settings.discord_webhook_url.get_secret_value(),
                params={"wait": "true"},
                json=payload,
                follow_redirects=False,
            )
        except httpx.TransportError:
            self.db.delivery_failed(
                row, self.owner, "network_error", backoff(row["failures"]), self.settings.max_retries
            )
            return True
        if 200 <= response.status_code < 300:
            self.db.delivered(row["id"], self.owner)
        elif response.status_code == 429:
            self.db.delivery_failed(
                row,
                self.owner,
                "rate_limited",
                retry_after(response),
                self.settings.max_retries,
                rate_limited=True,
            )
        else:
            transient = response.status_code == 408 or response.status_code >= 500
            self.db.delivery_failed(
                row,
                self.owner,
                "upstream_unavailable" if transient else "webhook_rejected",
                backoff(row["failures"]),
                self.settings.max_retries,
                permanent=not transient,
            )
        return True

    async def run(self):
        try:
            while True:
                worked = await self.once()
                await asyncio.sleep(0.5 if worked else 2)
        finally:
            self.db.release_owner(self.owner)
