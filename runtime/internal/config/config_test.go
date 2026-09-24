package config_test

import (
	"os"
	"strings"
	"testing"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/config"
)

func clearEnv(t *testing.T) {
	t.Helper()
	keys := []string{
		"ENVIRONMENT",
		"DATABASE_URL",
		"RUNTIME_DATABASE_URL",
		"NATS_URL",
		"WORKER_ID",
		"LEASE_DURATION_SECONDS",
		"RECONCILIATION_PERIOD_SECONDS",
	}
	for _, k := range keys {
		os.Unsetenv(k)
	}
}

func TestLoadFromEnv_TableDriven(t *testing.T) {
	tests := []struct {
		name         string
		env          map[string]string
		expectErr    bool
		errSubstring string
		validateCfg  func(t *testing.T, cfg *config.Config)
	}{
		{
			name: "missing values in development fall back to safe dev defaults",
			env: map[string]string{
				"ENVIRONMENT": "development",
			},
			expectErr: false,
			validateCfg: func(t *testing.T, cfg *config.Config) {
				if cfg.Environment != "development" {
					t.Errorf("expected Environment 'development', got %q", cfg.Environment)
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
			},
		},
		{
			name: "missing RUNTIME_DATABASE_URL in production returns error",
			env: map[string]string{
				"ENVIRONMENT": "production",
			},
			expectErr:    true,
			errSubstring: "RUNTIME_DATABASE_URL",
		},
		{
			name:         "missing RUNTIME_DATABASE_URL when ENVIRONMENT is omitted defaults to production and returns error",
			env:          map[string]string{},
			expectErr:    true,
			errSubstring: "RUNTIME_DATABASE_URL",
		},
		{
			name: "valid custom values are loaded",
			env: map[string]string{
				"ENVIRONMENT":                   "development",
				"RUNTIME_DATABASE_URL":          "postgres://app_user:secret@pg-cluster:5432/app_db?sslmode=require",
				"NATS_URL":                      "nats://nats-cluster:4222",
				"WORKER_ID":                     "worker-node-99",
				"LEASE_DURATION_SECONDS":        "120",
				"RECONCILIATION_PERIOD_SECONDS": "45",
			},
			expectErr: false,
			validateCfg: func(t *testing.T, cfg *config.Config) {
				if cfg.Environment != "development" {
					t.Errorf("expected Environment 'development', got %q", cfg.Environment)
				}
				if cfg.DatabaseURL != "postgres://app_user:secret@pg-cluster:5432/app_db?sslmode=require" {
					t.Errorf("expected custom DatabaseURL, got %q", cfg.DatabaseURL)
				}
				if cfg.NATSURL != "nats://nats-cluster:4222" {
					t.Errorf("expected custom NATSURL, got %q", cfg.NATSURL)
				}
				if cfg.WorkerID != "worker-node-99" {
					t.Errorf("expected WorkerID 'worker-node-99', got %q", cfg.WorkerID)
				}
				if cfg.LeaseDuration != 120*time.Second {
					t.Errorf("expected LeaseDuration 120s, got %v", cfg.LeaseDuration)
				}
				if cfg.ReconciliationPeriod != 45*time.Second {
					t.Errorf("expected ReconciliationPeriod 45s, got %v", cfg.ReconciliationPeriod)
				}
			},
		},
		{
			name: "rejected SQLAlchemy postgresql+asyncpg format",
			env: map[string]string{
				"RUNTIME_DATABASE_URL": "postgresql+asyncpg://hamicloud:secret@localhost:5432/hamicloud",
			},
			expectErr:    true,
			errSubstring: "RUNTIME_DATABASE_URL",
		},
		{
			name: "rejected SQLAlchemy postgresql+psycopg2 format",
			env: map[string]string{
				"RUNTIME_DATABASE_URL": "postgresql+psycopg2://hamicloud:secret@localhost:5432/hamicloud",
			},
			expectErr:    true,
			errSubstring: "RUNTIME_DATABASE_URL",
		},
		{
			name: "invalid integer for LEASE_DURATION_SECONDS",
			env: map[string]string{
				"ENVIRONMENT":            "development",
				"LEASE_DURATION_SECONDS": "invalid-int",
			},
			expectErr:    true,
			errSubstring: "LEASE_DURATION_SECONDS",
		},
		{
			name: "invalid integer for RECONCILIATION_PERIOD_SECONDS",
			env: map[string]string{
				"ENVIRONMENT":                   "development",
				"RECONCILIATION_PERIOD_SECONDS": "xyz",
			},
			expectErr:    true,
			errSubstring: "RECONCILIATION_PERIOD_SECONDS",
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			clearEnv(t)
			defer clearEnv(t)

			for k, v := range tc.env {
				t.Setenv(k, v)
			}

			cfg, err := config.LoadFromEnv()
			if tc.expectErr {
				if err == nil {
					t.Fatalf("expected error containing %q, got nil", tc.errSubstring)
				}
				if !strings.Contains(err.Error(), tc.errSubstring) {
					t.Fatalf("expected error containing %q, got %v", tc.errSubstring, err)
				}
			} else {
				if err != nil {
					t.Fatalf("expected no error, got %v", err)
				}
				if tc.validateCfg != nil {
					tc.validateCfg(t, cfg)
				}
			}
		})
	}
}

func TestLoadFromEnv_PasswordNotLeakedInError(t *testing.T) {
	clearEnv(t)
	defer clearEnv(t)

	secretPassword := "SuperSecretP@ssword987#"
	t.Setenv("RUNTIME_DATABASE_URL", "postgresql+asyncpg://hamicloud:"+secretPassword+"@localhost:5432/hamicloud")

	_, err := config.LoadFromEnv()
	if err == nil {
		t.Fatal("expected error for SQLAlchemy format, got nil")
	}

	if strings.Contains(err.Error(), secretPassword) {
		t.Fatalf("returned error leaked the database password: %q", err.Error())
	}

	if !strings.Contains(err.Error(), "RUNTIME_DATABASE_URL") {
		t.Fatalf("expected error to name the variable RUNTIME_DATABASE_URL, got: %q", err.Error())
	}
}
