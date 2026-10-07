# Runtime foundation — Step A

Approved scope: phase-0 audit and user request to proceed. Base: e2bf7be.

The production scanner already selects and sends holding alerts. This change adds
storage primitives only. It does not fix the live pre-send last_alert timestamp
yet: replacing that path is Step C. No scanner, scheduler, web, broker, or sender
integration is included here. Existing alert selection and strategy stay intact.

## Files and schema

- agent_runtime/{__init__,schemas,event_engine}.py: validated contracts and a
  transaction-owning repository (no network or strategy imports).
- database/models.py: four additive tables: agent_observations,
  agent_runtime_states, agent_events, agent_deliveries.
- alembic/versions/a41e7c9d2b60_agent_runtime_foundation.py: parent 7b21d9c4e6a0.
- config.py: runtime/delivery enabled flags (both false) and bounded retry/lease
  settings. No new dependencies.
- tests/test_agent_events.py: database, idempotence, race, restart and lease tests.

Observations have a caller-supplied stable ID and an episode/scan-sequence/revision
unique key. Account/holding IDs are historical references, without cascading FKs
to user-editable portfolio tables. Episode identity and scan sequence must be
provided by the future adapter, not inferred from ticker or wall time. Persisted
evidence is canonical strict JSON; unsupported objects and non-finite numbers are
rejected. Timestamps enter as aware UTC-compatible values and are stored as naive
UTC, matching existing DB conventions.

RuntimeState has an optimistic version and a last-processed observation cursor.
apply_observation takes an expected version and caller-classified events; it does
not classify signals itself. State update, event insertion and optional delivery
rows commit together. Unique episode/event-sequence keys and CAS protect races.
Identical observation retries return prior results; changed contents under the
same observation identity are rejected. Previously consumed/older observations
cannot overwrite current state. A lower data timestamp is rejected even if scan
sequence is higher. The caller must process stored observations in chronological
order; missing observations/episode lifecycle/gap reconciliation remain Step B.
Unavailable data updates quality/current status but preserves last valid status.

Events are immutable facts, not PENDING/HANDLED jobs. Only Delivery has execution
status (PENDING/SENDING/SENT/FAILED), attempts, next_attempt_at, a random lease
token, lease expiry and a bounded error code. Atomic conditional UPDATE claims a
row; late acknowledgments from an expired/replaced lease cannot complete it.
Expired final attempts become FAILED. Failure retries use capped exponential
backoff. No raw provider error/token is persisted. The same event/channel/route
has one delivery. Routes are opaque configuration keys, never tokens or URLs.

Runtime off performs no repository I/O. Runtime on/delivery off records facts
only, with no sendable jobs. Enabling delivery later does not backfill old shadow
observations. There is no network sender or autonomous retry loop in A. A future
sender must handle Telegram chunk partial success separately; leases cannot
provide exactly-once external delivery.

## Verification

Run compileall, new tests, then all tests in isolated SQLite with conftest's
credential/DB isolation. Verify old-to-new Alembic migration preserves legacy
rows, model/schema agreement including constraints/indexes, downgrade in a test
DB, and PostgreSQL offline SQL compilation. If local PostgreSQL is available,
run the same migration/transaction checks there; otherwise report that actual
PostgreSQL DDL/concurrency remains unverified before production rollout.

## Rollback and limitations

Flags off retain old behavior but startup still runs additive Alembic DDL.
Production rollback leaves new tables intact; destructive downgrade is test-only.
No production migration, deployment, real message, order, push or PR is part of A.
The known failed-alert suppression remains until integration in B/C. Finish with
files changed, commands/results, DB impact, remaining limitations; stop after A.
