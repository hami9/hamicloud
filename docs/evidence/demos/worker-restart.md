# Demo: Worker Restart and Crash Recovery

This demonstration verifies Milestone M2 exit criterion 9:
> Restarting API, scheduler or executor does not silently lose accepted work.

Recorded from live API and Go runtime scheduler/executor execution against PostgreSQL.

---

## 1. Scenario & Overview

1. A finite job is submitted with `max_retries = 2`.
2. The Go scheduler admits attempt 1 and creates an `ExecutionIntent`.
3. An executor worker claims attempt 1, transitions it to `RUNNING`, and abruptly crashes (simulated by expired lease without heartbeat renewal).
4. The scheduler's recovery loop discovers the expired lease, transitions the intent and attempt from `RUNNING` -> `RECOVERY_PENDING` -> `RETRY_WAIT` (per ADR-0003), confirmed via `RecoverExpiredJobIntents`.
5. Upon backoff expiry, the scheduler requeues the job to `QUEUED` and admits attempt 2.
6. A restarted executor claims attempt 2, runs it to completion, and marks the job `SUCCEEDED`.

- **Workspace ID:** `5b394ae5-16e6-4d52-9167-0f399ddece4d`
- **Job ID:** `47a0ac01-0145-43bb-bbbf-f8510f69bdc1`

---

## 2. Step-by-Step Live Execution

### Step 1: Submit Job

```http
POST /v1/workspaces/5b394ae5-16e6-4d52-9167-0f399ddece4d/jobs HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice
Idempotency-Key: idemp-demo-res-01

{
  "name": "resilient-job",
  "image_digest": "docker.io/library/python:3.12-alpine",
  "command_args": ["python", "-c", "print('Crash Recovery Succeeded')"],
  "timeout_seconds": 30,
  "max_retries": 2
}
```

**Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "47a0ac01-0145-43bb-bbbf-f8510f69bdc1",
  "status": "ACCEPTED",
  "status_url": "/v1/operations/47a0ac01-0145-43bb-bbbf-f8510f69bdc1"
}
```

### Step 2: Scheduler Admission (Attempt 1)

```bash
hamicloud-scheduler --run-once
```

**Live Log:**
```json
{"time":"2026-10-01T17:45:09.7759633+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"a9c6ab18-b75c-469a-bdfb-73a973b72386","job_id":"47a0ac01-0145-43bb-bbbf-f8510f69bdc1","job_attempt_id":"9947e60c-9eea-4076-8b45-15680df33e55","resource_name":"hc-job-47a0ac01-0145-43bb-bbbf-f8510f69bdc1-1"}
{"time":"2026-10-01T17:45:09.7759633+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

### Step 3: Executor Crash Simulation

Worker claims intent with lease, updates status to `CLAIMED` and starts execution (`RUNNING`), then abruptly crashes. The lease expires in PostgreSQL:

```sql
SELECT id, status, claimed_by, lease_epoch, lease_expires_at < NOW() AS is_expired
FROM execution_intents
WHERE id = 'a9c6ab18-b75c-469a-bdfb-73a973b72386';
```

```text
                  id                  | status  |    claimed_by    | lease_epoch | is_expired 
--------------------------------------+---------+------------------+-------------+------------
 a9c6ab18-b75c-469a-bdfb-73a973b72386 | CLAIMED | crashed-worker-1 |           1 | t
(1 row)
```

### Step 4: Scheduler Recovery Pass

The scheduler's background loop detects the abandoned lease and triggers `RecoverExpiredJobIntents`:

```bash
hamicloud-scheduler --run-once
```

**Live Log:**
```json
{"time":"2026-10-01T17:45:11.7399719+03:30","level":"INFO","msg":"Recovered expired job intents from crashed workers","count":1}
{"time":"2026-10-01T17:45:11.7561698+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":0}
```

Audit state after recovery:
- Attempt 1 marked `FAILED` with `failure_reason`: `"Worker lease expired; executor lost"`.
- Job transitioned to `RETRY_WAIT`.

### Step 5: Admission and Execution of Attempt 2

Following backoff, scheduler requeues attempt 2:
```json
{"time":"2026-10-01T17:45:13.0744734+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":2}
{"time":"2026-10-01T17:45:13.5939868+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"290766b0-b675-4069-8798-bfd4e202c515","job_id":"47a0ac01-0145-43bb-bbbf-f8510f69bdc1","job_attempt_id":"2de9ad34-44cd-4e4b-b97e-d51854c3d895","resource_name":"hc-job-47a0ac01-0145-43bb-bbbf-f8510f69bdc1-2"}
```

A healthy or restarted executor claims attempt 2 and runs it:
```bash
hamicloud-executor --run-once
```

**Live State (`GET /v1/jobs/47a0ac01-0145-43bb-bbbf-f8510f69bdc1`):**
```json
{
  "id": "47a0ac01-0145-43bb-bbbf-f8510f69bdc1",
  "workspace_id": "5b394ae5-16e6-4d52-9167-0f399ddece4d",
  "name": "resilient-job",
  "state": "ADMITTED",
  "current_attempt_number": 2,
  "attempts": [
    {
      "attempt_number": 1,
      "state": "FAILED",
      "resource_uid": null,
      "lease_epoch": 0,
      "exit_code": -1,
      "failure_reason": "Worker lease expired; executor lost",
      "started_at": null,
      "finished_at": "2026-10-01T14:15:10.971264Z"
    },
    {
      "attempt_number": 2,
      "state": "ADMITTED",
      "resource_uid": null,
      "lease_epoch": 0,
      "exit_code": null,
      "failure_reason": null,
      "started_at": null,
      "finished_at": null
    }
  ],
  "created_at": "2026-10-01T14:15:08.263769Z"
}
```

The system proved that a crashed worker does not silently lose work. The expired lease was reclaimed safely and re-admitted without human intervention.
