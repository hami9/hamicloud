import uuid
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import psycopg2

TEST_DB_SYNC = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test"


def test_dead_letter_endpoint_authorization_and_isolation(client: TestClient):
    """Test workspace authorization and isolation on dead-letter-records endpoint."""
    # 1. Alice creates workspace 1
    alice_headers = {"X-Dev-Subject": "alice_dlq_auth"}
    ws1_res = client.post(
        "/v1/workspaces",
        json={"name": "Alice DLQ Workspace", "slug": f"alice-dlq-{uuid.uuid4().hex[:6]}"},
        headers=alice_headers,
    )
    assert ws1_res.status_code == 201
    ws1_id = ws1_res.json()["id"]

    # 2. Bob (non-member) requests Alice's dead-letter records -> 404 Not Found
    bob_headers = {"X-Dev-Subject": "bob_dlq_auth"}
    bob_res = client.get(
        f"/v1/workspaces/{ws1_id}/dead-letter-records",
        headers=bob_headers,
    )
    assert bob_res.status_code == 404
    assert bob_res.json()["error_code"] == "NOT_FOUND"

    # 3. Alice requests her dead-letter records -> 200 OK
    alice_res = client.get(
        f"/v1/workspaces/{ws1_id}/dead-letter-records",
        headers=alice_headers,
    )
    assert alice_res.status_code == 200
    data = alice_res.json()
    assert data["items"] == []
    assert data["next_cursor"] is None


def test_dead_letter_record_created_and_listed(client: TestClient):
    """Verify dead-letter record creation upon job failure and retrieval via API."""
    alice_headers = {"X-Dev-Subject": "alice_dlq_create"}
    ws_res = client.post(
        "/v1/workspaces",
        json={"name": "Alice Job DLQ", "slug": f"alice-job-dlq-{uuid.uuid4().hex[:6]}"},
        headers=alice_headers,
    )
    assert ws_res.status_code == 201
    ws_id = ws_res.json()["id"]

    # Submit job
    submit_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={
            "name": "failing-batch-job",
            "image_digest": "docker.io/library/alpine:3.20@sha256:beefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeef",
            "command_args": ["sh", "-c", "exit 1"],
            "max_retries": 1,
        },
        headers={**alice_headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert submit_res.status_code == 202
    job_id = submit_res.json()["operation_id"]

    # Simulate worker moving job to FAILED in database (transactional update)
    with psycopg2.connect(TEST_DB_SYNC) as conn:
        with conn.cursor() as cur:
            # Create attempt 1 as failed
            cur.execute(
                """
                INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, exit_code, failure_reason, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    job_id,
                    ws_id,
                    1,
                    "FAILED",
                    1,
                    137,
                    "OOMKilled: container exceeded memory limit",
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                ),
            )
            # Update job to FAILED with current_attempt_number=1
            cur.execute(
                """
                UPDATE jobs
                SET state = 'FAILED', current_attempt_number = 1, updated_at = %s
                WHERE id = %s
                """,
                (datetime.now(timezone.utc), job_id),
            )
            # Insert dead_letter_records (guaranteed by trigger or runtime store)
            # Verify trigger or insert
            cur.execute(
                """
                SELECT id, job_id, workspace_id, last_attempt, exit_code, failure_reason
                FROM dead_letter_records
                WHERE job_id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            assert row is not None, "Dead-letter record must be created when job moves to FAILED"
            assert str(row[1]) == str(job_id)
            assert str(row[2]) == str(ws_id)
            assert row[3] == 1
            assert row[4] == 137
            assert "OOMKilled" in row[5]

    # Query API endpoint
    dlq_res = client.get(
        f"/v1/workspaces/{ws_id}/dead-letter-records",
        headers=alice_headers,
    )
    assert dlq_res.status_code == 200
    dlq_data = dlq_res.json()
    assert len(dlq_data["items"]) == 1
    item = dlq_data["items"][0]
    assert item["job_id"] == str(job_id)
    assert item["workspace_id"] == str(ws_id)
    assert item["job_name"] == "failing-batch-job"
    assert item["last_attempt"] == 1
    assert item["exit_code"] == 137
    assert "OOMKilled" in item["failure_reason"]
    assert item["created_at"] is not None


def test_dead_letter_cursor_pagination(client: TestClient):
    """Verify cursor pagination and ordering on dead-letter-records endpoint."""
    alice_headers = {"X-Dev-Subject": "alice_dlq_pag"}
    ws_res = client.post(
        "/v1/workspaces",
        json={"name": "Alice Pag DLQ", "slug": f"alice-dlq-pag-{uuid.uuid4().hex[:6]}"},
        headers=alice_headers,
    )
    assert ws_res.status_code == 201
    ws_id = ws_res.json()["id"]

    # Create 3 jobs and move them to FAILED
    job_ids = []
    for i in range(3):
        res = client.post(
            f"/v1/workspaces/{ws_id}/jobs",
            json={
                "name": f"batch-job-{i}",
                "image_digest": "docker.io/library/alpine:3.20@sha256:beefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeefbeef",
                "max_retries": 1,
            },
            headers={**alice_headers, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert res.status_code == 202
        job_ids.append(res.json()["operation_id"])

    with psycopg2.connect(TEST_DB_SYNC) as conn:
        with conn.cursor() as cur:
            for i, jid in enumerate(job_ids):
                cur.execute(
                    """
                    INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, exit_code, failure_reason, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(uuid.uuid4()),
                        jid,
                        ws_id,
                        1,
                        "FAILED",
                        1,
                        i + 1,
                        f"Error reason {i}",
                        datetime.now(timezone.utc),
                        datetime.now(timezone.utc),
                    ),
                )
                cur.execute(
                    """
                    UPDATE jobs SET state = 'FAILED', current_attempt_number = 1 WHERE id = %s
                    """,
                    (jid,),
                )

    # 1. Fetch first page with limit=2
    page1_res = client.get(
        f"/v1/workspaces/{ws_id}/dead-letter-records?limit=2",
        headers=alice_headers,
    )
    assert page1_res.status_code == 200
    page1 = page1_res.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] is not None

    # 2. Fetch second page with cursor
    page2_res = client.get(
        f"/v1/workspaces/{ws_id}/dead-letter-records?limit=2&cursor={page1['next_cursor']}",
        headers=alice_headers,
    )
    assert page2_res.status_code == 200
    page2 = page2_res.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None

    # Assert distinct items and total count = 3
    retrieved_job_ids = [item["job_id"] for item in page1["items"] + page2["items"]]
    assert len(set(retrieved_job_ids)) == 3
    for jid in job_ids:
        assert str(jid) in retrieved_job_ids
