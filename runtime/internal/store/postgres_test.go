package store

import (
	"context"
	"os"
	"testing"
	"time"
)

func getTestDatabaseURL() string {
	if url := os.Getenv("RUNTIME_DATABASE_URL"); url != "" {
		return url
	}
	return "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud_test?sslmode=disable"
}

// TestPostgresStore_MarkJobAttemptSucceeded_CancelRace asserts that when a job enters CANCEL_REQUESTED
// before attempt completion, MarkJobAttemptSucceeded atomically marks the job CANCELLED (per Decision D4)
// while recording the attempt as SUCCEEDED with exit_code 0.
// Disabling the CASE WHEN state = 'CANCEL_REQUESTED' THEN 'CANCELLED' branch MUST cause this test to fail.
func TestPostgresStore_MarkJobAttemptSucceeded_CancelRace(t *testing.T) {
	ctx := context.Background()
	dbURL := getTestDatabaseURL()

	st, err := NewPostgresStore(ctx, dbURL)
	if err != nil {
		t.Skipf("skipping integration test, cannot connect to postgres at %s: %v", dbURL, err)
	}
	defer st.Close()

	now := time.Now().UTC()
	wsID := NewUUID()
	jobID := NewUUID()
	attemptID := NewUUID()
	intentID := NewUUID()

	_, err = st.pool.Exec(ctx, `
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
	dbURL := getTestDatabaseURL()

	st, err := NewPostgresStore(ctx, dbURL)
	if err != nil {
		t.Skipf("skipping integration test, cannot connect to postgres at %s: %v", dbURL, err)
	}
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
	err = st.MarkJobAttemptFailed(ctx, intentID, attemptID, jobID, "process killed by signal", 137, 1, true)
	if err != nil {
		t.Fatalf("MarkJobAttemptFailed returned error: %v", err)
	}

	var jobState string
	_ = st.pool.QueryRow(ctx, `SELECT state FROM jobs WHERE id = $1;`, jobID).Scan(&jobState)
	if jobState != "CANCELLED" {
		t.Fatalf("Decision D5 violation: job in CANCEL_REQUESTED that failed must transition to CANCELLED, got %s", jobState)
	}
}

// TestPostgresStore_UnfencedIntentRejected verifies that leaseEpoch <= 0 is explicitly rejected.
func TestPostgresStore_UnfencedIntentRejected(t *testing.T) {
	ctx := context.Background()
	dbURL := getTestDatabaseURL()

	st, err := NewPostgresStore(ctx, dbURL)
	if err != nil {
		t.Skipf("skipping integration test, cannot connect to postgres at %s: %v", dbURL, err)
	}
	defer st.Close()

	if err := st.MarkJobAttemptSucceeded(ctx, "i1", "a1", "j1", "r1", 0); err == nil {
		t.Fatal("expected error for leaseEpoch == 0 in MarkJobAttemptSucceeded, got nil")
	}
	if err := st.MarkJobAttemptFailed(ctx, "i1", "a1", "j1", "err", 1, 0, false); err == nil {
		t.Fatal("expected error for leaseEpoch == 0 in MarkJobAttemptFailed, got nil")
	}
	if err := st.MarkJobAttemptCancelled(ctx, "i1", "a1", "j1", 0); err == nil {
		t.Fatal("expected error for leaseEpoch == 0 in MarkJobAttemptCancelled, got nil")
	}
	if err := st.MarkReleaseHealthy(ctx, "i1", "r1", "app1", "res1", 0); err == nil {
		t.Fatal("expected error for leaseEpoch == 0 in MarkReleaseHealthy, got nil")
	}
	if err := st.MarkReleaseFailed(ctx, "i1", "r1", "err", 0); err == nil {
		t.Fatal("expected error for leaseEpoch == 0 in MarkReleaseFailed, got nil")
	}
}
