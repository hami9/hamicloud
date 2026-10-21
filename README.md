# hamicloud

Single-tenant cloud runtime and container orchestration platform built with FastAPI, Go, Kubernetes, and React.

## Install

Start the local infrastructure stack (PostgreSQL 16, Redis 7, NATS JetStream, MinIO, Keycloak, OCI Registry, Rootless BuildKit) and verify your local Kubernetes cluster:

```powershell
docker compose -f deploy/compose/docker-compose.yml up -d
kubectl cluster-info
```

Install Python control-plane dependencies, run database migrations, and build the Go runtime engine:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e "apps/api[dev]"
alembic -c apps/api/alembic.ini upgrade head
cd runtime && go build ./...
```

Install and build the React operator console:

```powershell
cd apps/web
npm install
npm run build
```

Deploy to a production Kubernetes cluster with Helm:

```powershell
helm upgrade --install hamicloud ./deploy/helm/hamicloud `
  --namespace hamicloud-system `
  --create-namespace
```

## Usage

Start the FastAPI control plane on port `8000`:

```powershell
.\.venv\Scripts\uvicorn app.main:app --app-dir apps/api --host 0.0.0.0 --port 8000
```

Create an isolated workspace with hard CPU, memory, and concurrent job quotas:

```bash
curl -X POST http://localhost:8000/v1/workspaces \
  -H "Authorization: Bearer $OIDC_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Payments Core",
    "slug": "payments-core",
    "quota_cpu_millicores": 4000,
    "quota_memory_mb": 8192,
    "quota_storage_mb": 20480,
    "quota_max_concurrent_jobs": 10
  }'
```

Deploy a digest-pinned stateless HTTP service release:

```bash
curl -X POST http://localhost:8000/v1/services/$SERVICE_ID/releases \
  -H "Authorization: Bearer $OIDC_TOKEN" \
  -H "Idempotency-Key: rel-2026-001" \
  -H "Content-Type: application/json" \
  -d '{
    "artifact_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "replicas": 2,
    "cpu_millicores": 500,
    "memory_mb": 512,
    "port": 8080,
    "health_check_path": "/healthz",
    "rollout_strategy": "canary",
    "canary_weight": 20
  }'
```

Dispatch a finite batch job execution:

```bash
curl -X POST http://localhost:8000/v1/jobs/$JOB_ID/run \
  -H "Authorization: Bearer $OIDC_TOKEN" \
  -H "Idempotency-Key: job-run-2026-001"
```

Run the end-to-end v1.0 release verification suite:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demos\m6_v1_release_verification.ps1
```

## Architecture

- **Control Plane (`apps/api`):** Python 3.12, FastAPI, SQLAlchemy 2.0 (asyncpg), OIDC/Keycloak RBAC (`admin`, `developer`, `viewer`), AES-256-GCM envelope-encrypted secrets, transactional outbox, and Prometheus `/metrics`.
- **Runtime Engine (`runtime`):** Go 1.23 scheduler, NATS JetStream consumer, Redis lease fencing (`SET NX PX` + monotonic fencing tokens), Kubernetes reconciler (`client-go`), Rootless BuildKit builder, and S3/MinIO log archiver.
- **Operator Console (`apps/web`):** React 19 and Vite dashboard for workspaces, services, canary rollouts, one-click rollbacks, batch jobs, write-only secrets, audit trails, and quota telemetry.
- **Helm Chart (`deploy/helm/hamicloud`):** Production Kubernetes manifests enforcing `PodSecurity: restricted:latest`, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, and default-deny `NetworkPolicy`.
- **Runtime Health & Workload Security:** Workload pods default to non-root execution via `WORKLOAD_RUN_AS_NON_ROOT` in production while allowing unprivileged root demo containers in development. Scheduler and executor export HTTP `/healthz` (loop tick liveness) and `/readyz` (database ping & shutdown status) endpoints on `HEALTH_ADDR` (default `:8081`).

## Benchmarks

Measured against live PostgreSQL, Redis, NATS JetStream, MinIO, Rootless BuildKit, and Kubernetes (`desktop-control-plane`):

| Benchmark | Target SLO | Measured v1.0 (`docs/evidence/M5.md`) | Status |
|---|---|---|---|
| Control-plane read latency (`GET`) | `p95 < 150 ms` | `p95 = 114.50 ms` (`p50 = 31.28 ms`) | PASS |
| Control-plane mutation ack (`POST`) | `p95 < 400 ms` | `p95 = 256.92 ms` (`p50 = 73.21 ms`) | PASS |
| Scheduler admission latency | `p95 < 2000 ms` | `p95 = 345.29 ms` (`p50 = 279.09 ms`) | PASS |
| Warm K8s service rollout to `HEALTHY` | `< 30 s` | `12.28 s` | PASS |
| Warm K8s finite job to `SUCCEEDED` | `< 30 s` | `13.97 s` (cold pull `44.01 s`) | PASS |
| Executor crash recovery (`SIGKILL`) | `0` duplicate K8s resources | `0` duplicates (`15.81 s` recovery) | PASS |
| Disaster backup & restore (`pg_dump` + artifacts) | `RTO < 15 min` (`RPO = 0`) | `RTO = 18.68 s` (`RPO = 0.0 s`) | PASS |

## Documentation

- **Tenant Ecosystem:** [HamiKnowledge](https://github.com/hami9/HamiKnowledge) — multi-user RAG knowledge platform on HamiCloud
- **Release Artifacts:** [v1.0.0 Release Notes](docs/release/v1.0.0-release-notes.md) | [Security Review v1.0](docs/release/security-review-v1.0.md) | [Architecture Case Study](docs/release/case-study.md) | [6-Minute Demo Guide](docs/release/six-minute-demo-guide.md)
- **Milestone Evidence:** [M0](docs/evidence/M0.md) | [M1](docs/evidence/M1.md) | [M2](docs/evidence/M2.md) | [M3](docs/evidence/M3.md) | [M4](docs/evidence/M4.md) | [M5](docs/evidence/M5.md) | [M6](docs/evidence/M6.md)
- **Architecture Decision Records:** [ADR-0001](docs/adr/ADR-0001-responsibility-split.md) | [ADR-0002](docs/adr/ADR-0002-durable-state-and-transactional-outbox.md) | [ADR-0003](docs/adr/ADR-0003-delivery-semantics-and-idempotent-execution.md) | [ADR-0004](docs/adr/ADR-0004-trust-model-and-tenant-isolation.md) | [ADR-0005](docs/adr/ADR-0005-technology-stack-and-compatibility-matrix.md)
- **Operational Runbooks:** [API 5xx](docs/runbooks/api-5xx-errors.md) | [Stalled Operations](docs/runbooks/stalled-operations.md) | [Retry Storms](docs/runbooks/retry-storms.md) | [Disk Pressure](docs/runbooks/disk-pressure.md) | [Disaster Restore](docs/runbooks/disaster-restore.md)

## License

MIT
