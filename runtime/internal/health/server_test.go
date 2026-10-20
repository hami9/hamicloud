package health

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestHealthServer_Liveness(t *testing.T) {
	window := 100 * time.Millisecond
	srv := NewServer(":0", window, nil, nil)
	handler := srv.Handler()

	// 1. Freshly initialized: liveness is OK
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", rec.Code)
	}

	// 2. Advance time past staleness window: liveness fails with 503
	srv.SetLastTick(time.Now().Add(-200 * time.Millisecond))
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected status 503 for stale loop, got %d", rec.Code)
	}

	// 3. Record tick restores liveness to 200
	srv.RecordTick()
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected status 200 after tick, got %d", rec.Code)
	}
}

func TestHealthServer_Readiness_Success(t *testing.T) {
	dbCalled := false
	pingDB := func(ctx context.Context) error {
		dbCalled = true
		return nil
	}

	srv := NewServer(":0", 1*time.Minute, pingDB, nil)
	handler := srv.Handler()

	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d", rec.Code)
	}
	if !dbCalled {
		t.Fatal("expected DB ping to be invoked")
	}
}

func TestHealthServer_Readiness_DBFailure(t *testing.T) {
	pingDB := func(ctx context.Context) error {
		return errors.New("connection refused")
	}

	srv := NewServer(":0", 1*time.Minute, pingDB, nil)
	handler := srv.Handler()

	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected status 503 on db failure, got %d", rec.Code)
	}
}

func TestHealthServer_Readiness_ShuttingDown(t *testing.T) {
	dbCalled := false
	pingDB := func(ctx context.Context) error {
		dbCalled = true
		return nil
	}

	srv := NewServer(":0", 1*time.Minute, pingDB, nil)
	srv.SetShuttingDown()
	handler := srv.Handler()

	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected status 503 during shutdown, got %d", rec.Code)
	}
	if dbCalled {
		t.Fatal("expected DB ping to be skipped during shutdown")
	}
}

func TestHealthServer_StartAndShutdown(t *testing.T) {
	srv := NewServer("127.0.0.1:0", 5*time.Second, func(ctx context.Context) error { return nil }, nil)
	if err := srv.Start(); err != nil {
		t.Fatalf("Start failed: %v", err)
	}

	addr := srv.listener.Addr().String()
	client := &http.Client{Timeout: 2 * time.Second}

	// Verify /healthz over actual HTTP
	resp, err := client.Get(fmt.Sprintf("http://%s/healthz", addr))
	if err != nil {
		t.Fatalf("GET /healthz failed: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK || string(body) != "ok\n" {
		t.Fatalf("unexpected /healthz response: %d %q", resp.StatusCode, string(body))
	}

	// Verify /readyz over actual HTTP
	readyResp, err := client.Get(fmt.Sprintf("http://%s/readyz", addr))
	if err != nil {
		t.Fatalf("GET /readyz failed: %v", err)
	}
	defer readyResp.Body.Close()
	readyBody, _ := io.ReadAll(readyResp.Body)
	if readyResp.StatusCode != http.StatusOK || string(readyBody) != "ready\n" {
		t.Fatalf("unexpected /readyz response: %d %q", readyResp.StatusCode, string(readyBody))
	}

	// Graceful shutdown
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		t.Fatalf("Shutdown failed: %v", err)
	}

	if !srv.IsShuttingDown() {
		t.Fatal("expected IsShuttingDown to return true")
	}
}
