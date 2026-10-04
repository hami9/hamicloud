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

- **Workspace ID:** `32cb1deb-aff4-41a4-bc22-a5363d2ba182`
- **Job ID:** `456ad0ab-1c51-4665-87fc-461b798726ca`
- **Declared Budget:** `max_retries = 1` (allowing attempt 1 plus exactly 1 retry)
- **Command:** `python -c "import sys; sys.exit(42)"`

---

## 2. Step-by-Step Live Execution Quoting Raw Evidence

### Step 1: Submit Failing Job

Submitted via `POST /v1/workspaces/32cb1deb-aff4-41a4-bc22-a5363d2ba182/jobs` with `Idempotency-Key: idemp-failed-job-b896211e` and `max_retries = 1`.

**Raw Response (`01-submit-job.json`):**
```json
{"operation_id":"456ad0ab-1c51-4665-87fc-461b798726ca","status":"ACCEPTED","status_url":"/v1/operations/456ad0ab-1c51-4665-87fc-461b798726ca"}
```

---

### Step 2: Attempt 1 Admission by Scheduler

Executed `hamicloud-scheduler.exe --run-once`.

**Raw Log Output (`02-scheduler-admit-1.log`):**
```json
{"time":"2026-10-04T16:41:13.640781+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-04T16:41:13.6916441+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:41:13.6926403+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-04T16:41:13.6926403+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-04T16:41:13.8243307+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"4f7fcb9e-41ad-489c-90bd-708f59e086b7","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","job_attempt_id":"08fa9e64-aaf0-45f1-b59b-5858a62fb76a","resource_name":"hc-job-456ad0ab-1c51-4665-87fc-461b798726ca-1"}
{"time":"2026-10-04T16:41:13.8249856+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 3: Attempt 1 Execution & Non-Zero Exit Code

Executed `hamicloud-executor.exe --run-once` with `WORKLOAD_WORKSPACE_ID=32cb1deb-aff4-41a4-bc22-a5363d2ba182`.

**Raw Log Output (`03-executor-attempt-1.log`):**
```json
{"time":"2026-10-04T16:41:13.8827397+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-04T16:41:14.0433579+03:30","level":"WARN","msg":"Kubernetes cluster unavailable; falling back to development-only HTTPProbeRunner and LocalProcessJobRunner","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:41:14.0433579+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true,"workspace_ids":["32cb1deb-aff4-41a4-bc22-a5363d2ba182"]}
{"time":"2026-10-04T16:41:14.0433579+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-04T16:41:14.1368657+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"4f7fcb9e-41ad-489c-90bd-708f59e086b7","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","job_name":"fatal-process","attempt_number":1,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-04T16:41:14.1368657+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"4f7fcb9e-41ad-489c-90bd-708f59e086b7","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","job_name":"fatal-process","attempt_number":1,"lease_epoch":1}
{"time":"2026-10-04T16:41:14.3728855+03:30","level":"WARN","msg":"Job attempt failed","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","attempt_number":1,"exit_code":42,"should_retry":true,"reason":"process exited with status 42"}
{"time":"2026-10-04T16:41:14.5570225+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

Since `attempt_number (1) <= max_retries (1)`, `should_retry` is `true`. The job safely transitioned to `RETRY_WAIT`.

---

### Step 4: Attempt 2 Requeue & Admission

Following the 6.0-second retry backoff window, `hamicloud-scheduler.exe --run-once` was executed.

**Raw Log Output (`04-scheduler-requeue.log`):**
```json
{"time":"2026-10-04T16:41:20.6089454+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-04T16:41:20.8388928+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:41:20.839411+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-04T16:41:20.839411+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-04T16:41:20.9537449+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-10-04T16:41:21.1918237+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"0590957e-304f-4f56-bc96-b5add0760423","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","job_attempt_id":"8ad70ffb-ff31-47bd-9b19-f27e254b0359","resource_name":"hc-job-456ad0ab-1c51-4665-87fc-461b798726ca-2"}
{"time":"2026-10-04T16:41:21.1926249+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 5: Attempt 2 Execution & Retry Budget Exhaustion

Executed `hamicloud-executor.exe --run-once` with `WORKLOAD_WORKSPACE_ID=32cb1deb-aff4-41a4-bc22-a5363d2ba182` on attempt 2.

**Raw Log Output (`05-executor-attempt-2.log`):**
```json
{"time":"2026-10-04T16:41:21.2538364+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-04T16:41:21.3112561+03:30","level":"WARN","msg":"Kubernetes cluster unavailable; falling back to development-only HTTPProbeRunner and LocalProcessJobRunner","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-04T16:41:21.3112561+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true,"workspace_ids":["32cb1deb-aff4-41a4-bc22-a5363d2ba182"]}
{"time":"2026-10-04T16:41:21.3112561+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-04T16:41:21.4199355+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"0590957e-304f-4f56-bc96-b5add0760423","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","job_name":"fatal-process","attempt_number":2,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-04T16:41:21.4199355+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"0590957e-304f-4f56-bc96-b5add0760423","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","job_name":"fatal-process","attempt_number":2,"lease_epoch":1}
{"time":"2026-10-04T16:41:21.5560215+03:30","level":"WARN","msg":"Job attempt failed","job_id":"456ad0ab-1c51-4665-87fc-461b798726ca","attempt_number":2,"exit_code":42,"should_retry":false,"reason":"process exited with status 42"}
{"time":"2026-10-04T16:41:21.5878425+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

With `attempt_number = 2` exceeding `max_retries = 1`, `should_retry` evaluates to `false`. The job transitions to terminal state `FAILED`.

---

### Step 6: Final Verification via API & Database

**Raw API Response (`06-get-job-final.json`):**
```json
{"id":"456ad0ab-1c51-4665-87fc-461b798726ca","workspace_id":"32cb1deb-aff4-41a4-bc22-a5363d2ba182","name":"fatal-process","state":"FAILED","current_attempt_number":2,"attempts":[{"attempt_number":1,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":42,"failure_reason":"process exited with status 42","started_at":"2026-10-04T13:11:14.071120Z","finished_at":"2026-10-04T13:11:14.405964Z"},{"attempt_number":2,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":42,"failure_reason":"process exited with status 42","started_at":"2026-10-04T13:11:21.335954Z","finished_at":"2026-10-04T13:11:21.557009Z"}],"created_at":"2026-10-04T13:11:13.372441Z"}
```

**Raw Database Attempts Query (`07-db-attempts.txt`):**
```text
 attempt_number | state  | exit_code |        failure_reason         | has_started | has_finished 
----------------+--------+-----------+-------------------------------+-------------+--------------
              1 | FAILED |        42 | process exited with status 42 | t           | t
              2 | FAILED |        42 | process exited with status 42 | t           | t
(2 rows)
```

The declared retry budget (`max_retries = 1`) was respected: exactly 2 attempts were executed, diagnostic exit codes (`42`) and failure reasons were captured, and the job terminated cleanly in `FAILED`.
