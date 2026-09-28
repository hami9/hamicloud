package executor

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type Store interface {
	ClaimNextServiceRelease(ctx context.Context, workerID string, leaseDuration time.Duration) (*store.ClaimedWorkload, error)
	ClaimNextJobAttempt(ctx context.Context, workerID string, leaseDuration time.Duration) (*store.ClaimedJobWorkload, error)
	RenewLease(ctx context.Context, intentID string, currentEpoch int, extension time.Duration) error
}

type Reconciler interface {
	ReconcileOne(ctx context.Context, workload *store.ClaimedWorkload) error
}

type JobReconciler interface {
	ReconcileJob(ctx context.Context, workload *store.ClaimedJobWorkload) error
}

type Executor struct {
	store         Store
	reconciler    Reconciler
	jobReconciler JobReconciler
	workerID      string
	leaseDuration time.Duration
	logger        *slog.Logger
	wakeCh        chan struct{}
}

func NewExecutor(
	st Store,
	rec Reconciler,
	workerID string,
	leaseDuration time.Duration,
	logger *slog.Logger,
) *Executor {
	if logger == nil {
		logger = slog.Default()
	}
	return &Executor{
		store:         st,
		reconciler:    rec,
		workerID:      workerID,
		leaseDuration: leaseDuration,
		logger:        logger,
		wakeCh:        make(chan struct{}, 1),
	}
}

// Wake non-blockingly signals the executor to immediately perform a work check.
func (e *Executor) Wake() {
	select {
	case e.wakeCh <- struct{}{}:
	default:
	}
}

// SetJobReconciler configures an optional JobReconciler for processing job attempts.
func (e *Executor) SetJobReconciler(jr JobReconciler) {
	e.jobReconciler = jr
}

func (e *Executor) startLeaseHeartbeat(parentCtx context.Context, intentID string, leaseEpoch int) (context.Context, context.CancelFunc) {
	workloadCtx, cancelWorkload := context.WithCancel(parentCtx)
	renewCtx, cancelRenew := context.WithCancel(parentCtx)

	renewInterval := e.leaseDuration / 3
	if renewInterval < 10*time.Millisecond {
		renewInterval = 10 * time.Millisecond
	}

	go func() {
		ticker := time.NewTicker(renewInterval)
		defer ticker.Stop()
		for {
			select {
			case <-renewCtx.Done():
				return
			case <-ticker.C:
				if err := e.store.RenewLease(renewCtx, intentID, leaseEpoch, e.leaseDuration); err != nil {
					e.logger.Warn("Failed to renew lease for claimed intent, cancelling workload context",
						"intent_id", intentID,
						"lease_epoch", leaseEpoch,
						"error", err,
					)
					cancelWorkload()
					return
				}
				e.logger.Debug("Successfully renewed lease for claimed intent",
					"intent_id", intentID,
					"lease_epoch", leaseEpoch,
				)
			}
		}
	}()

	cleanup := func() {
		cancelRenew()
		cancelWorkload()
	}
	return workloadCtx, cleanup
}

// RunOnce attempts to claim and reconcile a pending job attempt or service release intent.
// Returns true if an intent was claimed and processed, false if no work was available.
func (e *Executor) RunOnce(ctx context.Context) (bool, error) {
	// 1. Try to claim and reconcile pending job attempt
	if e.jobReconciler != nil {
		jobWorkload, err := e.store.ClaimNextJobAttempt(ctx, e.workerID, e.leaseDuration)
		if err != nil {
			return false, fmt.Errorf("claim next job attempt: %w", err)
		}
		if jobWorkload != nil {
			e.logger.Info("Claimed job attempt intent",
				"intent_id", jobWorkload.IntentID,
				"job_id", jobWorkload.JobID,
				"job_name", jobWorkload.JobName,
				"attempt_number", jobWorkload.AttemptNumber,
				"worker_id", e.workerID,
				"lease_epoch", jobWorkload.LeaseEpoch,
			)

			workloadCtx, cleanup := e.startLeaseHeartbeat(ctx, jobWorkload.IntentID, jobWorkload.LeaseEpoch)
			defer cleanup()

			if err := e.jobReconciler.ReconcileJob(workloadCtx, jobWorkload); err != nil {
				e.logger.Error("Reconcile error for job attempt",
					"intent_id", jobWorkload.IntentID,
					"job_id", jobWorkload.JobID,
					"error", err,
				)
				return true, err
			}
			return true, nil
		}
	}

	// 2. Try to claim and reconcile pending service release
	workload, err := e.store.ClaimNextServiceRelease(ctx, e.workerID, e.leaseDuration)
	if err != nil {
		return false, fmt.Errorf("claim next service release: %w", err)
	}
	if workload == nil {
		return false, nil // No pending intents
	}

	e.logger.Info("Claimed service release intent",
		"intent_id", workload.IntentID,
		"release_id", workload.ReleaseID,
		"app_slug", workload.ApplicationSlug,
		"worker_id", e.workerID,
		"lease_epoch", workload.LeaseEpoch,
	)

	workloadCtx, cleanup := e.startLeaseHeartbeat(ctx, workload.IntentID, workload.LeaseEpoch)
	defer cleanup()

	if err := e.reconciler.ReconcileOne(workloadCtx, workload); err != nil {
		e.logger.Error("Reconcile error for service release",
			"intent_id", workload.IntentID,
			"release_id", workload.ReleaseID,
			"error", err,
		)
		return true, err
	}

	return true, nil
}

// Start runs the executor loop polling for work.
func (e *Executor) Start(ctx context.Context, idlePollInterval time.Duration) error {
	e.logger.Info("Starting Executor control loop",
		"worker_id", e.workerID,
		"lease_duration", e.leaseDuration,
		"poll_interval", idlePollInterval,
	)

	for {
		select {
		case <-ctx.Done():
			e.logger.Info("Executor control loop stopping")
			return ctx.Err()
		default:
		}

		processed, err := e.RunOnce(ctx)
		if err != nil {
			e.logger.Warn("Executor step encountered error", "error", err)
		}

		if !processed {
			// Back off when no pending work exists
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(idlePollInterval):
			case <-e.wakeCh:
				e.logger.Debug("Executor awakened by event notification")
			}
		}
	}
}
