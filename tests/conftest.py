import pytest

from app.config import Settings
from app.models import Event
from app.storage import Database

WEBHOOK = "https://discord.com/api/webhooks/123456789/test_token_not_real"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        app_env="test",
        database_path=tmp_path / "monitor.db",
        admin_token="test-admin-token-with-at-least-32-characters",
        discord_webhook_url=WEBHOOK,
    )


@pytest.fixture
def db(settings):
    store = Database(settings.database_path)
    store.initialize()
    store.ensure_collection("hackerone", "hacktivity")
    return store


@pytest.fixture
def event():
    return Event(
        provider="hackerone",
        collection="hacktivity",
        external_id="1",
        title="Example issue",
        url="https://hackerone.com/reports/1",
        program="example",
        severity="high",
        state="new",
        occurred_at="2026-11-12T12:00:00Z",
    )


def h1_record(identity="1"):
    return {
        "id": identity,
        "type": "report",
        "attributes": {
            "title": "Example issue",
            "severity_rating": "high",
            "latest_disclosable_activity_at": "2026-11-12T12:00:00Z",
            "substate": "resolved",
        },
        "relationships": {"program": {"data": {"attributes": {"handle": "example"}}}},
    }
