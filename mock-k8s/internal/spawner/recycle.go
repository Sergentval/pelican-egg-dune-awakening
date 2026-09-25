package spawner

import (
	"fmt"
	"log/slog"
	"strings"
)

// InstanceRef identifies one tracked UE5 instance for callers outside the
// spawner. PID is 0 while the instance is still starting.
type InstanceRef struct {
	Key    string // namespace/name of its ServerSetScale
	Suffix string // p<pool index>; names its pidfile and log
	PID    int
}

// InstancesOf lists the tracked instances running mapName.
func (s *Spawner) InstancesOf(mapName string) []InstanceRef {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []InstanceRef
	for key, list := range s.instances {
		for _, in := range list {
			if in.MapName == mapName {
				out = append(out, InstanceRef{Key: key, Suffix: in.Suffix, PID: in.PID})
			}
		}
	}
	return out
}

// Recycle restarts one live instance in place: it stops the UE5 process
// (SIGTERM, which saves its state) and then starts a fresh one for the same
// map. It blocks until the old process has exited, up to terminateGrace, and
// reports whether a replacement was started (false when the map's
// ServerSetScale vanished meanwhile, so nothing should be waited for).
//
// The replacement is held back until the old process is gone. Two servers on
// one partition claim the same IGW index and the incumbent dies on it, so the
// key is marked draining for the whole shutdown and reconcileUpLocked starts
// nothing for it, whether asked by the reconcile loop or by a Director patch.
//
// An instance still starting (no pid yet) is refused: it has not read any
// state worth refreshing, and stopping it mid-boot is the phantom case the
// spawner already handles elsewhere.
func (s *Spawner) Recycle(key, suffix string) (bool, error) {
	s.reconcileMu.Lock()
	s.mu.Lock()
	list := s.instances[key]
	idx := -1
	for i := range list {
		if list[i].Suffix == suffix {
			idx = i
			break
		}
	}
	if idx < 0 {
		s.mu.Unlock()
		s.reconcileMu.Unlock()
		return false, fmt.Errorf("recycle %s/%s: no such tracked instance", key, suffix)
	}
	inst := list[idx]
	if inst.PID <= 0 {
		s.mu.Unlock()
		s.reconcileMu.Unlock()
		return false, fmt.Errorf("recycle %s/%s: instance is still starting", key, suffix)
	}
	s.instances[key] = append(list[:idx:idx], list[idx+1:]...)
	s.draining[key]++
	s.mu.Unlock()
	s.reconcileMu.Unlock()
	s.persist()

	slog.Info("spawner: recycling UE5", "key", key, "suffix", suffix, "pid", inst.PID)
	stopped := s.teardown(key, inst)

	s.reconcileMu.Lock()
	defer s.reconcileMu.Unlock()
	s.mu.Lock()
	s.finishDrainingLocked(key)
	if !stopped {
		// Still the live server: keep tracking it rather than start a second
		// one beside it.
		s.instances[key] = append(s.instances[key], inst)
		s.mu.Unlock()
		s.persist()
		return false, fmt.Errorf("recycle %s/%s: could not stop pid %d; left it running", key, suffix, inst.PID)
	}
	s.recycledTotal++
	s.mu.Unlock()

	ns, name, _ := strings.Cut(key, "/")
	obj, ok := s.store.Get(ns, name)
	if !ok {
		slog.Warn("spawner: recycled an instance whose ServerSetScale is gone; not respawning", "key", key)
		return false, nil
	}
	return s.reconcileUpLocked(obj, false) > 0, nil
}
