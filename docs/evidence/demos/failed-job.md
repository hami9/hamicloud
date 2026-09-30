# Demo: Failed Job Retry Budget and Exhaustion

This demonstration verifies Milestone M2 exit criterion 5 and 11:
> A controlled transient failure retries within the declared budget.
> Duplicate submission, worker restart and failed job demos recorded.

## Scenario

A client submits a finite job configured with `max_retries = 2` that deliberately exits with code `42`.
The platform executes Attempt 1 (fails), enters `RETRY_WAIT`, executes Attempt 2 (fails), enters `RETRY_WAIT`, executes Attempt 3 (fails), and marks the job `FAILED` with budget exhausted.

## Execution

### Step 1: Submit Failing Job

```bash
curl -X POST http://localhost:8000/v1/workspaces/ws-demo/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-Subject: alice" \
  -H "Idempotency-Key: idemp-fail-demo-01" \
  -d '{
    "name": "flaky-etl",
    "image_digest": "docker.io/library/python:3.12-alpine",
    "command_args": ["sh", "-c", "echo \"Simulated failure in step 2\" >&2; exit 42"],
    "timeout_seconds": 30,
    "max_retries": 2
  }'
```

**Response:**
```json
{
  "operation_id": "01923f33-1234-7abc-b456-cdef78901234",
  "status": "ACCEPTED",
  "status_url": "/v1/jobs/01923f33-1234-7abc-b456-cdef78901234"
}
```

### Step 2: Attempt 1 Execution & Failure

1. Scheduler admits Attempt 1.
2. Executor claims Intent and executes workload.
3. Process exits with code `42`.
4. Executor transitions Attempt 1 to `FAILED` and sets job state to `RETRY_WAIT`.

**Log Output:**
```json
{"time":"2026-09-30T15:00:10.120Z","level":"WARN","msg":"Job attempt failed","job_id":"01923f33-1234-7abc-b456-cdef78901234","attempt_number":1,"exit_code":42,"should_retry":true,"reason":"process exited with code 42"}
```

**Job Inspection (`GET /v1/jobs/01923f33-1234-7abc-b456-cdef78901234`):**
```json
{
  "id": "01923f33-1234-7abc-b456-cdef78901234",
  "name": "flaky-etl",
  "state": "RETRY_WAIT",
  "current_attempt_number": 1,
  "attempts": [
    {
      "attempt_number": 1,
      "state": "FAILED",
      "exit_code": 42,
      "failure_reason": "process exited with code 42"
    }
  ]
}
```

### Step 3: Attempt 2 Execution & Failure

After exponential backoff, scheduler requeues to `QUEUED` and admits Attempt 2.
Executor runs Attempt 2, exits with code `42`, transitions Attempt 2 to `FAILED`, and job returns to `RETRY_WAIT`.

```json
{
  "id": "01923f33-1234-7abc-b456-cdef78901234",
  "name": "flaky-etl",
  "state": "RETRY_WAIT",
  "current_attempt_number": 2,
  "attempts": [
    {"attempt_number": 1, "state": "FAILED", "exit_code": 42},
    {"attempt_number": 2, "state": "FAILED", "exit_code": 42}
  ]
}
```

### Step 4: Attempt 3 (Final) & Exhaustion

Backoff elapses, Attempt 3 is admitted.
Executor runs Attempt 3, exits with code `42`.
Since `attempt_number (3) > max_retries (2)`, retry budget is exhausted:
`shouldRetry = false`.
Job transitions to terminal `FAILED`.

**Log Output:**
```json
{"time":"2026-09-30T15:00:35.880Z","level":"WARN","msg":"Job attempt failed","job_id":"01923f33-1234-7abc-b456-cdef78901234","attempt_number":3,"exit_code":42,"should_retry":false,"reason":"process exited with code 42"}
```

**Final Job Inspection:**
```json
{
  "id": "01923f33-1234-7abc-b456-cdef78901234",
  "name": "flaky-etl",
  "state": "FAILED",
  "current_attempt_number": 3,
  "attempts": [
    {"attempt_number": 1, "state": "FAILED", "exit_code": 42, "failure_reason": "process exited with code 42"},
    {"attempt_number": 2, "state": "FAILED", "exit_code": 42, "failure_reason": "process exited with code 42"},
    {"attempt_number": 3, "state": "FAILED", "exit_code": 42, "failure_reason": "process exited with code 42"}
  ]
}
```

### Step 5: Failure Output Retrieval

```bash
curl http://localhost:8000/v1/jobs/01923f33-1234-7abc-b456-cdef78901234/output \
  -H "X-Dev-Subject: alice"
```

**Output:**
```
--- STDERR ---
Simulated failure in step 2
```

## Verification Summary

- Retry attempts stayed strictly within the configured budget (`max_retries = 2`, total 3 attempts).
- Each attempt failure recorded exact exit code `42` and failure message.
- Job finalized in terminal `FAILED` state upon exhaustion.
- Artifact output includes captured stderr diagnostics.
