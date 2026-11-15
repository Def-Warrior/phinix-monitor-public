import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from app.models import Event, matches_rules, utcnow

MIGRATIONS = [
    [
        """CREATE TABLE events (
          provider TEXT NOT NULL, collection TEXT NOT NULL, external_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          PRIMARY KEY(provider, collection, external_id))""",
        """CREATE TABLE collections (
          provider TEXT NOT NULL, name TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
          baseline_done INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'waiting',
          error_code TEXT, last_success TEXT, pages INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(provider, name))""",
        """CREATE TABLE providers (
          name TEXT PRIMARY KEY, cooldown_until REAL NOT NULL DEFAULT 0,
          error_code TEXT, auth_alerted INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE outbox (
          id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          failures INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL DEFAULT 0,
          owner TEXT, lease_until REAL, error_code TEXT, created_at TEXT NOT NULL, sent_at TEXT)""",
        "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ],
    [
        "CREATE INDEX outbox_due ON outbox(state, due_at, lease_until)",
        "CREATE INDEX events_seen ON events(last_seen DESC)",
    ],
    [
        """CREATE TABLE scope_jobs (
          id TEXT PRIMARY KEY, program_id TEXT NOT NULL, kind TEXT NOT NULL,
          from_version TEXT, to_version TEXT, occurred_at INTEGER NOT NULL, notify INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', error_code TEXT, next_attempt REAL NOT NULL DEFAULT 0)""",
        """CREATE TABLE scope_programs (
          program_id TEXT PRIMARY KEY, last_checked REAL NOT NULL, status TEXT NOT NULL,
          domains_json TEXT NOT NULL)""",
        """CREATE TABLE domain_additions (
          id TEXT PRIMARY KEY, change_id TEXT NOT NULL, program_id TEXT NOT NULL,
          domain TEXT NOT NULL, endpoints TEXT NOT NULL, scope_url TEXT NOT NULL,
          occurred_at INTEGER NOT NULL, detected_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          UNIQUE(change_id,domain))""",
        "CREATE INDEX domain_newest ON domain_additions(active,occurred_at DESC)",
        "CREATE INDEX scope_pending ON scope_jobs(status,next_attempt,occurred_at DESC)",
    ],
]


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self.connection(True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise RuntimeError("Database schema is newer than this application")
            for i in range(version, len(MIGRATIONS)):
                for statement in MIGRATIONS[i]:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={i + 1}")

    def ping(self):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM metadata LIMIT 1").fetchone() is not False

    def ensure_collection(self, provider, name):
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO collections(provider,name) VALUES (?,?)", (provider, name))
            db.execute("INSERT OR IGNORE INTO providers(name) VALUES (?)", (provider,))

    def checkpoint(self, provider, name):
        with self.connection() as db:
            return dict(
                db.execute(
                    "SELECT * FROM collections WHERE provider=? AND name=?", (provider, name)
                ).fetchone()
            )

    @staticmethod
    def _queue(db, key, payload):
        db.execute(
            "INSERT OR IGNORE INTO outbox(id,dedupe_key,payload,created_at) VALUES (?,?,?,?)",
            (uuid.uuid4().hex, key, json.dumps(payload), utcnow()),
        )

    def commit_page(self, provider, collection, events, next_cursor, settings):
        # No awaits or network operations inside this transaction.
        with self.connection(True) as db:
            checkpoint = db.execute(
                "SELECT * FROM collections WHERE provider=? AND name=?", (provider, collection)
            ).fetchone()
            if checkpoint is None:
                raise RuntimeError("Collection not initialized")
            baseline = not checkpoint["baseline_done"] and not settings.notify_history
            now = utcnow()
            for event in events:
                if event.provider != provider or event.collection != collection:
                    raise ValueError("Mismatched collection")
                identity = (provider, collection, event.external_id)
                previous = db.execute(
                    "SELECT fingerprint,revision FROM events WHERE provider=? AND collection=? AND external_id=?",
                    identity,
                ).fetchone()
                fingerprint = event.fingerprint()
                changed = previous is None or previous["fingerprint"] != fingerprint
                revision = 1 if previous is None else previous["revision"] + int(changed)
                db.execute(
                    """INSERT INTO events VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(provider,collection,external_id)
                   DO UPDATE SET fingerprint=excluded.fingerprint,payload=excluded.payload,
                     revision=excluded.revision,last_seen=excluded.last_seen""",
                    (*identity, fingerprint, event.payload(), revision, now, now),
                )
                if changed and not baseline and settings.delivery_enabled and matches_rules(event, settings):
                    self._queue(
                        db,
                        f"{provider}:{collection}:{event.external_id}:{revision}",
                        {
                            "kind": "new" if previous is None else "changed",
                            "event": event.model_dump(mode="json"),
                        },
                    )
            db.execute(
                """UPDATE collections SET cursor=?,baseline_done=?,status=?,error_code=NULL,
                       last_success=?,pages=pages+1 WHERE provider=? AND name=?""",
                (
                    next_cursor or 0,
                    int(checkpoint["baseline_done"] or next_cursor is None),
                    "syncing" if next_cursor is not None else "healthy",
                    now,
                    provider,
                    collection,
                ),
            )

    def collection_error(self, provider, name, code):
        with self.connection(True) as db:
            db.execute(
                "UPDATE collections SET status='error',error_code=? WHERE provider=? AND name=?",
                (code, provider, name),
            )

    def provider_state(self, provider):
        with self.connection() as db:
            return dict(db.execute("SELECT * FROM providers WHERE name=?", (provider,)).fetchone())

    def provider_error(self, provider, code, delay, notify):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM providers WHERE name=?", (provider,)).fetchone()
            if code == "auth_error" and notify and not row["auth_alerted"]:
                self._queue(
                    db,
                    f"auth:{provider}:{uuid.uuid4().hex}",
                    {"kind": "operational", "provider": provider, "code": code},
                )
            db.execute(
                "UPDATE providers SET cooldown_until=?,error_code=?,auth_alerted=? WHERE name=?",
                (time.time() + delay, code, int(row["auth_alerted"] or code == "auth_error"), provider),
            )

    def provider_success(self, provider):
        with self.connection(True) as db:
            db.execute(
                "UPDATE providers SET cooldown_until=0,error_code=NULL,auth_alerted=0 WHERE name=?",
                (provider,),
            )

    def claim(self, owner, lease_seconds=120):
        now = time.time()
        with self.connection(True) as db:
            cooldown = db.execute("SELECT value FROM metadata WHERE key='discord_cooldown'").fetchone()
            if cooldown and float(cooldown[0]) > now:
                return None
            row = db.execute(
                """SELECT * FROM outbox WHERE (state='pending' AND due_at<=?)
                          OR (state='sending' AND lease_until<=?) ORDER BY created_at,id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE outbox SET state='sending',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                (owner, now + lease_seconds, row["id"]),
            )
            return dict(db.execute("SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone())

    def delivered(self, row_id, owner):
        with self.connection(True) as db:
            db.execute(
                "UPDATE outbox SET state='sent',sent_at=?,owner=NULL,lease_until=NULL,error_code=NULL WHERE id=? AND owner=?",
                (utcnow(), row_id, owner),
            )

    def delivery_failed(self, row, owner, code, delay, max_retries, permanent=False, rate_limited=False):
        failures = row["failures"] + (0 if rate_limited else 1)
        state = "failed" if permanent or failures > max_retries else "pending"
        with self.connection(True) as db:
            db.execute(
                """UPDATE outbox SET state=?,failures=?,due_at=?,owner=NULL,lease_until=NULL,error_code=?
                       WHERE id=? AND owner=?""",
                (state, failures, time.time() + delay, code, row["id"], owner),
            )
            if rate_limited:
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('discord_cooldown',?)",
                    (str(time.time() + delay),),
                )

    def release_owner(self, owner):
        with self.connection(True) as db:
            db.execute(
                "UPDATE outbox SET state='pending',owner=NULL,lease_until=NULL WHERE owner=?", (owner,)
            )

    def queue_test(self):
        with self.connection(True) as db:
            recent = db.execute(
                "SELECT created_at FROM outbox WHERE dedupe_key LIKE 'test:%' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if recent:
                from datetime import datetime

                if time.time() - datetime.fromisoformat(recent[0]).timestamp() < 60:
                    return False
            self._queue(db, f"test:{uuid.uuid4().hex}", {"kind": "test"})
        return True

    def notification_status(self):
        with self.connection() as db:
            counts = {
                r["state"]: r["n"] for r in db.execute("SELECT state,count(*) n FROM outbox GROUP BY state")
            }
            recent = [
                dict(r)
                for r in db.execute(
                    "SELECT id,state,attempts,failures,error_code,created_at,sent_at FROM outbox ORDER BY created_at DESC LIMIT 30"
                )
            ]
            return {
                "counts": {k: counts.get(k, 0) for k in ["pending", "sending", "sent", "failed"]},
                "recent": recent,
            }

    def snapshot(self):
        with self.connection() as db:
            return {
                "collections": [
                    dict(r) for r in db.execute("SELECT * FROM collections ORDER BY provider,name")
                ],
                "provider_states": [dict(r) for r in db.execute("SELECT * FROM providers")],
                "event_count": db.execute("SELECT count(*) FROM events").fetchone()[0],
                "schema_version": db.execute("PRAGMA user_version").fetchone()[0],
            }

    def save_cloud_status(self, status):
        with self.connection(True) as db:
            db.execute(
                "INSERT INTO metadata(key,value) VALUES ('cloud_status',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({**status, "updated_at": utcnow()}),),
            )

    def cloud_status(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='cloud_status'").fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def browser_link(db, event):
        """API credentials never belong in links. Use the provider's signed-in web UI."""
        url = event["url"]
        if event["provider"] == "intigriti" and urlsplit(url).hostname == "api.intigriti.com":
            program = db.execute(
                "SELECT payload FROM events WHERE provider='intigriti' "
                "AND collection='programs' AND external_id=?",
                (event["program"],),
            ).fetchone()
            if program:
                candidate = Event.model_validate_json(program[0])
                if urlsplit(candidate.url).hostname in {"app.intigriti.com", "intigriti.com"}:
                    return candidate.url, "Open program on Intigriti"
            return "https://app.intigriti.com/researcher/dashboard", "Open Intigriti dashboard"
        if event["provider"] == "bugcrowd" and urlsplit(url).hostname == "hacker-api.bugcrowd.com":
            return "https://bugcrowd.com/dashboard", "Open Bugcrowd dashboard"
        return url, "Open record on provider website"

    def events(self, limit=50, offset=0, provider="", severity="", query=""):
        clauses, params = ["1=1"], []
        for field, value in [("provider", provider), ("severity", severity)]:
            if value:
                clauses.append(f"json_extract(payload,'$.{field}')=?")
                params.append(value)
        if query:
            clauses.append(
                "(instr(lower(json_extract(payload,'$.title')),lower(?))>0 OR instr(lower(json_extract(payload,'$.program')),lower(?))>0)"
            )
            params.extend([query, query])
        where = " AND ".join(clauses)
        with self.connection() as db:
            total = db.execute(f"SELECT count(*) FROM events WHERE {where}", params).fetchone()[0]
            rows = db.execute(
                f"SELECT payload,first_seen,last_seen,revision FROM events WHERE {where} ORDER BY last_seen DESC,provider,collection,external_id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
            items = []
            for row in rows:
                event = json.loads(row["payload"])
                browser_url, link_label = self.browser_link(db, event)
                items.append(
                    {
                        **event,
                        "browser_url": browser_url,
                        "link_label": link_label,
                        "first_seen": row["first_seen"],
                        "last_seen": row["last_seen"],
                        "revision": row["revision"],
                    }
                )
            return {"total": total, "items": items}

    def import_legacy(self, path, settings):
        # Only the explicit versioned interchange format is recognized. Unknown files fail closed.
        if not path:
            return
        with self.connection() as db:
            if db.execute("SELECT 1 FROM metadata WHERE key='legacy_imported'").fetchone():
                return
        if path.stat().st_size > 10_000_000:
            raise ValueError("Legacy file too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("format") != "phinix-events-v1"
            or not isinstance(data.get("events"), list)
        ):
            raise ValueError("Unsupported legacy format")
        events = [Event.model_validate(e) for e in data["events"]]
        with self.connection(True) as db:
            for event in events:
                now = utcnow()
                db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)",
                    (
                        event.provider,
                        event.collection,
                        event.external_id,
                        event.fingerprint(),
                        event.payload(),
                        1,
                        now,
                        now,
                    ),
                )
            db.execute("INSERT INTO metadata VALUES ('legacy_imported',?)", (utcnow(),))

    def backup(self, destination):
        with self.connection() as db, closing(sqlite3.connect(destination)) as target:
            db.backup(target)
