# Runbook: HamiCloudJobRetryStorm (`retry-storms.md`)

- **Alert Name:** `HamiCloudJobRetryStorm`
- **Severity:** Warning
- **Trigger Expression:** `hamicloud_job_retry_ratio > 0.25 or hamicloud_dead_letter_total > 10` for `5m`

## Diagnostic Steps

1. Inspect `/metrics` and query PostgreSQL for the top failing jobs and failure reasons across workspaces:
   ```bash
   curl -s http://127.0.0.1:8000/metrics | grep -E "hamicloud_job_retry_ratio|hamicloud_dead_letter_total"
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "SELECT workspace_id, job_id, attempt_number, state, exit_code, failure_reason FROM job_attempts WHERE state = 'FAILED' ORDER BY updated_at DESC LIMIT 20;"
   ```
2. Inspect dead-letter records via `GET /v1/workspaces/{workspace_id}/dead-letter-records` or PostgreSQL:
   ```bash
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "SELECT id, workspace_id, job_id, final_failure_reason, total_attempts, failed_at FROM dead_letter_records ORDER BY failed_at DESC LIMIT 10;"
   ```
3. Check Kubernetes events in the workload namespace for image pull failures (`ErrImagePull`), OOMKills (`exit_code = 137`), or expired worker leases.

## Mitigation Commands

1. If a misconfigured tenant workload is generating repeated transient failures, cancel the offending active jobs in that workspace:
   ```bash
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "UPDATE jobs SET state = 'CANCEL_REQUESTED', updated_at = NOW() WHERE workspace_id = '<WORKSPACE_ID>' AND state IN ('QUEUED', 'ADMITTED', 'STARTING', 'RUNNING');"
   ./bin/hamicloud-executor --run-once
   ```
2. Once the underlying image or dependency issue is resolved, replay dead-lettered jobs selectively via `POST /v1/jobs/{job_id}/rerun` with a fresh `Idempotency-Key`.

## Verification Criteria

- Offending jobs transition cleanly to `CANCELLED` or `SUCCEEDED` after fix and rerun.
- Active `CONCURRENT_JOB` quota reservations for terminated jobs are marked `RELEASED`.
