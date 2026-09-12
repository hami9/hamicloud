import uuid
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.models.job import Job, JobState
from app.models.application import Application, WorkloadType
from app.models.release import Release, ReleaseStatus
from app.main import app


def test_job_state_validation():
    from app.models.job import JobState
    assert JobState.QUEUED.value == "QUEUED"
    assert JobState.CANCEL_REQUESTED.value == "CANCEL_REQUESTED"
    assert JobState.SUCCEEDED.value == "SUCCEEDED"
