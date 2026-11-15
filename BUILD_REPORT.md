# Validation for Supabase / Render migration

Local checks: 185 tests passed; Ruff lint and format checks passed. New tests cover Supabase checkpoint round trips, conflicting writers, missing and corrupt state, recovery using the last good viewer snapshot, restricted API routes, and disabled provider/Discord workers on the viewer.

These automated tests use mock provider responses. Live deployment verification is recorded separately after provisioning; local tests alone do not establish working hosting or live token permissions.
