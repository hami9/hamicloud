# ADR-0003: Delivery Semantics, Idempotent Execution, and Fencing

- **Status:** Accepted
- **Date:** 2026-09-12
- **Authors:** HamiCloud Engineering Agent, hami9
- **Deciders:** hami9 (Project Owner)

---

## Context and Problem Statement

In distributed orchestration systems, network failures, node reboots, and process pauses (e.g. garbage collection or scheduling preemption) are inevitable. When an executor encounters a delay or partition, it may wake up after its task has been reassigned to another executor, attempting to write stale state ("zombie worker" problem).

Furthermore, message brokers deliver messages multiple times during network hiccups, and clients submit duplicate HTTP requests when network timeouts occur.

HamiCloud must define mathematically sound delivery and execution semantics that guarantee idempotent control-plane state transitions and fence stale database commits, while acknowledging that physical workload execution is at-least-once.

---

## Decision Drivers

1. **Honesty on Distributed Guarantees:** Never claim "exactly-once" execution. Kubernetes workloads and network I/O cannot guarantee exactly-once side effects; the control plane must enforce at-least-once delivery with idempotent transitions.
2. **Workload Tolerance of Multiple Starts:** Kubernetes can start a job's program or container more than once (e.g. node evictions, kubelet restarts, API server resyncs). Workload code must tolerate being started more than once, and applications that modify external systems need their own idempotency keys or destination fencing.
3. **Clear Separation of Retry vs. Redelivery:**
   - **Application Retry:** Failure of a workload run. Creates a brand-new `JobAttempt` with incremented attempt number, logs, and exponential jittered backoff.
   - **Broker Redelivery:** Re-delivery of a message notification for an existing attempt due to worker crash or unacknowledged message. Must NOT consume the application retry budget or increment attempt count.
4. **Lease Epoch Fencing:** Guard against zombie processes committing updates after losing their lease.
5. **Deterministic Kubernetes Resource Identifiers:** Repeated create calls must discover existing cluster resources rather than spawning duplicates.

---

## Decision

### 1. Delivery & Transition Invariants
- All control-plane events and state transitions are **strictly at-least-once delivered and idempotent**.
- Re-processing an identical event produces identical database state without side effects.
- **Workload Multi-Start Invariant:** Because Kubernetes can start a container more than once during cluster disruptions, **workload code must tolerate being started more than once**. Applications that write to external systems must implement their own idempotency mechanisms or fencing at the destination.
- Idempotency records store `(workspace_id, endpoint, idempotency_key, request_hash, response_code, response_body, expires_at)`.
  - **Retention Contract:** Records are retained for at least 24 hours (`expires_at = NOW() + INTERVAL '24 hours'`).
  - **Active read check:** The read path checks `expires_at`. If an existing record is expired (`expires_at <= NOW()`), the read path deletes the expired record upon read so the key can be reused immediately, and the incoming request is admitted as a fresh operation.
  - Same key + same hash (unexpired): returns cached `202 Accepted` response immediately.
  - Same key + different hash (unexpired): returns `409 Conflict` (`IDEMPOTENCY_CONFLICT`).
  - **Sweeper Ownership (T10):** Untouched expired idempotency records that are never read again are physically purged by an asynchronous background sweeper task, owned and scheduled by Milestone M1.

### 2. Job State Machine & Lifecycle (Decision D4)
The logical job lifecycle is governed by an explicit state transition table shared across the Python control API and Go runtime:

- **States:**
  - `QUEUED`: Job submitted and persisted, awaiting scheduler evaluation.
  - `ADMITTED`: Quotas reserved and scheduling constraints satisfied.
  - `STARTING`: Workload specification submitted to the Kubernetes cluster.
  - `RUNNING`: Container workload actively executing on an assigned node.
  - `RETRY_WAIT`: Attempt failed with retry budget remaining; waiting for exponential backoff delay.
  - `RECOVERY_PENDING`: Node or executor lost contact / lease; awaiting lease timeout or node recovery.
  - `CANCEL_REQUESTED`: Cancellation requested by client, awaiting container SIGTERM/SIGKILL termination.
  - `SUCCEEDED`: Terminal success (container exited with status code 0).
  - `FAILED`: Terminal failure (retry budget exhausted or non-retryable execution error).
  - `CANCELLED`: Terminal cancellation confirmed.

- **Legal State Transitions:**
  - `QUEUED → ADMITTED`
  - `ADMITTED → STARTING`
  - `STARTING → RUNNING`
  - `STARTING → RETRY_WAIT` (pod creation or scheduling failure with retries remaining)
  - `STARTING → FAILED` (pod specification rejected or retry budget exhausted)
  - `RUNNING → SUCCEEDED` (workload process exited with status code 0)
  - `RUNNING → RETRY_WAIT` (workload failed with retries remaining)
  - `RUNNING → FAILED` (workload failed with retries exhausted)
  - `RETRY_WAIT → QUEUED` (backoff duration elapsed; re-entering queue for next attempt)
  - `QUEUED → CANCEL_REQUESTED` (cancellation requested before admission)
  - `ADMITTED → CANCEL_REQUESTED` (cancellation requested before pod start)
  - `STARTING → CANCEL_REQUESTED` (cancellation requested while pod is starting)
  - `RUNNING → CANCEL_REQUESTED` (cancellation requested while container is running)
  - `RETRY_WAIT → CANCEL_REQUESTED` (cancellation requested while waiting out retry backoff)
  - `RUNNING → RECOVERY_PENDING`: Active attempt's node becomes unreachable or loses lease heartbeat before termination is confirmed.
  - `RECOVERY_PENDING → RETRY_WAIT`: Previous workload on the lost node is confirmed terminated and retries remain for a new attempt.
  - `RECOVERY_PENDING → FAILED`: Recovery timeout expires without termination confirmation or retry budget is exhausted.
  - `RECOVERY_PENDING → CANCEL_REQUESTED`: Client requests cancellation while the job is in recovery pending state.
  - `CANCEL_REQUESTED → CANCELLED` (workload termination confirmed)

- **Decision D4 (Cancellation Racing Workload Completion):**
  If a cancellation request races with a finishing workload: the logical job ends in `CANCELLED`; the attempt record retains its true outcome (`SUCCEEDED`) and its actual process `exit_code: 0`. There is explicitly **no** `CANCEL_REQUESTED → SUCCEEDED` edge on the logical job.

### 3. Execution Lease & Epoch Fencing
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

### 4. Generation-Scoped Kubernetes Resource Naming
Resource names created in Kubernetes follow deterministic conventions:
- For HTTP Services: `hc-svc-{app_id}-{generation}`
- For Finite Jobs: `hc-job-{job_id}-{attempt_number}`

When an executor prepares to create a Kubernetes resource:
1. It queries Kubernetes for an existing resource with that deterministic name.
2. If found, it reads the resource's `metadata.uid` and `metadata.resourceVersion`.
3. If not found, it creates the resource and records the returned `UID`.
4. Subsequent updates pass Kubernetes `resourceVersion` preconditions.

### 5. Retry Budget & Backoff Policy
- Maximum attempts: 3 attempts total per logical job.
- Backoff formula: Full-jitter exponential delay:
  $$\text{delay} = \min(\text{cap}, \text{base} \times 2^{\text{attempt}}) \times \text{random}(0, 1)$$
  - Base: 5 seconds, Cap: 60 seconds.
- Kubernetes Jobs are configured with `restartPolicy: Never` and `backoffLimit: 0`. HamiCloud's scheduler exclusively manages attempt creation and timing.

---

## Consequences

### Positive
- Database lease fencing protects database state against stale executor commits (though it cannot prevent a partitioned process from performing external network I/O before lease expiry).
- Transparent tracking of actual container failures versus platform delivery events.
- Deterministic reconciliation recovery without orphan Kubernetes resources.
- Clear expectations for workload developers regarding multi-start tolerance and external side effects.

### Negative / Tradeoffs
- Workers must maintain active heartbeats to extend leases for long-running reconciliations.
- Workloads must be architected to handle repeated starts gracefully or provide external idempotency.
- Schema requires dedicated columns for `lease_epoch`, `lease_expires_at`, and `claimed_by`.

