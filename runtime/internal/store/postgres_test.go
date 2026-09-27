package store

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/domain"
)

func getTestDatabaseURL() string {
	if url := os.Getenv("RUNTIME_DATABASE_URL"); url != "" {
		return url
	}
	return "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test?sslmode=disable"
}

func connectTestStore(t *testing.T, ctx context.Context) *PostgresStore {
	t.Helper()
	dbURL := getTestDatabaseURL()
	st, err := NewPostgresStore(ctx, dbURL)
	if err != nil {
		if os.Getenv("CI") == "true" {
			t.Fatalf("database connection required in CI (RUNTIME_DATABASE_URL=%s): %v", dbURL, err)
		}
		t.Skipf("skipping integration test, cannot connect to postgres at %s: %v", dbURL, err)
	}
	return st
}

// TestPostgresStore_MarkJobAttemptSucceeded_CancelRace asserts that when a job enters CANCEL_REQUESTED
// before attempt completion, MarkJobAttemptSucceeded atomically marks the job CANCELLED (per Decision D4)
// while recording the attempt as SUCCEEDED with exit_code 0.
// Disabling the CASE WHEN state = 'CANCEL_REQUESTED' THEN 'CANCELLED' branch MUST cause this test to fail.
func TestPostgresStore_MarkJobAttemptSucceeded_CancelRace(t *testing.T) {
	ctx := context.Background()
	st := connectTestStore(t, ctx)
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, err := st.pool.Exec(ctx, `
		INSERT INTO workspaces (id, name, slug, created_at, updated_at)
		VALUES ($1, 'D4 Race WS', $2, $3, $3)
		ON CONFLICT (id) DO NOTHING;
	`, wsID, "d4-race-"+wsID[:8], now)
	if err != nil {
		t.Fatalf("insert test workspace: %v", err)
	}

	_, err = st.pool.Exec(ctx, `
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
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ($1, 'D5 Race WS', $2, $3, $3) ON CONFLICT (id) DO NOTHING;`, wsID, "d5-race-"+wsID[:8], now)
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
	defer st.Close()

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
		if err := st.MarkReleaseHealthy(ctx, "i1", "r1", "app1", "res1", epoch); err == nil {
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
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ($1, 'Running WS', $2, $3, $3) ON CONFLICT (id) DO NOTHING;`, wsID, "run-ws-"+wsID[:8], now)
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
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ($1, 'Term WS', $2, $3, $3) ON CONFLICT (id) DO NOTHING;`, wsID, "term-ws-"+wsID[:8], now)
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
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ($1, 'Fence WS', $2, $3, $3) ON CONFLICT (id) DO NOTHING;`, wsID, "fence-ws-"+wsID[:8], now)
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
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, _ = st.pool.Exec(ctx, `INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ($1, 'CancelGuard WS', $2, $3, $3) ON CONFLICT (id) DO NOTHING;`, wsID, "cg-ws-"+wsID[:8], now)
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
