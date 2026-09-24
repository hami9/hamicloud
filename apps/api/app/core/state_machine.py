"""Job state machine transitions and validation according to Roadmap, ADR-0003, and contracts."""

from typing import Dict, Set

from app.models.job import JobState


class JobTransitionError(Exception):
    """Base error for state transition violations."""

    pass


class TerminalStateError(JobTransitionError):
    """Raised when attempting to transition out of a terminal state."""

    pass


class InvalidStateTransitionError(JobTransitionError):
    """Raised when an edge between two non-terminal states is illegal."""

    pass


TERMINAL_JOB_STATES: Set[JobState] = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.CANCELLED,
}

LEGAL_JOB_TRANSITIONS: Dict[JobState, Set[JobState]] = {
    JobState.QUEUED: {
        JobState.ADMITTED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.ADMITTED: {
        JobState.STARTING,
        JobState.CANCEL_REQUESTED,
    },
    JobState.STARTING: {
        JobState.RUNNING,
        JobState.RETRY_WAIT,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.RUNNING: {
        JobState.SUCCEEDED,
        JobState.RETRY_WAIT,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
        JobState.RECOVERY_PENDING,
    },
    JobState.RETRY_WAIT: {
        JobState.QUEUED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.RECOVERY_PENDING: {
        JobState.RETRY_WAIT,
        JobState.FAILED,
        JobState.CANCEL_REQUESTED,
    },
    JobState.CANCEL_REQUESTED: {
        JobState.CANCELLED,
    },
    JobState.SUCCEEDED: set(),
    JobState.FAILED: set(),
    JobState.CANCELLED: set(),
}


def is_terminal_job_state(state: JobState) -> bool:
    """Return True if state is terminal (no outward transitions permitted)."""
    return state in TERMINAL_JOB_STATES


def validate_job_transition(current: JobState, next_state: JobState) -> None:
    """Validate whether transitioning from current to next_state is legal.

    Raises TerminalStateError if attempting to transition out of a terminal state.
    Raises InvalidStateTransitionError if the transition edge does not exist.
    """
    if is_terminal_job_state(current):
        raise TerminalStateError(f"Cannot transition out of terminal state '{current.value}'")

    allowed = LEGAL_JOB_TRANSITIONS.get(current, set())
    if next_state not in allowed:
        raise InvalidStateTransitionError(
            f"Invalid job state transition from '{current.value}' to '{next_state.value}'"
        )
