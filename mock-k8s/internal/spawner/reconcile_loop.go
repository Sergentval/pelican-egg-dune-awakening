package spawner

import (
	"log/slog"
	"os"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
)

const (
	baseBackoff = time.Minute
	maxBackoff  = 15 * time.Minute
)

// recordFailure increments a map's consecutive-failure count and sets the next
// eligible retry time. The first failure has no delay (retry next tick); each
// further failure doubles the delay (1m, 2m, 4m, …) capped at maxBackoff.
func (s *Spawner) recordFailure(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	bs := s.backoff[key]
	bs.failures++
	bs.nextRetry = s.backoffUntilLocked(bs.failures)
	s.backoff[key] = bs
}

// backoffUntilLocked returns the next-retry time for the given failure count.
// Caller holds s.mu (it reads s.now()). A failure count <= 1 means "no delay".
func (s *Spawner) backoffUntilLocked(failures int) time.Time {
	if failures <= 1 {
		return time.Time{}
	}
	delay := baseBackoff << (failures - 2) // base * 2^(failures-2)
	if delay <= 0 || delay > maxBackoff {  // <=0 guards int64 shift overflow
		delay = maxBackoff
	}
	return s.now().Add(delay)
}

// inBackoff reports whether a map is currently within its crash-loop backoff
// window and must not be respawned yet.
func (s *Spawner) inBackoff(key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	bs, ok := s.backoff[key]
	return ok && bs.failures > 1 && s.now().Before(bs.nextRetry)
}

// resetBackoff clears a map's backoff state (called once a respawn survives a
// clean tick).
func (s *Spawner) resetBackoff(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.backoff, key)
}

// aliveAs reports whether pid is still the same process incarnation recorded by
// startTime; a zero start-time falls back to a plain liveness probe (legacy /
// restored instances).
func aliveAs(pid int, startTime uint64) bool {
	if startTime == 0 {
		return proc.Alive(pid)
	}
	return proc.SameProcess(pid, startTime)
}

// isChanClosed non-blockingly reports whether ch (only ever closed, never sent
// to) is closed.
func isChanClosed(ch chan struct{}) bool {
	if ch == nil {
		return false
	}
	select {
	case <-ch:
		return true
	default:
		return false
	}
}

// sweep reaps tracked instances whose process is gone (a crashed UE5) or whose
// spawn failed (pid never captured), releasing their slots and recording a
// failure for backoff. Live and still-starting instances are left alone.
// Returns the set of map keys that had at least one reap.
func (s *Spawner) sweep() map[string]bool {
	// Phase 1: snapshot identities under s.mu (no syscalls under the lock).
	type probe struct {
		key        string
		allocIndex int
		pid        int
		startTime  uint64
		phantom    bool
		pidPath    string
	}
	var probes []probe
	s.mu.Lock()
	for key, list := range s.instances {
		for _, in := range list {
			probes = append(probes, probe{
				key:        key,
				allocIndex: in.Allocation.Index,
				pid:        in.PID,
				startTime:  in.StartTime,
				phantom:    in.PID == 0 && isChanClosed(in.pidReady),
				pidPath:    s.pidPath(in.MapName, in.Suffix),
			})
		}
	}
	s.mu.Unlock()

	// Phase 2: classify outside the lock.
	var dead []probe
	for _, p := range probes {
		if p.pid > 0 {
			if !aliveAs(p.pid, p.startTime) {
				dead = append(dead, p)
			}
		} else if p.phantom {
			dead = append(dead, p)
		}
	}
	if len(dead) == 0 {
		return nil
	}

	// Phase 3: remove dead under s.mu (match by pool index + pid), count,
	// record, and collect the entries we actually removed.
	reaped := make(map[string]bool, len(dead))
	var confirmed []probe
	s.mu.Lock()
	for _, d := range dead {
		list := s.instances[d.key]
		for i := range list {
			if list[i].Allocation.Index == d.allocIndex && list[i].PID == d.pid {
				s.instances[d.key] = append(list[:i:i], list[i+1:]...)
				s.reapedTotal++
				reaped[d.key] = true
				confirmed = append(confirmed, d)
				break
			}
		}
	}
	s.mu.Unlock()

	// Phase 4: side effects + backoff outside s.mu — only for entries we
	// actually removed, so each slot is released exactly once.
	for _, d := range confirmed {
		s.pool.Release(d.allocIndex)
		_ = os.Remove(d.pidPath)
	}
	for key := range reaped {
		s.recordFailure(key)
	}
	if len(confirmed) > 0 {
		s.persist()
	}
	return reaped
}

// reconcileUpLocked spawns instances for obj until current == desired (only
// when desired > current). Caller MUST hold s.reconcileMu. When respectBackoff
// is true and the map is in crash-loop backoff, it spawns nothing. Returns the
// number spawned.
func (s *Spawner) reconcileUpLocked(obj serversetscale.Object, respectBackoff bool) int {
	key := obj.Metadata.Namespace + "/" + obj.Metadata.Name
	mapName, _ := obj.Spec["mapName"].(string)
	if mapName == "" {
		mapName, _ = obj.Spec["map"].(string)
	}
	if !safeInstanceName(mapName) {
		slog.Error("spawner: refusing ServerSetScale with unsafe map name", "key", key, "map", mapName)
		return 0
	}
	desired := readReplicas(obj.Spec)
	partitionID := readPartitionID(obj.Spec)

	s.mu.Lock()
	current := len(s.instances[key])
	s.mu.Unlock()
	if desired <= current {
		return 0
	}
	if respectBackoff && s.inBackoff(key) {
		return 0
	}
	for i := current; i < desired; i++ {
		s.spawnOne(obj, mapName, partitionID, i)
	}
	return desired - current
}

// currentCount returns the number of instances tracked for key.
func currentCount(s *Spawner, key string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.instances[key])
}
