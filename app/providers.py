import asyncio
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.models import Event


class ProviderError(Exception):
    def __init__(self, code, delay=0, http_status=None):
        super().__init__(code)
        self.code, self.delay = code, delay
        self.http_status = http_status


def backoff(attempt):
    return min(60, 2 ** min(attempt, 6)) + random.uniform(0, 1)


def retry_after(response, default=60):
    values = []
    for key in ("Retry-After", "X-RateLimit-Reset-After"):
        raw = response.headers.get(key)
        if raw:
            try:
                values.append(float(raw))
            except ValueError:
                try:
                    values.append((parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("retry_after"), (int, float)):
            values.append(body["retry_after"])
    except (ValueError, TypeError):
        pass
    valid = [x for x in values if math.isfinite(x) and x >= 0]
    return max(1, max(valid)) if valid else default


def object_value(value):
    if not isinstance(value, dict):
        raise ProviderError("schema_error")
    return value


def required_text(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("schema_error")
    return value.strip()


def optional_text(obj, key, default="unknown"):
    value = obj.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ProviderError("schema_error")
    return value or default


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ProviderError("schema_error")
    return str(value)


def intigriti_program_url(value):
    url = urlsplit(value)
    if not url.query:
        return value  # Event validates the final fixed provider host.
    params = parse_qs(url.query)
    redirect = params.get("redirect", [])
    if (
        url.scheme != "https"
        or url.netloc != "app.intigriti.com"
        or url.fragment
        or set(params) != {"redirect"}
        or len(redirect) != 1
    ):
        raise ProviderError("schema_error")
    target = redirect[0]
    # The official webLinks field wraps a relative program path in a login redirect.
    if (
        not target.startswith("/programs/")
        or "?" in target
        or "#" in target
        or "\\" in target
        or any(ord(c) < 32 for c in target)
        or any(p in {".", ".."} for p in target.split("/"))
    ):
        raise ProviderError("schema_error")
    return "https://app.intigriti.com" + quote(target, safe="/%-._~")


class Adapter:
    def __init__(self, name, settings, client):
        self.name, self.settings, self.client = name, settings, client
        self.base = {
            "hackerone": "https://api.hackerone.com",
            "bugcrowd": "https://hacker-api.bugcrowd.com",
            "intigriti": "https://api.intigriti.com/external/researcher",
        }[name]
        self.collections = settings.collections(name)

    def path(self, collection):
        if collection not in self.collections:
            raise ProviderError("schema_error")
        if self.name == "hackerone":
            return "/v1/hackers/hacktivity" if collection == "hacktivity" else "/v1/hackers/me/reports"
        if self.name == "intigriti":
            return "/v1/programs/activities" if collection == "activities" else "/v1/programs"
        return f"/v1/{collection}"

    async def get_json(self, collection, cursor):
        size = self.settings.page_size
        if self.name == "hackerone":
            params = {"page[number]": cursor or 1, "page[size]": size}
            if collection == "hacktivity":
                params["sort"] = "-latest_disclosable_activity_at"
            auth = httpx.BasicAuth(
                self.settings.hackerone_username.get_secret_value(),
                self.settings.hackerone_token.get_secret_value(),
            )
            headers = {"Accept": "application/json"}
        elif self.name == "intigriti":
            if collection == "activities":
                size = min(size, self.settings.intigriti_activity_window - cursor)
                if size <= 0:
                    raise ProviderError("schema_error")
            params = {"offset": cursor, "limit": size}
            auth = None
            headers = {
                "Accept": "application/json",
                "Authorization": "Bearer " + self.settings.intigriti_token.get_secret_value(),
            }
        else:
            params = {"offset": cursor, "limit": size}
            if collection == "submissions":
                params["sort"] = "-last_activity_at"
            auth = None
            headers = {
                "Accept": "application/json",
                "Authorization": "Token " + self.settings.bugcrowd_token.get_secret_value(),
            }
        url = self.base + self.path(collection) + "?" + urlencode(params)
        return await self.request_json(url, headers, auth)

    async def intigriti_resource(self, program_id, version_id=None):
        identity = str(UUID(program_id))
        path = f"/v1/programs/{identity}"
        if version_id is not None:
            path += "/domains/" + str(UUID(version_id))
        if self.name != "intigriti":
            raise ProviderError("schema_error")
        return await self.request_json(
            self.base + path,
            {
                "Accept": "application/json",
                "Authorization": "Bearer " + self.settings.intigriti_token.get_secret_value(),
            },
            None,
        )

    async def request_json(self, url, headers, auth):
        for attempt in range(self.settings.max_retries + 1):
            try:
                # Stream with a hard decoded body limit; redirects never receive credentials.
                async with self.client.stream(
                    "GET", url, headers=headers, auth=auth, follow_redirects=False
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 4_000_000:
                            raise ProviderError("response_too_large")
                    # aiter_bytes already decoded content encodings. Do not decode them twice.
                    decoded_headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k.lower() not in {"content-encoding", "content-length"}
                    }
                    body = httpx.Response(
                        response.status_code, headers=decoded_headers, content=bytes(content)
                    )
                if body.status_code in (401, 403):
                    raise ProviderError("auth_error", self.settings.auth_cooldown_seconds, body.status_code)
                if body.status_code == 429:
                    raise ProviderError("rate_limited", retry_after(body))
                if body.status_code == 408 or body.status_code >= 500:
                    if attempt < self.settings.max_retries:
                        await asyncio.sleep(backoff(attempt))
                        continue
                    raise ProviderError("upstream_unavailable")
                if not 200 <= body.status_code < 300:
                    raise ProviderError(
                        "redirect_rejected" if 300 <= body.status_code < 400 else "request_error",
                        http_status=body.status_code,
                    )
                try:
                    return object_value(body.json())
                except ValueError:
                    raise ProviderError("schema_error") from None
            except httpx.DecodingError:
                raise ProviderError("schema_error") from None
            except httpx.TransportError:
                if attempt == self.settings.max_retries:
                    raise ProviderError("network_error") from None
                await asyncio.sleep(backoff(attempt))

    async def fetch(self, collection, cursor):
        body = await self.get_json(collection, cursor)
        try:
            data = body.get("records" if self.name == "intigriti" else "data")
            if not isinstance(data, list) or len(data) > self.settings.page_size:
                raise ProviderError("schema_error")
            events = [self.normalize(collection, object_value(item)) for item in data]
            if len({e.external_id for e in events}) != len(events):
                raise ProviderError("schema_error")
            next_cursor = self.next_cursor(collection, body, cursor, len(data))
            return events, next_cursor
        except (ValidationError, ValueError, TypeError, KeyError):
            raise ProviderError("schema_error") from None

    def next_cursor(self, collection, body, cursor, count):
        if self.name == "intigriti":
            total = body.get("maxCount")
            if type(total) is not int or total < 0 or (count and cursor + count > total):
                raise ProviderError("schema_error")
            # An empty page after deletions completes this scan; the next scan starts at zero.
            if not count:
                return None
            end = min(total, self.settings.intigriti_activity_window) if collection == "activities" else total
            return cursor + count if cursor + count < end else None
        if self.name == "bugcrowd":
            page = object_value(body.get("pagination"))
            for key in ("offset", "limit", "total_count"):
                if type(page.get(key)) is not int or page[key] < 0:
                    raise ProviderError("schema_error")
            if (
                type(page.get("has_more")) is not bool
                or page["limit"] < 1
                or page["limit"] > self.settings.page_size
                or page["offset"] != cursor
            ):
                raise ProviderError("schema_error")
            if page["has_more"]:
                if count != page["limit"]:
                    raise ProviderError("schema_error")
                return cursor + page["limit"]
            return None
        links = object_value(body.get("links", {}))
        if "next" not in links:
            return (cursor or 1) + 1 if count == self.settings.page_size else None
        if links["next"] is None:
            return None
        if not isinstance(links["next"], str):
            raise ProviderError("schema_error")
        url = urlsplit(urljoin(self.base + self.path(collection), links["next"]))
        if (
            url.scheme != "https"
            or url.netloc != "api.hackerone.com"
            or url.path != self.path(collection)
            or url.fragment
            or "\\" in links["next"]
            or any(ord(c) < 33 for c in links["next"])
        ):
            raise ProviderError("unsafe_pagination")
        page = parse_qs(url.query).get("page[number]", [])
        if len(page) != 1 or not page[0].isdigit() or int(page[0]) <= (cursor or 1):
            raise ProviderError("schema_error")
        return int(page[0])

    def normalize(self, collection, item):
        if self.name == "intigriti":
            return self.normalize_intigriti(collection, item)
        external_id = identifier(item.get("id"))
        if self.name == "hackerone":
            if item.get("type") not in {"report", "hacktivity_item"}:
                raise ProviderError("schema_error")
            attrs = object_value(item.get("attributes"))
            relationships = object_value(item.get("relationships", {}))
            program = object_value(object_value(relationships.get("program", {})).get("data", {}) or {})
            prog_attrs = object_value(program.get("attributes", {}))
            severity_data = object_value(
                object_value(relationships.get("severity", {})).get("data", {}) or {}
            )
            severity_attrs = object_value(severity_data.get("attributes", {}))
            severity = optional_text(attrs, "severity_rating", optional_text(severity_attrs, "rating"))
            timestamps = [
                attrs.get(k)
                for k in (
                    "latest_disclosable_activity_at",
                    "updated_at",
                    "last_program_activity_at",
                    "last_reporter_activity_at",
                    "created_at",
                    "submitted_at",
                )
                if attrs.get(k)
            ]
            # Validate every provided timestamp; use the latest known activity, never polling time.
            parsed = [
                datetime.fromisoformat(x.replace("Z", "+00:00")) for x in timestamps if isinstance(x, str)
            ]
            if len(parsed) != len(timestamps) or any(t.tzinfo is None for t in parsed):
                raise ProviderError("schema_error")
            return Event(
                provider=self.name,
                collection=collection,
                external_id=external_id,
                title=optional_text(attrs, "title", "Undisclosed report"),
                url=optional_text(attrs, "url", f"https://hackerone.com/reports/{external_id}"),
                program=optional_text(prog_attrs, "handle", optional_text(prog_attrs, "name")),
                category="report",
                severity=severity.lower(),
                state=optional_text(attrs, "state", optional_text(attrs, "substate")).lower(),
                occurred_at=max(parsed) if parsed else None,
            )
        if collection == "engagements":
            if item.get("type") != "engagement":
                raise ProviderError("schema_error")
            attrs = object_value(item.get("attributes"))
            return Event(
                provider=self.name,
                collection=collection,
                external_id=external_id,
                title=optional_text(attrs, "name", "Unnamed engagement"),
                url=f"{self.base}/v1/engagements/{external_id}",
                program=optional_text(attrs, "code", external_id),
                category="engagement",
                state=optional_text(attrs, "state"),
                occurred_at=attrs.get("updated_at") or attrs.get("created_at"),
            )
        required = {"id", "engagement_id", "title", "state", "severity", "needs_action", "last_activity_at"}
        if not required <= item.keys() or type(item["needs_action"]) is not bool:
            raise ProviderError("schema_error")
        severity = required_text(item, "severity")
        mapping = {
            "p1_critical": "critical",
            "p2_high": "high",
            "p2_severe": "high",
            "p3_moderate": "medium",
            "p4_low": "low",
            "p5_informational": "informational",
        }
        if severity not in mapping:
            raise ProviderError("schema_error")
        return Event(
            provider=self.name,
            collection=collection,
            external_id=external_id,
            title=required_text(item, "title"),
            url=f"{self.base}/v1/submissions/{external_id}",
            program=optional_text(item, "engagement_id"),
            category="submission",
            severity=mapping[severity],
            state=required_text(item, "state"),
            occurred_at=item.get("last_activity_at"),
            needs_action=item["needs_action"],
        )

    def normalize_intigriti(self, collection, item):
        def enum(value):
            value = object_value(value)
            if type(value.get("id")) is not int:
                raise ProviderError("schema_error")
            return required_text(value, "value")

        if type(item.get("following")) is not bool:
            raise ProviderError("schema_error")
        enum(item.get("type"))
        if collection == "programs":
            identity = str(UUID(required_text(item, "id")))
            status = enum(item.get("status"))
            enum(item.get("confidentialityLevel"))
            for key in ("minBounty", "maxBounty"):
                object_value(item.get(key))
            # Include bounty/status metadata in change detection without storing payout data.
            canonical = {k: v for k, v in item.items() if k != "following"}
            digest = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            return Event(
                provider=self.name,
                collection=collection,
                external_id=identity,
                title=required_text(item, "name"),
                url=intigriti_program_url(required_text(object_value(item.get("webLinks")), "detail")),
                program=required_text(item, "handle"),
                category="program",
                state=status,
                source_fingerprint=digest,
            )
        identity = str(UUID(required_text(item, "programId")))
        created = item.get("createdAt")
        if type(created) is not int or not 0 <= created <= 253402300799:
            raise ProviderError("schema_error")
        # The live v1 contract uses Unix seconds, verified against the official activity feed.
        occurred = datetime.fromtimestamp(created, timezone.utc)
        activity = object_value(item.get("activity"))
        # v1 supplies no activity ID. Coalesce exact source duplicates using immutable content.
        canonical = {
            "programId": identity,
            "typeId": item["type"]["id"],
            "createdAt": created,
            "activity": activity,
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        return Event(
            provider=self.name,
            collection=collection,
            external_id=digest,
            title=enum(item["type"]),
            url=f"{self.base}/v1/programs/{identity}",
            program=identity,
            category="program_activity",
            occurred_at=occurred,
        )
