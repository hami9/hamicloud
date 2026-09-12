# ADR-0001: Clear Component Ownership & Responsibility Split

- **Status:** Accepted
- **Date:** 2026-09-12
- **Authors:** HamiCloud Engineering Agent, hami9
- **Deciders:** hami9 (Project Owner)

---

## Context and Problem Statement

A common anti-pattern in platform engineering is blurry division of responsibility: an API directly creating Kubernetes pods, or a queue worker making business authorization decisions, or an orchestration engine attempting to schedule pods onto specific cluster nodes. Without strict boundaries, systems become fragile, difficult to test, prone to split-brain states, and impossible to secure.

HamiCloud needs clear, non-negotiable boundaries separating product access, scheduling/admission, cluster reconciliation, and container execution.

---

## Decision Drivers

1. **Isolation of Concerns:** Changes to API authorization must not risk cluster reconciliation stability.
2. **Kubernetes Separation of Powers:** Kubernetes already has a world-class scheduler for physical node placement (`kube-scheduler`). HamiCloud should decide *what* and *when* work runs, never *where* it runs.
3. **Auditability & Durability:** User requests must be validated, authorized, and made durable before touching external orchestration.
4. **Resilience to Component Crashes:** If the API crashes, ongoing workloads must not be affected. If an executor crashes, state must not be lost or corrupted.

---

## Decision

We establish four distinct, non-overlapping ownership domains:

```text
+-----------------------+      +-----------------------+      +-----------------------+      +-----------------------+
|  FastAPI Control API  | ---> |     Go Scheduler      | ---> |  Go Execution Worker  | ---> |   Kubernetes API      |
| (Product Access / Desired) | | (Admission / Intents) |      | (Reconciliation Loop) |      | (Pod Placement & Lifecycle)
+-----------------------+      +-----------------------+      +-----------------------+      +-----------------------+
```

### 1. FastAPI Control API (`apps/api/`)
- **Owns:** Product access, user authentication (OIDC/JWT), workspace authorization (RBAC), input validation, desired-state persistence in PostgreSQL, public HTTP API contract, and event outbox recording.
- **Forbidden:** Never calls the Kubernetes API. Never schedules workloads directly. Never performs asynchronous long-running execution.

### 2. Go Scheduler (`runtime/cmd/hamicloud-scheduler`)
- **Owns:** Admission control. Evaluates when a build, job, or release is eligible to execute based on workspace quotas, concurrency limits, and fair-queuing policies. Writes durable `ExecutionIntent` records to PostgreSQL.
- **Forbidden:** Never places pods on nodes. Never calls the Kubernetes API directly. Never modifies product-level workspace or user permissions.

### 3. Go Execution Worker (`runtime/cmd/hamicloud-executor`)
- **Owns:** Cluster reconciliation. Claims pending `ExecutionIntent` records using bounded leases and monotonic epochs. Translates intents into deterministic Kubernetes resources (Deployments, Services, Jobs, HTTPRoutes). Observes actual cluster status and persists updates to PostgreSQL.
- **Forbidden:** Never performs admission or quota checks. Never mutates desired-state specifications. Stale workers whose lease epoch has expired are strictly rejected by database fencing.

### 4. Kubernetes Runtime
- **Owns:** Physical Pod placement, container execution, process restart within a Pod, and node resource isolation.
- **Forbidden:** Does not manage application retry policies (HamiCloud configures `restartPolicy: Never` and `backoffLimit: 0` for its managed Jobs).

---

## Consequences

### Positive
- Unit and integration tests can isolate each layer completely (e.g., test scheduler admission logic without mocking Kubernetes).
- Clear transactional boundaries: no network I/O across database transactions.
- Zero risk of competing schedulers conflicting with `kube-scheduler`.

### Negative / Tradeoffs
- Slight operational overhead of managing two distinct process modes (Scheduler and Executor) in the Go runtime codebase.
- Inter-component communication requires durable state handoffs (PostgreSQL transactional outbox and execution intents).
