from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
import re
import time
from typing import Any, Optional
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dead_letter import DeadLetterRecord
from app.models.execution_intent import ExecutionIntent, IntentStatus
from app.models.job import Job, JobAttempt, JobState
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.release import Release, ReleaseStatus

_correlation_id_ctx: ContextVar[str] = ContextVar("correlation_id", default="")
_trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")
_traceparent_ctx: ContextVar[str] = ContextVar("traceparent", default="")

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def parse_or_create_traceparent(
    raw_traceparent: Optional[str],
    raw_trace_id: Optional[str] = None,
) -> tuple[str, str]:
    """Parse W3C traceparent header or generate a valid trace_id and traceparent."""
    if raw_traceparent:
        candidate = raw_traceparent.strip().lower()
        match = _TRACEPARENT_RE.match(candidate)
        if match and match.group(1) != "0" * 32 and match.group(2) != "0" * 16:
            trace_id = match.group(1)
            flags = match.group(3)
            span_id = uuid.uuid4().hex[:16]
            return trace_id, f"00-{trace_id}-{span_id}-{flags}"

    if raw_trace_id:
        cleaned = re.sub(r"[^0-9a-f]", "", raw_trace_id.strip().lower())
        if len(cleaned) == 32 and cleaned != "0" * 32:
            span_id = uuid.uuid4().hex[:16]
            return cleaned, f"00-{cleaned}-{span_id}-01"

    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return trace_id, f"00-{trace_id}-{span_id}-01"


def set_trace_context(correlation_id: str, trace_id: str, traceparent: str) -> None:
    _correlation_id_ctx.set(correlation_id)
    _trace_id_ctx.set(trace_id)
    _traceparent_ctx.set(traceparent)


def get_trace_context() -> dict[str, str]:
    ctx: dict[str, str] = {}
    cid = _correlation_id_ctx.get()
    tid = _trace_id_ctx.get()
    tp = _traceparent_ctx.get()
    if cid:
        ctx["correlation_id"] = cid
    if tid:
        ctx["trace_id"] = tid
    if tp:
        ctx["traceparent"] = tp
    return ctx


class TelemetryCollector:
    """In-process HTTP latency/status tracker + live DB operational gauge collector."""

    def __init__(self, max_samples: int = 5000) -> None:
        self._max_samples = max_samples
        self._durations_ms: deque[float] = deque(maxlen=max_samples)
        self._status_counts: dict[str, int] = {
            "2xx": 0,
            "3xx": 0,
            "4xx": 0,
            "5xx": 0,
        }
        self._started_at = time.time()

    def record_request(self, status_code: int, duration_ms: float) -> None:
        if 200 <= status_code < 300:
            self._status_counts["2xx"] += 1
        elif 300 <= status_code < 400:
            self._status_counts["3xx"] += 1
        elif 400 <= status_code < 500:
            self._status_counts["4xx"] += 1
        elif status_code >= 500:
            self._status_counts["5xx"] += 1
        self._durations_ms.append(max(0.0, float(duration_ms)))

    def get_http_summary(self) -> dict[str, Any]:
        samples = sorted(self._durations_ms)
        n = len(samples)

        def _pct(p: float) -> float:
            if n == 0:
                return 0.0
            idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
            return round(samples[idx], 3)

        total_reqs = sum(self._status_counts.values())
        err_5xx = self._status_counts["5xx"]
        err_rate = round(err_5xx / total_reqs, 6) if total_reqs > 0 else 0.0
        return {
            "requests_total": total_reqs,
            "status_2xx": self._status_counts["2xx"],
            "status_4xx": self._status_counts["4xx"],
            "status_5xx": err_5xx,
            "error_rate_5xx": err_rate,
            "latency_p50_ms": _pct(50.0),
            "latency_p95_ms": _pct(95.0),
            "latency_p99_ms": _pct(99.0),
        }

    async def collect_metrics(
        self,
        db: AsyncSession,
        workspace_id: Optional[uuid.UUID] = None,
    ) -> dict[str, Any]:
        http_summary = self.get_http_summary()
        now = datetime.now(timezone.utc)

        # 1. Pending outbox events
        outbox_stmt = select(func.count()).select_from(OutboxEvent).where(
            OutboxEvent.status == OutboxStatus.PENDING
        )
        if workspace_id is not None:
            outbox_stmt = outbox_stmt.where(OutboxEvent.workspace_id == workspace_id)
        outbox_pending = int((await db.execute(outbox_stmt)).scalar() or 0)

        # 2. Active executor leases (CLAIMED intents)
        leases_stmt = select(func.count()).select_from(ExecutionIntent).where(
            ExecutionIntent.status == IntentStatus.CLAIMED
        )
        if workspace_id is not None:
            leases_stmt = leases_stmt.where(ExecutionIntent.workspace_id == workspace_id)
        active_leases = int((await db.execute(leases_stmt)).scalar() or 0)

        # 3. Queued jobs count & oldest queued job age
        queued_stmt = select(func.count(), func.min(Job.created_at)).where(
            Job.state == JobState.QUEUED
        )
        if workspace_id is not None:
            queued_stmt = queued_stmt.where(Job.workspace_id == workspace_id)
        queued_row = (await db.execute(queued_stmt)).one()
        queued_jobs = int(queued_row[0] or 0)
        oldest_created_at: Optional[datetime] = queued_row[1]
        oldest_queued_age_s = 0.0
        if oldest_created_at is not None:
            if oldest_created_at.tzinfo is None:
                oldest_created_at = oldest_created_at.replace(tzinfo=timezone.utc)
            oldest_queued_age_s = round(max(0.0, (now - oldest_created_at).total_seconds()), 3)

        # 4. Running jobs count
        running_stmt = select(func.count()).select_from(Job).where(
            Job.state.in_([JobState.ADMITTED, JobState.STARTING, JobState.RUNNING])
        )
        if workspace_id is not None:
            running_stmt = running_stmt.where(Job.workspace_id == workspace_id)
        running_jobs = int((await db.execute(running_stmt)).scalar() or 0)

        # 5. Healthy services count
        healthy_stmt = select(func.count()).select_from(Release).where(
            Release.status == ReleaseStatus.HEALTHY
        )
        if workspace_id is not None:
            healthy_stmt = healthy_stmt.where(Release.workspace_id == workspace_id)
        healthy_services = int((await db.execute(healthy_stmt)).scalar() or 0)

        # 6. Job retry ratio
        attempts_stmt = select(func.count()).select_from(JobAttempt)
        retries_stmt = select(func.count()).select_from(JobAttempt).where(
            JobAttempt.attempt_number > 1
        )
        if workspace_id is not None:
            attempts_stmt = attempts_stmt.where(JobAttempt.workspace_id == workspace_id)
            retries_stmt = retries_stmt.where(JobAttempt.workspace_id == workspace_id)
        total_attempts = int((await db.execute(attempts_stmt)).scalar() or 0)
        retry_attempts = int((await db.execute(retries_stmt)).scalar() or 0)
        retry_ratio = round(retry_attempts / total_attempts, 4) if total_attempts > 0 else 0.0

        # 7. Dead-letter records count
        dlq_stmt = select(func.count()).select_from(DeadLetterRecord)
        if workspace_id is not None:
            dlq_stmt = dlq_stmt.where(DeadLetterRecord.workspace_id == workspace_id)
        dlq_total = int((await db.execute(dlq_stmt)).scalar() or 0)

        return {
            **http_summary,
            "outbox_pending_total": outbox_pending,
            "active_leases_total": active_leases,
            "queued_jobs_total": queued_jobs,
            "oldest_queued_job_age_seconds": oldest_queued_age_s,
            "running_jobs_total": running_jobs,
            "healthy_services_total": healthy_services,
            "job_retry_ratio": retry_ratio,
            "dead_letter_total": dlq_total,
            "collected_at": now,
        }

    def render_prometheus(self, data: dict[str, Any]) -> str:
        lines = [
            "# HELP hamicloud_http_requests_total Total HTTP requests handled by status class.",
            "# TYPE hamicloud_http_requests_total counter",
            f'hamicloud_http_requests_total{{status_class="2xx"}} {data["status_2xx"]}',
            f'hamicloud_http_requests_total{{status_class="4xx"}} {data["status_4xx"]}',
            f'hamicloud_http_requests_total{{status_class="5xx"}} {data["status_5xx"]}',
            "# HELP hamicloud_http_request_duration_ms_p50 HTTP request latency p50 in milliseconds.",
            "# TYPE hamicloud_http_request_duration_ms_p50 gauge",
            f'hamicloud_http_request_duration_ms_p50 {data["latency_p50_ms"]}',
            "# HELP hamicloud_http_request_duration_ms_p95 HTTP request latency p95 in milliseconds.",
            "# TYPE hamicloud_http_request_duration_ms_p95 gauge",
            f'hamicloud_http_request_duration_ms_p95 {data["latency_p95_ms"]}',
            "# HELP hamicloud_http_request_duration_ms_p99 HTTP request latency p99 in milliseconds.",
            "# TYPE hamicloud_http_request_duration_ms_p99 gauge",
            f'hamicloud_http_request_duration_ms_p99 {data["latency_p99_ms"]}',
            "# HELP hamicloud_outbox_pending_total Pending transactional outbox events awaiting broker publication.",
            "# TYPE hamicloud_outbox_pending_total gauge",
            f'hamicloud_outbox_pending_total {data["outbox_pending_total"]}',
            "# HELP hamicloud_active_leases_total Active claimed execution intents held by workers.",
            "# TYPE hamicloud_active_leases_total gauge",
            f'hamicloud_active_leases_total {data["active_leases_total"]}',
            "# HELP hamicloud_queued_jobs_total Jobs currently in QUEUED state awaiting scheduler admission.",
            "# TYPE hamicloud_queued_jobs_total gauge",
            f'hamicloud_queued_jobs_total {data["queued_jobs_total"]}',
            "# HELP hamicloud_oldest_queued_job_age_seconds Age in seconds of the oldest QUEUED job.",
            "# TYPE hamicloud_oldest_queued_job_age_seconds gauge",
            f'hamicloud_oldest_queued_job_age_seconds {data["oldest_queued_job_age_seconds"]}',
            "# HELP hamicloud_running_jobs_total Jobs currently admitted, starting, or running.",
            "# TYPE hamicloud_running_jobs_total gauge",
            f'hamicloud_running_jobs_total {data["running_jobs_total"]}',
            "# HELP hamicloud_healthy_services_total Healthy serving releases across workspaces.",
            "# TYPE hamicloud_healthy_services_total gauge",
            f'hamicloud_healthy_services_total {data["healthy_services_total"]}',
            "# HELP hamicloud_job_retry_ratio Ratio of retried job attempts (attempt > 1) to total attempts.",
            "# TYPE hamicloud_job_retry_ratio gauge",
            f'hamicloud_job_retry_ratio {data["job_retry_ratio"]}',
            "# HELP hamicloud_dead_letter_total Dead-lettered job records.",
            "# TYPE hamicloud_dead_letter_total gauge",
            f'hamicloud_dead_letter_total {data["dead_letter_total"]}',
            "",
        ]
        return "\n".join(lines)


telemetry_collector = TelemetryCollector()
