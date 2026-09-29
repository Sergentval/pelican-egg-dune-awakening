package traveldemand

import (
	"fmt"
	"log/slog"
	"sync"
	"time"
)

// CapAnnouncer broadcasts, server-wide, that a destination could not be
// started because every instance slot is in use. The game has no per-player
// message (admin_welcome.py documents it), so this is the only way to tell
// the waiting player anything; rate limits keep it from becoming noise: one
// broadcast a minute server-wide, and the same map at most every 5 minutes.
type CapAnnouncer struct {
	maxLive int
	send    func(title, body string) error
	now     func() time.Time

	mu        sync.Mutex
	lastAny   time.Time
	lastByMap map[string]time.Time
}

const (
	announceEvery        = time.Minute
	announceSameMapEvery = 5 * time.Minute
)

// NewCapAnnouncer builds an announcer; send delivers one broadcast.
func NewCapAnnouncer(maxLive int, send func(title, body string) error) *CapAnnouncer {
	return &CapAnnouncer{maxLive: maxLive, send: send, now: time.Now, lastByMap: map[string]time.Time{}}
}

func (a *CapAnnouncer) AtCapacity(mapName string) {
	a.mu.Lock()
	now := a.now()
	if now.Sub(a.lastAny) < announceEvery {
		a.mu.Unlock()
		return
	}
	if last, ok := a.lastByMap[mapName]; ok && now.Sub(last) < announceSameMapEvery {
		a.mu.Unlock()
		return
	}
	a.lastAny, a.lastByMap[mapName] = now, now
	a.mu.Unlock()

	body := fmt.Sprintf("A destination could not be started: the server is running its maximum of %d instances. "+
		"If you are stuck on \"Connecting\", cancel and try again in a few minutes.", a.maxLive)
	if err := a.send("Server busy", body); err != nil {
		slog.Warn("traveldemand: could not broadcast the instance-cap notice", "map", mapName, "err", err)
	}
}
