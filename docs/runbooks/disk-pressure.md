# Runbook: HamiCloudPersistentDiskPressure (`disk-pressure.md`)

- **Alert Name:** `HamiCloudPersistentDiskPressure`
- **Severity:** Critical
- **Trigger Expression:** `kubelet_volume_stats_available_bytes / clamp_min(kubelet_volume_stats_capacity_bytes, 1) < 0.15` for `5m`

## Diagnostic Steps

1. Check disk usage across PostgreSQL, MinIO artifact storage, OCI registry (`hamicloud-registry`), and BuildKit cache:
   ```bash
   docker system df
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "SELECT pg_size_pretty(pg_database_size('hamicloud')) AS db_size;"
   ```
2. Inspect completed Kubernetes Job pods and superseded ReplicaSets in the workload namespace:
   ```bash
   kubectl get jobs,pods -n default
   ```

## Mitigation Commands

1. Purge published outbox events older than retention window and vacuum PostgreSQL tables:
   ```bash
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "DELETE FROM outbox_events WHERE status = 'PUBLISHED' AND published_at < NOW() - INTERVAL '7 days'; VACUUM ANALYZE outbox_events;"
   ```
2. Prune completed Kubernetes Job pods and unused BuildKit build cache:
   ```bash
   kubectl delete jobs -l app.kubernetes.io/managed-by=hamicloud-reconciler --field-selector status.successful=1
   docker exec hamicloud-buildkit buildctl prune --keep-duration 24h
   ```

## Verification Criteria

- Available volume capacity rises above `20%` (`kubelet_volume_stats_available_bytes / kubelet_volume_stats_capacity_bytes >= 0.20`).
- Kubernetes nodes report `DiskPressure=False` in `kubectl describe nodes`.
