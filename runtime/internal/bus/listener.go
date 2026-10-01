package bus

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// Wakeable represents a control-plane worker that can be woken up by an incoming event notification.
type Wakeable interface {
	Wake()
}

// EventListener subscribes to Core NATS subjects to trigger immediate wakeup
// passes in registered scheduler and executor loops (ADR-0002).
//
// Note: This uses Core NATS subject subscriptions for low-latency notifications, not JetStream
// consumer state. Event durability, idempotency, and recovery guarantees are fully backed
// by the transactional outbox and state machines in PostgreSQL.
//
// If NATS is unreachable or temporarily down at startup or during execution, EventListener
// retries connection with backoff and degrades gracefully, allowing background workers to
// continue operating on their periodic database polling fallback.
type EventListener struct {
	natsURL  string
	subjects []string
	logger   *slog.Logger
	targets  []Wakeable

	mu   sync.Mutex
	nc   *nats.Conn
	subs []*nats.Subscription
}

// NewEventListener constructs an EventListener for the given subjects and NATS endpoint.
func NewEventListener(natsURL string, subjects []string, logger *slog.Logger) *EventListener {
	if logger == nil {
		logger = slog.Default()
	}
	return &EventListener{
		natsURL:  natsURL,
		subjects: subjects,
		logger:   logger,
	}
}

// Register registers a Wakeable worker (such as Scheduler or Executor) to be notified on incoming events.
func (l *EventListener) Register(target Wakeable) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.targets = append(l.targets, target)
}

// NotifyTargets signals all registered Wakeable workers to perform an immediate work pass.
func (l *EventListener) NotifyTargets() {
	l.mu.Lock()
	targets := make([]Wakeable, len(l.targets))
	copy(targets, l.targets)
	l.mu.Unlock()

	for _, t := range targets {
		t.Wake()
	}
}

// Start connects to NATS and subscribes to event subjects. If NATS is unavailable at startup,
// it retries connecting in the background with backoff while allowing control processes to rely on periodic polling.
func (l *EventListener) Start(ctx context.Context) error {
	opts := []nats.Option{
		nats.Name("hamicloud-runtime-listener"),
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2 * time.Second),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			l.logger.Warn("Disconnected from NATS broker; operating on periodic polling fallback", "error", err)
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			l.logger.Info("Reconnected to NATS broker; event-driven wakeups active")
		}),
	}

	retryInterval := 100 * time.Millisecond
	maxRetryInterval := 2 * time.Second

	var nc *nats.Conn
	for {
		var err error
		nc, err = nats.Connect(l.natsURL, opts...)
		if err == nil {
			break
		}
		l.logger.Warn("NATS broker unavailable at startup; runtime proceeding in periodic polling mode (retrying)",
			"nats_url", l.natsURL,
			"error", err,
		)

		select {
		case <-ctx.Done():
			return nil
		case <-time.After(retryInterval):
			retryInterval *= 2
			if retryInterval > maxRetryInterval {
				retryInterval = maxRetryInterval
			}
		}
	}

	l.mu.Lock()
	l.nc = nc
	l.mu.Unlock()

	l.logger.Info("Connected to NATS broker for event-driven dispatch", "nats_url", l.natsURL)

	for _, subj := range l.subjects {
		subject := subj
		sub, err := nc.Subscribe(subject, func(msg *nats.Msg) {
			l.logger.Debug("Received NATS event notification", "subject", msg.Subject)
			l.NotifyTargets()
			_ = msg.Ack()
		})
		if err != nil {
			l.logger.Warn("Failed to subscribe to NATS subject", "subject", subject, "error", err)
			continue
		}
		l.mu.Lock()
		l.subs = append(l.subs, sub)
		l.mu.Unlock()
		l.logger.Info("Subscribed to NATS event notifications", "subject", subject)
	}

	<-ctx.Done()

	l.mu.Lock()
	defer l.mu.Unlock()
	for _, sub := range l.subs {
		_ = sub.Unsubscribe()
	}
	if l.nc != nil {
		l.nc.Close()
	}
	l.logger.Info("NATS event listener stopped")
	return nil
}
