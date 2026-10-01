# Demo: Duplicate Submission Idempotency

This demonstration verifies Milestone M2 exit criterion 6:
> Repeating the same submission returns the same operation.

Recorded from live API and runtime execution against PostgreSQL.

---

## 1. Environment & Setup

- **API Target:** `http://localhost:8000` (FastAPI)
- **Database:** PostgreSQL 16 Alpine (`hamicloud_test`)
- **Caller Identity:** `X-Dev-Subject: alice`

---

## 2. Step-by-Step Live Execution

### Step 1: Create Workspace

```http
POST /v1/workspaces HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice

{
  "name": "Duplicate Submission Demo",
  "slug": "demo-dup-a87557a3"
}
```

**Response:**
```http
HTTP/1.1 201 Created
Content-Type: application/json

{
  "id": "67ba6f66-c19b-45b1-acd0-d4eeb5504770",
  "name": "Duplicate Submission Demo",
  "slug": "demo-dup-a87557a3",
  "role": "OWNER",
  "created_at": "2026-10-01T14:14:41.883556Z"
}
```

### Step 2: First Job Submission with Idempotency-Key

```http
POST /v1/workspaces/67ba6f66-c19b-45b1-acd0-d4eeb5504770/jobs HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice
Idempotency-Key: idemp-demo-dup-fbc288e8ec2c

{
  "name": "data-aggregation",
  "image_digest": "docker.io/library/python:3.12-alpine",
  "command_args": ["python", "-c", "print('Processed records')"],
  "timeout_seconds": 60,
  "max_retries": 2
}
```

**Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "849e4a35-637c-41ee-b867-a64bac5d1160",
  "status": "ACCEPTED",
  "status_url": "/v1/operations/849e4a35-637c-41ee-b867-a64bac5d1160"
}
```

### Step 3: Duplicate Submission with Identical Key

```http
POST /v1/workspaces/67ba6f66-c19b-45b1-acd0-d4eeb5504770/jobs HTTP/1.1
Host: localhost:8000
Content-Type: application/json
X-Dev-Subject: alice
Idempotency-Key: idemp-demo-dup-fbc288e8ec2c

{
  "name": "data-aggregation",
  "image_digest": "docker.io/library/python:3.12-alpine",
  "command_args": ["python", "-c", "print('Processed records')"],
  "timeout_seconds": 60,
  "max_retries": 2
}
```

**Response:**
```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{
  "operation_id": "849e4a35-637c-41ee-b867-a64bac5d1160",
  "status": "ACCEPTED",
  "status_url": "/v1/operations/849e4a35-637c-41ee-b867-a64bac5d1160"
}
```

---

## 3. Database Integrity Verification

```sql
SELECT count(*) AS job_count
FROM jobs
WHERE workspace_id = '67ba6f66-c19b-45b1-acd0-d4eeb5504770';
```

**Result:**
```text
 job_count 
-----------
         1
(1 row)
```

The duplicate submission returned the exact same `operation_id` (`849e4a35-637c-41ee-b867-a64bac5d1160`) without inserting redundant job or execution intent records.
