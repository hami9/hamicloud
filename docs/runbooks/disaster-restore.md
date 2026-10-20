# Runbook: HamiCloudBackupRestoreVerificationFailed (`disaster-restore.md`)

- **Alert Name:** `HamiCloudBackupRestoreVerificationFailed`
- **Severity:** Critical
- **Trigger Expression:** `hamicloud_backup_restore_last_verify_success == 0` for `1m`
- **Targets:** RTO $\le 60$ minutes, RPO $\le 15$ minutes

## Diagnostic Steps

1. Verify PostgreSQL container health and check available backup archives and artifact snapshots:
   ```bash
   docker exec hamicloud-postgres pg_isready -U hamicloud
   ```
2. Check whether the failure occurred during `pg_dump`, artifact directory archiving, `pg_restore`, or post-restore checksum/row-count reconciliation.

## Mitigation Commands

1. Capture a fresh consistent custom-format PostgreSQL dump (`pg_dump -Fc`) and archive job output artifacts:
   ```bash
   docker exec hamicloud-postgres pg_dump -U hamicloud -Fc -f /tmp/hamicloud.dump hamicloud
   docker cp hamicloud-postgres:/tmp/hamicloud.dump ./backups/hamicloud.dump
   ```
2. Restore into an isolated target database and verify schema version, tenant row counts, encrypted secrets, and artifact SHA-256 digests:
   ```bash
   docker exec hamicloud-postgres psql -U hamicloud -d postgres -c "DROP DATABASE IF EXISTS hamicloud_restore_drill; CREATE DATABASE hamicloud_restore_drill OWNER hamicloud;"
   docker exec hamicloud-postgres pg_restore -U hamicloud -d hamicloud_restore_drill /tmp/hamicloud.dump
   ```
3. Run the Go Scheduler and Executor in `--run-once` mode against the restored state to reconcile any in-flight `execution_intents` against live Kubernetes cluster resources.

## Verification Criteria

- `pg_restore` completes with exit code `0` within the 60-minute RTO target.
- Restored counts of `workspaces`, `applications`, `releases`, `jobs`, `secret_references`, and `audit_events` match the pre-backup snapshot 100%.
- Restored job artifacts match their pre-backup SHA-256 checksums.
