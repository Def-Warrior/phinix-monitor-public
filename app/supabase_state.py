"""Private, versioned checkpoints in Supabase; no credentials enter snapshots."""

import base64
import gzip
import hashlib
import io
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from app.cloud_errors import CloudStateError
from app.site_publish import site_snapshot

MAX_STATE_BYTES = 25_000_000


class SupabaseState:
    def __init__(self, client, url=None, key=None):
        self.client = client
        self.url = (url if url is not None else os.environ.get("SUPABASE_URL", "")).rstrip("/")
        key = key if key is not None else os.environ.get("SUPABASE_SECRET_KEY", "")
        if not re.fullmatch(r"https://[a-z0-9]{20}\.supabase\.co", self.url) or not key:
            raise CloudStateError("invalid_supabase_configuration")
        self.headers = {"apikey": key}
        if key.startswith("eyJ"):
            self.headers["Authorization"] = "Bearer " + key
        self.revision = None
        self.loaded = False

    async def request(self, method, path, **kwargs):
        try:
            response = await self.client.request(
                method,
                self.url + "/rest/v1/" + path,
                headers=self.headers,
                follow_redirects=False,
                timeout=20,
                **kwargs,
            )
            if response.status_code == 409:
                raise CloudStateError("supabase_state_conflict")
            if response.status_code != 200:
                raise CloudStateError("supabase_state_request_failed")
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise CloudStateError("supabase_state_unavailable") from None

    async def load(self, destination, initialize=False):
        rows = await self.request("GET", "phinix_checkpoint?id=eq.1&select=revision,body,sha256,byte_count")
        if rows == []:
            if not initialize:
                raise CloudStateError("state_missing_manual_initialization_required")
            self.loaded = True
            return False
        try:
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError
            row = rows[0]
            revision = str(UUID(row["revision"]))
            if revision == self.revision:
                return False
            if not 0 < row["byte_count"] <= MAX_STATE_BYTES or len(row["body"]) > 35_000_000:
                raise ValueError
            encoded = base64.b64decode(row["body"], validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as compressed:
                raw = compressed.read(MAX_STATE_BYTES + 1)
            if (
                len(raw) != row["byte_count"]
                or not raw.startswith(b"SQLite format 3\0")
                or hashlib.sha256(raw).hexdigest() != row["sha256"]
            ):
                raise ValueError
            destination.write_bytes(raw)
            destination.chmod(0o600)
            with closing(sqlite3.connect(destination)) as conn:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError
        except (KeyError, TypeError, ValueError, OSError, EOFError, sqlite3.Error):
            raise CloudStateError("invalid_supabase_state") from None
        self.revision = revision
        self.loaded = True
        return True

    async def save(self, db, dashboard=""):
        if not self.loaded:
            raise CloudStateError("supabase_state_not_loaded")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.db"
            db.backup(snapshot)
            if snapshot.stat().st_size > MAX_STATE_BYTES:
                raise CloudStateError("state_size_limit")
            raw = snapshot.read_bytes()
        new_revision = str(uuid4())
        saved = await self.request(
            "POST",
            "rpc/phinix_commit",
            json={
                "expected_revision": self.revision,
                "next_revision": new_revision,
                "snapshot_body": base64.b64encode(gzip.compress(raw, mtime=0)).decode(),
                "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
                "snapshot_bytes": len(raw),
                "domain_feed": site_snapshot(db),
            },
        )
        if saved != new_revision:
            raise CloudStateError("supabase_commit_not_confirmed")
        self.revision = new_revision
