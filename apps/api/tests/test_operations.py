
from app.models.job import JobState


def test_job_state_validation():
    assert JobState.QUEUED.value == "QUEUED"
    assert JobState.CANCEL_REQUESTED.value == "CANCEL_REQUESTED"
    assert JobState.SUCCEEDED.value == "SUCCEEDED"
