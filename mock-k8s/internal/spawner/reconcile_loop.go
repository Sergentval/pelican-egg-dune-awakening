package spawner

import "time"

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
