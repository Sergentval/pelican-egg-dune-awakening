package traveldemand

import (
	"log/slog"
	"sync"
	"time"
)

// The reaper is the other half of the watcher.
//
// The watcher starts a map when a player asks to travel there. Until this
// existed, nothing ever stopped one again — and MaxConcurrentInstances is a
// budget over EVERY tracked instance, always-warm maps included. A reporter's
// server keeps six maps warm out of a budget of eight, so two on-demand slots
// existed, the first two dungeons anyone visited took them, and no dungeon
// could ever start again:
//
//	traveldemand: at the instance cap, refusing to start a map a player asked
//	for map=Story_ArtOfKanly live=8 max=8
//
// AutomaticStopDuration sat in ondemand.ini the whole time — parsed, logged at
// boot, applied by nobody. This applies it.

// ReapScaler is the spawner side of stopping a map.
type ReapScaler interface {
	// ScaledUpMaps is the map names currently above zero replicas.
	ScaledUpMaps() []string
	// ScaleToZero stops a map. Idempotent.
	ScaleToZero(mapName string) error
}

// Occupancy reports how many players are connected to each map.
type Occupancy interface {
	// PlayerCounts returns the live per-map count. An error means UNKNOWN,
	// never empty — the reaper holds rather than acting on it.
	PlayerCounts() (map[string]int, error)
}

// Reaper stops on-demand maps that have sat empty for AutomaticStopDuration.
type Reaper struct {
	// mu serializes Tick (the reaper loop) and FreeSlot (called from the
	// travel watcher's goroutine): both read and write emptySince.
	mu sync.Mutex

	scaler     ReapScaler
	occupancy  Occupancy
	idleAfter  time.Duration
	alwaysWarm map[string]bool

	// emptySince is when each map was last seen with nobody on it. A map that
	// is not up, or has players, has no entry — so a freshly started map
	// cannot inherit an old timer and die seconds after it comes up.
	emptySince map[string]time.Time
	lastReason string
	now        func() time.Time

	// ends, when set, stops finished story instances early (see storyend.go).
	ends *StoryEnd
}

// WithStoryEnd enables the story-end guard on this reaper.
func (r *Reaper) WithStoryEnd(s *StoryEnd) *Reaper {
	r.ends = s
	return r
}

// NewReaper builds a reaper. idleAfter <= 0 disables reaping entirely.
// alwaysWarm maps are the operator's floor and are never touched — they are
// why the budget is tight, but they are not the reaper's to reclaim.
func NewReaper(scaler ReapScaler, occ Occupancy, idleAfter time.Duration, alwaysWarm []string) *Reaper {
	warm := make(map[string]bool, len(alwaysWarm))
	for _, m := range alwaysWarm {
		warm[m] = true
	}
	return &Reaper{
		scaler:     scaler,
		occupancy:  occ,
		idleAfter:  idleAfter,
		alwaysWarm: warm,
		emptySince: map[string]time.Time{},
		now:        time.Now,
	}
}

// Tick stops every on-demand map that has been empty for long enough. Never
// returns an error: it runs in a loop beside a live server, and one unreadable
// poll must not stop the next.
func (r *Reaper) Tick() {
	if r.idleAfter <= 0 {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	counts, err := r.occupancy.PlayerCounts()
	if err != nil {
		// Hold. A failed query read as "zero everywhere" would reap every
		// instance on the server at once.
		if reason := err.Error(); reason != r.lastReason {
			r.lastReason = reason
			slog.Warn("traveldemand: cannot read player counts, holding every instance", "err", err)
		}
		return
	}
	r.lastReason = ""

	up := r.scaler.ScaledUpMaps()
	live := make(map[string]bool, len(up))
	for _, m := range up {
		live[m] = true
	}
	// Forget maps that are no longer up, so one that is started again later
	// begins its idle countdown from scratch.
	for m := range r.emptySince {
		if !live[m] {
			delete(r.emptySince, m)
		}
	}

	now := r.now()
	for _, mapName := range up {
		if r.alwaysWarm[mapName] {
			delete(r.emptySince, mapName)
			continue
		}
		if r.stopFinishedStory(mapName, counts[mapName]) {
			continue
		}
		if counts[mapName] > 0 {
			// Somebody is there. Any countdown restarts from when they leave.
			delete(r.emptySince, mapName)
			continue
		}
		since, seen := r.emptySince[mapName]
		if !seen {
			r.emptySince[mapName] = now
			continue
		}
		idle := now.Sub(since)
		if idle < r.idleAfter {
			continue
		}
		if err := r.scaler.ScaleToZero(mapName); err != nil {
			slog.Error("traveldemand: could not stop an idle map",
				"map", mapName, "idle", idle.Round(time.Second), "err", err)
			continue // keep the timer: retry on the next tick
		}
		delete(r.emptySince, mapName)
		slog.Info("traveldemand: stopping a map nobody is on, freeing an instance slot",
			"map", mapName, "idle", idle.Round(time.Second))
	}
}

// freeSlotMinEmpty is how long an instance must have been empty before the
// watcher may stop it to make room. A player who dropped mid-mission comes
// back within the grace period; a map that emptied a moment ago is theirs.
const freeSlotMinEmpty = time.Minute

// FreeSlot stops the on-demand map that has been empty the longest (at least
// freeSlotMinEmpty) to make room for exclude, and returns it; "" when there is
// none. Never touches an always-warm map or exclude. An unreadable player
// count is an error and stops nothing: unknown is not empty.
func (r *Reaper) FreeSlot(exclude string) (string, error) {
	counts, err := r.occupancy.PlayerCounts()
	if err != nil {
		return "", err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	best, bestSince := "", time.Time{}
	for _, m := range r.scaler.ScaledUpMaps() {
		if m == exclude || r.alwaysWarm[m] || counts[m] > 0 {
			continue
		}
		since, seen := r.emptySince[m]
		if !seen || now.Sub(since) < freeSlotMinEmpty {
			continue
		}
		if best == "" || since.Before(bestSince) {
			best, bestSince = m, since
		}
	}
	if best == "" {
		return "", nil
	}
	if err := r.scaler.ScaleToZero(best); err != nil {
		return "", err
	}
	delete(r.emptySince, best)
	if r.ends != nil {
		r.ends.Forget(best)
	}
	return best, nil
}

// stopFinishedStory stops mapName if the story-end guard says its story
// ended and was abandoned (and nobody is on it) or is looping on its player.
// Reports whether it stopped the map.
func (r *Reaper) stopFinishedStory(mapName string, players int) bool {
	if r.ends == nil {
		return false
	}
	v := r.ends.Check(mapName)
	if !v.StopNow && !(v.StopIfEmpty && players == 0) {
		return false
	}
	if err := r.scaler.ScaleToZero(mapName); err != nil {
		slog.Error("traveldemand: could not stop a finished story instance", "map", mapName, "err", err)
		return false
	}
	r.ends.Forget(mapName)
	delete(r.emptySince, mapName)
	slog.Info("traveldemand: stopping a finished story instance so its player is not sent back into it",
		"map", mapName, "reason", v.Why, "players", players)
	return true
}

// Run ticks until done. interval <= 0 disables the reaper.
func (r *Reaper) Run(done <-chan struct{}, interval time.Duration) {
	if interval <= 0 || r.idleAfter <= 0 {
		slog.Info("traveldemand: reaper disabled", "interval", interval, "idle_after", r.idleAfter)
		return
	}
	slog.Info("traveldemand: reaping maps nobody is on",
		"interval", interval, "idle_after", r.idleAfter, "always_warm", len(r.alwaysWarm))
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			r.Tick()
		}
	}
}
