# Demo: Failed Job and Retry Budget Exhaustion

This demonstration verifies Milestone M2 exit criterion 7:
> A controlled transient failure retries within the declared budget.

Recorded via automated script `scripts/demos/failed_job.ps1` from live API and Go runtime scheduler/executor execution against PostgreSQL.
All raw CLI, JSON, log, and SQL outputs are captured in `docs/evidence/demos/raw/failed-job/`.
**Reproduction command:**
```powershell
powershell -ExecutionPolicy Bypass -File ./scripts/demos/failed_job.ps1
```

---

## 1. Execution Overview

- **Workspace ID:** `b9f97564-f564-4204-b2f0-f9835e769bea`
- **Job ID:** `a6137530-d353-4152-9db8-6c5fd5d1b1ab`
- **Declared Budget:** `max_retries = 1` (allowing attempt 1 plus exactly 1 retry)
- **Command:** `python -c "import sys; sys.exit(42)"`

---

## 2. Step-by-Step Live Execution Quoting Raw Evidence

### Step 1: Submit Failing Job

Submitted via `POST /v1/workspaces/b9f97564-f564-4204-b2f0-f9835e769bea/jobs` with `Idempotency-Key: idemp-failed-job-af4b7e0a` and `max_retries = 1`.

**Raw Response (`01-submit-job.json`):**
```json
{"operation_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","status":"ACCEPTED","status_url":"/v1/operations/a6137530-d353-4152-9db8-6c5fd5d1b1ab"}
```

---

### Step 2: Attempt 1 Admission by Scheduler

Executed `hamicloud-scheduler.exe --run-once`.

**Raw Log Output (`02-scheduler-admit-1.log`):**
```json
{"time":"2026-10-19T18:24:12.2374015+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-19T18:24:12.3296239+03:30","level":"INFO","msg":"Connected scheduler to Kubernetes cluster for job recovery","namespace":"default"}
{"time":"2026-10-19T18:24:12.330096+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-19T18:24:12.330096+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-19T18:24:12.4344675+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"64f68f77-a101-4885-8a3b-9d24952b5e20","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","job_attempt_id":"3a785b69-e303-4ea3-a0f6-ae0a69071c32","resource_name":"hc-job-a6137530-d353-4152-9db8-6c5fd5d1b1ab-1"}
{"time":"2026-10-19T18:24:12.4344675+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 3: Attempt 1 Execution & Non-Zero Exit Code

Executed `hamicloud-executor.exe --run-once` with `WORKLOAD_WORKSPACE_ID=b9f97564-f564-4204-b2f0-f9835e769bea` on live Kubernetes cluster (`desktop-control-plane`).

**Raw Log Output (`03-executor-attempt-1.log`):**
```json
{"time":"2026-10-19T18:24:12.4821427+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-19T18:24:12.5420903+03:30","level":"INFO","msg":"Connected to Kubernetes cluster","namespace":"default","ingress_domain":""}
{"time":"2026-10-19T18:24:12.5420903+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true,"workspace_ids":["b9f97564-f564-4204-b2f0-f9835e769bea"]}
{"time":"2026-10-19T18:24:12.5420903+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-19T18:24:12.6602682+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"64f68f77-a101-4885-8a3b-9d24952b5e20","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","job_name":"fatal-process","attempt_number":1,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-19T18:24:12.6602682+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"64f68f77-a101-4885-8a3b-9d24952b5e20","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","job_name":"fatal-process","attempt_number":1,"lease_epoch":1}
{"time":"2026-10-19T18:24:21.7527194+03:30","level":"WARN","msg":"Job attempt failed","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","attempt_number":1,"exit_code":42,"should_retry":true,"reason":"container exited with code 42: Error"}
{"time":"2026-10-19T18:24:21.8067113+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

Since `attempt_number (1) <= max_retries (1)`, `should_retry` is `true`. The job safely transitioned to `RETRY_WAIT`.

---

### Step 4: Attempt 2 Requeue & Admission

Following the 6.0-second retry backoff window, `hamicloud-scheduler.exe --run-once` was executed.

**Raw Log Output (`04-scheduler-requeue.log`):**
```json
{"time":"2026-10-19T18:24:27.8467421+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-19T18:24:27.917855+03:30","level":"INFO","msg":"Connected scheduler to Kubernetes cluster for job recovery","namespace":"default"}
{"time":"2026-10-19T18:24:27.917855+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-19T18:24:27.917855+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-19T18:24:28.0223854+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-10-19T18:24:28.0921013+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"bd4a3e08-6994-4f87-a8a6-064f6779b5b0","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","job_attempt_id":"bbeb78d6-9006-41fa-b0a5-c3e2bb32f5b3","resource_name":"hc-job-a6137530-d353-4152-9db8-6c5fd5d1b1ab-2"}
{"time":"2026-10-19T18:24:28.0921013+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 5: Attempt 2 Execution & Retry Budget Exhaustion

Executed `hamicloud-executor.exe --run-once` with `WORKLOAD_WORKSPACE_ID=b9f97564-f564-4204-b2f0-f9835e769bea` on attempt 2.

**Raw Log Output (`05-executor-attempt-2.log`):**
```json
{"time":"2026-10-19T18:24:28.1500572+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-19T18:24:28.1973823+03:30","level":"INFO","msg":"Connected to Kubernetes cluster","namespace":"default","ingress_domain":""}
{"time":"2026-10-19T18:24:28.1983962+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true,"workspace_ids":["b9f97564-f564-4204-b2f0-f9835e769bea"]}
{"time":"2026-10-19T18:24:28.1983962+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-19T18:24:28.3102008+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"bd4a3e08-6994-4f87-a8a6-064f6779b5b0","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","job_name":"fatal-process","attempt_number":2,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-19T18:24:28.3102008+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"bd4a3e08-6994-4f87-a8a6-064f6779b5b0","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","job_name":"fatal-process","attempt_number":2,"lease_epoch":1}
{"time":"2026-10-19T18:24:36.9758722+03:30","level":"WARN","msg":"Job attempt failed","job_id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","attempt_number":2,"exit_code":42,"should_retry":false,"reason":"container exited with code 42: Error"}
{"time":"2026-10-19T18:24:37.0874878+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

With `attempt_number = 2` exceeding `max_retries = 1`, `should_retry` evaluates to `false`. The job transitions to terminal state `FAILED`.

---

### Step 6: Final Verification via API & Database

**Raw API Response (`06-get-job-final.json`):**
```json
{"id":"a6137530-d353-4152-9db8-6c5fd5d1b1ab","workspace_id":"b9f97564-f564-4204-b2f0-f9835e769bea","name":"fatal-process","state":"FAILED","current_attempt_number":2,"attempts":[{"attempt_number":1,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":42,"failure_reason":"container exited with code 42: Error","started_at":"2026-10-19T14:54:12.565139Z","finished_at":"2026-10-19T14:54:21.753488Z"},{"attempt_number":2,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":42,"failure_reason":"container exited with code 42: Error","started_at":"2026-10-19T14:54:28.214379Z","finished_at":"2026-10-19T14:54:36.976392Z"}],"created_at":"2026-10-19T14:54:09.468353Z"}
```

**Raw Database Attempts Query (`07-db-attempts.txt`):**
```text
 attempt_number | state  | exit_code |            failure_reason            | has_started | has_finished 
----------------+--------+-----------+--------------------------------------+-------------+--------------
              1 | FAILED |        42 | container exited with code 42: Error | t           | t
              2 | FAILED |        42 | container exited with code 42: Error | t           | t
(2 rows)
```

The declared retry budget (`max_retries = 1`) was respected: exactly 2 attempts were executed on the real Kubernetes cluster, diagnostic exit codes (`42`) and failure reasons (`container exited with code 42: Error`) were captured, and the job terminated cleanly in `FAILED`.
