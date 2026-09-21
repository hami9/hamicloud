package config_test

import (
	"os"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/config"
)

func clearEnv(t *testing.T) {
	t.Helper()
	keys := []string{
		"ENVIRONMENT",
		"DATABASE_URL",
		"NATS_URL",
		"WORKER_ID",
		"LEASE_DURATION_SECONDS",
		"RECONCILIATION_PERIOD_SECONDS",
	}
	for _, k := range keys {
		os.Unsetenv(k)
	}
}

func TestLoadFromEnv_Defaults(t *testing.T) {
	clearEnv(t)
	defer clearEnv(t)

	cfg, err := config.LoadFromEnv()
	if err != nil {
		t.Fatalf("expected no error loading default config, got %v", err)
	}

	if cfg.Environment != "production" {
		t.Errorf("expected default Environment to fail-closed to 'production', got %q", cfg.Environment)
	}

	expectedDB := "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
	if cfg.DatabaseURL != expectedDB {
		t.Errorf("expected DatabaseURL %q, got %q", expectedDB, cfg.DatabaseURL)
	}

	if cfg.NATSURL != "nats://localhost:4222" {
		t.Errorf("expected NATSURL 'nats://localhost:4222', got %q", cfg.NATSURL)
	}

	if cfg.WorkerID != "local-worker-1" {
		t.Errorf("expected WorkerID 'local-worker-1', got %q", cfg.WorkerID)
	}

	if cfg.LeaseDuration != 60*time.Second {
		t.Errorf("expected LeaseDuration 60s, got %v", cfg.LeaseDuration)
	}

	if cfg.ReconciliationPeriod != 30*time.Second {
		t.Errorf("expected ReconciliationPeriod 30s, got %v", cfg.ReconciliationPeriod)
	}
}

func TestLoadFromEnv_CustomOverrides(t *testing.T) {
	clearEnv(t)
	defer clearEnv(t)

	t.Setenv("ENVIRONMENT", "development")
	t.Setenv("DATABASE_URL", "postgres://custom_user:pass@db-host:5432/custom_db")
	t.Setenv("NATS_URL", "nats://nats-cluster:4222")
	t.Setenv("WORKER_ID", "executor-prod-node-3")
	t.Setenv("LEASE_DURATION_SECONDS", "120")
	t.Setenv("RECONCILIATION_PERIOD_SECONDS", "45")

	cfg, err := config.LoadFromEnv()
	if err != nil {
		t.Fatalf("expected no error loading overridden config, got %v", err)
	}

	if cfg.Environment != "development" {
		t.Errorf("expected Environment 'development', got %q", cfg.Environment)
	}

	if cfg.DatabaseURL != "postgres://custom_user:pass@db-host:5432/custom_db" {
		t.Errorf("expected custom DatabaseURL, got %q", cfg.DatabaseURL)
	}

	if cfg.NATSURL != "nats://nats-cluster:4222" {
		t.Errorf("expected custom NATSURL, got %q", cfg.NATSURL)
	}

	if cfg.WorkerID != "executor-prod-node-3" {
		t.Errorf("expected custom WorkerID, got %q", cfg.WorkerID)
	}

	if cfg.LeaseDuration != 120*time.Second {
		t.Errorf("expected LeaseDuration 120s, got %v", cfg.LeaseDuration)
	}

	if cfg.ReconciliationPeriod != 45*time.Second {
		t.Errorf("expected ReconciliationPeriod 45s, got %v", cfg.ReconciliationPeriod)
	}
}

func TestLoadFromEnv_PostgresqlAsyncpgPrefixNormalization(t *testing.T) {
	clearEnv(t)
	defer clearEnv(t)

	t.Setenv("DATABASE_URL", "postgresql+asyncpg://hamicloud:hamicloud_secret@localhost:5432/hamicloud")

	cfg, err := config.LoadFromEnv()
	if err != nil {
		t.Fatalf("expected no error, got %v", err)
	}

	expectedDB := "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud"
	if cfg.DatabaseURL != expectedDB {
		t.Errorf("expected prefix 'postgresql+asyncpg://' normalized to 'postgres://', got %q", cfg.DatabaseURL)
	}
}

func TestLoadFromEnv_InvalidLeaseDuration(t *testing.T) {
	clearEnv(t)
	defer clearEnv(t)

	t.Setenv("LEASE_DURATION_SECONDS", "not-a-number")

	_, err := config.LoadFromEnv()
	if err == nil {
		t.Fatal("expected error on invalid LEASE_DURATION_SECONDS, got nil")
	}
}

func TestLoadFromEnv_InvalidReconciliationPeriod(t *testing.T) {
	clearEnv(t)
	defer clearEnv(t)

	t.Setenv("RECONCILIATION_PERIOD_SECONDS", "invalid-seconds")

	_, err := config.LoadFromEnv()
	if err == nil {
		t.Fatal("expected error on invalid RECONCILIATION_PERIOD_SECONDS, got nil")
	}
}
