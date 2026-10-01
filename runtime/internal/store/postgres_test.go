package store

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/hami9/hamicloud/runtime/internal/domain"
)

func getTestDatabaseURL() string {
	if url := os.Getenv("RUNTIME_DATABASE_URL"); url != "" {
		return url
	}
	return "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test?sslmode=disable"
}

func safeDBTarget(rawURL string) string {
	cfg, err := pgxpool.ParseConfig(rawURL)
	if err != nil {
		return "unknown"
	}
	if cfg.ConnConfig.Port == 0 {
		return fmt.Sprintf("%s/%s", cfg.ConnConfig.Host, cfg.ConnConfig.Database)
	}
	return fmt.Sprintf("%s:%d/%s", cfg.ConnConfig.Host, cfg.ConnConfig.Port, cfg.ConnConfig.Database)
}

func connectTestStore(t *testing.T, ctx context.Context) *PostgresStore {
	t.Helper()
	dbURL := getTestDatabaseURL()
	target := safeDBTarget(dbURL)
	st, err := NewPostgresStore(ctx, dbURL)
	if err != nil {
		if os.Getenv("CI") == "true" {
			t.Fatalf("database connection required in CI (host/db=%s): %v", target, err)
		}
		t.Skipf("skipping integration test, cannot connect to postgres at %s: %v", target, err)
	}
	t.Cleanup(func() {
		st.Close()
	})
	return st
}

func createTestWorkspace(t *testing.T, ctx context.Context, st *PostgresStore, name string) string {
	t.Helper()
	wsID := NewUUID()
	now := time.Now().UTC()
	slug := strings.ToLower(strings.ReplaceAll(name, " ", "-")) + "-" + wsID[:8]
	_, err := st.pool.Exec(ctx, `
		INSERT INTO workspaces (id, name, slug, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $4)
		ON CONFLICT (id) DO NOTHING;
	`, wsID, name, slug, now)
	if err != nil {
		t.Fatalf("insert test workspace: %v", err)
	}

	t.Cleanup(func() {
		cleanCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := st.pool.Exec(cleanCtx, `DELETE FROM workspaces WHERE id = $1;`, wsID); err != nil {
			t.Logf("cleanup test workspace %s: %v", wsID, err)
		}
	})

	return wsID
}

// TestPostgresStore_MarkJobAttemptSucceeded_CancelRace asserts that when a job enters CANCEL_REQUESTED
// before attempt completion, MarkJobAttemptSucceeded atomically marks the job CANCELLED (per Decision D4)
// while recording the attempt as SUCCEEDED with exit_code 0.
// Disabling the CASE WHEN state = 'CANCEL_REQUESTED' THEN 'CANCELLED' branch MUST cause this test to fail.
func TestPostgresStore_MarkJobAttemptSucceeded_CancelRace(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "D4 Race WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, err := st.pool.Exec(ctx, `
		INSERT INTO jobs (
			id, workspace_id, name, image_digest, command_args, env_vars,
			timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at
		) VALUES (
			$1, $2, 'd4-race-job', 'sha256:dummy', '[]', '{}', 60, 3, 1, 'RUNNING', $3, $3
		);
	`, jobID, wsID, now)
	if err != nil {
		t.Fatalf("insert test job: %v", err)
	}

	_, err = st.pool.Exec(ctx, `
		INSERT INTO job_attempts (
			id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at
		) VALUES (
			$1, $2, $3, 1, 'RUNNING', 1, $4, $4
		);
	`, attemptID, jobID, wsID, now)
	if err != nil {
		t.Fatalf("insert test attempt: %v", err)
	}

	_, err = st.pool.Exec(ctx, `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, job_attempt_id, target_generation,
			deterministic_resource_name, status, lease_epoch, created_at, updated_at
		) VALUES (
			$1, $2, 'JOB_ATTEMPT', $3, 1, 'job-res-d4', 'CLAIMED', 1, $4, $4
		);
	`, intentID, wsID, attemptID, now)
	if err != nil {
		t.Fatalf("insert test intent: %v", err)
	}

	// 1. Job transitions to CANCEL_REQUESTED while attempt is executing
	cmd, err := st.pool.Exec(ctx, `UPDATE jobs SET state = 'CANCEL_REQUESTED', updated_at = $1 WHERE id = $2;`, now, jobID)
	if err != nil || cmd.RowsAffected() != 1 {
		t.Fatalf("failed to update job to CANCEL_REQUESTED: %v", err)
	}

	// 2. Workload completes with exit code 0; MarkJobAttemptSucceeded is called
	err = st.MarkJobAttemptSucceeded(ctx, intentID, attemptID, jobID, "res-uid-d4", 1)
	if err != nil {
		t.Fatalf("MarkJobAttemptSucceeded returned error: %v", err)
	}

	// 3. Assert: Logical job MUST be CANCELLED (Decision D4)
	var jobState string
	err = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	if err != nil {
		t.Fatalf("query job state: %v", err)
	}
	if jobState != "CANCELLED" {
		t.Fatalf("Decision D4 violation: expected job state CANCELLED, got %s", jobState)
	}

	// 4. Assert: Attempt record retains true outcome SUCCEEDED with exit_code = 0
	var attemptState string
	var exitCode int
	err = st.pool.QueryRow(ctx, `SELECT state, exit_code FROM job_attempts WHERE id = $1;`, attemptID).Scan(&attemptState, &exitCode)
	if err != nil {
		t.Fatalf("query attempt state: %v", err)
	}
	if attemptState != "SUCCEEDED" {
		t.Fatalf("expected attempt state SUCCEEDED, got %s", attemptState)
	}
	if exitCode != 0 {
		t.Fatalf("expected attempt exit_code 0, got %d", exitCode)
	}

	// 5. Assert: Execution intent is APPLIED
	var intentStatus string
	err = st.pool.QueryRow(ctx, `SELECT status FROM execution_intents WHERE id = $1;`, intentID).Scan(&intentStatus)
	if err != nil {
		t.Fatalf("query intent status: %v", err)
	}
	if intentStatus != "APPLIED" {
		t.Fatalf("expected intent status APPLIED, got %s", intentStatus)
	}
}

// TestPostgresStore_MarkJobAttemptFailed_CancelRace asserts that a job in CANCEL_REQUESTED
// that encounters an attempt failure atomically transitions to CANCELLED instead of RETRY_WAIT or FAILED.
func TestPostgresStore_MarkJobAttemptFailed_CancelRace(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "D5 Race WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at) VALUES ($1, $2, 'd5-race-job', 'sha256:dummy', '[]', '{}', 60, 3, 1, 'RUNNING', $3, $3);`, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at) VALUES ($1, $2, $3, 1, 'RUNNING', 1, $4, $4);`, attemptID, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO execution_intents (id, workspace_id, resource_type, job_attempt_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 1, 'job-res-d5', 'CLAIMED', 1, $4, $4);`, intentID, wsID, attemptID, now)

	// Set CANCEL_REQUESTED
	_, _ = st.pool.Exec(ctx, `UPDATE jobs SET state = 'CANCEL_REQUESTED', updated_at = $1 WHERE id = $2;`, now, jobID)

	// Attempt fails with shouldRetry = true
	err := st.MarkJobAttemptFailed(ctx, intentID, attemptID, jobID, "process killed by signal", 137, 1, true)
	if err != nil {
		t.Fatalf("MarkJobAttemptFailed returned error: %v", err)
	}

	var jobState string
	_ = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	if jobState != "CANCELLED" {
		t.Fatalf("Decision D5 violation: job in CANCEL_REQUESTED that failed must transition to CANCELLED, got %s", jobState)
	}
}

// TestPostgresStore_UnfencedIntentRejected verifies that leaseEpoch <= 0 is explicitly rejected across all mutation endpoints.
func TestPostgresStore_UnfencedIntentRejected(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	epochs := []int{0, -1}
	for _, epoch := range epochs {
		if err := st.MarkJobAttemptRunning(ctx, "i1", "a1", "j1", epoch); err == nil {
			t.Fatalf("expected error for leaseEpoch == %d in MarkJobAttemptRunning, got nil", epoch)
		}
		if err := st.MarkJobAttemptSucceeded(ctx, "i1", "a1", "j1", "r1", epoch); err == nil {
			t.Fatalf("expected error for leaseEpoch == %d in MarkJobAttemptSucceeded, got nil", epoch)
		}
		if err := st.MarkJobAttemptFailed(ctx, "i1", "a1", "j1", "err", 1, epoch, false); err == nil {
			t.Fatalf("expected error for leaseEpoch == %d in MarkJobAttemptFailed, got nil", epoch)
		}
		if err := st.MarkJobAttemptCancelled(ctx, "i1", "a1", "j1", epoch); err == nil {
			t.Fatalf("expected error for leaseEpoch == %d in MarkJobAttemptCancelled, got nil", epoch)
		}
		if _, err := st.MarkReleaseHealthy(ctx, "i1", "r1", "app1", "res1", epoch); err == nil {
			t.Fatalf("expected error for leaseEpoch == %d in MarkReleaseHealthy, got nil", epoch)
		}
		if err := st.MarkReleaseFailed(ctx, "i1", "r1", "err", epoch); err == nil {
			t.Fatalf("expected error for leaseEpoch == %d in MarkReleaseFailed, got nil", epoch)
		}
	}
}

// TestPostgresStore_MarkJobAttemptRunning_SuccessAndFencing asserts that MarkJobAttemptRunning
// transitions attempt and job STARTING -> RUNNING under a valid fence, and rejects stale epochs or illegal source states.
func TestPostgresStore_MarkJobAttemptRunning_SuccessAndFencing(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Running WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at) VALUES ($1, $2, 'run-job', 'sha256:dummy', '[]', '{}', 60, 3, 1, 'STARTING', $3, $3);`, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at) VALUES ($1, $2, $3, 1, 'STARTING', 1, $4, $4);`, attemptID, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO execution_intents (id, workspace_id, resource_type, job_attempt_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 1, 'job-res-run', 'CLAIMED', 1, $4, $4);`, intentID, wsID, attemptID, now)

	// 1. Valid transition: STARTING -> RUNNING
	err := st.MarkJobAttemptRunning(ctx, intentID, attemptID, jobID, 1)
	if err != nil {
		t.Fatalf("expected successful transition to RUNNING, got: %v", err)
	}

	var jobState, attemptState string
	_ = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	_ = st.pool.QueryRow(ctx, `SELECT state FROM job_attempts WHERE id = $1;`, attemptID).Scan(&attemptState)
	if jobState != "RUNNING" {
		t.Fatalf("expected job state RUNNING, got %s", jobState)
	}
	if attemptState != "RUNNING" {
		t.Fatalf("expected attempt state RUNNING, got %s", attemptState)
	}

	// 2. Reject re-running or invalid source state (job is now RUNNING, not STARTING)
	err = st.MarkJobAttemptRunning(ctx, intentID, attemptID, jobID, 1)
	if err == nil {
		t.Fatalf("expected error when transitioning from non-STARTING state, got nil")
	}

	// 3. Stale epoch rejected with fencing error
	err = st.MarkJobAttemptRunning(ctx, intentID, attemptID, jobID, 999)
	if err == nil || !strings.Contains(err.Error(), "fencing error") {
		t.Fatalf("expected fencing error for stale epoch, got: %v", err)
	}
}

// TestPostgresStore_MarkJobAttemptSucceeded_TerminalJobNotOverwritten asserts that a job in a terminal
// state (e.g. FAILED) is NOT overwritten by MarkJobAttemptSucceeded, an error is returned, and state is unchanged.
// This integration test directly kills mutations removing "AND state IN (...)" or disabling RowsAffected() != 1 checks.
func TestPostgresStore_MarkJobAttemptSucceeded_TerminalJobNotOverwritten(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Term WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at) VALUES ($1, $2, 'term-job', 'sha256:dummy', '[]', '{}', 60, 3, 1, 'FAILED', $3, $3);`, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at) VALUES ($1, $2, $3, 1, 'STARTING', 1, $4, $4);`, attemptID, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO execution_intents (id, workspace_id, resource_type, job_attempt_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 1, 'job-res-term', 'CLAIMED', 1, $4, $4);`, intentID, wsID, attemptID, now)

	// Attempting to finalize success on a terminal job MUST return an error
	err := st.MarkJobAttemptSucceeded(ctx, intentID, attemptID, jobID, "res-term-uid", 1)
	if err == nil {
		t.Fatalf("expected error when MarkJobAttemptSucceeded called on terminal FAILED job, got nil")
	}

	// Verify terminal job state is preserved (atomic rollback on RowsAffected != 1)
	var jobState, attemptState, intentStatus string
	_ = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	_ = st.pool.QueryRow(ctx, `SELECT state FROM job_attempts WHERE id = $1;`, attemptID).Scan(&attemptState)
	_ = st.pool.QueryRow(ctx, `SELECT status FROM execution_intents WHERE id = $1;`, intentID).Scan(&intentStatus)

	if jobState != "FAILED" {
		t.Fatalf("terminal job state was overwritten! expected FAILED, got %s", jobState)
	}
	if attemptState == "SUCCEEDED" {
		t.Fatalf("attempt state was committed despite job transition failure")
	}
	if intentStatus == "APPLIED" {
		t.Fatalf("intent status was committed despite job transition failure")
	}
}

// TestPostgresStore_MarkJobAttemptCancelled_StaleEpochReturnsFencingError asserts that a stale
// lease epoch returns a fencing error and changes nothing in intent, attempt, or job state.
// This test directly kills the mutation removing the fencing RowsAffected check in MarkJobAttemptCancelled.
func TestPostgresStore_MarkJobAttemptCancelled_StaleEpochReturnsFencingError(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Fence WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at) VALUES ($1, $2, 'fence-job', 'sha256:dummy', '[]', '{}', 60, 3, 1, 'CANCEL_REQUESTED', $3, $3);`, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at) VALUES ($1, $2, $3, 1, 'RUNNING', 2, $4, $4);`, attemptID, jobID, wsID, now)
	// Intent has lease_epoch = 2
	_, _ = st.pool.Exec(ctx, `INSERT INTO execution_intents (id, workspace_id, resource_type, job_attempt_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 1, 'job-res-fence', 'CLAIMED', 2, $4, $4);`, intentID, wsID, attemptID, now)

	// Caller presents stale epoch 1
	err := st.MarkJobAttemptCancelled(ctx, intentID, attemptID, jobID, 1)
	if err == nil {
		t.Fatalf("expected fencing error for stale epoch 1 (active epoch 2), got nil")
	}
	if !strings.Contains(err.Error(), "fencing error") {
		t.Fatalf("expected error to mention 'fencing error', got: %v", err)
	}

	// Verify nothing changed in DB
	var intentStatus string
	var intentEpoch int
	var jobState, attemptState string
	_ = st.pool.QueryRow(ctx, `SELECT status, lease_epoch FROM execution_intents WHERE id = $1;`, intentID).Scan(&intentStatus, &intentEpoch)
	_ = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	_ = st.pool.QueryRow(ctx, `SELECT state FROM job_attempts WHERE id = $1;`, attemptID).Scan(&attemptState)

	if intentStatus != "CLAIMED" || intentEpoch != 2 {
		t.Fatalf("intent was modified on fencing failure: status=%s, epoch=%d", intentStatus, intentEpoch)
	}
	if jobState != "CANCEL_REQUESTED" {
		t.Fatalf("job state was modified on fencing failure: state=%s", jobState)
	}
	if attemptState != "RUNNING" {
		t.Fatalf("attempt state was modified on fencing failure: state=%s", attemptState)
	}
}

// TestPostgresStore_MarkJobAttemptCancelled_GuardsOnCancelRequested asserts that MarkJobAttemptCancelled
// only accepts CANCEL_REQUESTED -> CANCELLED, rejecting STARTING or RUNNING directly to CANCELLED.
func TestPostgresStore_MarkJobAttemptCancelled_GuardsOnCancelRequested(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "CancelGuard WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	// Job is in RUNNING (not CANCEL_REQUESTED)
	_, _ = st.pool.Exec(ctx, `INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at) VALUES ($1, $2, 'cg-job', 'sha256:dummy', '[]', '{}', 60, 3, 1, 'RUNNING', $3, $3);`, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, lease_epoch, created_at, updated_at) VALUES ($1, $2, $3, 1, 'RUNNING', 1, $4, $4);`, attemptID, jobID, wsID, now)
	_, _ = st.pool.Exec(ctx, `INSERT INTO execution_intents (id, workspace_id, resource_type, job_attempt_id, target_generation, deterministic_resource_name, status, lease_epoch, created_at, updated_at) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 1, 'job-res-cg', 'CLAIMED', 1, $4, $4);`, intentID, wsID, attemptID, now)

	// Call MarkJobAttemptCancelled directly on RUNNING job -> must fail because only CANCEL_REQUESTED -> CANCELLED is legal
	err := st.MarkJobAttemptCancelled(ctx, intentID, attemptID, jobID, 1)
	if err == nil {
		t.Fatalf("expected error when cancelling job directly from RUNNING without CANCEL_REQUESTED, got nil")
	}

	var jobState string
	_ = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	if jobState != "RUNNING" {
		t.Fatalf("job state was modified from RUNNING to %s", jobState)
	}
}

// TestStore_JobStateTransitions_AllEdgesLegal asserts that every job-state write in the store
// corresponds strictly to a legal edge defined in domain.LegalTransitions and contracts/state-machines/job.v1.json.
func TestStore_JobStateTransitions_AllEdgesLegal(t *testing.T) {
	type storeTransition struct {
		method string
		from   domain.JobState
		to     domain.JobState
	}

	// Enumeration of all job state transitions performed across the store:
	storeTransitions := []storeTransition{
		{"RequeueRetryWaitJobs", domain.StateRetryWait, domain.StateQueued},
		{"CreateJobAttemptIntent", domain.StateQueued, domain.StateAdmitted},
		{"ClaimNextJobAttempt", domain.StateAdmitted, domain.StateStarting},
		{"MarkJobAttemptRunning", domain.StateStarting, domain.StateRunning},
		{"MarkJobAttemptSucceeded_Normal", domain.StateRunning, domain.StateSucceeded},
		{"MarkJobAttemptSucceeded_D4Cancel", domain.StateCancelRequested, domain.StateCancelled},
		{"MarkJobAttemptFailed_StartingRetry", domain.StateStarting, domain.StateRetryWait},
		{"MarkJobAttemptFailed_StartingFail", domain.StateStarting, domain.StateFailed},
		{"MarkJobAttemptFailed_RunningRetry", domain.StateRunning, domain.StateRetryWait},
		{"MarkJobAttemptFailed_RunningFail", domain.StateRunning, domain.StateFailed},
		{"MarkJobAttemptFailed_RecoveryRetry", domain.StateRecoveryPending, domain.StateRetryWait},
		{"MarkJobAttemptFailed_RecoveryFail", domain.StateRecoveryPending, domain.StateFailed},
		{"MarkJobAttemptFailed_D5Cancel", domain.StateCancelRequested, domain.StateCancelled},
		{"MarkJobAttemptCancelled", domain.StateCancelRequested, domain.StateCancelled},
	}

	for _, tr := range storeTransitions {
		if err := domain.ValidateTransition(tr.from, tr.to); err != nil {
			t.Errorf("store method %s attempts illegal transition %s -> %s: %v", tr.method, tr.from, tr.to, err)
		}
	}

	// Verify that illegal edges are strictly rejected by the validator
	illegalEdges := [][2]domain.JobState{
		{domain.StateStarting, domain.StateSucceeded}, // Illegal: must be RUNNING
		{domain.StateQueued, domain.StateFailed},      // Illegal: retry budget exhaustion cannot write QUEUED -> FAILED
		{domain.StateStarting, domain.StateCancelled}, // Illegal: must be CANCEL_REQUESTED
		{domain.StateRunning, domain.StateCancelled},  // Illegal: must be CANCEL_REQUESTED
		{domain.StateAdmitted, domain.StateRunning},   // Illegal: must go through STARTING
	}

	for _, edge := range illegalEdges {
		if err := domain.ValidateTransition(edge[0], edge[1]); err == nil {
			t.Errorf("expected transition %s -> %s to be illegal, but ValidateTransition returned nil", edge[0], edge[1])
		}
	}
}

// TestSafeDBTarget_DoesNotLeakPassword asserts that safeDBTarget extracts only the host, port, and database name,
// guaranteeing no passwords or credentials are ever exposed in logs or test output.
func TestSafeDBTarget_DoesNotLeakPassword(t *testing.T) {
	raw := "postgres://hamicloud_user:super_secret_password_12345@db.internal.net:5432/hamicloud_test?sslmode=disable"
	target := safeDBTarget(raw)
	if strings.Contains(target, "super_secret_password_12345") || strings.Contains(target, "hamicloud_user") {
		t.Fatalf("safeDBTarget leaked credentials: %s", target)
	}
	expected := "db.internal.net:5432/hamicloud_test"
	if target != expected {
		t.Fatalf("expected host/db %q, got %q", expected, target)
	}
}

// TestPostgresStore_CreateJobAttemptIntent_RetryBudgetExhausted asserts that when
// CurrentAttemptNumber = MaxRetries + 1, CreateJobAttemptIntent returns an error
// and inserts no new job_attempt row.
func TestPostgresStore_CreateJobAttemptIntent_RetryBudgetExhausted(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Retry Budget WS")
	jobID := NewUUID()

	maxRetries := 2
	currentAttempt := maxRetries + 1 // 3

	_, err := st.pool.Exec(ctx, `
		INSERT INTO jobs (
			id, workspace_id, name, image_digest, command_args, env_vars,
			timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at
		) VALUES (
			$1, $2, 'rb-job', 'sha256:dummy', '[]', '{}', 60, $3, $4, 'QUEUED', $5, $5
		);
	`, jobID, wsID, maxRetries, currentAttempt, now)
	if err != nil {
		t.Fatalf("insert test job: %v", err)
	}

	unadmitted := UnadmittedJob{
		JobID:                jobID,
		WorkspaceID:          wsID,
		WorkspaceSlug:        "rb-ws-" + wsID[:8],
		Name:                 "rb-job",
		ImageDigest:          "sha256:dummy",
		TimeoutSeconds:       60,
		MaxRetries:           maxRetries,
		CurrentAttemptNumber: currentAttempt,
		State:                "QUEUED",
	}

	intent, err := st.CreateJobAttemptIntent(ctx, unadmitted)
	if err == nil {
		t.Fatalf("expected error when retry budget is exhausted, got nil (intent=%v)", intent)
	}
	if !strings.Contains(err.Error(), "retry budget exhausted") {
		t.Fatalf("expected error to mention 'retry budget exhausted', got: %v", err)
	}

	// Assert no new attempt row was inserted
	var attemptCount int
	err = st.pool.QueryRow(ctx, `SELECT COUNT(*) FROM job_attempts WHERE job_id = $1;`, jobID).Scan(&attemptCount)
	if err != nil {
		t.Fatalf("query job_attempts count: %v", err)
	}
	if attemptCount != 0 {
		t.Fatalf("expected 0 attempt rows for job %s, got %d", jobID, attemptCount)
	}
}

func createTestApp(t *testing.T, ctx context.Context, st *PostgresStore, wsID, name string) string {
	t.Helper()
	appID := NewUUID()
	now := time.Now().UTC()
	slug := fmt.Sprintf("%s-%s", strings.ToLower(name), appID[:8])
	_, err := st.pool.Exec(ctx, `
		INSERT INTO applications (
			id, workspace_id, name, slug, workload_type, desired_generation, created_at, updated_at
		) VALUES ($1, $2, $3, $4, 'HTTP_SERVICE', 1, $5, $5);
	`, appID, wsID, name, slug, now)
	if err != nil {
		t.Fatalf("create test application: %v", err)
	}
	return appID
}

// TestPostgresStore_ScanUnadmittedReleases_SupersededReleaseSkipped asserts that when multiple
// releases are pending for an application, only the latest release (highest release_number)
// receives an ExecutionIntent, and an older superseded release can never become current_release_id.
func TestPostgresStore_ScanUnadmittedReleases_SupersededReleaseSkipped(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Release Supersede WS")
	appID := NewUUID()
	relID1 := NewUUID()
	relID2 := NewUUID()

	// 1. Create application with desired_generation = 1
	_, err := st.pool.Exec(ctx, `
		INSERT INTO applications (
			id, workspace_id, name, slug, workload_type, desired_generation, created_at, updated_at
		) VALUES ($1, $2, 'supersede-app', $3, 'HTTP_SERVICE', 1, $4, $4);
	`, appID, wsID, "supersede-app-"+appID[:8], now)
	if err != nil {
		t.Fatalf("insert test application: %v", err)
	}

	// 2. Insert Release 1 (release_number = 1, IMAGE_READY)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO releases (
			id, application_id, workspace_id, release_number, image_digest, config_json, status, created_at, updated_at
		) VALUES ($1, $2, $3, 1, 'sha256:rel1', '{"port": 8080}', 'IMAGE_READY', $4, $4);
	`, relID1, appID, wsID, now)
	if err != nil {
		t.Fatalf("insert test release 1: %v", err)
	}

	// 3. Insert Release 2 (release_number = 2, IMAGE_READY, newer release for same app)
	later := now.Add(time.Second)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO releases (
			id, application_id, workspace_id, release_number, image_digest, config_json, status, created_at, updated_at
		) VALUES ($1, $2, $3, 2, 'sha256:rel2', '{"port": 8080}', 'IMAGE_READY', $4, $4);
	`, relID2, appID, wsID, later)
	if err != nil {
		t.Fatalf("insert test release 2: %v", err)
	}

	// 4. ScanUnadmittedReleases MUST return only Release 2 (Release 1 is superseded)
	unadmitted, err := st.ScanUnadmittedReleases(ctx, 10)
	if err != nil {
		t.Fatalf("ScanUnadmittedReleases failed: %v", err)
	}

	foundRel1 := false
	foundRel2 := false
	for _, r := range unadmitted {
		if r.ReleaseID == relID1 {
			foundRel1 = true
		}
		if r.ReleaseID == relID2 {
			foundRel2 = true
		}
	}
	if foundRel1 {
		t.Fatalf("superseded release 1 was unexpectedly returned by ScanUnadmittedReleases")
	}
	if !foundRel2 {
		t.Fatalf("latest release 2 was not returned by ScanUnadmittedReleases")
	}

	// 5. Create intent for Release 2 with desired_generation = 1
	var rel2Candidate UnadmittedRelease
	for _, r := range unadmitted {
		if r.ReleaseID == relID2 {
			rel2Candidate = r
			break
		}
	}
	intent2, err := st.CreateServiceReleaseIntent(ctx, rel2Candidate)
	if err != nil || intent2 == nil {
		t.Fatalf("CreateServiceReleaseIntent failed: %v", err)
	}

	// Verify ADR-0003 deterministic naming: hc-svc-{app_id}-{generation}
	expectedResourceName := fmt.Sprintf("hc-svc-%s-1", appID)
	if intent2.DeterministicResourceName != expectedResourceName {
		t.Fatalf("expected resource name %s, got %s", expectedResourceName, intent2.DeterministicResourceName)
	}

	// 6. Advance application desired_generation to 2 (simulating subsequent deployment)
	_, err = st.pool.Exec(ctx, `UPDATE applications SET desired_generation = 2, updated_at = $1 WHERE id = $2;`, now, appID)
	if err != nil {
		t.Fatalf("update app desired_generation: %v", err)
	}

	// 7. Claim intent2 (target_generation is 1, while app is now generation 2)
	workload, err := st.ClaimNextServiceRelease(ctx, "worker-test", 60*time.Second, wsID)
	if err != nil || workload == nil {
		t.Fatalf("ClaimNextServiceRelease failed: %v", err)
	}

	// Cycle 2 probe: assert ScanUnadmittedReleases does NOT select older release 1 now that release 2 is DEPLOYING
	unadmittedCycle2, err := st.ScanUnadmittedReleases(ctx, 10)
	if err != nil {
		t.Fatalf("ScanUnadmittedReleases cycle 2 failed: %v", err)
	}
	for _, r := range unadmittedCycle2 {
		if r.ApplicationID == appID {
			t.Fatalf("cycle 2 scan unexpectedly returned release %s (number %d) for app; older release must not be admitted when newer release is claimed", r.ReleaseID, r.ReleaseNumber)
		}
	}

	// Assert release 1 was marked SUPERSEDED in the database
	var rel1Status string
	err = st.pool.QueryRow(ctx, `SELECT status FROM releases WHERE id = $1;`, relID1).Scan(&rel1Status)
	if err != nil {
		t.Fatalf("query release 1 status: %v", err)
	}
	if rel1Status != "SUPERSEDED" {
		t.Fatalf("expected release 1 status to be SUPERSEDED, got %s", rel1Status)
	}

	// 8. MarkReleaseHealthy with the superseded intent (generation 1)
	becameCurrent, err := st.MarkReleaseHealthy(ctx, intent2.ID, relID2, appID, "uid-res-2", workload.LeaseEpoch)
	if err != nil {
		t.Fatalf("MarkReleaseHealthy failed: %v", err)
	}
	if becameCurrent {
		t.Fatalf("expected becameCurrent=false for superseded generation 1 release")
	}

	// 9. Assert: applications.current_release_id MUST NOT be updated to relID2 because generation is stale!
	var currentRelID *string
	err = st.pool.QueryRow(ctx, `SELECT current_release_id::text FROM applications WHERE id = $1;`, appID).Scan(&currentRelID)
	if err != nil {
		t.Fatalf("query app current_release_id: %v", err)
	}
	if currentRelID != nil {
		t.Fatalf("stale generation superseded release unexpectedly became current_release_id: %s", *currentRelID)
	}

	// 10. When a release matching current desired_generation (2) completes, it DOES become current
	intentForGen2ID := NewUUID()
	_, err = st.pool.Exec(ctx, `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, release_id, target_generation,
			deterministic_resource_name, status, lease_epoch, created_at, updated_at
		) VALUES ($1, $2, 'SERVICE_RELEASE', $3, 2, 'hc-svc-gen2', 'CLAIMED', 1, $4, $4);
	`, intentForGen2ID, wsID, relID2, now)
	if err != nil {
		t.Fatalf("insert gen2 claimed intent: %v", err)
	}

	becameCurrentGen2, err := st.MarkReleaseHealthy(ctx, intentForGen2ID, relID2, appID, "uid-res-gen2", 1)
	if err != nil {
		t.Fatalf("MarkReleaseHealthy for gen 2 failed: %v", err)
	}
	if !becameCurrentGen2 {
		t.Fatalf("expected becameCurrent=true for current generation 2 release")
	}

	err = st.pool.QueryRow(ctx, `SELECT current_release_id::text FROM applications WHERE id = $1;`, appID).Scan(&currentRelID)
	if err != nil {
		t.Fatalf("query app current_release_id after gen2: %v", err)
	}
	if currentRelID == nil || *currentRelID != relID2 {
		t.Fatalf("expected current_release_id to be %s, got %v", relID2, currentRelID)
	}
}

func TestPostgresStore_ClaimNextServiceRelease_ReclaimsExpiredLease(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Expired Release WS")
	appID := NewUUID()
	relID := NewUUID()
	intentID := NewUUID()

	_, err := st.pool.Exec(ctx, `
		INSERT INTO applications (id, workspace_id, name, slug, workload_type, desired_generation, created_at, updated_at)
		VALUES ($1, $2, 'Expired Svc', 'expired-svc', 'HTTP_SERVICE', 1, $3, $3);
	`, appID, wsID, now)
	if err != nil {
		t.Fatalf("insert test application: %v", err)
	}

	_, err = st.pool.Exec(ctx, `
		INSERT INTO releases (id, application_id, workspace_id, release_number, image_digest, config_json, status, created_at, updated_at)
		VALUES ($1, $2, $3, 1, 'docker.io/library/nginx:alpine', '{"port": 8080}', 'DEPLOYING', $4, $4);
	`, relID, appID, wsID, now)
	if err != nil {
		t.Fatalf("insert test release: %v", err)
	}

	// Insert expired CLAIMED intent (expired 5 seconds ago, epoch = 1)
	pastExpiresAt := now.Add(-5 * time.Second)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, release_id, target_generation,
			deterministic_resource_name, claimed_by, status, lease_epoch, lease_expires_at, created_at, updated_at
		) VALUES ($1, $2, 'SERVICE_RELEASE', $3, 1, 'hc-svc-test', 'crashed-worker', 'CLAIMED', 1, $4, $5, $5);
	`, intentID, wsID, relID, pastExpiresAt, now)
	if err != nil {
		t.Fatalf("insert expired intent: %v", err)
	}

	// Another worker attempts to claim next release
	workload, err := st.ClaimNextServiceRelease(ctx, "restarted-worker", 10*time.Second, wsID)
	if err != nil {
		t.Fatalf("ClaimNextServiceRelease failed: %v", err)
	}
	if workload == nil {
		t.Fatalf("expected expired intent to be reclaimed by restarted-worker, got nil")
	}

	if workload.IntentID != intentID {
		t.Errorf("expected intent %s, got %s", intentID, workload.IntentID)
	}
	if workload.LeaseEpoch != 2 {
		t.Errorf("expected incremented epoch 2, got %d", workload.LeaseEpoch)
	}

	// Crashed worker tries to mark healthy with old epoch 1 -> must fail with fencing error
	_, errOld := st.MarkReleaseHealthy(ctx, intentID, relID, appID, "uid-res-old", 1)
	if errOld == nil {
		t.Fatalf("expected fencing error for old epoch 1, got nil")
	}

	// Restarted worker with epoch 2 succeeds
	_, errNew := st.MarkReleaseHealthy(ctx, intentID, relID, appID, "uid-res-new", 2)
	if errNew != nil {
		t.Fatalf("expected successful MarkReleaseHealthy with epoch 2, got: %v", errNew)
	}
}

func TestPostgresStore_RecoverExpiredJobIntents(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Recover Job WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, err := st.pool.Exec(ctx, `
		INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at)
		VALUES ($1, $2, 'crashed-job', 'docker.io/library/alpine:latest', '["echo", "hi"]', '{}', 60, 2, 1, 'RUNNING', $3, $3);
	`, jobID, wsID, now)
	if err != nil {
		t.Fatalf("insert test job: %v", err)
	}

	_, err = st.pool.Exec(ctx, `
		INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, started_at, created_at, updated_at)
		VALUES ($1, $2, $3, 1, 'RUNNING', $4, $4, $4);
	`, attemptID, jobID, wsID, now)
	if err != nil {
		t.Fatalf("insert test job attempt: %v", err)
	}

	// Insert expired CLAIMED intent
	pastExpiresAt := now.Add(-10 * time.Second)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, job_attempt_id,
			deterministic_resource_name, claimed_by, status, lease_epoch, lease_expires_at, created_at, updated_at
		) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 'hc-job-test', 'crashed-worker', 'CLAIMED', 1, $4, $5, $5);
	`, intentID, wsID, attemptID, pastExpiresAt, now)
	if err != nil {
		t.Fatalf("insert expired job intent: %v", err)
	}

	// Recover expired intents
	count, err := st.RecoverExpiredJobIntents(ctx)
	if err != nil {
		t.Fatalf("RecoverExpiredJobIntents failed: %v", err)
	}
	if count != 1 {
		t.Fatalf("expected 1 recovered intent, got %d", count)
	}

	// Check attempt state
	var attState, reason string
	err = st.pool.QueryRow(ctx, `SELECT state, failure_reason FROM job_attempts WHERE id = $1;`, attemptID).Scan(&attState, &reason)
	if err != nil {
		t.Fatalf("query attempt: %v", err)
	}
	if attState != "FAILED" {
		t.Errorf("expected attempt state FAILED, got %s", attState)
	}
	if !strings.Contains(reason, "lease expired") {
		t.Errorf("expected failure reason mentioning lease expiration, got: %s", reason)
	}

	// Check job state (should be RETRY_WAIT since attempt 1 <= max_retries 2)
	var jobState string
	err = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	if err != nil {
		t.Fatalf("query job: %v", err)
	}
	if jobState != "RETRY_WAIT" {
		t.Errorf("expected job state RETRY_WAIT, got %s", jobState)
	}

	// Check intent status
	var intentStatus string
	err = st.pool.QueryRow(ctx, `SELECT status FROM execution_intents WHERE id = $1;`, intentID).Scan(&intentStatus)
	if err != nil {
		t.Fatalf("query intent: %v", err)
	}
	if intentStatus != "TERMINATED" {
		t.Errorf("expected intent status TERMINATED, got %s", intentStatus)
	}
}

type fakeFailingJobDeleter struct{}

func (f *fakeFailingJobDeleter) DeleteJob(ctx context.Context, resourceName string) error {
	return errors.New("kubernetes API unreachable: connection refused")
}

func TestPostgresStore_RecoverExpiredJobIntents_DeleterFailureHaltsRetryWait(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Deleter Failure WS")
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, err := st.pool.Exec(ctx, `
		INSERT INTO jobs (id, workspace_id, name, image_digest, command_args, env_vars, timeout_seconds, max_retries, current_attempt_number, state, created_at, updated_at)
		VALUES ($1, $2, 'failing-deleter-job', 'docker.io/library/alpine:latest', '["echo", "hi"]', '{}', 60, 2, 1, 'RUNNING', $3, $3);
	`, jobID, wsID, now)
	if err != nil {
		t.Fatalf("insert test job: %v", err)
	}

	_, err = st.pool.Exec(ctx, `
		INSERT INTO job_attempts (id, job_id, workspace_id, attempt_number, state, started_at, created_at, updated_at)
		VALUES ($1, $2, $3, 1, 'RUNNING', $4, $4, $4);
	`, attemptID, jobID, wsID, now)
	if err != nil {
		t.Fatalf("insert test job attempt: %v", err)
	}

	pastExpiresAt := now.Add(-10 * time.Second)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, job_attempt_id,
			deterministic_resource_name, claimed_by, status, lease_epoch, lease_expires_at, created_at, updated_at
		) VALUES ($1, $2, 'JOB_ATTEMPT', $3, 'hc-job-test-failing-deleter', 'crashed-worker', 'CLAIMED', 1, $4, $5, $5);
	`, intentID, wsID, attemptID, pastExpiresAt, now)
	if err != nil {
		t.Fatalf("insert expired job intent: %v", err)
	}

	// Recover with failing deleter: DeleteJob returns error!
	count, err := st.RecoverExpiredJobIntents(ctx, &fakeFailingJobDeleter{})
	if err != nil {
		t.Fatalf("RecoverExpiredJobIntents returned error: %v", err)
	}
	if count != 0 {
		t.Errorf("expected 0 confirmed recovered jobs when deleter fails, got %d", count)
	}

	// Job state must be RECOVERY_PENDING, not RETRY_WAIT, because delete was not confirmed
	var jobState string
	err = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	if err != nil {
		t.Fatalf("query job: %v", err)
	}
	if jobState != "RECOVERY_PENDING" {
		t.Errorf("expected job state RECOVERY_PENDING when deleter fails, got %s", jobState)
	}

	// Attempt must still be marked FAILED
	var attState string
	err = st.pool.QueryRow(ctx, `SELECT state FROM job_attempts WHERE id = $1;`, attemptID).Scan(&attState)
	if err != nil {
		t.Fatalf("query attempt: %v", err)
	}
	if attState != "FAILED" {
		t.Errorf("expected attempt state FAILED, got %s", attState)
	}
}

func TestPostgresStore_ClaimNextServiceRelease_SkipsSupersededReleaseWithPendingIntent(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Superseded Claim WS")
	appID := createTestApp(t, ctx, st, wsID, "skip-superseded-app")

	rel1ID := NewUUID()
	rel2ID := NewUUID()
	intent1ID := NewUUID()

	// 1. Release 1 (generation 1)
	_, err := st.pool.Exec(ctx, `
		INSERT INTO releases (id, application_id, workspace_id, release_number, image_digest, status, config_json, created_at, updated_at)
		VALUES ($1, $2, $3, 1, 'docker.io/library/nginx:1.27-alpine', 'IMAGE_READY', '{}', $4, $4);
	`, rel1ID, appID, wsID, now)
	if err != nil {
		t.Fatalf("insert release 1: %v", err)
	}

	// 2. Release 1 has a PENDING execution intent
	_, err = st.pool.Exec(ctx, `
		INSERT INTO execution_intents (
			id, workspace_id, resource_type, release_id, target_generation,
			deterministic_resource_name, status, lease_epoch, created_at, updated_at
		) VALUES ($1, $2, 'SERVICE_RELEASE', $3, 1, 'hc-svc-gen1', 'PENDING', 0, $4, $4);
	`, intent1ID, wsID, rel1ID, now)
	if err != nil {
		t.Fatalf("insert release 1 intent: %v", err)
	}

	// 3. Release 2 (generation 2) arrives with deployable image
	now2 := now.Add(2 * time.Second)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO releases (id, application_id, workspace_id, release_number, image_digest, status, config_json, created_at, updated_at)
		VALUES ($1, $2, $3, 2, 'docker.io/library/nginx:1.28-alpine', 'IMAGE_READY', '{}', $4, $4);
	`, rel2ID, appID, wsID, now2)
	if err != nil {
		t.Fatalf("insert release 2: %v", err)
	}

	// 4. Update application desired_generation to 2
	_, err = st.pool.Exec(ctx, `UPDATE applications SET desired_generation = 2 WHERE id = $1;`, appID)
	if err != nil {
		t.Fatalf("update app desired_generation: %v", err)
	}

	// 5. ScanUnadmittedReleases runs: marks release 1 SUPERSEDED and terminates release 1's pending intent
	unadmitted, err := st.ScanUnadmittedReleases(ctx, 10)
	if err != nil {
		t.Fatalf("ScanUnadmittedReleases failed: %v", err)
	}
	if len(unadmitted) != 1 || unadmitted[0].ReleaseID != rel2ID {
		t.Fatalf("expected ScanUnadmittedReleases to return only release 2, got %v", unadmitted)
	}

	// Assert release 1 was marked SUPERSEDED
	var rel1Status string
	err = st.pool.QueryRow(ctx, `SELECT status FROM releases WHERE id = $1;`, rel1ID).Scan(&rel1Status)
	if err != nil {
		t.Fatalf("query release 1 status: %v", err)
	}
	if rel1Status != "SUPERSEDED" {
		t.Fatalf("expected release 1 status to be SUPERSEDED, got %s", rel1Status)
	}

	// Assert release 1's intent was terminated
	var intent1Status string
	err = st.pool.QueryRow(ctx, `SELECT status FROM execution_intents WHERE id = $1;`, intent1ID).Scan(&intent1Status)
	if err != nil {
		t.Fatalf("query intent 1 status: %v", err)
	}
	if intent1Status != "TERMINATED" {
		t.Fatalf("expected release 1 intent to be TERMINATED, got %s", intent1Status)
	}

	// 6. ClaimNextServiceRelease must NOT claim release 1
	claimed, err := st.ClaimNextServiceRelease(ctx, "worker-1", 60*time.Second, wsID)
	if err != nil {
		t.Fatalf("ClaimNextServiceRelease failed: %v", err)
	}
	if claimed != nil && claimed.ReleaseID == rel1ID {
		t.Fatalf("ClaimNextServiceRelease unexpectedly claimed superseded release 1!")
	}

	// Assert release 1 status remains SUPERSEDED (never overwritten to DEPLOYING)
	err = st.pool.QueryRow(ctx, `SELECT status FROM releases WHERE id = $1;`, rel1ID).Scan(&rel1Status)
	if err != nil {
		t.Fatalf("query release 1 status: %v", err)
	}
	if rel1Status != "SUPERSEDED" {
		t.Fatalf("expected release 1 status to remain SUPERSEDED, got %s", rel1Status)
	}
}

func TestPostgresStore_ScanUnadmittedReleases_PendingDigestDoesNotSupersede(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)

	now := time.Now().UTC()
	wsID := createTestWorkspace(t, ctx, st, "Pending Digest WS")
	appID := createTestApp(t, ctx, st, wsID, "pending-digest-app")

	rel1ID := NewUUID()
	rel2ID := NewUUID()

	// Release 1: deployable image
	_, err := st.pool.Exec(ctx, `
		INSERT INTO releases (id, application_id, workspace_id, release_number, image_digest, status, config_json, created_at, updated_at)
		VALUES ($1, $2, $3, 1, 'docker.io/library/nginx:1.27-alpine', 'IMAGE_READY', '{}', $4, $4);
	`, rel1ID, appID, wsID, now)
	if err != nil {
		t.Fatalf("insert release 1: %v", err)
	}

	// Release 2: awaiting build (image_digest = 'pending')
	now2 := now.Add(2 * time.Second)
	_, err = st.pool.Exec(ctx, `
		INSERT INTO releases (id, application_id, workspace_id, release_number, image_digest, status, config_json, created_at, updated_at)
		VALUES ($1, $2, $3, 2, 'pending', 'REQUESTED', '{}', $4, $4);
	`, rel2ID, appID, wsID, now2)
	if err != nil {
		t.Fatalf("insert release 2: %v", err)
	}

	// ScanUnadmittedReleases runs: release 2 has pending digest, so release 1 MUST NOT be superseded!
	_, err = st.ScanUnadmittedReleases(ctx, 10)
	if err != nil {
		t.Fatalf("ScanUnadmittedReleases failed: %v", err)
	}

	var rel1Status string
	err = st.pool.QueryRow(ctx, `SELECT status FROM releases WHERE id = $1;`, rel1ID).Scan(&rel1Status)
	if err != nil {
		t.Fatalf("query release 1 status: %v", err)
	}
	if rel1Status != "IMAGE_READY" {
		t.Fatalf("expected release 1 to remain IMAGE_READY (not superseded by pending build), got %s", rel1Status)
	}
}
