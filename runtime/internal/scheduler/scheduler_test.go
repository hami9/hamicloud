package scheduler

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type mockSchedulerStore struct {
	mu                 sync.Mutex
	unadmittedReleases []store.UnadmittedRelease
	unadmittedJobs     []store.UnadmittedJob
	requeuedCount      int
	createdIntents     []*store.ExecutionIntent
}

func (m *mockSchedulerStore) ScanUnadmittedReleases(ctx context.Context, limit int) ([]store.UnadmittedRelease, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.unadmittedReleases, nil
}

func (m *mockSchedulerStore) CreateServiceReleaseIntent(ctx context.Context, rel store.UnadmittedRelease) (*store.ExecutionIntent, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	intent := &store.ExecutionIntent{
		ID:                        "intent-rel-" + rel.ReleaseID,
		ReleaseID:                 &rel.ReleaseID,
		TargetGeneration:          rel.DesiredGeneration,
		DeterministicResourceName: "dep-" + rel.ApplicationSlug + "-123",
	}
	m.createdIntents = append(m.createdIntents, intent)
	return intent, nil
}

func (m *mockSchedulerStore) ScanUnadmittedJobs(ctx context.Context, limit int) ([]store.UnadmittedJob, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.unadmittedJobs, nil
}

func (m *mockSchedulerStore) CreateJobAttemptIntent(ctx context.Context, job store.UnadmittedJob) (*store.ExecutionIntent, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	attemptID := "attempt-1"
	intent := &store.ExecutionIntent{
		ID:                        "intent-job-" + job.JobID,
		JobAttemptID:              &attemptID,
		DeterministicResourceName: "job-" + job.Name + "-1",
	}
	m.createdIntents = append(m.createdIntents, intent)
	return intent, nil
}

func (m *mockSchedulerStore) RequeueRetryWaitJobs(ctx context.Context, baseBackoff time.Duration, limit int) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.requeuedCount, nil
}

func TestScheduler_RunOnce_Empty(t *testing.T) {
	st := &mockSchedulerStore{}
	sched := NewScheduler(st, nil)

	count, err := sched.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if count != 0 {
		t.Errorf("expected count=0, got %d", count)
	}
}

func TestScheduler_RunOnce_AdmitsReleasesAndJobs(t *testing.T) {
	st := &mockSchedulerStore{
		unadmittedReleases: []store.UnadmittedRelease{
			{ReleaseID: "rel-1", ApplicationSlug: "web-app", DesiredGeneration: 1},
		},
		unadmittedJobs: []store.UnadmittedJob{
			{JobID: "job-1", Name: "batch-proc", MaxRetries: 3, State: "QUEUED"},
			{JobID: "job-2", Name: "report-gen", MaxRetries: 3, State: "QUEUED"},
		},
		requeuedCount: 1,
	}
	sched := NewScheduler(st, nil)

	count, err := sched.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// 1 release + 2 jobs = 3 admitted
	if count != 3 {
		t.Errorf("expected 3 admitted intents, got %d", count)
	}
	if len(st.createdIntents) != 3 {
		t.Errorf("expected 3 created intents, got %d", len(st.createdIntents))
	}
}
