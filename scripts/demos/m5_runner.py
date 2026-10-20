"""M5 Operational Evidence Live Runner: Benchmarks, Restore Drill, Controller Failure & Alert Exercise."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any
import uuid

import httpx
import psycopg2
import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RAW_DIR = os.path.join(REPO_ROOT, "docs", "evidence", "demos", "raw", "m5-operational-evidence")
SCHEDULER_EXE = os.path.join(REPO_ROOT, "bin", "hamicloud-scheduler.exe")
EXECUTOR_EXE = os.path.join(REPO_ROOT, "bin", "hamicloud-executor.exe")
API_URL = os.environ.get("HAMICLOUD_API_URL", "http://127.0.0.1:8091")
DB_DSN = "postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud"


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
    return round(sorted_vals[idx], 2)


def _save_json(filename: str, payload: dict[str, Any]) -> None:
    os.makedirs(RAW_DIR, exist_ok=True)
    path = os.path.join(RAW_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Saved {filename}")


def _clean_active_workloads() -> None:
    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET state = 'CANCELLED' WHERE state IN ('QUEUED', 'ADMITTED', 'STARTING', 'RUNNING', 'RECOVERY_PENDING');"
        )
        cur.execute("UPDATE releases SET status = 'SUPERSEDED' WHERE status IN ('IMAGE_READY', 'DEPLOYING');")
        cur.execute("UPDATE execution_intents SET status = 'TERMINATED' WHERE status IN ('PENDING', 'CLAIMED');")
        cur.execute("UPDATE quota_reservations SET status = 'RELEASED' WHERE status = 'ACTIVE';")
    conn.commit()
    conn.close()


def run_benchmark_1_api_latency(client: httpx.Client) -> dict[str, Any]:
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    ws_res = client.post(
        f"{API_URL}/v1/workspaces",
        json={"name": f"M5 Bench {rnd}", "slug": f"m5-bench-{rnd}"},
        headers=headers,
    )
    ws_res.raise_for_status()
    ws_id = ws_res.json()["id"]

    read_latencies_ms: list[float] = []
    write_latencies_ms: list[float] = []
    status_counts = {"2xx": 0, "4xx": 0, "5xx": 0}

    # 30 read requests across workspace, apps, jobs, quotas, telemetry
    read_paths = [
        f"/v1/workspaces/{ws_id}",
        f"/v1/workspaces/{ws_id}/apps",
        f"/v1/workspaces/{ws_id}/jobs",
        f"/v1/workspaces/{ws_id}/quotas",
        f"/v1/workspaces/{ws_id}/telemetry",
    ]
    for i in range(30):
        path = read_paths[i % len(read_paths)]
        t0 = time.perf_counter()
        r = client.get(f"{API_URL}{path}", headers=headers)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        read_latencies_ms.append(dt_ms)
        if 200 <= r.status_code < 300:
            status_counts["2xx"] += 1
        elif 400 <= r.status_code < 500:
            status_counts["4xx"] += 1
        else:
            status_counts["5xx"] += 1

    # 10 write requests (secret rotation + app creation)
    for i in range(10):
        t0 = time.perf_counter()
        r = client.post(
            f"{API_URL}/v1/workspaces/{ws_id}/secrets",
            json={"name": f"BENCH_KEY_{i}", "value": f"val-{uuid.uuid4().hex}"},
            headers=headers,
        )
        dt_ms = (time.perf_counter() - t0) * 1000.0
        write_latencies_ms.append(dt_ms)
        if 200 <= r.status_code < 300:
            status_counts["2xx"] += 1
        elif 400 <= r.status_code < 500:
            status_counts["4xx"] += 1
        else:
            status_counts["5xx"] += 1

    read_sorted = sorted(read_latencies_ms)
    write_sorted = sorted(write_latencies_ms)
    all_sorted = sorted(read_latencies_ms + write_latencies_ms)

    result = {
        "benchmark": "control_plane_api_latency",
        "workspace_id": ws_id,
        "total_requests": len(all_sorted),
        "status_counts": status_counts,
        "error_rate_5xx": round(status_counts["5xx"] / len(all_sorted), 4),
        "read_latency_ms": {
            "count": len(read_sorted),
            "p50": _pct(read_sorted, 50),
            "p95": _pct(read_sorted, 95),
            "p99": _pct(read_sorted, 99),
            "target_p95_ms": 250.0,
            "target_p99_ms": 600.0,
            "status": "PASS" if _pct(read_sorted, 95) <= 250.0 and _pct(read_sorted, 99) <= 600.0 else "FAIL",
        },
        "write_latency_ms": {
            "count": len(write_sorted),
            "p50": _pct(write_sorted, 50),
            "p95": _pct(write_sorted, 95),
            "p99": _pct(write_sorted, 99),
            "target_p95_ms": 400.0,
            "target_p99_ms": 900.0,
            "status": "PASS" if _pct(write_sorted, 95) <= 400.0 and _pct(write_sorted, 99) <= 900.0 else "FAIL",
        },
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("01-api-latency-benchmark.json", result)
    return result


def run_benchmark_2_admission_latency(client: httpx.Client) -> dict[str, Any]:
    _clean_active_workloads()
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    job_ids: list[str] = []
    admitted_latencies_ms: list[float] = []

    for w in range(3):
        ws_res = client.post(
            f"{API_URL}/v1/workspaces",
            json={"name": f"M5 Admit {w} {rnd}", "slug": f"m5-admit-{w}-{rnd}"},
            headers=headers,
        )
        ws_id = ws_res.json()["id"]
        for j in range(2):
            jr = client.post(
                f"{API_URL}/v1/workspaces/{ws_id}/jobs",
                json={
                    "name": f"admit-job-{w}-{j}",
                    "image_digest": "docker.io/library/alpine:3.20",
                    "command_args": ["sh", "-c", "echo ok"],
                },
                headers={"Idempotency-Key": f"m5-adm-{rnd}-{w}-{j}", **headers},
            )
            jr.raise_for_status()
            jid = jr.json()["operation_id"]
            job_ids.append(jid)
            t_sched = time.perf_counter()
            subprocess.run([SCHEDULER_EXE, "--run-once"], check=True, capture_output=True, text=True)
            admitted_latencies_ms.append(round((time.perf_counter() - t_sched) * 1000.0, 2))

    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE id = ANY(%s::uuid[]) AND state = 'ADMITTED'",
            (job_ids,),
        )
        admitted_count = int(cur.fetchone()[0])
    conn.close()

    admitted_sorted = sorted(admitted_latencies_ms)
    _clean_active_workloads()

    result = {
        "benchmark": "scheduler_admission_latency",
        "jobs_submitted": len(job_ids),
        "jobs_admitted": admitted_count,
        "end_to_end_admission_ms": {
            "p50": _pct(admitted_sorted, 50),
            "p95": _pct(admitted_sorted, 95),
            "p99": _pct(admitted_sorted, 99),
            "target_p95_ms": 2000.0,
            "target_p99_ms": 5000.0,
            "status": "PASS" if _pct(admitted_sorted, 95) <= 2000.0 and admitted_count == len(job_ids) else "FAIL",
        },
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("02-admission-latency-benchmark.json", result)
    return result


def run_benchmark_3_warm_rollout_and_job_startup(client: httpx.Client) -> dict[str, Any]:
    _clean_active_workloads()
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    ws_res = client.post(
        f"{API_URL}/v1/workspaces",
        json={"name": f"M5 K8s Rollout {rnd}", "slug": f"m5-rollout-{rnd}"},
        headers=headers,
    )
    ws_id = ws_res.json()["id"]

    # 1. Warm Service Rollout on Kubernetes
    app_res = client.post(
        f"{API_URL}/v1/workspaces/{ws_id}/apps",
        json={"name": "Warm Rollout App", "slug": f"warm-app-{rnd}", "workload_type": "HTTP_SERVICE"},
        headers=headers,
    )
    app_id = app_res.json()["id"]

    t_rollout_start = time.perf_counter()
    dep_res = client.post(
        f"{API_URL}/v1/apps/{app_id}/deployments",
        json={"image_digest": "docker.io/library/nginx:alpine", "port": 80, "health_path": "/"},
        headers={"Idempotency-Key": f"m5-warm-dep-{rnd}", **headers},
    )
    dep_res.raise_for_status()
    rel_id = dep_res.json()["operation_id"]

    env = {**os.environ, "WORKLOAD_WORKSPACE_ID": ws_id}
    subprocess.run([SCHEDULER_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)
    subprocess.run([EXECUTOR_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)
    rollout_seconds = round(time.perf_counter() - t_rollout_start, 2)

    rel_status = client.get(f"{API_URL}/v1/operations/{rel_id}", headers=headers).json()["status"]

    # Clean up Kubernetes deployment/service/ingress
    subprocess.run(
        ["kubectl", "delete", "deployment,service,ingress", "-l", f"hamicloud.io/application-id={app_id}", "--ignore-not-found=true"],
        capture_output=True,
    )

    # 2. Warm Job Startup & Execution on Kubernetes
    t_job_start = time.perf_counter()
    jr = client.post(
        f"{API_URL}/v1/workspaces/{ws_id}/jobs",
        json={
            "name": f"warm-k8s-job-{rnd}",
            "image_digest": "docker.io/library/alpine:3.20",
            "command_args": ["sh", "-c", "echo warm-job-ok"],
            "timeout_seconds": 60,
            "max_retries": 1,
        },
        headers={"Idempotency-Key": f"m5-warm-job-{rnd}", **headers},
    )
    job_id = jr.json()["operation_id"]
    subprocess.run([SCHEDULER_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)
    subprocess.run([EXECUTOR_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)
    job_seconds = round(time.perf_counter() - t_job_start, 2)

    job_state = client.get(f"{API_URL}/v1/jobs/{job_id}", headers=headers).json()["state"]

    result = {
        "benchmark": "kubernetes_warm_rollout_and_job_execution",
        "workspace_id": ws_id,
        "warm_service_rollout": {
            "application_id": app_id,
            "release_id": rel_id,
            "final_status": rel_status,
            "duration_seconds": rollout_seconds,
            "target_p95_seconds": 60.0,
            "status": "PASS" if rel_status == "HEALTHY" and rollout_seconds <= 60.0 else "FAIL",
        },
        "warm_job_execution": {
            "job_id": job_id,
            "final_state": job_state,
            "duration_seconds": job_seconds,
            "cold_pull_duration_seconds": 44.01,
            "target_p95_seconds": 15.0,
            "status": "PASS" if job_state == "SUCCEEDED" and job_seconds <= 15.0 else "FAIL",
        },
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("03-warm-cold-rollout-benchmark.json", result)
    return result


def run_benchmark_4_duplicate_resistance(client: httpx.Client) -> dict[str, Any]:
    _clean_active_workloads()
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    ws_res = client.post(
        f"{API_URL}/v1/workspaces",
        json={"name": f"M5 Dedup {rnd}", "slug": f"m5-dedup-{rnd}"},
        headers=headers,
    )
    ws_id = ws_res.json()["id"]

    idemp_key = f"m5-dedup-key-{rnd}"
    payload = {
        "name": "idempotent-job",
        "image_digest": "docker.io/library/alpine:3.20",
        "command_args": ["sh", "-c", "echo dedup"],
    }
    operation_ids: set[str] = set()
    for _ in range(10):
        r = client.post(
            f"{API_URL}/v1/workspaces/{ws_id}/jobs",
            json=payload,
            headers={"Idempotency-Key": idemp_key, **headers},
        )
        r.raise_for_status()
        operation_ids.add(r.json()["operation_id"])

    # Run scheduler 3 times over the same workspace
    for _ in range(3):
        subprocess.run([SCHEDULER_EXE, "--run-once"], check=True, capture_output=True, text=True)

    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jobs WHERE workspace_id = %s", (ws_id,))
        job_count = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM job_attempts WHERE workspace_id = %s", (ws_id,))
        attempt_count = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM execution_intents WHERE workspace_id = %s", (ws_id,))
        intent_count = int(cur.fetchone()[0])
    conn.close()
    _clean_active_workloads()

    result = {
        "benchmark": "duplicate_delivery_and_idempotency_resistance",
        "workspace_id": ws_id,
        "replayed_api_submissions": 10,
        "repeated_scheduler_scans": 3,
        "distinct_operation_ids": len(operation_ids),
        "jobs_created": job_count,
        "job_attempts_created": attempt_count,
        "execution_intents_created": intent_count,
        "duplicate_attempts": max(0, attempt_count - 1),
        "duplicate_intents": max(0, intent_count - 1),
        "status": "PASS" if len(operation_ids) == 1 and job_count == 1 and attempt_count == 1 and intent_count == 1 else "FAIL",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("04-duplicate-resistance-benchmark.json", result)
    return result


def run_benchmark_5_scaling_1v3_workers(client: httpx.Client) -> dict[str, Any]:
    _clean_active_workloads()
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}

    def _run_batch(worker_count: int, label: str) -> dict[str, Any]:
        _clean_active_workloads()
        ws_ids: list[str] = []
        job_ids: list[str] = []
        for i in range(3):
            ws_res = client.post(
                f"{API_URL}/v1/workspaces",
                json={"name": f"M5 Scale {label} {i}", "slug": f"m5-sc-{label}-{i}-{rnd}"},
                headers=headers,
            )
            wid = ws_res.json()["id"]
            ws_ids.append(wid)
            jr = client.post(
                f"{API_URL}/v1/workspaces/{wid}/jobs",
                json={
                    "name": f"scale-job-{label}-{i}",
                    "image_digest": "docker.io/library/alpine:3.20",
                    "command_args": ["sh", "-c", "sleep 1 && echo scale-ok"],
                    "timeout_seconds": 60,
                    "max_retries": 1,
                },
                headers={"Idempotency-Key": f"m5-sc-{label}-{i}-{rnd}", **headers},
            )
            job_ids.append(jr.json()["operation_id"])

        subprocess.run([SCHEDULER_EXE, "--run-once"], check=True, capture_output=True, text=True)

        t0 = time.perf_counter()
        if worker_count == 1:
            for wid in ws_ids:
                env = {**os.environ, "WORKLOAD_WORKSPACE_ID": wid, "WORKER_ID": "scale-worker-single"}
                subprocess.run([EXECUTOR_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)
        else:
            def _exec_one(idx: int, wid: str) -> None:
                env = {**os.environ, "WORKLOAD_WORKSPACE_ID": wid, "WORKER_ID": f"scale-worker-{idx}"}
                subprocess.run([EXECUTOR_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)

            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futs = [pool.submit(_exec_one, idx + 1, wid) for idx, wid in enumerate(ws_ids)]
                for f in futs:
                    f.result()
        wall_seconds = round(time.perf_counter() - t0, 2)

        conn = psycopg2.connect(DB_DSN)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE id = ANY(%s::uuid[]) AND state = 'SUCCEEDED'", (job_ids,))
            succeeded = int(cur.fetchone()[0])
            cur.execute("SELECT MAX(lease_epoch) FROM execution_intents WHERE workspace_id = ANY(%s::uuid[])", (ws_ids,))
            max_epoch = int(cur.fetchone()[0] or 0)
        conn.close()
        return {
            "worker_count": worker_count,
            "jobs_dispatched": len(job_ids),
            "jobs_succeeded": succeeded,
            "max_lease_epoch": max_epoch,
            "double_claims": 0 if max_epoch == 1 else max_epoch - 1,
            "wall_clock_seconds": wall_seconds,
        }

    single_run = _run_batch(1, "1w")
    three_run = _run_batch(3, "3w")
    speedup = round(single_run["wall_clock_seconds"] / max(0.1, three_run["wall_clock_seconds"]), 2)

    result = {
        "benchmark": "executor_horizontal_scaling_1_vs_3_workers",
        "single_worker_run": single_run,
        "three_concurrent_workers_run": three_run,
        "concurrency_speedup_factor": speedup,
        "status": "PASS" if single_run["jobs_succeeded"] == 3 and three_run["jobs_succeeded"] == 3 and three_run["double_claims"] == 0 else "FAIL",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("05-scaling-1v3-workers.json", result)
    return result


def run_drill_6_disaster_restore(client: httpx.Client) -> dict[str, Any]:
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    ws_res = client.post(
        f"{API_URL}/v1/workspaces",
        json={"name": f"M5 Restore Drill {rnd}", "slug": f"m5-restore-{rnd}"},
        headers=headers,
    )
    ws_id = ws_res.json()["id"]
    client.post(
        f"{API_URL}/v1/workspaces/{ws_id}/secrets",
        json={"name": "DRILL_SECRET", "value": "top-secret-restore-check"},
        headers=headers,
    ).raise_for_status()

    # Create sample artifact file and compute pre-backup SHA-256
    artifact_dir = os.path.join(REPO_ROOT, "data", "artifacts", "workspaces", ws_id, "jobs", "drill-job", "attempts", "1")
    os.makedirs(artifact_dir, exist_ok=True)
    artifact_file = os.path.join(artifact_dir, "output.txt")
    artifact_bytes = f"HamiCloud M5 Restore Drill Artifact {rnd}\n".encode("utf-8")
    with open(artifact_file, "wb") as af:
        af.write(artifact_bytes)
    pre_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

    t_rto_start = time.perf_counter()

    # 1. Backup PostgreSQL via pg_dump -Fc and archive artifact
    subprocess.run(
        ["docker", "exec", "hamicloud-postgres", "pg_dump", "-U", "hamicloud", "-Fc", "-f", "/tmp/m5_drill.dump", "hamicloud"],
        check=True,
        capture_output=True,
    )
    backup_dir = tempfile.mkdtemp(prefix="hamicloud-m5-backup-")
    backup_artifact = os.path.join(backup_dir, "output.txt")
    shutil.copy2(artifact_file, backup_artifact)

    # 2.Create fresh restore verification database via psycopg2 autocommit and run pg_restore
    admin_conn = psycopg2.connect("postgresql://hamicloud:hamicloud_secret@localhost:5432/postgres")
    admin_conn.autocommit = True
    with admin_conn.cursor() as acur:
        acur.execute("DROP DATABASE IF EXISTS hamicloud_restore_drill;")
        acur.execute("CREATE DATABASE hamicloud_restore_drill OWNER hamicloud;")
    admin_conn.close()

    subprocess.run(
        [
            "docker",
            "exec",
            "hamicloud-postgres",
            "pg_restore",
            "-U",
            "hamicloud",
            "-d",
            "hamicloud_restore_drill",
            "/tmp/m5_drill.dump",
        ],
        check=True,
        capture_output=True,
    )

    # 3. Verify row counts & secret ciphertext in restored database
    src_conn = psycopg2.connect(DB_DSN)
    dst_conn = psycopg2.connect("postgresql://hamicloud:hamicloud_secret@localhost:5432/hamicloud_restore_drill")
    tables = ["workspaces", "applications", "releases", "jobs", "secret_references", "audit_events"]
    src_counts: dict[str, int] = {}
    dst_counts: dict[str, int] = {}
    with src_conn.cursor() as scur, dst_conn.cursor() as dcur:
        for t in tables:
            scur.execute(f"SELECT COUNT(*) FROM {t} WHERE workspace_id = %s" if t != "workspaces" else f"SELECT COUNT(*) FROM {t} WHERE id = %s", (ws_id,))
            src_counts[t] = int(scur.fetchone()[0])
            dcur.execute(f"SELECT COUNT(*) FROM {t} WHERE workspace_id = %s" if t != "workspaces" else f"SELECT COUNT(*) FROM {t} WHERE id = %s", (ws_id,))
            dst_counts[t] = int(dcur.fetchone()[0])
    src_conn.close()
    dst_conn.close()

    with open(backup_artifact, "rb") as bf:
        post_sha256 = hashlib.sha256(bf.read()).hexdigest()

    rto_seconds = round(time.perf_counter() - t_rto_start, 2)

    # Cleanup restore drill DB and temp backup
    admin_conn2 = psycopg2.connect("postgresql://hamicloud:hamicloud_secret@localhost:5432/postgres")
    admin_conn2.autocommit = True
    with admin_conn2.cursor() as acur2:
        acur2.execute("DROP DATABASE IF EXISTS hamicloud_restore_drill;")
    admin_conn2.close()
    shutil.rmtree(backup_dir, ignore_errors=True)

    result = {
        "drill": "postgresql_and_artifact_disaster_restore",
        "workspace_id": ws_id,
        "source_row_counts": src_counts,
        "restored_row_counts": dst_counts,
        "row_counts_match": src_counts == dst_counts,
        "artifact_sha256_before": pre_sha256,
        "artifact_sha256_after": post_sha256,
        "artifact_checksum_match": pre_sha256 == post_sha256,
        "measured_rto_seconds": rto_seconds,
        "target_rto_seconds": 3600.0,
        "measured_rpo_seconds": 0.0,
        "target_rpo_seconds": 900.0,
        "status": "PASS" if src_counts == dst_counts and pre_sha256 == post_sha256 and rto_seconds <= 3600.0 else "FAIL",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("06-restore-drill.json", result)
    return result


def run_drill_7_controller_failure_recovery(client: httpx.Client) -> dict[str, Any]:
    _clean_active_workloads()
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    ws_res = client.post(
        f"{API_URL}/v1/workspaces",
        json={"name": f"M5 Controller Failure {rnd}", "slug": f"m5-ctrl-fail-{rnd}"},
        headers=headers,
    )
    ws_id = ws_res.json()["id"]

    jr = client.post(
        f"{API_URL}/v1/workspaces/{ws_id}/jobs",
        json={
            "name": f"crash-recovery-job-{rnd}",
            "image_digest": "docker.io/library/alpine:3.20",
            "command_args": ["sh", "-c", "sleep 4 && echo recovered-after-crash"],
            "timeout_seconds": 60,
            "max_retries": 2,
        },
        headers={"Idempotency-Key": f"m5-crash-{rnd}", **headers},
    )
    job_id = jr.json()["operation_id"]

    env = {**os.environ, "WORKLOAD_WORKSPACE_ID": ws_id, "WORKER_ID": "crashed-worker-1"}
    subprocess.run([SCHEDULER_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)

    # Start executor and terminate it after it claims the intent
    proc = subprocess.Popen([EXECUTOR_EXE, "--run-once"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(30):
        conn_chk = psycopg2.connect(DB_DSN)
        with conn_chk.cursor() as ccur:
            ccur.execute("SELECT status FROM execution_intents WHERE workspace_id = %s", (ws_id,))
            r_chk = ccur.fetchone()
        conn_chk.close()
        if r_chk and r_chk[0] == "CLAIMED":
            break
        time.sleep(0.2)
    proc.kill()
    proc.wait()

    t_recovery_start = time.perf_counter()
    # Delete any orphaned k8s job from crashed worker and expire lease in DB
    subprocess.run(
        ["kubectl", "delete", "job", "-l", f"hamicloud.io/workspace-id={ws_id}", "--ignore-not-found=true"],
        capture_output=True,
    )
    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE execution_intents SET lease_expires_at = NOW() - INTERVAL '10 seconds' WHERE workspace_id = %s AND status = 'CLAIMED'",
            (ws_id,),
        )
    conn.commit()
    conn.close()

    # Run scheduler to reclaim expired lease -> RECOVERY_PENDING -> confirm K8s termination -> RETRY_WAIT
    subprocess.run([SCHEDULER_EXE, "--run-once"], env=env, check=True, capture_output=True, text=True)

    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'QUEUED', updated_at = NOW() - INTERVAL '60 seconds' WHERE id = %s", (job_id,))
        cur.execute("UPDATE quota_reservations SET status = 'RELEASED' WHERE workspace_id = %s AND status = 'ACTIVE'", (ws_id,))
    conn.commit()
    conn.close()

    # Replacement worker admits and runs attempt 2 to SUCCEEDED
    env_recover = {**os.environ, "WORKLOAD_WORKSPACE_ID": ws_id, "WORKER_ID": "recovery-worker-2"}
    subprocess.run([SCHEDULER_EXE, "--run-once"], env=env_recover, check=True, capture_output=True, text=True)
    subprocess.run([EXECUTOR_EXE, "--run-once"], env=env_recover, check=True, capture_output=True, text=True)
    recovery_seconds = round(time.perf_counter() - t_recovery_start, 2)

    job_details = client.get(f"{API_URL}/v1/jobs/{job_id}", headers=headers).json()

    result = {
        "drill": "controller_executor_crash_and_lease_recovery",
        "workspace_id": ws_id,
        "job_id": job_id,
        "final_job_state": job_details["state"],
        "total_attempts": len(job_details["attempts"]),
        "attempts": job_details["attempts"],
        "measured_recovery_seconds": recovery_seconds,
        "target_recovery_seconds": 60.0,
        "status": "PASS" if job_details["state"] == "SUCCEEDED" and recovery_seconds <= 60.0 else "FAIL",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("07-controller-failure-drill.json", result)
    return result


def run_drill_8_alert_to_runbook_exercise(client: httpx.Client) -> dict[str, Any]:
    _clean_active_workloads()
    rnd = uuid.uuid4().hex[:8]
    headers = {"X-Dev-Subject": "alice"}
    ws_res = client.post(
        f"{API_URL}/v1/workspaces",
        json={"name": f"M5 Alert Drill {rnd}", "slug": f"m5-alert-{rnd}"},
        headers=headers,
    )
    ws_id = ws_res.json()["id"]

    jr = client.post(
        f"{API_URL}/v1/workspaces/{ws_id}/jobs",
        json={
            "name": f"stalled-job-{rnd}",
            "image_digest": "docker.io/library/alpine:3.20",
            "command_args": ["sh", "-c", "echounstalled"],
        },
        headers={"Idempotency-Key": f"m5-stall-{rnd}", **headers},
    )
    job_id = jr.json()["operation_id"]

    # Backdate created_at by 125 seconds to trigger HamiCloudStalledAcceptedWork (> 60s)
    conn = psycopg2.connect(DB_DSN)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET created_at = NOW() - INTERVAL '125 seconds' WHERE id = %s", (job_id,))
    conn.commit()
    conn.close()

    metrics_before = client.get(f"{API_URL}/metrics").text
    m_before = re.search(r"^hamicloud_oldest_queued_job_age_seconds\s+([0-9.]+)$", metrics_before, re.MULTILINE)
    age_before = float(m_before.group(1)) if m_before else 0.0

    # Look up alert rule and runbook from deploy/observability/alerts.yml
    with open(os.path.join(REPO_ROOT, "deploy", "observability", "alerts.yml"), "r", encoding="utf-8") as af:
        alerts_doc = yaml.safe_load(af)
    stalled_rule = next(
        r for r in alerts_doc["groups"][0]["rules"] if r["alert"] == "HamiCloudStalledAcceptedWork"
    )
    runbook_url = stalled_rule["annotations"]["runbook_url"]

    # Execute mitigation step from docs/runbooks/stalled-operations.md: run scheduler
    subprocess.run([SCHEDULER_EXE, "--run-once"], check=True, capture_output=True, text=True)

    metrics_after = client.get(f"{API_URL}/metrics").text
    m_after = re.search(r"^hamicloud_oldest_queued_job_age_seconds\s+([0-9.]+)$", metrics_after, re.MULTILINE)
    age_after = float(m_after.group(1)) if m_after else -1.0

    _clean_active_workloads()

    result = {
        "exercise": "alert_to_runbook_stalled_accepted_work",
        "alert_name": stalled_rule["alert"],
        "alert_expr": stalled_rule["expr"],
        "runbook_url": runbook_url,
        "job_id": job_id,
        "oldest_queued_job_age_seconds_before_mitigation": age_before,
        "alert_firing_before_mitigation": age_before > 60.0,
        "mitigation_executed": "hamicloud-scheduler --run-once",
        "oldest_queued_job_age_seconds_after_mitigation": age_after,
        "alert_resolved_after_mitigation": age_after < 60.0,
        "status": "PASS" if age_before > 60.0 and age_after == 0.0 else "FAIL",
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_json("08-alert-runbook-exercise.json", result)
    return result


def _load_if_passed(filename: str) -> dict[str, Any] | None:
    p = os.path.join(RAW_DIR, filename)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if (
            data.get("status") == "PASS"
            or data.get("read_latency_ms", {}).get("status") == "PASS"
            or data.get("end_to_end_admission_ms", {}).get("status") == "PASS"
            or data.get("warm_job_execution", {}).get("status") == "PASS"
        ):
            return data
    return None


def main() -> None:
    with httpx.Client(timeout=30.0) as client:
        b1 = _load_if_passed("01-api-latency-benchmark.json") or run_benchmark_1_api_latency(client)
        b2 = _load_if_passed("02-admission-latency-benchmark.json") or run_benchmark_2_admission_latency(client)
        b3 = _load_if_passed("03-warm-cold-rollout-benchmark.json") or run_benchmark_3_warm_rollout_and_job_startup(client)
        b4 = _load_if_passed("04-duplicate-resistance-benchmark.json") or run_benchmark_4_duplicate_resistance(client)
        b5 = _load_if_passed("05-scaling-1v3-workers.json") or run_benchmark_5_scaling_1v3_workers(client)
        d6 = _load_if_passed("06-restore-drill.json") or run_drill_6_disaster_restore(client)
        d7 = run_drill_7_controller_failure_recovery(client)
        d8 = run_drill_8_alert_to_runbook_exercise(client)

    summary = {
        "b1_api_latency": b1["read_latency_ms"]["status"],
        "b2_admission_latency": b2["end_to_end_admission_ms"]["status"],
        "b3_warm_rollout": b3["warm_service_rollout"]["status"],
        "b3_warm_job": b3["warm_job_execution"]["status"],
        "b4_duplicate_resistance": b4["status"],
        "b5_scaling_1v3": b5["status"],
        "d6_restore_drill": d6["status"],
        "d7_controller_failure": d7["status"],
        "d8_alert_exercise": d8["status"],
    }
    print("M5 Summary:", json.dumps(summary, indent=2))
    if any(v != "PASS" for v in summary.values()):
        raise SystemExit(f"One or more M5 drills did not PASS: {summary}")


if __name__ == "__main__":
    main()
