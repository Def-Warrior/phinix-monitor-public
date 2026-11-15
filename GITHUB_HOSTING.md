# GitHub Actions monitoring

The current `monitor.yml` stores private state in Supabase and needs only read permission on repository contents. See DEPLOYMENT.md for its secrets, initialization and single-writer migration sequence.

Scheduled runs use cron `57 * * * *` (UTC). GitHub may delay them. `PHINIX_HOSTING_ENABLED=true` enables the schedule; clearing it pauses scheduled polling. A manual workflow dispatch remains available to the repository owner. Never initialize missing state as a way to bypass a failed restore.

No private domains appear in the public job summary. Intigriti responses, credentials, webhook URLs and checkpoint files are not logged or uploaded as artifacts.

## Legacy private repository mode

`PHINIX_STATE_BACKEND=github` retains the old private state-branch backend for existing installations. That backend refuses public repositories and requires contents write permission. The included workflow intentionally defaults to Supabase and contents read. Do not make a repository public if it contains historical state branches, run summaries or database objects, even after deleting the visible branch. Preserve it privately and publish a fresh reviewed source copy.
