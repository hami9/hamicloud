# ADR-0003: Delivery Semantics, Idempotent Execution, and Fencing

- **Status:** Accepted
- **Date:** 2026-09-12
- **Authors:** HamiCloud Engineering Agent, hami9
- **Deciders:** hami9 (Project Owner)

---

## Context and Problem Statement

In distributed orchestration systems, network failures, node reboots, and process pauses (e.g. garbage collection or scheduling preemption) are inevitable. When an executor encounters a delay or partition, it may wake up after its task has been reassigned to another executor, attempting to write stale state ("zombie worker" problem).

Furthermore, message brokers deliver messages multiple times during network hiccups, and clients submit duplicate HTTP requests when network timeouts occur.

HamiCloud must define mathematically sound delivery and execution semantics that eliminate race conditions, duplicate execution, and state corruption.

---

## Decision Drivers

1. **Honesty on Distributed Guarantees:** Never claim "exactly-once" execution. Kubernetes workloads and network I/O cannot guarantee exactly-once side effects; the control plane must enforce at-least-once delivery with idempotent transitions.
2. **Clear Separation of Retry vs. Redelivery:**
   - **Application Retry:** Failure of a workload run. Creates a brand-new `JobAttempt` with incremented attempt number, logs, and exponential jittered backoff.
   - **Broker Redelivery:** Re-delivery of a message notification for an existing attempt due to worker crash or unacknowledged message. Must NOT consume the application retry budget or increment attempt count.
3. **Lease Epoch Fencing:** Guard against zombie processes committing updates after losing their lease.
4. **Deterministic Kubernetes Resource Identifiers:** Repeated create calls must discover existing cluster resources rather than spawning duplicates.

---

## Decision

### 1. Delivery & Transition Invariants
- All control-plane events and state transitions are **strictly at-least-once delivered and idempotent**.
- Re-processing an identical event produces identical database state without side effects.
- Idempotency records store `(workspace_id, endpoint, idempotency_key, request_hash, response_code, response_body, expires_at)`.
  - Same key + same hash: returns cached response immediately.
  - Same key + different hash: returns `409 Conflict`.

### 2. Execution Lease & Epoch Fencing
To execute a job attempt or reconcile a release, a worker must claim the `ExecutionIntent` in PostgreSQL:
```sql
UPDATE execution_intents
SET 
    claimed_by = :worker_id,
    lease_expires_at = NOW() + INTERVAL '60 seconds',
    lease_epoch = lease_epoch + 1,
    status = 'CLAIMED',
    updated_at = NOW()
WHERE id = :intent_id
  AND (status = 'PENDING' OR lease_expires_at < NOW())
RETURNING lease_epoch;
```

When writing state transitions or finalizing execution:
```sql
UPDATE job_attempts
SET 
    state = :new_state,
    exit_code = :exit_code,
    updated_at = NOW()
WHERE id = :attempt_id
  AND lease_epoch = :claimed_epoch;
```
If zero rows are updated, the worker knows its lease was revoked and its claim was reclaimed by another worker; it aborts without committing.

### 3. Generation-Scoped Kubernetes Resource Naming
Resource names created in Kubernetes follow deterministic conventions:
- For HTTP Services: `hc-svc-{app_id}-{generation}`
- For Finite Jobs: `hc-job-{job_id}-{attempt_number}`

When an executor prepares to create a Kubernetes resource:
1. It queries Kubernetes for an existing resource with that deterministic name.
2. If found, it reads the resource's `metadata.uid` and `metadata.resourceVersion`.
3. If not found, it creates the resource and records the returned `UID`.
4. Subsequent updates pass Kubernetes `resourceVersion` preconditions.

### 4. Retry Budget & Backoff Policy
- Maximum attempts: 3 attempts total per logical job.
- Backoff formula: Full-jitter exponential delay:
  $$\text{delay} = \min(\text{cap}, \text{base} \times 2^{\text{attempt}}) \times \text{random}(0, 1)$$
  - Base: 5 seconds, Cap: 60 seconds.
- Kubernetes Jobs are configured with `restartPolicy: Never` and `backoffLimit: 0`. HamiCloud's scheduler exclusively manages attempt creation and timing.

---

## Consequences

### Positive
- Total protection against zombie executor updates.
- Transparent tracking of actual container failures versus platform delivery events.
- Deterministic reconciliation recovery without orphan Kubernetes resources.

### Negative / Tradeoffs
- Workers must maintain active heartbeats to extend leases for long-running reconciliations.
- Schema requires dedicated columns for `lease_epoch`, `lease_expires_at`, and `claimed_by`.
