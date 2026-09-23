# ADR-0002: Durable State and Transactional Outbox Pattern

- **Status:** Accepted
- **Date:** 2026-09-12
- **Authors:** HamiCloud Engineering Agent, hami9
- **Deciders:** hami9 (Project Owner)

---

## Context and Problem Statement

Distributed systems frequently suffer from the "dual-write" problem: when an API receives a deployment or job request, it must persist the state to a database and publish an event to a message broker. If the database write succeeds but the broker publish fails (e.g. network partition, process crash), the system enters a split-brain state where accepted work is silently lost. Conversely, publishing before committing can notify workers of state that rolls back.

Furthermore, message brokers must not be treated as permanent databases. A broker outage or message expiration must never destroy customer state.

---

## Decision Drivers

1. **PostgreSQL Owns Truth:** Relational state with foreign keys, checks, and ACID transactions is the sole authoritative state.
2. **Zero Work Loss:** A crash at any millisecond must leave the system in a consistent, recoverable state.
3. **No Distributed 2-Phase Commit (2PC):** 2PC across databases and message brokers introduces prohibitive latency and coordinator failure modes.
4. **Decoupling Network I/O from DB Locks:** Database transactions must be kept sub-millisecond; holding locks open while awaiting network I/O to a broker or Kubernetes API is strictly prohibited.

---

## Decision

We adopt the **Transactional Outbox Pattern** with **Periodic Reconciliation Fallback**:

```text
[API Request]
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│ SINGLE DATABASE TRANSACTION                             │
│ 1. Validate idempotency key                             │
│ 2. Insert/Update desired state (App / Job / Release)    │
│ 3. Insert Idempotency Record (cached response)          │
│ 4. Insert Outbox Event (topic, event_id, payload)       │
└─────────────────────────────────────────────────────────┘
      │
      ▼ Commit Successful (Return 202 Accepted to Client)
      │
      ▼
┌─────────────────────────────────────────────────────────┐
│ Outbox Dispatcher Process                               │
│ - SELECT FOR UPDATE SKIP LOCKED on pending outbox events│
│ - Publish to NATS JetStream (with stable event_id)      │
│ - Mark event as PUBLISHED upon JetStream ACK            │
└─────────────────────────────────────────────────────────┘
      │
      ▼ NATS JetStream
┌─────────────────────────────────────────────────────────┐
│ Go Scheduler / Consumer                                 │
│ - Pull message with explicit ACK                        │
│ - Record durable ExecutionIntent in PostgreSQL          │
│ - ACK message to NATS                                   │
└─────────────────────────────────────────────────────────┘
      │
      ▼ Fallback Mechanism
┌─────────────────────────────────────────────────────────┐
│ Background Reconciliation Scanner (every 30 seconds)   │
│ - Finds unadmitted jobs / unhandled events              │
│ - Re-emits notifications if broker dropped messages     │
└─────────────────────────────────────────────────────────┘
```

### Invariants

1. **Atomic Request Acceptance:** An API request is accepted and returned to the caller (`202 Accepted`) ONLY AFTER the single transaction encompassing desired state, idempotency record, and outbox event has successfully committed.
2. **At-Least-Once Broker Notification:** The outbox dispatcher publishes events to NATS JetStream with stable deterministic UUIDs (`event_id`). If the dispatcher crashes before recording publication, it may publish the event again on restart. Consumers handle duplicates idempotently.
3. **Lost Notification Behavior and Periodic Reconciliation Scans:**
   If NATS JetStream is temporarily unavailable, drops a notification, partitions from consumers, or the outbox dispatcher crashes before publishing, **accepted work is never lost** because PostgreSQL is the single durable system of record. Notification loss merely delays processing until the next reconciliation tick. To guarantee self-healing convergence, the control plane and runtime run dedicated periodic reconciliation scans covering every category of accepted work:
   - **Queued Jobs (Period: 30s, Go Scheduler):**
     ```sql
     SELECT id, workspace_id, current_attempt_number 
     FROM jobs 
     WHERE state = 'QUEUED' 
       AND updated_at < NOW() - INTERVAL '30 seconds';
     ```
     Repairs jobs whose initial `job.submitted.v1` notification was lost, queuing them for execution intent generation.
   - **Releases with Unapplied Desired Generations (Period: 30s, Go Scheduler/Controller):**
     ```sql
     SELECT a.id, a.workspace_id, a.desired_generation, a.current_release_id 
     FROM applications a
     LEFT JOIN execution_intents ei 
       ON ei.release_id = a.current_release_id 
      AND ei.target_generation = a.desired_generation 
      AND ei.status IN ('PENDING', 'APPLIED')
     WHERE a.current_release_id IS NOT NULL 
       AND ei.id IS NULL 
       AND a.updated_at < NOW() - INTERVAL '30 seconds';
     ```
     Repairs deployments and rollbacks whose `app.deployment.requested.v1` notification was dropped, creating the missing workload execution intent.
   - **Pending Cancellations (Period: 15s, Go Executor/Reconciler):**
     ```sql
     SELECT id, workspace_id, current_attempt_number 
     FROM jobs 
     WHERE state = 'CANCEL_REQUESTED' 
       AND updated_at < NOW() - INTERVAL '15 seconds';
     ```
     Repairs jobs whose `job.cancellation.requested.v1` notification was lost or whose container termination timed out, re-asserting teardown against the Kubernetes API.
   - **Stale PENDING Outbox Events (Period: 10s, Outbox Dispatcher Worker):**
     ```sql
     SELECT id, event_id, topic, payload_json 
     FROM outbox_events 
     WHERE status = 'PENDING' 
       AND created_at < NOW() - INTERVAL '10 seconds'
     ORDER BY created_at ASC 
     LIMIT 100 
     FOR UPDATE SKIP LOCKED;
     ```
     Repairs events that were committed to the outbox but never received an initial dispatch ACK from NATS JetStream due to dispatcher process restarts.

4. **Outbox Retention and Purge Ownership:**
   Published outbox events are retained in `outbox_events` for exactly 7 days to facilitate operational troubleshooting, auditing, and re-delivery verification. A dedicated control-plane housekeeping cron worker (owned by the API background maintenance service, running daily at 02:00 UTC) physically purges expired rows:
   ```sql
   DELETE FROM outbox_events 
   WHERE status = 'PUBLISHED' 
     AND published_at < NOW() - INTERVAL '7 days';
   ```

---

## Consequences

### Positive
- Guaranteed zero work loss: database failure rolls back the entire request cleanly, and message broker failure never loses committed work.
- Decoupled latency: API responses are not bound to broker latency or cluster health.
- Self-healing platform that automatically recovers after broker outages across all workloads (jobs, releases, cancellations, outbox).

### Negative / Tradeoffs
- Slight latency between database commit and worker wake-up (typically < 15ms with outbox polling/LISTEN-NOTIFY).
- Outbox table requires periodic vacuuming and cleanup of published events (retained for 7 days, purged by the background housekeeping worker).
