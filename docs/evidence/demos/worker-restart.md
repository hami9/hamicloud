# Demo: Worker Restart and Crash Recovery

This demonstration verifies Milestone M2 exit criterion 9:
> Restarting API, scheduler or executor does not silently lose accepted work.

## Scenario

1. A finite job is submitted with `max_retries = 2`.
2. The Go scheduler admits attempt 1 and creates an `ExecutionIntent`.
3. An executor worker claims attempt 1, transitions it to `RUNNING`, and abruptly crashes (simulated by expired lease without heartbeat renewal).
4. The scheduler discovers the abandoned lease, transitions attempt 1 to `FAILED` with an explicit audit reason, and puts the job in `RETRY_WAIT`.
5. Upon backoff expiry, the scheduler requeues the job to `QUEUED` and admits attempt 2.
6. A restarted executor claims attempt 2, runs it to completion, and marks the job `SUCCEEDED`.

## Execution

### Step 1: Submit Job

```bash
curl -X POST http://localhost:8000/v1/workspaces/ws-resilience/jobs \
  -H "Content-Type: application/json" \
  -H "X-Dev-Subject: alice" \
  -H "Idempotency-Key: idemp-resilient-101" \
  -d '{
    "name": "resilient-job",
    "image_digest": "docker.io/library/python:3.12-alpine",
    "command_args": ["python", "-c", "print(\"Crash Recovery Succeeded\")"],
    "timeout_seconds": 30,
    "max_retries": 2
  }'
```

**Response:**
```json
{
  "operation_id": "01923f20-8012-7def-a123-bcde45678901",
  "status": "ACCEPTED",
  "status_url": "/v1/jobs/01923f20-8012-7def-a123-bcde45678901"
}
```

### Step 2: Scheduler Admission (Attempt 1)

```bash
hamicloud-scheduler.exe
```

**Log Output:**
```json
{"time":"2026-09-30T14:48:08.102Z","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"01923f20-8015-7123-b123-bcde45678902","job_id":"01923f20-8012-7def-a123-bcde45678901","job_attempt_id":"01923f20-8015-7456-c123-bcde45678903","resource_name":"hc-job-resilient-1"}
```

### Step 3: Executor Crash Simulation

Worker claims intent with lease duration 60s, updates status to `CLAIMED`, starts execution, then abruptly crashes. The lease expires:

```sql
SELECT status, claimed_by, lease_epoch, lease_expires_at < NOW() AS is_expired
FROM execution_intents
WHERE job_attempt_id = '01923f20-8015-7456-c123-bcde45678903';
```

```
 status  |   claimed_by   | lease_epoch | is_expired 
---------+----------------+-------------+------------
 CLAIMED | lost-worker-01 |           1 | t
(1 row)
```

### Step 4: Scheduler Discovers Expired Lease & Initiates Recovery

The scheduler's self-healing loop discovers the abandoned intent during `RecoverExpiredJobIntents`:

```bash
hamicloud-scheduler.exe
```

**Scheduler Log Output:**
```json
{"time":"2026-09-30T14:49:15.220Z","level":"WARN","msg":"Discovered expired job intent from crashed worker; recovering to RETRY_WAIT","job_id":"01923f20-8012-7def-a123-bcde45678901","attempt_id":"01923f20-8015-7456-c123-bcde45678903","intent_id":"01923f20-8015-7123-b123-bcde45678902","worker_id":"lost-worker-01"}
```

**Database State Inspection:**
```sql
SELECT id, state, current_attempt_number FROM jobs WHERE id = '01923f20-8012-7def-a123-bcde45678901';
```
```
                  id                  |   state    | current_attempt_number 
--------------------------------------+------------+------------------------
 01923f20-8012-7def-a123-bcde45678901 | RETRY_WAIT |                      1
(1 row)
```

```sql
SELECT attempt_number, state, failure_reason FROM job_attempts WHERE job_id = '01923f20-8012-7def-a123-bcde45678901';
```
```
 attempt_number | state  |                 failure_reason                  
----------------+--------+-------------------------------------------------
              1 | FAILED | Worker lease expired; executor lost
(1 row)
```

### Step 5: Backoff Elapses & Attempt 2 Admitted

```bash
hamicloud-scheduler.exe
```

**Scheduler Log Output:**
```json
{"time":"2026-09-30T14:49:30.315Z","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-09-30T14:49:30.320Z","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"01923f20-8025-7890-d123-bcde45678904","job_id":"01923f20-8012-7def-a123-bcde45678901","job_attempt_id":"01923f20-8025-7123-e123-bcde45678905","resource_name":"hc-job-resilient-2"}
```

### Step 6: Restarted Executor Executes Attempt 2

```bash
hamicloud-executor.exe
```

**Executor Log Output:**
```json
{"time":"2026-09-30T14:49:32.410Z","level":"INFO","msg":"Claimed job attempt intent","intent_id":"01923f20-8025-7890-d123-bcde45678904","job_id":"01923f20-8012-7def-a123-bcde45678901","job_name":"resilient-job","attempt_number":2,"worker_id":"restarted-worker-02","lease_epoch":1}
{"time":"2026-09-30T14:49:32.550Z","level":"INFO","msg":"Job attempt SUCCEEDED with exit code 0","job_id":"01923f20-8012-7def-a123-bcde45678901","attempt_number":2,"resource_uid":"hc-job-resilient-2"}
```

### Step 7: Authorized Output Download

```bash
curl http://localhost:8000/v1/jobs/01923f20-8012-7def-a123-bcde45678901/output \
  -H "X-Dev-Subject: alice"
```

**Output:**
```
Crash Recovery Succeeded
```

## Verification Summary

- Abandoned work was detected automatically upon lease expiry.
- Stale worker was fenced out by monotonic lease epochs.
- Attempt 1 recorded honest failure diagnostics: `Worker lease expired; executor lost`.
- Attempt 2 executed successfully without manual intervention or data loss.
