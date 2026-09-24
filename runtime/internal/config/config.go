package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds runtime configuration for the scheduler and executor processes.
type Config struct {
	Environment          string
	DatabaseURL          string
	NATSURL              string
	WorkerID             string
	LeaseDuration        time.Duration
	ReconciliationPeriod time.Duration
}

// LoadFromEnv loads runtime configuration from environment variables with safe defaults.
func LoadFromEnv() (*Config, error) {
	dbURL := getEnv("RUNTIME_DATABASE_URL", "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable")
	if strings.HasPrefix(dbURL, "postgresql+") {
		return nil, fmt.Errorf("invalid RUNTIME_DATABASE_URL: SQLAlchemy driver format is rejected (%s)", dbURL)
	}

	natsURL := getEnv("NATS_URL", "nats://localhost:4222")
	env := getEnv("ENVIRONMENT", "production")
	workerID := getEnv("WORKER_ID", "local-worker-1")

	leaseSecStr := getEnv("LEASE_DURATION_SECONDS", "60")
	leaseSec, err := strconv.Atoi(leaseSecStr)
	if err != nil {
		return nil, fmt.Errorf("invalid LEASE_DURATION_SECONDS: %w", err)
	}

	reconSecStr := getEnv("RECONCILIATION_PERIOD_SECONDS", "30")
	reconSec, err := strconv.Atoi(reconSecStr)
	if err != nil {
		return nil, fmt.Errorf("invalid RECONCILIATION_PERIOD_SECONDS: %w", err)
	}

	return &Config{
		Environment:          env,
		DatabaseURL:          dbURL,
		NATSURL:              natsURL,
		WorkerID:             workerID,
		LeaseDuration:        time.Duration(leaseSec) * time.Second,
		ReconciliationPeriod: time.Duration(reconSec) * time.Second,
	}, nil
}

func getEnv(key, defaultVal string) string {
	if val, ok := os.LookupEnv(key); ok && val != "" {
		return val
	}
	return defaultVal
}
