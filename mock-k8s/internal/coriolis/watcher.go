// Package coriolis restarts Deep Desert servers after each Coriolis cycle
// boundary (issue #119).
//
// Dune applies a new Coriolis cycle only when a server BOOTS: the seed and
// cycle dates are read at startup, and the map wipe runs in the database
// function coriolis_update_seed, which each server calls for its own map. A
// Deep Desert that is up across the boundary therefore keeps the old layout
// until someone restarts it. Our test server's log of the 2026-06-02 cycle
// shows it: the storm ran, 05:00 UTC passed with the server up and nothing
// logged, and only the 07:32 boot printed
//
//	LogCoriolis: Display: Current Coriolis World Seed: 3
//	LogCoriolis: Display: This Coriolis Cycle start date UTC: 2026.06.02-05.00.00
//
// Funcom's battlegroup operator restarts servers on a schedule; mock-k8s
// replaces that operator, so this watcher does the one restart the cycle
// needs. It reads the boundary from each instance's own boot line
//
//	LogCoriolis: Display: Next Coriolis Cycle start date UTC: 2026.06.02-05.00.00
//
// rather than hard-coding "Tuesday 05:00", so it follows whatever cycle the
// game computes.
package coriolis

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	nextCycleMarker = "Next Coriolis Cycle start date UTC: "
	cycleLayout     = "2006.01.02-15.04.05"

	// retryCooldown spaces out attempts on one server after a failed
	// recycle, so a server that refuses to stop is not hammered every tick.
	retryCooldown = 30 * time.Minute
	// staggerBound is how long the next recycle waits for the previous
	// replacement to come back before giving up on it. A Deep Desert boot
	// takes about 40 s; this only has to be generous, not tight.
	staggerBound = 15 * time.Minute
)

// Target is one Deep Desert server the watcher may restart.
type Target struct {
	// Group names the server slot across a restart: the replacement may run
	// under a new pid (and, for mock-k8s, a new pool suffix), but it belongs
	// to the same group. The stagger waits on it.
	Group string
	// LogPath is the server's UE5 log, which carries its boundary line. It is
	// also the identity for "already recycled for this boundary".
	LogPath string
	// Live is true once the server has a running pid. A server still booting
	// may have a log that ends with the PREVIOUS boot's lines.
	Live bool

	key, suffix string // spawner targets
	partition   string // dimension targets
}

// Source lists the servers it manages and restarts one of them.
type Source interface {
	Targets() []Target
	// Recycle restarts t and reports whether a replacement was started.
	Recycle(t Target) (respawned bool, err error)
}

type candidate struct {
	src Source
	t   Target
}

// Watcher restarts Deep Desert servers once their Coriolis boundary has
// passed.
type Watcher struct {
	sources []Source
	delay   time.Duration
	now     func() time.Time

	tails     map[string]*tail     // by log path
	acted     map[string]time.Time // log path -> boundary already recycled for
	attempted map[string]time.Time // log path -> last recycle attempt
	warned    map[string]bool      // log path -> "no cycle line" already logged

	// waitGroup is the server slot whose replacement the next recycle waits
	// for, so the Deep Desert servers restart one at a time.
	waitGroup string
	waitSince time.Time
}

// New builds a watcher over sources. delay is how long after the boundary to
// act: the storm's own end-of-cycle handling runs at the boundary, and
// restarting in the same second would race it.
func New(sources []Source, delay time.Duration) *Watcher {
	return &Watcher{
		sources:   append([]Source(nil), sources...),
		delay:     delay,
		now:       time.Now,
		tails:     map[string]*tail{},
		acted:     map[string]time.Time{},
		attempted: map[string]time.Time{},
		warned:    map[string]bool{},
	}
}

// Run ticks until stop closes. interval <= 0 disables the watcher.
func (w *Watcher) Run(stop <-chan struct{}, interval time.Duration) {
	if interval <= 0 {
		slog.Info("coriolis: recycle watcher disabled")
		return
	}
	slog.Info("coriolis: recycle watcher started", "interval", interval, "delay", w.delay)
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			w.Tick()
		}
	}
}

// Tick recycles at most one server whose boundary has passed. Never returns
// an error: it runs beside a live server, and one bad poll must not stop the
// next.
func (w *Watcher) Tick() {
	now := w.now()
	var cands []candidate
	for _, src := range w.sources {
		for _, t := range src.Targets() {
			cands = append(cands, candidate{src: src, t: t})
		}
	}
	sort.Slice(cands, func(i, j int) bool { return cands[i].t.LogPath < cands[j].t.LogPath })

	// Keep every tail current, even for servers we will not act on this tick,
	// so a fresh boot's line is never mistaken for an old one later.
	for _, c := range cands {
		w.tailFor(c.t.LogPath).refresh()
	}

	if w.waitingForReplacement(now, cands) {
		return
	}
	for _, c := range cands {
		if w.maybeRecycle(now, c) {
			return
		}
	}
}

// waitingForReplacement reports whether the previous recycle's replacement is
// still booting (and within staggerBound).
func (w *Watcher) waitingForReplacement(now time.Time, cands []candidate) bool {
	if w.waitGroup == "" {
		return false
	}
	for _, c := range cands {
		if c.t.Group == w.waitGroup && c.t.Live {
			w.waitGroup = ""
			return false
		}
	}
	if now.Sub(w.waitSince) > staggerBound {
		slog.Warn("coriolis: recycled server has not come back; moving on",
			"group", w.waitGroup, "waited", now.Sub(w.waitSince).Round(time.Second))
		w.waitGroup = ""
		return false
	}
	return true
}

// maybeRecycle recycles c if its boundary has passed and it has not been
// recycled for that boundary yet. Reports whether it attempted a recycle.
func (w *Watcher) maybeRecycle(now time.Time, c candidate) bool {
	path := c.t.LogPath
	if !c.t.Live {
		return false
	}
	next := w.tails[path].next
	if next.IsZero() {
		if !w.warned[path] {
			w.warned[path] = true
			slog.Warn("coriolis: no cycle line in this server's log; it will not be recycled at the boundary",
				"group", c.t.Group, "log", path)
		}
		return false
	}
	if now.Before(next.Add(w.delay)) || w.acted[path].Equal(next) {
		return false
	}
	if last, ok := w.attempted[path]; ok && now.Sub(last) < retryCooldown {
		return false
	}

	w.attempted[path] = now
	slog.Info("coriolis: cycle boundary passed while the server was up; restarting it to apply the new cycle",
		"group", c.t.Group, "log", filepath.Base(path), "boundary", next.Format(time.RFC3339))
	respawned, err := c.src.Recycle(c.t)
	if err != nil {
		slog.Warn("coriolis: recycle failed; will retry later",
			"group", c.t.Group, "retry_after", retryCooldown, "err", err)
		return true
	}
	w.acted[path] = next
	if respawned {
		w.waitGroup = c.t.Group
		w.waitSince = now
	}
	return true
}

func (w *Watcher) tailFor(path string) *tail {
	t, ok := w.tails[path]
	if !ok {
		t = &tail{path: path}
		w.tails[path] = t
	}
	return t
}

// tail follows one UE5 log and remembers the last Coriolis boundary it printed.
type tail struct {
	path    string
	offset  int64
	next    time.Time
	lastErr string
}

// refresh scans new lines and logs a read error once per distinct error.
func (t *tail) refresh() {
	if _, err := t.scan(); err != nil {
		if msg := err.Error(); msg != t.lastErr {
			t.lastErr = msg
			slog.Warn("coriolis: cannot read instance log", "log", t.path, "err", err)
		}
		return
	}
	t.lastErr = ""
}

// scan reads the lines appended since the last scan and reports whether a new
// boundary was seen. A file that SHRANK was trimmed in place by
// rotate-logs.sh: it is re-read from the start, but the boundary already known
// is kept, because the trim may have removed the only line that carried it. A
// missing file is not an error; the instance may not have logged yet. A final
// line without its newline is still being written and is left for next time.
func (t *tail) scan() (bool, error) {
	f, err := os.Open(t.path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return false, err
	}
	if info.Size() < t.offset {
		t.offset = 0
	}
	if info.Size() == t.offset {
		return false, nil
	}
	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		return false, err
	}

	marker := []byte(nextCycleMarker)
	r := bufio.NewReaderSize(f, 64*1024)
	updated := false
	for {
		line, err := r.ReadBytes('\n')
		if errors.Is(err, io.EOF) {
			return updated, nil // any partial line stays unread
		}
		if err != nil {
			return updated, err
		}
		t.offset += int64(len(line))
		if !bytes.Contains(line, marker) {
			continue
		}
		if ts, ok := parseNextCycle(string(line)); ok {
			t.next = ts
			updated = true
		}
	}
}

// parseNextCycle extracts the boundary from a "Next Coriolis Cycle start
// date UTC" line. The game prints it in UTC.
func parseNextCycle(line string) (time.Time, bool) {
	i := strings.Index(line, nextCycleMarker)
	if i < 0 {
		return time.Time{}, false
	}
	ts, err := time.ParseInLocation(cycleLayout, strings.TrimSpace(line[i+len(nextCycleMarker):]), time.UTC)
	if err != nil {
		return time.Time{}, false
	}
	return ts, true
}
