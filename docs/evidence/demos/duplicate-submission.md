# Demo: Duplicate Submission Idempotency

This demonstration verifies Milestone M2 exit criterion 6:
> Repeating the same submission returns the same operation.

Recorded via automated script `scripts/demos/duplicate_submission.ps1` from live API and runtime execution against PostgreSQL.
All raw JSON and SQL outputs are captured in `docs/evidence/demos/raw/duplicate-submission/`.
**Reproduction command:**
```powershell
powershell -ExecutionPolicy Bypass -File ./scripts/demos/duplicate_submission.ps1
```

---

## 1. Environment & Setup

- **API Target:** `http://127.0.0.1:8088` (FastAPI)
- **Database:** PostgreSQL 16 Alpine (`hamicloud`)
- **Caller Identity:** `X-Dev-Subject: alice`

---

## 2. Step-by-Step Live Execution Quoting Raw Evidence

### Step 1: Create Workspace

Submitted via `POST /v1/workspaces`.

**Raw Response (`01-create-workspace.json`):**
```json
{"id":"9c3546cf-e751-4ae7-9c49-f844c3d54210","name":"Duplicate Submission Demo cb8f9d8d","slug":"demo-dup-cb8f9d8d","created_at":"2026-10-04T13:33:22.598109Z"}
```

---

### Step 2: First Job Submission with Idempotency-Key

Submitted via `POST /v1/workspaces/9c3546cf-e751-4ae7-9c49-f844c3d54210/jobs` with `Idempotency-Key: idemp-demo-dup-cb8f9d8d`.

**Raw Response (`02-first-submission.json`):**
```json
{"operation_id":"5c35be01-6a8d-405f-b766-0a1918e1494d","status":"ACCEPTED","status_url":"/v1/operations/5c35be01-6a8d-405f-b766-0a1918e1494d"}
```

---

### Step 3: Duplicate Submission with Identical Key

Resubmitted identical payload to `POST /v1/workspaces/9c3546cf-e751-4ae7-9c49-f844c3d54210/jobs` with the same `Idempotency-Key: idemp-demo-dup-cb8f9d8d`.

**Raw Response (`03-duplicate-submission.json`):**
```json
{"operation_id":"5c35be01-6a8d-405f-b766-0a1918e1494d","status":"ACCEPTED","status_url":"/v1/operations/5c35be01-6a8d-405f-b766-0a1918e1494d"}
```

---

## 3. Database Integrity Verification

**Raw Database Query (`04-db-job-count.txt`):**
```text
 job_count 
-----------
         1
(1 row)
```

The database confirms that exactly **1** job row exists for workspace `9c3546cf-e751-4ae7-9c49-f844c3d54210` with ID `5c35be01-6a8d-405f-b766-0a1918e1494d`.

---

## 4. Conclusion

Both submissions returned the identical operation ID (`5c35be01-6a8d-405f-b766-0a1918e1494d`) and exactly 1 database job was recorded, conclusively verifying idempotency.
