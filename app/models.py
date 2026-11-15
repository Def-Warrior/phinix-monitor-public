import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["hackerone", "bugcrowd", "intigriti"]
    collection: Literal["hacktivity", "reports", "engagements", "submissions", "programs", "activities"]
    external_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(max_length=2048)
    program: str = Field(default="unknown", max_length=250)
    category: str = Field(default="report", max_length=100)
    severity: str = Field(default="unknown", max_length=50)
    state: str = Field(default="unknown", max_length=100)
    needs_action: bool | None = None
    occurred_at: datetime | None = None
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("external_id")
    @classmethod
    def stable_id(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("Invalid stable ID")
        return value

    @field_validator("occurred_at")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Timestamp must include timezone")
            return value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def source(self):
        u = urlsplit(self.url)
        hosts = {
            "hackerone": {"hackerone.com"},
            "bugcrowd": {"bugcrowd.com", "hacker-api.bugcrowd.com"},
            "intigriti": {"intigriti.com", "app.intigriti.com", "api.intigriti.com"},
        }
        collections = {
            "hackerone": {"hacktivity", "reports"},
            "bugcrowd": {"engagements", "submissions"},
            "intigriti": {"programs", "activities"},
        }
        if (
            u.scheme != "https"
            or u.hostname not in hosts[self.provider]
            or u.username
            or u.password
            or u.port not in (None, 443)
            or u.fragment
            or u.query
            or "\\" in self.url
            or any(ord(c) < 33 for c in self.url)
            or self.collection not in collections[self.provider]
        ):
            raise ValueError("Invalid source URL or collection")
        return self

    def payload(self) -> str:
        # Preserve existing H1/Bugcrowd fingerprints when upgrading old state.
        excluded = {"source_fingerprint"} if self.source_fingerprint is None else set()
        return json.dumps(
            self.model_dump(mode="json", exclude=excluded), sort_keys=True, separators=(",", ":")
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(self.payload().encode()).hexdigest()


def matches_rules(event: Event, settings) -> bool:
    for name, value in [
        ("providers", event.provider),
        ("programs", event.program),
        ("categories", event.category),
        ("severities", event.severity),
    ]:
        choices = {x.strip().casefold() for x in getattr(settings, f"notify_{name}").split(",") if x.strip()}
        if choices and value.casefold() not in choices:
            return False
    keywords = [x.strip().casefold() for x in settings.notify_keywords.split(",") if x.strip()]
    return not keywords or any(
        x in f"{event.title} {event.program} {event.category}".casefold() for x in keywords
    )
