import json
import os
import re
from typing import Any
import uuid
import psycopg2
from fastapi.testclient import TestClient
import yaml

TEST_DATABASE_URL_SYNC = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _create_workspace(client: TestClient, slug_prefix: str, owner_subject: str) -> tuple[str, dict[str, str]]:
    headers = {"X-Dev-Subject": owner_subject}
    slug = f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    res = client.post(
        "/v1/workspaces",
        json={"name": f"Workspace {slug}", "slug": slug},
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"], headers


def _cleanup_test_rows() -> None:
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute("UPDATE releases SET status = 'SUPERSEDED' WHERE status = 'IMAGE_READY'")
        cur.execute("UPDATE jobs SET state = 'CANCELLED' WHERE state = 'QUEUED'")
    conn.commit()
    conn.close()


def test_m5_prometheus_metrics_and_live_db_counters(client: TestClient, clean_db: Any) -> None:
    """Verify /metrics exposes Prometheus gauges/counters reflecting live HTTP and database state."""
    ws_id, headers = _create_workspace(client, "m5-metrics", "alice")
    job_digest = "registry.example.com/worker@sha256:" + "a" * 64

    jr = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "metrics-probe-job", "image_digest": job_digest},
        headers={"Idempotency-Key": f"m5-j-{uuid.uuid4().hex[:8]}", **headers},
    )
    assert jr.status_code == 202

    m_res = client.get("/metrics")
    assert m_res.status_code == 200
    assert "text/plain" in m_res.headers.get("content-type", "")
    text_body = m_res.text

    required_metrics = [
        'hamicloud_http_requests_total{status_class="2xx"}',
        'hamicloud_http_requests_total{status_class="4xx"}',
        'hamicloud_http_requests_total{status_class="5xx"}',
        "hamicloud_http_request_duration_ms_p50",
        "hamicloud_http_request_duration_ms_p95",
        "hamicloud_http_request_duration_ms_p99",
        "hamicloud_outbox_pending_total",
        "hamicloud_active_leases_total",
        "hamicloud_queued_jobs_total",
        "hamicloud_oldest_queued_job_age_seconds",
        "hamicloud_running_jobs_total",
        "hamicloud_healthy_services_total",
        "hamicloud_job_retry_ratio",
        "hamicloud_dead_letter_total",
    ]
    for metric_name in required_metrics:
        assert metric_name in text_body, f"Missing {metric_name} in /metrics output"

    queued_match = re.search(r"^hamicloud_queued_jobs_total\s+(\d+)$", text_body, re.MULTILINE)
    assert queued_match is not None
    assert int(queued_match.group(1)) >= 1

    outbox_match = re.search(r"^hamicloud_outbox_pending_total\s+(\d+)$", text_body, re.MULTILINE)
    assert outbox_match is not None
    assert int(outbox_match.group(1)) >= 1

    _cleanup_test_rows()


def test_m5_workspace_telemetry_rbac_and_isolation(client: TestClient, clean_db: Any) -> None:
    """Verify /v1/workspaces/{workspace_id}/telemetry enforces VIEWER+ access and isolates workspace counts."""
    ws_a, alice_headers = _create_workspace(client, "m5-tele-a", "alice")
    ws_b, bob_headers = _create_workspace(client, "m5-tele-b", "bob")

    # Add charlie as VIEWER in Workspace A
    add_v = client.post(
        f"/v1/workspaces/{ws_a}/members",
        json={"user_subject": "charlie", "role": "VIEWER"},
        headers=alice_headers,
    )
    assert add_v.status_code in (200, 201)

    # Submit 1 job in Workspace A
    job_digest = "registry.example.com/worker@sha256:" + "b" * 64
    jr = client.post(
        f"/v1/workspaces/{ws_a}/jobs",
        json={"name": "ws-a-job", "image_digest": job_digest},
        headers={"Idempotency-Key": f"m5-ta-{uuid.uuid4().hex[:8]}", **alice_headers},
    )
    assert jr.status_code == 202

    # VIEWER charlie can inspect Workspace A telemetry
    tele_a = client.get(f"/v1/workspaces/{ws_a}/telemetry", headers={"X-Dev-Subject": "charlie"})
    assert tele_a.status_code == 200
    body_a = tele_a.json()
    assert body_a["workspace_id"] == ws_a
    assert body_a["queued_jobs_total"] == 1
    assert body_a["outbox_pending_total"] >= 1

    # Workspace B telemetry does not see Workspace A's queued job
    tele_b = client.get(f"/v1/workspaces/{ws_b}/telemetry", headers=bob_headers)
    assert tele_b.status_code == 200
    assert tele_b.json()["queued_jobs_total"] == 0

    # Non-member bob receives 404 Not Found byte-identical to non-existent workspace
    fake_ws = str(uuid.uuid4())
    r_cross = client.get(f"/v1/workspaces/{ws_a}/telemetry", headers=bob_headers)
    r_fake = client.get(f"/v1/workspaces/{fake_ws}/telemetry", headers=bob_headers)
    assert r_cross.status_code == 404
    assert r_fake.status_code == 404
    assert r_cross.json()["error_code"] == "NOT_FOUND"
    assert r_cross.json()["message"].replace(ws_a, "<ID>") == r_fake.json()["message"].replace(fake_ws, "<ID>")

    # Unauthenticated request receives 401
    r_unauth = client.get(f"/v1/workspaces/{ws_a}/telemetry")
    assert r_unauth.status_code == 401

    _cleanup_test_rows()


def test_m5_w3c_traceparent_propagation_to_headers_and_outbox(client: TestClient, clean_db: Any) -> None:
    """Verify W3C traceparent and X-Correlation-ID propagate to HTTP response headers and outbox event headers."""
    ws_id, alice_headers = _create_workspace(client, "m5-trace", "alice")
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    incoming_traceparent = f"00-{trace_id}-00f067aa0ba902b7-01"
    correlation_id = "corr_m5_trace_propagation_01"

    job_digest = "registry.example.com/worker@sha256:" + "c" * 64
    res = client.post(
        f"/v1/workspaces/{ws_id}/jobs",
        json={"name": "traced-job", "image_digest": job_digest},
        headers={
            "Idempotency-Key": f"m5-tr-{uuid.uuid4().hex[:8]}",
            "traceparent": incoming_traceparent,
            "X-Correlation-ID": correlation_id,
            **alice_headers,
        },
    )
    assert res.status_code == 202
    assert res.headers.get("X-Correlation-ID") == correlation_id
    assert res.headers.get("X-Trace-ID") == trace_id
    resp_tp = res.headers.get("traceparent", "")
    assert resp_tp.startswith(f"00-{trace_id}-") and resp_tp.endswith("-01")

    job_id = res.json()["operation_id"]

    # Verify outbox event headers in PostgreSQL contain trace_id, traceparent, and correlation_id
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT headers_json FROM outbox_events WHERE workspace_id = %s AND topic = 'job.submitted.v1' ORDER BY created_at DESC LIMIT 1",
            (ws_id,),
        )
        row = cur.fetchone()
    conn.close()
    assert row is not None
    headers_json = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    assert headers_json.get("trace_id") == trace_id
    assert headers_json.get("correlation_id") == correlation_id
    assert str(headers_json.get("traceparent", "")).startswith(f"00-{trace_id}-")
    assert job_id is not None

    _cleanup_test_rows()


def test_m5_observability_alerts_and_runbooks_contract() -> None:
    """Verify deploy/observability/alerts.yml, grafana-dashboard.json, and docs/runbooks/*.md are complete."""
    alerts_path = os.path.join(REPO_ROOT, "deploy", "observability", "alerts.yml")
    dashboard_path = os.path.join(REPO_ROOT, "deploy", "observability", "grafana-dashboard.json")
    assert os.path.exists(alerts_path), f"Missing {alerts_path}"
    assert os.path.exists(dashboard_path), f"Missing {dashboard_path}"

    with open(alerts_path, "r", encoding="utf-8") as f:
        alerts_doc = yaml.safe_load(f)
    rules = alerts_doc["groups"][0]["rules"]
    expected_alerts = {
        "HamiCloudApiHigh5xxRate",
        "HamiCloudStalledAcceptedWork",
        "HamiCloudJobRetryStorm",
        "HamiCloudPersistentDiskPressure",
        "HamiCloudBackupRestoreVerificationFailed",
    }
    found_alerts = set()
    for rule in rules:
        alert_name = rule["alert"]
        found_alerts.add(alert_name)
        assert rule.get("expr"), f"Alert {alert_name} missing expr"
        assert rule.get("for"), f"Alert {alert_name} missing duration 'for'"
        runbook_rel = rule["annotations"]["runbook_url"]
        runbook_abs = os.path.join(REPO_ROOT, *runbook_rel.split("/"))
        assert os.path.exists(runbook_abs), f"Alert {alert_name} links to missing runbook {runbook_rel}"
        with open(runbook_abs, "r", encoding="utf-8") as rf:
            rb_text = rf.read()
        assert "## Diagnostic Steps" in rb_text, f"{runbook_rel} missing Diagnostic Steps"
        assert "## Mitigation Commands" in rb_text, f"{runbook_rel} missing Mitigation Commands"
        assert "## Verification Criteria" in rb_text, f"{runbook_rel} missing Verification Criteria"

    assert expected_alerts.issubset(found_alerts)

    with open(dashboard_path, "r", encoding="utf-8") as df:
        dash = json.load(df)
    assert len(dash.get("panels", [])) >= 4
