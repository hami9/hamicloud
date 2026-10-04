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

- **Workspace ID:** `0ac9d44e-c821-4f13-b401-05519de1bdb7`
- **Job ID:** `4751554d-3a0d-4db5-a2bd-9deeaf874337`

---

## 2. Step-by-Step Execution Quoting Raw Evidence

### Step 1: Submit Long-Running Job

Submitted via `POST /v1/workspaces/0ac9d44e-c821-4f13-b401-05519de1bdb7/jobs` with `Idempotency-Key: idemp-worker-restart-7f20b4d5` and command `python -c "import time; time.sleep(15)"`.

**Raw Response (`01-submit-job.json`):**
```json
{"operation_id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","status":"ACCEPTED","status_url":"/v1/operations/4751554d-3a0d-4db5-a2bd-9deeaf874337"}
```

---

### Step 2: Scheduler Admission (Attempt 1)

Executed `hamicloud-scheduler.exe --run-once`.

**Raw Log Output (`02-scheduler-admit-1.log`):**
```json
{"time":"2026-10-04T16:38:39.4603352+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-04T16:38:46.8615049+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:38:46.9492035+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-04T16:38:46.9492035+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-04T16:38:51.0344902+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"74f536a2-b89a-44e3-b3b3-ff6568356920","job_id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","job_attempt_id":"c536a771-fa9f-4472-b73a-7b9752dd2914","resource_name":"hc-job-4751554d-3a0d-4db5-a2bd-9deeaf874337-1"}
{"time":"2026-10-04T16:38:51.035994+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 3: Executor Claims Attempt 1 & Begins Running

The executor process was started with `WORKER_ID=executor-worker-crash-test`, `LEASE_DURATION_SECONDS=5`, and `WORKLOAD_WORKSPACE_ID=0ac9d44e-c821-4f13-b401-05519de1bdb7`.

**Raw Database Verification (`03-claimed-attempt-1-db.txt`):**
```text
                  id                  | status  |         claimed_by         | lease_epoch 
--------------------------------------+---------+----------------------------+-------------
 74f536a2-b89a-44e3-b3b3-ff6568356920 | CLAIMED | executor-worker-crash-test |           1
(1 row)
```

---

### Step 4: Abrupt Process Kill (Simulated Crash)

While attempt 1 was actively running the 15-second command, the worker process was abruptly terminated.

**Raw Action Record (`04-kill-worker.txt`):**
```text
Terminated worker process PID 14600 with Stop-Process -Force while executing 15s sleep
```

---

### Step 5: Wait for Lease Expiry

The demo waited 6.0 seconds (exceeding the 5.0-second lease window).

**Raw Database Query (`05-lease-expired-db.txt`):**
```text
                  id                  | status  |         claimed_by         | lease_epoch | is_expired 
--------------------------------------+---------+----------------------------+-------------+------------
 74f536a2-b89a-44e3-b3b3-ff6568356920 | CLAIMED | executor-worker-crash-test |           1 | t
(1 row)
```

---

### Step 6: Scheduler Recovery Pass

Executed `hamicloud-scheduler.exe --run-once` to detect the abandoned lease and recover the job.

**Raw Log Output (`06-scheduler-recovery.log`):**
```json
{"time":"2026-10-04T16:39:06.9369315+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-04T16:39:06.9805843+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:39:06.9805843+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-04T16:39:06.9805843+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-04T16:39:07.2440216+03:30","level":"INFO","msg":"Recovered expired job intents from crashed workers","count":1}
{"time":"2026-10-04T16:39:07.2669882+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":0}
```

**Raw Database State (`07-db-after-recovery.txt`):**
```text
                  id                  |   state    | current_attempt_number 
--------------------------------------+------------+------------------------
 4751554d-3a0d-4db5-a2bd-9deeaf874337 | RETRY_WAIT |                      1
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
{"time":"2026-10-04T16:39:13.8064112+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-04T16:39:14.070637+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:39:14.070637+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-04T16:39:14.070637+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-04T16:39:14.1555418+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-10-04T16:39:14.2577706+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"9f107aea-b67c-4d96-b91d-46842578a603","job_id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","job_attempt_id":"9cc39aca-5774-4952-bcfd-1595e0b5c4aa","resource_name":"hc-job-4751554d-3a0d-4db5-a2bd-9deeaf874337-2"}
{"time":"2026-10-04T16:39:14.2577706+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 9: Fresh Executor Claims & Completes Attempt 2

Executed `hamicloud-executor.exe --run-once` with `WORKLOAD_WORKSPACE_ID=0ac9d44e-c821-4f13-b401-05519de1bdb7` on attempt 2. The command ran for its full 15 seconds.

**Raw Log Output (`09-executor-attempt-2.log`):**
```json
{"time":"2026-10-04T16:39:14.3058102+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-04T16:39:14.4763606+03:30","level":"WARN","msg":"Kubernetes cluster unavailable; falling back to development-only HTTPProbeRunner and LocalProcessJobRunner","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:39:14.4763606+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true,"workspace_ids":["0ac9d44e-c821-4f13-b401-05519de1bdb7"]}
{"time":"2026-10-04T16:39:14.4763606+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-04T16:39:14.6034117+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"9f107aea-b67c-4d96-b91d-46842578a603","job_id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","job_name":"crash-recovery-job","attempt_number":2,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-04T16:39:14.6034888+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"9f107aea-b67c-4d96-b91d-46842578a603","job_id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","job_name":"crash-recovery-job","attempt_number":2,"lease_epoch":1}
{"time":"2026-10-04T16:39:29.7308267+03:30","level":"INFO","msg":"Job attempt SUCCEEDED with exit code 0","job_id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","attempt_number":2,"resource_uid":"res-job-4751554d-2"}
{"time":"2026-10-04T16:39:29.827905+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

Between claim (`16:39:14.603`) and completion (`16:39:29.730`), attempt 2 executed for 15.13 seconds to run `time.sleep(15)`.

---

### Step 10: Final State Verification via API & Database

**Raw API Response from `GET /v1/jobs/4751554d-3a0d-4db5-a2bd-9deeaf874337` (`10-get-job-final.json`):**
```json
{"id":"4751554d-3a0d-4db5-a2bd-9deeaf874337","workspace_id":"0ac9d44e-c821-4f13-b401-05519de1bdb7","name":"crash-recovery-job","state":"SUCCEEDED","current_attempt_number":2,"attempts":[{"attempt_number":1,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":-1,"failure_reason":"Worker lease expired; executor lost","started_at":"2026-10-04T13:08:58.641588Z","finished_at":"2026-10-04T13:09:07.012234Z"},{"attempt_number":2,"state":"SUCCEEDED","resource_uid":null,"lease_epoch":1,"exit_code":0,"failure_reason":null,"started_at":"2026-10-04T13:09:14.492449Z","finished_at":"2026-10-04T13:09:29.731882Z"}],"created_at":"2026-10-04T13:06:23.809055Z"}
```

**Raw Database Attempts Query (`11-db-final-attempts.txt`):**
```text
 attempt_number |   state   | exit_code |           failure_reason            |          started_at           |          finished_at          
----------------+-----------+-----------+-------------------------------------+-------------------------------+-------------------------------
              1 | FAILED    |        -1 | Worker lease expired; executor lost | 2026-10-04 13:08:58.641588+00 | 2026-10-04 13:09:07.012234+00
              2 | SUCCEEDED |         0 |                                     | 2026-10-04 13:09:14.492449+00 | 2026-10-04 13:09:29.731882+00
(2 rows)
```

---

## 3. Conclusion

The recording conclusively demonstrates that:
1. When a worker process crashes abruptly, the job is not silently lost.
2. Attempt 1 recorded real execution before death: non-null `started_at` (`13:08:58.641588Z`), `lease_epoch = 1`, and status `CLAIMED`.
3. The expired lease was recovered by the scheduler, setting attempt 1 to `FAILED` (`exit_code = -1`, `failure_reason = 'Worker lease expired; executor lost'`) and the job to `RETRY_WAIT`.
4. Retry backoff (> 5.0s) was strictly observed before requeuing attempt 2.
5. Attempt 2 ran the full 15-second command (`13:09:14.492449Z` to `13:09:29.731882Z`, 15.24s) to clean success (`exit_code = 0`), reaching terminal state `SUCCEEDED`.
