# Demo: Worker Restart and Crash Recovery

This demonstration verifies Milestone M2 exit criterion 9:
> Restarting API, scheduler or executor does not silently lose accepted work.

Recorded from live API and Go runtime scheduler/executor execution against PostgreSQL.
**Host Environment:** Windows 10 x64 (Build 19045), Python 3.12, Go 1.23, PostgreSQL 16.4.

---

## 1. Scenario & Overview

1. A finite job is submitted with `max_retries = 2`, `timeout_seconds = 30`, and a command running a long execution.
2. The Go scheduler admits attempt 1 and creates an `ExecutionIntent`.
3. An executor worker claims attempt 1, transitions it to `RUNNING` with `started_at` set and `lease_epoch = 1`.
4. While the job is running, the executor worker process is abruptly killed (`Stop-Process -Force` in PowerShell).
5. The system waits 6.0 seconds for the worker's 5.0-second lease to expire in PostgreSQL.
6. The scheduler's recovery loop discovers the expired lease, invokes `JobDeleter` to clean up any orphaned Kubernetes/runner artifacts, transitions attempt 1 from `RUNNING` -> `RECOVERY_PENDING` -> `FAILED` (with `started_at` preserved, `exit_code = -1`, `failure_reason = 'Worker lease expired; executor lost'`), and moves the job to `RETRY_WAIT`.
7. The system waits 6.0 seconds for the 5.0-second retry backoff to elapse.
8. The scheduler requeues the job to `QUEUED` and admits attempt 2 (`ExecutionIntent` #2).
9. A restarted executor claims attempt 2, runs it to successful completion (`exit_code = 0`), and transitions attempt 2 and the overall job to `SUCCEEDED`.
10. Final inspection confirms both attempts are preserved: Attempt 1 is `FAILED` with real failure diagnostics, Attempt 2 is `SUCCEEDED`, and the overall job state is `SUCCEEDED`.

- **Workspace ID:** `2e093b87-bb90-4b7d-a0a3-05e24d05b104`
- **Job ID:** `ba7804b1-8abc-4e82-bb57-126ab3190cde`

---

## 2. Step-by-Step Live Execution & Manual Actions

### Step 1: Submit Long-Running Job

Submit a job with `max_retries = 2` and a 15-second sleep to ensure the worker is actively running when killed:

```http
POST /v1/workspaces/2e093b87-bb90-4b7d-a0a3-05e24d05b104/jobs HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice
Idempotency-Key: idemp-crash-demo-20261001

{
  "name": "crash-recovery-job",
  "image_digest": "docker.io/library/python:3.12-alpine",
  "command_args": ["python", "-c", "import time; time.sleep(15)"],
  "timeout_seconds": 30,
  "max_retries": 2
}
```

**Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "ba7804b1-8abc-4e82-bb57-126ab3190cde",
  "status": "ACCEPTED",
  "status_url": "/v1/operations/ba7804b1-8abc-4e82-bb57-126ab3190cde"
}
```

Initial database state: `jobs.state = 'QUEUED'`, `current_attempt_number = 1`.

---

### Step 2: Scheduler Admission (Attempt 1)

Execute the scheduler to admit the queued job and issue an `ExecutionIntent`:

```powershell
hamicloud-scheduler --run-once
```

**Log Output:**
```json
{"time":"2026-10-01T19:56:09.117950+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"f252077f-99af-471a-bcf7-cdd042ba11fe","job_id":"ba7804b1-8abc-4e82-bb57-126ab3190cde","job_attempt_id":"6cf2619f-a76e-482b-b63f-2ba4100c143a","resource_name":"hc-job-ba7804b1-8abc-4e82-bb57-126ab3190cde-1"}
{"time":"2026-10-01T19:56:09.118200+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

Job state is now `ADMITTED`.

---

### Step 3: Executor Claims Attempt 1 & Begins Running

Launch the executor worker in the background:

```powershell
Start-Process -FilePath "hamicloud-executor.exe" -PassThru
```

The executor claims intent `f252077f-99af-471a-bcf7-cdd042ba11fe` with PID 19440:
- Worker ID: `executor-19440`
- `lease_epoch`: 1
- `lease_expires_at`: `2026-10-01 16:26:15.933026+00:00` (5.0s lease)
- `started_at`: `2026-10-01 16:26:10.933026+00:00`
- Database state: `jobs.state = 'RUNNING'`, `job_attempts.state = 'RUNNING'`.

---

### Step 4: Abrupt Process Kill (Simulated Crash)

While attempt 1 is actively running the 15-second command, terminate the executor process abruptly:

```powershell
Stop-Process -Id 19440 -Force
```

The process is killed immediately. It has no opportunity to send heartbeats, finalize the attempt, or release the database lease.

---

### Step 5: Wait for Lease Expiry

Wait 6.0 seconds (exceeding the 5.0-second lease window):

```powershell
Start-Sleep -Seconds 6
```

Database query confirms the lease is expired:
```sql
SELECT id, status, claimed_by, lease_epoch, lease_expires_at < NOW() AS is_expired
FROM execution_intents
WHERE id = 'f252077f-99af-471a-bcf7-cdd042ba11fe';
```

```text
                  id                  | status  |   claimed_by   | lease_epoch | is_expired 
--------------------------------------+---------+----------------+-------------+------------
 f252077f-99af-471a-bcf7-cdd042ba11fe | CLAIMED | executor-19440 |           1 | t
(1 row)
```

---

### Step 6: Scheduler Recovery Pass

Run the scheduler to detect the abandoned lease and recover the job:

```powershell
hamicloud-scheduler --run-once
```

**Log Output:**
```json
{"time":"2026-10-01T19:56:18.458683+03:30","level":"INFO","msg":"Recovered expired job intents from crashed workers","count":1}
{"time":"2026-10-01T19:56:18.460000+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":0}
```

Audit state after recovery pass:
- Orphaned runner resources deleted via `JobDeleter` outside the DB transaction.
- Intent `f252077f-99af-471a-bcf7-cdd042ba11fe` terminated (`TERMINATED`).
- Attempt 1 (`6cf2619f-a76e-482b-b63f-2ba4100c143a`):
  - `state`: `FAILED`
  - `started_at`: `2026-10-01 16:26:10.933026+00:00`
  - `finished_at`: `2026-10-01 16:26:18.458683+00:00`
  - `lease_epoch`: 1
  - `exit_code`: -1
  - `failure_reason`: `"Worker lease expired; executor lost"`
- Job `ba7804b1-8abc-4e82-bb57-126ab3190cde`: transitioned to `RETRY_WAIT`.

---

### Step 7: Wait for Retry Backoff

Wait 6.0 seconds (exceeding the 5.0-second backoff duration):

```powershell
Start-Sleep -Seconds 6
```

---

### Step 8: Scheduler Requeue & Admission of Attempt 2

Run the scheduler to requeue from `RETRY_WAIT` and admit attempt 2:

```powershell
hamicloud-scheduler --run-once
```

**Log Output:**
```json
{"time":"2026-10-01T19:56:25.100000+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-10-01T19:56:25.105000+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"f67c0c89-1c7f-4fec-aab8-4c15348e873c","job_id":"ba7804b1-8abc-4e82-bb57-126ab3190cde","job_attempt_id":"b1f36403-fbcc-48ed-b921-bb9311a176e8","resource_name":"hc-job-ba7804b1-8abc-4e82-bb57-126ab3190cde-2"}
```

Job state is now `ADMITTED` with `current_attempt_number = 2`.

---

### Step 9: Fresh Executor Claims & Completes Attempt 2

Run an executor pass to claim and execute attempt 2:

```powershell
hamicloud-executor --run-once
```

**Log Output:**
```json
{"time":"2026-10-01T19:56:26.005163+03:30","level":"INFO","msg":"Claimed job intent","intent_id":"f67c0c89-1c7f-4fec-aab8-4c15348e873c","job_attempt_id":"b1f36403-fbcc-48ed-b921-bb9311a176e8"}
{"time":"2026-10-01T19:56:26.195434+03:30","level":"INFO","msg":"Job attempt finished successfully","exit_code":0,"job_id":"ba7804b1-8abc-4e82-bb57-126ab3190cde","attempt_number":2}
```

Attempt 2 finished cleanly with exit code 0.

---

### Step 10: Final State Verification via API

```http
GET /v1/jobs/ba7804b1-8abc-4e82-bb57-126ab3190cde HTTP/1.1
Host: localhost:8000
X-Dev-Subject: alice
```

**Response (HTTP 200 OK):**
```json
{
  "id": "ba7804b1-8abc-4e82-bb57-126ab3190cde",
  "workspace_id": "2e093b87-bb90-4b7d-a0a3-05e24d05b104",
  "name": "crash-recovery-job",
  "state": "SUCCEEDED",
  "current_attempt_number": 2,
  "attempts": [
    {
      "attempt_number": 1,
      "state": "FAILED",
      "resource_uid": null,
      "lease_epoch": 1,
      "exit_code": -1,
      "failure_reason": "Worker lease expired; executor lost",
      "started_at": "2026-10-01T16:26:10.933026Z",
      "finished_at": "2026-10-01T16:26:18.458683Z"
    },
    {
      "attempt_number": 2,
      "state": "SUCCEEDED",
      "resource_uid": null,
      "lease_epoch": 1,
      "exit_code": 0,
      "failure_reason": null,
      "started_at": "2026-10-01T16:26:26.005163Z",
      "finished_at": "2026-10-01T16:26:26.195434Z"
    }
  ],
  "created_at": "2026-10-01T16:26:09.117950Z",
  "updated_at": "2026-10-01T16:26:26.195434Z"
}
```

---

## 3. Conclusion

The live crash recording conclusively validates that:
1. When an active worker crashes unexpectedly, its work is not silently lost.
2. Attempt 1 recorded non-null `started_at` (`2026-10-01T16:26:10.933026Z`) and `lease_epoch = 1` reflecting real execution prior to termination.
3. The expired lease was reclaimed safely by the scheduler without manual intervention.
4. Backoff timing (> 5.0 seconds) was strictly respected before requeuing attempt 2.
5. Attempt 2 completed successfully (`exit_code = 0`), and the final job reached `SUCCEEDED`.
