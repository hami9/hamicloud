package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/hami9/hamicloud/runtime/internal/bus"
	"github.com/hami9/hamicloud/runtime/internal/config"
	"github.com/hami9/hamicloud/runtime/internal/executor"
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
	defer pgStore.Close()

	runner, err := reconciler.NewHTTPProbeRunner(cfg.Environment, "127.0.0.1")
	if err != nil {
		logger.Error("Failed to initialize workload runner", "error", err)
		os.Exit(1)
	}
	rec := reconciler.NewServiceReconciler(pgStore, runner, logger)

	jobRunner, err := reconciler.NewLocalProcessJobRunner(cfg.Environment, cfg.ArtifactsDir)
	if err != nil {
		logger.Error("Failed to initialize job runner", "error", err)
		os.Exit(1)
	}
	jobRec := reconciler.NewJobReconciler(pgStore, jobRunner, logger)

	exec := executor.NewExecutor(pgStore, rec, cfg.WorkerID, cfg.LeaseDuration, logger)
	exec.SetJobReconciler(jobRec)

	logger.Info("Executor initialized with configuration",
		"environment", cfg.Environment,
		"lease_duration", cfg.LeaseDuration,
		"worker_id", cfg.WorkerID,
		"run_once", cfg.RunOnce,
	)

	if cfg.RunOnce {
		logger.Info("Executing single executor reconciliation pass (RUN_ONCE)")
		processed, err := exec.RunOnce(ctx)
		if err != nil {
			logger.Error("Executor RunOnce failed", "error", err)
			os.Exit(1)
		}
		logger.Info("Executor RunOnce completed successfully", "workload_processed", processed)
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
	logger.Info("Shutting down HamiCloud Execution Worker gracefully...")
	fmt.Println("Executor shutdown complete.")
}
