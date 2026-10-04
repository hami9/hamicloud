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
{"id":"6ab9c9a7-fcbe-4b1a-974a-08d6d649597c","name":"Duplicate Submission Demo bafbfb21","slug":"demo-dup-bafbfb21","role":"OWNER","created_at":"2026-10-04T08:42:20.198305Z"}
```

---

### Step 2: First Job Submission with Idempotency-Key

Submitted via `POST /v1/workspaces/6ab9c9a7-fcbe-4b1a-974a-08d6d649597c/jobs` with `Idempotency-Key: idemp-demo-dup-bafbfb21`.

**Raw Response (`02-first-submission.json`):**
```json
{"operation_id":"ac08b01b-aa81-460f-9246-ef4486ca2444","status":"ACCEPTED","status_url":"/v1/operations/ac08b01b-aa81-460f-9246-ef4486ca2444"}
```

---

### Step 3: Duplicate Submission with Identical Key

Resubmitted identical payload to `POST /v1/workspaces/6ab9c9a7-fcbe-4b1a-974a-08d6d649597c/jobs` with the same `Idempotency-Key: idemp-demo-dup-bafbfb21`.

**Raw Response (`03-duplicate-submission.json`):**
```json
{"operation_id":"ac08b01b-aa81-460f-9246-ef4486ca2444","status":"ACCEPTED","status_url":"/v1/operations/ac08b01b-aa81-460f-9246-ef4486ca2444"}
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

The duplicate submission returned the exact same `operation_id` (`ac08b01b-aa81-460f-9246-ef4486ca2444`) without inserting duplicate jobs or execution intents in the database.
