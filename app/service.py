import asyncio
import json
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from app.discord import Dispatcher
from app.domains import DomainStore, poll_domains
from app.providers import Adapter, ProviderError, backoff
from app.storage import Database

log = logging.getLogger("phinix")


class MonitoringService:
    def __init__(self, db, settings, client):
        self.db, self.settings, self.client = db, settings, client
        self.adapters = {
            p: Adapter(p, settings, client)
            for p in ("hackerone", "bugcrowd", "intigriti")
            if settings.enabled(p)
        }
        self.dispatcher = Dispatcher(db, settings, client)
        self.tasks = {}
        self.worker_health = {}
        self.stopping = False
        self.started_at = time.time()
        self.storage_ok = True
        self.snapshot_refresh_error = False

    def refresh_snapshot(self):
        manifest = self.settings.snapshot_manifest_path
        if not self.settings.snapshot_mode or not manifest:
            return
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            path = Path(data["database"]).resolve()
            if path.parent != manifest.resolve().parent or path.suffix != ".db":
                raise ValueError("Invalid snapshot path")
            if path != self.db.path.resolve():
                candidate = Database(path)
                if not candidate.cloud_status() or not candidate.ping():
                    raise ValueError("Missing snapshot status")
                self.db = candidate
            self.snapshot_refresh_error = bool(data.get("sync_error"))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            self.snapshot_refresh_error = True

    async def start(self):
        for name, adapter in self.adapters.items():
            for collection in adapter.collections:
                self.db.ensure_collection(name, collection)
            self.tasks[name] = asyncio.create_task(
                self.supervise(name, lambda a=adapter: self.provider_loop(a))
            )
            self.worker_health[name] = {"state": "starting", "restarts": 0, "error_code": None}
        if self.settings.delivery_enabled:
            self.worker_health["discord"] = {"state": "starting", "restarts": 0, "error_code": None}
            self.tasks["discord"] = asyncio.create_task(self.supervise("discord", self.dispatcher.run))

    async def supervise(self, name, factory):
        restarts = 0
        while not self.stopping:
            try:
                self.worker_health[name].update(state="running", error_code=None)
                await factory()
                if not self.stopping:
                    raise RuntimeError("Unexpected worker exit")
            except asyncio.CancelledError:
                raise
            except Exception:
                restarts += 1
                self.worker_health[name].update(
                    state="recovering", restarts=restarts, error_code="worker_error"
                )
                # Never log exception messages, request URLs, upstream bodies, or credentials.
                log.warning("worker_restart provider=%s", name)
                await asyncio.sleep(backoff(restarts))

    async def poll_once(self, adapter):
        provider = self.db.provider_state(adapter.name)
        if provider["cooldown_until"] > time.time():
            return
        if adapter.name == "intigriti" and self.settings.intigriti_domains_only:
            self.db.ensure_collection("intigriti", "domains")
            try:
                await poll_domains(adapter, self.db, self.settings)
                self.db.provider_success("intigriti")
                self.storage_ok = True
            except ProviderError as exc:
                self.db.collection_error("intigriti", "domains", exc.code)
                if exc.code in {"auth_error", "rate_limited"}:
                    self.db.provider_error("intigriti", exc.code, exc.delay, self.settings.delivery_enabled)
            except (ValueError, TypeError, KeyError):
                self.db.collection_error("intigriti", "domains", "schema_error")
            return
        provider_ok = True
        for collection in adapter.collections:
            try:
                for _ in range(self.settings.pages_per_cycle):
                    checkpoint = self.db.checkpoint(adapter.name, collection)
                    events, next_cursor = await adapter.fetch(collection, checkpoint["cursor"])
                    self.db.commit_page(adapter.name, collection, events, next_cursor, self.settings)
                    self.storage_ok = True
                    if next_cursor is None:
                        break
            except ProviderError as exc:
                self.db.collection_error(adapter.name, collection, exc.code)
                provider_ok = False
                if exc.code in {"auth_error", "rate_limited"}:
                    self.db.provider_error(adapter.name, exc.code, exc.delay, self.settings.delivery_enabled)
                    return
                # Schema or network failure belongs to this collection only.
            except Exception:
                self.storage_ok = False
                raise
        if provider_ok:
            self.db.provider_success(adapter.name)

    async def provider_loop(self, adapter):
        while not self.stopping:
            await self.poll_once(adapter)
            state = self.db.provider_state(adapter.name)
            delay = max(self.settings.poll_interval_seconds, state["cooldown_until"] - time.time())
            await asyncio.sleep(delay + random.uniform(0, min(10, delay * 0.05)))

    def ready(self):
        try:
            database_ok = self.db.ping()
        except Exception:
            database_ok = False
        workers_ok = all(
            not t.done() and self.worker_health[name]["state"] in {"starting", "running"}
            for name, t in self.tasks.items()
        )
        return bool(database_ok and self.storage_ok and workers_ok and not self.stopping)

    def status(self):
        if self.settings.snapshot_mode:
            saved = self.db.cloud_status()
            if saved:
                updated = datetime.fromisoformat(saved["updated_at"])
                return {
                    **saved,
                    "mode": "github_snapshot",
                    "workers": {},
                    "snapshot_updated_at": saved["updated_at"],
                    "snapshot_stale": (datetime.now(timezone.utc) - updated).total_seconds() > 10800,
                    "snapshot_refresh_error": self.snapshot_refresh_error,
                    "ready": self.ready(),
                    "poll_interval_seconds": 3600,
                    "local_delivery_enabled": False,
                }
        snapshot = self.db.snapshot()
        if self.settings.intigriti_domains_only:
            snapshot["collections"] = [
                c for c in snapshot["collections"] if c["provider"] == "intigriti" and c["name"] == "domains"
            ]
        provider_states = {p["name"]: p for p in snapshot.pop("provider_states")}
        integrations = []
        for name in ("hackerone", "bugcrowd", "intigriti"):
            enabled = name in self.adapters
            collections = [c for c in snapshot["collections"] if c["provider"] == name]
            state = provider_states.get(name, {})
            errors = sum(c["status"] == "error" for c in collections)
            status = "disabled"
            if enabled:
                status = state.get("error_code") or (
                    "error"
                    if errors == len(collections) and errors
                    else "degraded"
                    if errors
                    else "syncing"
                    if any(
                        not c["baseline_done"] or c["cursor"] or c["status"] == "syncing" for c in collections
                    )
                    else "healthy"
                )
            integrations.append(
                {
                    "name": name,
                    "enabled": enabled,
                    "status": status,
                    "reason": (
                        "Enable Intigriti and configure its token"
                        if name == "intigriti"
                        else "Credentials not configured"
                    )
                    if not enabled
                    else None,
                    "cooldown_until": state.get("cooldown_until", 0),
                }
            )
        return {
            **snapshot,
            "mode": "snapshot_unavailable" if self.settings.snapshot_mode else "live",
            "focus": "intigriti_domains" if self.settings.intigriti_domains_only else "all",
            "domain_monitoring": DomainStore(self.db).stats()
            if self.settings.intigriti_domains_only
            else None,
            "local_delivery_enabled": self.settings.delivery_enabled,
            "ready": self.ready(),
            "uptime_seconds": int(time.time() - self.started_at),
            "workers": self.worker_health,
            "integrations": integrations,
            "discord": {
                "enabled": self.settings.delivery_enabled,
                "channel_label": self.settings.discord_channel_name,
                "include_details": self.settings.discord_include_details,
            },
            "notifications": self.db.notification_status(),
            "poll_interval_seconds": self.settings.poll_interval_seconds,
        }

    async def stop(self):
        self.stopping = True
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
