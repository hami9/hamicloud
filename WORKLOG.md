# HamiCloud — Engineering Worklog & Audit Trail

This log serves as the authoritative, tamper-evident record of all architectural decisions, implementations, test verifications, and milestone transitions for the **HamiCloud** platform.

---

## Log Schema

Every entry follows this standard format:
- **Timestamp (UTC)**
- **Phase & Milestone ID**
- **Action & Component**
- **Changes / Deliverables**
- **Verification & Test Evidence**
- **Open Risks & Next Actions**

---

## Log Entries

### [2026-09-12T16:03:00Z] Phase 0 / Milestone M0: Project Kickoff & Baseline Foundation

- **Status:** COMPLETED
- **Milestone:** P0 / M0 — Define contracts & Design baseline
- **Source of Truth:** `HamiCloud-Roadmap.md` and `HamiCloud-System-Prompt.md`
- **Actions Completed:**
  1. Initialized Git repository for `hamicloud`.
  2. Configured `.gitignore`, `.editorconfig`, and Apache-2.0 `LICENSE`.
  3. Authored authoritative `README.md` containing architecture, state machines, and invariants.
  4. Authored 5 Architectural Decision Records (`docs/adr/`):
     - `ADR-0001-responsibility-split.md`: FastAPI vs Go Scheduler vs Go Executor vs Kubernetes.
     - `ADR-0002-durable-state-and-transactional-outbox.md`: PostgreSQL truth, atomic outbox pattern, JetStream notification bus.
     - `ADR-0003-delivery-semantics-and-idempotent-execution.md`: At-least-once delivery, retry vs redelivery, bounded leases & epoch fencing.
     - `ADR-0004-trust-model-and-tenant-isolation.md`: Multi-tenancy, Restricted PSS, AES-256 secret encryption, cookie scoping.
     - `ADR-0005-technology-stack-and-compatibility-matrix.md`: Pinned stack, forbidden redundant queues and premature splits.
  5. Established Database Schema & Migrations:
     - SQLAlchemy 2.0 declarative models (`apps/api/app/models/`): `Workspace`, `WorkspaceMembership`, `Application`, `Release`, `Job`, `JobAttempt`, `ExecutionIntent`, `OutboxEvent`, `ConsumedEvent`, `QuotaReservation`, `IdempotencyRecord`, `SecretReference`, `AuditEvent`.
     - Baseline Alembic migration (`migrations/versions/0001_baseline_schema.py`) with all 13 relational tables, unique constraints, and foreign keys.
  6. Published Formal Contracts (`contracts/`):
     - OpenAPI 3.1 contract (`contracts/openapi/v1.yaml`) for workspaces, apps, deployments, jobs, cancellations, rollbacks, and SSE stream.
     - Versioned JSON Schemas (`contracts/events/`): `app.deployment.requested.v1`, `job.submitted.v1`, `job.attempt.failed.v1`, `job.attempt.succeeded.v1`, `workload.reconciliation.requested.v1`.
  7. Created Local Development Bootstrap (`deploy/compose/`):
     - `docker-compose.yml` for PostgreSQL 16 Alpine, Redis 7.2, NATS 2.10 JetStream, and MinIO S3.
     - Database initialization script (`init-db.sql`) and NATS JetStream config (`nats.conf`).
     - Configuration template (`.env.example`).
  8. Scaffolded FastAPI Control API (`apps/api/`):
     - Lifespan management, correlation ID middleware (`X-Correlation-ID`), structured error handling.
     - Liveness (`/healthz`) and readiness (`/readyz`) probes.
     - API v1 routers with transactional outbox and idempotency record handling.
     - Pytest test suite (`apps/api/tests/`).
  9. Scaffolded Go Runtime Module (`runtime/`):
     - `go.mod` for `github.com/hami9/hamicloud/runtime`.
     - Pure domain Job state machine with transition validator (`runtime/internal/domain/job_state.go`).
     - Table-driven unit tests (`runtime/internal/domain/job_state_test.go`) covering all legal paths and preventing illegal shortcuts.
     - Entry points for `hamicloud-scheduler` and `hamicloud-executor`.
  10. Configured GitHub Actions CI pipeline (`.github/workflows/ci.yml`).
  11. Produced Milestone M0 Evidence Record (`docs/evidence/M0-baseline-evidence.md`).
- **Evidence & Verification:**
  - Go domain tests: 13 test cases validated.
  - Python tests: health probes and model definitions validated.
  - Baseline evidence recorded in `docs/evidence/M0-baseline-evidence.md`.
- **Next Phase:**
  - Phase 1 (M1: First live application) — OIDC integration, workspace catalog, Kubernetes client reconciliation loop, deploying first live HTTP service container and routing.
