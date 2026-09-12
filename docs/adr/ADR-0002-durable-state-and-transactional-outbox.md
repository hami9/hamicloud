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
3. **Reconciliation Fallback:** If NATS is temporarily unavailable, down, or drops a packet, accepted work remains safely recorded in PostgreSQL. The Go scheduler runs a periodic reconciliation query (`SELECT ... WHERE status = 'QUEUED' AND updated_at < NOW() - INTERVAL '30s'`) to recover missed notifications.

---

## Consequences

### Positive
- Guaranteed zero work loss: database failure rolls back the entire request cleanly, and message broker failure never loses committed work.
- Decoupled latency: API responses are not bound to broker latency or cluster health.
- Self-healing platform that automatically recovers after broker outages.

### Negative / Tradeoffs
- Slight latency between database commit and worker wake-up (typically < 15ms with outbox polling/LISTEN-NOTIFY).
- Outbox table requires periodic vacuuming and cleanup of published events (retained for 7 days for audit/debugging).
