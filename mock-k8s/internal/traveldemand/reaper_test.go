package traveldemand

import (
	"errors"
	"testing"
	"time"
)

// What this guards, and why it was written the day after the watcher shipped.
//
// The watcher starts a map when a player asks to travel there. Nothing ever
// stopped one again. MaxConcurrentInstances is a budget over EVERY tracked
// instance, and a reporter's server keeps six maps always-warm out of a budget
// of eight, so two on-demand slots existed, were taken by the first two
// dungeons anyone visited, and were never given back:
//
//	traveldemand: at the instance cap, refusing to start a map a player asked
//	for map=Story_ArtOfKanly live=8 max=8
//
// Every other dungeon then sat at "(servers: [], num: 0)" until the next
// restart. AutomaticStopDuration was in ondemand.ini the whole time — parsed,
// logged at boot, and applied by nobody.

const pit = "CB_Dungeon_ThePit"

type fakeReapScaler struct {
	up      []string
	stopped []string
	err     error
}

func (f *fakeReapScaler) ScaledUpMaps() []string { return f.up }
func (f *fakeReapScaler) ScaleToZero(m string) error {
	if f.err != nil {
		return f.err
	}
	f.stopped = append(f.stopped, m)
	var kept []string
	for _, s := range f.up {
		if s != m {
			kept = append(kept, s)
		}
	}
	f.up = kept
	return nil
}

type fakeOccupancy struct {
	counts map[string]int
	err    error
}

func (f *fakeOccupancy) PlayerCounts() (map[string]int, error) { return f.counts, f.err }

// clock lets a test walk ten minutes forward without sleeping.
type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

func newTestReaper(s ReapScaler, o Occupancy, warm []string) (*Reaper, *clock) {
	c := &clock{t: time.Date(2026, 9, 21, 12, 0, 0, 0, time.UTC)}
	r := NewReaper(s, o, 10*time.Minute, warm)
	r.now = c.now
	return r, c
}

func TestReaper_StopsAMapThatHasBeenEmptyLongEnough(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	r, c := newTestReaper(s, &fakeOccupancy{counts: map[string]int{pit: 0}}, nil)

	r.Tick() // starts the timer
	c.advance(9 * time.Minute)
	r.Tick()
	if len(s.stopped) != 0 {
		t.Fatalf("reaped after 9 minutes: %v (AutomaticStopDuration is 10)", s.stopped)
	}
	c.advance(2 * time.Minute)
	r.Tick()
	if len(s.stopped) != 1 || s.stopped[0] != pit {
		t.Errorf("stopped = %v, want [%s] after 11 minutes empty", s.stopped, pit)
	}
}

func TestReaper_NeverStopsAMapWithPlayersOnIt(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	r, c := newTestReaper(s, &fakeOccupancy{counts: map[string]int{pit: 1}}, nil)
	r.Tick()
	c.advance(time.Hour)
	r.Tick()
	if len(s.stopped) != 0 {
		t.Errorf("evicted a player: %v", s.stopped)
	}
}

// Somebody arriving has to reset the clock, or a dungeon a group has been
// clearing for eleven minutes gets pulled out from under them.
func TestReaper_APlayerArrivingResetsTheIdleTimer(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	occ := &fakeOccupancy{counts: map[string]int{pit: 0}}
	r, c := newTestReaper(s, occ, nil)

	r.Tick()
	c.advance(9 * time.Minute)
	occ.counts[pit] = 2 // a group arrives
	r.Tick()
	occ.counts[pit] = 0 // and leaves
	c.advance(2 * time.Minute)
	r.Tick()
	if len(s.stopped) != 0 {
		t.Fatalf("reaped %v — the timer must restart from when they LEFT", s.stopped)
	}
	c.advance(11 * time.Minute)
	r.Tick()
	if len(s.stopped) != 1 {
		t.Errorf("never reaped after the group left: %v", s.stopped)
	}
}

// The always-warm maps are the operator's floor. They are the reason the
// budget is tight in the first place, but they are not the reaper's to touch.
func TestReaper_LeavesAlwaysWarmMapsAlone(t *testing.T) {
	s := &fakeReapScaler{up: []string{"Survival_1", "SH_Arrakeen", pit}}
	occ := &fakeOccupancy{counts: map[string]int{"Survival_1": 0, "SH_Arrakeen": 0, pit: 0}}
	r, c := newTestReaper(s, occ, []string{"Survival_1", "SH_Arrakeen"})

	r.Tick()
	c.advance(time.Hour)
	r.Tick()
	if len(s.stopped) != 1 || s.stopped[0] != pit {
		t.Errorf("stopped = %v, want only %s", s.stopped, pit)
	}
}

// The rule that keeps a bad query from emptying the whole server: an error is
// "unknown", never "nobody is here".
func TestReaper_HoldsWhenThePlayerCountCannotBeRead(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	r, c := newTestReaper(s, &fakeOccupancy{err: errors.New("psql: connection refused")}, nil)
	r.Tick()
	c.advance(time.Hour)
	r.Tick()
	if len(s.stopped) != 0 {
		t.Errorf("reaped on an unreadable player count: %v", s.stopped)
	}
}

// A map absent from the count has no running instance heartbeating, so it is
// not a candidate at all — and must not accumulate an idle timer that fires
// the moment it is started for real.
func TestReaper_AMapThatIsNotUpIsNotACandidate(t *testing.T) {
	s := &fakeReapScaler{up: []string{}}
	occ := &fakeOccupancy{counts: map[string]int{}}
	r, c := newTestReaper(s, occ, nil)

	r.Tick()
	c.advance(time.Hour)
	// now it gets started by the watcher, and is empty for one second
	s.up = []string{pit}
	occ.counts[pit] = 0
	r.Tick()
	if len(s.stopped) != 0 {
		t.Errorf("reaped a map one second after it started: %v", s.stopped)
	}
}

func TestReaper_KeepsGoingAfterAScalerError(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit, "CB_Dungeon_OldCarthag"}, err: errors.New("nope")}
	occ := &fakeOccupancy{counts: map[string]int{pit: 0, "CB_Dungeon_OldCarthag": 0}}
	r, c := newTestReaper(s, occ, nil)
	r.Tick()
	c.advance(11 * time.Minute)
	r.Tick() // both fail
	s.err = nil
	c.advance(time.Minute)
	r.Tick()
	if len(s.stopped) != 2 {
		t.Errorf("stopped = %v, want both once the scaler recovered", s.stopped)
	}
}

// Disabled means disabled: an AutomaticStopDuration of zero must not reap
// instantly, it must not reap at all.
func TestReaper_ZeroDurationDisablesReaping(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	r, c := newTestReaper(s, &fakeOccupancy{counts: map[string]int{pit: 0}}, nil)
	r.idleAfter = 0
	r.Tick()
	c.advance(time.Hour)
	r.Tick()
	if len(s.stopped) != 0 {
		t.Errorf("reaped with reaping disabled: %v", s.stopped)
	}
}

// --- making room at the cap (#136) -------------------------------------------

func TestReaper_FreeSlotStopsTheLongestEmptyMap(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit, bandit, "SH_HarkoVillage"}}
	o := &fakeOccupancy{counts: map[string]int{pit: 0, bandit: 0, "SH_HarkoVillage": 2}}
	r, c := newTestReaper(s, o, nil)
	r.Tick() // bandit and pit empty from now
	c.advance(90 * time.Second)
	freed, err := r.FreeSlot("SH_Arrakeen")
	if err != nil || freed == "" {
		t.Fatalf("FreeSlot = %q, %v; want an empty map stopped", freed, err)
	}
	if freed == "SH_HarkoVillage" {
		t.Fatal("stopped a map with players on it")
	}
	if len(s.stopped) != 1 || s.stopped[0] != freed {
		t.Fatalf("stopped %v, want exactly %s", s.stopped, freed)
	}
}

// A player who dropped mid-mission comes back within the grace period: an
// instance that emptied a moment ago is not up for grabs.
func TestReaper_FreeSlotSparesAJustEmptiedMap(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	r, c := newTestReaper(s, &fakeOccupancy{counts: map[string]int{pit: 0}}, nil)
	r.Tick()
	c.advance(20 * time.Second)
	if freed, _ := r.FreeSlot("SH_Arrakeen"); freed != "" || len(s.stopped) != 0 {
		t.Fatalf("freed %q (stopped %v) 20 s after it emptied", freed, s.stopped)
	}
}

func TestReaper_FreeSlotNeverTouchesWarmMapsOrTheRequestedOne(t *testing.T) {
	s := &fakeReapScaler{up: []string{"Survival_1", pit}}
	r, c := newTestReaper(s, &fakeOccupancy{counts: map[string]int{"Survival_1": 0, pit: 0}}, []string{"Survival_1"})
	r.Tick()
	c.advance(5 * time.Minute)
	if freed, _ := r.FreeSlot(pit); freed != "" {
		t.Fatalf("freed %q: the warm map and the map being asked for are both off limits", freed)
	}
}

func TestReaper_FreeSlotHoldsOnUnknownOccupancy(t *testing.T) {
	s := &fakeReapScaler{up: []string{pit}}
	o := &fakeOccupancy{counts: map[string]int{pit: 0}}
	r, c := newTestReaper(s, o, nil)
	r.Tick()
	c.advance(5 * time.Minute)
	o.err = errors.New("psql down")
	if freed, err := r.FreeSlot("SH_Arrakeen"); err == nil || freed != "" || len(s.stopped) != 0 {
		t.Fatalf("FreeSlot = %q, %v with unknown occupancy; want an error and nothing stopped", freed, err)
	}
}
