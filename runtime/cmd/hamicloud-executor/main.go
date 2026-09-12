package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/hami9/hamicloud/runtime/internal/config"
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

	logger.Info("Executor initialized with configuration",
		"environment", cfg.Environment,
		"lease_duration", cfg.LeaseDuration,
		"worker_id", cfg.WorkerID,
	)

	// Keep alive until shutdown signal received
	<-ctx.Done()
	logger.Info("Shutting down HamiCloud Execution Worker gracefully...")
	fmt.Println("Executor shutdown complete.")
}
