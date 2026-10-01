package reconciler

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

// WorkloadRunner abstracts the physical infrastructure deployment and health verification.
type WorkloadRunner interface {
	Deploy(ctx context.Context, workload *store.ClaimedWorkload) (string, error)
	CheckReadiness(ctx context.Context, workload *store.ClaimedWorkload) (bool, string, error)
	Teardown(ctx context.Context, workload *store.ClaimedWorkload) error
}

// HTTPProbeRunner is a development-only runner that probes service readiness directly against localhost.
// It is NOT a security boundary, executes no container lifecycle management, and MUST NEVER be used outside ENVIRONMENT=development.
type HTTPProbeRunner struct {
	BaseHost   string
	HTTPClient *http.Client
}

func NewHTTPProbeRunner(env string, baseHost string) (*HTTPProbeRunner, error) {
	if env != "development" {
		return nil, fmt.Errorf("HTTPProbeRunner is forbidden when ENVIRONMENT=%q: local runners are development-only and require a real Kubernetes runner outside development", env)
	}
	if baseHost == "" {
		baseHost = "127.0.0.1"
	}
	return &HTTPProbeRunner{
		BaseHost: baseHost,
		HTTPClient: &http.Client{
			Timeout: 2 * time.Second,
		},
	}, nil
}

func (h *HTTPProbeRunner) Deploy(ctx context.Context, workload *store.ClaimedWorkload) (string, error) {
	// Resource UID matches deterministic name
	return workload.DeterministicResourceName, nil
}

func (h *HTTPProbeRunner) CheckReadiness(ctx context.Context, workload *store.ClaimedWorkload) (bool, string, error) {
	healthPath := workload.HealthPath
	if healthPath == "" {
		healthPath = "/"
	} else if !strings.HasPrefix(healthPath, "/") {
		healthPath = "/" + healthPath
	}

	targetURL := fmt.Sprintf("http://%s:%d%s", h.BaseHost, workload.Port, healthPath)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, targetURL, nil)
	if err != nil {
		return false, fmt.Sprintf("failed to construct probe request: %v", err), nil
	}

	resp, err := h.HTTPClient.Do(req)
	if err != nil {
		return false, fmt.Sprintf("readiness probe failed on %s: %v", targetURL, err), nil
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)

	if resp.StatusCode >= 200 && resp.StatusCode < 300 {
		return true, "", nil
	}

	return false, fmt.Sprintf("readiness probe returned unhealthy HTTP status %d on %s", resp.StatusCode, targetURL), nil
}

func (h *HTTPProbeRunner) Teardown(ctx context.Context, workload *store.ClaimedWorkload) error {
	return nil
}

type ServiceReconciler struct {
	store         *store.PostgresStore
	runner        WorkloadRunner
	logger        *slog.Logger
	maxRetries    int
	retryInterval time.Duration
}

func NewServiceReconciler(st *store.PostgresStore, runner WorkloadRunner, logger *slog.Logger) *ServiceReconciler {
	if logger == nil {
		logger = slog.Default()
	}
	return &ServiceReconciler{
		store:         st,
		runner:        runner,
		logger:        logger,
		maxRetries:    3,
		retryInterval: 500 * time.Millisecond,
	}
}

func (r *ServiceReconciler) ReconcileOne(ctx context.Context, workload *store.ClaimedWorkload) error {
	r.logger.Info("Starting reconciliation for service release",
		"intent_id", workload.IntentID,
		"release_id", workload.ReleaseID,
		"app_slug", workload.ApplicationSlug,
		"port", workload.Port,
		"health_path", workload.HealthPath,
		"lease_epoch", workload.LeaseEpoch,
	)

	// 1. Deploy physical workload / ingress
	resUID, err := r.runner.Deploy(ctx, workload)
	if err != nil {
		reason := fmt.Sprintf("workload deployment failed: %v", err)
		r.logger.Error(reason, "release_id", workload.ReleaseID)
		_ = r.runner.Teardown(ctx, workload)
		if markErr := r.store.MarkReleaseFailed(ctx, workload.IntentID, workload.ReleaseID, reason, workload.LeaseEpoch); markErr != nil {
			r.logger.Error("Failed to mark release failed", "error", markErr)
		}
		return err
	}

	// 2. Perform readiness probing with bounded retries
	var lastFailureReason string
	isReady := false

	for attempt := 1; attempt <= r.maxRetries; attempt++ {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		ready, failureReason, probeErr := r.runner.CheckReadiness(ctx, workload)
		if probeErr != nil {
			lastFailureReason = fmt.Sprintf("probe error: %v", probeErr)
		} else if ready {
			isReady = true
			break
		} else {
			lastFailureReason = failureReason
		}

		if attempt < r.maxRetries {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(r.retryInterval):
			}
		}
	}

	// 3. Update logical state in store based on outcome
	if isReady {
		r.logger.Info("Service release passed readiness probe. Marking HEALTHY.",
			"release_id", workload.ReleaseID,
			"app_id", workload.ApplicationID,
			"lease_epoch", workload.LeaseEpoch,
		)
		if err := r.store.MarkReleaseHealthy(ctx, workload.IntentID, workload.ReleaseID, workload.ApplicationID, resUID, workload.LeaseEpoch); err != nil {
			r.logger.Error("Failed to mark release healthy in database", "error", err)
			return err
		}

		// Tear down all previous generations so older deployments, services, and ingresses do not leak
		for gen := 1; gen < workload.TargetGeneration; gen++ {
			prevWorkload := *workload
			prevWorkload.TargetGeneration = gen
			prevWorkload.DeterministicResourceName = fmt.Sprintf("hc-svc-%s-%d", workload.ApplicationSlug, gen)
			if tdErr := r.runner.Teardown(ctx, &prevWorkload); tdErr != nil {
				r.logger.Warn("Failed to teardown older generation workload", "generation", gen, "error", tdErr)
			}
		}
		return nil
	}

	// Invalid readiness detected - surface to user visibly
	r.logger.Warn("Service release failed readiness probe. Marking DEPLOY_FAILED.",
		"release_id", workload.ReleaseID,
		"reason", lastFailureReason,
		"lease_epoch", workload.LeaseEpoch,
	)
	_ = r.runner.Teardown(ctx, workload)
	if err := r.store.MarkReleaseFailed(ctx, workload.IntentID, workload.ReleaseID, lastFailureReason, workload.LeaseEpoch); err != nil {
		r.logger.Error("Failed to mark release failed in database", "error", err)
		return err
	}

	return nil
}
