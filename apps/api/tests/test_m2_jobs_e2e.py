import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
from typing import Generator
import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")

SCHEDULER_BIN = os.path.join(
    RUNTIME_DIR, "dist", "hamicloud-scheduler.exe" if sys.platform == "win32" else "hamicloud-scheduler"
)
EXECUTOR_BIN = os.path.join(
    RUNTIME_DIR, "dist", "hamicloud-executor.exe" if sys.platform == "win32" else "hamicloud-executor"
)

TEST_DB_URL = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test?sslmode=disable"
TEST_DB_SYNC = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test"


def run_scheduler_once() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["RUNTIME_DATABASE_URL"] = TEST_DB_URL
    env["ENVIRONMENT"] = "development"
    env["RUN_ONCE"] = "true"

    if os.path.exists(SCHEDULER_BIN):
        cmd = [SCHEDULER_BIN]
    else:
        cmd = ["go", "run", "./cmd/hamicloud-scheduler"]

    return subprocess.run(cmd, cwd=RUNTIME_DIR, env=env, capture_output=True, text=True, check=True)


def run_executor_once() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["RUNTIME_DATABASE_URL"] = TEST_DB_URL
    env["ENVIRONMENT"] = "development"
    env["RUN_ONCE"] = "true"
    env["ARTIFACTS_DIR"] = os.path.join(REPO_ROOT, "var", "artifacts")

    if os.path.exists(EXECUTOR_BIN):
        cmd = [EXECUTOR_BIN]
    else:
        cmd = ["go", "run", "./cmd/hamicloud-executor"]

    return subprocess.run(cmd, cwd=RUNTIME_DIR, env=env, capture_output=True, text=True, check=True)


class EphemeralServiceHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Service OK")

    def log_message(self, format, *args):
        pass


@pytest.fixture
def mock_service_server() -> Generator[int, None, None]:
    server = socketserver.TCPServer(("127.0.0.1", 0), EphemeralServiceHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield port

    server.shutdown()
    server.server_close()


def test_m2_e2e_finite_job_lifecycle_and_output_download(client: TestClient, clean_db: None):
    """
    Milestone M2 Exit Criterion 1 & 2:
    Finite background job is admitted, executed by Go worker, stdout persisted to disk,
    and served via authorized GET /v1/jobs/{job_id}/output.
    """
    headers = {"X-Dev-Subject": "alice", "Idempotency-Key": f"idemp-ws-{time.time()}"}

    # 1. Create Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "M2 Job Space", "slug": "m2-job-space"},
        headers=headers,
    )
    assert ws_resp.status_code == 201, ws_resp.text
    ws_id = ws_resp.json()["id"]

    # 2. Submit finite job with Python command
    expected_output = "HamiCloud M2 Test Output Success: Calculation=42"
    python_cmd = f"print('{expected_output}')"
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={
            "name": "data-processor-job",
            "image_digest": "docker.io/library/python:3.12-alpine",
            "command_args": [sys.executable, "-c", python_cmd],
            "timeout_seconds": 30,
            "max_retries": 1,
        },
        headers={**headers, "Idempotency-Key": f"idemp-job-sub-{time.time()}"},
    )
    assert job_resp.status_code == 202, job_resp.text
    job_id = job_resp.json()["operation_id"]

    # Verify initial state is QUEUED
    details_resp = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert details_resp.status_code == 200
    assert details_resp.json()["state"] == "QUEUED"

    # Attempting to download output while job is running/queued returns 409
    early_out = client.get(f"/v1/jobs/{job_id}/output", headers=headers)
    assert early_out.status_code == 409

    # 3. Run Go Scheduler admission pass
    sched_proc = run_scheduler_once()
    assert sched_proc.returncode == 0

    # Job is now ADMITTED with attempt 1 created
    details_admitted = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert details_admitted.status_code == 200
    assert details_admitted.json()["state"] == "ADMITTED"
    assert len(details_admitted.json()["attempts"]) == 1

    # 4. Run Go Executor reconciliation pass
    exec_proc = run_executor_once()
    assert exec_proc.returncode == 0

    # 5. Verify job is SUCCEEDED and attempt 1 details
    details_succeeded = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert details_succeeded.status_code == 200
    job_data = details_succeeded.json()
    assert job_data["state"] == "SUCCEEDED"
    assert len(job_data["attempts"]) == 1

    att = job_data["attempts"][0]
    assert att["state"] == "SUCCEEDED"
    assert att["exit_code"] == 0
    assert att["failure_reason"] is None
    assert att["started_at"] is not None
    assert att["finished_at"] is not None

    # 6. Download job output and verify content
    out_resp = client.get(f"/v1/jobs/{job_id}/output", headers=headers)
    assert out_resp.status_code == 200
    assert out_resp.headers["content-type"].startswith("text/plain")
    assert f"job-{job_id}-output.txt" in out_resp.headers.get("content-disposition", "")
    assert expected_output in out_resp.text

    # 7. Tenant isolation check: bob (non-member) cannot access job details or output
    bob_headers = {"X-Dev-Subject": "bob"}
    assert client.get(f"/v1/jobs/{job_id}", headers=bob_headers).status_code == 404
    assert client.get(f"/v1/jobs/{job_id}/output", headers=bob_headers).status_code == 404

    # 8. Unfabricated output check: when artifact is missing from disk, return 404 (never fabricate output)
    artifact_file = os.path.join(
        settings.effective_artifacts_dir, str(ws_id), str(job_id), "output.txt"
    )
    if os.path.exists(artifact_file):
        os.remove(artifact_file)
    missing_resp = client.get(f"/v1/jobs/{job_id}/output", headers=headers)
    assert missing_resp.status_code == 404
    assert missing_resp.json()["error_code"] == "NOT_FOUND"
    assert missing_resp.json()["message"] == "Job output artifact not found"


def test_m2_e2e_job_retry_backoff_and_exhaustion(client: TestClient, clean_db: None):
    """
    Milestone M2 Exit Criterion 3:
    Transient failure consumes retry budget, enters RETRY_WAIT, backoff requeues,
    and exhausting retries leads to terminal FAILED state.
    """
    headers = {"X-Dev-Subject": "alice", "Idempotency-Key": f"idemp-ws-{time.time()}"}

    # 1. Create Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Retry Space", "slug": "retry-space"},
        headers=headers,
    )
    assert ws_resp.status_code == 201, ws_resp.text
    ws_id = ws_resp.json()["id"]

    # 2. Submit failing job with max_retries = 2
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={
            "name": "failing-job",
            "image_digest": "docker.io/library/python:3.12-alpine",
            "command_args": [sys.executable, "-c", "import sys; print('Failing attempt'); sys.exit(7)"],
            "timeout_seconds": 30,
            "max_retries": 1,
        },
        headers={**headers, "Idempotency-Key": f"idemp-job-fail-{time.time()}"},
    )
    assert job_resp.status_code == 202
    job_id = job_resp.json()["operation_id"]

    # Pass 1: Scheduler admits attempt 1
    assert run_scheduler_once().returncode == 0
    # Pass 1: Executor runs attempt 1 -> exit 7 -> RETRY_WAIT (since attempt 1 <= max_retries 2)
    assert run_executor_once().returncode == 0

    details_1 = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert details_1["state"] == "RETRY_WAIT"
    assert len(details_1["attempts"]) == 1
    assert details_1["attempts"][0]["state"] == "FAILED"
    assert details_1["attempts"][0]["exit_code"] == 7

    # Immediate scheduler run does not requeue because backoff hasn't elapsed
    assert run_scheduler_once().returncode == 0
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).json()["state"] == "RETRY_WAIT"

    # Simulate backoff elapsed by rewinding updated_at by 15 seconds in DB
    conn = psycopg2.connect(TEST_DB_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET updated_at = NOW() - INTERVAL '15 seconds' WHERE id = %s",
            (job_id,),
        )
    conn.commit()
    conn.close()

    # Pass 2: Scheduler requeues retry_wait job and admits attempt 2
    assert run_scheduler_once().returncode == 0
    details_2_admitted = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert details_2_admitted["state"] == "ADMITTED"
    assert len(details_2_admitted["attempts"]) == 2

    # Pass 2: Executor runs attempt 2 -> exit 7 -> retry budget exhausted (attempt 2 == max_retries 2)
    assert run_executor_once().returncode == 0
    details_final = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert details_final["state"] == "FAILED"
    assert len(details_final["attempts"]) == 2
    assert details_final["attempts"][1]["state"] == "FAILED"
    assert details_final["attempts"][1]["exit_code"] == 7

    # Output endpoint returns available logs or failure summary with status 200
    out_resp = client.get(f"/v1/jobs/{job_id}/output", headers=headers)
    assert out_resp.status_code == 200
    assert "Failing attempt" in out_resp.text or "Exit Code: 7" in out_resp.text


def test_m2_e2e_job_cancellation(client: TestClient, clean_db: None):
    """
    Milestone M2 Exit Criterion 4:
    Active cancellation transitions job to CANCEL_REQUESTED and terminates as CANCELLED.
    """
    headers = {"X-Dev-Subject": "alice", "Idempotency-Key": f"idemp-ws-{time.time()}"}

    # 1. Create Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Cancel Space", "slug": "cancel-space"},
        headers=headers,
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    # 2. Submit long running job
    job_resp = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={
            "name": "long-running-job",
            "image_digest": "docker.io/library/python:3.12-alpine",
            "command_args": [sys.executable, "-c", "import time; time.sleep(10)"],
            "timeout_seconds": 60,
            "max_retries": 1,
        },
        headers={**headers, "Idempotency-Key": f"idemp-job-cancel-{time.time()}"},
    )
    assert job_resp.status_code == 202
    job_id = job_resp.json()["operation_id"]

    # Scheduler admits attempt 1
    assert run_scheduler_once().returncode == 0

    # User issues cancel request via API
    cancel_resp = client.post(
        f"/v1/jobs/{job_id}/cancel",
        headers={**headers, "Idempotency-Key": f"idemp-req-cancel-{time.time()}"},
    )
    assert cancel_resp.status_code == 202
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).json()["state"] == "CANCEL_REQUESTED"

    # Downloading output while job is in CANCEL_REQUESTED returns 409 Conflict
    assert client.get(f"/v1/jobs/{job_id}/output", headers=headers).status_code == 409

    # Executor reconciles intent, observes cancellation signal, and marks attempt CANCELLED
    assert run_executor_once().returncode == 0

    final_job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert final_job["state"] == "CANCELLED"
    assert len(final_job["attempts"]) == 1
    assert final_job["attempts"][0]["state"] == "CANCELLED"

    # Job output artifact does not exist since execution was cancelled before start (API returns 404, never fabricating output)
    out_resp = client.get(f"/v1/jobs/{job_id}/output", headers=headers)
    assert out_resp.status_code == 404
    assert out_resp.json()["error_code"] == "NOT_FOUND"
    assert out_resp.json()["message"] == "Job output artifact not found"


def test_m2_e2e_service_rollback_workflow(
    client: TestClient, clean_db: None, mock_service_server: int
):
    """
    Milestone M2 Exit Criterion 5:
    Service rollback reverts application to previous release configuration.
    """
    headers = {"X-Dev-Subject": "alice", "Idempotency-Key": f"idemp-ws-{time.time()}"}
    port = mock_service_server

    # 1. Create Workspace & Application
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Rollback Space", "slug": "rollback-space"},
        headers=headers,
    )
    assert ws_resp.status_code == 201
    ws_id = ws_resp.json()["id"]

    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Rollback App", "slug": "rollback-app", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    )
    assert app_resp.status_code == 201
    app_id = app_resp.json()["id"]

    # 2. Deploy Release 1 (v1.0.0, healthy port)
    dep1_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={
            "image_digest": "docker.io/library/nginx:v1.0.0",
            "port": port,
            "health_path": "/healthz",
        },
        headers={**headers, "Idempotency-Key": f"idemp-dep-1-{time.time()}"},
    )
    assert dep1_resp.status_code == 202
    rel1_id = dep1_resp.json()["operation_id"]

    # Scheduler & Executor reconcile Release 1
    assert run_scheduler_once().returncode == 0
    assert run_executor_once().returncode == 0

    app_after_1 = client.get(f"/v1/apps/{app_id}", headers=headers).json()
    assert app_after_1["current_release_id"] == rel1_id

    # 3. Deploy Release 2 (v2.0.0, healthy port)
    dep2_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={
            "image_digest": "docker.io/library/nginx:v2.0.0",
            "port": port,
            "health_path": "/healthz",
        },
        headers={**headers, "Idempotency-Key": f"idemp-dep-2-{time.time()}"},
    )
    assert dep2_resp.status_code == 202
    rel2_id = dep2_resp.json()["operation_id"]

    # Scheduler & Executor reconcile Release 2
    assert run_scheduler_once().returncode == 0
    assert run_executor_once().returncode == 0

    app_after_2 = client.get(f"/v1/apps/{app_id}", headers=headers).json()
    assert app_after_2["current_release_id"] == rel2_id

    # 4. Rollback to Release 1
    rollback_resp = client.post(
        f"/v1/apps/{app_id}/rollbacks",
        json={"target_release_id": rel1_id},
        headers={**headers, "Idempotency-Key": f"idemp-rollback-{time.time()}"},
    )
    assert rollback_resp.status_code == 202
    rel3_id = rollback_resp.json()["operation_id"]
    assert rel3_id != rel1_id
    assert rel3_id != rel2_id

    # Scheduler & Executor reconcile Release 3 (the rolled-back release)
    assert run_scheduler_once().returncode == 0
    assert run_executor_once().returncode == 0

    # 5. Verify App now points to Release 3, which inherited Release 1's image
    app_after_rollback = client.get(f"/v1/apps/{app_id}", headers=headers).json()
    assert app_after_rollback["current_release_id"] == rel3_id

    rel3_details = [
        r
        for r in client.get(f"/v1/apps/{app_id}/releases", headers=headers).json()["items"]
        if r["id"] == rel3_id
    ][0]
    assert rel3_details["image_digest"] == "docker.io/library/nginx:v1.0.0"
    assert rel3_details["status"] == "HEALTHY"
    assert rel3_details["release_number"] == 3
