# Phinix — Intigriti domain monitor

A private dashboard for Intigriti testing domains, with new scope additions first and durable Discord delivery. It reads official, authorized APIs; it does not contact the listed targets or submit reports. The source can be public: monitoring data and credentials stay private in Supabase and hosting secret settings.

## Free hosting

The active Render website is [phinix-monitor.onrender.com](https://phinix-monitor.onrender.com), using the Free compute plan (0.1 CPU, 512 MB RAM) and a private Phinix admin token for sign-in from any device. Supabase stores the monitoring checkpoint and domain feed. The existing private GitHub repository runs the hourly Intigriti worker and sends confirmed new-domain alerts to Discord; this public repository distributes reviewed source and does not run a second worker. The private Sites mirror remains available independently with account sign-in.

Render runs in snapshot mode: it never polls providers or sends Discord messages. Its temporary cache reloads from Supabase after restarts and checks for updates every five minutes while signed-in requests are active. The browser refreshes every 15 seconds. Free Render services sleep when idle and may take about a minute to wake; GitHub polling continues independently.

Both Supabase tables enable row-level security and deny `anon` and `authenticated` access. Only server credentials can access them. A database transaction saves each checkpoint and domain feed together, checking the previous revision to reject competing writers. Data is saved before alerts are sent. Missing or corrupt state stops the worker without silently rebuilding its baseline.

Use [DEPLOYMENT.md](DEPLOYMENT.md) for setup. Never publish an old repository containing a `phinix-state` branch or private run summaries. Export only reviewed source into a fresh repository, and keep the original private history private.

## Current deployment: domains only

- `INTIGRITI_DOMAINS_ONLY=true` focuses the deployed workflow and dashboard on Intigriti. HackerOne and Bugcrowd workers are disabled in this mode.
- For each domain-scope activity, Phinix fetches the exact previous and next scope versions. A domain is new only when it enters the in-scope domain set. Reward changes, description edits, status changes, and new paths on an existing hostname do not produce domain-added alerts.
- Current domains from recently updated programs are loaded silently as **Existing scope**. Confirmed additions appear above that baseline, ordered by the provider's change timestamp. This is not a complete catalog of every Intigriti program.
- URL and wildcard domains are included. IP ranges, mobile apps, other asset types, closed programs, and out-of-scope assets are excluded. No Bounty assets remain eligible. Exact listed URL/path constraints and a scope reference remain visible.
- Discord messages name the added domain. Generic program/activity messages and connection-test messages are suppressed. Queued old messages are retained as suppressed audit records. Previously sent Discord messages are not removed.
- Initial recent history is silent. Only scope activities created after this mode's first successful poll can notify. Repeated versions do not re-alert; a genuine removal and later re-addition can alert again. Unavailable previous scope data is retried, never treated as an empty scope.

The default input window is the latest 50 program activities, and each run processes up to 12 persistent scope jobs within the hosted time budget. Bursts exceeding the activity window between polls can be missed. Pending comparisons resume on later runs; recently tracked programs are also rechecked incrementally. Removed domains disappear once checked. The dashboard shows the last check time and retains the provider's scope link.

## Optional Sites mirror

An optional Sites mirror uses account sign-in and refreshes its domain feed every 30 seconds while open. After each successful cycle, the GitHub worker can send a minimal domain snapshot to that private website's persistent database. It is independent of the Render viewer and does not require the computer to stay on.

`PHINIX_SITE_URL` is a repository variable for the approved HTTPS Sites origin. `PHINIX_SITE_TOKEN` is an encrypted repository secret for that private Site's service access. Neither Intigriti credentials nor the Discord webhook is copied into the website. If both settings are absent, monitoring continues without website publication. A failed website upload fails the run visibly after monitoring state has already been saved, so the next run can retry without resetting scope history.

## Underlying monitor capabilities

The general adapters below remain in the codebase. They are not enabled in the current Intigriti-only deployment.

- HackerOne Hacktivity and your own reports, using the Hacker API.
- Bugcrowd engagements and your submissions, using the **Hacker API v1.0.0**, not the customer API.
- Per-collection page checkpoints, silent first-sync baselines, content revisions, and deduplicated notifications.
- Provider-wide authentication/rate-limit cooldowns, bounded retries, supervised workers, and safe readiness reporting.
- Persistent Discord outbox with atomic leases, retry scheduling, mention suppression, and generic alerts by default.
- Admin login, searchable/filterable event history, integration health, delivery history, and a connection-test button.
- Hourly GitHub Actions hosting with a private dashboard and durable state, plus Docker, Docker Compose, optional Render hosting, backups, and normalized JSON export.
- Intigriti programs and recent program activities, using the official Researcher API v1.

## Run locally

Python 3.12 is required. From this project directory:

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app init
```

Edit the newly created `.env` locally. An admin token is generated for you. Add the API credentials and Discord webhook there, then:

```sh
python -m app serve
```

Open <http://127.0.0.1:8000> and enter `ADMIN_TOKEN` from your `.env`. The server starts safely with all integrations disabled if no provider credentials are set. This is an empty, live workspace, not a simulated provider feed.

`python -m app serve` binds only to this device, disables proxy-header trust, and returns HTTP 403 for a non-loopback peer or an unexpected hostname. Admin login is still required. Other devices cannot connect to its loopback listener. GitHub repository access is account-based, not locked to this computer; your account can open the private repository from another signed-in device.

The installed local launcher uses `SNAPSHOT_MODE=true` to display the hosted monitor's saved status, including enabled providers and Discord delivery, without starting duplicate workers. It checks for a newer private snapshot every five minutes. The UI shows when cloud data was saved and warns when it is over three hours old or a refresh fails. GitHub remains the hourly poller. The standalone `python -m app serve` command runs a normal local monitor unless snapshot mode is configured.

Event links open provider web pages. Intigriti activity links resolve to their saved program page; if that program is unavailable, the link opens the Intigriti dashboard. Bugcrowd API-only records link to its dashboard. Provider APIs require separate credentials even when you are signed into their websites; opening an API URL directly can return 401. Phinix never puts API tokens in browser links.

## Credentials and Discord

`HACKERONE_USERNAME` and `HACKERONE_TOKEN` use HTTP Basic authentication against `api.hackerone.com`. Select authorized collections with `HACKERONE_COLLECTIONS=hacktivity,reports`.

`BUGCROWD_TOKEN` is the `ID:secret` pair **without** the `Token ` prefix. It needs `read:engagement_list` and/or `read:submissions` for the selected `BUGCROWD_COLLECTIONS`. It never needs write scopes. The adapter uses `hacker-api.bugcrowd.com` and the documented offset/limit envelope.

Set `INTIGRITI_TOKEN` to your Researcher API personal access token and `INTIGRITI_ENABLED=true`. The GitHub workflow enables it automatically when the repository secret exists. `INTIGRITI_COLLECTIONS=programs,activities` fetches program summaries and the most recent 50 activities on each scan. It never requests payouts. Tokens expire; replace the secret with a renewed token before expiry.

`INTIGRITI_ACTIVITY_WINDOW` can increase the recent window to at most 500. This is not a full historical activity import; more than that many new activities between completed scans can be missed. Intigriti supplies no activity ID, so Phinix hashes the program ID, type ID, creation timestamp, and canonical activity content. Exact source duplicates coalesce. Bounty and program metadata changes create program revisions; changing your following preference does not.

In Discord, choose the intended server, create/select the text channel `phinix`, and open **Edit Channel → Integrations → Webhooks**. Create an incoming webhook, ensure its channel is `#phinix`, and put its URL in `DISCORD_WEBHOOK_URL` through your local `.env` or hosting secret settings. A channel name alone cannot route a webhook. `DISCORD_CHANNEL_NAME` is only a dashboard label.

Restart/redeploy after environment changes. Sign in to Phinix, click **Send connection test**, and verify that the row becomes `sent` and the message appears in the correct channel. Webhook secrets are never exposed through the dashboard or committed to Git.

`DISCORD_INCLUDE_DETAILS=false` sends a generic notification. Set it to `true` only if everyone who can access the destination channel may see the report titles, programs, states, severity, and source links. No proof of concept, full report body, or attachment is stored or sent by this implementation.

That setting describes the optional general monitor. Domain-only mode instead sends the requested domain names and scope references, and hides the connection-test control.

## Notification rules

Use comma-separated `NOTIFY_PROVIDERS`, `NOTIFY_PROGRAMS`, `NOTIFY_CATEGORIES`, `NOTIFY_SEVERITIES`, and `NOTIFY_KEYWORDS`. Non-empty groups are combined with AND; values within a group use OR. Exact matching is case-insensitive except keywords, which match substrings of title/program/category. Categories are `report`, `engagement`, `submission`, `program`, or `program_activity`. Intigriti activity program filters use the source program UUID; its program summaries use the handle. Rules affect new outbox entries, not stored history or messages already queued.

First sync is silent until **all pages of that collection** have completed. Set `NOTIFY_HISTORY=true` before startup only if you explicitly want existing records to produce alerts. Configuring a webhook later does not automatically notify every stored record.

## Hosting

**Free GitHub mode:** the [hourly monitoring workflow](.github/workflows/monitor.yml) runs the providers and Discord dispatcher without an always-on server. Open the [private dashboard](https://github.com/Def-Warrior/phinix-monitor/tree/phinix-state) for its latest snapshot or [Actions](https://github.com/Def-Warrior/phinix-monitor/actions/workflows/monitor.yml) for run history. Setup and limits are in [GITHUB_HOSTING.md](GITHUB_HOSTING.md). Preserve a $0 Actions spending cap; jobs stop rather than incur overage charges when the included allowance is exhausted.

GitHub Pages cannot execute this Python backend. The GitHub-hosted dashboard is a private repository page updated after each run; the interactive FastAPI dashboard remains available locally. A continuously available interactive web server requires Render or another suitable host. Do not publish report data or credentials to Pages.

The optional `render.yaml` describes one paid web service and a 1 GB persistent disk. It is not used by the free GitHub mode. See [DEPLOYMENT.md](DEPLOYMENT.md) if choosing that option later.

Alternatively, with `.env` configured: `docker compose up --build -d`. Compose binds only to localhost. For internet access, add an HTTPS reverse proxy and set `APP_ENV=production`. Production requires an admin token of at least 32 characters and uses secure session cookies.

## Persistence and reliability

Run **one process / one instance / one Uvicorn worker**. A local lifetime file lock rejects a second process using the same database. SQLite migrations use a write transaction and `user_version`. A page’s events, revisions, notification rows, and next checkpoint commit in one transaction. Invalid pages leave the checkpoint unchanged; valid sibling collections can continue.

`MAX_RETRIES=N` means one initial attempt plus N retries for transient provider requests and Discord failures. Discord 429 responses defer delivery without exhausting the transient-error budget. Cooldowns survive restart. Provider 401/403 pauses all of that provider’s collections, raises one operational alert per episode, and retries after `AUTH_COOLDOWN_SECONDS`.

Pagination resumes after the configured page budget, then starts another full scan after reaching the end. These APIs use mutable page/offset lists rather than immutable snapshots, so changes during a scan can shift page boundaries. Repeated complete scans converge for records still present; transient records removed between polls cannot be guaranteed. There is no invented server-side watermark guarantee.

Discord delivery is **at least once**. A crash between remote acceptance and local success can duplicate a message. Outbox rows have leases; a crashed sender’s claim becomes available after expiry. Failed permanent deliveries remain visible. After fixing a webhook, new events can deliver; failed rows are retained for review and are not silently re-sent.

## Health and API

| Route | Access | Purpose |
|---|---|---|
| `/health`, `/health/live` | Public | Minimal process liveness |
| `/health/ready` | Public | Minimal readiness, HTTP 503 on storage/worker failure |
| `/` | Public shell | Login page; no private data before login |
| `/api/login`, `/api/logout` | Token / session | Eight-hour HttpOnly, same-site session |
| `/api/status`, `/api/monitoring` | Admin | Runtime and collection status |
| `/api/integrations` | Admin | Configuration presence, no credentials |
| `/api/events` | Admin | Paginated event search |
| `/api/notifications/status` | Admin | Queue counts and sanitized delivery history |
| `/api/notifications/test` | Admin POST | Rate-limited connection test |

API clients can use `Authorization: Bearer <ADMIN_TOKEN>`. Browser writes require the same Origin as the application. Never put a token in a URL. Integration credential failures are visible degradation rather than a dead process. No integrations configured is a ready service with disabled providers, not proof of working live integrations.

## Backups and legacy state

```sh
python -m app backup backups/phinix-backup.db
python -m app export backups/events.json
```

Backups use SQLite’s online backup API. Store backups securely off the service disk and test restore periodically. To restore, stop the service, replace its database with a backup, remove stale WAL/SHM sidecars only while stopped, and start the same or newer application version. Never restore over a running database.

No earlier source archive or legacy data format was supplied. Automatic import accepts only the explicit `phinix-events-v1` export format; set `LEGACY_STATE_PATH` to that JSON file. It validates everything before writing, deduplicates, imports silently, and records a one-time marker. Unrecognized files fail startup rather than guessing their schema.

## Development and checks

```sh
python -m pip install -r requirements-dev.lock
python -m ruff check .
python -m ruff format --check .
python -m pytest -q --cov=app --cov-fail-under=85
```

The tests use mocked HTTP responses derived from the public contracts. They do not certify live account permissions or live delivery. See [BUILD_REPORT.md](BUILD_REPORT.md) for the checks actually run on this build, and [docs/API_SOURCES.md](docs/API_SOURCES.md) for official references.
