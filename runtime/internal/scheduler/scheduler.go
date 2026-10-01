package scheduler

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type Store interface {
	ScanUnadmittedReleases(ctx context.Context, limit int) ([]store.UnadmittedRelease, error)
	CreateServiceReleaseIntent(ctx context.Context, rel store.UnadmittedRelease) (*store.ExecutionIntent, error)
	ScanUnadmittedJobs(ctx context.Context, limit int) ([]store.UnadmittedJob, error)
	CreateJobAttemptIntent(ctx context.Context, job store.UnadmittedJob) (*store.ExecutionIntent, error)
	RequeueRetryWaitJobs(ctx context.Context, baseBackoff time.Duration, limit int) (int, error)
	RecoverExpiredJobIntents(ctx context.Context, optionalDeleter ...store.JobDeleter) (int, error)
}

type Scheduler struct {
	store      Store
	jobDeleter store.JobDeleter
	logger     *slog.Logger
	wakeCh     chan struct{}
}

func NewScheduler(st Store, jobDeleter store.JobDeleter, logger *slog.Logger) *Scheduler {
	if logger == nil {
		logger = slog.Default()
	}
	return &Scheduler{
		store:      st,
		jobDeleter: jobDeleter,
		logger:     logger,
		wakeCh:     make(chan struct{}, 1),
	}
}

// Wake non-blockingly signals the scheduler to perform an immediate admission pass.
func (s *Scheduler) Wake() {
	select {
	case s.wakeCh <- struct{}{}:
	default:
	}
}

// RunOnce scans for releases and jobs awaiting execution intents and admits them.
func (s *Scheduler) RunOnce(ctx context.Context) (int, error) {
	// 0. Recover any expired job attempt intents from crashed executors
	recovered, err := s.store.RecoverExpiredJobIntents(ctx, s.jobDeleter)
	if err != nil {
		s.logger.Warn("Failed to recover expired job intents", "error", err)
	} else if recovered > 0 {
		s.logger.Info("Recovered expired job intents from crashed workers", "count", recovered)
	}

	// 1. Requeue any jobs in RETRY_WAIT whose exponential backoff has elapsed
	requeued, err := s.store.RequeueRetryWaitJobs(ctx, 5*time.Second, 50)
	if err != nil {
		s.logger.Warn("Failed to requeue retry_wait jobs", "error", err)
	} else if requeued > 0 {
		s.logger.Info("Requeued retry_wait jobs back to QUEUED for next attempt", "count", requeued)
	}

	totalAdmitted := 0

	// 2. Scan and admit service releases
	unadmittedReleases, err := s.store.ScanUnadmittedReleases(ctx, 50)
	if err != nil {
		return totalAdmitted, fmt.Errorf("scan unadmitted releases: %w", err)
	}
	for _, rel := range unadmittedReleases {
		intent, err := s.store.CreateServiceReleaseIntent(ctx, rel)
		if err != nil {
			s.logger.Error("Failed to create execution intent for release",
				"release_id", rel.ReleaseID,
				"app_slug", rel.ApplicationSlug,
				"error", err,
			)
			continue
		}
		if intent != nil {
			s.logger.Info("Admitted release and created ExecutionIntent",
				"intent_id", intent.ID,
				"release_id", rel.ReleaseID,
				"resource_name", intent.DeterministicResourceName,
				"generation", intent.TargetGeneration,
			)
			totalAdmitted++
		}
	}

	// 3. Scan and admit jobs
	unadmittedJobs, err := s.store.ScanUnadmittedJobs(ctx, 50)
	if err != nil {
		return totalAdmitted, fmt.Errorf("scan unadmitted jobs: %w", err)
	}
	for _, job := range unadmittedJobs {
		intent, err := s.store.CreateJobAttemptIntent(ctx, job)
		if err != nil {
			s.logger.Error("Failed to create execution intent for job",
				"job_id", job.JobID,
				"job_name", job.Name,
				"error", err,
			)
			continue
		}
		if intent != nil {
			s.logger.Info("Admitted job and created ExecutionIntent",
				"intent_id", intent.ID,
				"job_id", job.JobID,
				"job_attempt_id", intent.JobAttemptID,
				"resource_name", intent.DeterministicResourceName,
			)
			totalAdmitted++
		}
	}

	return totalAdmitted, nil
}

// Start runs the admission scheduler loop until context is cancelled.
func (s *Scheduler) Start(ctx context.Context, period time.Duration) error {
	s.logger.Info("Starting Scheduler admission loop", "period", period)
	ticker := time.NewTicker(period)
	defer ticker.Stop()

	// Initial scan immediately
	if _, err := s.RunOnce(ctx); err != nil {
		s.logger.Warn("Initial scheduler admission run reported error", "error", err)
	}

	for {
		select {
		case <-ctx.Done():
			s.logger.Info("Scheduler admission loop stopping")
			return ctx.Err()
		case <-ticker.C:
			if _, err := s.RunOnce(ctx); err != nil {
				s.logger.Warn("Scheduler admission cycle reported error", "error", err)
			}
		case <-s.wakeCh:
			s.logger.Info("Scheduler triggered by event notification")
			if _, err := s.RunOnce(ctx); err != nil {
				s.logger.Warn("Scheduler event admission pass reported error", "error", err)
			}
		}
	}
}
