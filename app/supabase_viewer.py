"""Read-only cache for the free web service; polling stays in GitHub Actions."""

import asyncio
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from app.cloud_errors import CloudStateError
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

    async def refresh(self):
        async with self.lock:
            if time.monotonic() < self.next_check and self.db:
                return self.db
            self.next_check = time.monotonic() + 2
            candidate = Path(self.directory.name) / f"{uuid4()}.db"
            revision = self.state.revision
            try:
                if await self.state.load(candidate):
                    db = Database(candidate)
                    db.initialize()
                    if not db.cloud_status():
                        raise CloudStateError("missing_snapshot_status")
                    previous = self.db
                    self.db = db
                    if previous:
                        for suffix in ("", "-wal", "-shm"):
                            Path(str(previous.path) + suffix).unlink(missing_ok=True)
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
