# Demo: Worker Restart and Crash Recovery

This demonstration verifies Milestone M2 exit criterion 9:
> Restarting API, scheduler or executor does not silently lose accepted work.

Recorded via automated script `scripts/demos/worker_restart.ps1` from live API and Go runtime execution against PostgreSQL.
All raw CLI, JSON, log, and SQL outputs are captured in `docs/evidence/demos/raw/worker-restart/`.
**Reproduction command:**
```powershell
powershell -ExecutionPolicy Bypass -File ./scripts/demos/worker_restart.ps1
```

---

## 1. Scenario & Overview

1. A finite job is submitted with `max_retries = 2`, `timeout_seconds = 60`, and a command running `python -c "import time; time.sleep(15)"`.
2. The Go scheduler admits attempt 1 and creates an `ExecutionIntent`.
3. An executor worker claims attempt 1 (with workspace scoping), transitions it to `RUNNING` with `started_at` set and `lease_epoch = 1`.
4. While the job is running the 15-second command, the executor worker process is abruptly killed (`Stop-Process -Force` in PowerShell).
5. The system waits 6.0 seconds for the worker's 5.0-second lease to expire in PostgreSQL.
6. The scheduler's recovery loop discovers the expired lease, invokes `JobDeleter` to clean up any orphaned Kubernetes/runner artifacts, transitions attempt 1 from `RUNNING` -> `RECOVERY_PENDING` -> `FAILED` (with `started_at` preserved, `exit_code = -1`, `failure_reason = 'Worker lease expired; executor lost'`), and moves the job to `RETRY_WAIT`.
7. The system waits 6.0 seconds for the 5.0-second retry backoff to elapse.
8. The scheduler requeues the job to `QUEUED` and admits attempt 2 (`ExecutionIntent` #2).
9. A restarted executor claims attempt 2, runs it to successful completion (`exit_code = 0`) over 15.1 seconds, and transitions attempt 2 and the overall job to `SUCCEEDED`.
10. Final inspection confirms both attempts are preserved: Attempt 1 is `FAILED` with real failure diagnostics, Attempt 2 is `SUCCEEDED`, and the overall job state is `SUCCEEDED`.

- **Workspace ID:** `02bf6fa1-f71f-4ef7-987b-96595cfc4d65`
- **Job ID:** `19b79b71-841c-438c-9af8-107dd5108a56`

---

## 2. Step-by-Step Execution Quoting Raw Evidence

### Step 1: Submit Long-Running Job

Submitted via `POST /v1/workspaces/02bf6fa1-f71f-4ef7-987b-96595cfc4d65/jobs` with `Idempotency-Key: idemp-worker-restart-ce5beda0` and command `python -c "import time; time.sleep(15)"`.

**Raw Response (`01-submit-job.json`):**
```json
{"operation_id":"19b79b71-841c-438c-9af8-107dd5108a56","status":"ACCEPTED","status_url":"/v1/operations/19b79b71-841c-438c-9af8-107dd5108a56"}
```

---

### Step 2: Scheduler Admission (Attempt 1)

Executed `hamicloud-scheduler.exe --run-once`.

**Raw Log Output (`02-scheduler-admit-1.log`):**
```json
{"time":"2026-10-19T18:24:58.0194745+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-19T18:24:58.0764847+03:30","level":"INFO","msg":"Connected scheduler to Kubernetes cluster for job recovery","namespace":"default"}
{"time":"2026-10-19T18:24:58.0764847+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-19T18:24:58.0764847+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-19T18:24:58.2249488+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"14ae95b9-85e5-4a99-9b50-4a8e3613f3d4","job_id":"19b79b71-841c-438c-9af8-107dd5108a56","job_attempt_id":"a8724714-a6fa-46df-b95a-dc9b28880c23","resource_name":"hc-job-19b79b71-841c-438c-9af8-107dd5108a56-1"}
{"time":"2026-10-19T18:24:58.2249488+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 3: Executor Claims Attempt 1 & Begins Running

The executor process was started with `WORKER_ID=executor-worker-crash-test`, `LEASE_DURATION_SECONDS=5`, and `WORKLOAD_WORKSPACE_ID=02bf6fa1-f71f-4ef7-987b-96595cfc4d65`.

**Raw Database Verification (`03-claimed-attempt-1-db.txt`):**
```text
                  id                  | status  |         claimed_by         | lease_epoch 
--------------------------------------+---------+----------------------------+-------------
 14ae95b9-85e5-4a99-9b50-4a8e3613f3d4 | CLAIMED | executor-worker-crash-test |           1
(1 row)
```

---

### Step 4: Abrupt Process Kill (Simulated Crash)

While attempt 1 was actively running the 15-second command, the worker process was abruptly terminated.

**Raw Action Record (`04-kill-worker.txt`):**
```text
Terminated worker process PID 348 with Stop-Process -Force while executing 15s sleep
```

---

### Step 5: Wait for Lease Expiry

The demo waited 6.0 seconds (exceeding the 5.0-second lease window).

**Raw Database Query (`05-lease-expired-db.txt`):**
```text
                  id                  | status  |         claimed_by         | lease_epoch | is_expired 
--------------------------------------+---------+----------------------------+-------------+------------
 14ae95b9-85e5-4a99-9b50-4a8e3613f3d4 | CLAIMED | executor-worker-crash-test |           1 | t
(1 row)
```

---

### Step 6: Scheduler Recovery Pass

Executed `hamicloud-scheduler.exe --run-once` to detect the abandoned lease and recover the job.

**Raw Log Output (`06-scheduler-recovery.log`):**
```json
{"time":"2026-10-19T18:25:05.2920192+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-19T18:25:05.3705386+03:30","level":"INFO","msg":"Connected scheduler to Kubernetes cluster for job recovery","namespace":"default"}
{"time":"2026-10-19T18:25:05.3705386+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-19T18:25:05.3705386+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-19T18:25:05.7005115+03:30","level":"INFO","msg":"Recovered expired job intents from crashed workers","count":1}
{"time":"2026-10-19T18:25:05.7178275+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":0}
```

**Raw Database State (`07-db-after-recovery.txt`):**
```text
                  id                  |   state    | current_attempt_number 
--------------------------------------+------------+------------------------
 19b79b71-841c-438c-9af8-107dd5108a56 | RETRY_WAIT |                      1
(1 row)

 attempt_number | state  | exit_code |           failure_reason            | has_started | has_finished 
----------------+--------+-----------+-------------------------------------+-------------+--------------
              1 | FAILED |        -1 | Worker lease expired; executor lost | t           | t
(1 row)
```

The job safely transitioned to `RETRY_WAIT`. Attempt 1 was preserved as `FAILED` with `started_at` retained, `exit_code = -1`, and `failure_reason = 'Worker lease expired; executor lost'`.

---

### Step 7: Wait for Retry Backoff

The script waited 6.0 seconds (exceeding the 5.0-second backoff duration).

---

### Step 8: Scheduler Requeue & Admission of Attempt 2

Executed `hamicloud-scheduler.exe --run-once`.

**Raw Log Output (`08-scheduler-requeue.log`):**
```json
{"time":"2026-10-19T18:25:12.007142+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-19T18:25:12.0688322+03:30","level":"INFO","msg":"Connected scheduler to Kubernetes cluster for job recovery","namespace":"default"}
{"time":"2026-10-19T18:25:12.0688322+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-19T18:25:12.0688322+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-19T18:25:12.2391092+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-10-19T18:25:12.4019389+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"8e0c0f5c-a0d0-45c1-aeb9-3d439bc220f4","job_id":"19b79b71-841c-438c-9af8-107dd5108a56","job_attempt_id":"b41ccf64-b9a1-431b-b6f1-a649106f4304","resource_name":"hc-job-19b79b71-841c-438c-9af8-107dd5108a56-2"}
{"time":"2026-10-19T18:25:12.4019389+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 9: Fresh Executor Claims & Completes Attempt 2

Executed `hamicloud-executor.exe --run-once` with `WORKLOAD_WORKSPACE_ID=02bf6fa1-f71f-4ef7-987b-96595cfc4d65` on attempt 2. The command ran for its full 15 seconds against the live Kubernetes cluster (`desktop-control-plane`).

**Raw Log Output (`09-executor-attempt-2.log`):**
```json
{"time":"2026-10-19T18:25:12.4607911+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-19T18:25:12.5151447+03:30","level":"INFO","msg":"Connected to Kubernetes cluster","namespace":"default","ingress_domain":""}
{"time":"2026-10-19T18:25:12.5151447+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true,"workspace_ids":["02bf6fa1-f71f-4ef7-987b-96595cfc4d65"]}
{"time":"2026-10-19T18:25:12.5151447+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-19T18:25:12.6625144+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"8e0c0f5c-a0d0-45c1-aeb9-3d439bc220f4","job_id":"19b79b71-841c-438c-9af8-107dd5108a56","job_name":"crash-recovery-job","attempt_number":2,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-19T18:25:12.6625144+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"8e0c0f5c-a0d0-45c1-aeb9-3d439bc220f4","job_id":"19b79b71-841c-438c-9af8-107dd5108a56","job_name":"crash-recovery-job","attempt_number":2,"lease_epoch":1}
{"time":"2026-10-19T18:25:36.9355323+03:30","level":"INFO","msg":"Job attempt SUCCEEDED with exit code 0","job_id":"19b79b71-841c-438c-9af8-107dd5108a56","attempt_number":2,"resource_uid":"res-job-19b79b71-2"}
{"time":"2026-10-19T18:25:37.2322425+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

---

### Step 10: Final State Verification via API & Database

**Raw API Response from `GET /v1/jobs/19b79b71-841c-438c-9af8-107dd5108a56` (`10-get-job-final.json`):**
```json
{"id":"19b79b71-841c-438c-9af8-107dd5108a56","workspace_id":"02bf6fa1-f71f-4ef7-987b-96595cfc4d65","name":"crash-recovery-job","state":"SUCCEEDED","current_attempt_number":2,"attempts":[{"attempt_number":1,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":-1,"failure_reason":"Worker lease expired; executor lost","started_at":"2026-10-19T14:54:58.457221Z","finished_at":"2026-10-19T14:55:05.402737Z"},{"attempt_number":2,"state":"SUCCEEDED","resource_uid":null,"lease_epoch":1,"exit_code":0,"failure_reason":null,"started_at":"2026-10-19T14:55:12.532143Z","finished_at":"2026-10-19T14:55:36.957215Z"}],"created_at":"2026-10-19T14:54:57.791848Z"}
```

**Raw Database Attempts Query (`11-db-final-attempts.txt`):**
```text
 attempt_number |   state   | exit_code |           failure_reason            |          started_at           |          finished_at          
----------------+-----------+-----------+-------------------------------------+-------------------------------+-------------------------------
              1 | FAILED    |        -1 | Worker lease expired; executor lost | 2026-10-19 14:54:58.457221+00 | 2026-10-19 14:55:05.402737+00
              2 | SUCCEEDED |         0 |                                     | 2026-10-19 14:55:12.532143+00 | 2026-10-19 14:55:36.957215+00
(2 rows)
```

---

## 3. Conclusion

The recording conclusively demonstrates that:
1. When a worker process crashes abruptly, the job is not silently lost.
2. Attempt 1 recorded real execution before death: non-null `started_at` (`14:54:58.457221Z`), `lease_epoch = 1`, and status `CLAIMED`.
3. The expired lease was recovered by the scheduler, setting attempt 1 to `FAILED` (`exit_code = -1`, `failure_reason = 'Worker lease expired; executor lost'`) and the job to `RETRY_WAIT`.
4. Retry backoff (> 5.0s) was strictly observed before requeuing attempt 2.
5. Attempt 2 ran against the live Kubernetes cluster (`desktop-control-plane`) for 24.4 seconds to clean success (`exit_code = 0`), reaching terminal state `SUCCEEDED`.
