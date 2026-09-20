import logging
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


def violated_constraint(exc: IntegrityError) -> Optional[str]:
    """Return the name of the constraint the driver reported, or None.

    asyncpg exposes ``constraint_name`` on its own exception, which SQLAlchemy
    wraps twice: ``IntegrityError.orig`` is the dialect wrapper and
    ``orig.__cause__`` is the asyncpg error. Nothing is matched by substring,
    so user-supplied text that happens to contain a constraint name can never
    be mistaken for a constraint violation.
    """
    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None)
    name = getattr(cause, "constraint_name", None) or getattr(orig, "constraint_name", None)
    return name if isinstance(name, str) and name else None


def unexpected_integrity_error(exc: IntegrityError, context: str) -> HTTPException:
    """Log an integrity violation that no write path claims, and build its 500.

    Every mutating endpoint maps the constraints it expects to a status code of
    its own and hands everything else here, so an unmapped violation is a bug in
    the schema or in this service rather than something the caller did. The
    constraint name goes to the log, never to the response body.
    """
    logger.error(
        "Unmapped integrity violation in %s (constraint=%s)",
        context,
        violated_constraint(exc),
        exc_info=exc,
    )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Database integrity constraint violation",
    )
