from typing import Optional
from sqlalchemy.exc import IntegrityError


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
