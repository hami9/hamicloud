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

- **Workspace ID:** `02403b64-8e41-4f2b-9f82-fc2eae910178`
- **Job ID:** `32d5c9e8-da02-4f44-be1e-289cb67a394f`
- **Declared Budget:** `max_retries = 1` (allowing attempt 1 plus exactly 1 retry)
- **Command:** `python -c "import sys; sys.exit(42)"`

---

## 2. Step-by-Step Live Execution Quoting Raw Evidence

### Step 1: Submit Failing Job

Submitted via `POST /v1/workspaces/02403b64-8e41-4f2b-9f82-fc2eae910178/jobs` with `Idempotency-Key: idemp-failed-job-1a8da57a` and `max_retries = 1`.

**Raw Response (`01-submit-job.json`):**
```json
{"operation_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","status":"ACCEPTED","status_url":"/v1/operations/32d5c9e8-da02-4f44-be1e-289cb67a394f"}
```

---

### Step 2: Attempt 1 Admission by Scheduler

Executed `hamicloud-scheduler.exe --run-once`.

**Raw Log Output (`02-scheduler-admit-1.log`):**
```json
{"time":"2026-10-03T19:06:44.2057796+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-03T19:06:44.2882772+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-03T19:06:44.2882772+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-03T19:06:44.2882772+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-03T19:06:44.8219642+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"30ce719b-c439-447d-8ae5-115f577cf479","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","job_attempt_id":"3157e3c1-0c58-45ae-952b-472e389df0b7","resource_name":"hc-job-32d5c9e8-da02-4f44-be1e-289cb67a394f-1"}
{"time":"2026-10-03T19:06:44.8229649+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 3: Attempt 1 Execution & Non-Zero Exit Code

Executed `hamicloud-executor.exe --run-once`.

**Raw Log Output (`03-executor-attempt-1.log`):**
```json
{"time":"2026-10-03T19:06:48.7909569+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-03T19:06:48.8899849+03:30","level":"WARN","msg":"Kubernetes cluster unavailable; falling back to development-only HTTPProbeRunner and LocalProcessJobRunner","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-03T19:06:48.8909874+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-03T19:06:48.8909874+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-03T19:06:49.0725515+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"30ce719b-c439-447d-8ae5-115f577cf479","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","job_name":"fatal-process","attempt_number":1,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-03T19:06:49.0725515+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"30ce719b-c439-447d-8ae5-115f577cf479","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","job_name":"fatal-process","attempt_number":1,"lease_epoch":1}
{"time":"2026-10-03T19:06:49.5483812+03:30","level":"WARN","msg":"Job attempt failed","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","attempt_number":1,"exit_code":42,"should_retry":true,"reason":"process exited with status 42"}
{"time":"2026-10-03T19:06:49.6961474+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

Since `attempt_number (1) <= max_retries (1)`, `should_retry` is `true`. The job safely transitioned to `RETRY_WAIT`.

---

### Step 4: Attempt 2 Requeue & Admission

Following the 6.0-second retry backoff window, `hamicloud-scheduler.exe --run-once` was executed.

**Raw Log Output (`04-scheduler-requeue.log`):**
```json
{"time":"2026-10-03T19:06:55.7766345+03:30","level":"INFO","msg":"Starting HamiCloud Scheduler process","version":"0.1.0"}
{"time":"2026-10-03T19:06:55.8569837+03:30","level":"WARN","msg":"Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-03T19:06:55.8569837+03:30","level":"INFO","msg":"Scheduler initialized with configuration","environment":"development","reconciliation_period":30000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-03T19:06:55.8569837+03:30","level":"INFO","msg":"Executing single scheduler admission pass (RUN_ONCE)"}
{"time":"2026-10-03T19:06:56.0963777+03:30","level":"INFO","msg":"Requeued retry_wait jobs back to QUEUED for next attempt","count":1}
{"time":"2026-10-03T19:06:56.2427494+03:30","level":"INFO","msg":"Admitted job and created ExecutionIntent","intent_id":"391d124e-8d9d-4b84-a72a-1756cabf98bd","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","job_attempt_id":"341e3d3f-56bb-498c-851f-28784a9e52ce","resource_name":"hc-job-32d5c9e8-da02-4f44-be1e-289cb67a394f-2"}
{"time":"2026-10-03T19:06:56.2432578+03:30","level":"INFO","msg":"Scheduler RunOnce completed successfully","admitted_count":1}
```

---

### Step 5: Attempt 2 Execution & Retry Budget Exhaustion

Executed `hamicloud-executor.exe --run-once` on attempt 2.

**Raw Log Output (`05-executor-attempt-2.log`):**
```json
{"time":"2026-10-03T19:07:00.3813094+03:30","level":"INFO","msg":"Starting HamiCloud Execution Worker process","version":"0.1.0"}
{"time":"2026-10-03T19:07:00.8439049+03:30","level":"WARN","msg":"Kubernetes cluster unavailable; falling back to development-only HTTPProbeRunner and LocalProcessJobRunner","warning":"in-cluster kubernetes configuration not available: unable to load in-cluster configuration, KUBERNETES_SERVICE_HOST and KUBERNETES_SERVICE_PORT must be defined"}
{"time":"2026-10-03T19:07:00.8439049+03:30","level":"INFO","msg":"Executor initialized with configuration","environment":"development","lease_duration":60000000000,"worker_id":"local-worker-1","run_once":true}
{"time":"2026-10-03T19:07:00.8439049+03:30","level":"INFO","msg":"Executing single executor reconciliation pass (RUN_ONCE)"}
{"time":"2026-10-03T19:07:01.0021612+03:30","level":"INFO","msg":"Claimed job attempt intent","intent_id":"391d124e-8d9d-4b84-a72a-1756cabf98bd","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","job_name":"fatal-process","attempt_number":2,"worker_id":"local-worker-1","lease_epoch":1}
{"time":"2026-10-03T19:07:01.0021612+03:30","level":"INFO","msg":"Starting reconciliation for job attempt","intent_id":"391d124e-8d9d-4b84-a72a-1756cabf98bd","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","job_name":"fatal-process","attempt_number":2,"lease_epoch":1}
{"time":"2026-10-03T19:07:01.4758604+03:30","level":"WARN","msg":"Job attempt failed","job_id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","attempt_number":2,"exit_code":42,"should_retry":false,"reason":"process exited with status 42"}
{"time":"2026-10-03T19:07:01.7405861+03:30","level":"INFO","msg":"Executor RunOnce completed successfully","workload_processed":true}
```

With `attempt_number = 2` exceeding `max_retries = 1`, `should_retry` evaluates to `false`. The job transitions to terminal state `FAILED`.

---

### Step 6: Final Verification via API & Database

**Raw API Response (`06-get-job-final.json`):**
```json
{"id":"32d5c9e8-da02-4f44-be1e-289cb67a394f","workspace_id":"02403b64-8e41-4f2b-9f82-fc2eae910178","name":"fatal-process","state":"FAILED","current_attempt_number":2,"attempts":[{"attempt_number":1,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":42,"failure_reason":"process exited with status 42","started_at":"2026-10-03T15:36:44.861565Z","finished_at":"2026-10-03T15:36:49.549383Z"},{"attempt_number":2,"state":"FAILED","resource_uid":null,"lease_epoch":1,"exit_code":42,"failure_reason":"process exited with status 42","started_at":"2026-10-03T15:37:00.869699Z","finished_at":"2026-10-03T15:37:01.476860Z"}],"created_at":"2026-10-03T15:35:12.841164Z"}
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
