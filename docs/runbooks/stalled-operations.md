# Runbook: HamiCloudStalledAcceptedWork (`stalled-operations.md`)

- **Alert Name:** `HamiCloudStalledAcceptedWork`
- **Severity:** Critical
- **Trigger Expression:** `hamicloud_oldest_queued_job_age_seconds > 60 or hamicloud_outbox_pending_total > 50` for `1m`

## Diagnostic Steps

1. Inspect `/metrics` to determine whether work is stalled in the transactional outbox (`hamicloud_outbox_pending_total`) or in scheduler admission (`hamicloud_oldest_queued_job_age_seconds`):
   ```bash
   curl -s http://127.0.0.1:8000/metrics | grep -E "hamicloud_oldest_queued_job_age_seconds|hamicloud_outbox_pending_total|hamicloud_queued_jobs_total"
   ```
2. Check PostgreSQL for jobs stuck in `QUEUED` or `RECOVERY_PENDING` and check active workspace quota reservations:
   ```bash
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "SELECT id, workspace_id, state, created_at FROM jobs WHERE state IN ('QUEUED', 'RECOVERY_PENDING') ORDER BY created_at ASC LIMIT 20;"
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "SELECT workspace_id, resource_class, status, expires_at FROM quota_reservations WHERE status = 'ACTIVE';"
   ```
3. Verify NATS JetStream connectivity and Go Scheduler / Executor liveness.

## Mitigation Commands

1. Run an immediate Go Scheduler reconciliation pass to expire leaked leases/quota reservations and admit `QUEUED` jobs into `execution_intents`:
   ```bash
   ./bin/hamicloud-scheduler --run-once
   ```
2. Run an immediate Go Executor reconciliation pass to claim and execute admitted intents on Kubernetes:
   ```bash
   ./bin/hamicloud-executor --run-once
   ```

## Verification Criteria

- `hamicloud_oldest_queued_job_age_seconds` drops below `60` (or `0` when all eligible queued jobs are admitted).
- `hamicloud_outbox_pending_total` drains toward `0`.
- Previously stalled jobs transition from `QUEUED` to `ADMITTED` / `RUNNING` / `SUCCEEDED`.
