package domain

import (
	"errors"
	"fmt"
)

// JobState represents the logical or attempt state of a HamiCloud Job.
type JobState string

const (
	StateQueued          JobState = "QUEUED"
	StateAdmitted        JobState = "ADMITTED"
	StateStarting        JobState = "STARTING"
	StateRunning         JobState = "RUNNING"
	StateSucceeded       JobState = "SUCCEEDED"
	StateRetryWait       JobState = "RETRY_WAIT"
	StateFailed          JobState = "FAILED"
	StateCancelRequested JobState = "CANCEL_REQUESTED"
	StateCancelled       JobState = "CANCELLED"
)

var (
	ErrInvalidTransition = errors.New("invalid job state transition")
	ErrTerminalState     = errors.New("cannot transition out of a terminal state")
)

// legalTransitions maps each valid starting state to its allowed destination states.
var legalTransitions = map[JobState]map[JobState]bool{
	StateQueued: {
		StateAdmitted:        true,
		StateCancelRequested: true,
		StateCancelled:       true,
	},
	StateAdmitted: {
		StateStarting:        true,
		StateCancelRequested: true,
		StateCancelled:       true,
		StateFailed:          true,
	},
	StateStarting: {
		StateRunning:         true,
		StateRetryWait:       true,
		StateFailed:          true,
		StateCancelRequested: true,
	},
	StateRunning: {
		StateSucceeded:       true,
		StateRetryWait:       true,
		StateFailed:          true,
		StateCancelRequested: true,
	},
	StateRetryWait: {
		StateQueued:          true,
		StateCancelRequested: true,
		StateCancelled:       true,
	},
	StateCancelRequested: {
		StateCancelled: true,
		StateFailed:    true,
		StateSucceeded: true,
	},
	StateSucceeded: {},
	StateFailed:    {},
	StateCancelled: {},
}

// IsTerminal returns true if the state cannot transition to any other state.
func (s JobState) IsTerminal() bool {
	return s == StateSucceeded || s == StateFailed || s == StateCancelled
}

// ValidateTransition verifies whether transitioning from current to next is legal.
func ValidateTransition(current, next JobState) error {
	if current.IsTerminal() {
		return fmt.Errorf("%w: current state %s is terminal", ErrTerminalState, current)
	}

	allowed, exists := legalTransitions[current]
	if !exists || !allowed[next] {
		return fmt.Errorf("%w: from %s to %s", ErrInvalidTransition, current, next)
	}

	return nil
}
