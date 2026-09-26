package traveldemand

import (
	"errors"
	"path/filepath"
	"testing"
	"time"
)

// Issue #136. At the instance cap the watcher refused the map and told only
// mock-k8s.log; the Director kept the request queued for a group with no
// server until it expired 300 s later, and the player sat on "Connecting to
// ..." with nothing to say why. The watcher now makes room from an instance
// nobody is on before refusing, and says so on the server when it cannot.

type fakeFreer struct {
	freed  string
	err    error
	asked  []string
	onFree func()
}

func (f *fakeFreer) FreeSlot(exclude string) (string, error) {
	f.asked = append(f.asked, exclude)
	if f.onFree != nil && f.err == nil && f.freed != "" {
		f.onFree()
	}
	return f.freed, f.err
}

type fakeAnnouncer struct{ maps []string }

func (f *fakeAnnouncer) AtCapacity(mapName string) { f.maps = append(f.maps, mapName) }

func travel(t *testing.T, p, mapName string) {
	t.Helper()
	writeFile(t, p, "Received travel request for 1 player(s) to "+mapName+" (instancingMode=ClassicalInstancing)\n")
}

func TestWatcher_AtCapMakesRoomThenStarts(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	s := &fakeScaler{live: 8}
	fr := &fakeFreer{freed: "CB_Overland_S_06"}
	fr.onFree = func() { s.live-- } // the spawner untracks the stopped instance at once
	a := &fakeAnnouncer{}
	w := New(p, s, 8).WithCapacity(fr, a)
	travel(t, p, "SH_Arrakeen")
	w.Tick()
	if len(fr.asked) != 1 || fr.asked[0] != "SH_Arrakeen" {
		t.Fatalf("freer asked %v, want one request excluding the map being started", fr.asked)
	}
	if len(s.calls) != 1 || s.calls[0] != "SH_Arrakeen" {
		t.Fatalf("scaler calls = %v, want SH_Arrakeen started once room was made", s.calls)
	}
	if len(a.maps) != 0 {
		t.Fatalf("announced %v although the map could start", a.maps)
	}
}

func TestWatcher_AtCapWithNothingToFreeAnnounces(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	s := &fakeScaler{live: 8}
	a := &fakeAnnouncer{}
	w := New(p, s, 8).WithCapacity(&fakeFreer{}, a)
	travel(t, p, "SH_Arrakeen")
	w.Tick()
	if len(s.calls) != 0 {
		t.Fatalf("started past the cap: %v", s.calls)
	}
	if len(a.maps) != 1 || a.maps[0] != "SH_Arrakeen" {
		t.Fatalf("announced %v, want the refused map", a.maps)
	}
}

func TestWatcher_FreerErrorStillRefusesAndAnnounces(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	s := &fakeScaler{live: 8}
	a := &fakeAnnouncer{}
	w := New(p, s, 8).WithCapacity(&fakeFreer{err: errors.New("psql down")}, a)
	travel(t, p, "SH_Arrakeen")
	w.Tick()
	if len(s.calls) != 0 || len(a.maps) != 1 {
		t.Fatalf("calls=%v announced=%v, want a refusal and an announcement", s.calls, a.maps)
	}
}

// Several players travelling to a map that is already running need no new
// instance: that is not a refusal, whatever the count.
func TestWatcher_AtCapAMapAlreadyUpIsNotRefused(t *testing.T) {
	p := filepath.Join(t.TempDir(), "director.log")
	s := &fakeScaler{live: 8, already: true}
	fr := &fakeFreer{freed: "x"}
	a := &fakeAnnouncer{}
	w := New(p, s, 8).WithCapacity(fr, a)
	travel(t, p, bandit)
	w.Tick()
	if len(fr.asked) != 0 || len(a.maps) != 0 {
		t.Fatalf("freer asked %v, announced %v, for a map that is already up", fr.asked, a.maps)
	}
}

// --- the broadcast ---------------------------------------------------------

func TestCapAnnouncer_RateLimited(t *testing.T) {
	var sent []string
	now := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)
	a := NewCapAnnouncer(8, func(title, body string) error { sent = append(sent, body); return nil })
	a.now = func() time.Time { return now }

	a.AtCapacity("SH_Arrakeen")
	a.AtCapacity("SH_Arrakeen") // same minute: dropped
	now = now.Add(30 * time.Second)
	a.AtCapacity("CB_Dungeon_ThePit") // another map, but within the global minute: dropped
	if len(sent) != 1 {
		t.Fatalf("sent %d broadcasts, want 1 (one per minute server-wide)", len(sent))
	}
	now = now.Add(2 * time.Minute)
	a.AtCapacity("SH_Arrakeen") // global minute passed, but this map was announced 2.5 min ago
	if len(sent) != 1 {
		t.Fatalf("sent %d, want no repeat for the same map within 5 minutes", len(sent))
	}
	a.AtCapacity("CB_Dungeon_ThePit")
	if len(sent) != 2 {
		t.Fatalf("sent %d, want the second map announced once the minute passed", len(sent))
	}
	now = now.Add(5 * time.Minute)
	a.AtCapacity("SH_Arrakeen")
	if len(sent) != 3 {
		t.Fatalf("sent %d, want SH_Arrakeen announced again after 5 minutes", len(sent))
	}
}
