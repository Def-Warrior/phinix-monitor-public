import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.discord import Dispatcher, build_message
from app.main import create_app


@pytest.mark.parametrize(
    "peer,host,expected",
    [
        ("127.0.0.1", "127.0.0.1", 200),
        ("::1", "localhost", 200),
        ("192.168.1.20", "localhost", 403),
        ("127.0.0.1", "evil.example", 403),
    ],
)
def test_local_only_rejects_other_devices_and_foreign_hosts(settings, peer, host, expected):
    settings.local_only_access = True
    settings.discord_webhook_url = type(settings.discord_webhook_url)("")
    with TestClient(create_app(settings), base_url=f"http://{host}", client=(peer, 12345)) as client:
        response = client.get("/", headers={"x-forwarded-for": "127.0.0.1"})
        assert response.status_code == expected
        if expected == 403:
            assert response.json()["detail"] == "Access restricted to this device"


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "production"},
        {"hackerone_username": "name"},
        {"intigriti_enabled": True},
        {"discord_webhook_url": "https://evil.test/hook"},
        {"bugcrowd_token": "Bearer wrong"},
        {"hackerone_collections": "anything"},
    ],
)
def test_invalid_configuration_fails_without_secret_echo(overrides):
    with pytest.raises(ValueError) as exc:
        Settings(_env_file=None, **overrides)
    assert "input_value" not in str(exc.value)


def test_public_health_private_status_and_session(settings):
    settings.discord_webhook_url = type(settings.discord_webhook_url)("")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/status").status_code == 401
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        response = client.post("/api/login", json={"token": settings.admin_token.get_secret_value()})
        assert response.status_code == 200 and "HttpOnly" in response.headers["set-cookie"]
        status = client.get("/api/status")
        assert status.status_code == 200
        assert settings.admin_token.get_secret_value() not in status.text
        assert status.json()["integrations"][-1]["status"] == "disabled"
        assert client.post("/api/logout").status_code == 403
        assert client.post("/api/logout", headers={"origin": "http://testserver"}).status_code == 200
        assert client.get("/api/events").status_code == 401


def test_bearer_validation_security_headers_and_test_queue(settings):
    transport = httpx.MockTransport(lambda r: httpx.Response(204))
    with TestClient(create_app(settings, transport)) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token.get_secret_value()}
        assert client.get("/api/events?limit=101", headers=headers).status_code == 422
        response = client.get("/api/status", headers=headers)
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "webhooks/" not in response.text
        assert client.post("/api/notifications/test", headers=headers).status_code == 200
        assert client.post("/api/notifications/test", headers=headers).status_code == 429


def test_login_rate_limit_and_cross_origin(settings):
    settings.discord_webhook_url = type(settings.discord_webhook_url)("")
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/login", json={"token": "x"}, headers={"origin": "https://evil.test"}
            ).status_code
            == 403
        )
        for _ in range(10):
            assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"token": "wrong"}).status_code == 429


def test_database_failure_degrades_readiness(settings, monkeypatch):
    settings.discord_webhook_url = type(settings.discord_webhook_url)("")
    app = create_app(settings)
    with TestClient(app) as client:

        def failed():
            raise OSError("sensitive disk path")

        monkeypatch.setattr(app.state.db, "ping", failed)
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_no_admin_token_denies_status(settings):
    settings.admin_token = type(settings.admin_token)("")
    settings.discord_webhook_url = type(settings.discord_webhook_url)("")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/status").status_code == 503


def test_safe_discord_embed(event):
    payload = {
        "kind": "new",
        "event": event.model_copy(update={"title": "@everyone [click](https://evil.test)"}).model_dump(
            mode="json"
        ),
    }
    private = build_message(payload)
    assert "@everyone" not in json.dumps(private)
    assert "example" not in json.dumps(private)
    detailed = build_message(payload, True)
    assert detailed["allowed_mentions"]["parse"] == []
    assert "@everyone" not in detailed["embeds"][0]["title"]
    assert len(detailed["embeds"][0]["title"]) <= 240


@pytest.mark.parametrize(
    "status,state,error",
    [
        (204, "sent", None),
        (400, "failed", "webhook_rejected"),
        (302, "failed", "webhook_rejected"),
        (500, "pending", "upstream_unavailable"),
        (429, "pending", "rate_limited"),
    ],
)
async def test_discord_delivery_outcomes(db, settings, status, state, error):
    db.queue_test()

    def handler(request):
        assert request.url.host == "discord.com"
        assert request.url.params["wait"] == "true"
        assert json.loads(request.content)["allowed_mentions"]["parse"] == []
        return httpx.Response(status, headers={"Retry-After": "60"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Dispatcher(db, settings, client).once()
    recent = db.notification_status()["recent"][0]
    assert recent["state"] == state and recent["error_code"] == error


async def test_disabled_delivery_keeps_outbox(db, settings):
    db.queue_test()
    settings.discord_webhook_url = type(settings.discord_webhook_url)("")
    assert not await Dispatcher(db, settings, None).once()
    assert db.notification_status()["counts"]["pending"] == 1


async def test_network_error_retains_event_and_retries(db, settings, event):
    settings.notify_history = True
    db.commit_page("hackerone", "hacktivity", [event], None, settings)

    def handler(request):
        raise httpx.ConnectError("secret URL must not be logged")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await Dispatcher(db, settings, client).once()
    assert db.snapshot()["event_count"] == 1
    assert db.notification_status()["counts"]["pending"] == 1
