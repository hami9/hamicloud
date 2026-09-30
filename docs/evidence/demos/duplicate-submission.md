# Demo: Duplicate Submission Idempotency

This demonstration verifies Milestone M2 exit criterion 6:
> Repeating the same submission returns the same operation.

## Scenario

A client submits an identical job submission request twice with the same `Idempotency-Key` header (`idemp-demo-dup-42`).

## Execution

### Step 1: Initial Submission

```bash
curl -X POST http://localhost:8000/v1/workspaces/ws-demo/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-Subject: alice" \
  -H "Idempotency-Key: idemp-demo-dup-42" \
  -d '{
    "name": "data-aggregation",
    "image_digest": "docker.io/library/python:3.12-alpine",
    "command_args": ["python", "-c", "print(\"Processed records\")"],
    "timeout_seconds": 60,
    "max_retries": 2
  }'
```

**HTTP Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "01923f11-9a74-721d-9e12-4015f8ba8123",
  "status": "ACCEPTED",
  "status_url": "/v1/jobs/01923f11-9a74-721d-9e12-4015f8ba8123"
}
```

### Step 2: Duplicate Submission with Identical Key

```bash
curl -X POST http://localhost:8000/v1/workspaces/ws-demo/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-Subject: alice" \
  -H "Idempotency-Key: idemp-demo-dup-42" \
  -d '{
    "name": "data-aggregation",
    "image_digest": "docker.io/library/python:3.12-alpine",
    "command_args": ["python", "-c", "print(\"Processed records\")"],
    "timeout_seconds": 60,
    "max_retries": 2
  }'
```

**HTTP Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "01923f11-9a74-721d-9e12-4015f8ba8123",
  "status": "ACCEPTED",
  "status_url": "/v1/jobs/01923f11-9a74-721d-9e12-4015f8ba8123"
}
```

### Step 3: Database Verification

Query PostgreSQL to confirm zero duplicated records exist:

```sql
SELECT id, name, idempotency_key, state FROM jobs WHERE idempotency_key = 'idemp-demo-dup-42';
```

**Result:**
```
                  id                  |       name       |   idempotency_key   |  state  
--------------------------------------+------------------+---------------------+---------
 01923f11-9a74-721d-9e12-4015f8ba8123 | data-aggregation | idemp-demo-dup-42   | QUEUED
(1 row)
```

Query `outbox_events` to confirm only one outbox event was generated:

```sql
SELECT id, event_type, deduplication_id, status FROM outbox_events WHERE aggregate_id = '01923f11-9a74-721d-9e12-4015f8ba8123';
```

**Result:**
```
                  id                  |     event_type     |         deduplication_id         |  status   
--------------------------------------+--------------------+----------------------------------+-----------
 01923f11-9a77-78ab-8c90-1289fe123456 | job.requested.v1   | 01923f11-9a74-721d-9e12-4015f... | PUBLISHED
(1 row)
```

## Verification Summary

- Exact identical HTTP status and payload returned on repeated submission.
- Exactly one job record created in `jobs`.
- Exactly one event published to NATS JetStream without duplication.
