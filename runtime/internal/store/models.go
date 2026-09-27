package store

import "time"

type IntentStatus string

const (
	IntentStatusPending    IntentStatus = "PENDING"
	IntentStatusClaimed    IntentStatus = "CLAIMED"
	IntentStatusApplied    IntentStatus = "APPLIED"
	IntentStatusTerminated IntentStatus = "TERMINATED"
)

type IntentResourceType string

const (
	IntentResourceServiceRelease IntentResourceType = "SERVICE_RELEASE"
	IntentResourceJobAttempt     IntentResourceType = "JOB_ATTEMPT"
)

type ReleaseStatus string

const (
	ReleaseStatusRequested    ReleaseStatus = "REQUESTED"
	ReleaseStatusBuilding     ReleaseStatus = "BUILDING"
	ReleaseStatusImageReady   ReleaseStatus = "IMAGE_READY"
	ReleaseStatusDeploying    ReleaseStatus = "DEPLOYING"
	ReleaseStatusHealthy      ReleaseStatus = "HEALTHY"
	ReleaseStatusBuildFailed  ReleaseStatus = "BUILD_FAILED"
	ReleaseStatusDeployFailed ReleaseStatus = "DEPLOY_FAILED"
)

type UnadmittedRelease struct {
	ReleaseID         string
	ApplicationID     string
	WorkspaceID       string
	WorkspaceSlug     string
	ApplicationSlug   string
	ReleaseNumber     int
	ImageDigest       string
	ConfigJSON        []byte
	DesiredGeneration int
}

type ClaimedWorkload struct {
	IntentID                  string
	WorkspaceID               string
	WorkspaceSlug             string
	ReleaseID                 string
	ApplicationID             string
	ApplicationSlug           string
	ReleaseNumber             int
	ImageDigest               string
	Port                      int
	HealthPath                string
	TargetGeneration          int
	DeterministicResourceName string
	LeaseEpoch                int
}

type ExecutionIntent struct {
	ID                        string
	WorkspaceID               string
	ResourceType              IntentResourceType
	ReleaseID                 *string
	JobAttemptID              *string
	ResourceUID               *string
	TargetGeneration          int
	DeterministicResourceName string
	Status                    IntentStatus
	ClaimedBy                 *string
	LeaseEpoch                int
	LeaseExpiresAt            *time.Time
	CreatedAt                 time.Time
	UpdatedAt                 time.Time
}

type UnadmittedJob struct {
	JobID                string
	WorkspaceID          string
	WorkspaceSlug        string
	Name                 string
	ImageDigest          string
	CommandArgsJSON      []byte
	EnvVarsJSON          []byte
	TimeoutSeconds       int
	MaxRetries           int
	CurrentAttemptNumber int
	State                string
}

type ClaimedJobWorkload struct {
	IntentID                  string
	JobAttemptID              string
	JobID                     string
	WorkspaceID               string
	WorkspaceSlug             string
	JobName                   string
	ImageDigest               string
	CommandArgs               []string
	EnvVars                   map[string]string
	TimeoutSeconds            int
	MaxRetries                int
	AttemptNumber             int
	DeterministicResourceName string
	LeaseEpoch                int
}
