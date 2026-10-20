# Runbook: HamiCloudApiHigh5xxRate (`api-5xx-errors.md`)

- **Alert Name:** `HamiCloudApiHigh5xxRate`
- **Severity:** Critical
- **Trigger Expression:** `(hamicloud_http_requests_total{status_class="5xx"} / clamp_min(sum(hamicloud_http_requests_total), 1)) > 0.01` for `5m`

## Diagnostic Steps

1. Inspect the `/readyz` and `/metrics` endpoints on the Control Plane API to determine whether PostgreSQL or Redis connectivity is degraded:
   ```bash
   curl -i http://127.0.0.1:8000/readyz
   curl -s http://127.0.0.1:8000/metrics | grep hamicloud_http_requests_total
   ```
2. Check PostgreSQL and Redis container health and connection saturation:
   ```bash
   docker ps --filter "name=hamicloud-"
   docker exec hamicloud-postgres pg_isready -U hamicloud
   docker exec hamicloud-redis redis-cli ping
   ```
3. Correlate failing requests using the `X-Correlation-ID` and `X-Trace-ID` headers returned on `500` / `503` responses.

## Mitigation Commands

1. If PostgreSQL or Redis is unreachable or unhealthy, restart the affected backing service and verify schema migrations:
   ```bash
   docker compose -f deploy/compose/docker-compose.yml up -d postgres redis
   alembic -c migrations/alembic.ini upgrade head
   ```
2. If PostgreSQL connection pools are exhausted due to stalled connections, terminate idle-in-transaction sessions older than 60 seconds:
   ```bash
   docker exec hamicloud-postgres psql -U hamicloud -d hamicloud -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle in transaction' AND state_change < NOW() - INTERVAL '60 seconds';"
   ```

## Verification Criteria

- `GET /readyz` returns HTTP `200 OK` with `{"ready": true, "database": true, "redis": true}`.
- Subsequent API requests return `2xx` responses and `hamicloud_http_requests_total{status_class="5xx"}` stops incrementing.
