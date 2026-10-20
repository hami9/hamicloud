package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/bus"
	"github.com/hami9/hamicloud/runtime/internal/config"
	"github.com/hami9/hamicloud/runtime/internal/executor"
	"github.com/hami9/hamicloud/runtime/internal/health"
	"github.com/hami9/hamicloud/runtime/internal/reconciler"
	"github.com/hami9/hamicloud/runtime/internal/store"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
	slog.SetDefault(logger)

	logger.Info("Starting HamiCloud Execution Worker process", "version", "0.1.0")

	cfg, err := config.LoadFromEnv()
	if err != nil {
		logger.Error("Failed to load configuration", "error", err)
		os.Exit(1)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	pgStore, err := store.NewPostgresStore(ctx, cfg.DatabaseURL)
	if err != nil {
		logger.Error("Failed to initialize PostgreSQL store", "error", err)
		os.Exit(1)
	}

	var runner reconciler.WorkloadRunner
	var jobRunner reconciler.JobTaskRunner

	kubeClient, kubeErr := reconciler.BuildKubeClient(cfg.KubeconfigPath)
	if kubeErr == nil {
		logger.Info("Connected to Kubernetes cluster", "namespace", cfg.KubeNamespace, "ingress_domain", cfg.IngressDomain)
		runner = reconciler.NewKubeWorkloadRunner(kubeClient, cfg.KubeNamespace, cfg.IngressDomain)
		jobRunner, err = reconciler.NewKubeJobTaskRunner(kubeClient, cfg.KubeNamespace, cfg.ArtifactsDir)
		if err != nil {
			logger.Error("Failed to initialize Kubernetes job runner", "error", err)
			os.Exit(1)
		}
	} else {
		if cfg.Environment != "development" {
			logger.Error("Failed to initialize Kubernetes client in non-development environment", "error", kubeErr)
			os.Exit(1)
		}
		logger.Warn("Kubernetes cluster unavailable; falling back to development-only HTTPProbeRunner and LocalProcessJobRunner", "warning", kubeErr)
		runner, err = reconciler.NewHTTPProbeRunner(cfg.Environment, "127.0.0.1")
		if err != nil {
			logger.Error("Failed to initialize workload runner", "error", err)
			os.Exit(1)
		}
		jobRunner, err = reconciler.NewLocalProcessJobRunner(cfg.Environment, cfg.ArtifactsDir)
		if err != nil {
			logger.Error("Failed to initialize job runner", "error", err)
			os.Exit(1)
		}
	}

	rec := reconciler.NewServiceReconciler(pgStore, runner, logger)
	if kubeErr == nil {
		rec.SetRetryPolicy(30, 1*time.Second)
	}
	jobRec := reconciler.NewJobReconciler(pgStore, jobRunner, logger)
	buildRunner := reconciler.NewIsolatedBuildKitRunner("", "")
	buildRec := reconciler.NewBuildReconciler(pgStore, buildRunner, logger)

	exec := executor.NewExecutor(pgStore, rec, cfg.WorkerID, cfg.LeaseDuration, logger)
	if len(cfg.WorkspaceIDs) > 0 {
		exec.SetWorkspaceFilter(cfg.WorkspaceIDs...)
	}
	exec.SetJobReconciler(jobRec)
	exec.SetBuildReconciler(buildRec)

	var reconciliationWG sync.WaitGroup
	exec.SetWaitGroup(&reconciliationWG)

	// Liveness staleness window: 30s (exceeds 3x poll interval of 2s while allowing headroom for long reconciliations)
	healthSrv := health.NewServer(cfg.HealthAddr, 30*time.Second, pgStore.Ping, logger)
	exec.SetOnTick(healthSrv.RecordTick)

	if err := healthSrv.Start(); err != nil {
		logger.Error("Failed to start health HTTP server", "addr", cfg.HealthAddr, "error", err)
		os.Exit(1)
	}

	logger.Info("Executor initialized with configuration",
		"environment", cfg.Environment,
		"lease_duration", cfg.LeaseDuration,
		"worker_id", cfg.WorkerID,
		"run_once", cfg.RunOnce,
		"workspace_ids", cfg.WorkspaceIDs,
		"health_addr", cfg.HealthAddr,
		"shutdown_timeout", cfg.ShutdownTimeout,
	)

	if cfg.RunOnce {
		logger.Info("Executing single executor reconciliation pass (RUN_ONCE)")
		processed, err := exec.RunOnce(ctx)
		if err != nil {
			logger.Error("Executor RunOnce failed", "error", err)
			_ = healthSrv.Shutdown(context.Background())
			pgStore.Close()
			os.Exit(1)
		}
		logger.Info("Executor RunOnce completed successfully", "workload_processed", processed)
		_ = healthSrv.Shutdown(context.Background())
		pgStore.Close()
		return
	}

	// Start NATS event listener for immediate wake-ups on workload events
	listener := bus.NewEventListener(cfg.NATSURL, []string{
		"job.>",
		"app.>",
		"workload.>",
	}, logger)
	listener.Register(exec)

	go func() {
		if err := listener.Start(ctx); err != nil && err != context.Canceled {
			logger.Warn("Executor event listener stopped", "error", err)
		}
	}()

	// Run executor in background goroutine
	go func() {
		pollInterval := 2 * time.Second
		if err := exec.Start(ctx, pollInterval); err != nil && err != context.Canceled {
			logger.Error("Executor loop stopped with error", "error", err)
		}
	}()

	<-ctx.Done()
	logger.Info("Shutdown signal received, initiating graceful executor shutdown...")

	// 1. Mark health server as shutting down (causes /readyz to return 503)
	healthSrv.SetShuttingDown()

	// 2. Stop claiming new work
	exec.Stop()

	// 3. Wait for in-flight reconciliation goroutines via sync.WaitGroup with bounded timeout
	waitCh := make(chan struct{})
	go func() {
		reconciliationWG.Wait()
		close(waitCh)
	}()

	select {
	case <-waitCh:
		logger.Info("All in-flight workload reconciliations completed successfully")
	case <-time.After(cfg.ShutdownTimeout):
		logger.Warn("Shutdown timeout reached while waiting for in-flight reconciliations", "timeout", cfg.ShutdownTimeout)
	}

	// 4. Close bus and database pool (in-flight terminal state writes complete via detached contexts)
	listener.Close()
	pgStore.Close()

	// 5. Shutdown health HTTP server gracefully
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := healthSrv.Shutdown(shutdownCtx); err != nil {
		logger.Warn("Error shutting down health server", "error", err)
	}

	logger.Info("HamiCloud Execution Worker shutdown complete.")
	fmt.Println("Executor shutdown complete.")
}
