import json
import sqlite3
import time

import pytest

from app.models import Event, matches_rules
from app.storage import Database


def test_migrations_are_versioned_and_idempotent(db):
    db.initialize()
    assert db.snapshot()["schema_version"] == 3
    assert db.ping()


def test_silent_baseline_spans_pages_and_survives_restart(db, settings, event):
    db.commit_page("hackerone", "hacktivity", [event], 2, settings)
    assert not db.checkpoint("hackerone", "hacktivity")["baseline_done"]
    reopened = Database(settings.database_path)
    reopened.commit_page(
        "hackerone", "hacktivity", [event.model_copy(update={"external_id": "2"})], None, settings
    )
    assert reopened.checkpoint("hackerone", "hacktivity")["baseline_done"]
    assert reopened.notification_status()["counts"]["pending"] == 0
    assert reopened.snapshot()["event_count"] == 2


def test_changes_are_deduplicated_but_reversions_alert(db, settings, event):
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    changed = event.model_copy(update={"state": "resolved"})
    for _ in range(2):
        db.commit_page("hackerone", "hacktivity", [changed], None, settings)
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    assert db.notification_status()["counts"]["pending"] == 2
    assert db.events()["items"][0]["revision"] == 3


def test_page_is_atomic_on_persistence_error(db, settings, event):
    invalid = event.model_copy(update={"collection": "reports"})
    with pytest.raises(ValueError):
        db.commit_page("hackerone", "hacktivity", [event, invalid], 2, settings)
    assert db.snapshot()["event_count"] == 0
    assert db.checkpoint("hackerone", "hacktivity")["cursor"] == 0


def test_collections_keep_separate_identity(db, settings, event):
    db.ensure_collection("hackerone", "reports")
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    db.commit_page(
        "hackerone", "reports", [event.model_copy(update={"collection": "reports"})], None, settings
    )
    assert db.snapshot()["event_count"] == 2


def test_filters_do_not_discard_events(db, settings, event):
    settings.notify_history = True
    settings.notify_severities = "critical"
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    assert db.snapshot()["event_count"] == 1
    assert db.notification_status()["counts"]["pending"] == 0


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("notify_providers", "BUGCROWD", False),
        ("notify_programs", "EXAMPLE", True),
        ("notify_categories", "engagement", False),
        ("notify_keywords", "issue,security", True),
        ("notify_keywords", "xss", False),
    ],
)
def test_notification_filters(settings, event, key, value, expected):
    setattr(settings, key, value)
    assert matches_rules(event, settings) == expected


def test_atomic_claim_lease_and_stale_owner(db):
    assert db.queue_test()
    first = db.claim("worker-a")
    assert first and not db.claim("worker-b")
    with db.connection(True) as conn:
        conn.execute("UPDATE outbox SET lease_until=?", (time.time() - 1,))
    second = db.claim("worker-b")
    assert second["id"] == first["id"]
    db.delivered(first["id"], "worker-a")
    assert db.notification_status()["counts"]["sending"] == 1
    db.delivered(second["id"], "worker-b")
    assert db.notification_status()["counts"]["sent"] == 1


def test_rate_limit_is_global_and_does_not_spend_failure_budget(db):
    db.queue_test()
    row = db.claim("a")
    db.delivery_failed(row, "a", "rate_limited", 90, 3, rate_limited=True)
    assert not db.claim("b")
    assert db.notification_status()["recent"][0]["failures"] == 0


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_delivery_retry_budget_is_initial_plus_n(db, retries):
    db.queue_test()
    for attempt in range(retries + 1):
        row = db.claim("a")
        assert row["attempts"] == attempt + 1
        db.delivery_failed(row, "a", "network_error", 0, retries)
    assert db.notification_status()["counts"]["failed"] == 1
    assert not db.claim("a")


def test_auth_alert_once_per_episode(db):
    db.provider_error("hackerone", "auth_error", 3600, True)
    db.provider_error("hackerone", "auth_error", 3600, True)
    assert db.notification_status()["counts"]["pending"] == 1
    db.provider_success("hackerone")
    db.provider_error("hackerone", "auth_error", 3600, True)
    assert db.notification_status()["counts"]["pending"] == 2


def test_search_and_pagination(db, settings, event):
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    assert db.events(query="EXAMPLE")["total"] == 1
    assert db.events(query="' OR 1=1 --")["total"] == 0
    assert db.events(provider="bugcrowd")["total"] == 0
    assert db.events(severity="high", offset=1)["items"] == []


def test_legacy_import_validates_before_writing_and_runs_once(db, settings, event, tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"format": "phinix-events-v1", "events": [event.model_dump(mode="json")]}))
    db.import_legacy(path, settings)
    db.import_legacy(path, settings)
    assert db.snapshot()["event_count"] == 1
    assert db.notification_status()["counts"]["pending"] == 0


def test_legacy_unknown_format_is_rejected(db, settings, tmp_path):
    path = tmp_path / "unknown.json"
    path.write_text('{"reports": []}')
    with pytest.raises(ValueError):
        db.import_legacy(path, settings)


def test_backup_is_consistent(db, settings, event, tmp_path):
    db.commit_page("hackerone", "hacktivity", [event], None, settings)
    path = tmp_path / "backup.db"
    db.backup(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://hackerone.com/reports/1",
        "https://evil.test/1",
        "https://hackerone.com.evil.test/1",
        "https://user@hackerone.com/1",
        "https://hackerone.com:444/1",
        "https://hackerone.com/1?token=x",
    ],
)
def test_untrusted_source_urls_rejected(event, url):
    with pytest.raises(ValueError):
        Event.model_validate({**event.model_dump(), "url": url})
