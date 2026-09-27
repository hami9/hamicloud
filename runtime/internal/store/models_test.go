package store

import (
	"regexp"
	"testing"
)

func TestNewUUID_Format(t *testing.T) {
	uuidRegex := regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

	for i := 0; i < 100; i++ {
		u := NewUUID()
		if !uuidRegex.MatchString(u) {
			t.Fatalf("generated invalid RFC4122 v4 UUID: %s", u)
		}
	}
}

func TestNewUUID_Uniqueness(t *testing.T) {
	seen := make(map[string]bool)
	for i := 0; i < 1000; i++ {
		u := NewUUID()
		if seen[u] {
			t.Fatalf("collision detected for UUID: %s", u)
		}
		seen[u] = true
	}
}
