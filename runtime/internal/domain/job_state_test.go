package domain_test

import (
	"errors"
	"testing"

	"github.com/hami9/hamicloud/runtime/internal/domain"
)

func TestValidateTransition(t *testing.T) {
	tests := []struct {
		name        string
		current     domain.JobState
		next        domain.JobState
		expectError error
	}{
		// Valid transitions
		{
			name:        "QUEUED to ADMITTED",
			current:     domain.StateQueued,
			next:        domain.StateAdmitted,
			expectError: nil,
		},
		{
			name:        "ADMITTED to STARTING",
			current:     domain.StateAdmitted,
			next:        domain.StateStarting,
			expectError: nil,
		},
		{
			name:        "STARTING to RUNNING",
			current:     domain.StateStarting,
			next:        domain.StateRunning,
			expectError: nil,
		},
		{
			name:        "RUNNING to SUCCEEDED",
			current:     domain.StateRunning,
			next:        domain.StateSucceeded,
			expectError: nil,
		},
		{
			name:        "RUNNING to RETRY_WAIT",
			current:     domain.StateRunning,
			next:        domain.StateRetryWait,
			expectError: nil,
		},
		{
			name:        "RETRY_WAIT to QUEUED",
			current:     domain.StateRetryWait,
			next:        domain.StateQueued,
			expectError: nil,
		},
		{
			name:        "RUNNING to FAILED",
			current:     domain.StateRunning,
			next:        domain.StateFailed,
			expectError: nil,
		},
		{
			name:        "RUNNING to CANCEL_REQUESTED",
			current:     domain.StateRunning,
			next:        domain.StateCancelRequested,
			expectError: nil,
		},
		{
			name:        "CANCEL_REQUESTED to CANCELLED",
			current:     domain.StateCancelRequested,
			next:        domain.StateCancelled,
			expectError: nil,
		},

		// Illegal transitions
		{
			name:        "QUEUED directly to RUNNING (skipping admission)",
			current:     domain.StateQueued,
			next:        domain.StateRunning,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "SUCCEEDED to RUNNING (transitioning from terminal)",
			current:     domain.StateSucceeded,
			next:        domain.StateRunning,
			expectError: domain.ErrTerminalState,
		},
		{
			name:        "FAILED to QUEUED (cannot transition out of terminal failure)",
			current:     domain.StateFailed,
			next:        domain.StateQueued,
			expectError: domain.ErrTerminalState,
		},
		{
			name:        "CANCELLED to STARTING (cannot transition out of cancelled)",
			current:     domain.StateCancelled,
			next:        domain.StateStarting,
			expectError: domain.ErrTerminalState,
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			err := domain.ValidateTransition(tc.current, tc.next)
			if tc.expectError != nil {
				if err == nil {
					t.Fatalf("expected error %v, got nil", tc.expectError)
				}
				if !errors.Is(err, tc.expectError) {
					t.Fatalf("expected error %v, got %v", tc.expectError, err)
				}
			} else if err != nil {
				t.Fatalf("expected no error, got %v", err)
			}
		})
	}
}

func TestIsTerminal(t *testing.T) {
	terminals := []domain.JobState{
		domain.StateSucceeded,
		domain.StateFailed,
		domain.StateCancelled,
	}

	for _, state := range terminals {
		if !state.IsTerminal() {
			t.Errorf("expected state %s to be terminal, but got false", state)
		}
	}

	nonTerminals := []domain.JobState{
		domain.StateQueued,
		domain.StateAdmitted,
		domain.StateStarting,
		domain.StateRunning,
		domain.StateRetryWait,
		domain.StateCancelRequested,
	}

	for _, state := range nonTerminals {
		if state.IsTerminal() {
			t.Errorf("expected state %s to NOT be terminal, but got true", state)
		}
	}
}
