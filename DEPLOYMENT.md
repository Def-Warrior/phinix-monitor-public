# Free Render + Supabase deployment

The active deployment uses [phinix-monitor.onrender.com](https://phinix-monitor.onrender.com), Supabase storage, and the existing private GitHub worker. The public repository contains reviewed source only; leave its polling workflow disabled. The private Sites mirror is also retained.

Render hosts one free Python web service in Singapore (0.1 CPU, 512 MB RAM), with no attached disk or paid database. Supabase persists the private monitoring checkpoint and current domain feed. GitHub Actions runs the hourly Intigriti poller independently of Render sleep cycles.

1. Create a Supabase Free project. Apply the SQL files in `supabase/migrations/` in filename order. Verify both tables have RLS enabled and no public access.
2. Save the project URL in `SUPABASE_URL` and a server secret key in `SUPABASE_SECRET_KEY`. Keep the key in GitHub Actions and Render secret settings, never in source or browser JavaScript.
3. Configure GitHub Actions secrets `SUPABASE_SECRET_KEY`, `INTIGRITI_TOKEN`, and `DISCORD_WEBHOOK_URL`. Add repository variable `SUPABASE_URL`. Keep `PHINIX_HOSTING_ENABLED` unset until initial state is ready.
4. For a fresh installation only, manually run the monitor with `initialize=true`. For an existing installation, pause the old writer, copy its latest validated checkpoint to Supabase, verify it, then run with `initialize=false`. Never initialize over missing historical state as a recovery shortcut.
5. Create one Render Python web service from the public source repository with plan `free`, region `singapore`, build command `pip install -r requirements.txt`, and start command `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log`.
6. Set Render variables `APP_ENV=production`, `SNAPSHOT_MODE=true`, `SUPABASE_SNAPSHOT=true`, `INTIGRITI_DOMAINS_ONLY=true`, `DATABASE_PATH=/tmp/phinix/placeholder.db`, `PYTHON_VERSION=3.12.14`, and `SUPABASE_URL`. Set secrets `ADMIN_TOKEN` (at least 32 random characters) and `SUPABASE_SECRET_KEY`. No Intigriti token or Discord webhook belongs on the viewer.
7. Verify `/health/ready` returns 200, anonymous `/api/domains` returns 401, and signing in shows the same domains and saved timestamp as Supabase. Set `PHINIX_HOSTING_ENABLED=true` on exactly one monitoring repository. Run a manual cycle, then verify a scheduled cycle.

The included `render.yaml` documents the same free service configuration; it creates no paid disk, database, cron job, or worker. Direct creation uses Render environment settings as its source of truth.

## Privacy and recovery

The public repository contains only source. Database snapshots, provider scope data, webhook URLs and tokens must not be committed or attached as public workflow artifacts. Public runner summaries contain only completion status. Existing private source/state history must remain private even after publishing a clean replacement.

Supabase stores one compressed, checksummed SQLite checkpoint and one JSON domain feed in PostgreSQL. A transactional compare-and-swap prevents concurrent writers from overwriting newer state. Initial baselines and sent-notification records migrate together, avoiding historical alerts. A failed save stops delivery. Discord remains at-least-once: a crash after Discord accepts an alert but before the final checkpoint can duplicate that alert.

The viewer keeps a verified private temporary cache during Supabase outages and displays a refresh warning. A new viewer instance requires a valid remote checkpoint. Corrupt or absent checkpoints are never treated as an empty monitor.

Keep an encrypted or otherwise access-restricted off-site checkpoint backup. Restore only with all writers paused, using a reviewed migration; do not overwrite a live checkpoint.

## Free-tier limits

Render sleeps after idle periods and has monthly free-service limits. The workspace's build pipeline spending limit is $0. With a payment method saved, outbound bandwidth beyond the included allowance can still be billed; the free compute plan is not an account-wide spending cap. See [Render's free-plan limits](https://render.com/docs/free#monthly-usage-limits). Supabase Free has database and egress limits and may pause inactive projects. GitHub schedules can be delayed or disabled after long repository inactivity. Free plans do not guarantee uninterrupted service. Keep compute on Free and any GitHub paid-usage budget at zero; do not enable paid resources or automatic upgrades.
