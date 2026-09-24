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
	StateRetryWait       JobState = "RETRY_WAIT"
	StateRecoveryPending JobState = "RECOVERY_PENDING"
	StateCancelRequested JobState = "CANCEL_REQUESTED"
	StateSucceeded       JobState = "SUCCEEDED"
	StateFailed          JobState = "FAILED"
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
	},
	StateAdmitted: {
		StateStarting:        true,
		StateCancelRequested: true,
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
		StateRecoveryPending: true,
	},
	StateRetryWait: {
		StateQueued:          true,
		StateCancelRequested: true,
	},
	StateRecoveryPending: {
		StateRetryWait:       true,
		StateFailed:          true,
		StateCancelRequested: true,
	},
	StateCancelRequested: {
		StateCancelled: true,
	},
	StateSucceeded: {},
	StateFailed:    {},
	StateCancelled: {},
}

// LegalTransitions returns a deep copy of the legal transitions table.
func LegalTransitions() map[JobState]map[JobState]bool {
	copyMap := make(map[JobState]map[JobState]bool, len(legalTransitions))
	for k, v := range legalTransitions {
		dest := make(map[JobState]bool, len(v))
		for dk, dv := range v {
			dest[dk] = dv
		}
		copyMap[k] = dest
	}
	return copyMap
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
