package traveldemand

import (
	"os"
	"path/filepath"
	"testing"
)

// Real lines from the reporters' server, 2026-09-26 (trimmed to the parts the
// guard reads).
const (
	lineBoot        = "LogInit: Log: Command Line: CB_Story_OrbitalMonitor -log -unattended\n"
	lineCompleted   = "[2026.09.26-05.07.37:724][  1][870][32]LogDungeonScaling: Display: Dungeon None completed at difficulty 1. It took 1958.270000 (s)\n"
	lineTravelOut   = "[2026.09.04-08.26.59:574][  1][870][30]LogTravel: Display: [D0D7]: Telling [BP_DunePlayerController_C_2147479925] to client travel to [location=X=22636.000 Y=-14506.000 Z=450.000, map=HaggaBasin]\n"
	lineGraceLeft   = "[2026.09.26-05.10.33:692][  1][870][32]LogTravelEvent: Display: [Success] FlowType:\"Login\", Stage:\"End\", PlayerId:\"1A3628F67968807F\", FlowId:\"\", Reason:\"Grace Period:Disconnected from instanced map: OrbitalMonitor\"\n"
	lineReload      = "[2026.09.26-05.10.33:889][  1][870][32]LogLoad: Log: LoadMap: /Game/DLC/B1C4/Maps/MainStory_Chapter4/OrbitalMonitor/CB_Story_OrbitalMonitor\n"
	lineStoryLooped = "[2026.09.26-05.14.53:741][  1][870][32]LogJourneyStoryManager: Warning: virtual bool AJourneyStoryManagerBase::TryStartStory() - Story: BP_JourneyStory_Manager_FortyFears_C_1 - Cannot start a story with no pending players\n"
)

type fakeLogs map[string][]string

func (f fakeLogs) InstanceLogs(mapName string) []string { return f[mapName] }

func writeLog(t *testing.T, path string, lines ...string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	for _, l := range lines {
		if _, err := f.WriteString(l); err != nil {
			t.Fatal(err)
		}
	}
}

func newStoryEndFixture(t *testing.T) (*StoryEnd, string) {
	t.Helper()
	p := filepath.Join(t.TempDir(), "ue5-CB_Story_OrbitalMonitor-p6.log")
	return NewStoryEnd(fakeLogs{"CB_Story_OrbitalMonitor": {p}}), p
}

// The Forty Fears ending: the story completes, the credits roll, and the
// CLIENT drops the connection instead of being sent anywhere. Its saved
// location stays on the instance, so every reconnect lands back in it.
func TestStoryEnd_CompletedThenLeftWithoutTravelIsAbandoned(t *testing.T) {
	s, p := newStoryEndFixture(t)
	writeLog(t, p, lineBoot, lineCompleted, lineGraceLeft, lineReload)
	v := s.Check("CB_Story_OrbitalMonitor")
	if !v.StopIfEmpty || v.StopNow {
		t.Fatalf("verdict = %+v, want StopIfEmpty (the level reload after the disconnect must not clear it)", v)
	}
}

// Every other completion on that server was followed by the server sending
// the player on: those instances are left to the normal idle timer.
func TestStoryEnd_CompletedThenSentOnIsNormal(t *testing.T) {
	s, p := newStoryEndFixture(t)
	writeLog(t, p, lineBoot, lineCompleted, lineTravelOut, lineGraceLeft, lineReload)
	if v := s.Check("CB_Story_OrbitalMonitor"); v.StopIfEmpty || v.StopNow {
		t.Fatalf("verdict = %+v, want nothing for a normal exit", v)
	}
}

// Leaving mid-mission is what the grace period is for: the player must be
// able to come back to the instance.
func TestStoryEnd_LeftBeforeCompletionIsNormal(t *testing.T) {
	s, p := newStoryEndFixture(t)
	writeLog(t, p, lineBoot, lineGraceLeft, lineReload)
	if v := s.Check("CB_Story_OrbitalMonitor"); v.StopIfEmpty || v.StopNow {
		t.Fatalf("verdict = %+v, want nothing for a mid-mission disconnect", v)
	}
}

// The safety net: the loop itself, seen while the player is inside.
func TestStoryEnd_StoryLoopStopsNow(t *testing.T) {
	s, p := newStoryEndFixture(t)
	writeLog(t, p, lineBoot, lineStoryLooped)
	if v := s.Check("CB_Story_OrbitalMonitor"); !v.StopNow {
		t.Fatalf("verdict = %+v, want StopNow on the story loop", v)
	}
}

// A new boot of the instance starts clean: yesterday's ending must not stop
// today's run.
func TestStoryEnd_NewBootResets(t *testing.T) {
	s, p := newStoryEndFixture(t)
	writeLog(t, p, lineBoot, lineCompleted, lineGraceLeft, lineStoryLooped)
	s.Check("CB_Story_OrbitalMonitor")
	writeLog(t, p, lineBoot)
	if v := s.Check("CB_Story_OrbitalMonitor"); v.StopIfEmpty || v.StopNow {
		t.Fatalf("verdict after a new boot = %+v, want nothing", v)
	}
}

// After the reaper stops the map, a restart may be asked for before the new
// boot has written a line. Forget must clear the verdict so the fresh
// instance is not stopped on sight.
func TestStoryEnd_ForgetClearsVerdict(t *testing.T) {
	s, p := newStoryEndFixture(t)
	writeLog(t, p, lineBoot, lineCompleted, lineGraceLeft)
	if v := s.Check("CB_Story_OrbitalMonitor"); !v.StopIfEmpty {
		t.Fatalf("setup: verdict = %+v", v)
	}
	s.Forget("CB_Story_OrbitalMonitor")
	if v := s.Check("CB_Story_OrbitalMonitor"); v.StopIfEmpty || v.StopNow {
		t.Fatalf("verdict after Forget = %+v, want nothing", v)
	}
}

const orbital = "CB_Story_OrbitalMonitor"

func newGuardedReaper(t *testing.T, players int) (*Reaper, *fakeReapScaler, *fakeOccupancy, string) {
	t.Helper()
	p := filepath.Join(t.TempDir(), "ue5-CB_Story_OrbitalMonitor-p6.log")
	s := &fakeReapScaler{up: []string{orbital}}
	o := &fakeOccupancy{counts: map[string]int{orbital: players}}
	r, _ := newTestReaper(s, o, nil)
	r.WithStoryEnd(NewStoryEnd(fakeLogs{orbital: {p}}))
	return r, s, o, p
}

// The abandoned ending is stopped on the first tick that sees it empty, not
// ten minutes later: that is the whole workaround.
func TestReaper_StopsAbandonedStoryAtOnce(t *testing.T) {
	r, s, _, p := newGuardedReaper(t, 0)
	writeLog(t, p, lineBoot, lineCompleted, lineGraceLeft, lineReload)
	r.Tick()
	if len(s.stopped) != 1 || s.stopped[0] != orbital {
		t.Fatalf("stopped = %v, want the abandoned story instance stopped on the first tick", s.stopped)
	}
}

// Someone else still inside: a party member is not thrown out because their
// friend quit.
func TestReaper_AbandonedStoryWithPlayersInsideWaits(t *testing.T) {
	r, s, _, p := newGuardedReaper(t, 1)
	writeLog(t, p, lineBoot, lineCompleted, lineGraceLeft)
	r.Tick()
	if len(s.stopped) != 0 {
		t.Fatalf("stopped %v with a player still on the map", s.stopped)
	}
}

func TestReaper_StoryLoopStopsEvenWithThePlayerInside(t *testing.T) {
	r, s, _, p := newGuardedReaper(t, 1)
	writeLog(t, p, lineBoot, lineStoryLooped)
	r.Tick()
	if len(s.stopped) != 1 {
		t.Fatalf("stopped = %v, want the looping instance stopped although its player is inside", s.stopped)
	}
}

// Once stopped, the map can be asked for again; the old verdict must not
// stop the fresh instance before its first boot line.
func TestReaper_RestartedMapIsNotStoppedOnStaleVerdict(t *testing.T) {
	r, s, _, p := newGuardedReaper(t, 0)
	writeLog(t, p, lineBoot, lineCompleted, lineGraceLeft)
	r.Tick()
	s.up = []string{orbital} // a player asks for it again
	r.Tick()
	if len(s.stopped) != 1 {
		t.Fatalf("stopped = %v, want one stop only: the restarted instance was stopped on a stale verdict", s.stopped)
	}
}
