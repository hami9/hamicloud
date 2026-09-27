import asyncio
from datetime import datetime, timedelta, timezone
import uuid
import psycopg2
import pytest
from sqlalchemy import select

from app.core.events import OutboxTopic, create_outbox_event
from app.db.base import utc_now
from app.db.session import AsyncSessionLocal
from app.models.outbox import OutboxEvent, OutboxStatus
from app.workers.outbox_dispatcher import OutboxDispatcher, purge_expired_outbox_events
from tests.conftest import TEST_DATABASE_URL_SYNC

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture
def test_workspace():
    """Create a test workspace for outbox event foreign key references."""
    ws_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(TEST_DATABASE_URL_SYNC)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (%s, %s, %s, %s, %s)",
            (ws_id, "Outbox Test WS", f"outbox-ws-{uuid.uuid4().hex[:8]}", now, now),
        )
    conn.commit()
    conn.close()
    return uuid.UUID(ws_id)


@pytest.mark.asyncio
async def test_outbox_drain_batch_publishes_to_jetstream(test_workspace):
    """Verify that OutboxDispatcher drains pending events, publishes to JetStream, and marks them PUBLISHED."""
    ws_id = test_workspace

    # 1. Insert 3 pending outbox events into the database
    events = [
        create_outbox_event(
            workspace_id=ws_id,
            topic=OutboxTopic.JOB_SUBMITTED,
            payload={"job_id": str(uuid.uuid4()), "action": "submit"},
            headers={"X-Test-Trace": "trace-1"},
        ),
        create_outbox_event(
            workspace_id=ws_id,
            topic=OutboxTopic.APP_DEPLOYMENT_REQUESTED,
            payload={"app_id": str(uuid.uuid4()), "generation": 1},
            headers={"X-Test-Trace": "trace-2"},
        ),
        create_outbox_event(
            workspace_id=ws_id,
            topic=OutboxTopic.JOB_CANCELLATION_REQUESTED,
            payload={"job_id": str(uuid.uuid4()), "reason": "user_cancelled"},
            headers={"X-Test-Trace": "trace-3"},
        ),
    ]

    event_ids = [e.event_id for e in events]

    async with AsyncSessionLocal() as session:
        for e in events:
            session.add(e)
        await session.commit()

    # 2. Run OutboxDispatcher drain_batch
    dispatcher = OutboxDispatcher()
    try:
        await dispatcher.connect()
        drained_count = await dispatcher.drain_batch(batch_size=10)
        assert drained_count == 3

        # 3. Verify in database that status is PUBLISHED and published_at is set
        async with AsyncSessionLocal() as session:
            stmt = select(OutboxEvent).where(OutboxEvent.event_id.in_(event_ids))
            db_events = (await session.execute(stmt)).scalars().all()
            assert len(db_events) == 3
            for db_e in db_events:
                assert db_e.status == OutboxStatus.PUBLISHED
                assert db_e.published_at is not None
                assert db_e.retry_count == 0

        # 4. Draining again returns 0 since no pending events remain
        second_drain = await dispatcher.drain_batch(batch_size=10)
        assert second_drain == 0

    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_outbox_retry_on_publish_failure(test_workspace):
    """Verify that publish failures increment retry_count and eventually transition to FAILED."""
    ws_id = test_workspace

    event = create_outbox_event(
        workspace_id=ws_id,
        topic=OutboxTopic.JOB_SUBMITTED,
        payload={"job_id": str(uuid.uuid4())},
    )
    event_id = event.event_id

    async with AsyncSessionLocal() as session:
        session.add(event)
        await session.commit()

    dispatcher = OutboxDispatcher(max_retries=3)
    try:
        await dispatcher.connect()

        # Simulate publish failure by replacing js.publish with a failing stub
        assert dispatcher.js is not None

        async def failing_publish(*args, **kwargs):
            raise RuntimeError("Simulated NATS cluster outage")

        dispatcher.js.publish = failing_publish  # type: ignore[assignment]

        # 1. First failure
        drained = await dispatcher.drain_batch(batch_size=10)
        assert drained == 0

        async with AsyncSessionLocal() as session:
            db_e = (await session.execute(select(OutboxEvent).where(OutboxEvent.event_id == event_id))).scalar_one()
            assert db_e.status == OutboxStatus.PENDING
            assert db_e.retry_count == 1

        # 2. Second failure
        await dispatcher.drain_batch(batch_size=10)
        async with AsyncSessionLocal() as session:
            db_e = (await session.execute(select(OutboxEvent).where(OutboxEvent.event_id == event_id))).scalar_one()
            assert db_e.status == OutboxStatus.PENDING
            assert db_e.retry_count == 2

        # 3. Third failure -> Reaches max_retries=3 -> Marked FAILED
        await dispatcher.drain_batch(batch_size=10)
        async with AsyncSessionLocal() as session:
            db_e = (await session.execute(select(OutboxEvent).where(OutboxEvent.event_id == event_id))).scalar_one()
            assert db_e.status == OutboxStatus.FAILED
            assert db_e.retry_count == 3

    finally:
        await dispatcher.close()


@pytest.mark.asyncio
async def test_purge_expired_outbox_events(test_workspace):
    """Verify physical purging of published outbox events older than retention window (ADR-0002 §4)."""
    ws_id = test_workspace
    now = utc_now()

    # 1. Old published event (10 days old) -> Should be purged
    old_event = create_outbox_event(
        workspace_id=ws_id,
        topic=OutboxTopic.JOB_SUBMITTED,
        payload={"job_id": str(uuid.uuid4())},
    )
    old_event.status = OutboxStatus.PUBLISHED
    old_event.published_at = now - timedelta(days=10)

    # 2. Recent published event (2 days old) -> Should be retained
    recent_event = create_outbox_event(
        workspace_id=ws_id,
        topic=OutboxTopic.JOB_SUBMITTED,
        payload={"job_id": str(uuid.uuid4())},
    )
    recent_event.status = OutboxStatus.PUBLISHED
    recent_event.published_at = now - timedelta(days=2)

    # 3. Pending event -> Should NEVER be purged regardless of age
    pending_event = create_outbox_event(
        workspace_id=ws_id,
        topic=OutboxTopic.JOB_SUBMITTED,
        payload={"job_id": str(uuid.uuid4())},
    )

    async with AsyncSessionLocal() as session:
        session.add_all([old_event, recent_event, pending_event])
        await session.commit()

    # Run purge with 7-day retention
    async with AsyncSessionLocal() as session:
        purged = await purge_expired_outbox_events(session, retention_days=7)
        assert purged == 1

    # Verify database contents
    async with AsyncSessionLocal() as session:
        # Old event is gone
        old_check = (await session.execute(
            select(OutboxEvent).where(OutboxEvent.event_id == old_event.event_id)
        )).scalar_one_or_none()
        assert old_check is None

        # Recent event still exists
        recent_check = (await session.execute(
            select(OutboxEvent).where(OutboxEvent.event_id == recent_event.event_id)
        )).scalar_one_or_none()
        assert recent_check is not None

        # Pending event still exists
        pending_check = (await session.execute(
            select(OutboxEvent).where(OutboxEvent.event_id == pending_event.event_id)
        )).scalar_one_or_none()
        assert pending_check is not None


@pytest.mark.asyncio
async def test_outbox_dispatcher_worker_loop_start_and_stop():
    """Verify that OutboxDispatcher worker loop starts, runs, and terminates cleanly when stopped."""
    dispatcher = OutboxDispatcher(poll_interval=0.1)

    task = asyncio.create_task(dispatcher.run())
    # Let worker loop initialize and complete at least one cycle
    await asyncio.sleep(0.3)

    # Signal graceful stop
    dispatcher.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
    assert not task.cancelled()
