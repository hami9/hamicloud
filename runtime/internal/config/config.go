package config

import (
	"fmt"
	"os"
	"path/filepath"
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
	ArtifactsDir         string
	KubeconfigPath       string
	KubeNamespace        string
	IngressDomain        string
	LeaseDuration        time.Duration
	ReconciliationPeriod time.Duration
	RunOnce              bool
}

// LoadFromEnv loads runtime configuration from environment variables with safe defaults.
func LoadFromEnv() (*Config, error) {
	env := getEnv("ENVIRONMENT", "production")

	dbURL := getEnv("RUNTIME_DATABASE_URL", "")
	if dbURL == "" {
		if env != "development" {
			return nil, fmt.Errorf("RUNTIME_DATABASE_URL is required when ENVIRONMENT is not development")
		}
		dbURL = "postgres://hamicloud:hamicloud_secret@localhost:5432/hamicloud?sslmode=disable"
	}
	if strings.HasPrefix(dbURL, "postgresql+") {
		return nil, fmt.Errorf("invalid RUNTIME_DATABASE_URL: SQLAlchemy driver format is rejected")
	}

	artifactsDir := getEnv("ARTIFACTS_DIR", "")
	if artifactsDir == "" {
		if env != "development" {
			return nil, fmt.Errorf("ARTIFACTS_DIR is required when ENVIRONMENT is not development")
		}
		artifactsDir = filepath.Join("var", "artifacts")
	}
	absArtifactsDir, err := filepath.Abs(artifactsDir)
	if err != nil {
		return nil, fmt.Errorf("resolve ARTIFACTS_DIR: %w", err)
	}

	natsURL := getEnv("NATS_URL", "nats://localhost:4222")
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

	kubeconfigPath := getEnv("KUBECONFIG", "")
	kubeNamespace := getEnv("KUBE_NAMESPACE", "default")
	ingressDomain := getEnv("INGRESS_DOMAIN", "")

	runOnce := getEnv("RUN_ONCE", "false") == "true"
	for _, arg := range os.Args[1:] {
		if arg == "--run-once" || arg == "-run-once" {
			runOnce = true
			break
		}
	}

	return &Config{
		Environment:          env,
		DatabaseURL:          dbURL,
		NATSURL:              natsURL,
		WorkerID:             workerID,
		ArtifactsDir:         absArtifactsDir,
		KubeconfigPath:       kubeconfigPath,
		KubeNamespace:        kubeNamespace,
		IngressDomain:        ingressDomain,
		LeaseDuration:        time.Duration(leaseSec) * time.Second,
		ReconciliationPeriod: time.Duration(reconSec) * time.Second,
		RunOnce:              runOnce,
	}, nil
}

func getEnv(key, defaultVal string) string {
	if val, ok := os.LookupEnv(key); ok && val != "" {
		return val
	}
	return defaultVal
}
