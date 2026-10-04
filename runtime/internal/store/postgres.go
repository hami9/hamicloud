package store

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/hami9/hamicloud/runtime/internal/domain"
)

// legalSourcesSQL builds a SQL IN clause containing single-quoted source states
// derived dynamically from domain.LegalTransitions.
func legalSourcesSQL(targets ...domain.JobState) string {
	seen := make(map[domain.JobState]bool)
	var sources []domain.JobState
	for _, target := range targets {
		for _, src := range domain.LegalSourcesFor(target) {
			if !seen[src] {
				seen[src] = true
				sources = append(sources, src)
			}
		}
	}
	sort.Slice(sources, func(i, j int) bool {
		return sources[i] < sources[j]
	})
	parts := make([]string, len(sources))
	for i, s := range sources {
		parts[i] = fmt.Sprintf("'%s'", s)
	}
	return strings.Join(parts, ", ")
}

// NewUUID generates a compliant RFC 4122 v4 UUID without external dependencies.
func NewUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// JobDeleter abstracts deletion or cleanup of dead Kubernetes Job resources during intent recovery.
type JobDeleter interface {
	DeleteJob(ctx context.Context, resourceName string) error
}

type PostgresStore struct {
	pool       *pgxpool.Pool
	jobDeleter JobDeleter
}

func (s *PostgresStore) SetJobDeleter(d JobDeleter) {
	s.jobDeleter = d
}

func NewPostgresStore(ctx context.Context, dbURL string) (*PostgresStore, error) {
	cfg, err := pgxpool.ParseConfig(dbURL)
	if err != nil {
		return nil, fmt.Errorf("failed to parse database url: %w", err)
	}
	cfg.MaxConns = 10
	cfg.MinConns = 2

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("failed to connect to postgres: %w", err)
	}

	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("failed to ping postgres: %w", err)
	}

	return &PostgresStore{pool: pool}, nil
}

func (s *PostgresStore) Close() {
	if s.pool != nil {
		s.pool.Close()
	}
}

// ScanUnadmittedReleases finds releases that have not yet had an execution intent created.
// For each application, only the latest release (highest release_number) is eligible,
// skipping superseded releases per ADR-0003.
func (s *PostgresStore) ScanUnadmittedReleases(ctx context.Context, limit int) ([]UnadmittedRelease, error) {
	// In an atomic transaction, mark older releases as SUPERSEDED and terminate their unclaimed PENDING intents
	txSupersede, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin supersede tx: %w", err)
	}
	defer txSupersede.Rollback(ctx)

	// Older releases are only superseded if a newer release is deployable (image_digest != 'pending') per ADR-0003
	supersedeQuery := `
		UPDATE releases
		SET status = 'SUPERSEDED',
		    status_reason = 'Superseded by newer release',
		    updated_at = NOW()
		WHERE status IN ('IMAGE_READY', 'REQUESTED')
		  AND release_number < (
		      SELECT MAX(r2.release_number)
		      FROM releases r2
		      WHERE r2.application_id = releases.application_id
		        AND r2.image_digest != 'pending'
		  );
	`
	if _, err := txSupersede.Exec(ctx, supersedeQuery); err != nil {
		return nil, fmt.Errorf("mark superseded releases: %w", err)
	}

	// Terminate any unclaimed PENDING execution intents for superseded releases in the same transaction
	terminateIntentsQuery := `
		UPDATE execution_intents
		SET status = 'TERMINATED',
		    updated_at = NOW()
		WHERE status = 'PENDING'
		  AND release_id IN (
		      SELECT id FROM releases WHERE status = 'SUPERSEDED'
		  );
	`
	if _, err := txSupersede.Exec(ctx, terminateIntentsQuery); err != nil {
		return nil, fmt.Errorf("terminate superseded release intents: %w", err)
	}

	if err := txSupersede.Commit(ctx); err != nil {
		return nil, fmt.Errorf("commit supersede tx: %w", err)
	}

	query := `
		SELECT r.id, r.application_id, r.workspace_id, w.slug, a.slug, r.release_number, r.image_digest, r.config_json, a.desired_generation
		FROM releases r
		JOIN applications a ON a.id = r.application_id
		JOIN workspaces w ON w.id = r.workspace_id
		WHERE r.status IN ('IMAGE_READY', 'REQUESTED')
		  AND r.image_digest != 'pending'
		  AND r.release_number = (
		      SELECT MAX(r2.release_number)
		      FROM releases r2
		      WHERE r2.application_id = r.application_id
		  )
		  AND NOT EXISTS (
		      SELECT 1 FROM execution_intents ei
		      WHERE ei.release_id = r.id
		        AND ei.target_generation = a.desired_generation
		        AND ei.status IN ('PENDING', 'CLAIMED', 'APPLIED')
		  )
		ORDER BY r.created_at ASC
		LIMIT $1;
	`
	rows, err := s.pool.Query(ctx, query, limit)
	if err != nil {
		return nil, fmt.Errorf("query unadmitted releases: %w", err)
	}
	defer rows.Close()

	var releases []UnadmittedRelease
	for rows.Next() {
		var rel UnadmittedRelease
		err := rows.Scan(
			&rel.ReleaseID,
			&rel.ApplicationID,
			&rel.WorkspaceID,
			&rel.WorkspaceSlug,
			&rel.ApplicationSlug,
			&rel.ReleaseNumber,
			&rel.ImageDigest,
			&rel.ConfigJSON,
			&rel.DesiredGeneration,
		)
		if err != nil {
			return nil, fmt.Errorf("scan release row: %w", err)
		}
		releases = append(releases, rel)
	}
	return releases, nil
}

func safePrefix(s string, length int) string {
	if len(s) > length {
		return s[:length]
	}
	return s
}

// CreateServiceReleaseIntent inserts a durable ExecutionIntent for a service release.
// Deterministic resource naming adheres to ADR-0003: hc-svc-{app_id}-{generation}.
func (s *PostgresStore) CreateServiceReleaseIntent(ctx context.Context, rel UnadmittedRelease) (*ExecutionIntent, error) {
	intentID := NewUUID()
	resourceName := fmt.Sprintf("hc-svc-%s-%d", rel.ApplicationID, rel.DesiredGeneration)
	now := time.Now().UTC()

	query := `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, release_id, target_generation,
			deterministic_resource_name, status, lease_epoch, created_at, updated_at
		) VALUES ($1, $2, 'SERVICE_RELEASE', $3, $4, $5, 'PENDING', 0, $6, $6)
		ON CONFLICT (resource_type, job_attempt_id, release_id, target_generation) DO NOTHING
		RETURNING id, workspace_id, resource_type, release_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at;
	`
	var intent ExecutionIntent
	err := s.pool.QueryRow(ctx, query, intentID, rel.WorkspaceID, rel.ReleaseID, rel.DesiredGeneration, resourceName, now).Scan(
		&intent.ID,
		&intent.WorkspaceID,
		&intent.ResourceType,
		&intent.ReleaseID,
		&intent.TargetGeneration,
		&intent.DeterministicResourceName,
		&intent.Status,
		&intent.LeaseEpoch,
		&intent.CreatedAt,
		&intent.UpdatedAt,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			// Already exists due to ON CONFLICT DO NOTHING
			return nil, nil
		}
		return nil, fmt.Errorf("insert execution intent: %w", err)
	}
	return &intent, nil
}

type releaseConfigPayload struct {
	Port       *int   `json:"port"`
	HealthPath string `json:"health_path"`
}

// ClaimNextServiceRelease atomically claims the next pending service release intent using FOR UPDATE SKIP LOCKED.
func (s *PostgresStore) ClaimNextServiceRelease(ctx context.Context, workerID string, leaseDuration time.Duration, workspaceIDs ...string) (*ClaimedWorkload, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	var selectQuery string
	var queryArgs []any
	if len(workspaceIDs) > 0 && workspaceIDs[0] != "" {
		selectQuery = `
			SELECT ei.id, ei.workspace_id, w.slug, ei.release_id, r.application_id, a.slug,
			       r.release_number, r.image_digest, r.config_json, ei.target_generation,
			       ei.deterministic_resource_name, ei.lease_epoch
			FROM execution_intents ei
			JOIN releases r ON r.id = ei.release_id
			JOIN applications a ON a.id = r.application_id
			JOIN workspaces w ON w.id = ei.workspace_id
			WHERE ei.resource_type = 'SERVICE_RELEASE'
			  AND (ei.status = 'PENDING' OR (ei.status = 'CLAIMED' AND ei.lease_expires_at < NOW()))
			  AND r.status != 'SUPERSEDED'
			  AND ei.workspace_id = $1
			ORDER BY ei.created_at ASC
			LIMIT 1
			FOR UPDATE OF ei SKIP LOCKED;
		`
		queryArgs = append(queryArgs, workspaceIDs[0])
	} else {
		selectQuery = `
			SELECT ei.id, ei.workspace_id, w.slug, ei.release_id, r.application_id, a.slug,
			       r.release_number, r.image_digest, r.config_json, ei.target_generation,
			       ei.deterministic_resource_name, ei.lease_epoch
			FROM execution_intents ei
			JOIN releases r ON r.id = ei.release_id
			JOIN applications a ON a.id = r.application_id
			JOIN workspaces w ON w.id = ei.workspace_id
			WHERE ei.resource_type = 'SERVICE_RELEASE'
			  AND (ei.status = 'PENDING' OR (ei.status = 'CLAIMED' AND ei.lease_expires_at < NOW()))
			  AND r.status != 'SUPERSEDED'
			ORDER BY ei.created_at ASC
			LIMIT 1
			FOR UPDATE OF ei SKIP LOCKED;
		`
	}

	var workload ClaimedWorkload
	var rawConfig []byte
	err = tx.QueryRow(ctx, selectQuery, queryArgs...).Scan(
		&workload.IntentID,
		&workload.WorkspaceID,
		&workload.WorkspaceSlug,
		&workload.ReleaseID,
		&workload.ApplicationID,
		&workload.ApplicationSlug,
		&workload.ReleaseNumber,
		&workload.ImageDigest,
		&rawConfig,
		&workload.TargetGeneration,
		&workload.DeterministicResourceName,
		&workload.LeaseEpoch,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil // No pending intents
		}
		return nil, fmt.Errorf("query pending service release intent: %w", err)
	}

	// Parse configuration
	port := 8080
	healthPath := "/healthz"
	if len(rawConfig) > 0 {
		var cfg releaseConfigPayload
		if err := json.Unmarshal(rawConfig, &cfg); err == nil {
			if cfg.Port != nil && *cfg.Port > 0 {
				port = *cfg.Port
			}
			if cfg.HealthPath != "" {
				healthPath = cfg.HealthPath
			}
		}
	}
	workload.Port = port
	workload.HealthPath = healthPath

	now := time.Now().UTC()
	leaseExpiresAt := now.Add(leaseDuration)
	newEpoch := workload.LeaseEpoch + 1

	updateIntentQuery := `
		UPDATE execution_intents
		SET status = 'CLAIMED',
		    claimed_by = $1,
		    lease_epoch = $2,
		    lease_expires_at = $3,
		    updated_at = $4
		WHERE id = $5;
	`
	if _, err := tx.Exec(ctx, updateIntentQuery, workerID, newEpoch, leaseExpiresAt, now, workload.IntentID); err != nil {
		return nil, fmt.Errorf("update claimed intent: %w", err)
	}

	updateReleaseQuery := `
		UPDATE releases
		SET status = 'DEPLOYING',
		    updated_at = $1
		WHERE id = $2 AND status != 'SUPERSEDED';
	`
	cmdRelease, err := tx.Exec(ctx, updateReleaseQuery, now, workload.ReleaseID)
	if err != nil {
		return nil, fmt.Errorf("update release status to deploying: %w", err)
	}
	if cmdRelease.RowsAffected() == 0 {
		// Release was marked SUPERSEDED concurrently; do not deploy it
		return nil, nil
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, fmt.Errorf("commit claim tx: %w", err)
	}

	workload.LeaseEpoch = newEpoch
	return &workload, nil
}

// MarkReleaseHealthy marks the execution intent APPLIED, release HEALTHY, and updates application current_release_id.
// It enforces monotonic lease epoch fencing to prevent split-brain updates.
// Returns true if this release actually became the application's current_release_id.
func (s *PostgresStore) MarkReleaseHealthy(ctx context.Context, intentID, releaseID, appID, resourceUID string, leaseEpoch int) (bool, error) {
	if leaseEpoch <= 0 {
		return false, fmt.Errorf("lease epoch must be greater than zero, got %d", leaseEpoch)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()

	// 1. Mark intent applied (fenced by lease_epoch)
	intentQuery := `
		UPDATE execution_intents
		SET status = 'APPLIED',
		    resource_uid = $1,
		    updated_at = $2
		WHERE id = $3 AND lease_epoch = $4 AND status = 'CLAIMED';
	`
	cmd, err := tx.Exec(ctx, intentQuery, resourceUID, now, intentID, leaseEpoch)
	if err != nil {
		return false, fmt.Errorf("update intent applied: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return false, fmt.Errorf("fencing error: intent %s epoch %d is stale or no longer claimed", intentID, leaseEpoch)
	}

	// 2. Mark release healthy
	_, err = tx.Exec(ctx, `
		UPDATE releases
		SET status = 'HEALTHY',
		    status_reason = NULL,
		    updated_at = $1
		WHERE id = $2;
	`, now, releaseID)
	if err != nil {
		return false, fmt.Errorf("update release healthy: %w", err)
	}

	// 3. Update application current_release_id ONLY IF intent's target_generation still equals applications.desired_generation
	cmdApp, err := tx.Exec(ctx, `
		UPDATE applications
		SET current_release_id = $1,
		    updated_at = $2
		WHERE id = $3
		  AND desired_generation = (
		      SELECT target_generation FROM execution_intents WHERE id = $4
		  );
	`, releaseID, now, appID, intentID)
	if err != nil {
		return false, fmt.Errorf("update app current_release_id: %w", err)
	}

	becameCurrent := cmdApp.RowsAffected() > 0
	if err := tx.Commit(ctx); err != nil {
		return false, fmt.Errorf("commit tx: %w", err)
	}

	return becameCurrent, nil
}

// MarkReleaseFailed records deploy/readiness failure on the intent and release.
// It enforces monotonic lease epoch fencing to prevent split-brain updates.
func (s *PostgresStore) MarkReleaseFailed(ctx context.Context, intentID, releaseID, reason string, leaseEpoch int) error {
	if leaseEpoch <= 0 {
		return fmt.Errorf("lease epoch must be greater than zero, got %d", leaseEpoch)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()

	intentQuery := `
		UPDATE execution_intents
		SET status = 'TERMINATED',
		    updated_at = $1
		WHERE id = $2 AND lease_epoch = $3 AND status = 'CLAIMED';
	`
	cmd, err := tx.Exec(ctx, intentQuery, now, intentID, leaseEpoch)
	if err != nil {
		return fmt.Errorf("update intent terminated: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return fmt.Errorf("fencing error: intent %s epoch %d is stale or no longer claimed", intentID, leaseEpoch)
	}

	_, err = tx.Exec(ctx, `
		UPDATE releases
		SET status = 'DEPLOY_FAILED',
		    status_reason = $1,
		    updated_at = $2
		WHERE id = $3;
	`, reason, now, releaseID)
	if err != nil {
		return fmt.Errorf("update release deploy_failed: %w", err)
	}

	return tx.Commit(ctx)
}

// RenewLease extends the lease for an in-flight claimed intent, fenced by monotonic lease epoch.
func (s *PostgresStore) RenewLease(ctx context.Context, intentID string, currentEpoch int, extension time.Duration) error {
	if currentEpoch <= 0 {
		return fmt.Errorf("lease epoch must be greater than zero, got %d", currentEpoch)
	}
	now := time.Now().UTC()
	newExpiresAt := now.Add(extension)

	query := `
		UPDATE execution_intents
		SET lease_expires_at = $1,
		    updated_at = $2
		WHERE id = $3
		  AND lease_epoch = $4
		  AND status = 'CLAIMED';
	`
	cmd, err := s.pool.Exec(ctx, query, newExpiresAt, now, intentID, currentEpoch)
	if err != nil {
		return fmt.Errorf("renew lease: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return fmt.Errorf("lease renewal rejected: intent %s epoch %d is stale or no longer claimed", intentID, currentEpoch)
	}
	return nil
}

// ScanUnadmittedJobs finds jobs in QUEUED state awaiting admission.
func (s *PostgresStore) ScanUnadmittedJobs(ctx context.Context, limit int) ([]UnadmittedJob, error) {
	query := `
		SELECT j.id, j.workspace_id, w.slug, j.name, j.image_digest, j.command_args, j.env_vars,
		       j.timeout_seconds, j.max_retries, j.current_attempt_number, j.state
		FROM jobs j
		JOIN workspaces w ON w.id = j.workspace_id
		WHERE j.state = 'QUEUED'
		ORDER BY j.created_at ASC
		LIMIT $1;
	`
	rows, err := s.pool.Query(ctx, query, limit)
	if err != nil {
		return nil, fmt.Errorf("query unadmitted jobs: %w", err)
	}
	defer rows.Close()

	var jobs []UnadmittedJob
	for rows.Next() {
		var j UnadmittedJob
		err := rows.Scan(
			&j.JobID,
			&j.WorkspaceID,
			&j.WorkspaceSlug,
			&j.Name,
			&j.ImageDigest,
			&j.CommandArgsJSON,
			&j.EnvVarsJSON,
			&j.TimeoutSeconds,
			&j.MaxRetries,
			&j.CurrentAttemptNumber,
			&j.State,
		)
		if err != nil {
			return nil, fmt.Errorf("scan job row: %w", err)
		}
		jobs = append(jobs, j)
	}
	return jobs, nil
}

// CreateJobAttemptIntent transactionally admits a job, creates a new JobAttempt, and inserts an ExecutionIntent.
func (s *PostgresStore) CreateJobAttemptIntent(ctx context.Context, job UnadmittedJob) (*ExecutionIntent, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()
	nextAttemptNumber := job.CurrentAttemptNumber + 1

	// Check if retry budget is exceeded. MarkJobAttemptFailed already decides RETRY_WAIT vs FAILED,
	// so this path should be unreachable: return an error instead of writing FAILED.
	if nextAttemptNumber > job.MaxRetries+1 {
		return nil, fmt.Errorf("cannot create attempt: retry budget exhausted (attempt %d > max_retries %d + 1)", nextAttemptNumber, job.MaxRetries)
	}

	// 1. Create JobAttempt
	attemptID := NewUUID()
	insertAttemptQuery := `
		INSERT INTO job_attempts (
			id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at
		) VALUES ($1, $2, $3, $4, 'ADMITTED', 0, $5, $5)
		ON CONFLICT (job_id, attempt_number) DO NOTHING;
	`
	cmd, err := tx.Exec(ctx, insertAttemptQuery, attemptID, job.JobID, job.WorkspaceID, nextAttemptNumber, now)
	if err != nil {
		return nil, fmt.Errorf("insert job attempt: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return nil, nil
	}

	// 2. Transition Job to ADMITTED
	updateJobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = 'ADMITTED',
		    current_attempt_number = $1,
		    updated_at = $2
		WHERE id = $3 AND state IN (%s);
	`, legalSourcesSQL(domain.StateAdmitted))
	cmd, err = tx.Exec(ctx, updateJobQuery, nextAttemptNumber, now, job.JobID)
	if err != nil {
		return nil, fmt.Errorf("update job admitted: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return nil, nil
	}

	// 3. Insert ExecutionIntent (deterministic resource name per attempt: hc-job-{job_id}-{attempt_number})
	intentID := NewUUID()
	resourceName := fmt.Sprintf("hc-job-%s-%d", job.JobID, nextAttemptNumber)

	insertIntentQuery := `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, job_attempt_id, target_generation,
			deterministic_resource_name, status, lease_epoch, created_at, updated_at
		) VALUES ($1, $2, 'JOB_ATTEMPT', $3, $4, $5, 'PENDING', 0, $6, $6)
		ON CONFLICT (resource_type, job_attempt_id, release_id, target_generation) DO NOTHING
		RETURNING id, workspace_id, resource_type, job_attempt_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at;
	`
	var intent ExecutionIntent
	err = tx.QueryRow(ctx, insertIntentQuery, intentID, job.WorkspaceID, attemptID, nextAttemptNumber, resourceName, now).Scan(
		&intent.ID,
		&intent.WorkspaceID,
		&intent.ResourceType,
		&intent.JobAttemptID,
		&intent.TargetGeneration,
		&intent.DeterministicResourceName,
		&intent.Status,
		&intent.LeaseEpoch,
		&intent.CreatedAt,
		&intent.UpdatedAt,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("insert execution intent for job: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, fmt.Errorf("commit admission tx: %w", err)
	}

	return &intent, nil
}

// RequeueRetryWaitJobs finds jobs in RETRY_WAIT whose exponential backoff has elapsed and transitions them to QUEUED.
func (s *PostgresStore) RequeueRetryWaitJobs(ctx context.Context, baseBackoff time.Duration, limit int) (int, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, current_attempt_number, updated_at
		FROM jobs
		WHERE state = 'RETRY_WAIT'
		ORDER BY updated_at ASC
		LIMIT $1;
	`, limit)
	if err != nil {
		return 0, fmt.Errorf("query retry_wait jobs: %w", err)
	}
	defer rows.Close()

	type retryJob struct {
		id            string
		attemptNumber int
		updatedAt     time.Time
	}
	var toCheck []retryJob
	for rows.Next() {
		var r retryJob
		if err := rows.Scan(&r.id, &r.attemptNumber, &r.updatedAt); err != nil {
			return 0, fmt.Errorf("scan retry_wait job: %w", err)
		}
		toCheck = append(toCheck, r)
	}

	now := time.Now().UTC()
	requeuedCount := 0

	for _, j := range toCheck {
		shift := j.attemptNumber - 1
		if shift < 0 {
			shift = 0
		}
		if shift > 5 {
			shift = 5 // Cap multiplier at 2^5 = 32
		}
		backoff := baseBackoff * (1 << shift)
		if backoff > 60*time.Second {
			backoff = 60 * time.Second
		}

		if now.Sub(j.updatedAt) >= backoff {
			cmd, err := s.pool.Exec(ctx, fmt.Sprintf(`
				UPDATE jobs
				SET state = 'QUEUED',
				    updated_at = $1
				WHERE id = $2 AND state IN (%s);
			`, legalSourcesSQL(domain.StateQueued)), now, j.id)
			if err != nil {
				return requeuedCount, fmt.Errorf("requeue job %s: %w", j.id, err)
			}
			if cmd.RowsAffected() > 0 {
				requeuedCount++
			}
		}
	}

	return requeuedCount, nil
}

// ExpiredJobRecoveryInfo contains information required to recover a job attempt whose worker lease expired or whose recovery is incomplete.
type ExpiredJobRecoveryInfo struct {
	IntentID                  string
	AttemptID                 string
	JobID                     string
	AttemptNumber             int
	MaxRetries                int
	LeaseEpoch                int
	JobState                  string
	DeterministicResourceName string
	NeedsBegin                bool
	JobUpdatedAt              time.Time
}

// FindExpiredJobIntents queries:
// 1. CLAIMED job attempt intents whose worker lease has expired.
// 2. Jobs in RECOVERY_PENDING (incomplete recovery from previous pass or failed delete).
// 3. Jobs in STARTING or RUNNING whose latest attempt's intent was TERMINATED by recovery.
func (s *PostgresStore) FindExpiredJobIntents(ctx context.Context) ([]ExpiredJobRecoveryInfo, error) {
	query := `
		SELECT ei.id, ei.job_attempt_id, ja.job_id, ja.attempt_number, j.max_retries, ei.lease_epoch, j.state,
		       ei.deterministic_resource_name,
		       (ei.status = 'CLAIMED' AND ei.lease_expires_at < NOW()) AS needs_begin,
		       j.updated_at
		FROM execution_intents ei
		JOIN job_attempts ja ON ja.id = ei.job_attempt_id
		JOIN jobs j ON j.id = ja.job_id
		WHERE ei.resource_type = 'JOB_ATTEMPT'
		  AND ja.attempt_number = j.current_attempt_number
		  AND (
		    (ei.status = 'CLAIMED' AND ei.lease_expires_at < NOW() AND j.state IN ('STARTING', 'RUNNING', 'CANCEL_REQUESTED'))
		    OR
		    (j.state = 'RECOVERY_PENDING')
		    OR
		    (j.state IN ('STARTING', 'RUNNING') AND ei.status = 'TERMINATED')
		  )
		ORDER BY ei.created_at ASC;
	`
	rows, err := s.pool.Query(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("query expired job intents: %w", err)
	}
	defer rows.Close()

	var expired []ExpiredJobRecoveryInfo
	for rows.Next() {
		var item ExpiredJobRecoveryInfo
		if err := rows.Scan(
			&item.IntentID,
			&item.AttemptID,
			&item.JobID,
			&item.AttemptNumber,
			&item.MaxRetries,
			&item.LeaseEpoch,
			&item.JobState,
			&item.DeterministicResourceName,
			&item.NeedsBegin,
			&item.JobUpdatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan expired job intent: %w", err)
		}
		expired = append(expired, item)
	}
	return expired, nil
}

// BeginJobRecovery begins recovery of an expired job intent in its own database transaction.
// It moves RUNNING jobs to RECOVERY_PENDING, marks the attempt FAILED/CANCELLED, and terminates the intent.
func (s *PostgresStore) BeginJobRecovery(ctx context.Context, item ExpiredJobRecoveryInfo) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin recovery tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()
	isCancel := domain.JobState(item.JobState) == domain.StateCancelRequested

	// Step 1: Follow ADR-0003 and job.v1.json:
	// Only RUNNING transitions to RECOVERY_PENDING.
	// If cancel requested, leave state in CANCEL_REQUESTED.
	// If job was in STARTING, leave state in STARTING (it will transition directly to RETRY_WAIT or FAILED in confirm).
	if !isCancel && domain.JobState(item.JobState) == domain.StateRunning {
		guard := legalSourcesSQL(domain.StateRecoveryPending)
		tag, err := tx.Exec(ctx, fmt.Sprintf(`
			UPDATE jobs
			SET state = 'RECOVERY_PENDING',
			    updated_at = $1
			WHERE id = $2 AND state IN (%s);
		`, guard), now, item.JobID)
		if err != nil {
			return fmt.Errorf("transition job %s to RECOVERY_PENDING: %w", item.JobID, err)
		}
		if tag.RowsAffected() != 1 {
			return fmt.Errorf("expected 1 row affected transitioning job %s to RECOVERY_PENDING, got %d", item.JobID, tag.RowsAffected())
		}
	}

	// Step 2: Finalize attempt state
	attemptState := domain.StateFailed
	failureReason := "Worker lease expired; executor lost"
	exitCode := -1
	if isCancel {
		attemptState = domain.StateCancelled
		failureReason = "Job cancellation confirmed during recovery"
		exitCode = 130
	}

	tagAttempt, err := tx.Exec(ctx, `
		UPDATE job_attempts
		SET state = $1,
		    failure_reason = $2,
		    exit_code = $3,
		    finished_at = $4,
		    updated_at = $4
		WHERE id = $5 AND state IN ('STARTING', 'RUNNING');
	`, string(attemptState), failureReason, exitCode, now, item.AttemptID)
	if err != nil {
		return fmt.Errorf("finalize expired attempt %s: %w", item.AttemptID, err)
	}
	if tagAttempt.RowsAffected() != 1 {
		return fmt.Errorf("expected 1 row affected finalizing attempt %s, got %d", item.AttemptID, tagAttempt.RowsAffected())
	}

	// Step 3: Mark intent TERMINATED with lease_epoch check AND status='CLAIMED' AND lease_expires_at < now
	tagIntent, err := tx.Exec(ctx, `
		UPDATE execution_intents
		SET status = 'TERMINATED',
		    updated_at = $1
		WHERE id = $2 AND lease_epoch = $3 AND status = 'CLAIMED' AND lease_expires_at < $1;
	`, now, item.IntentID, item.LeaseEpoch)
	if err != nil {
		return fmt.Errorf("terminate expired intent %s: %w", item.IntentID, err)
	}
	if tagIntent.RowsAffected() != 1 {
		return fmt.Errorf("expected 1 row affected terminating intent %s, got %d (lease may have been renewed)", item.IntentID, tagIntent.RowsAffected())
	}

	return tx.Commit(ctx)
}

// ConfirmJobRecovery completes job recovery in its own transaction after the old Kubernetes Job is confirmed deleted.
// It transitions the job state to RETRY_WAIT, FAILED, or CANCELLED using guards derived from legalSourcesSQL.
func (s *PostgresStore) ConfirmJobRecovery(ctx context.Context, jobID string, shouldRetry bool, isCancel bool) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin confirm recovery tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()
	targetJobState := domain.StateRetryWait
	if isCancel {
		targetJobState = domain.StateCancelled
	} else if !shouldRetry {
		targetJobState = domain.StateFailed
	}

	guard := legalSourcesSQL(targetJobState)
	jobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = $1,
		    updated_at = $2
		WHERE id = $3 AND state IN (%s);
	`, guard)
	tagJob, err := tx.Exec(ctx, jobQuery, string(targetJobState), now, jobID)
	if err != nil {
		return fmt.Errorf("update job %s to %s: %w", jobID, targetJobState, err)
	}
	if tagJob.RowsAffected() != 1 {
		return fmt.Errorf("expected 1 row affected updating job %s to %s, got %d", jobID, targetJobState, tagJob.RowsAffected())
	}

	return tx.Commit(ctx)
}

// RecoverExpiredJobIntents finds CLAIMED job attempt intents whose lease has expired,
// as well as jobs stuck in RECOVERY_PENDING or incomplete recovery from previous passes,
// runs recovery for each intent in an isolated transaction, confirms deletion with JobDeleter,
// and moves the job to RETRY_WAIT/FAILED/CANCELLED.
func (s *PostgresStore) RecoverExpiredJobIntents(ctx context.Context, optionalDeleter ...JobDeleter) (int, error) {
	var deleter JobDeleter = s.jobDeleter
	if len(optionalDeleter) > 0 {
		deleter = optionalDeleter[0]
	}

	expired, err := s.FindExpiredJobIntents(ctx)
	if err != nil {
		return 0, fmt.Errorf("find expired job intents: %w", err)
	}

	recoveredCount := 0
	for _, item := range expired {
		isCancel := domain.JobState(item.JobState) == domain.StateCancelRequested
		shouldRetry := !isCancel && item.AttemptNumber <= item.MaxRetries

		// Step 1: If recovery has not yet begun on this intent, execute BeginJobRecovery
		// in an isolated transaction (moves RUNNING -> RECOVERY_PENDING, finalizes attempt & terminates intent).
		if item.NeedsBegin {
			if err := s.BeginJobRecovery(ctx, item); err != nil {
				slog.Error("Failed to begin job recovery", "job_id", item.JobID, "intent_id", item.IntentID, "error", err)
				continue
			}
		}

		// Step 2: Delete old Kubernetes Job outside DB transaction and confirm deletion before moving to RETRY_WAIT per ADR-0003
		deleteConfirmed := true
		if deleter != nil && item.DeterministicResourceName != "" {
			if delErr := deleter.DeleteJob(ctx, item.DeterministicResourceName); delErr != nil {
				slog.Error("Failed to delete kubernetes job during recovery", "job_id", item.JobID, "resource_name", item.DeterministicResourceName, "error", delErr)
				deleteConfirmed = false
			}
		}

		// Recovery timeout check per ADR-0003:
		// "RECOVERY_PENDING -> FAILED: Recovery timeout expires without termination confirmation or retry budget is exhausted."
		recoveryTimeoutExpired := time.Since(item.JobUpdatedAt) >= 5*time.Minute
		if !deleteConfirmed {
			if recoveryTimeoutExpired {
				slog.Warn("Recovery timeout expired without delete confirmation; forcing transition to FAILED", "job_id", item.JobID)
				shouldRetry = false
			} else {
				// Retry on next recovery pass
				continue
			}
		}

		// Step 3: Now that deletion is confirmed (or timed out), transition to RETRY_WAIT, FAILED, or CANCELLED
		if err := s.ConfirmJobRecovery(ctx, item.JobID, shouldRetry, isCancel); err != nil {
			slog.Error("Failed to confirm job recovery", "job_id", item.JobID, "error", err)
			continue
		}

		recoveredCount++
	}

	return recoveredCount, nil
}

// ClaimNextJobAttempt atomically claims the next pending job attempt intent using FOR UPDATE SKIP LOCKED.
func (s *PostgresStore) ClaimNextJobAttempt(ctx context.Context, workerID string, leaseDuration time.Duration, workspaceIDs ...string) (*ClaimedJobWorkload, error) {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	var selectQuery string
	var queryArgs []any
	if len(workspaceIDs) > 0 && workspaceIDs[0] != "" {
		selectQuery = `
			SELECT ei.id, ei.job_attempt_id, ja.job_id, ja.workspace_id, w.slug, j.name, j.image_digest,
			       j.command_args, j.env_vars, j.timeout_seconds, j.max_retries, ja.attempt_number,
			       ei.deterministic_resource_name, ei.lease_epoch
			FROM execution_intents ei
			JOIN job_attempts ja ON ja.id = ei.job_attempt_id
			JOIN jobs j ON j.id = ja.job_id
			JOIN workspaces w ON w.id = ja.workspace_id
			WHERE ei.resource_type = 'JOB_ATTEMPT'
			  AND ei.status = 'PENDING'
			  AND ei.workspace_id = $1
			ORDER BY ei.created_at ASC
			LIMIT 1
			FOR UPDATE OF ei SKIP LOCKED;
		`
		queryArgs = append(queryArgs, workspaceIDs[0])
	} else {
		selectQuery = `
			SELECT ei.id, ei.job_attempt_id, ja.job_id, ja.workspace_id, w.slug, j.name, j.image_digest,
			       j.command_args, j.env_vars, j.timeout_seconds, j.max_retries, ja.attempt_number,
			       ei.deterministic_resource_name, ei.lease_epoch
			FROM execution_intents ei
			JOIN job_attempts ja ON ja.id = ei.job_attempt_id
			JOIN jobs j ON j.id = ja.job_id
			JOIN workspaces w ON w.id = ja.workspace_id
			WHERE ei.resource_type = 'JOB_ATTEMPT'
			  AND ei.status = 'PENDING'
			ORDER BY ei.created_at ASC
			LIMIT 1
			FOR UPDATE OF ei SKIP LOCKED;
		`
	}

	var workload ClaimedJobWorkload
	var rawArgs, rawEnv []byte
	err = tx.QueryRow(ctx, selectQuery, queryArgs...).Scan(
		&workload.IntentID,
		&workload.JobAttemptID,
		&workload.JobID,
		&workload.WorkspaceID,
		&workload.WorkspaceSlug,
		&workload.JobName,
		&workload.ImageDigest,
		&rawArgs,
		&rawEnv,
		&workload.TimeoutSeconds,
		&workload.MaxRetries,
		&workload.AttemptNumber,
		&workload.DeterministicResourceName,
		&workload.LeaseEpoch,
	)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil // No pending job intents
		}
		return nil, fmt.Errorf("query pending job attempt intent: %w", err)
	}

	if len(rawArgs) > 0 {
		_ = json.Unmarshal(rawArgs, &workload.CommandArgs)
	}
	if len(rawEnv) > 0 {
		_ = json.Unmarshal(rawEnv, &workload.EnvVars)
	}

	now := time.Now().UTC()
	leaseExpiresAt := now.Add(leaseDuration)
	newEpoch := workload.LeaseEpoch + 1

	updateIntentQuery := `
		UPDATE execution_intents
		SET status = 'CLAIMED',
		    claimed_by = $1,
		    lease_epoch = $2,
		    lease_expires_at = $3,
		    updated_at = $4
		WHERE id = $5;
	`
	if _, err := tx.Exec(ctx, updateIntentQuery, workerID, newEpoch, leaseExpiresAt, now, workload.IntentID); err != nil {
		return nil, fmt.Errorf("update claimed job intent: %w", err)
	}

	updateAttemptQuery := `
		UPDATE job_attempts
		SET state = 'STARTING',
		    lease_epoch = $1,
		    started_at = COALESCE(started_at, $2),
		    updated_at = $2
		WHERE id = $3;
	`
	if _, err := tx.Exec(ctx, updateAttemptQuery, newEpoch, now, workload.JobAttemptID); err != nil {
		return nil, fmt.Errorf("update job attempt to starting: %w", err)
	}

	updateJobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = CASE WHEN state = 'CANCEL_REQUESTED' THEN 'CANCEL_REQUESTED' ELSE 'STARTING' END,
		    updated_at = $1
		WHERE id = $2 AND state IN (%s, 'CANCEL_REQUESTED');
	`, legalSourcesSQL(domain.StateStarting))
	if _, err := tx.Exec(ctx, updateJobQuery, now, workload.JobID); err != nil {
		return nil, fmt.Errorf("update job to starting: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, fmt.Errorf("commit job claim tx: %w", err)
	}

	workload.LeaseEpoch = newEpoch
	return &workload, nil
}

// MarkJobAttemptRunning transitions attempt and job from STARTING to RUNNING, fenced by lease epoch.
func (s *PostgresStore) MarkJobAttemptRunning(ctx context.Context, intentID, attemptID, jobID string, leaseEpoch int) error {
	if leaseEpoch <= 0 {
		return fmt.Errorf("lease epoch must be greater than zero, got %d", leaseEpoch)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()

	// 1. Verify lease epoch fencing on execution_intents
	intentQuery := `
		UPDATE execution_intents
		SET updated_at = $1
		WHERE id = $2 AND lease_epoch = $3 AND status = 'CLAIMED';
	`
	cmd, err := tx.Exec(ctx, intentQuery, now, intentID, leaseEpoch)
	if err != nil {
		return fmt.Errorf("verify job intent claimed: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return fmt.Errorf("fencing error: intent %s epoch %d is stale or no longer claimed", intentID, leaseEpoch)
	}

	// 2. Transition attempt STARTING -> RUNNING
	cmdAttempt, err := tx.Exec(ctx, `
		UPDATE job_attempts
		SET state = 'RUNNING',
		    updated_at = $1
		WHERE id = $2 AND state = 'STARTING';
	`, now, attemptID)
	if err != nil {
		return fmt.Errorf("update job attempt running: %w", err)
	}
	if cmdAttempt.RowsAffected() != 1 {
		return fmt.Errorf("failed to transition attempt %s to RUNNING: expected 1 row affected, got %d", attemptID, cmdAttempt.RowsAffected())
	}

	// 3. Transition job STARTING -> RUNNING guarded by domain legal sources
	updateJobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = 'RUNNING',
		    updated_at = $1
		WHERE id = $2 AND state IN (%s);
	`, legalSourcesSQL(domain.StateRunning))
	cmdJob, err := tx.Exec(ctx, updateJobQuery, now, jobID)
	if err != nil {
		return fmt.Errorf("update job running: %w", err)
	}
	if cmdJob.RowsAffected() != 1 {
		return fmt.Errorf("failed to transition job %s to RUNNING: expected 1 row affected, got %d", jobID, cmdJob.RowsAffected())
	}

	return tx.Commit(ctx)
}

// MarkJobAttemptSucceeded finalizes a successful job attempt and marks logical job SUCCEEDED (or CANCELLED if cancel requested).
func (s *PostgresStore) MarkJobAttemptSucceeded(ctx context.Context, intentID, attemptID, jobID, resourceUID string, leaseEpoch int) error {
	if leaseEpoch <= 0 {
		return fmt.Errorf("lease epoch must be greater than zero, got %d", leaseEpoch)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()

	intentQuery := `
		UPDATE execution_intents
		SET status = 'APPLIED',
		    resource_uid = $1,
		    updated_at = $2
		WHERE id = $3 AND lease_epoch = $4 AND status = 'CLAIMED';
	`
	cmd, err := tx.Exec(ctx, intentQuery, resourceUID, now, intentID, leaseEpoch)
	if err != nil {
		return fmt.Errorf("update job intent applied: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return fmt.Errorf("fencing error: intent %s epoch %d is stale or no longer claimed", intentID, leaseEpoch)
	}

	_, err = tx.Exec(ctx, `
		UPDATE job_attempts
		SET state = 'SUCCEEDED',
		    exit_code = 0,
		    finished_at = $1,
		    updated_at = $1
		WHERE id = $2;
	`, now, attemptID)
	if err != nil {
		return fmt.Errorf("update job attempt succeeded: %w", err)
	}

	updateJobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = CASE WHEN state = 'CANCEL_REQUESTED' THEN 'CANCELLED' ELSE 'SUCCEEDED' END,
		    updated_at = $1
		WHERE id = $2 AND state IN (%s);
	`, legalSourcesSQL(domain.StateSucceeded, domain.StateCancelled))
	cmdJob, err := tx.Exec(ctx, updateJobQuery, now, jobID)
	if err != nil {
		return fmt.Errorf("update job state: %w", err)
	}
	if cmdJob.RowsAffected() != 1 {
		return fmt.Errorf("failed to transition job %s: expected 1 row affected, got %d (job not in active state)", jobID, cmdJob.RowsAffected())
	}

	return tx.Commit(ctx)
}

// MarkJobAttemptFailed records an attempt failure, transitioning to RETRY_WAIT if retries remain, or FAILED if exhausted.
// If the job has entered CANCEL_REQUESTED, it atomically transitions to CANCELLED instead of RETRY_WAIT/FAILED.
func (s *PostgresStore) MarkJobAttemptFailed(ctx context.Context, intentID, attemptID, jobID, reason string, exitCode int, leaseEpoch int, shouldRetry bool) error {
	if leaseEpoch <= 0 {
		return fmt.Errorf("lease epoch must be greater than zero, got %d", leaseEpoch)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()

	intentQuery := `
		UPDATE execution_intents
		SET status = 'TERMINATED',
		    updated_at = $1
		WHERE id = $2 AND lease_epoch = $3 AND status = 'CLAIMED';
	`
	cmd, err := tx.Exec(ctx, intentQuery, now, intentID, leaseEpoch)
	if err != nil {
		return fmt.Errorf("update job intent terminated: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return fmt.Errorf("fencing error: intent %s epoch %d is stale or no longer claimed", intentID, leaseEpoch)
	}

	_, err = tx.Exec(ctx, `
		UPDATE job_attempts
		SET state = 'FAILED',
		    exit_code = $1,
		    failure_reason = $2,
		    finished_at = $3,
		    updated_at = $3
		WHERE id = $4;
	`, exitCode, reason, now, attemptID)
	if err != nil {
		return fmt.Errorf("update job attempt failed: %w", err)
	}

	updateJobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = CASE
		    WHEN state = 'CANCEL_REQUESTED' THEN 'CANCELLED'
		    WHEN $1::boolean THEN 'RETRY_WAIT'
		    ELSE 'FAILED'
		END,
		    updated_at = $2
		WHERE id = $3 AND state IN (%s);
	`, legalSourcesSQL(domain.StateRetryWait, domain.StateFailed, domain.StateCancelled))
	cmdJob, err := tx.Exec(ctx, updateJobQuery, shouldRetry, now, jobID)
	if err != nil {
		return fmt.Errorf("update job state: %w", err)
	}
	if cmdJob.RowsAffected() != 1 {
		return fmt.Errorf("failed to transition job %s: expected 1 row affected, got %d (job not in active state)", jobID, cmdJob.RowsAffected())
	}

	return tx.Commit(ctx)
}

// MarkJobAttemptCancelled records cancellation on the intent, attempt, and job.
func (s *PostgresStore) MarkJobAttemptCancelled(ctx context.Context, intentID, attemptID, jobID string, leaseEpoch int) error {
	if leaseEpoch <= 0 {
		return fmt.Errorf("lease epoch must be greater than zero, got %d", leaseEpoch)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	now := time.Now().UTC()

	intentQuery := `
		UPDATE execution_intents
		SET status = 'TERMINATED',
		    updated_at = $1
		WHERE id = $2 AND lease_epoch = $3 AND status = 'CLAIMED';
	`
	cmd, err := tx.Exec(ctx, intentQuery, now, intentID, leaseEpoch)
	if err != nil {
		return fmt.Errorf("update job intent terminated: %w", err)
	}
	if cmd.RowsAffected() == 0 {
		return fmt.Errorf("fencing error: intent %s epoch %d is stale or no longer claimed", intentID, leaseEpoch)
	}

	_, err = tx.Exec(ctx, `
		UPDATE job_attempts
		SET state = 'CANCELLED',
		    finished_at = $1,
		    updated_at = $1
		WHERE id = $2;
	`, now, attemptID)
	if err != nil {
		return fmt.Errorf("update job attempt cancelled: %w", err)
	}

	updateJobQuery := fmt.Sprintf(`
		UPDATE jobs
		SET state = 'CANCELLED',
		    updated_at = $1
		WHERE id = $2 AND state IN (%s);
	`, legalSourcesSQL(domain.StateCancelled))
	cmdJob, err := tx.Exec(ctx, updateJobQuery, now, jobID)
	if err != nil {
		return fmt.Errorf("update job cancelled: %w", err)
	}
	if cmdJob.RowsAffected() != 1 {
		return fmt.Errorf("failed to transition job %s to CANCELLED: expected 1 row affected, got %d", jobID, cmdJob.RowsAffected())
	}

	return tx.Commit(ctx)
}

// IsJobCancelRequested checks whether a job has transition state CANCEL_REQUESTED.
func (s *PostgresStore) IsJobCancelRequested(ctx context.Context, jobID string) (bool, error) {
	var state string
	err := s.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&state)
	if err != nil {
		return false, err
	}
	return state == "CANCEL_REQUESTED", nil
}
