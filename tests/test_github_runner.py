import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from app.github_runner import BRANCH, CloudStateError, GitHubState, dashboard, execute, main, run_cycle
from app.service import MonitoringService
from app.storage import Database


class GitRemote:
    """An in-memory Git object store with non-fast-forward rejection."""

    def __init__(self):
        self.private = True
        self.head = None
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.conflict = False

    @staticmethod
    def object_sha(kind, content):
        return hashlib.sha1(kind.encode() + b" " + str(len(content)).encode() + b"\0" + content).hexdigest()

    def handle(self, request):
        path = request.url.path.removeprefix("/repos/owner/repo")
        method = request.method
        data = json.loads(request.content) if request.content else None
        if path == "":
            return httpx.Response(200, json={"private": self.private})
        if method == "GET":
            if path == f"/git/ref/heads/{BRANCH}":
                return (
                    httpx.Response(200, json={"object": {"sha": self.head}})
                    if self.head
                    else httpx.Response(404)
                )
            identity = path.rsplit("/", 1)[1]
            if "/blobs/" in path:
                return httpx.Response(
                    200,
                    json={"encoding": "base64", "content": base64.b64encode(self.blobs[identity]).decode()},
                )
            if "/commits/" in path:
                return httpx.Response(200, json=self.commits[identity])
            if "/trees/" in path:
                return httpx.Response(200, json=self.trees[identity])
        if path == "/git/blobs":
            raw = base64.b64decode(data["content"])
            identity = self.object_sha("blob", raw)
            self.blobs[identity] = raw
        elif path == "/git/trees":
            identity = self.object_sha("tree", request.content)
            self.trees[identity] = {"tree": [dict(x, size=len(self.blobs[x["sha"]])) for x in data["tree"]]}
        elif path == "/git/commits":
            identity = self.object_sha("commit", request.content)
            self.commits[identity] = {"tree": {"sha": data["tree"]}, "parents": data["parents"]}
        elif path.startswith("/git/refs"):
            assert data.get("force") is not True
            parents = self.commits[data["sha"]]["parents"]
            if self.conflict or (self.head and self.head not in parents):
                return httpx.Response(422, json={"message": "conflict"})
            self.head = data["sha"]
            return httpx.Response(200, json={"object": {"sha": self.head}})
        else:
            raise AssertionError((method, path))
        return httpx.Response(201, json={"sha": identity})


@pytest.fixture
def remote():
    return GitRemote()


async def test_private_state_roundtrip_and_append_only_history(remote, db, settings, event, tmp_path):
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    db.queue_test()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
        state = GitHubState(client, "owner/repo")
        await state.load(tmp_path / "unused.db", initialize=True)
        await state.save(db, "# Private dashboard")
        first = remote.head
        await state.save(db, "# Updated dashboard")
        assert remote.commits[remote.head]["parents"] == [first]
        other = GitHubState(client, "owner/repo")
        restored = tmp_path / "restored.db"
        await other.load(restored)
        store = Database(restored)
        assert store.events()["items"][0]["title"] == event.title
        assert store.notification_status()["counts"]["pending"] == 1
        assert store.checkpoint("hackerone", "hacktivity")["baseline_done"] == 1


async def test_missing_state_never_silently_resets(remote, tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
        with pytest.raises(CloudStateError, match="manual_initialization"):
            await GitHubState(client, "owner/repo").load(tmp_path / "state.db")


async def test_public_repository_cannot_receive_state(remote, db, tmp_path):
    remote.private = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
        state = GitHubState(client, "owner/repo")
        with pytest.raises(CloudStateError, match="private_repository_required"):
            await state.load(tmp_path / "state.db", initialize=True)
        with pytest.raises(CloudStateError, match="not_checked"):
            await state.save(db, "private")
    assert not remote.blobs


async def test_concurrent_writer_is_not_overwritten(remote, db, tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
        state = GitHubState(client, "owner/repo")
        await state.load(tmp_path / "state.db", initialize=True)
        await state.save(db, "original")
        original = remote.head
        remote.conflict = True
        with pytest.raises(CloudStateError):
            await state.save(db, "conflicting")
        assert remote.head == original == state.head


@pytest.mark.parametrize("damage", ["corrupt", "large", "missing", "encoding", "tree", "sha"])
async def test_invalid_remote_state_fails_closed(remote, db, tmp_path, damage):
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
        state = GitHubState(client, "owner/repo")
        await state.load(tmp_path / "state.db", initialize=True)
        await state.save(db, "original")
        entries = remote.trees[remote.commits[remote.head]["tree"]["sha"]]["tree"]
        entry = next(x for x in entries if x["path"] == "phinix.db")
        if damage == "corrupt":
            remote.blobs[entry["sha"]] = b"SQLite format 3\0damaged"
        elif damage == "large":
            entry["size"] = 25_000_001
        elif damage == "missing":
            entries.remove(entry)
        elif damage == "tree":
            remote.trees[remote.commits[remote.head]["tree"]["sha"]]["truncated"] = True
        elif damage == "sha":
            entry["sha"] = "not-a-sha"
        handler = remote.handle

        def intercept(request):
            response = handler(request)
            if damage == "encoding" and "/git/blobs/" in request.url.path:
                return httpx.Response(200, json={"encoding": "utf-8", "content": "bad"})
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(intercept)) as broken:
            with pytest.raises(CloudStateError):
                await GitHubState(broken, "owner/repo").load(tmp_path / "broken.db")


async def test_state_size_limit_blocks_upload(remote, db, tmp_path, monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.handle)) as client:
        state = GitHubState(client, "owner/repo")
        await state.load(tmp_path / "state.db", initialize=True)
        monkeypatch.setattr("app.github_runner.MAX_STATE_BYTES", 1)
        with pytest.raises(CloudStateError, match="state_size_limit"):
            await state.save(db, "summary")
        assert not remote.blobs


@pytest.mark.parametrize(
    "response", [httpx.Response(500), httpx.Response(200, text="bad"), httpx.Response(200, json=[])]
)
async def test_github_errors_do_not_expose_remote_bodies(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(CloudStateError, match="github_state_"):
            await GitHubState(client, "owner/repo").request("GET")


def test_repository_path_injection_rejected():
    with pytest.raises(CloudStateError):
        GitHubState(None, "owner/repo?secret=bad")


class CaptureState:
    def __init__(self, fail_at=0):
        self.snapshots = []
        self.fail_at = fail_at

    async def save(self, db, summary):
        if len(self.snapshots) + 1 == self.fail_at:
            raise CloudStateError("github_state_unavailable")
        self.snapshots.append((db.notification_status()["counts"].copy(), summary))


async def test_outbox_durable_before_external_delivery(db, settings):
    state = CaptureState()

    def discord(request):
        assert state.snapshots[0][0]["pending"] == 1
        return httpx.Response(200, json={"id": "test"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(discord)) as client:
        result = await run_cycle(MonitoringService(db, settings, client), state, send_test=True)
    assert state.snapshots[-1][0]["sent"] == 1
    assert "Run completed" in result


async def test_failed_first_save_never_sends_discord(db, settings):
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: requests.append(r))) as client:
        with pytest.raises(CloudStateError):
            await run_cycle(MonitoringService(db, settings, client), CaptureState(fail_at=1), send_test=True)
    assert not requests


async def test_delivery_can_drain_more_than_twenty_alerts(db, settings, monkeypatch):
    service = MonitoringService(db, settings, None)
    state = CaptureState()
    delivered = 0

    async def once():
        nonlocal delivered
        assert state.snapshots, "Outbox must be durable before delivery"
        if delivered == 25:
            return False
        delivered += 1
        return True

    async def no_delay(seconds):
        pass

    service.dispatcher.once = once
    monkeypatch.setattr("app.github_runner.asyncio.sleep", no_delay)
    await run_cycle(service, state)
    assert delivered == 25
    assert len(state.snapshots) == 2


async def test_failed_final_save_keeps_durable_pending_outbox(db, settings):
    state = CaptureState(fail_at=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        with pytest.raises(CloudStateError):
            await run_cycle(MonitoringService(db, settings, client), state, send_test=True)
    assert state.snapshots[0][0]["pending"] == 1
    assert db.notification_status()["counts"]["sent"] == 1


async def test_time_limit_persists_partial_progress_and_releases_claims(db, settings, event):
    settings.hackerone_username = "user"
    settings.hackerone_token = "token"
    # Fixture assignments intentionally go through a fresh validated Settings instance.
    settings = type(settings)(**settings.model_dump())
    state = CaptureState()
    async with httpx.AsyncClient() as client:
        service = MonitoringService(db, settings, client)

        async def partial(adapter):
            db.commit_page("hackerone", "hacktivity", [event], 1, settings)
            await asyncio.Event().wait()

        async def slow_delivery():
            db.claim(service.dispatcher.owner)
            await asyncio.Event().wait()

        service.poll_once = partial
        service.dispatcher.once = slow_delivery
        result = await run_cycle(service, state, send_test=True, poll_seconds=0.02, delivery_seconds=0.02)
    assert db.events()["total"] == 1
    assert db.checkpoint("hackerone", "hacktivity")["cursor"] == 1
    assert db.notification_status()["counts"]["sending"] == 0
    assert "poll_time_limit" in result and "delivery_time_limit" in result


async def test_provider_exception_is_sanitized(db, settings):
    service = MonitoringService(db, settings, None)

    class Adapter:
        collections = []

    service.adapters = {"hackerone": Adapter()}

    async def broken(adapter):
        raise RuntimeError("secret-token-do-not-print")

    service.poll_once = broken
    result = await run_cycle(service, CaptureState())
    assert "provider_cycle_error" in result and "secret-token-do-not-print" not in result


def test_dashboard_escapes_untrusted_content(db, settings, event):
    event.title = "<script>alert(1)</script> ![leak](https://evil.test) @everyone"
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    result = dashboard(MonitoringService(db, settings, None), "completed")
    assert "<script>" not in result and "@everyone" not in result
    assert "&lt;script&gt;" in result and settings.discord_webhook_url.get_secret_value() not in result


async def test_untrusted_ref_fails_before_network(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/untrusted")
    with pytest.raises(CloudStateError, match="trusted_main"):
        await execute()


def test_entrypoint_sanitizes_exception(monkeypatch, tmp_path, capsys):
    async def broken():
        raise RuntimeError("do-not-show-secret")

    monkeypatch.setattr("app.github_runner.execute", broken)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.raises(SystemExit):
        main()
    assert "cloud_run_failed" in summary.read_text()
    assert "do-not-show-secret" not in capsys.readouterr().out


async def test_execute_restores_runs_and_writes_summary(remote, tmp_path, monkeypatch):
    for key, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_TOKEN": "fake",
        "GITHUB_REF": "refs/heads/main",
        "PHINIX_INITIALIZE": "true",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }.items():
        monkeypatch.setenv(key, value)
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        "app.github_runner.httpx.AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(remote.handle), **kwargs),
    )
    await execute()
    assert remote.head and "Run completed" in (tmp_path / "summary.md").read_text()
