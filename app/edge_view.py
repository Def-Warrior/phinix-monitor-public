"""Render's private read-only cache of the Supabase Edge monitor feed."""

import json
from datetime import datetime

from app.storage import Database


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0


class EdgeDatabase(Database):
    def __init__(self, path, feed, runtime):
        super().__init__(path)
        if feed.get("schema") != 2 or not isinstance(feed.get("items"), list):
            raise ValueError("invalid_edge_feed")
        self.feed, self.runtime = feed, runtime
        self.initialize()
        with self.connection(True) as conn:
            for item in feed["items"]:
                conn.execute(
                    "INSERT INTO domain_additions VALUES(?,?,?,?,?,?,?,?,1)",
                    (
                        item["id"],
                        item["id"],
                        item["program_id"],
                        item["domain"],
                        json.dumps(item["endpoints"]),
                        item["scope_url"],
                        epoch(item["added_at"]),
                        feed["updated_at"],
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO scope_programs VALUES(?,?,?,?)",
                    (item["program_id"], epoch(item["checked_at"]), "open", "{}"),
                )

    def notification_status(self):
        stats = self.feed["stats"]
        recent = self.feed.get("notifications", [])
        return {
            "counts": {
                "sent": stats["sent_alerts"],
                "pending": stats["pending_alerts"],
                "sending": 0,
                "failed": stats.get("failed_alerts", sum(x["state"] == "failed" for x in recent)),
            },
            "recent": recent,
        }

    def cloud_status(self):
        runtime = self.runtime
        status = runtime["status"] if runtime["enabled"] else "paused"
        updated = runtime["last_poll"] or self.feed["updated_at"]
        return {
            "mode": "supabase_edge",
            "focus": "intigriti_domains",
            "updated_at": updated,
            "poll_interval_seconds": 6,
            "domain_monitoring": self.feed["stats"],
            "integrations": [
                {
                    "name": "intigriti",
                    "enabled": runtime["enabled"],
                    "status": status,
                    "reason": (
                        f"{self.feed['stats']['restricted_programs']} program(s) unavailable to your Intigriti account; access is retried hourly."
                        if status == "healthy" and self.feed["stats"].get("restricted_programs")
                        else None
                    ),
                    "cooldown_until": runtime.get("provider_cooldown", 0),
                }
            ],
            "discord": {"enabled": runtime["configured"], "channel_label": "phinix", "include_details": True},
            "notifications": self.notification_status(),
            "delivery_timing": {
                "last_sent_at": runtime.get("last_sent_at"),
                "source_to_discord_ms": runtime.get("last_source_delivery_ms"),
                "api_seen_to_discord_ms": runtime.get("last_api_seen_delivery_ms"),
            },
            "collections": [
                {
                    "provider": "intigriti",
                    "name": "domains",
                    "baseline_done": True,
                    "pages": 1,
                    "last_success": updated,
                    "status": status,
                    "error_code": runtime.get("last_error"),
                }
            ],
        }
