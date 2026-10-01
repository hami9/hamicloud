# Demo: Failed Job and Retry Budget Exhaustion

This demonstration verifies Milestone M2 exit criterion 7:
> A controlled transient failure retries within the declared budget.

Recorded from live API and Go runtime scheduler/executor execution against PostgreSQL.

---

## 1. Execution Overview

- **Workspace ID:** `41733adc-1c57-4ded-9f71-09000af96f1d`
- **Job ID:** `1811a610-1a80-4598-8aaa-796dc19c5bf3`
- **Declared Budget:** `max_retries = 1` (allowing attempt 1 plus 1 retry)

---

## 2. Step-by-Step Live Execution

### Step 1: Submit Failing Job

```http
POST /v1/workspaces/41733adc-1c57-4ded-9f71-09000af96f1d/jobs HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice
Idempotency-Key: idemp-demo-fail-42

{
  "name": "fatal-process",
  "image_digest": "docker.io/library/python:3.12-alpine",
  "command_args": ["python", "-c", "import sys; sys.exit(42)"],
  "timeout_seconds": 30,
  "max_retries": 1
}
```

**Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "1811a610-1a80-4598-8aaa-796dc19c5bf3",
  "status": "ACCEPTED",
  "status_url": "/v1/operations/1811a610-1a80-4598-8aaa-796dc19c5bf3"
}
```

### Step 2: Attempt 1 Admission by Scheduler

```bash
hamicloud-scheduler --run-once
```

**Live Log:**
```json
{"time":"2026-10-01T17:44:47.3665072+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"4f002249-b163-424d-8f30-554614dcdd7c","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","job_attempt_id":"ec3325ea-6a8e-40dc-81ea-64324557ab77","resource_name":"hc-job-1811a610-1a80-4598-8aaa-796dc19c5bf3-1"}
{"time":"2026-10-01T17:44:47.3665072+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":2}
```

### Step 3: Attempt 1 Execution & Non-Zero Exit Code

```bash
hamicloud-executor --run-once
```

**Live Log:**
```json
{"time":"2026-10-01T17:45:07.1240549+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"4f002249-b163-424d-8f30-554614dcdd7c","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","job_name":"fatal-process","attempt_number":1,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-01T17:45:07.1240549+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"4f002249-b163-424d-8f30-554614dcdd7c","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","job_name":"fatal-process","attempt_number":1,"lease_epoch":1}
{"time":"2026-10-01T17:45:07.3453255+03:30","level":"WARN","msg":"Job attempt failed","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","attempt_number":1,"exit_code":42,"should_retry":true,"reason":"process exited with status 42"}
```

Job transitions to `RETRY_WAIT`.

### Step 4: Attempt 2 Requeue and Admission

Following backoff, scheduler requeues attempt 2:
```json
{"time":"2026-10-01T17:45:13.0744734+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":2}
{"time":"2026-10-01T17:45:13.3586407+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"53308a52-aa2f-45a3-a97c-2644a1622494","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","job_attempt_id":"2b43cef3-7695-4b43-9896-560544ff9d62","resource_name":"hc-job-1811a610-1a80-4598-8aaa-796dc19c5bf3-2"}
```

### Step 5: Attempt 2 Execution & Retry Budget Exhaustion

```bash
hamicloud-executor --run-once
```

**Live Log:**
```json
{"time":"2026-10-01T17:45:15.1175771+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"53308a52-aa2f-45a3-a97c-2644a1622494","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","job_name":"fatal-process","attempt_number":2,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-01T17:45:15.7588866+03:30","level":"WARN","msg":"Job attempt failed","job_id":"1811a610-1a80-4598-8aaa-796dc19c5bf3","attempt_number":2,"exit_code":42,"should_retry":false,"reason":"process exited with status 42"}
```

With `max_retries = 1` and 2 failed attempts, `should_retry` is `false`. The job transitions to terminal state `FAILED`.
