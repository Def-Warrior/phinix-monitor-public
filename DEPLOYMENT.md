# Free Render + Supabase deployment

The active deployment uses [phinix-monitor.onrender.com](https://phinix-monitor.onrender.com), Supabase storage, and a six-second Supabase Edge worker. Both GitHub polling workflows stay disabled; GitHub hosts source and validates changes. The legacy Sites mirror retains its last snapshot but is no longer the live dashboard.

Render hosts one free Python web service in Singapore (0.1 CPU, 512 MB RAM), with no attached disk or paid database. Supabase persists private normalized monitoring state and the domain feed. Cron invokes the Edge worker independently of Render sleep cycles. The old SQLite checkpoint is retained as a migration backup.

## Migration from the legacy GitHub worker

1. Use the existing Supabase Free project and checkpoint/feed tables. Apply unapplied SQL migrations in filename order. Verify all four monitoring tables have RLS enabled and no public access. These migrations extend the existing checkpoint installation; they are not a fresh database bootstrap.
2. Save the project URL in `SUPABASE_URL` and a server secret key in `SUPABASE_SECRET_KEY`. Keep the key in GitHub Actions and Render secret settings, never in source or browser JavaScript.
3. Configure GitHub Actions secrets `SUPABASE_SECRET_KEY`, `INTIGRITI_TOKEN`, and `DISCORD_WEBHOOK_URL`. Add repository variable `SUPABASE_URL`. Keep `PHINIX_HOSTING_ENABLED` unset until initial state is ready.
4. Keep Edge disabled while transferring the existing provider/webhook secrets into Vault through the private `migrate-edge-secrets.yml` workflow. Deploy `supabase/functions/phinix-poll` with its custom key authentication. Pause the old GitHub writer and wait for active runs to finish. Back up the latest validated checkpoint and import programs, jobs, additions and the outbox through `phinix_edge_rpc`'s `import` operation. Preserve identities, timestamps, baseline start time and delivery cooldown; compare all counts. Never initialize over missing historical state as a recovery shortcut.
5. Create one Render Python web service from the public source repository with plan `free`, region `singapore`, build command `pip install -r requirements.txt`, and start command `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --no-access-log`.
6. Set Render variables `APP_ENV=production`, `SNAPSHOT_MODE=true`, `SUPABASE_SNAPSHOT=true`, `INTIGRITI_DOMAINS_ONLY=true`, `DATABASE_PATH=/tmp/phinix/placeholder.db`, `PYTHON_VERSION=3.12.14`, and `SUPABASE_URL`. Set secrets `ADMIN_TOKEN` (at least 32 random characters) and `SUPABASE_SECRET_KEY`. No Intigriti token or Discord webhook belongs on the viewer.
7. Verify the Edge `mode=probe` request authenticates with Intigriti and anonymous calls return 401. Enable `phinix_runtime.enabled`; confirm the `phinix-poll-six-seconds` Cron job advances `last_poll`, releases its lease and receives successful HTTP responses. Verify `/health/ready` returns 200, anonymous `/api/domains` returns 401, and signing in shows the imported domains with monitoring mode `supabase_edge`. Keep `PHINIX_HOSTING_ENABLED=false` on the private repository and unset on the public repository.

The included `render.yaml` documents the same free service configuration; it creates no paid disk, database, cron job, or worker. Direct creation uses Render environment settings as its source of truth.

## Privacy and recovery

The public repository contains only source. Database snapshots, provider scope data, webhook URLs and tokens must not be committed or attached as public workflow artifacts. Public runner summaries contain only completion status. Existing private source/state history must remain private even after publishing a clean replacement.

Supabase stores normalized live state, runtime health and a JSON domain feed, plus the retained legacy checkpoint. A lease prevents concurrent Edge cycles; database transactions persist additions and their outbox records before delivery. Initial baselines and sent-notification records migrate together. Discord remains at-least-once: a crash after Discord accepts an alert but before its acknowledgement is saved can duplicate that alert.

The viewer keeps its private temporary cache during Supabase outages and displays a refresh warning. A new viewer instance requires valid runtime/feed state (or a legacy checkpoint before cutover). Missing state is never treated as an empty monitor. See [EDGE-MONITOR.md](EDGE-MONITOR.md) for limits and rollback requirements.

Keep an access-restricted backup of the normalized live tables. Restore only with all writers paused. The retained pre-migration checkpoint does not include later Edge changes and must not be used as a current recovery point.

## Free-tier limits

Render sleeps after idle periods and has monthly free-service limits. The workspace's build pipeline spending limit is $0. With a payment method saved, outbound bandwidth beyond the included allowance can still be billed; the free compute plan is not an account-wide spending cap. See [Render's free-plan limits](https://render.com/docs/free#monthly-usage-limits). Supabase Free has database and egress limits and may pause inactive projects. GitHub schedules can be delayed or disabled after long repository inactivity. Free plans do not guarantee uninterrupted service. Keep compute on Free and any GitHub paid-usage budget at zero; do not enable paid resources or automatic upgrades.
