"""Intigriti scope-version differences. Never contact the listed testing targets."""

import hashlib
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from app.models import Event, utcnow
from app.providers import ProviderError, intigriti_program_url, object_value, required_text


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def domain_name(endpoint):
    """Extract a domain for display, retaining the exact endpoint separately."""
    if not isinstance(endpoint, str) or not 0 < len(endpoint.strip()) <= 1800:
        return None
    endpoint = endpoint.strip()
    if any(ord(c) < 33 for c in endpoint) or "\\" in endpoint:
        return None
    try:
        parsed = urlsplit(endpoint if "://" in endpoint else "//" + endpoint)
        _ = parsed.port  # Reject malformed ports; keep valid port/path limits in the original endpoint.
        if parsed.scheme not in {"", "http", "https"} or parsed.username or parsed.password:
            return None
        host = (parsed.hostname or "").rstrip(".").lower()
        wildcard = host.startswith("*.")
        bare = host[2:] if wildcard else host
        bare = bare.encode("idna").decode("ascii")
        try:
            ipaddress.ip_address(bare)
            return None
        except ValueError:
            pass
        if (
            len(bare) > 253
            or "." not in bare
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in bare.split(".")
            )
        ):
            return None
        return ("*." if wildcard else "") + bare
    except (ValueError, UnicodeError):
        return None


def scope_domains(version, expected_id=None):
    version = object_value(version)
    identity = str(UUID(required_text(version, "id")))
    if expected_id and identity != expected_id:
        raise ProviderError("schema_error")
    content = version.get("content")
    # Null content is unavailable, not proof of an empty previous scope.
    if not isinstance(content, list) or len(content) > 10000:
        raise ProviderError("scope_content_unavailable")
    result = {}
    for raw in content:
        item = object_value(raw)
        kind = object_value(item.get("type"))
        tier = object_value(item.get("tier"))
        tier_name = required_text(tier, "value").casefold()
        if tier.get("id") == 5 or tier_name == "out of scope":
            continue
        if tier_name != "no bounty" and not re.fullmatch(r"tier [1-5]", tier_name):
            continue
        if (kind.get("id"), kind.get("value")) not in {(1, "Url"), (7, "Wildcard")}:
            continue
        endpoint = required_text(item, "endpoint")
        name = domain_name(endpoint)
        if name:
            result.setdefault(name, set()).add(endpoint)
    return {name: sorted(values) for name, values in result.items()}


def current_scope(detail, program_id):
    if str(UUID(required_text(detail, "id"))) != program_id:
        raise ProviderError("schema_error")
    state = object_value(detail.get("status"))
    opened = state.get("id") == 3 and state.get("value") == "Open"
    domains = scope_domains(detail.get("domains"))
    url = intigriti_program_url(required_text(object_value(detail.get("webLinks")), "detail"))
    Event(
        provider="intigriti", collection="programs", external_id=program_id, title="Scope reference", url=url
    )
    return domains if opened else {}, url, "open" if opened else "not_open"


class DomainStore:
    def __init__(self, db):
        self.db = db

    def ingest(self, records):
        prepared = []
        for record in records:
            kind = object_value(record.get("type"))
            if kind.get("id") not in {1, 3}:
                continue
            program = str(UUID(required_text(record, "programId")))
            created = record.get("createdAt")
            if type(created) is not int or not 0 <= created <= 253402300799:
                raise ProviderError("schema_error")
            activity = object_value(record.get("activity"))
            before = after = None
            if kind["id"] == 1:
                before = str(UUID(required_text(activity, "fromVersionId")))
                after = str(UUID(required_text(activity, "toVersionId")))
                if UUID(before).int == 0 or before == after:
                    continue
            job_kind = "scope" if kind["id"] == 1 else "status"
            identity = (
                digest(program, job_kind, before, after)
                if job_kind == "scope"
                else digest(program, job_kind, created)
            )
            prepared.append((identity, program, job_kind, before, after, created))
        with self.db.connection(True) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='domain_monitor_started'").fetchone()
            started = float(row[0]) if row else time.time()
            if not row:
                db.execute(
                    "INSERT INTO metadata(key,value) VALUES ('domain_monitor_started',?)", (str(started),)
                )
            for job in prepared:
                db.execute(
                    "INSERT OR IGNORE INTO scope_jobs(id,program_id,kind,from_version,to_version,occurred_at,notify) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (*job, int(job[-1] >= started)),
                )
            stale = db.execute(
                "SELECT program_id FROM scope_programs WHERE last_checked<? ORDER BY last_checked LIMIT 1",
                (time.time() - 3600,),
            ).fetchone()
            if stale:
                identity = digest("refresh", stale[0], int(time.time() // 3600))
                db.execute(
                    "INSERT OR IGNORE INTO scope_jobs(id,program_id,kind,occurred_at,notify) VALUES (?,?,'status',?,0)",
                    (identity, stale[0], int(time.time())),
                )

    def pending(self, limit):
        with self.db.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM scope_jobs WHERE status IN ('pending','error') AND next_attempt<=? "
                    "ORDER BY notify DESC,CASE WHEN kind='scope' THEN 0 ELSE 1 END,occurred_at DESC,id LIMIT ?",
                    (time.time(), limit),
                )
            ]

    def fail(self, job, code, delay=3600):
        with self.db.connection(True) as db:
            db.execute(
                "UPDATE scope_jobs SET status='error',error_code=?,next_attempt=? WHERE id=?",
                (code, time.time() + delay, job["id"]),
            )

    def unavailable(self, program_id):
        with self.db.connection(True) as db:
            db.execute("UPDATE domain_additions SET active=0 WHERE program_id=?", (program_id,))
            db.execute("UPDATE scope_programs SET status='unavailable' WHERE program_id=?", (program_id,))

    def finish(self, job, added, current, scope_url, program_status, settings):
        now = utcnow()
        with self.db.connection(True) as db:
            row = db.execute("SELECT status FROM scope_jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None or row[0] == "done":
                return
            db.execute(
                "INSERT INTO scope_programs VALUES (?,?,?,?) ON CONFLICT(program_id) DO UPDATE SET "
                "last_checked=excluded.last_checked,status=excluded.status,domains_json=excluded.domains_json",
                (job["program_id"], time.time(), program_status, json.dumps(current)),
            )
            for old in db.execute(
                "SELECT id,domain FROM domain_additions WHERE program_id=?", (job["program_id"],)
            ):
                active = old["domain"] in current
                db.execute("UPDATE domain_additions SET active=? WHERE id=?", (int(active), old["id"]))
                if active:
                    db.execute(
                        "UPDATE domain_additions SET endpoints=?,scope_url=? WHERE id=?",
                        (json.dumps(current[old["domain"]]), scope_url, old["id"]),
                    )
            targets = []
            for name, endpoints in current.items():
                known = db.execute(
                    "SELECT 1 FROM domain_additions WHERE program_id=? AND domain=? LIMIT 1",
                    (job["program_id"], name),
                ).fetchone()
                if not known:
                    db.execute(
                        "INSERT INTO domain_additions VALUES (?,?,?,?,?,?,0,?,1)",
                        (
                            digest("baseline", job["program_id"], name),
                            "baseline:" + job["program_id"],
                            job["program_id"],
                            name,
                            json.dumps(endpoints),
                            scope_url,
                            now,
                        ),
                    )
            for name in sorted(set(added) & set(current)):
                identity = digest(job["id"], name)
                inserted = db.execute(
                    "INSERT OR IGNORE INTO domain_additions VALUES (?,?,?,?,?,?,?,?,1)",
                    (
                        identity,
                        job["id"],
                        job["program_id"],
                        name,
                        json.dumps(current[name]),
                        scope_url,
                        job["occurred_at"],
                        now,
                    ),
                ).rowcount
                if inserted:
                    targets.append({"domain": name, "endpoints": current[name]})
            # One domain per message keeps exact path constraints readable and within Discord limits.
            if job["notify"] and settings.delivery_enabled:
                for target in targets:
                    self.db._queue(
                        db,
                        "domain:" + digest(job["id"], target["domain"]),
                        {
                            "kind": "domain_added",
                            "program_id": job["program_id"],
                            **target,
                            "scope_url": scope_url,
                            "occurred_at": datetime.fromtimestamp(
                                job["occurred_at"], timezone.utc
                            ).isoformat(),
                        },
                    )
            db.execute(
                "UPDATE scope_jobs SET status='done',error_code=NULL,next_attempt=0 WHERE id=?", (job["id"],)
            )

    def stats(self):
        with self.db.connection() as db:
            counts = dict(db.execute("SELECT status,count(*) FROM scope_jobs GROUP BY status"))
            return {
                "active_domains": db.execute(
                    "SELECT count(*) FROM (SELECT program_id,domain FROM domain_additions "
                    "WHERE active=1 GROUP BY program_id,domain)"
                ).fetchone()[0],
                "pending_changes": counts.get("pending", 0),
                "failed_changes": counts.get("error", 0),
                "checked_changes": counts.get("done", 0),
                "sent_alerts": db.execute(
                    "SELECT count(*) FROM outbox WHERE state='sent' AND json_extract(payload,'$.kind')='domain_added'"
                ).fetchone()[0],
                "pending_alerts": db.execute(
                    "SELECT count(*) FROM outbox WHERE state IN ('pending','sending') AND json_extract(payload,'$.kind')='domain_added'"
                ).fetchone()[0],
            }

    def listings(self, limit=50, offset=0, query=""):
        with self.db.connection() as db:
            # Re-added domains appear once at their latest addition, not once per scan or version.
            where = "active=1 AND instr(lower(domain),lower(?))>0"
            total = db.execute(
                f"SELECT count(*) FROM (SELECT program_id,domain FROM domain_additions WHERE {where} "
                "GROUP BY program_id,domain)",
                (query,),
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT *,row_number() OVER (PARTITION BY program_id,domain ORDER BY occurred_at DESC,id DESC) AS rank "
                f"FROM domain_additions WHERE {where}",
                (query,),
            ).fetchall()
            items = sorted(
                (dict(r) for r in rows if r["rank"] == 1),
                key=lambda r: (r["occurred_at"], r["id"]),
                reverse=True,
            )[offset : offset + limit]
            for item in items:
                item["endpoints"] = json.loads(item["endpoints"])
                item["added_at"] = (
                    datetime.fromtimestamp(item["occurred_at"], timezone.utc).isoformat()
                    if item["occurred_at"]
                    else None
                )
                item["baseline"] = item["occurred_at"] == 0
                checked = db.execute(
                    "SELECT last_checked FROM scope_programs WHERE program_id=?", (item["program_id"],)
                ).fetchone()
                item["checked_at"] = (
                    datetime.fromtimestamp(checked[0], timezone.utc).isoformat() if checked else None
                )
            return {"total": total, "items": items}


async def poll_domains(adapter, db, settings):
    store = DomainStore(db)
    records, cursor = [], 0
    for _ in range(settings.pages_per_cycle):
        body = await adapter.get_json("activities", cursor)
        page = body.get("records")
        if not isinstance(page, list) or len(page) > settings.page_size:
            raise ProviderError("schema_error")
        for record in page:
            adapter.normalize("activities", object_value(record))
        records.extend(page)
        cursor = adapter.next_cursor("activities", body, cursor, len(page))
        if cursor is None:
            break
    store.ingest(records)
    for job in store.pending(settings.intigriti_scope_jobs_per_cycle):
        current = None
        try:
            detail = await adapter.intigriti_resource(job["program_id"])
            current, url, status = current_scope(detail, job["program_id"])
            added = set()
            if job["kind"] == "scope" and current:
                previous = await adapter.intigriti_resource(job["program_id"], job["from_version"])
                before = scope_domains(previous.get("domains"), job["from_version"])
                if detail["domains"]["id"] == job["to_version"]:
                    after = scope_domains(detail["domains"], job["to_version"])
                else:
                    version = await adapter.intigriti_resource(job["program_id"], job["to_version"])
                    after = scope_domains(version.get("domains"), job["to_version"])
                added = set(after) - set(before)
            store.finish(job, added, current, url, status, settings)
        except ProviderError as exc:
            if current is None and exc.http_status in {403, 404}:
                store.unavailable(job["program_id"])
            store.fail(job, "scope_access_denied" if exc.http_status == 403 else exc.code, exc.delay or 3600)
            if exc.code == "rate_limited" or (exc.code == "auth_error" and exc.http_status != 403):
                raise
        except (ValueError, TypeError, KeyError):
            store.fail(job, "schema_error")
    stats = store.stats()
    with db.connection(True) as conn:
        conn.execute(
            "UPDATE collections SET baseline_done=1,cursor=0,status=?,error_code=?,last_success=?,pages=pages+1 "
            "WHERE provider='intigriti' AND name='domains'",
            (
                "error" if stats["failed_changes"] else "syncing" if stats["pending_changes"] else "healthy",
                "scope_check_failed" if stats["failed_changes"] else None,
                utcnow(),
            ),
        )
