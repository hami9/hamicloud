package bus

import (
	"context"
	"sync"
	"testing"
	"time"
)

type mockWakeable struct {
	mu        sync.Mutex
	wakeCalls int
}

func (m *mockWakeable) Wake() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.wakeCalls++
}

func (m *mockWakeable) getCalls() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.wakeCalls
}

func TestEventListener_NotifyTargets(t *testing.T) {
	w1 := &mockWakeable{}
	w2 := &mockWakeable{}

	l := NewEventListener("nats://127.0.0.1:4222", []string{"job.>", "app.>"}, nil)
	l.Register(w1)
	l.Register(w2)

	l.NotifyTargets()

	if w1.getCalls() != 1 {
		t.Errorf("expected 1 call on w1, got %d", w1.getCalls())
	}
	if w2.getCalls() != 1 {
		t.Errorf("expected 1 call on w2, got %d", w2.getCalls())
	}

	l.NotifyTargets()
	if w1.getCalls() != 2 || w2.getCalls() != 2 {
		t.Errorf("expected 2 calls, got w1=%d, w2=%d", w1.getCalls(), w2.getCalls())
	}
}

func TestEventListener_GracefulDegradationWhenNATSUnavailable(t *testing.T) {
	// Point to non-existent NATS port
	l := NewEventListener("nats://127.0.0.1:59998", []string{"job.>"}, nil)
	w := &mockWakeable{}
	l.Register(w)

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	err := l.Start(ctx)
	if err != nil {
		t.Fatalf("expected nil error on degradation, got: %v", err)
	}

	// Should not have crashed or hung
	if w.getCalls() != 0 {
		t.Errorf("expected 0 wake calls on unavailable broker, got %d", w.getCalls())
	}
}
