package health

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"sync/atomic"
	"time"
)

// PingFunc verifies database connectivity with a given context timeout.
type PingFunc func(ctx context.Context) error

// Server provides /healthz (liveness) and /readyz (readiness) HTTP endpoints.
type Server struct {
	addr            string
	stalenessWindow time.Duration
	pingDB          PingFunc
	logger          *slog.Logger
	listener        net.Listener
	httpServer      *http.Server
	lastTick        atomic.Int64 // UnixNano
	shuttingDown    atomic.Bool
}

// NewServer initializes a health Server with an address, staleness window, and database ping callback.
func NewServer(addr string, stalenessWindow time.Duration, pingDB PingFunc, logger *slog.Logger) *Server {
	if addr == "" {
		addr = ":8081"
	}
	if stalenessWindow <= 0 {
		stalenessWindow = 90 * time.Second
	}
	if logger == nil {
		logger = slog.Default()
	}

	s := &Server{
		addr:            addr,
		stalenessWindow: stalenessWindow,
		pingDB:          pingDB,
		logger:          logger,
	}
	s.lastTick.Store(time.Now().UnixNano())
	return s
}

// RecordTick updates the atomic timestamp of the latest loop execution.
func (s *Server) RecordTick() {
	s.lastTick.Store(time.Now().UnixNano())
}

// SetLastTick sets a specific tick timestamp, useful for testing staleness transitions.
func (s *Server) SetLastTick(t time.Time) {
	s.lastTick.Store(t.UnixNano())
}

// LastTick returns the time of the most recent recorded tick.
func (s *Server) LastTick() time.Time {
	return time.Unix(0, s.lastTick.Load())
}

// SetShuttingDown marks the server as shutting down, causing /readyz to return 503.
func (s *Server) SetShuttingDown() {
	s.shuttingDown.Store(true)
}

// IsShuttingDown reports whether graceful shutdown has begun.
func (s *Server) IsShuttingDown() bool {
	return s.shuttingDown.Load()
}

// Handler returns the HTTP handler registered with /healthz and /readyz.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/readyz", s.handleReadyz)
	return mux
}

// handleHealthz handles Kubernetes liveness probes.
// Returns 200 OK while the main control loop has ticked within the staleness window;
// returns 503 Service Unavailable if loop execution has stalled.
func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	last := time.Unix(0, s.lastTick.Load())
	elapsed := time.Since(last)
	if elapsed > s.stalenessWindow {
		s.logger.Warn("Liveness probe failed: main loop stale",
			"elapsed", elapsed.String(),
			"staleness_window", s.stalenessWindow.String(),
		)
		http.Error(w, fmt.Sprintf("stale: last tick was %v ago (threshold: %v)", elapsed.Round(time.Millisecond), s.stalenessWindow), http.StatusServiceUnavailable)
		return
	}

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok\n"))
}

// handleReadyz handles Kubernetes readiness probes.
// Returns 200 OK only if the service is not shutting down and database ping succeeds within 2s.
// Returns 503 Service Unavailable during shutdown or on database connectivity failure.
func (s *Server) handleReadyz(w http.ResponseWriter, r *http.Request) {
	if s.shuttingDown.Load() {
		http.Error(w, "shutting down", http.StatusServiceUnavailable)
		return
	}

	if s.pingDB != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()

		if err := s.pingDB(ctx); err != nil {
			s.logger.Warn("Readiness probe failed: database ping error", "error", err)
			http.Error(w, fmt.Sprintf("database unavailable: %v", err), http.StatusServiceUnavailable)
			return
		}
	}

	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ready\n"))
}

// Start binds to the configured network address and launches the HTTP server in a background goroutine.
func (s *Server) Start() error {
	ln, err := net.Listen("tcp", s.addr)
	if err != nil {
		return fmt.Errorf("health server failed to listen on %s: %w", s.addr, err)
	}
	s.listener = ln
	s.httpServer = &http.Server{
		Handler: s.Handler(),
	}

	s.logger.Info("Started health HTTP server", "addr", ln.Addr().String())

	go func() {
		if err := s.httpServer.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			s.logger.Error("Health HTTP server stopped with error", "error", err)
		}
	}()

	return nil
}

// Shutdown initiates graceful termination of the health server.
func (s *Server) Shutdown(ctx context.Context) error {
	s.SetShuttingDown()
	if s.httpServer == nil {
		return nil
	}
	return s.httpServer.Shutdown(ctx)
}
