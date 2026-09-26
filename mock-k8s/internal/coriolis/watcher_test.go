package coriolis

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/spawner"
)

// fakeRecycler stands in for the spawner. Recycle marks the instance as
// restarting (pid 0), which is what the real spawner's replacement looks like
// until its pidfile lands.
type fakeRecycler struct {
	insts     []spawner.InstanceRef
	calls     []string
	err       error
	noRespawn bool
}

func (f *fakeRecycler) InstancesOf(mapName string) []spawner.InstanceRef {
	if mapName != "DeepDesert_1" {
		return nil
	}
	return append([]spawner.InstanceRef(nil), f.insts...)
}

func (f *fakeRecycler) Recycle(key, suffix string) (bool, error) {
	f.calls = append(f.calls, key+"|"+suffix)
	if f.err != nil {
		return false, f.err
	}
	for i := range f.insts {
		if f.insts[i].Key == key && f.insts[i].Suffix == suffix {
			f.insts[i].PID = 0
		}
	}
	return !f.noRespawn, nil
}

func (f *fakeRecycler) setLive(key string, pid int) {
	for i := range f.insts {
		if f.insts[i].Key == key {
			f.insts[i].PID = pid
		}
	}
}

// bootLines is what a DD prints at startup (real lines from the 2026-06-02
// cycle on our test server).
func bootLines(this, next string) string {
	return "[2026.06.01-20.28.33:589][  0][809]LogCoriolis: Display: Current Coriolis World Seed: 2\n" +
		"[2026.06.01-20.28.33:589][  0][809]LogCoriolis: Display: This Coriolis Cycle start date UTC: " + this + "\n" +
		"[2026.06.01-20.28.33:590][  0][809]LogCoriolis: Display: Next Coriolis Cycle start date UTC: " + next + "\n" +
		"[2026.06.01-20.28.39:015][  0][809]LogNet: Log: AddClientConnection: Added client connection\n"
}

func appendLog(t *testing.T, path, s string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.WriteString(s); err != nil {
		t.Fatal(err)
	}
}

type harness struct {
	w   *Watcher
	rec *fakeRecycler
	dir string
	now time.Time
}

func newHarness(t *testing.T, insts ...spawner.InstanceRef) *harness {
	t.Helper()
	h := &harness{rec: &fakeRecycler{insts: insts}, dir: t.TempDir()}
	h.w = New([]Source{NewSpawnerSource(h.rec, h.dir, []string{"DeepDesert_1"})}, 2*time.Minute)
	h.w.now = func() time.Time { return h.now }
	return h
}

func (h *harness) logOf(suffix string) string {
	return filepath.Join(h.dir, "logs", "ue5-DeepDesert_1-"+suffix+".log")
}

func (h *harness) at(s string) {
	ts, err := time.Parse(cycleLayout, s)
	if err != nil {
		panic(err)
	}
	h.now = ts
}

func ddInstance(key, suffix string, pid int) spawner.InstanceRef {
	return spawner.InstanceRef{Key: key, Suffix: suffix, PID: pid}
}

func mkLogDir(t *testing.T, h *harness) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(h.dir, "logs"), 0o755); err != nil {
		t.Fatal(err)
	}
}

func TestParseCycleLine(t *testing.T) {
	got, ok := parseNextCycle("[2026.06.01-20.28.33:590][  0][809]LogCoriolis: Display: Next Coriolis Cycle start date UTC: 2026.06.02-05.00.00")
	want := time.Date(2026, 6, 2, 5, 0, 0, 0, time.UTC)
	if !ok || !got.Equal(want) {
		t.Fatalf("parseNextCycle = %v, %v; want %v", got, ok, want)
	}
	for _, line := range []string{
		"[2026.06.01-20.28.33:589][  0][809]LogCoriolis: Display: This Coriolis Cycle start date UTC: 2026.05.26-05.00.00",
		"LogCoriolis: Display: Next Coriolis Cycle start date UTC: garbage",
		"",
	} {
		if _, ok := parseNextCycle(line); ok {
			t.Errorf("parseNextCycle(%q) matched, want no match", line)
		}
	}
}

// The #119 case: a DD booted before the boundary is still up after it.
func TestTick_RecyclesDDOnceAfterBoundaryPlusDelay(t *testing.T) {
	h := newHarness(t, ddInstance("default/dd", "p2", 100))
	mkLogDir(t, h)
	appendLog(t, h.logOf("p2"), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))

	h.at("2026.06.02-04.59.00")
	h.w.Tick()
	h.at("2026.06.02-05.01.00") // boundary passed, delay not yet
	h.w.Tick()
	if len(h.rec.calls) != 0 {
		t.Fatalf("recycled before boundary+delay: %v", h.rec.calls)
	}

	h.at("2026.06.02-05.02.30")
	h.w.Tick()
	if len(h.rec.calls) != 1 || h.rec.calls[0] != "default/dd|p2" {
		t.Fatalf("calls = %v, want one recycle of default/dd|p2", h.rec.calls)
	}

	// The replacement boots and prints the NEXT boundary: nothing more to do.
	h.rec.setLive("default/dd", 101)
	appendLog(t, h.logOf("p2"), bootLines("2026.06.02-05.00.00", "2026.06.09-05.00.00"))
	h.at("2026.06.02-05.10.00")
	h.w.Tick()
	h.at("2026.06.05-12.00.00")
	h.w.Tick()
	if len(h.rec.calls) != 1 {
		t.Fatalf("recycled again after the fresh boot: %v", h.rec.calls)
	}
}

// Same boundary twice (say the game did not advance its cycle): act once,
// never loop.
func TestTick_NeverRecyclesTwiceForTheSameBoundary(t *testing.T) {
	h := newHarness(t, ddInstance("default/dd", "p2", 100))
	mkLogDir(t, h)
	appendLog(t, h.logOf("p2"), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	h.at("2026.06.02-05.03.00")
	h.w.Tick()

	h.rec.setLive("default/dd", 101)
	appendLog(t, h.logOf("p2"), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	for _, ts := range []string{"2026.06.02-05.30.00", "2026.06.02-09.00.00", "2026.06.03-09.00.00"} {
		h.at(ts)
		h.w.Tick()
	}
	if len(h.rec.calls) != 1 {
		t.Fatalf("calls = %v, want exactly one recycle for boundary 2026.06.02", h.rec.calls)
	}
}

func TestTick_LeavesStartingInstancesAlone(t *testing.T) {
	h := newHarness(t, ddInstance("default/dd", "p2", 0))
	mkLogDir(t, h)
	// The file still ends with the PREVIOUS boot's lines: a boot in progress
	// must not be mistaken for a stale server.
	appendLog(t, h.logOf("p2"), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	if len(h.rec.calls) != 0 {
		t.Fatalf("recycled an instance that is still starting: %v", h.rec.calls)
	}
}

func TestTick_NoCycleLineNoAction(t *testing.T) {
	h := newHarness(t, ddInstance("default/dd", "p2", 100))
	mkLogDir(t, h)
	appendLog(t, h.logOf("p2"), "[2026.06.01-20.28.33:589][  0][809]LogInit: Log: nothing about Coriolis\n")
	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	if len(h.rec.calls) != 0 {
		t.Fatalf("recycled without knowing the boundary: %v", h.rec.calls)
	}
}

// Several DD instances due at once restart one at a time: the next waits
// until the previous one is back.
func TestTick_StaggersInstances(t *testing.T) {
	h := newHarness(t,
		ddInstance("default/dd-a", "p2", 100),
		ddInstance("default/dd-b", "p3", 200))
	mkLogDir(t, h)
	for _, sfx := range []string{"p2", "p3"} {
		appendLog(t, h.logOf(sfx), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	}
	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	h.at("2026.06.02-05.04.00")
	h.w.Tick() // the first replacement has no pid yet
	if len(h.rec.calls) != 1 {
		t.Fatalf("calls = %v, want one recycle while the first replacement is booting", h.rec.calls)
	}

	first := h.rec.calls[0]
	key := first[:len(first)-3]
	h.rec.setLive(key, 300)
	h.at("2026.06.02-05.05.00")
	h.w.Tick()
	if len(h.rec.calls) != 2 || h.rec.calls[1] == first {
		t.Fatalf("calls = %v, want the second instance recycled once the first is back", h.rec.calls)
	}
}

// A replacement that never comes back must not wedge the other instances
// forever.
func TestTick_StaggerWaitIsBounded(t *testing.T) {
	h := newHarness(t,
		ddInstance("default/dd-a", "p2", 100),
		ddInstance("default/dd-b", "p3", 200))
	mkLogDir(t, h)
	for _, sfx := range []string{"p2", "p3"} {
		appendLog(t, h.logOf(sfx), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	}
	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	h.at("2026.06.02-05.30.00") // well past the stagger bound, still no pid
	h.w.Tick()
	if len(h.rec.calls) != 2 {
		t.Fatalf("calls = %v, want the second instance recycled after the stagger bound", h.rec.calls)
	}
}

func TestTick_FailedRecycleRetriesAfterCooldown(t *testing.T) {
	h := newHarness(t, ddInstance("default/dd", "p2", 100))
	h.rec.err = errors.New("simulated")
	mkLogDir(t, h)
	appendLog(t, h.logOf("p2"), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))

	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	h.at("2026.06.02-05.04.00")
	h.w.Tick()
	if len(h.rec.calls) != 1 {
		t.Fatalf("calls = %v, want no immediate retry after a failure", h.rec.calls)
	}
	h.rec.err = nil
	h.at("2026.06.02-05.40.00")
	h.w.Tick()
	if len(h.rec.calls) != 2 {
		t.Fatalf("calls = %v, want a retry once the cooldown has passed", h.rec.calls)
	}
}

// rotate-logs.sh trims logs in place. The boundary already read must survive
// the trim, or a DD whose boot lines were trimmed away would never recycle.
func TestTick_BoundarySurvivesLogTrim(t *testing.T) {
	h := newHarness(t, ddInstance("default/dd", "p2", 100))
	mkLogDir(t, h)
	appendLog(t, h.logOf("p2"), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	h.at("2026.06.01-21.00.00")
	h.w.Tick()

	if err := os.WriteFile(h.logOf("p2"), []byte("[2026.06.02-02.00.00:000][  1][809]LogTemp: trimmed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	if len(h.rec.calls) != 1 {
		t.Fatalf("calls = %v, want the recycle to happen despite the trimmed log", h.rec.calls)
	}
}

// A line still being written (no newline yet) is not read until complete.
func TestTailer_WaitsForCompleteLine(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "x.log")
	appendLog(t, p, "[2026.06.01-20.28.33:590][  0][809]LogCoriolis: Display: Next Coriolis Cycle start date UTC: 2026.06.0")
	tl := newTail(p)
	if err := tl.scan(); err != nil {
		t.Fatal(err)
	}
	if !tl.next.IsZero() {
		t.Fatalf("parsed a half-written line: %v", tl.next)
	}
	appendLog(t, p, "2-05.00.00\n")
	if err := tl.scan(); err != nil {
		t.Fatal(err)
	}
	if want := time.Date(2026, 6, 2, 5, 0, 0, 0, time.UTC); !tl.next.Equal(want) {
		t.Fatalf("next = %v, want %v", tl.next, want)
	}
}

// No replacement was started (its ServerSetScale vanished): the other DD
// instances must not wait the stagger bound for it.
func TestTick_NoStaggerWaitWithoutReplacement(t *testing.T) {
	h := newHarness(t,
		ddInstance("default/dd-a", "p2", 100),
		ddInstance("default/dd-b", "p3", 200))
	h.rec.noRespawn = true
	mkLogDir(t, h)
	for _, sfx := range []string{"p2", "p3"} {
		appendLog(t, h.logOf(sfx), bootLines("2026.05.26-05.00.00", "2026.06.02-05.00.00"))
	}
	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	h.at("2026.06.02-05.04.00")
	h.w.Tick()
	if len(h.rec.calls) != 2 {
		t.Fatalf("calls = %v, want the second instance recycled on the next tick", h.rec.calls)
	}
}
