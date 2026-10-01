package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/hami9/hamicloud/runtime/internal/bus"
	"github.com/hami9/hamicloud/runtime/internal/config"
	"github.com/hami9/hamicloud/runtime/internal/reconciler"
	"github.com/hami9/hamicloud/runtime/internal/scheduler"
	"github.com/hami9/hamicloud/runtime/internal/store"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	}))
	slog.SetDefault(logger)

	logger.Info("Starting HamiCloud Scheduler process", "version", "0.1.0")

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
	defer pgStore.Close()

	var jobDeleter store.JobDeleter
	kubeClient, kubeErr := reconciler.BuildKubeClient(cfg.KubeconfigPath)
	if kubeErr == nil {
		logger.Info("Connected scheduler to Kubernetes cluster for job recovery", "namespace", cfg.KubeNamespace)
		jobDeleter = reconciler.NewKubeJobDeleter(kubeClient, cfg.KubeNamespace)
	} else {
		if cfg.Environment != "development" {
			logger.Error("Failed to initialize Kubernetes client for scheduler in non-development environment", "error", kubeErr)
			os.Exit(1)
		}
		logger.Warn("Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter in development", "warning", kubeErr)
		jobDeleter = &reconciler.NoopJobDeleter{}
	}

	sched := scheduler.NewScheduler(pgStore, jobDeleter, logger)

	logger.Info("Scheduler initialized with configuration",
		"environment", cfg.Environment,
		"reconciliation_period", cfg.ReconciliationPeriod,
		"worker_id", cfg.WorkerID,
		"run_once", cfg.RunOnce,
	)

	if cfg.RunOnce {
		logger.Info("Executing single scheduler admission pass (RUN_ONCE)")
		count, err := sched.RunOnce(ctx)
		if err != nil {
			logger.Error("Scheduler RunOnce failed", "error", err)
			os.Exit(1)
		}
		logger.Info("Scheduler RunOnce completed successfully", "admitted_count", count)
		return
	}

	// Start NATS event listener for immediate wake-ups on admission events
	listener := bus.NewEventListener(cfg.NATSURL, []string{
		"job.submitted.v1",
		"app.deployment.requested.v1",
		"app.rollback.requested.v1",
	}, logger)
	listener.Register(sched)

	go func() {
		if err := listener.Start(ctx); err != nil && err != context.Canceled {
			logger.Warn("Scheduler event listener stopped", "error", err)
		}
	}()

	// Run scheduler in background goroutine
	go func() {
		if err := sched.Start(ctx, cfg.ReconciliationPeriod); err != nil && err != context.Canceled {
			logger.Error("Scheduler loop stopped with error", "error", err)
		}
	}()

	<-ctx.Done()
	logger.Info("Shutting down HamiCloud Scheduler gracefully...")
	fmt.Println("Scheduler shutdown complete.")
}
