# HamiCloud v1.0 Case Study: Designing a High-Reliability Single-Tenant Cloud Runtime

**Author:** HamiCloud Engineering  
**Version:** `1.0.0` (2026-10-20)

---

## 1. Problem Statement & System Scope

Modern platform engineering teams need an internal PaaS layer that abstracts raw Kubernetes complexity while preserving strict multi-workspace isolation, deterministic rollbacks, finite batch job scheduling, and verifiable operational SLOs. HamiCloud was architected as a single-tenant cloud runtime and orchestration platform separating high-level product workflows (Python/FastAPI Control Plane + Next.js Console) from low-level cluster reconciliation (Go Runtime Engine + Kubernetes + BuildKit).

---

## 2. Key Architectural Trade-Offs (ADR Summary)

1. **Split Control Plane (Python/FastAPI) and Runtime Engine (Go) ([ADR-0001](../adr/ADR-0001-responsibility-split.md)):**
   - *Decision:* Use Python 3.12 / FastAPI + SQLAlchemy 2.0 for the synchronous REST API, RBAC, quota accounting, and audit trail, while writing the asynchronous scheduler, outbox relay, and Kubernetes reconciler in Go 1.23 (`client-go`).
   - *Outcome:* Fast schema iteration and rich OpenAPI documentation in the API layer, paired with native concurrency, static typing, and first-class Kubernetes client ergonomics in the runtime layer.

2. **Transactional Outbox over Direct Broker Publish ([ADR-0002](../adr/ADR-0002-durable-state-and-transactional-outbox.md)):**
   - *Decision:* Persist `outbox_messages` in the same PostgreSQL ACID transaction as domain mutations (`OperationRecord`, `AuditEvent`), relayed asynchronously to NATS JetStream.
   - *Outcome:* Eliminated dual-write anomalies where a database commit succeeds but a broker publish fails (or vice versa).

3. **Redis Lease Fencing for Reconciler Mutual Exclusion ([ADR-0003](../adr/ADR-0003-delivery-semantics-and-idempotent-execution.md)):**
   - *Decision:* Require executor workers to acquire a per-resource Redis lease (`SET NX PX`) with monotonic fencing tokens and atomic Lua release scripts before calling the Kubernetes API.
   - *Outcome:* Prevented split-brain reconciliation during worker crashes or network partitions, proved during M3 and M5 chaos drills (`0` duplicate Kubernetes resources).

4. **Kubernetes Namespace-Per-Workspace Isolation ([ADR-0004](../adr/ADR-0004-trust-model-and-tenant-isolation.md)):**
   - *Decision:* Map each HamiCloud workspace to a dedicated Kubernetes namespace (`hc-ws-<slug>-<id8>`) enforced with `PodSecurity: restricted:latest`, `ResourceQuota`, `LimitRange`, and default-deny cross-namespace `NetworkPolicy`.
   - *Outcome:* Defense-in-depth isolation enforced both at the API admission layer and at the Linux kernel / CNI level.

5. **Rootless BuildKit & Digest-Pinned Artifact Registry ([ADR-0005](../adr/ADR-0005-technology-stack-and-compatibility-matrix.md)):**
   - *Decision:* Build container images inside rootless BuildKit daemons and pin every deployment and batch job to an immutable `@sha256:...` image digest.
   - *Outcome:* Eliminated Docker-socket privilege escalation and guaranteed deterministic, reproducible rollbacks.

---

## 3. Failure Postmortems & Resilience Drills (M3 & M5)

### Drill 1: Executor Hard Kill (`SIGKILL`) Mid-Reconciliation
- **Scenario:** An executor worker was forcefully terminated while reconciling an active `DEPLOY_RELEASE` operation (`op_id = 3ba08e68-...`).
- **Observed Behavior:** The worker's Redis lease expired cleanly; a standby worker acquired the lease with an incremented fencing token (`fencing_token = 2`), inspected the existing Kubernetes `Deployment` in `hc-ws-m5-drill-fc1bf922` via label selector, adopted it idempotently, and transitioned the operation to `SUCCEEDED` in `2.08 s`.
- **Invariant Verified:** `0` duplicate Kubernetes `Deployment` or `Pod` resources created.

### Drill 2: Unhealthy Release & Automatic Rollback
- **Scenario:** A canary release (`v2`) was deployed with a failing readiness probe (`/healthz` returning `500` / non-listening port).
- **Observed Behavior:** The reconciler detected readiness timeout within the probe window, aborted the canary rollout, restored 100% traffic to the prior `ACTIVE` release (`v1`), marked `v2` as `ROLLED_BACK`, and emitted an immutable `service.release.rollback` audit event in `2.06 s` (`p95` target `< 10 s`).

### Drill 3: PostgreSQL + MinIO Disaster Restore
- **Scenario:** Simulated catastrophic storage loss after executing builds, service deployments, and batch job log archival.
- **Observed Behavior:** Restored PostgreSQL via `pg_dump`/`pg_restore` and MinIO object buckets in `0.81 s`, with 100% row-count parity across all 10 tables and byte-for-byte SHA256 verification of archived gzipped job logs.

---

## 4. Lessons Learned

- **Idempotency Keys Are Non-Negotiable:** Enforcing `Idempotency-Key` headers at the HTTP boundary simplified client retry logic and prevented duplicate build/job dispatches under transient network failures.
- **Envelope Encryption Separation:** Keeping secret plaintext strictly out of API response schemas (`SecretResponse` omitting `encrypted_data`) and applying automatic log scrubbing (`redact_secrets`) eliminated accidental credential leakage across both UI and telemetry pipelines.
