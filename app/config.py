import re
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: str = "development"
    local_only_access: bool = False
    snapshot_mode: bool = False
    supabase_snapshot: bool = False
    snapshot_manifest_path: Path | None = None
    database_path: Path = Path("data/phinix.db")
    admin_token: SecretStr = SecretStr("")
    hackerone_username: SecretStr = SecretStr("")
    hackerone_token: SecretStr = SecretStr("")
    hackerone_collections: str = "hacktivity,reports"
    bugcrowd_token: SecretStr = SecretStr("")
    bugcrowd_collections: str = "engagements,submissions"
    intigriti_token: SecretStr = SecretStr("")
    intigriti_collections: str = "programs,activities"
    # Rolling recent activity window, not an unbounded historical import.
    intigriti_activity_window: int = Field(50, ge=1, le=500)
    intigriti_domains_only: bool = False
    intigriti_scope_jobs_per_cycle: int = Field(12, ge=1, le=30)
    discord_webhook_url: SecretStr = SecretStr("")
    discord_channel_name: str = "phinix"
    # Private report contents are kept out of Discord by default.
    discord_include_details: bool = False
    poll_interval_seconds: int = Field(300, ge=30, le=86400)
    page_size: int = Field(50, ge=1, le=100)
    pages_per_cycle: int = Field(3, ge=1, le=100)
    max_retries: int = Field(3, ge=0, le=10)
    http_timeout_seconds: float = Field(20, ge=1, le=60)
    auth_cooldown_seconds: int = Field(3600, ge=60, le=86400)
    notify_history: bool = False
    notify_providers: str = ""
    notify_programs: str = ""
    notify_categories: str = ""
    notify_severities: str = ""
    notify_keywords: str = ""
    legacy_state_path: Path | None = None
    intigriti_enabled: bool = False

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.app_env not in {"development", "production", "test"}:
            raise ValueError("Unsupported APP_ENV")
        if self.supabase_snapshot and not self.snapshot_mode:
            raise ValueError("Supabase web access requires SNAPSHOT_MODE")
        if self.app_env == "production" and len(self.admin_token.get_secret_value()) < 32:
            raise ValueError("Production requires an ADMIN_TOKEN of at least 32 characters")
        if bool(self.hackerone_username.get_secret_value()) != bool(self.hackerone_token.get_secret_value()):
            raise ValueError("Both HackerOne credentials are required")
        token = self.bugcrowd_token.get_secret_value()
        if token and not re.fullmatch(r"[^\s:]+:[^\s:]+", token):
            raise ValueError("BUGCROWD_TOKEN must contain the ID:secret pair without the Token prefix")
        webhook = self.discord_webhook_url.get_secret_value()
        if webhook and not re.fullmatch(
            r"https://discord\.com/api(?:/v10)?/webhooks/[0-9]+/[A-Za-z0-9_-]+", webhook
        ):
            raise ValueError("Use an HTTPS discord.com incoming webhook without query parameters")
        if self.intigriti_enabled and not self.intigriti_token.get_secret_value():
            raise ValueError("Intigriti requires INTIGRITI_TOKEN when enabled")
        for name, allowed in [
            ("hackerone", {"hacktivity", "reports"}),
            ("bugcrowd", {"engagements", "submissions"}),
            ("intigriti", {"programs", "activities"}),
        ]:
            selected = self.collections(name)
            if not selected or not set(selected) <= allowed:
                raise ValueError("Invalid provider collection selection")
        for value in [
            self.hackerone_username,
            self.hackerone_token,
            self.bugcrowd_token,
            self.intigriti_token,
            self.admin_token,
        ]:
            if any(c in value.get_secret_value() for c in "\r\n\x00"):
                raise ValueError("Credentials cannot contain control characters")
        return self

    def collections(self, provider: str) -> list[str]:
        return list(
            dict.fromkeys(x.strip() for x in getattr(self, f"{provider}_collections").split(",") if x.strip())
        )

    def enabled(self, provider: str) -> bool:
        if self.intigriti_domains_only and provider != "intigriti":
            return False
        return (
            not self.snapshot_mode
            and bool(getattr(self, f"{provider}_token").get_secret_value())
            and (provider != "intigriti" or self.intigriti_enabled)
        )

    @property
    def delivery_enabled(self) -> bool:
        return not self.snapshot_mode and bool(self.discord_webhook_url.get_secret_value())
