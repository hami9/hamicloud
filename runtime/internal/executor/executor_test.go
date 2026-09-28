package executor

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type mockStore struct {
	mu           sync.Mutex
	claimed      *store.ClaimedWorkload
	claimedJob   *store.ClaimedJobWorkload
	claimErr     error
	renewCalls   int
	renewErr     error
	renewBlocker chan struct{}
}

func (m *mockStore) ClaimNextServiceRelease(ctx context.Context, workerID string, leaseDuration time.Duration) (*store.ClaimedWorkload, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.claimed, m.claimErr
}

func (m *mockStore) ClaimNextJobAttempt(ctx context.Context, workerID string, leaseDuration time.Duration) (*store.ClaimedJobWorkload, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.claimedJob, m.claimErr
}

func (m *mockStore) RenewLease(ctx context.Context, intentID string, currentEpoch int, extension time.Duration) error {
	m.mu.Lock()
	m.renewCalls++
	err := m.renewErr
	blocker := m.renewBlocker
	m.mu.Unlock()

	if blocker != nil {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-blocker:
		}
	}
	return err
}

type mockReconciler struct {
	mu           sync.Mutex
	reconcileErr error
	reconciled   bool
	observedCtx  context.Context
	onReconcile  func(ctx context.Context)
}

func (m *mockReconciler) ReconcileOne(ctx context.Context, workload *store.ClaimedWorkload) error {
	m.mu.Lock()
	m.reconciled = true
	m.observedCtx = ctx
	callback := m.onReconcile
	err := m.reconcileErr
	m.mu.Unlock()

	if callback != nil {
		callback(ctx)
	}
	return err
}

type mockJobReconciler struct {
	mu           sync.Mutex
	reconciled   bool
	reconcileErr error
}

func (m *mockJobReconciler) ReconcileJob(ctx context.Context, workload *store.ClaimedJobWorkload) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.reconciled = true
	return m.reconcileErr
}

func TestExecutor_RunOnce_NoPendingIntents(t *testing.T) {
	st := &mockStore{claimed: nil}
	rec := &mockReconciler{}
	exec := NewExecutor(st, rec, "worker-1", 10*time.Second, nil)

	processed, err := exec.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if processed {
		t.Errorf("expected processed=false when store returns nil workload")
	}
	if rec.reconciled {
		t.Errorf("expected reconciler not to be called")
	}
}

func TestExecutor_RunOnce_ClaimAndReconcileSuccess(t *testing.T) {
	workload := &store.ClaimedWorkload{
		IntentID:        "intent-123",
		ReleaseID:       "rel-456",
		ApplicationSlug: "web-svc",
		Port:            8080,
		HealthPath:      "/healthz",
		LeaseEpoch:      1,
	}
	st := &mockStore{claimed: workload}
	rec := &mockReconciler{}
	exec := NewExecutor(st, rec, "worker-1", 10*time.Second, nil)

	processed, err := exec.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !processed {
		t.Errorf("expected processed=true")
	}
	if !rec.reconciled {
		t.Errorf("expected reconciler to be called")
	}
}

func TestExecutor_RunOnce_HeartbeatLeaseRenewal(t *testing.T) {
	workload := &store.ClaimedWorkload{
		IntentID:        "intent-heartbeat",
		ReleaseID:       "rel-hb",
		ApplicationSlug: "hb-app",
		Port:            8080,
		HealthPath:      "/healthz",
		LeaseEpoch:      1,
	}
	st := &mockStore{claimed: workload}
	rec := &mockReconciler{
		onReconcile: func(ctx context.Context) {
			// Sleep long enough for at least one renewal ticker tick
			time.Sleep(50 * time.Millisecond)
		},
	}
	// leaseDuration 60ms -> renewInterval 20ms
	exec := NewExecutor(st, rec, "worker-hb", 60*time.Millisecond, nil)

	processed, err := exec.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !processed {
		t.Errorf("expected processed=true")
	}

	st.mu.Lock()
	calls := st.renewCalls
	st.mu.Unlock()

	if calls < 1 {
		t.Errorf("expected at least 1 lease renewal call, got %d", calls)
	}
}

func TestExecutor_RunOnce_LeaseRenewalFailureCancelsWorkloadCtx(t *testing.T) {
	workload := &store.ClaimedWorkload{
		IntentID:        "intent-lost",
		ReleaseID:       "rel-lost",
		ApplicationSlug: "lost-app",
		Port:            8080,
		HealthPath:      "/healthz",
		LeaseEpoch:      1,
	}
	st := &mockStore{
		claimed:  workload,
		renewErr: errors.New("lease renewal rejected: epoch is stale"),
	}

	workloadCtxCancelled := make(chan struct{})
	rec := &mockReconciler{
		onReconcile: func(ctx context.Context) {
			select {
			case <-ctx.Done():
				close(workloadCtxCancelled)
			case <-time.After(500 * time.Millisecond):
				t.Errorf("timed out waiting for workload context to be cancelled after lease renewal failure")
			}
		},
	}
	// leaseDuration 30ms -> renewInterval 10ms
	exec := NewExecutor(st, rec, "worker-fenced", 30*time.Millisecond, nil)

	_, _ = exec.RunOnce(context.Background())

	select {
	case <-workloadCtxCancelled:
		// Succeeded: workload context was cancelled by executor when lease renewal failed
	case <-time.After(200 * time.Millisecond):
		t.Errorf("expected workload context to be cancelled upon fencing/renewal failure")
	}
}

func TestExecutor_Start_StopsOnContextCancellation(t *testing.T) {
	st := &mockStore{claimed: nil}
	rec := &mockReconciler{}
	exec := NewExecutor(st, rec, "worker-1", 10*time.Second, nil)

	ctx, cancel := context.WithCancel(context.Background())
	errCh := make(chan error, 1)

	go func() {
		errCh <- exec.Start(ctx, 10*time.Millisecond)
	}()

	time.Sleep(20 * time.Millisecond)
	cancel()

	select {
	case err := <-errCh:
		if !errors.Is(err, context.Canceled) {
			t.Errorf("expected context.Canceled, got %v", err)
		}
	case <-time.After(1 * time.Second):
		t.Fatalf("executor Start failed to stop within 1 second after context cancel")
	}
}

func TestExecutor_RunOnce_ClaimAndReconcileJobSuccess(t *testing.T) {
	jobWorkload := &store.ClaimedJobWorkload{
		IntentID:       "intent-job-1",
		JobAttemptID:   "attempt-1",
		JobID:          "job-1",
		JobName:        "test-task",
		AttemptNumber:  1,
		LeaseEpoch:     1,
		TimeoutSeconds: 60,
	}
	st := &mockStore{claimedJob: jobWorkload}
	rec := &mockReconciler{}
	jobRec := &mockJobReconciler{}
	exec := NewExecutor(st, rec, "worker-job", 10*time.Second, nil)
	exec.SetJobReconciler(jobRec)

	processed, err := exec.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !processed {
		t.Errorf("expected processed=true for job attempt")
	}
	if !jobRec.reconciled {
		t.Errorf("expected job reconciler to be called")
	}
	if rec.reconciled {
		t.Errorf("expected service reconciler NOT to be called when job claimed")
	}
}

func TestExecutor_Wake(t *testing.T) {
	st := &mockStore{claimed: nil}
	rec := &mockReconciler{}
	exec := NewExecutor(st, rec, "worker-wake", 10*time.Second, nil)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	exec.Wake()

	done := make(chan struct{})
	go func() {
		_ = exec.Start(ctx, 10*time.Second)
		close(done)
	}()

	time.Sleep(20 * time.Millisecond)
	exec.Wake()
	time.Sleep(20 * time.Millisecond)
	cancel()
	<-done
}
