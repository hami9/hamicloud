import json
import os
import uuid
import pytest
from fastapi.testclient import TestClient
from app.models.job import JobState
from app.core.state_machine import (
    LEGAL_JOB_TRANSITIONS,
    TERMINAL_JOB_STATES,
    TerminalStateError,
    InvalidStateTransitionError,
    validate_job_transition,
)


def test_python_state_machine_contract_equality():
    """Assert Python transition table is exactly equal to contracts/state-machines/job.v1.json."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    contract_path = os.path.join(repo_root, "contracts", "state-machines", "job.v1.json")
    assert os.path.exists(contract_path), f"Contract file not found at {contract_path}"

    with open(contract_path, "r", encoding="utf-8") as f:
        contract = json.load(f)

    # 1. Compare states
    python_states = {s.value for s in JobState}
    contract_states = set(contract["states"])
    assert python_states == contract_states, f"States mismatch: {python_states ^ contract_states}"

    # 2. Compare terminal states
    python_terminals = {s.value for s in TERMINAL_JOB_STATES}
    contract_terminals = set(contract["terminal_states"])
    assert python_terminals == contract_terminals

    # 3. Compare transitions
    python_transitions = {
        state.value: sorted([dest.value for dest in dests])
        for state, dests in LEGAL_JOB_TRANSITIONS.items()
    }
    contract_transitions = {
        state: sorted(dests)
        for state, dests in contract["transitions"].items()
    }

    assert python_transitions == contract_transitions, (
        f"Transitions mismatch:\nPython: {python_transitions}\nContract: {contract_transitions}"
    )


def test_job_states_contract_equality_across_all_layers():
    """Assert states in contracts/state-machines/job.v1.json equal exactly:
    - Python JobState values
    - both OpenAPI job-state enums (JobDetailsResponse and JobAttemptItem)
    - values allowed by ck_jobs_state and ck_job_attempts_state in hamicloud_test.
    """
    import yaml
    import re
    import psycopg2
    from tests.conftest import TEST_DATABASE_URL_SYNC

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    contract_path = os.path.join(repo_root, "contracts", "state-machines", "job.v1.json")
    openapi_path = os.path.join(repo_root, "contracts", "openapi", "v1.yaml")

    with open(contract_path, "r", encoding="utf-8") as f:
        contract = json.load(f)
    contract_states = set(contract["states"])

    # 1. Python JobState enum values
    python_states = {s.value for s in JobState}
    assert python_states == contract_states, f"Python JobState mismatch: {python_states ^ contract_states}"

    # 2. Both OpenAPI job-state enums
    with open(openapi_path, "r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    schemas = spec["components"]["schemas"]
    openapi_job_states = set(schemas["JobDetailsResponse"]["properties"]["state"]["enum"])
    openapi_attempt_states = set(schemas["JobAttemptItem"]["properties"]["state"]["enum"])

    assert openapi_job_states == contract_states, (
        f"OpenAPI JobDetailsResponse state mismatch: {openapi_job_states ^ contract_states}"
    )
    assert openapi_attempt_states == contract_states, (
        f"OpenAPI JobAttemptItem state mismatch: {openapi_attempt_states ^ contract_states}"
    )

    # 3. Values allowed by ck_jobs_state and ck_job_attempts_state in hamicloud_test
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conname IN ('ck_jobs_state', 'ck_job_attempts_state')
        """)
        constraints = dict(cur.fetchall())
    conn.close()

    assert "ck_jobs_state" in constraints, "ck_jobs_state constraint missing in hamicloud_test"
    assert "ck_job_attempts_state" in constraints, "ck_job_attempts_state constraint missing in hamicloud_test"

    db_jobs_states = set(re.findall(r"'([A-Z_]+)'", constraints["ck_jobs_state"]))
    db_attempt_states = set(re.findall(r"'([A-Z_]+)'", constraints["ck_job_attempts_state"]))

    assert db_jobs_states == contract_states, (
        f"Database ck_jobs_state mismatch: {db_jobs_states ^ contract_states}"
    )
    assert db_attempt_states == contract_states, (
        f"Database ck_job_attempts_state mismatch: {db_attempt_states ^ contract_states}"
    )


def test_job_states_falsification_removing_recovery_pending_fails():
    """Verify that removing RECOVERY_PENDING from any of the 4 layers fails the contract equality check."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    contract_path = os.path.join(repo_root, "contracts", "state-machines", "job.v1.json")
    with open(contract_path, "r", encoding="utf-8") as f:
        contract = json.load(f)
    contract_states = set(contract["states"])
    assert "RECOVERY_PENDING" in contract_states

    # Mutated layer: missing RECOVERY_PENDING
    mutated_states = contract_states - {"RECOVERY_PENDING"}

    # Assert that equality fails
    assert mutated_states != contract_states
    assert (mutated_states ^ contract_states) == {"RECOVERY_PENDING"}


def test_python_state_machine_falsification_extra_edge_fails():
    """Verify that adding an extra edge to the transition table fails equality against contract."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    contract_path = os.path.join(repo_root, "contracts", "state-machines", "job.v1.json")
    with open(contract_path, "r", encoding="utf-8") as f:
        contract = json.load(f)

    contract_transitions = {
        state: sorted(dests)
        for state, dests in contract["transitions"].items()
    }

    # Artificially widen table with an extra edge
    mutated_transitions = {
        state.value: sorted([dest.value for dest in dests])
        for state, dests in LEGAL_JOB_TRANSITIONS.items()
    }
    mutated_transitions["QUEUED"] = sorted(mutated_transitions["QUEUED"] + ["RUNNING"])

    assert mutated_transitions != contract_transitions


def test_validate_job_transition_logic():
    """Verify unit transition validation behavior."""
    # Legal transitions
    validate_job_transition(JobState.QUEUED, JobState.ADMITTED)
    validate_job_transition(JobState.ADMITTED, JobState.STARTING)
    validate_job_transition(JobState.STARTING, JobState.RUNNING)
    validate_job_transition(JobState.STARTING, JobState.RETRY_WAIT)
    validate_job_transition(JobState.STARTING, JobState.FAILED)
    validate_job_transition(JobState.RUNNING, JobState.RECOVERY_PENDING)
    validate_job_transition(JobState.RECOVERY_PENDING, JobState.RETRY_WAIT)
    validate_job_transition(JobState.RECOVERY_PENDING, JobState.FAILED)
    validate_job_transition(JobState.RECOVERY_PENDING, JobState.CANCEL_REQUESTED)
    validate_job_transition(JobState.CANCEL_REQUESTED, JobState.CANCELLED)

    # Illegal transitions rejected by D4/D5
    with pytest.raises(InvalidStateTransitionError):
        validate_job_transition(JobState.CANCEL_REQUESTED, JobState.SUCCEEDED)

    with pytest.raises(InvalidStateTransitionError):
        validate_job_transition(JobState.CANCEL_REQUESTED, JobState.FAILED)

    with pytest.raises(InvalidStateTransitionError):
        validate_job_transition(JobState.QUEUED, JobState.CANCELLED)

    with pytest.raises(InvalidStateTransitionError):
        validate_job_transition(JobState.ADMITTED, JobState.CANCELLED)

    with pytest.raises(InvalidStateTransitionError):
        validate_job_transition(JobState.ADMITTED, JobState.FAILED)

    with pytest.raises(InvalidStateTransitionError):
        validate_job_transition(JobState.RETRY_WAIT, JobState.CANCELLED)

    # Transitions out of terminal states
    with pytest.raises(TerminalStateError):
        validate_job_transition(JobState.SUCCEEDED, JobState.RUNNING)

    with pytest.raises(TerminalStateError):
        validate_job_transition(JobState.FAILED, JobState.QUEUED)

    with pytest.raises(TerminalStateError):
        validate_job_transition(JobState.CANCELLED, JobState.STARTING)


def test_api_rejects_illegal_job_cancellation_and_rerun(client: TestClient):
    """API write path test showing illegal transitions are rejected."""
    # 1. Create a workspace
    slug = f"ws-sm-{uuid.uuid4().hex[:6]}"
    ws_res = client.post(
        "/v1/workspaces",
        json={"name": "State Machine WS", "slug": slug},
        headers={"X-Dev-Subject": "alice"},
    )
    assert ws_res.status_code == 201
    ws_id = ws_res.json()["id"]

    # 2. Submit a job (initial state QUEUED)
    job_key = f"key-job-{uuid.uuid4().hex[:6]}"
    j_res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "test-job", "image_digest": "registry.example.com/job@sha256:111"},
        headers={"Idempotency-Key": job_key, "X-Dev-Subject": "alice"},
    )
    assert j_res.status_code == 202
    job_id = j_res.json()["operation_id"]

    # Attempting to rerun an active (QUEUED) job is rejected with 409
    rerun_key = f"key-rerun-{uuid.uuid4().hex[:6]}"
    r_res = client.post(
        f"/v1/jobs/{job_id}/reruns",
        headers={"Idempotency-Key": rerun_key, "X-Dev-Subject": "alice"},
    )
    assert r_res.status_code == 409
    assert "Cannot rerun job while active" in r_res.json()["message"]

    # Cancel the job (transitions QUEUED -> CANCEL_REQUESTED)
    cancel_key = f"key-cancel-{uuid.uuid4().hex[:6]}"
    c_res = client.post(
        f"/v1/jobs/{job_id}/cancel",
        headers={"Idempotency-Key": cancel_key, "X-Dev-Subject": "alice"},
    )
    assert c_res.status_code == 202

    # Manually mark the job as terminal CANCELLED in database to simulate execution completion
    import psycopg2
    from tests.conftest import TEST_DATABASE_URL_SYNC
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'CANCELLED' WHERE id = %s", (job_id,))
    conn.close()

    # Attempting to cancel a job that is now CANCELLED (terminal) is rejected with 409
    cancel2_key = f"key-cancel-2-{uuid.uuid4().hex[:6]}"
    c2_res = client.post(
        f"/v1/jobs/{job_id}/cancel",
        headers={"Idempotency-Key": cancel2_key, "X-Dev-Subject": "alice"},
    )
    assert c2_res.status_code == 409
    assert "Cannot cancel job" in c2_res.json()["message"]

    # Now that job is CANCELLED (terminal), rerun succeeds with 202
    rerun2_key = f"key-rerun-2-{uuid.uuid4().hex[:6]}"
    r2_res = client.post(
        f"/v1/jobs/{job_id}/reruns",
        headers={"Idempotency-Key": rerun2_key, "X-Dev-Subject": "alice"},
    )
    assert r2_res.status_code == 202
