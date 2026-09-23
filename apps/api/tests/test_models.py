import json
import uuid
import psycopg2
import pytest
from tests.conftest import TEST_DATABASE_URL_SYNC


def _get_connection():
    return psycopg2.connect(TEST_DATABASE_URL_SYNC)


@pytest.fixture
def clean_db():
    conn = _get_connection()
    conn.autocommit = True
    cur = conn.cursor()
    # Create a test workspace for the constraints suite
    ws_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (%s, %s, %s, NOW(), NOW())",
        (ws_id, "Constraint Test WS", f"test-ws-{ws_id[:8]}"),
    )
    yield ws_id
    # Clean up workspace cascades to child tables
    cur.execute("DELETE FROM workspaces WHERE id = %s", (ws_id,))
    cur.close()
    conn.close()


def test_constraint_idempotency_uniqueness(clean_db):
    ws_id = clean_db
    conn = _get_connection()
    cur = conn.cursor()
    endpoint = "/v1/test-endpoint"
    idem_key = f"key-{uuid.uuid4()}"

    # First insert succeeds
    cur.execute(
        """
        INSERT INTO idempotency_records 
        (id, workspace_id, endpoint, idempotency_key, request_hash, response_code, response_body, expires_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + interval '1 day', NOW(), NOW())
        """,
        (str(uuid.uuid4()), ws_id, endpoint, idem_key, "hash1", 202, json.dumps({"status": "ok"})),
    )
    conn.commit()

    # Second insert with identical (workspace_id, endpoint, idempotency_key) must be rejected
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO idempotency_records 
            (id, workspace_id, endpoint, idempotency_key, request_hash, response_code, response_body, expires_at, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + interval '1 day', NOW(), NOW())
            """,
            (str(uuid.uuid4()), ws_id, endpoint, idem_key, "hash2", 202, json.dumps({"status": "conflict"})),
        )
        conn.commit()

    assert "uq_idempotency_workspace_key" in str(exc_info.value)
    conn.rollback()
    conn.close()


def test_constraint_attempt_number_uniqueness(clean_db):
    ws_id = clean_db
    conn = _get_connection()
    cur = conn.cursor()
    job_id = str(uuid.uuid4())

    cur.execute(
        """
        INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        """,
        (job_id, ws_id, "Test Job", "sha256:111", json.dumps([]), json.dumps({}), 600, 3, 1, "RUNNING"),
    )
    conn.commit()

    # Attempt 1 succeeds
    cur.execute(
        """
        INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        """,
        (str(uuid.uuid4()), job_id, ws_id, 1, "RUNNING", 0),
    )
    conn.commit()

    # Duplicate attempt 1 must be rejected
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (str(uuid.uuid4()), job_id, ws_id, 1, "RUNNING", 1),
        )
        conn.commit()

    assert "uq_job_attempt_number" in str(exc_info.value)
    conn.rollback()
    conn.close()


def test_constraint_consumed_event_per_handler(clean_db):
    conn = _get_connection()
    cur = conn.cursor()
    event_id = str(uuid.uuid4())

    try:
        # Handler A consumes event -> succeeds
        cur.execute(
            "INSERT INTO consumed_events (id, event_id, handler, processed_at) VALUES (%s, %s, %s, NOW())",
            (str(uuid.uuid4()), event_id, "handler_alpha"),
        )
        conn.commit()

        # Handler B consumes same event -> succeeds (per-handler uniqueness)
        cur.execute(
            "INSERT INTO consumed_events (id, event_id, handler, processed_at) VALUES (%s, %s, %s, NOW())",
            (str(uuid.uuid4()), event_id, "handler_beta"),
        )
        conn.commit()

        # Handler A consumes same event again -> must be rejected
        with pytest.raises(psycopg2.IntegrityError) as exc_info:
            cur.execute(
                "INSERT INTO consumed_events (id, event_id, handler, processed_at) VALUES (%s, %s, %s, NOW())",
                (str(uuid.uuid4()), event_id, "handler_alpha"),
            )
            conn.commit()

        assert "uq_consumed_event_handler" in str(exc_info.value)
        conn.rollback()
    finally:
        try:
            conn.rollback()
            cur.execute("DELETE FROM consumed_events WHERE event_id = %s", (event_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            cur.close()
            conn.close()


def test_constraint_intent_uniqueness(clean_db):
    ws_id = clean_db
    conn = _get_connection()
    cur = conn.cursor()
    job_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())

    cur.execute(
        """
        INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        """,
        (job_id, ws_id, "Intent Job", "sha256:222", json.dumps([]), json.dumps({}), 600, 3, 1, "RUNNING"),
    )
    cur.execute(
        """
        INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        """,
        (attempt_id, job_id, ws_id, 1, "RUNNING", 0),
    )
    conn.commit()

    # Insert intent 1 for attempt_id at gen 1 -> succeeds
    cur.execute(
        """
        INSERT INTO execution_intents 
        (id, workspace_id, resource_type, job_attempt_id, release_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        """,
        (str(uuid.uuid4()), ws_id, "JOB_ATTEMPT", attempt_id, None, 1, f"intent-{attempt_id}-1", "PENDING", 0),
    )
    conn.commit()

    # Insert duplicate intent with same (resource_type, job_attempt_id, release_id, target_generation) -> must be rejected
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO execution_intents 
            (id, workspace_id, resource_type, job_attempt_id, release_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (str(uuid.uuid4()), ws_id, "JOB_ATTEMPT", attempt_id, None, 1, f"intent-{attempt_id}-1-dup", "PENDING", 0),
        )
        conn.commit()

    assert "uq_execution_intents_target" in str(exc_info.value)
    conn.rollback()
    conn.close()


def test_constraint_orphan_workspace_id(clean_db):
    conn = _get_connection()
    cur = conn.cursor()
    orphan_ws_id = str(uuid.uuid4())

    try:
        # Attempting to insert an application with non-existent workspace_id must fail
        with pytest.raises(psycopg2.IntegrityError) as exc_info:
            cur.execute(
                """
                INSERT INTO applications 
                (id, workspace_id, name, slug, workload_type, desired_generation, current_release_id, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (str(uuid.uuid4()), orphan_ws_id, "Orphan App", f"orphan-{orphan_ws_id[:8]}", "HTTP_SERVICE", 1, None),
            )
            conn.commit()

        assert "foreign key constraint" in str(exc_info.value).lower()
        conn.rollback()

        # Attempting to insert an outbox_event with non-existent workspace_id must fail
        with pytest.raises(psycopg2.IntegrityError) as exc_info:
            cur.execute(
                """
                INSERT INTO outbox_events 
                (id, event_id, workspace_id, schema_version, topic, payload_json, headers_json, status, retry_count, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                """,
                (str(uuid.uuid4()), str(uuid.uuid4()), orphan_ws_id, 1, "test.topic", json.dumps({}), json.dumps({}), "PENDING", 0),
            )
            conn.commit()

        assert "foreign key constraint" in str(exc_info.value).lower()
        conn.rollback()
    finally:
        try:
            conn.rollback()
            cur.execute("DELETE FROM applications WHERE workspace_id = %s", (orphan_ws_id,))
            cur.execute("DELETE FROM outbox_events WHERE workspace_id = %s", (orphan_ws_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            cur.close()
            conn.close()


def test_constraint_invalid_state_value(clean_db):
    ws_id = clean_db
    conn = _get_connection()
    cur = conn.cursor()

    # 1. Invalid job state
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (str(uuid.uuid4()), ws_id, "Bad State Job", "sha256:333", json.dumps([]), json.dumps({}), 600, 3, 0, "INVALID_STATE"),
        )
        conn.commit()
    assert "ck_jobs_state" in str(exc_info.value)
    conn.rollback()

    # 2. Invalid outbox status
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO outbox_events 
            (id, event_id, workspace_id, schema_version, topic, payload_json, headers_json, status, retry_count, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (str(uuid.uuid4()), str(uuid.uuid4()), ws_id, 1, "test.topic", json.dumps({}), json.dumps({}), "BOGUS_STATUS", 0),
        )
        conn.commit()
    assert "ck_outbox_events_status" in str(exc_info.value)
    conn.rollback()

    # 3. Invalid workspace membership role
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO workspace_memberships (id, workspace_id, user_subject, role, created_at, updated_at)
            VALUES (%s, %s, %s, %s, NOW(), NOW())
            """,
            (str(uuid.uuid4()), ws_id, "user-bad-role", "SUPER_ADMIN"),
        )
        conn.commit()
    assert "ck_workspace_memberships_role" in str(exc_info.value)
    conn.rollback()
    conn.close()


def test_constraint_execution_intent_typed_resource(clean_db):
    ws_id = clean_db
    conn = _get_connection()
    cur = conn.cursor()

    # Untyped intent (JOB_ATTEMPT without job_attempt_id) must fail CHECK constraint
    with pytest.raises(psycopg2.IntegrityError) as exc_info:
        cur.execute(
            """
            INSERT INTO execution_intents 
            (id, workspace_id, resource_type, job_attempt_id, release_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """,
            (str(uuid.uuid4()), ws_id, "JOB_ATTEMPT", None, None, 1, "untyped-intent", "PENDING", 0),
        )
        conn.commit()

    assert "ck_execution_intents_typed_resource" in str(exc_info.value)
    conn.rollback()
    conn.close()
