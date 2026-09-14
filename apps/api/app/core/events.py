import enum
from typing import Set


class OutboxTopic(str, enum.Enum):
    JOB_SUBMITTED = "job.submitted.v1"
    JOB_CANCELLATION_REQUESTED = "job.cancellation.requested.v1"
    APP_DEPLOYMENT_REQUESTED = "app.deployment.requested.v1"
    JOB_ATTEMPT_FAILED = "job.attempt.failed.v1"
    JOB_ATTEMPT_SUCCEEDED = "job.attempt.succeeded.v1"
    WORKLOAD_RECONCILIATION_REQUESTED = "workload.reconciliation.requested.v1"


