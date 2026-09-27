package reconciler

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type mockJobStore struct {
	mu              sync.Mutex
	succeededCalls  int
	failedCalls     int
	cancelledCalls  int
	lastExitCode    int
	lastReason      string
	lastShouldRetry bool
	isCancelled     bool
}

func (m *mockJobStore) MarkJobAttemptSucceeded(ctx context.Context, intentID, attemptID, jobID, resourceUID string, leaseEpoch int) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.succeededCalls++
	return nil
}

func (m *mockJobStore) MarkJobAttemptFailed(ctx context.Context, intentID, attemptID, jobID, reason string, exitCode int, leaseEpoch int, shouldRetry bool) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.failedCalls++
	m.lastExitCode = exitCode
	m.lastReason = reason
	m.lastShouldRetry = shouldRetry
	return nil
}

func (m *mockJobStore) MarkJobAttemptCancelled(ctx context.Context, intentID, attemptID, jobID string, leaseEpoch int) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.cancelledCalls++
	return nil
}

func (m *mockJobStore) IsJobCancelRequested(ctx context.Context, jobID string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.isCancelled, nil
}

type mockJobTaskRunner struct {
	exitCode int
	reason   string
	err      error
	delay    time.Duration
}

func (m *mockJobTaskRunner) RunJob(ctx context.Context, workload *store.ClaimedJobWorkload) (int, string, error) {
	if m.delay > 0 {
		select {
		case <-ctx.Done():
			return -1, "cancelled", ctx.Err()
		case <-time.After(m.delay):
		}
	}
	return m.exitCode, m.reason, m.err
}

func TestJobReconciler_Success(t *testing.T) {
	st := &mockJobStore{}
	runner := &mockJobTaskRunner{exitCode: 0}
	rec := NewJobReconciler(st, runner, nil)

	workload := &store.ClaimedJobWorkload{
		IntentID:       "intent-1",
		JobAttemptID:   "attempt-1",
		JobID:          "job-1",
		AttemptNumber:  1,
		MaxRetries:     3,
		TimeoutSeconds: 60,
	}

	err := rec.ReconcileJob(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.succeededCalls != 1 {
		t.Errorf("expected 1 succeeded call, got %d", st.succeededCalls)
	}
	if st.failedCalls != 0 || st.cancelledCalls != 0 {
		t.Errorf("expected 0 failed/cancelled calls")
	}
}

func TestJobReconciler_TransientFailure_Retries(t *testing.T) {
	st := &mockJobStore{}
	runner := &mockJobTaskRunner{exitCode: 1, reason: "temporary network outage"}
	rec := NewJobReconciler(st, runner, nil)

	workload := &store.ClaimedJobWorkload{
		IntentID:       "intent-1",
		JobAttemptID:   "attempt-1",
		JobID:          "job-1",
		AttemptNumber:  1,
		MaxRetries:     3,
		TimeoutSeconds: 60,
	}

	err := rec.ReconcileJob(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.failedCalls != 1 {
		t.Errorf("expected 1 failed call, got %d", st.failedCalls)
	}
	if !st.lastShouldRetry {
		t.Errorf("expected shouldRetry=true for attempt 1 of 3")
	}
	if st.lastExitCode != 1 {
		t.Errorf("expected exitCode=1, got %d", st.lastExitCode)
	}
}

func TestJobReconciler_BudgetExhausted_NoRetry(t *testing.T) {
	st := &mockJobStore{}
	runner := &mockJobTaskRunner{exitCode: 2, reason: "fatal bad argument"}
	rec := NewJobReconciler(st, runner, nil)

	// Attempt 4 of MaxRetries 3 -> budget exhausted
	workload := &store.ClaimedJobWorkload{
		IntentID:       "intent-1",
		JobAttemptID:   "attempt-4",
		JobID:          "job-1",
		AttemptNumber:  4,
		MaxRetries:     3,
		TimeoutSeconds: 60,
	}

	err := rec.ReconcileJob(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.failedCalls != 1 {
		t.Errorf("expected 1 failed call, got %d", st.failedCalls)
	}
	if st.lastShouldRetry {
		t.Errorf("expected shouldRetry=false when attempts exhausted")
	}
}

func TestJobReconciler_Cancellation_BeforeExecution(t *testing.T) {
	st := &mockJobStore{isCancelled: true}
	runner := &mockJobTaskRunner{exitCode: 0}
	rec := NewJobReconciler(st, runner, nil)

	workload := &store.ClaimedJobWorkload{
		IntentID:       "intent-1",
		JobAttemptID:   "attempt-1",
		JobID:          "job-1",
		AttemptNumber:  1,
		MaxRetries:     3,
		TimeoutSeconds: 60,
	}

	err := rec.ReconcileJob(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.cancelledCalls != 1 {
		t.Errorf("expected 1 cancelled call, got %d", st.cancelledCalls)
	}
	if st.succeededCalls != 0 || st.failedCalls != 0 {
		t.Errorf("expected no succeeded or failed calls")
	}
}

func TestJobReconciler_Cancellation_DuringExecution(t *testing.T) {
	st := &mockJobStore{isCancelled: false}
	runner := &mockJobTaskRunner{
		delay: 50 * time.Millisecond,
		err:   errors.New("interrupted"),
	}
	rec := NewJobReconciler(st, runner, nil)

	workload := &store.ClaimedJobWorkload{
		IntentID:       "intent-1",
		JobAttemptID:   "attempt-1",
		JobID:          "job-1",
		AttemptNumber:  1,
		MaxRetries:     3,
		TimeoutSeconds: 60,
	}

	// Trigger cancellation shortly after starting
	go func() {
		time.Sleep(15 * time.Millisecond)
		st.mu.Lock()
		st.isCancelled = true
		st.mu.Unlock()
	}()

	err := rec.ReconcileJob(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if st.cancelledCalls != 1 {
		t.Errorf("expected 1 cancelled call during execution, got %d", st.cancelledCalls)
	}
}

func TestHelperProcess(t *testing.T) {
	if os.Getenv("GO_WANT_HELPER_PROCESS") != "1" {
		return
	}
	for _, env := range os.Environ() {
		fmt.Println(env)
	}
	os.Exit(0)
}

func TestLocalProcessJobRunner_RefusesNonDevelopmentEnvironment(t *testing.T) {
	for _, env := range []string{"production", "staging", "", "test"} {
		runner, err := NewLocalProcessJobRunner(env, "")
		if err == nil {
			t.Errorf("expected error constructing LocalProcessJobRunner in env %q, got runner: %+v", env, runner)
		}
	}
	runner, err := NewLocalProcessJobRunner("development", t.TempDir())
	if err != nil {
		t.Fatalf("expected success constructing LocalProcessJobRunner in development, got: %v", err)
	}
	if runner == nil {
		t.Fatal("expected non-nil runner in development")
	}
}

func TestLocalProcessJobRunner_EnvironmentIsolation_DatabaseURLNotVisible(t *testing.T) {
	// Set host platform credential in current executor environment
	secretURL := "postgres://executor_user:SUPER_SECRET_PASSWORD@127.0.0.1:5432/hamicloud"
	t.Setenv("RUNTIME_DATABASE_URL", secretURL)
	t.Setenv("HOST_LEAK_VAR", "leaked_host_value")

	tmpDir := t.TempDir()
	runner, err := NewLocalProcessJobRunner("development", tmpDir)
	if err != nil {
		t.Fatalf("failed to create runner: %v", err)
	}

	workload := &store.ClaimedJobWorkload{
		WorkspaceID:   "ws-sec-test",
		JobID:         "job-sec-test",
		AttemptNumber: 1,
		CommandArgs:   []string{os.Args[0], "-test.run=TestHelperProcess", "--"},
		EnvVars: map[string]string{
			"GO_WANT_HELPER_PROCESS": "1",
			"DECLARED_JOB_VAR":       "declared_value_ok",
		},
	}

	exitCode, reason, err := runner.RunJob(context.Background(), workload)
	if err != nil {
		t.Fatalf("job execution failed: %v (reason: %s)", err, reason)
	}
	if exitCode != 0 {
		t.Fatalf("expected exit code 0, got %d (reason: %s)", exitCode, reason)
	}

	outputFile := filepath.Join(tmpDir, "ws-sec-test", "job-sec-test", "output.txt")
	contentBytes, err := os.ReadFile(outputFile)
	if err != nil {
		t.Fatalf("failed to read artifact output: %v", err)
	}
	output := string(contentBytes)

	// Assert declared env vars ARE present
	if !strings.Contains(output, "DECLARED_JOB_VAR=declared_value_ok") {
		t.Errorf("expected output to contain DECLARED_JOB_VAR=declared_value_ok, got:\n%s", output)
	}

	// Assert executor host credentials and env vars are NOT present
	if strings.Contains(output, "RUNTIME_DATABASE_URL") || strings.Contains(output, "SUPER_SECRET_PASSWORD") {
		t.Fatalf("SECURITY LEAK: output contains RUNTIME_DATABASE_URL or secret password!\n%s", output)
	}
	if strings.Contains(output, "HOST_LEAK_VAR") {
		t.Fatalf("SECURITY LEAK: output contains host environment variable HOST_LEAK_VAR!\n%s", output)
	}
}
