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
	"github.com/hami9/hamicloud/runtime/internal/health"
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

	var jobDeleter store.JobDeleter
	kubeClient, kubeErr := reconciler.BuildKubeClient(cfg.KubeconfigPath)
	if kubeErr == nil {
		logger.Info("Connected scheduler to Kubernetes cluster for job recovery", "namespace", cfg.KubeNamespace)
		jobDeleter = reconciler.NewKubeJobDeleter(kubeClient, cfg.KubeNamespace)
	} else {
		logger.Warn("Kubernetes cluster unavailable for scheduler; falling back to NoopJobDeleter", "warning", kubeErr)
		jobDeleter = &reconciler.NoopJobDeleter{}
	}

	sched := scheduler.NewScheduler(pgStore, jobDeleter, logger)

	var admissionWG sync.WaitGroup
	sched.SetWaitGroup(&admissionWG)

	// Liveness staleness window: 3x poll interval (default 90s)
	stalenessWindow := 3 * cfg.ReconciliationPeriod
	if stalenessWindow <= 0 {
		stalenessWindow = 90 * time.Second
	}
	healthSrv := health.NewServer(cfg.HealthAddr, stalenessWindow, pgStore.Ping, logger)
	sched.SetOnTick(healthSrv.RecordTick)

	if err := healthSrv.Start(); err != nil {
		logger.Error("Failed to start health HTTP server", "addr", cfg.HealthAddr, "error", err)
		os.Exit(1)
	}

	logger.Info("Scheduler initialized with configuration",
		"environment", cfg.Environment,
		"reconciliation_period", cfg.ReconciliationPeriod,
		"worker_id", cfg.WorkerID,
		"run_once", cfg.RunOnce,
		"health_addr", cfg.HealthAddr,
		"shutdown_timeout", cfg.ShutdownTimeout,
	)

	if cfg.RunOnce {
		logger.Info("Executing single scheduler admission pass (RUN_ONCE)")
		count, err := sched.RunOnce(ctx)
		if err != nil {
			logger.Error("Scheduler RunOnce failed", "error", err)
			_ = healthSrv.Shutdown(context.Background())
			pgStore.Close()
			os.Exit(1)
		}
		logger.Info("Scheduler RunOnce completed successfully", "admitted_count", count)
		_ = healthSrv.Shutdown(context.Background())
		pgStore.Close()
		return
	}

	// Start NATS event listener for immediate wake-ups on admission events
	listener := bus.NewEventListener(cfg.NATSURL, []string{
		"job.submitted.v1",
		"app.deployment.requested.v1",
		"app.build.completed.v1",
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
	logger.Info("Shutdown signal received, initiating graceful scheduler shutdown...")

	// 1. Mark health server as shutting down (causes /readyz to return 503)
	healthSrv.SetShuttingDown()

	// 2. Stop admitting new work
	sched.Stop()

	// 3. Wait for in-flight admission passes via sync.WaitGroup with bounded timeout
	waitCh := make(chan struct{})
	go func() {
		admissionWG.Wait()
		close(waitCh)
	}()

	select {
	case <-waitCh:
		logger.Info("All in-flight scheduler admission passes completed successfully")
	case <-time.After(cfg.ShutdownTimeout):
		logger.Warn("Shutdown timeout reached while waiting for in-flight admission passes", "timeout", cfg.ShutdownTimeout)
	}

	// 4. Close bus and database pool
	listener.Close()
	pgStore.Close()

	// 5. Shutdown health HTTP server gracefully
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := healthSrv.Shutdown(shutdownCtx); err != nil {
		logger.Warn("Error shutting down health server", "error", err)
	}

	logger.Info("HamiCloud Scheduler shutdown complete.")
	fmt.Println("Scheduler shutdown complete.")
}
