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
{"id":"b53cb358-907a-4ce5-af4f-b5cad990a643","name":"Duplicate Submission Demo 518fa599","slug":"demo-dup-518fa599","created_at":"2026-10-19T14:48:09.728648Z"}
```

---

### Step 2: First Job Submission with Idempotency-Key

Submitted via `POST /v1/workspaces/b53cb358-907a-4ce5-af4f-b5cad990a643/jobs` with `Idempotency-Key: idemp-demo-dup-518fa599`.

**Raw Response (`02-first-submission.json`):**
```json
{"operation_id":"11b66a04-7bc9-4883-91df-d314893a3904","status":"ACCEPTED","status_url":"/v1/operations/11b66a04-7bc9-4883-91df-d314893a3904"}
```

---

### Step 3: Duplicate Submission with Identical Key

Resubmitted identical payload to `POST /v1/workspaces/b53cb358-907a-4ce5-af4f-b5cad990a643/jobs` with the same `Idempotency-Key: idemp-demo-dup-518fa599`.

**Raw Response (`03-duplicate-submission.json`):**
```json
{"operation_id":"11b66a04-7bc9-4883-91df-d314893a3904","status":"ACCEPTED","status_url":"/v1/operations/11b66a04-7bc9-4883-91df-d314893a3904"}
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

The database confirms that exactly **1** job row exists for workspace `b53cb358-907a-4ce5-af4f-b5cad990a643` with ID `11b66a04-7bc9-4883-91df-d314893a3904`.

---

## 4. Conclusion

Both submissions returned the identical operation ID (`11b66a04-7bc9-4883-91df-d314893a3904`) and exactly 1 database job was recorded, conclusively verifying idempotency.
