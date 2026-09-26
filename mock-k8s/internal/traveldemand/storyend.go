package traveldemand

import (
	"bytes"
	"log/slog"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/logtail"
)

// The story-end guard works around a game bug in the September 2026 update.
//
// At the end of the last main-story mission (Forty Fears, in
// CB_Story_OrbitalMonitor) the credits roll and the CLIENT drops the
// connection. Every other dungeon and story ending on the reporters' server,
// 42 of them, had the server send the player on ("Telling [...] to client
// travel to [...]") first. Without that travel the character's saved location
// stays on the instance, and the Director routes every reconnect back to it:
//
//	Travel grant issued through grace period: {"Map":"CB_Story_OrbitalMonitor", ...}
//
// where the finished story will not restart and the credits play again:
//
//	TryStartStory() - Story: BP_JourneyStory_Manager_FortyFears_C_1 - Cannot start a story with no pending players
//
// The way out is the instance NOT running. Then the Director answers "Teleport
// not allowed, returning to WorldPartition 32 (ServerId = )" and falls back to
// the Overmap. That is how the reporter got free, after our 10-minute idle
// timer finally stopped the map. This guard stops it as soon as it can:
//
//   - the story completed, then its player left without being sent anywhere,
//     and the instance is empty: stop it now instead of in ten minutes;
//   - the loop itself shows up while the player is inside: stop it now, and
//     their next connection lands on the Overmap.
//
// Leaving mid-mission is left alone: coming back to the instance is exactly
// what the grace period is for.

var (
	sigBoot       = []byte("LogInit: Log: Command Line:")
	sigCompleted  = []byte("LogDungeonScaling: Display: Dungeon ")
	sigCompleted2 = []byte(" completed at difficulty")
	sigTravelOut  = []byte("to client travel to")
	sigGraceLeft  = []byte("Grace Period:Disconnected from instanced map")
	sigReload     = []byte("LogLoad: Log: LoadMap:")
	sigLooped     = []byte("Cannot start a story with no pending players")
)

// InstanceLogSource lists the UE5 logs of a map's running instances.
type InstanceLogSource interface {
	InstanceLogs(mapName string) []string
}

// Verdict is what the guard wants done with a map.
type Verdict struct {
	StopIfEmpty bool   // the story ended and was abandoned: stop once nobody is on it
	StopNow     bool   // the story is looping on a player: stop even with them inside
	Why         string // for the log line
}

type storyState struct {
	completed    bool // a dungeon/story completion since the last (re)load
	travelledOut bool // ...and the server sent a player on after it
	abandoned    bool // completed, then left with no travel: sticky until a new boot
	looping      bool // the finished-story loop: sticky until a new boot
}

// StoryEnd reads instance logs and keeps one verdict per log.
type StoryEnd struct {
	src   InstanceLogSource
	tails map[string]*logtail.Tail
	state map[string]*storyState
	errs  map[string]string
	byMap map[string]map[string]bool // map -> every log path Check has read for it
}

func NewStoryEnd(src InstanceLogSource) *StoryEnd {
	return &StoryEnd{
		src:   src,
		tails: map[string]*logtail.Tail{},
		state: map[string]*storyState{},
		errs:  map[string]string{},
		byMap: map[string]map[string]bool{},
	}
}

// Check reads what the map's instance logs gained and returns its verdict.
// A log is read from its start the first time it is seen: instance logs are
// small, and every boot resets the state, so replaying lands on the current
// boot's.
func (s *StoryEnd) Check(mapName string) Verdict {
	var v Verdict
	for _, path := range s.src.InstanceLogs(mapName) {
		if s.byMap[mapName] == nil {
			s.byMap[mapName] = map[string]bool{}
		}
		s.byMap[mapName][path] = true
		st := s.scan(path)
		switch {
		case st.looping:
			return Verdict{StopNow: true, Why: "finished story is looping on its player"}
		case st.abandoned:
			v = Verdict{StopIfEmpty: true, Why: "story completed and its player left without being sent on"}
		}
	}
	return v
}

// Forget clears the verdicts of a map's logs. Call it after stopping the
// map: a restart can be asked for before the new boot has written its first
// line, and the stale verdict would stop the fresh instance on sight. It
// covers every log Check has seen for the map, because a stopped instance has
// already left the spawner's list by the time the caller gets here.
func (s *StoryEnd) Forget(mapName string) {
	for path := range s.byMap[mapName] {
		if st, ok := s.state[path]; ok {
			*st = storyState{}
		}
	}
}

func (s *StoryEnd) scan(path string) *storyState {
	t, ok := s.tails[path]
	if !ok {
		t = logtail.New(path)
		s.tails[path] = t
		s.state[path] = &storyState{}
	}
	st := s.state[path]
	err := t.Scan(func(line []byte) {
		switch {
		case bytes.Contains(line, sigBoot):
			*st = storyState{}
		case bytes.Contains(line, sigLooped):
			st.looping = true
		case bytes.Contains(line, sigCompleted) && bytes.Contains(line, sigCompleted2):
			st.completed, st.travelledOut = true, false
		case bytes.Contains(line, sigTravelOut):
			if st.completed {
				st.travelledOut = true
			}
		case bytes.Contains(line, sigGraceLeft):
			if st.completed && !st.travelledOut {
				st.abandoned = true
			}
		case bytes.Contains(line, sigReload):
			// The game reloads the level when a story instance empties. That
			// resets the dungeon, not what we already concluded about it.
			st.completed, st.travelledOut = false, false
		}
	})
	if err != nil {
		if msg := err.Error(); msg != s.errs[path] {
			s.errs[path] = msg
			slog.Warn("traveldemand: cannot read instance log for the story-end guard", "log", path, "err", err)
		}
	} else {
		delete(s.errs, path)
	}
	return st
}
