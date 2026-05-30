package state

import (
	"os"
	"path/filepath"
	"testing"
)

func sampleState() State {
	return State{
		Version: currentVersion,
		Instances: []Instance{
			{Key: "default/sietch-survival", MapName: "Survival_1", PartitionID: 1, PoolIndex: 0, GamePort: 7900, IGWPort: 7950, Suffix: "p0", PID: 4242},
			{Key: "default/sietch-overmap", MapName: "Overmap", PartitionID: 2, PoolIndex: 1, GamePort: 7901, IGWPort: 7951, Suffix: "p1", PID: 4243},
		},
	}
}

func TestSaveLoad_RoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "mock-k8s-state.json")
	want := sampleState()

	if err := Save(path, want); err != nil {
		t.Fatalf("Save: %v", err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got.Version != want.Version {
		t.Errorf("version = %d, want %d", got.Version, want.Version)
	}
	if len(got.Instances) != len(want.Instances) {
		t.Fatalf("instances = %d, want %d", len(got.Instances), len(want.Instances))
	}
	for i := range want.Instances {
		if got.Instances[i] != want.Instances[i] {
			t.Errorf("instance[%d] = %+v, want %+v", i, got.Instances[i], want.Instances[i])
		}
	}
}

func TestSave_CreatesParentDir(t *testing.T) {
	// Mirrors production: server/state/ may not exist on first boot.
	path := filepath.Join(t.TempDir(), "server", "state", "mock-k8s-state.json")
	if err := Save(path, sampleState()); err != nil {
		t.Fatalf("Save into missing dir: %v", err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("state file not written: %v", err)
	}
}

func TestLoad_MissingFileIsEmptyNotError(t *testing.T) {
	got, err := Load(filepath.Join(t.TempDir(), "absent.json"))
	if err != nil {
		t.Fatalf("Load(missing) returned error: %v", err)
	}
	if len(got.Instances) != 0 {
		t.Errorf("Load(missing) instances = %d, want 0", len(got.Instances))
	}
}

func TestLoad_GarbageIsError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "garbage.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Error("Load(garbage) = nil error, want parse error")
	}
}

func TestSave_OverwriteAtomicLeavesNoTemp(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mock-k8s-state.json")
	if err := Save(path, sampleState()); err != nil {
		t.Fatal(err)
	}
	// Overwrite with a smaller state.
	smaller := State{Version: currentVersion, Instances: sampleState().Instances[:1]}
	if err := Save(path, smaller); err != nil {
		t.Fatal(err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Instances) != 1 {
		t.Errorf("after overwrite, instances = %d, want 1", len(got.Instances))
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Errorf("temp file lingered after Save: stat err = %v", err)
	}
}

func TestSave_DefaultsVersion(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mock-k8s-state.json")
	if err := Save(path, State{Instances: nil}); err != nil {
		t.Fatal(err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if got.Version != currentVersion {
		t.Errorf("version = %d, want %d (Save should stamp it)", got.Version, currentVersion)
	}
}
