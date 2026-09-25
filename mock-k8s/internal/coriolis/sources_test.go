package coriolis

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

// dimHarness drives a watcher over the dimensional servers only.
type dimHarness struct {
	src  *DimensionSource
	w    *Watcher
	dir  string
	now  time.Time
	runs []string
	live map[int]bool
	fail map[string]error
}

func newDimHarness(t *testing.T) *dimHarness {
	t.Helper()
	h := &dimHarness{dir: t.TempDir(), live: map[int]bool{}, fail: map[string]error{}}
	for _, d := range []string{"runtime/pids", "logs"} {
		if err := os.MkdirAll(filepath.Join(h.dir, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	h.src = NewDimensionSource(h.dir, []string{"DeepDesert_1"})
	h.src.alive = func(pid int) bool { return h.live[pid] }
	h.src.run = func(verb, partition string) error {
		h.runs = append(h.runs, verb+" "+partition)
		if err := h.fail[verb]; err != nil {
			return err
		}
		if verb == "dimension-down" { // the real verb removes the pidfile
			matches, _ := filepath.Glob(filepath.Join(h.dir, "runtime/pids", "ue5-*-p"+partition+".pid"))
			for _, m := range matches {
				_ = os.Remove(m)
			}
		}
		return nil
	}
	h.w = New([]Source{h.src}, 2*time.Minute)
	h.w.now = func() time.Time { return h.now }
	return h
}

func (h *dimHarness) at(s string) {
	ts, err := time.Parse(cycleLayout, s)
	if err != nil {
		panic(err)
	}
	h.now = ts
}

// up writes the pidfile and boot lines of a running dimensional server.
func (h *dimHarness) up(t *testing.T, mapName string, dim, partition, pid int, next string) {
	t.Helper()
	base := "ue5-" + mapName + "-dim" + strconv.Itoa(dim) + "-p" + strconv.Itoa(partition)
	if err := os.WriteFile(filepath.Join(h.dir, "runtime/pids", base+".pid"), []byte(strconv.Itoa(pid)+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	appendLog(t, filepath.Join(h.dir, "logs", base+".log"), bootLines("2026.05.26-05.00.00", next))
	h.live[pid] = true
}

func TestDimensionSource_TargetsOnlyThatMapsDimensions(t *testing.T) {
	h := newDimHarness(t)
	h.up(t, "DeepDesert_1", 1, 101, 500, "2026.06.02-05.00.00")
	h.up(t, "DeepDesert_1", 2, 102, 501, "2026.06.02-05.00.00")
	h.up(t, "Survival_1", 1, 201, 502, "2026.06.02-05.00.00")
	// mock-k8s's own DD pidfile is not a dimension target.
	_ = os.WriteFile(filepath.Join(h.dir, "runtime/pids", "ue5-DeepDesert_1-p0.pid"), []byte("503\n"), 0o644)

	got := h.src.Targets()
	if len(got) != 2 {
		t.Fatalf("targets = %+v, want DD dimensions 1 and 2 only", got)
	}
	for _, tg := range got {
		if !tg.Live || (tg.Group != "dim:101" && tg.Group != "dim:102") {
			t.Errorf("unexpected target %+v", tg)
		}
	}
}

// Dimensions 1..N are why this source exists: without it only dimension 0
// would reshape, and players in the other dimensions would keep the old map.
func TestDimensionSource_RecyclesEachDimensionOnceInTurn(t *testing.T) {
	h := newDimHarness(t)
	h.up(t, "DeepDesert_1", 1, 101, 500, "2026.06.02-05.00.00")
	h.up(t, "DeepDesert_1", 2, 102, 501, "2026.06.02-05.00.00")

	h.at("2026.06.02-05.03.00")
	h.w.Tick()
	if want := []string{"dimension-down 101", "dimension-up 101"}; !equal(h.runs, want) {
		t.Fatalf("runs = %v, want %v", h.runs, want)
	}
	h.at("2026.06.02-05.04.00")
	h.w.Tick() // dimension 101 is still coming back (no pidfile yet)
	if len(h.runs) != 2 {
		t.Fatalf("runs = %v: dimension 102 restarted while 101 was still booting", h.runs)
	}

	h.up(t, "DeepDesert_1", 1, 101, 600, "2026.06.09-05.00.00") // back, new cycle
	h.at("2026.06.02-05.05.00")
	h.w.Tick()
	if want := []string{"dimension-down 101", "dimension-up 101", "dimension-down 102", "dimension-up 102"}; !equal(h.runs, want) {
		t.Fatalf("runs = %v, want %v", h.runs, want)
	}
}

func TestDimensionSource_DownFailureDoesNotBringUp(t *testing.T) {
	h := newDimHarness(t)
	h.up(t, "DeepDesert_1", 1, 101, 500, "2026.06.02-05.00.00")
	h.fail["dimension-down"] = errors.New("simulated")
	tg := h.src.Targets()[0]
	if _, err := h.src.Recycle(tg); err == nil {
		t.Fatal("Recycle returned nil although dimension-down failed")
	}
	if want := []string{"dimension-down 101"}; !equal(h.runs, want) {
		t.Fatalf("runs = %v, want only the failed down (never an up beside a live server)", h.runs)
	}
}

func TestDimensionSource_UpFailureIsReported(t *testing.T) {
	h := newDimHarness(t)
	h.up(t, "DeepDesert_1", 1, 101, 500, "2026.06.02-05.00.00")
	h.fail["dimension-up"] = errors.New("simulated")
	respawned, err := h.src.Recycle(h.src.Targets()[0])
	if err == nil || respawned {
		t.Fatalf("Recycle = %v, %v; want an error and no respawn when dimension-up fails", respawned, err)
	}
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
