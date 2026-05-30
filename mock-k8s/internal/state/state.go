// Package state persists the spawner's instance ledger across mock-k8s
// restarts. Without it, every restart re-derives ServerSetScales via
// by-name GETs and re-allocates the port pool from index 0 — so a UE5 that
// is still running ends up with a different game/IGW port than it bound to,
// and the Director's map→port routing drifts. Persisting the ledger lets
// Restore re-adopt still-live instances on their original ports.
//
// The file lives at $BASE/server/state/mock-k8s-state.json and is written
// atomically (temp file + rename) so a crash mid-write can never leave a
// half-serialized ledger that fails to parse on the next boot.
package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// currentVersion is the on-disk schema version, stamped by Save and used by
// future migrations to recognise older ledgers.
const currentVersion = 1

// Instance is one persisted UE5 instance: enough to re-reserve its exact
// port slot and probe whether its process survived the restart.
type Instance struct {
	Key         string `json:"key"` // ServerSetScale namespace/name
	MapName     string `json:"mapName"`
	PartitionID int    `json:"partitionId"`
	PoolIndex   int    `json:"poolIndex"`
	GamePort    int    `json:"gamePort"`
	IGWPort     int    `json:"igwPort"`
	Suffix      string `json:"suffix"`
	PID         int    `json:"pid"`
}

// State is the full persisted ledger.
type State struct {
	Version   int        `json:"version"`
	Instances []Instance `json:"instances"`
}

// Load reads the ledger at path. A missing file is not an error — it
// returns an empty (current-version) ledger, which is the correct
// first-boot state. A present-but-unparseable file IS an error so the
// caller can decide whether to start fresh or abort.
func Load(path string) (State, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return State{Version: currentVersion}, nil
		}
		return State{}, fmt.Errorf("read state %s: %w", path, err)
	}
	var s State
	if err := json.Unmarshal(b, &s); err != nil {
		return State{}, fmt.Errorf("parse state %s: %w", path, err)
	}
	return s, nil
}

// Save writes the ledger to path atomically, creating the parent directory
// if needed and stamping the schema version. The temp-file-plus-rename
// dance guarantees readers ever see only a complete file.
func Save(path string, s State) error {
	if s.Version == 0 {
		s.Version = currentVersion
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("mkdir state dir: %w", err)
	}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal state: %w", err)
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return fmt.Errorf("write temp state: %w", err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("rename state into place: %w", err)
	}
	return nil
}
