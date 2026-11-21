"""Read-only cache for the free web service; polling runs independently."""

import asyncio
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from app.cloud_errors import CloudStateError
from app.edge_view import EdgeDatabase
from app.storage import Database
from app.supabase_state import SupabaseState


class SupabaseViewer:
    def __init__(self, client):
        self.state = SupabaseState(client)
        self.directory = tempfile.TemporaryDirectory(prefix="phinix-viewer-")
        self.db = None
        self.next_check = 0
        self.failed = False
        self.lock = asyncio.Lock()
        self.edge_mode = False

    async def edge_refresh(self, candidate):
        rows = await self.state.request(
            "GET",
            "phinix_runtime?id=eq.1&select=enabled,configured,last_poll,status,"
            "feed_revision,provider_cooldown,last_error,last_sent_at,last_source_delivery_ms,last_api_seen_delivery_ms",
        )
        runtime = rows[0] if rows else {}
        # A paused, migrated worker still owns the newest state after a viewer restart.
        migrated = runtime.get("configured") and runtime.get("last_poll")
        if not self.edge_mode and not runtime.get("enabled") and not migrated:
            return False
        revision = runtime["feed_revision"]
        if not self.edge_mode or revision != self.state.revision:
            rows = await self.state.request("GET", "phinix_feed?id=eq.1&select=payload")
            db = EdgeDatabase(candidate, rows[0]["payload"], runtime)
            self.replace(db)
        else:
            self.db.runtime = runtime
        self.state.revision = revision
        self.edge_mode = True
        return True

    def replace(self, db):
        previous, self.db = self.db, db
        if previous:
            for suffix in ("", "-wal", "-shm"):
                Path(str(previous.path) + suffix).unlink(missing_ok=True)

    async def refresh(self):
        async with self.lock:
            if time.monotonic() < self.next_check and self.db:
                return self.db
            self.next_check = time.monotonic() + 2
            candidate = Path(self.directory.name) / f"{uuid4()}.db"
            revision = self.state.revision
            try:
                if await self.edge_refresh(candidate):
                    pass
                elif await self.state.load(candidate):
                    db = Database(candidate)
                    db.initialize()
                    if not db.cloud_status():
                        raise CloudStateError("missing_snapshot_status")
                    self.replace(db)
                self.failed = False
            except Exception:
                self.state.revision = revision
                candidate.unlink(missing_ok=True)
                self.failed = True
                self.next_check = time.monotonic() + 60
                if self.db is None:
                    raise CloudStateError("supabase_snapshot_unavailable") from None
            return self.db

    def close(self):
        self.directory.cleanup()
