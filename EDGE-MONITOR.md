# Six-second Intigriti monitoring

Data path: Intigriti Researcher API → Supabase Edge Function → private Postgres state/outbox → Discord. Render reads the private domain feed. GitHub tokens are not used to fetch Intigriti domains.

## Operation

- `phinix_runtime.enabled` is the master switch. Cron skips disabled or leased cycles.
- Cron invokes `phinix-poll` every six seconds using a scoped key stored in Vault. The Edge Function validates it before using provider credentials. `verify_jwt=false` is intentional: authentication is implemented inside the function.
- `phinix_live_state` preserves program state, comparison jobs, domain additions and the delivery outbox. No browser can read these tables directly.
- `/functions/v1/phinix-poll?mode=probe` with the cron key performs an authenticated Intigriti read without changing monitoring state or sending messages.
- The Render viewer checks small runtime metadata every two seconds and loads the domain feed only after its revision changes. A heartbeat older than 60 seconds appears stale.
- The private GitHub monitor must stay paused. Its runner also refuses to poll while Edge monitoring is enabled. To roll back, disable Edge first and export its current state; never resume the old checkpoint without reconciling all new jobs and delivered alerts.
- Scheduler log retention is one day for these two jobs only. Domain and delivery history is retained.

## Capacity and delivery semantics

Six-second invocation cadence uses up to 446,400 invocations in a 31-day month, below the current 500,000 Free allowance before other functions and manual tests. Provider, bandwidth, database size and other project quotas still apply. No paid plan or paid worker is required by this design.

Healthy no-change cycles fetch one recent-activities page. New jobs compare the exact scope versions and current allowed domains. Two jobs run concurrently, up to four per cycle. Provider 401/429 responses cause cooldowns. Unknown scope history never becomes a fabricated addition.

Discord requests contain up to five domain embeds with mentions disabled. The outbox honors rate limits and bounded retries. Sent entries are not requeued. As with any external webhook, a lost response after Discord accepts a message can cause an ambiguous retry; exactly-once delivery cannot be guaranteed.

Under-ten-second delivery is a target from API visibility, not a guarantee from program publication. Long comparisons, more than four queued jobs, rate limits, service pauses and API delays can exceed it. The latest 50 activities are fetched; a larger burst during an outage can exceed that window. Monitor `last_poll`, `status`, `last_duration_ms`, `last_sent_at`, and pending/error counts.

`last_delivery_latency_ms` measures outbox creation to Discord acknowledgement, not original Intigriti publication time. Do not report it as publication-to-delivery latency.

`last_source_delivery_ms` measures the provider activity timestamp to Discord acknowledgement. `last_api_seen_delivery_ms` measures the worker's first observation of that activity to acknowledgement, including queued comparison work. These are recorded only for real domain alerts; a diagnostic message does not populate them. The dashboard displays these measurements after a real delivery.

Multiple status checks for the same program are consolidated; older status checks remain as superseded audit records. Version comparisons are preserved independently. Program-level 403 responses appear as distinct restricted programs, with hourly access retries, rather than multiple general processing failures. Access must be granted by Intigriti/the program owner; the worker does not bypass it.

## Validation

Run `pytest -q --cov=app --cov-fail-under=85` and `node --test tests/edge.test.mjs`. Production checks should confirm anonymous rejection, authenticated provider reads, advancing heartbeat, released leases, preserved baseline/sent counts, and actual Discord acknowledgements when new domains occur. Avoid fake domain alerts and repeated connection-test messages.
