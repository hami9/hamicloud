package domain_test

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/hami9/hamicloud/runtime/internal/domain"
)

type stateMachineContract struct {
	Name           string              `json:"name"`
	Version        string              `json:"version"`
	States         []string            `json:"states"`
	TerminalStates []string            `json:"terminal_states"`
	Transitions    map[string][]string `json:"transitions"`
}

func TestJobStateMachineContractEquality(t *testing.T) {
	contractPath := filepath.Join("..", "..", "..", "contracts", "state-machines", "job.v1.json")
	data, err := os.ReadFile(contractPath)
	if err != nil {
		t.Fatalf("failed to read contract file %s: %v", contractPath, err)
	}

	var contract stateMachineContract
	if err := json.Unmarshal(data, &contract); err != nil {
		t.Fatalf("failed to parse contract JSON: %v", err)
	}

	goTransitions := domain.LegalTransitions()

	// 1. Verify all states in contract match Go states exactly
	var goStates []string
	for state := range goTransitions {
		goStates = append(goStates, string(state))
	}
	sort.Strings(goStates)
	contractStates := make([]string, len(contract.States))
	copy(contractStates, contract.States)
	sort.Strings(contractStates)

	if !reflect.DeepEqual(goStates, contractStates) {
		t.Fatalf("state mismatch between Go (%v) and contract (%v)", goStates, contractStates)
	}

	// 2. Verify all transitions match exactly
	for stateStr, contractDests := range contract.Transitions {
		state := domain.JobState(stateStr)
		goDestsMap, exists := goTransitions[state]
		if !exists {
			t.Fatalf("state %s defined in contract missing in Go", stateStr)
		}

		var goDests []string
		for dest := range goDestsMap {
			goDests = append(goDests, string(dest))
		}
		sort.Strings(goDests)

		sortedContractDests := make([]string, len(contractDests))
		copy(sortedContractDests, contractDests)
		sort.Strings(sortedContractDests)

		if len(goDests) == 0 && len(sortedContractDests) == 0 {
			continue
		}
		if !reflect.DeepEqual(goDests, sortedContractDests) {
			t.Fatalf("transitions for %s mismatch: Go has %v, contract has %v", stateStr, goDests, sortedContractDests)
		}
	}

	// Verify no extraneous states in Go
	for goState := range goTransitions {
		if _, exists := contract.Transitions[string(goState)]; !exists {
			t.Fatalf("Go has extra state %s not in contract", goState)
		}
	}
}

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
			name:        "QUEUED to CANCEL_REQUESTED",
			current:     domain.StateQueued,
			next:        domain.StateCancelRequested,
			expectError: nil,
		},
		{
			name:        "ADMITTED to STARTING",
			current:     domain.StateAdmitted,
			next:        domain.StateStarting,
			expectError: nil,
		},
		{
			name:        "ADMITTED to CANCEL_REQUESTED",
			current:     domain.StateAdmitted,
			next:        domain.StateCancelRequested,
			expectError: nil,
		},
		{
			name:        "STARTING to RUNNING",
			current:     domain.StateStarting,
			next:        domain.StateRunning,
			expectError: nil,
		},
		{
			name:        "STARTING to RETRY_WAIT",
			current:     domain.StateStarting,
			next:        domain.StateRetryWait,
			expectError: nil,
		},
		{
			name:        "STARTING to FAILED",
			current:     domain.StateStarting,
			next:        domain.StateFailed,
			expectError: nil,
		},
		{
			name:        "STARTING to CANCEL_REQUESTED",
			current:     domain.StateStarting,
			next:        domain.StateCancelRequested,
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
			name:        "RUNNING to RECOVERY_PENDING",
			current:     domain.StateRunning,
			next:        domain.StateRecoveryPending,
			expectError: nil,
		},
		{
			name:        "RETRY_WAIT to QUEUED",
			current:     domain.StateRetryWait,
			next:        domain.StateQueued,
			expectError: nil,
		},
		{
			name:        "RETRY_WAIT to CANCEL_REQUESTED",
			current:     domain.StateRetryWait,
			next:        domain.StateCancelRequested,
			expectError: nil,
		},
		{
			name:        "RECOVERY_PENDING to RETRY_WAIT",
			current:     domain.StateRecoveryPending,
			next:        domain.StateRetryWait,
			expectError: nil,
		},
		{
			name:        "RECOVERY_PENDING to FAILED",
			current:     domain.StateRecoveryPending,
			next:        domain.StateFailed,
			expectError: nil,
		},
		{
			name:        "RECOVERY_PENDING to CANCEL_REQUESTED",
			current:     domain.StateRecoveryPending,
			next:        domain.StateCancelRequested,
			expectError: nil,
		},
		{
			name:        "CANCEL_REQUESTED to CANCELLED",
			current:     domain.StateCancelRequested,
			next:        domain.StateCancelled,
			expectError: nil,
		},

		// Illegal transitions rejected by D4 and D5
		{
			name:        "CANCEL_REQUESTED to SUCCEEDED (rejected by D4)",
			current:     domain.StateCancelRequested,
			next:        domain.StateSucceeded,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "CANCEL_REQUESTED to FAILED (rejected by D5)",
			current:     domain.StateCancelRequested,
			next:        domain.StateFailed,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "QUEUED directly to CANCELLED (rejected by D5)",
			current:     domain.StateQueued,
			next:        domain.StateCancelled,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "ADMITTED directly to CANCELLED (rejected by D5)",
			current:     domain.StateAdmitted,
			next:        domain.StateCancelled,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "ADMITTED directly to FAILED (rejected by D5)",
			current:     domain.StateAdmitted,
			next:        domain.StateFailed,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "RETRY_WAIT directly to CANCELLED (rejected by D5)",
			current:     domain.StateRetryWait,
			next:        domain.StateCancelled,
			expectError: domain.ErrInvalidTransition,
		},
		{
			name:        "QUEUED directly to RUNNING (skipping admission)",
			current:     domain.StateQueued,
			next:        domain.StateRunning,
			expectError: domain.ErrInvalidTransition,
		},

		// Transitions out of terminal states
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
		domain.StateRecoveryPending,
		domain.StateCancelRequested,
	}

	for _, state := range nonTerminals {
		if state.IsTerminal() {
			t.Errorf("expected state %s to NOT be terminal, but got true", state)
		}
	}
}
