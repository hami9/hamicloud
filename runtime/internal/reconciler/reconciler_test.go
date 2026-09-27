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
