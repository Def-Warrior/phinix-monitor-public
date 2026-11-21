"""Bounded GitHub Actions polling with private, durable repository state.

This is a scheduled alternative to the continuous web server. Never run both
against the same account/state. State must be saved remotely before Discord
delivery. A failed final save can duplicate a delivery, but cannot lose it.
"""

import asyncio
import base64
import hashlib
import html
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import httpx

from app.cloud_errors import CloudStateError
from app.config import Settings
from app.domains import DomainStore
from app.models import utcnow
from app.service import MonitoringService
from app.site_publish import publish_site
from app.storage import Database

BRANCH = "phinix-state"
MAX_STATE_BYTES = 25_000_000


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CloudStateError("invalid_git_object")
    return value


class GitHubState:
    def __init__(self, client, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise CloudStateError("invalid_repository")
        self.client = client
        self.repository = repository
        self.root = f"https://api.github.com/repos/{repository}"
        self.head = None
        self.checked_private = False

    async def request(self, method, path="", *, missing_ok=False, **kwargs):
        try:
            response = await self.client.request(method, self.root + path, **kwargs)
            if missing_ok and response.status_code == 404:
                return None
            if response.status_code not in {200, 201}:
                raise CloudStateError("github_state_request_failed")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (httpx.HTTPError, ValueError) as from_error:
            raise CloudStateError("github_state_unavailable") from from_error

    async def load(self, destination, initialize=False):
        repo = await self.request("GET")
        if repo.get("private") is not True:
            raise CloudStateError("private_repository_required")
        self.checked_private = True
        ref = await self.request("GET", f"/git/ref/heads/{BRANCH}", missing_ok=True)
        if ref is None:
            if not initialize:
                raise CloudStateError("state_missing_manual_initialization_required")
            return
        self.head = sha(ref["object"]["sha"])
        commit = await self.request("GET", f"/git/commits/{self.head}")
        tree = await self.request("GET", f"/git/trees/{sha(commit['tree']['sha'])}")
        entries = [x for x in tree["tree"] if x.get("path") == "phinix.db" and x.get("type") == "blob"]
        if len(entries) != 1 or tree.get("truncated"):
            raise CloudStateError("invalid_state_tree")
        entry = entries[0]
        if not 0 < entry.get("size", 0) <= MAX_STATE_BYTES:
            raise CloudStateError("state_size_limit")
        blob_sha = sha(entry["sha"])
        blob = await self.request("GET", f"/git/blobs/{blob_sha}")
        if blob.get("encoding") != "base64":
            raise CloudStateError("invalid_state_encoding")
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != entry["size"] or digest != blob_sha or not raw.startswith(b"SQLite format 3\0"):
            raise CloudStateError("invalid_state_blob")
        destination.write_bytes(raw)
        destination.chmod(0o600)
        with closing(sqlite3.connect(destination)) as conn:
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CloudStateError("invalid_state_database")

    async def save(self, db, dashboard):
        if not self.checked_private:
            raise CloudStateError("private_repository_not_checked")
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.db"
            db.backup(snapshot)
            if snapshot.stat().st_size > MAX_STATE_BYTES:
                raise CloudStateError("state_size_limit")
            raw = snapshot.read_bytes()
        entries = []
        for name, content in [("phinix.db", raw), ("README.md", dashboard.encode())]:
            blob = await self.request(
                "POST",
                "/git/blobs",
                json={"encoding": "base64", "content": base64.b64encode(content).decode()},
            )
            entries.append({"path": name, "mode": "100644", "type": "blob", "sha": sha(blob["sha"])})
        tree = await self.request("POST", "/git/trees", json={"tree": entries})
        commit = await self.request(
            "POST",
            "/git/commits",
            json={
                "message": "Update private Phinix monitoring state",
                "tree": sha(tree["sha"]),
                "parents": [self.head] if self.head else [],
            },
        )
        new_head = sha(commit["sha"])
        if self.head:
            # A second writer's commit makes this non-fast-forward and is rejected.
            await self.request("PATCH", f"/git/refs/heads/{BRANCH}", json={"sha": new_head, "force": False})
        else:
            await self.request("POST", "/git/refs", json={"ref": f"refs/heads/{BRANCH}", "sha": new_head})
        self.head = new_head


def safe(value):
    return html.escape(str(value)).replace("@", "@\u200b").replace("\n", " ").replace("\r", " ")


def dashboard(service, phase, warnings=()):
    status = service.status()
    if service.settings.intigriti_domains_only:
        return domain_dashboard(service, status, phase, warnings)
    lines = [
        "# Phinix · private monitoring dashboard",
        "",
        f"Updated **{utcnow()}** · **{safe(phase)}**",
        "",
        "Hourly GitHub Actions monitoring. Scheduled runs can be delayed. This is a snapshot, not a live server.",
        "",
        f"**Stored events: {status['event_count']}**",
        "",
        "| Integration | Status |",
        "|---|---|",
    ]
    for item in status["integrations"]:
        lines.append(f"| {item['name']} | {item['status']} |")
    lines.extend(
        [
            f"| Discord | {'configured' if status['discord']['enabled'] else 'disabled'} |",
            "",
            "## Deliveries",
            "",
            "| Pending | Sending | Sent | Failed |",
            "|---|---|---|---|",
            "| "
            + " | ".join(
                str(status["notifications"]["counts"][k]) for k in ("pending", "sending", "sent", "failed")
            )
            + " |",
            "",
            "## Collection checkpoints",
            "",
            "| Provider | Collection | Baseline complete | Pages | Last success |",
            "|---|---|---|---|---|",
        ]
    )
    for item in status["collections"]:
        lines.append(
            f"| {item['provider']} | {item['name']} | {bool(item['baseline_done'])} | {item['pages']} | {item['last_success'] or '—'} |"
        )
    lines.extend(
        [
            "",
            "## Recent records",
            "",
            "<table><tr><th>Provider</th><th>Title</th><th>Severity</th><th>State</th></tr>",
        ]
    )
    for event in service.db.events(limit=20)["items"]:
        lines.append(
            "<tr>"
            + "".join(f"<td>{safe(event[k])}</td>" for k in ("provider", "title", "severity", "state"))
            + "</tr>"
        )
    lines.extend(
        [
            "</table>",
            "",
            "Full records and checkpoints are in the private SQLite state snapshot. Credentials are stored only in repository secrets.",
            "",
        ]
    )
    if warnings:
        lines.extend(["Run notices: " + ", ".join(safe(w) for w in warnings), ""])
    return "\n".join(lines)


def domain_dashboard(service, status, phase, warnings):
    store = DomainStore(service.db)
    stats = store.stats()
    source = next(i for i in status["integrations"] if i["name"] == "intigriti")
    lines = [
        "# Phinix · Intigriti testing domains",
        "",
        f"Updated **{utcnow()}** · **{safe(phase)}**",
        "",
        f"Intigriti: **{safe(source['status'])}** · Hourly monitoring · Latest additions first",
        "",
        f"**{stats['active_domains']} domains** · {stats['sent_alerts']} domain alerts delivered · {stats['pending_alerts']} alerts pending",
        "",
        f"Scope changes checked: {stats['checked_changes']} · Waiting: {stats['pending_changes']} · Errors: {stats['failed_changes']}",
        "",
        "Confirmed scope additions appear first. Existing domains from recently updated programs are loaded silently as a baseline. Removed/out-of-scope assets and non-domain targets are excluded.",
        "",
        "<table><tr><th>Domain</th><th>Exact listed scope</th><th>Added (UTC)</th><th>Reference</th></tr>",
    ]
    for item in store.listings(limit=50)["items"]:
        lines.append(
            f"<tr><td><code>{safe(item['domain'])}</code></td><td>{safe(' · '.join(item['endpoints']))}</td>"
            f'<td>{safe(item["added_at"] or "Existing scope")}</td><td><a href="{safe(item["scope_url"])}">Scope</a></td></tr>'
        )
    lines.extend(
        [
            "</table>",
            "",
            "No connection-test messages or generic program/activity alerts are sent in this mode.",
            "",
        ]
    )
    if warnings:
        lines.append("Run notices: " + ", ".join(safe(w) for w in warnings))
    return "\n".join(lines)


async def run_cycle(service, state, *, send_test=False, poll_seconds=30, delivery_seconds=45):
    for name, adapter in service.adapters.items():
        for collection in adapter.collections:
            service.db.ensure_collection(name, collection)
    warnings = []
    try:
        async with asyncio.timeout(poll_seconds):
            results = await asyncio.gather(
                *(service.poll_once(a) for a in service.adapters.values()), return_exceptions=True
            )
            if any(isinstance(result, BaseException) for result in results):
                warnings.append("provider_cycle_error")
    except TimeoutError:
        warnings.append("poll_time_limit_partial_progress_saved")
    if send_test and not service.settings.intigriti_domains_only:
        service.db.queue_test()
    # Never send externally before new events and the outbox are durable remotely.
    service.db.save_cloud_status(service.status())
    await state.save(service.db, dashboard(service, "Events saved; delivery pending", warnings))
    try:
        async with asyncio.timeout(delivery_seconds):
            for _ in range(100):
                if not await service.dispatcher.once():
                    break
                await asyncio.sleep(0.5)
    except TimeoutError:
        warnings.append("delivery_time_limit_remaining_rows_saved")
    finally:
        service.db.release_owner(service.dispatcher.owner)
    result = dashboard(service, "Run completed", warnings)
    service.db.save_cloud_status(service.status())
    await state.save(service.db, result)
    return result


async def execute():
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise CloudStateError("trusted_main_branch_required")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "phinix.db"
        # No HTTP server exists in this mode; production web-server admin validation is not needed.
        settings = Settings(_env_file=None, database_path=path, app_env="test", legacy_state_path=None)
        async with (
            httpx.AsyncClient(
                timeout=8,
                follow_redirects=False,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            ) as github,
            httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=False) as providers,
        ):
            backend = os.environ.get("PHINIX_STATE_BACKEND", "github")
            if backend == "supabase":
                from app.supabase_state import SupabaseState

                state = SupabaseState(providers)
                runtime = await state.request("GET", "phinix_runtime?id=eq.1&select=enabled")
                if runtime and runtime[0].get("enabled"):
                    raise CloudStateError("supabase_edge_owns_monitoring")
            elif backend == "github":
                state = GitHubState(github, repository)
            else:
                raise CloudStateError("invalid_state_backend")
            await state.load(path, initialize=os.environ.get("PHINIX_INITIALIZE") == "true")
            db = Database(path)
            db.initialize()
            service = MonitoringService(db, settings, providers)
            result = await run_cycle(service, state, send_test=os.environ.get("PHINIX_SEND_TEST") == "true")
            await publish_site(db, providers)
            if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                # Public workflow logs must never include private program scope or report details.
                public_summary = (
                    "# Phinix monitor\n\nCycle completed. Private checkpoint saved in Supabase.\n"
                )
                Path(summary).write_text(
                    public_summary if backend == "supabase" else result, encoding="utf-8"
                )
            print("Phinix cycle completed; private state and dashboard saved.")


def main():
    try:
        asyncio.run(execute())
    except Exception as exc:
        code = str(exc) if isinstance(exc, CloudStateError) else "cloud_run_failed"
        print(f"Phinix stopped safely: {code}. Existing remote state was retained.")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            Path(summary).write_text(
                f"# Phinix needs attention\n\nError: `{code}`. No state reset was attempted.\n",
                encoding="utf-8",
            )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
