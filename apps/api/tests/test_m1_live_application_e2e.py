import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
from typing import Generator
import pytest
from fastapi.testclient import TestClient

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")

SCHEDULER_BIN = os.path.join(RUNTIME_DIR, "dist", "hamicloud-scheduler.exe" if sys.platform == "win32" else "hamicloud-scheduler")
EXECUTOR_BIN = os.path.join(RUNTIME_DIR, "dist", "hamicloud-executor.exe" if sys.platform == "win32" else "hamicloud-executor")


def run_scheduler_once() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["RUNTIME_DATABASE_URL"] = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test?sslmode=disable"
    env["ENVIRONMENT"] = "development"
    env["RUN_ONCE"] = "true"

    if os.path.exists(SCHEDULER_BIN):
        cmd = [SCHEDULER_BIN]
    else:
        cmd = ["go", "run", "./cmd/hamicloud-scheduler"]

    return subprocess.run(cmd, cwd=RUNTIME_DIR, env=env, capture_output=True, text=True, check=True)


def run_executor_once() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["RUNTIME_DATABASE_URL"] = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test?sslmode=disable"
    env["ENVIRONMENT"] = "development"
    env["RUN_ONCE"] = "true"

    if os.path.exists(EXECUTOR_BIN):
        cmd = [EXECUTOR_BIN]
    else:
        cmd = ["go", "run", "./cmd/hamicloud-executor"]

    return subprocess.run(cmd, cwd=RUNTIME_DIR, env=env, capture_output=True, text=True, check=True)


class LiveServiceHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","probe":"ok"}')
        elif self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>HamiCloud Live Workload M1</h1>")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy stdout logs during test execution
        pass


@pytest.fixture
def live_service_server() -> Generator[int, None, None]:
    """Start an ephemeral background HTTP server to act as a healthy target service."""
    server = socketserver.TCPServer(("127.0.0.1", 0), LiveServiceHandler)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield port

    server.shutdown()
    server.server_close()


def test_m1_e2e_invalid_readiness_probe_surfaces_visible_error(client: TestClient, clean_db: None):
    """
    Milestone M1 Exit Criterion 2:
    Invalid readiness is visible to the user, not silent.
    """
    headers = {"X-Dev-Subject": "alice", "Idempotency-Key": f"idemp-ws-{time.time()}"}

    # 1. Create Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Invalid Readiness WS", "slug": "inv-readiness-ws"},
        headers=headers,
    )
    assert ws_resp.status_code == 201, ws_resp.text
    ws_id = ws_resp.json()["id"]

    # 2. Create Application
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Broken Health Service", "slug": "broken-svc", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    )
    assert app_resp.status_code == 201, app_resp.text
    app_id = app_resp.json()["id"]

    # 3. Deploy release pointing to dead port (59992 - nothing listening)
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={
            "image_digest": "docker.io/library/nginx:alpine",
            "port": 59992,
            "health_path": "/healthz",
        },
        headers={**headers, "Idempotency-Key": f"idemp-dep-inv-{time.time()}"},
    )
    assert deploy_resp.status_code == 202, deploy_resp.text
    operation_id = deploy_resp.json()["operation_id"]

    # 4. Run Go Scheduler admission pass
    sched_proc = run_scheduler_once()
    assert sched_proc.returncode == 0

    # 5. Run Go Executor reconciliation pass
    exec_proc = run_executor_once()
    assert exec_proc.returncode == 0

    # 6. Verify that the user sees visible failure details on the release
    releases_resp = client.get(f"/v1/apps/{app_id}/releases", headers=headers)
    assert releases_resp.status_code == 200, releases_resp.text
    items = releases_resp.json()["items"]
    assert len(items) == 1
    rel = items[0]

    assert rel["id"] == operation_id
    assert rel["status"] == "DEPLOY_FAILED", f"Expected DEPLOY_FAILED, got {rel['status']}"
    assert rel["status_reason"] is not None
    # Readiness probe failure MUST be non-silent and specifically identify the failed target
    assert "readiness probe failed" in rel["status_reason"].lower()
    assert "59992" in rel["status_reason"]

    # 7. Verify the broken release is NOT promoted to current_release_id
    app_check = client.get(f"/v1/apps/{app_id}", headers=headers)
    assert app_check.status_code == 200
    assert app_check.json()["current_release_id"] is None


def test_m1_e2e_valid_readiness_reaches_working_service_url(
    client: TestClient, clean_db: None, live_service_server: int
):
    """
    Milestone M1 Exit Criterion 1:
    A clean environment reaches a working service URL through the UI / API.
    """
    headers = {"X-Dev-Subject": "alice", "Idempotency-Key": f"idemp-ws-{time.time()}"}
    port = live_service_server

    # 1. Create Workspace
    ws_resp = client.post(
        "/v1/workspaces",
        json={"name": "Healthy Service Space", "slug": "healthy-space"},
        headers=headers,
    )
    assert ws_resp.status_code == 201, ws_resp.text
    ws_id = ws_resp.json()["id"]

    # 2. Create Application
    app_resp = client.post(
        f"/v1/workspaces/{ws_id}/apps",
        json={"name": "Live HTTP App", "slug": "live-app", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    )
    assert app_resp.status_code == 201, app_resp.text
    app_id = app_resp.json()["id"]

    # 3. Deploy release pointing to active live service
    deploy_resp = client.post(
        f"/v1/apps/{app_id}/deployments",
        json={
            "image_digest": "docker.io/library/nginx:alpine",
            "port": port,
            "health_path": "/healthz",
        },
        headers={**headers, "Idempotency-Key": f"idemp-dep-healthy-{time.time()}"},
    )
    assert deploy_resp.status_code == 202, deploy_resp.text
    operation_id = deploy_resp.json()["operation_id"]

    # 4. Run Go Scheduler admission pass
    sched_proc = run_scheduler_once()
    assert sched_proc.returncode == 0

    # 5. Run Go Executor reconciliation pass
    exec_proc = run_executor_once()
    assert exec_proc.returncode == 0

    # 6. Verify release is HEALTHY and application points to it
    releases_resp = client.get(f"/v1/apps/{app_id}/releases", headers=headers)
    assert releases_resp.status_code == 200, releases_resp.text
    items = releases_resp.json()["items"]
    assert len(items) == 1
    rel = items[0]

    assert rel["id"] == operation_id
    assert rel["status"] == "HEALTHY", f"Expected HEALTHY, got {rel['status']}"

    app_check = client.get(f"/v1/apps/{app_id}", headers=headers)
    assert app_check.status_code == 200
    assert app_check.json()["current_release_id"] == operation_id

    # 7. Verify live service endpoint delivers working HTTP application payload
    import urllib.request

    service_url = f"http://127.0.0.1:{port}/"
    with urllib.request.urlopen(service_url, timeout=3) as resp:
        assert resp.status == 200
        content = resp.read().decode("utf-8")
        assert "HamiCloud Live Workload M1" in content
