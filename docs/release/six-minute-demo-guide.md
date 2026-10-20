# HamiCloud v1.0: Six-Minute End-to-End Demo Guide

This guide walks through a complete, reproducible 6-minute demonstration of HamiCloud `v1.0.0` on a local Windows/Linux workstation using Docker Compose and Kind (`desktop-control-plane`).

---

## Prerequisites

Ensure the local stack and Kind cluster are running:

```powershell
docker compose -f deploy/compose/docker-compose.yml up -d
kubectl cluster-info
```

To run the entire 6-minute verification flow automatically and capture raw evidence artifacts in `docs/evidence/demos/raw/m6-v1-release/`:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demos\m6_v1_release_verification.ps1
```

---

## Minute-by-Minute Walkthrough

### Minute 0–1: Workspace Provisioning, Quotas & Write-Only Secrets (M0, M4)
1. **Create Workspace:** `POST /v1/workspaces` creates a dedicated workspace and provisions the hardened Kubernetes namespace (`hc-ws-<slug>-<id8>`) with `PodSecurity: restricted:latest`, `ResourceQuota`, `LimitRange`, and default-deny `NetworkPolicy`.
2. **Enforce RBAC:** Verify that `admin`, `developer`, and `viewer` tokens obey the role matrix (`viewer` rejected with `403 Forbidden` on mutations; non-members rejected with `403 Forbidden`).
3. **Store Write-Only Secret:** `POST /v1/workspaces/{id}/secrets` encrypts secret values with AES-256-GCM envelope encryption. Subsequent `GET` requests return metadata only (`encrypted_data` and plaintext are never exposed).

### Minute 1–2: Rootless BuildKit Image Build & Digest Pinning (M1)
1. **Trigger Build:** `POST /v1/workspaces/{id}/builds` with an `Idempotency-Key` dispatches a `BUILD_ARTIFACT` operation via the transactional outbox to NATS JetStream.
2. **Register Immutable Artifact:** Rootless BuildKit builds the container image, pushes it to `localhost:5000`, and records the immutable `@sha256:...` digest in PostgreSQL.

### Minute 2–3: Stateless Service Deployment, Canary Rollout & Fast Rollback (M1, M3)
1. **Deploy Release v1:** `POST /v1/services/{id}/releases` reconciles a hardened Kubernetes `Deployment`, `Service`, and `Ingress`.
2. **Canary Rollout & Rollback:** Deploy a broken release (`v2`), observe readiness failure detection, and trigger `POST /v1/services/{id}/rollback` (completing in `~2.06 s`, well below the `p95 < 10 s` SLO).

### Minute 3–4: Finite Batch Job Dispatch, Quota Admission & Log Archival (M2)
1. **Quota Admission Check:** Submit a batch job exceeding workspace CPU/memory or `max_concurrent_jobs` quota and verify immediate `422 Unprocessable Entity` admission rejection with an audit event.
2. **Job Execution & S3 Archival:** Dispatch a valid `batch/v1.Job` (`POST /v1/jobs/{id}/run`), stream container logs with automatic secret redaction (`[REDACTED]`), and verify gzipped log archival in MinIO (`s3://hamicloud-logs/`).

### Minute 4–5: Fault Injection & Split-Brain Prevention (M3, M5)
1. **Mid-Operation Executor Kill:** Terminate the active executor mid-reconciliation (`SIGKILL`).
2. **Lease Expiry & Fencing Recovery:** Verify that the standby executor acquires the expired Redis lease with an incremented fencing token, adopts the existing Kubernetes resource, and completes the operation with **zero duplicate Kubernetes resources**.

### Minute 5–6: Operational Telemetry, SLO Verification & Disaster Restore (M5, M6)
1. **Scrape Prometheus Metrics:** Query `GET /metrics` and `GET /v1/workspaces/{id}/telemetry` to inspect latency histograms (`hamicloud_http_request_duration_seconds`), active operations, and workspace saturation ratios.
2. **Verify Helm Chart & Disaster Restore:** Validate the production Helm chart (`deploy/helm/hamicloud`) and confirm `< 1 s` PostgreSQL + MinIO backup/restore parity.
