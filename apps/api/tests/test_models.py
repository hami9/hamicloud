import uuid
from app.models.application import Application, WorkloadType
from app.models.execution_intent import ExecutionIntent, IntentResourceType, IntentStatus
from app.models.job import Job, JobAttempt, JobState
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.quota import QuotaReservation, QuotaResourceClass, QuotaStatus
from app.models.workspace import Workspace, WorkspaceMembership, WorkspaceRole


def test_workspace_and_membership_models():
    ws_id = uuid.uuid4()
    ws = Workspace(id=ws_id, name="Test Workspace", slug="test-ws")
    assert ws.name == "Test Workspace"
    assert ws.slug == "test-ws"

    member = WorkspaceMembership(
        workspace_id=ws_id,
        user_subject="user-sub-123",
        role=WorkspaceRole.OWNER,
    )
    assert member.role == WorkspaceRole.OWNER
    assert member.user_subject == "user-sub-123"


def test_job_and_attempt_states():
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        workspace_id=uuid.uuid4(),
        name="data-processor",
        image_digest="registry.local/worker@sha256:abc1234",
        command_args=["run", "--batch"],
        state=JobState.QUEUED,
        max_retries=3,
    )
    assert job.state == JobState.QUEUED
    assert job.max_retries == 3

    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=1,
        state=JobState.RUNNING,
        lease_epoch=1,
    )
    assert attempt.state == JobState.RUNNING
    assert attempt.lease_epoch == 1


def test_execution_intent_lease_epoch():
    intent = ExecutionIntent(
        workspace_id=uuid.uuid4(),
        resource_type=IntentResourceType.JOB_ATTEMPT,
        resource_id=uuid.uuid4(),
        target_generation=1,
        deterministic_resource_name="hc-job-1234-1",
        status=IntentStatus.PENDING,
        lease_epoch=0,
    )
    assert intent.status == IntentStatus.PENDING
    assert intent.lease_epoch == 0
    assert intent.deterministic_resource_name == "hc-job-1234-1"


def test_outbox_event_model():
    evt_id = uuid.uuid4()
    event = OutboxEvent(
        event_id=evt_id,
        topic="job.submitted.v1",
        payload_json={"job_id": "test-id"},
        status=OutboxStatus.PENDING,
    )
    assert event.topic == "job.submitted.v1"
    assert event.status == OutboxStatus.PENDING
    assert event.event_id == evt_id
