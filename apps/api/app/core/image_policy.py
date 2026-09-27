from fastapi import HTTPException, status

from app.core.config import settings
from app.schemas.common import ErrorCode


def validate_image_policy(image_digest: str) -> None:
    """Validate image digest against approved registry allowlist (Decision D13, ADR-0004 §8).

    Raises HTTP 422 Unprocessable Content with error_code=IMAGE_POLICY_VIOLATION
    if the image does not match any approved prefix or pattern.
    """
    allowed_patterns = settings.APPROVED_IMAGE_REGISTRIES
    if "*" in allowed_patterns:
        return

    for pattern in allowed_patterns:
        if pattern.endswith("*"):
            if image_digest.startswith(pattern[:-1]):
                return
        elif image_digest.startswith(pattern):
            return

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error_code": ErrorCode.IMAGE_POLICY_VIOLATION.value,
            "message": f"Image '{image_digest}' violates platform image policy.",
            "image_digest": image_digest,
            "allowed_registries": allowed_patterns,
        },
    )
