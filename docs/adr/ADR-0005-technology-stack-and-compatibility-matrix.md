# ADR-0005: Locked Technology Stack and Compatibility Matrix

- **Status:** Accepted
- **Date:** 2026-09-12
- **Authors:** HamiCloud Engineering Agent, hami9
- **Deciders:** hami9 (Project Owner)

---

## Context and Problem Statement

Infrastructure platforms frequently suffer from dependency bloat and premature architectural fragmentation. Introducing redundant message brokers (Kafka, Celery, Redis Streams), premature microservice splits, or complex service meshes increases operational fragility without providing engineering value for a single-cluster runtime.

HamiCloud locks its technology stack to proven, performant, and well-understood foundational components.

---

## Decision Drivers

1. **Simplicity and Reliability:** Fewer moving parts minimize failure domains.
2. **Deterministic Durability:** Only one system of record for truth (PostgreSQL) and one notification broker (NATS JetStream).
3. **Single Codebase per Language:** One Python modular monolith for the control API; one Go codebase with multi-mode CLI entry points for runtime operations.
4. **Reproducibility:** Pin exact major/minor versions across development, CI, and staging environments.

---

## Decision

### 1. Technology Matrix

| Component | Technology | Baseline Version | Rationale & Responsibility |
| --- | --- | --- | --- |
| **Control API** | Python / FastAPI | Python 3.12+, FastAPI 0.115+, Pydantic v2 | High developer ergonomics for API validation, auth, and schema migrations. |
| **ORM & Migrations** | SQLAlchemy & Alembic | SQLAlchemy 2.0+, Alembic 1.14+ | Authoritative owner of database schemas, explicit relational constraints. |
| **Runtime Orchestrator** | Go | Go 1.23+ | Memory efficiency, concurrent goroutines, low-latency control loops. |
| **Kubernetes Client** | `k8s.io/client-go` | v0.31+ | Official Kubernetes API client for reconcilers and informers. |
| **Database Driver (Go)** | `jackc/pgx/v5` | v5.6+ | High-performance native PostgreSQL driver with connection pooling. |
| **Primary Datastore** | PostgreSQL | 16 Alpine | ACID transactions, outbox table, logical state. |
| **Notification Broker** | NATS JetStream | 2.10+ | Durable, low-overhead at-least-once message streaming. |
| **Cache & Rate Limiting**| Redis | 7.2 Alpine | Atomic token buckets; strictly non-durable cache. |
| **Container Builds** | Dockerfile / BuildKit | BuildKit v0.15+ | Rootless, isolated OCI image builds from Git repositories. |
| **Execution Platform** | Kubernetes / containerd| Kubernetes 1.30+ | Container orchestration, pod placement, node isolation. |
| **Ingress & Routing** | Traefik | v3.1+ | Dynamic route configuration, TLS termination. |
| **Object Storage** | S3-Compatible / MinIO | RELEASE.2024+ | Artifact outputs, build logs, and backup storage. |
| **Identity Provider** | OIDC / Keycloak | 24+ (or mock OIDC) | Standard OIDC Authorization Code Flow with PKCE. |
| **Web Dashboard** | React / Vite / Tailwind| React 18+, Vite 5+, TS 5+ | Single-page application, typed API client, accessible UI. |
| **Telemetry** | OpenTelemetry | Collector v0.108+ | Vendor-neutral traces, metrics, structured logs. |

### 2. Forbidden Technologies (Hard Rules)
- **NO Celery, Kafka, or Redis Streams:** NATS JetStream is the only message broker. Redis is never used as a durable job queue.
- **NO Microservice Splitting:** FastAPI remains a modular monolith. Go runtime is a single binary with `scheduler` and `executor` subcommands.
- **NO Service Mesh:** Ingress routing is handled by Traefik; internal workload networking relies on standard Kubernetes Services and NetworkPolicies.
- **NO gRPC in v1:** Inter-process communication uses PostgreSQL transactional state and versioned JSON events over NATS. gRPC will only be evaluated if a measured synchronous internal bottleneck is demonstrated.

---

## Consequences

### Positive
- Unified stack minimizes cognitive load and simplifies local development (`docker compose up` starts all external dependencies).
- Strict version pinning prevents unexpected dependency drift between development and deployment.
- High testability: mock-free integration testing with lightweight containerized services.

### Negative / Tradeoffs
- Requires developers to be proficient in both Python (API & migrations) and Go (runtime & Kubernetes integration).
- Relational schema changes must be synchronized between Python Alembic models and Go SQL queries.
