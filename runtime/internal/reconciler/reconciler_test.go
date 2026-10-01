package reconciler

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

func TestHTTPProbeRunner_HealthyEndpoint(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"ok"}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer ts.Close()

	u, err := url.Parse(ts.URL)
	if err != nil {
		t.Fatalf("parse server url: %v", err)
	}
	port, _ := strconv.Atoi(u.Port())

	runner, err := NewHTTPProbeRunner("development", u.Hostname())
	if err != nil {
		t.Fatalf("failed to create runner: %v", err)
	}
	workload := &store.ClaimedWorkload{
		Port:       port,
		HealthPath: "/healthz",
	}

	ready, reason, err := runner.CheckReadiness(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !ready {
		t.Errorf("expected ready=true, got ready=false with reason: %s", reason)
	}
	if reason != "" {
		t.Errorf("expected empty reason for healthy endpoint, got: %s", reason)
	}
}

func TestHTTPProbeRunner_UnhealthyHTTPStatus(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("internal crash"))
	}))
	defer ts.Close()

	u, err := url.Parse(ts.URL)
	if err != nil {
		t.Fatalf("parse server url: %v", err)
	}
	port, _ := strconv.Atoi(u.Port())

	runner, err := NewHTTPProbeRunner("development", u.Hostname())
	if err != nil {
		t.Fatalf("failed to create runner: %v", err)
	}
	workload := &store.ClaimedWorkload{
		Port:       port,
		HealthPath: "/healthz",
	}

	ready, reason, err := runner.CheckReadiness(context.Background(), workload)
	if err != nil {
		t.Fatalf("unexpected probe error: %v", err)
	}
	if ready {
		t.Errorf("expected ready=false for 500 status, got ready=true")
	}
	if reason == "" {
		t.Errorf("expected explanatory reason for unhealthy status, got empty")
	}
}

func TestHTTPProbeRunner_ConnectionRefused(t *testing.T) {
	// Port 1 is reserved and typically nothing is listening
	runner, err := NewHTTPProbeRunner("development", "127.0.0.1")
	if err != nil {
		t.Fatalf("failed to create runner: %v", err)
	}
	workload := &store.ClaimedWorkload{
		Port:       1,
		HealthPath: "/healthz",
	}

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	ready, reason, err := runner.CheckReadiness(ctx, workload)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if ready {
		t.Errorf("expected ready=false for closed port, got ready=true")
	}
	if reason == "" {
		t.Errorf("expected explanatory reason for connection failure, got empty")
	}
}

func TestHTTPProbeRunner_RefusesNonDevelopmentEnvironment(t *testing.T) {
	for _, env := range []string{"production", "staging", "", "test"} {
		runner, err := NewHTTPProbeRunner(env, "127.0.0.1")
		if err == nil {
			t.Errorf("expected error constructing HTTPProbeRunner in env %q, got runner: %+v", env, runner)
		}
	}
	runner, err := NewHTTPProbeRunner("development", "127.0.0.1")
	if err != nil {
		t.Fatalf("expected success constructing HTTPProbeRunner in development, got: %v", err)
	}
	if runner == nil {
		t.Fatal("expected non-nil runner in development")
	}
}

type mockReconcilerStore struct {
	becameCurrent bool
	healthyCalled bool
	failedCalled  bool
}

func (m *mockReconcilerStore) MarkReleaseHealthy(ctx context.Context, intentID, releaseID, appID, resourceUID string, leaseEpoch int) (bool, error) {
	m.healthyCalled = true
	return m.becameCurrent, nil
}

func (m *mockReconcilerStore) MarkReleaseFailed(ctx context.Context, intentID, releaseID, reason string, leaseEpoch int) error {
	m.failedCalled = true
	return nil
}

type mockReconcilerRunner struct {
	teardownSupersededAppID string
	teardownSupersededGen   int
	teardownSupersededCount int
}

func (m *mockReconcilerRunner) Deploy(ctx context.Context, workload *store.ClaimedWorkload) (string, error) {
	return "uid-123", nil
}

func (m *mockReconcilerRunner) CheckReadiness(ctx context.Context, workload *store.ClaimedWorkload) (bool, string, error) {
	return true, "", nil
}

func (m *mockReconcilerRunner) Teardown(ctx context.Context, workload *store.ClaimedWorkload) error {
	return nil
}

func (m *mockReconcilerRunner) TeardownSupersededGenerations(ctx context.Context, applicationID string, currentGeneration int) error {
	m.teardownSupersededAppID = applicationID
	m.teardownSupersededGen = currentGeneration
	m.teardownSupersededCount++
	return nil
}

func TestServiceReconciler_TeardownOnlyWhenBecameCurrent(t *testing.T) {
	ctx := context.Background()

	workload := &store.ClaimedWorkload{
		IntentID:                  "intent-2",
		ReleaseID:                 "rel-2",
		ApplicationID:             "app-123",
		ApplicationSlug:           "my-svc",
		Port:                      8080,
		TargetGeneration:          2,
		DeterministicResourceName: "hc-svc-app-123-2",
		LeaseEpoch:                1,
	}

	// Case 1: becameCurrent is true -> TeardownSupersededGenerations MUST be called
	storeCurrent := &mockReconcilerStore{becameCurrent: true}
	runnerCurrent := &mockReconcilerRunner{}
	recCurrent := NewServiceReconciler(storeCurrent, runnerCurrent, nil)

	err := recCurrent.ReconcileOne(ctx, workload)
	if err != nil {
		t.Fatalf("unexpected reconcile error: %v", err)
	}
	if !storeCurrent.healthyCalled {
		t.Errorf("expected MarkReleaseHealthy to be called")
	}
	if runnerCurrent.teardownSupersededCount != 1 {
		t.Errorf("expected TeardownSupersededGenerations to be called once when becameCurrent=true, got %d", runnerCurrent.teardownSupersededCount)
	}
	if runnerCurrent.teardownSupersededAppID != "app-123" || runnerCurrent.teardownSupersededGen != 2 {
		t.Errorf("expected teardown for app-123 gen 2, got app=%s gen=%d", runnerCurrent.teardownSupersededAppID, runnerCurrent.teardownSupersededGen)
	}

	// Case 2: becameCurrent is false -> TeardownSupersededGenerations MUST NOT be called
	storeNotCurrent := &mockReconcilerStore{becameCurrent: false}
	runnerNotCurrent := &mockReconcilerRunner{}
	recNotCurrent := NewServiceReconciler(storeNotCurrent, runnerNotCurrent, nil)

	err = recNotCurrent.ReconcileOne(ctx, workload)
	if err != nil {
		t.Fatalf("unexpected reconcile error: %v", err)
	}
	if runnerNotCurrent.teardownSupersededCount != 0 {
		t.Errorf("expected TeardownSupersededGenerations NOT to be called when becameCurrent=false, got %d", runnerNotCurrent.teardownSupersededCount)
	}
}
