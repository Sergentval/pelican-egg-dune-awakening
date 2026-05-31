package state

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestLoad_CorruptIsErrCorrupt(t *testing.T) {
	p := filepath.Join(t.TempDir(), "ledger.json")
	if err := os.WriteFile(p, []byte("{not valid json"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err := Load(p)
	if !errors.Is(err, ErrCorrupt) {
		t.Fatalf("Load(corrupt) err = %v; want it to wrap ErrCorrupt", err)
	}
}

func TestLoad_TransientIOIsNotCorrupt(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root bypasses file permission bits")
	}
	p := filepath.Join(t.TempDir(), "ledger.json")
	if err := os.WriteFile(p, []byte(`{"version":1,"instances":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(p, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(p, 0o644) })

	_, err := Load(p)
	if err == nil {
		t.Fatal("Load(unreadable) err = nil; want an I/O error")
	}
	if errors.Is(err, ErrCorrupt) {
		t.Errorf("transient I/O error mis-classified as ErrCorrupt: %v", err)
	}
}
