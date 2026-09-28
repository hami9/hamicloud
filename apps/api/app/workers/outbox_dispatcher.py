import asyncio
from datetime import datetime, timedelta
import json
import logging
from typing import Optional, Sequence, Union
import uuid
from nats.aio.client import Client as NATSClient
from nats.js import JetStreamContext
from nats.js.api import StreamConfig
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.db.base import utc_now
from app.db.session import AsyncSessionLocal
from app.models.outbox import OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)

DEFAULT_SUBJECTS = ["job.>", "app.>", "workload.>"]


class OutboxDispatcher:
    """Drains transactional outbox events from PostgreSQL and publishes to NATS JetStream (ADR-0002).

    Uses `SELECT FOR UPDATE SKIP LOCKED` for concurrent multi-worker safety,
    publishes with deterministic `Nats-Msg-Id` for at-least-once deduplication,
    and updates event status to PUBLISHED upon broker acknowledgment.
    """

    def __init__(
        self,
        nats_url: Optional[str] = None,
        stream_name: Optional[str] = None,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        poll_interval: Optional[float] = None,
        batch_size: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        self.nats_url = nats_url or settings.NATS_URL
        self.stream_name = stream_name or settings.NATS_STREAM_NAME
        self.session_factory = session_factory or AsyncSessionLocal
        self.poll_interval = poll_interval or settings.OUTBOX_POLL_INTERVAL_SECONDS
        self.batch_size = batch_size or settings.OUTBOX_BATCH_SIZE
        self.max_retries = max_retries or settings.OUTBOX_MAX_RETRIES

        self.nc: Optional[NATSClient] = None
        self.js: Optional[JetStreamContext] = None
        self._stop_event = asyncio.Event()
        self._last_purge_at: Optional[datetime] = None

    async def connect(self) -> None:
        """Establish connection to NATS and ensure JetStream stream exists."""
        if self.nc is None or not self.nc.is_connected:
            self.nc = NATSClient()
            await self.nc.connect(self.nats_url)
            self.js = self.nc.jetstream()

            # Ensure durable Stream exists
            try:
                await self.js.add_stream(
                    name=self.stream_name,
                    config=StreamConfig(
                        name=self.stream_name,
                        subjects=DEFAULT_SUBJECTS,
                    ),
                )
                logger.info("JetStream stream '%s' initialized with subjects %s", self.stream_name, DEFAULT_SUBJECTS)
            except Exception as e:
                # Stream might already exist; try updating subjects or verify
                try:
                    stream_info = await self.js.stream_info(self.stream_name)
                    logger.info("JetStream stream '%s' already exists (seq: %d)", self.stream_name, stream_info.state.messages)
                except Exception as inner_e:
                    logger.warning("Could not ensure JetStream stream '%s': %s (inner: %s)", self.stream_name, e, inner_e)

    async def close(self) -> None:
        """Close NATS connection gracefully."""
        if self.nc and self.nc.is_connected:
            await self.nc.drain()
            await self.nc.close()
            self.nc = None
            self.js = None
            logger.info("Outbox dispatcher disconnected from NATS")

    async def drain_batch(self, batch_size: Optional[int] = None) -> int:
        """Fetch and publish a batch of pending outbox events within an isolated transaction.

        Returns the number of events successfully published to JetStream.
        """
        if self.js is None:
            await self.connect()

        assert self.js is not None, "JetStream context not initialized"

        limit = batch_size or self.batch_size
        published_count = 0

        async with self.session_factory() as session:
            async with session.begin():
                now = utc_now()
                stmt = (
                    select(OutboxEvent)
                    .where(OutboxEvent.status == OutboxStatus.PENDING)
                    .where(
                        or_(
                            OutboxEvent.next_attempt_at.is_(None),
                            OutboxEvent.next_attempt_at <= now,
                        )
                    )
                    .order_by(OutboxEvent.created_at.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                events = (await session.execute(stmt)).scalars().all()

                if not events:
                    return 0

                for event in events:
                    # Construct message payload and deduplication headers
                    payload_bytes = json.dumps(event.payload_json).encode("utf-8")
                    headers: dict[str, str] = {
                        "Nats-Msg-Id": str(event.event_id),
                        "X-Schema-Version": str(event.schema_version),
                        "X-Workspace-Id": str(event.workspace_id),
                    }
                    if event.headers_json:
                        for k, v in event.headers_json.items():
                            headers[str(k)] = str(v)

                    try:
                        # Bound individual publish network I/O timeout to 2.0s
                        ack = await self.js.publish(
                            subject=event.topic,
                            payload=payload_bytes,
                            headers=headers,
                            timeout=2.0,
                        )
                        event.status = OutboxStatus.PUBLISHED
                        event.published_at = now
                        published_count += 1
                        logger.debug(
                            "Dispatched outbox event %s to topic %s (stream seq %d)",
                            event.event_id,
                            event.topic,
                            ack.seq,
                        )
                    except Exception as exc:
                        # Differentiate permanent, event-specific errors from transient broker outages
                        err_str = str(exc).lower()
                        is_permanent = False
                        if isinstance(exc, (ValueError, TypeError)):
                            is_permanent = True
                        elif "no stream" in err_str or ("no responders" in err_str and "stream" in err_str):
                            is_permanent = True
                        else:
                            try:
                                from nats.js.errors import NoStreamResponseError
                                if isinstance(exc, NoStreamResponseError):
                                    is_permanent = True
                            except ImportError:
                                pass

                        if is_permanent:
                            event.status = OutboxStatus.FAILED
                            logger.error(
                                "Permanent failure for outbox event %s to topic %s: %s",
                                event.event_id,
                                event.topic,
                                exc,
                            )
                        else:
                            # Broker/connection failure: NEVER mark FAILED (ADR-0002).
                            # Keep status=PENDING with exponential backoff.
                            event.retry_count += 1
                            backoff_seconds = min(300, 2 ** min(event.retry_count, 8))
                            event.next_attempt_at = now + timedelta(seconds=backoff_seconds)
                            event.status = OutboxStatus.PENDING
                            logger.warning(
                                "Transient broker failure for outbox event %s to topic %s (attempt %d): %s. Backoff: %ds",
                                event.event_id,
                                event.topic,
                                event.retry_count,
                                exc,
                                backoff_seconds,
                            )

                # Commit updates to all processed events in the batch
                await session.flush()

        return published_count

    async def run(self) -> None:
        """Run continuous draining loop until stopped."""
        logger.info("Starting OutboxDispatcher worker loop...")
        self._stop_event.clear()

        await self.connect()

        try:
            while not self._stop_event.is_set():
                now = utc_now()
                # Scheduled daily purge check (ADR-0002 §4)
                if self._last_purge_at is None or (now - self._last_purge_at).total_seconds() >= 86400:
                    try:
                        async with self.session_factory() as purge_session:
                            purged = await purge_expired_outbox_events(purge_session)
                            if purged > 0:
                                logger.info("Purged %d expired outbox events", purged)
                            self._last_purge_at = now
                    except Exception as pe:
                        logger.error("Error during scheduled outbox purge: %s", pe, exc_info=True)

                try:
                    drained = await self.drain_batch()
                    # If events were drained, immediately try draining next batch (backlog work-stealing)
                    if drained > 0:
                        continue
                except Exception as e:
                    logger.error("Error during outbox drain batch: %s", e, exc_info=True)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()
            logger.info("OutboxDispatcher worker loop terminated")

    def stop(self) -> None:
        """Signal worker loop to stop gracefully."""
        self._stop_event.set()


async def purge_expired_outbox_events(
    session: AsyncSession,
    retention_days: int = 7,
) -> int:
    """Physically purge published outbox events older than retention_days (ADR-0002 §4).

    Returns number of purged rows.
    """
    cutoff = utc_now() - timedelta(days=retention_days)
    stmt = (
        delete(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.PUBLISHED)
        .where(OutboxEvent.published_at < cutoff)
    )
    result = await session.execute(stmt)
    await session.commit()
    rowcount = getattr(result, "rowcount", 0)
    return int(rowcount) if rowcount is not None else 0


async def requeue_failed_outbox_events(
    session: AsyncSession,
    event_ids: Optional[Sequence[Union[uuid.UUID, str]]] = None,
) -> int:
    """Requeue FAILED outbox events back to PENDING with immediate attempt."""
    stmt = (
        update(OutboxEvent)
        .where(OutboxEvent.status == OutboxStatus.FAILED)
        .values(
            status=OutboxStatus.PENDING,
            retry_count=0,
            next_attempt_at=utc_now(),
        )
    )
    if event_ids:
        stmt = stmt.where(OutboxEvent.event_id.in_(event_ids))
    res = await session.execute(stmt)
    await session.commit()
    rowcount = getattr(res, "rowcount", 0)
    return int(rowcount) if rowcount is not None else 0

