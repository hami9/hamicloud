package reconciler

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/store"
)

type JobStore interface {
	MarkJobAttemptRunning(ctx context.Context, intentID, attemptID, jobID string, leaseEpoch int) error
	MarkJobAttemptSucceeded(ctx context.Context, intentID, attemptID, jobID, resourceUID string, leaseEpoch int) error
	MarkJobAttemptFailed(ctx context.Context, intentID, attemptID, jobID, reason string, exitCode int, leaseEpoch int, shouldRetry bool) error
	MarkJobAttemptCancelled(ctx context.Context, intentID, attemptID, jobID string, leaseEpoch int) error
	IsJobCancelRequested(ctx context.Context, jobID string) (bool, error)
}

type JobTaskRunner interface {
	RunJob(ctx context.Context, workload *store.ClaimedJobWorkload) (exitCode int, failureReason string, err error)
}

// LocalProcessJobRunner is a development-only runner that executes jobs as local host processes.
// It is NOT a security boundary, provides no container isolation, and MUST NEVER be used outside ENVIRONMENT=development.
type LocalProcessJobRunner struct {
	artifactsDir string
}

func NewLocalProcessJobRunner(env string, artifactsDir string) (*LocalProcessJobRunner, error) {
	if env != "development" {
		return nil, fmt.Errorf("LocalProcessJobRunner is forbidden when ENVIRONMENT=%q: local runners are development-only and require a real Kubernetes runner outside development", env)
	}
	if artifactsDir == "" {
		artifactsDir = os.Getenv("ARTIFACTS_DIR")
		if artifactsDir == "" {
			artifactsDir = filepath.Join("var", "artifacts")
		}
	}
	absDir, err := filepath.Abs(artifactsDir)
	if err != nil {
		return nil, fmt.Errorf("resolve artifacts dir: %w", err)
	}
	return &LocalProcessJobRunner{artifactsDir: absDir}, nil
}

func (r *LocalProcessJobRunner) RunJob(ctx context.Context, workload *store.ClaimedJobWorkload) (int, string, error) {
	if len(workload.CommandArgs) == 0 {
		return 1, "empty command_args: local process runner requires an executable command", errors.New("empty command_args")
	}

	cmd := exec.CommandContext(ctx, workload.CommandArgs[0], workload.CommandArgs[1:]...)

	// The job process gets an empty environment plus only the job's declared env_vars.
	// Never pass the executor host environment (e.g. RUNTIME_DATABASE_URL).
	env := make([]string, 0, len(workload.EnvVars))
	for k, v := range workload.EnvVars {
		env = append(env, fmt.Sprintf("%s=%s", k, v))
	}
	cmd.Env = env

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()

	// Persist artifact output on disk
	if workload.WorkspaceID != "" && workload.JobID != "" {
		artifactBase := r.artifactsDir
		if artifactBase == "" {
			artifactBase = os.Getenv("ARTIFACTS_DIR")
			if artifactBase == "" {
				artifactBase = filepath.Join("var", "artifacts")
			}
		}
		artifactDir := filepath.Join(artifactBase, workload.WorkspaceID, workload.JobID)
		_ = os.MkdirAll(artifactDir, 0755)
		var content []byte
		if stdout.Len() > 0 && stderr.Len() > 0 {
			content = append(stdout.Bytes(), []byte("\n--- STDERR ---\n")...)
			content = append(content, stderr.Bytes()...)
		} else if stdout.Len() > 0 {
			content = stdout.Bytes()
		} else {
			content = stderr.Bytes()
		}
		_ = os.WriteFile(filepath.Join(artifactDir, fmt.Sprintf("attempt-%d-output.txt", workload.AttemptNumber)), content, 0644)
		_ = os.WriteFile(filepath.Join(artifactDir, "output.txt"), content, 0644)
	}

	if err == nil {
		return 0, "", nil
	}

	// Check if context timed out or was cancelled
	if ctx.Err() != nil {
		return -1, "job execution cancelled or timed out", ctx.Err()
	}

	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		exitCode := exitErr.ExitCode()
		reason := stderr.String()
		if reason == "" {
			reason = fmt.Sprintf("process exited with status %d", exitCode)
		}
		return exitCode, reason, nil
	}

	return -1, fmt.Sprintf("failed to execute process: %v", err), err
}

type JobReconciler struct {
	store  JobStore
	runner JobTaskRunner
	logger *slog.Logger
}

func NewJobReconciler(st JobStore, runner JobTaskRunner, logger *slog.Logger) *JobReconciler {
	if logger == nil {
		logger = slog.Default()
	}
	if runner == nil {
		panic("NewJobReconciler: runner must not be nil; local runners require explicit development environment")
	}
	return &JobReconciler{
		store:  st,
		runner: runner,
		logger: logger,
	}
}

// ReconcileJob coordinates the execution, cancellation monitoring, and state finalization for a claimed job attempt.
func (r *JobReconciler) ReconcileJob(ctx context.Context, workload *store.ClaimedJobWorkload) error {
	r.logger.Info("Starting reconciliation for job attempt",
		"intent_id", workload.IntentID,
		"job_id", workload.JobID,
		"job_name", workload.JobName,
		"attempt_number", workload.AttemptNumber,
		"lease_epoch", workload.LeaseEpoch,
	)

	// 1. Initial check: was job cancelled before claim execution started?
	cancelled, err := r.store.IsJobCancelRequested(ctx, workload.JobID)
	if err != nil {
		r.logger.Warn("Failed to check if job is cancelled", "job_id", workload.JobID, "error", err)
	}
	if cancelled {
		r.logger.Info("Job cancellation requested before execution started. Marking CANCELLED.",
			"job_id", workload.JobID,
			"attempt_number", workload.AttemptNumber,
		)
		return r.store.MarkJobAttemptCancelled(ctx, workload.IntentID, workload.JobAttemptID, workload.JobID, workload.LeaseEpoch)
	}

	// 2. Transition job and attempt STARTING -> RUNNING when workload actually starts (fenced by lease epoch)
	if err := r.store.MarkJobAttemptRunning(ctx, workload.IntentID, workload.JobAttemptID, workload.JobID, workload.LeaseEpoch); err != nil {
		if isCancel, _ := r.store.IsJobCancelRequested(ctx, workload.JobID); isCancel {
			return r.store.MarkJobAttemptCancelled(ctx, workload.IntentID, workload.JobAttemptID, workload.JobID, workload.LeaseEpoch)
		}
		return fmt.Errorf("transition job to RUNNING: %w", err)
	}

	// 3. Setup execution context with timeout
	timeout := time.Duration(workload.TimeoutSeconds) * time.Second
	if timeout <= 0 {
		timeout = 600 * time.Second
	}
	runCtx, cancelRun := context.WithTimeout(ctx, timeout)
	defer cancelRun()

	// Cancellation poller: watch for CANCEL_REQUESTED while job is running
	stopPoller := make(chan struct{})
	defer close(stopPoller)

	go func() {
		ticker := time.NewTicker(200 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-stopPoller:
				return
			case <-runCtx.Done():
				return
			case <-ticker.C:
				if isCancel, _ := r.store.IsJobCancelRequested(ctx, workload.JobID); isCancel {
					r.logger.Info("Active job detected CANCEL_REQUESTED signal. Aborting job execution.",
						"job_id", workload.JobID,
						"attempt_number", workload.AttemptNumber,
					)
					cancelRun()
					return
				}
			}
		}
	}()

	// 3. Run the workload
	exitCode, failureReason, runErr := r.runner.RunJob(runCtx, workload)

	// 4. Handle Success
	// Per Decision D4: If workload process completed with exit code 0, the attempt record retains
	// its true outcome (SUCCEEDED, exit_code: 0). If cancellation was requested, MarkJobAttemptSucceeded
	// atomically transitions the logical job to CANCELLED instead of SUCCEEDED.
	if exitCode == 0 && runErr == nil {
		prefix := workload.JobID
		if len(prefix) > 8 {
			prefix = prefix[:8]
		}
		resourceUID := fmt.Sprintf("res-job-%s-%d", prefix, workload.AttemptNumber)
		r.logger.Info("Job attempt SUCCEEDED with exit code 0",
			"job_id", workload.JobID,
			"attempt_number", workload.AttemptNumber,
			"resource_uid", resourceUID,
		)
		return r.store.MarkJobAttemptSucceeded(ctx, workload.IntentID, workload.JobAttemptID, workload.JobID, resourceUID, workload.LeaseEpoch)
	}

	// 5. Check if job was cancelled during execution (interrupted or failed workload)
	if isCancel, _ := r.store.IsJobCancelRequested(ctx, workload.JobID); isCancel {
		r.logger.Info("Job confirmed cancelled during execution. Marking CANCELLED.",
			"job_id", workload.JobID,
			"attempt_number", workload.AttemptNumber,
		)
		return r.store.MarkJobAttemptCancelled(ctx, workload.IntentID, workload.JobAttemptID, workload.JobID, workload.LeaseEpoch)
	}

	// 6. Handle Failure (transient vs terminal)
	// Retry policy per ADR-0003: If attempt_number <= max_retries, transition to RETRY_WAIT; else FAILED.
	shouldRetry := workload.AttemptNumber <= workload.MaxRetries
	if failureReason == "" {
		if runErr != nil {
			failureReason = runErr.Error()
		} else {
			failureReason = fmt.Sprintf("process exited with status %d", exitCode)
		}
	}

	r.logger.Warn("Job attempt failed",
		"job_id", workload.JobID,
		"attempt_number", workload.AttemptNumber,
		"exit_code", exitCode,
		"should_retry", shouldRetry,
		"reason", failureReason,
	)

	return r.store.MarkJobAttemptFailed(ctx, workload.IntentID, workload.JobAttemptID, workload.JobID, failureReason, exitCode, workload.LeaseEpoch, shouldRetry)
}
