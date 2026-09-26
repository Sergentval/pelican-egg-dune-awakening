package traveldemand

import (
	"os"
	"path/filepath"
	"testing"
)

// The Director routes a player to an instanced map only if a server for that
// group already exists; it never creates the first one. Nothing on our side
// created it either, so a reporter's travel to a mission read:
//
//	Processing travel queue for ClassicalInstancing group CB_Story_BanditFortress01 (servers: [], num: 0)
//
// once a minute until the request died:
//
//	Travel request expired. MapName: CB_Story_BanditFortress01
//
// The trigger was in the same log all along, one line earlier:
//
//	Received travel request for 1 player(s) to CB_Story_BanditFortress01 (instancingMode=ClassicalInstancing)
//
// This package watches for that line and scales the map to one replica. A cold
// instance is ready in about ten seconds, far inside the 300s expiry.

const bandit = "CB_Story_BanditFortress01"

func TestParse_PicksInstancedTravelRequests(t *testing.T) {
	chunk := `[07:09:32 7 INF Main] Received travel request for 1 player(s) to ` + bandit + ` (instancingMode=ClassicalInstancing)
[07:09:33 7 DBG Main] Created travel request TravelRequest { Token = 13 }
[07:10:01 9 INF Main] Received travel request for 4 player(s) to CB_Dungeon_ThePit (instancingMode=ClassicalInstancing)`
	got := Parse(chunk)
	want := []string{bandit, "CB_Dungeon_ThePit"}
	if len(got) != len(want) {
		t.Fatalf("Parse returned %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Parse[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// Survival_1, Overmap and the Deep Desert are spawned by AlwaysWarm and by the
// dimension scripts. Scaling them from here would fight those owners.
func TestParse_IgnoresNonInstancedModes(t *testing.T) {
	chunk := `Received travel request for 1 player(s) to Survival_1 (instancingMode=Dimension)
Received travel request for 1 player(s) to Overmap (instancingMode=SingleServer)
Received travel request for 2 player(s) to DeepDesert_1 (instancingMode=Dimension)`
	if got := Parse(chunk); len(got) != 0 {
		t.Errorf("Parse returned %v for non-instanced modes, want none", got)
	}
}

func TestParse_DeduplicatesWithinAChunk(t *testing.T) {
	line := "Received travel request for 1 player(s) to " + bandit + " (instancingMode=ClassicalInstancing)\n"
	if got := Parse(line + line + line); len(got) != 1 {
		t.Errorf("Parse returned %v for three identical requests, want one", got)
	}
}

func TestParse_IgnoresUnrelatedLines(t *testing.T) {
	chunk := `Processing travel queue for ClassicalInstancing group ` + bandit + ` (servers: [], num: 0)
Travel request expired. MapName: ` + bandit + `
[07:09:32 INF BGRP] Patching battlegroupdirectorstats`
	if got := Parse(chunk); len(got) != 0 {
		t.Errorf("Parse matched a non-request line: %v", got)
	}
}

// A map name is used to build a resource name and a process argument, so it
// must not be free text.
func TestParse_RejectsUnsafeMapNames(t *testing.T) {
	chunk := "Received travel request for 1 player(s) to ../../etc/passwd (instancingMode=ClassicalInstancing)\n" +
		"Received travel request for 1 player(s) to CB_x;rm (instancingMode=ClassicalInstancing)"
	if got := Parse(chunk); len(got) != 0 {
		t.Errorf("Parse accepted an unsafe map name: %v", got)
	}
}

// --- the tailer -----------------------------------------------------------

func writeFile(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestTailer_ReturnsOnlyNewContent(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	writeFile(t, p, "first\n")
	tl := NewTailer(p)
	if got, _ := tl.Read(); got != "" {
		t.Fatalf("first read = %q, want nothing: what was already there predates us", got)
	}
	writeFile(t, p, "first\nsecond\n")
	if got, _ := tl.Read(); got != "second\n" {
		t.Fatalf("second read = %q, want only the appended line", got)
	}
	if got, _ := tl.Read(); got != "" {
		t.Fatalf("third read returned %q, want nothing new", got)
	}
}

// director.log is append-only across restarts and reaches tens of megabytes; it
// holds every travel request the server has ever served. A tailer starting at
// offset 0 replays all of them on its first tick and starts a map for every
// destination anyone ever travelled to. That is not hypothetical: on our test
// box a restart brought up a mission instance from a request eight minutes
// dead, because the line was still in the file.
func TestTailer_SkipsTheHistoryThatWasThereBeforeItStarted(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	tl := NewTailer(p)
	got, err := tl.Read()
	if err != nil {
		t.Fatal(err)
	}
	if Parse(got) != nil {
		t.Errorf("replayed a travel request that predates the watcher: %q", got)
	}
}

// A log that does not exist yet is different: the Director has not started, so
// everything that lands in the file afterwards is live.
func TestTailer_ReadsAFileCreatedAfterItStarted(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	tl := NewTailer(p)
	if got, _ := tl.Read(); got != "" {
		t.Fatalf("read %q from a missing file", got)
	}
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	got, err := tl.Read()
	if err != nil {
		t.Fatal(err)
	}
	if len(Parse(got)) != 1 {
		t.Errorf("missed the first request written to a freshly created log: %q", got)
	}
}

// rotate-logs.sh trims a log IN PLACE (the writers hold it O_APPEND), so the
// file gets smaller while staying the same inode. A tailer that kept its old
// offset would read nothing ever again — the watcher would go silently deaf
// exactly on the busy servers whose logs get trimmed.
func TestTailer_RecoversFromInPlaceTruncation(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	writeFile(t, p, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n")
	tl := NewTailer(p) // primed at the end of the long line
	if _, err := tl.Read(); err != nil {
		t.Fatal(err)
	}
	writeFile(t, p, "short\n") // trimmed: now smaller than the offset
	got, err := tl.Read()
	if err != nil {
		t.Fatal(err)
	}
	if got != "short\n" {
		t.Errorf("after truncation read = %q, want the new content from offset 0", got)
	}
}

func TestTailer_MissingFileIsNotAnError(t *testing.T) {
	tl := NewTailer(filepath.Join(t.TempDir(), "absent.log"))
	got, err := tl.Read()
	if err != nil || got != "" {
		t.Errorf("missing file: got (%q, %v), want (\"\", nil) — the Director may not have started yet", got, err)
	}
}

// --- the scale decision ---------------------------------------------------

type fakeScaler struct {
	calls   []string
	live    int
	err     error
	already bool // the map was already up: ScaleToOne changes nothing
}

func (f *fakeScaler) LiveInstances() int       { return f.live }
func (f *fakeScaler) IsUp(mapName string) bool { return f.already }
func (f *fakeScaler) ScaleToOne(mapName string) (bool, error) {
	f.calls = append(f.calls, mapName)
	return !f.already, f.err
}

func TestWatcher_ScalesADemandedMap(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	f := &fakeScaler{}
	w := New(p, f, 8)
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	w.Tick()
	if len(f.calls) != 1 || f.calls[0] != bandit {
		t.Errorf("scaler calls = %v, want one for %s", f.calls, bandit)
	}
}

// The cap exists because MaxConcurrentInstances was only ever logged, never
// enforced: a burst of travel requests would otherwise start a UE5 per map and
// exhaust the port pool — or the box.
func TestWatcher_RefusesBeyondTheConcurrencyCap(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	f := &fakeScaler{live: 8}
	w := New(p, f, 8)
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	w.Tick()
	if len(f.calls) != 0 {
		t.Errorf("scaled past the cap: %v", f.calls)
	}
}

func TestWatcher_SecondTickDoesNotRescaleTheSameRequest(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	f := &fakeScaler{}
	w := New(p, f, 8)
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	w.Tick()
	w.Tick()
	if len(f.calls) != 1 {
		t.Errorf("scaler called %d times for one request: %v", len(f.calls), f.calls)
	}
}

// A scaler error must not wedge the watcher: the next travel request has to be
// served, and a map that failed once is retried when a player asks again.
func TestWatcher_KeepsGoingAfterAScalerError(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	f := &fakeScaler{err: os.ErrPermission}
	w := New(p, f, 8)
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	w.Tick()
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n"+
		"Received travel request for 1 player(s) to CB_Dungeon_ThePit (instancingMode=ClassicalInstancing)\n")
	w.Tick()
	if len(f.calls) != 2 || f.calls[1] != "CB_Dungeon_ThePit" {
		t.Errorf("watcher stopped after an error: %v", f.calls)
	}
}

// A request for a map that is already up is normal — several players travel to
// the same mission — and must still be handled without claiming a start.
func TestWatcher_HandlesARequestForAMapThatIsAlreadyUp(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	f := &fakeScaler{already: true}
	w := New(p, f, 8)
	writeFile(t, p, "Received travel request for 1 player(s) to "+bandit+" (instancingMode=ClassicalInstancing)\n")
	w.Tick()
	if len(f.calls) != 1 {
		t.Errorf("scaler calls = %v, want one: the request still has to be offered to the scaler", f.calls)
	}
}
