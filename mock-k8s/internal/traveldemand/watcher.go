// Package traveldemand starts an instanced map when a player asks to travel
// there.
//
// The Battlegroup Director routes a player to a ClassicalInstancing group only
// if a server for that group already exists — its queue reads
// "(servers: [], num: 0)" otherwise — and it never creates the first one. On a
// stock self-host nothing else did either, so travel to a mission or a hub sat
// in the queue until TravelRequestExpirationTimeSeconds (300s) killed it. That
// is the five-minute "In Queue" a reporter measured on 2026-09-20.
//
// The trigger was in the Director's own log the whole time:
//
//	Received travel request for 1 player(s) to CB_Story_BanditFortress01 (instancingMode=ClassicalInstancing)
//
// Watching for that line and scaling the map to one replica closes the loop: a
// cold instance is ready in about ten seconds, well inside the expiry.
package traveldemand

import (
	"io"
	"log/slog"
	"os"
	"regexp"
	"time"
)

// requestRe matches the Director's travel-request line. Only
// ClassicalInstancing: Dimension (Survival_1, Deep Desert) and SingleServer
// (Overmap) maps are owned by AlwaysWarm and by start-ue5-dimensions.sh, and
// scaling them from here would fight those owners.
var requestRe = regexp.MustCompile(
	`Received travel request for \d+ player\(s\) to ([A-Za-z0-9_]+) \(instancingMode=ClassicalInstancing\)`)

// safeMapName is the same shape the spawner refuses to run without: the name
// becomes part of a resource name and a process argument, so '.', '/' and ';'
// must be unrepresentable rather than merely unexpected.
var safeMapName = regexp.MustCompile(`^[A-Za-z0-9_]+$`)

// Parse returns the instanced maps a chunk of Director log asked to travel to,
// in first-seen order and without repeats.
func Parse(chunk string) []string {
	var out []string
	seen := map[string]bool{}
	for _, m := range requestRe.FindAllStringSubmatch(chunk, -1) {
		name := m[1]
		if !safeMapName.MatchString(name) || seen[name] {
			continue
		}
		seen[name] = true
		out = append(out, name)
	}
	return out
}

// Tailer reads the bytes appended to a file since the last call.
type Tailer struct {
	path   string
	offset int64
}

// NewTailer starts at the END of whatever the file already holds.
//
// This matters more than it looks. director.log is append-only across restarts
// and grows to tens of megabytes; every travel request the server has ever
// served is still in there. A tailer starting at offset 0 would read that whole
// history on its first tick and start a map for every destination anyone had
// ever travelled to — on our own test box, a restart brought up a mission
// instance from a request eight minutes dead. Only what is written from now on
// describes a player who is actually waiting.
//
// A file that does not exist yet is left at offset 0 on purpose: the Director
// has not started, so when the file appears everything in it is new.
func NewTailer(path string) *Tailer {
	t := &Tailer{path: path}
	if info, err := os.Stat(path); err == nil {
		t.offset = info.Size()
	}
	return t
}

// Read returns everything written since the previous Read.
//
// A file that has SHRUNK is re-read from the start. rotate-logs.sh trims logs
// in place — the writers hold them O_APPEND, so truncation is the only form of
// rotation that works here — and a tailer that kept its offset across that
// would read nothing ever again, going silently deaf on exactly the busy
// servers whose logs get trimmed.
//
// A missing file is not an error: the Director may not have started yet.
func (t *Tailer) Read() (string, error) {
	f, err := os.Open(t.path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if info.Size() < t.offset {
		t.offset = 0
	}
	if info.Size() == t.offset {
		return "", nil
	}
	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		return "", err
	}
	buf, err := io.ReadAll(f)
	if err != nil {
		return "", err
	}
	t.offset += int64(len(buf))
	return string(buf), nil
}

// Scaler is what the watcher acts through — the spawner side, kept behind an
// interface so the decision logic is testable without a battlegroup.
type Scaler interface {
	// LiveInstances is the number of UE5 processes currently tracked.
	LiveInstances() int
	// IsUp reports whether mapName already has an instance, so a request for
	// it needs no new slot.
	IsUp(mapName string) bool
	// ScaleToOne raises a map to at least one replica and reports whether this
	// call is what started it. Idempotent: a map that already has one must not
	// be disturbed, and returns started=false.
	ScaleToOne(mapName string) (started bool, err error)
}

// Watcher polls the Director's log and scales the maps players ask for.
type Watcher struct {
	tailer     *Tailer
	scaler     Scaler
	maxLive    int
	lastReason string

	// At the cap (#136): make room from an instance nobody is on, and say so
	// on the server when there is none. Both optional.
	freer    SlotFreer
	announce Announcer
}

// SlotFreer stops an on-demand instance nobody is on, to make room.
type SlotFreer interface {
	// FreeSlot stops one such instance other than exclude and returns its
	// map, or "" when there is none to stop. An error means the player
	// counts could not be read: nothing was stopped.
	FreeSlot(exclude string) (string, error)
}

// Announcer tells the players that a destination could not be started.
type Announcer interface {
	AtCapacity(mapName string)
}

// WithCapacity sets what the watcher does at the instance cap.
func (w *Watcher) WithCapacity(f SlotFreer, a Announcer) *Watcher {
	w.freer, w.announce = f, a
	return w
}

// New builds a watcher over logPath. maxLive caps how many instances may be
// running before demand is refused — MaxConcurrentInstances from ondemand.ini,
// which until now was parsed and logged but never enforced.
func New(logPath string, scaler Scaler, maxLive int) *Watcher {
	return &Watcher{tailer: NewTailer(logPath), scaler: scaler, maxLive: maxLive}
}

// Tick reads whatever the Director has written since the last call and scales
// each newly demanded map. Never returns an error: it runs in a loop beside a
// live server, and one unreadable read must not stop the next one.
func (w *Watcher) Tick() {
	chunk, err := w.tailer.Read()
	if err != nil {
		// Log once per distinct reason: a permanently unreadable log would
		// otherwise fill the very file we are reading.
		if reason := err.Error(); reason != w.lastReason {
			w.lastReason = reason
			slog.Warn("traveldemand: cannot read the Director log", "err", err)
		}
		return
	}
	w.lastReason = ""
	for _, mapName := range Parse(chunk) {
		// A map already running needs no new slot, whatever the count: several
		// players travelling to the same mission is the common case.
		if !w.scaler.IsUp(mapName) && !w.haveRoomFor(mapName) {
			continue
		}
		started, err := w.scaler.ScaleToOne(mapName)
		if err != nil {
			slog.Error("traveldemand: could not start the map a player asked for",
				"map", mapName, "err", err)
			continue
		}
		if !started {
			// The common case once a map is up: several players travel to the
			// same mission. Worth a line — it says the request was seen and
			// needed nothing — but not one that claims a start.
			slog.Debug("traveldemand: map a player asked for is already up", "map", mapName)
			continue
		}
		slog.Info("traveldemand: starting a map on a player's travel request", "map", mapName)
	}
}

// haveRoomFor reports whether a new instance for mapName fits under the cap,
// making room from an instance nobody is on if it has to. When it cannot, the
// refusal is logged and announced: the Director would otherwise hold the
// player on "Connecting to ..." until the request expires, with no word why.
func (w *Watcher) haveRoomFor(mapName string) bool {
	live := w.scaler.LiveInstances()
	if w.maxLive <= 0 || live < w.maxLive {
		return true
	}
	if w.freer != nil {
		freed, err := w.freer.FreeSlot(mapName)
		switch {
		case err != nil:
			slog.Warn("traveldemand: at the instance cap and cannot read player counts to make room",
				"map", mapName, "err", err)
		case freed != "":
			slog.Info("traveldemand: at the instance cap, stopped an instance nobody was on to make room",
				"map", mapName, "stopped", freed)
			if live = w.scaler.LiveInstances(); live < w.maxLive {
				return true
			}
		}
	}
	slog.Warn("traveldemand: at the instance cap, refusing to start a map a player asked for",
		"map", mapName, "live", live, "max", w.maxLive)
	if w.announce != nil {
		w.announce.AtCapacity(mapName)
	}
	return false
}

// Run ticks until ctx is done. interval <= 0 disables the watcher.
func (w *Watcher) Run(done <-chan struct{}, interval time.Duration) {
	if interval <= 0 {
		slog.Info("traveldemand: watcher disabled (interval <= 0)")
		return
	}
	slog.Info("traveldemand: watching the Director log for travel requests", "interval", interval)
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-done:
			return
		case <-t.C:
			w.Tick()
		}
	}
}
