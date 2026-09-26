// Package proc provides the minimal process-lifecycle helpers the spawner
// needs: liveness probing, graceful-then-forceful termination, and reading
// the pidfiles that scripts/lib.sh's launch_bg writes for each UE5 instance.
//
// UE5 dedicated servers are launched by scripts/start-ue5.sh via launch_bg,
// which `setsid`s the process (making it a session / process-group leader,
// so pgid == pid) and records the pid at
//
//	$BASE/runtime/pids/ue5-<map>-<suffix>.pid
//
// Because the process is setsid'd and disowned it is reparented to the
// container init (tini); when we signal it and it exits, init reaps it and
// the pid stops existing — so there is no zombie to make Alive lie.
package proc

import (
	"errors"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// Alive reports whether pid refers to a live process. A pid <= 0 is never
// alive: in kill(2) zero and negative values address process groups or the
// caller, which must never be mistaken for a UE5 instance.
func Alive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	switch {
	case err == nil:
		return true
	case errors.Is(err, syscall.EPERM):
		// Exists but owned by another uid — still "alive".
		return true
	default: // ESRCH and friends
		return false
	}
}

// Terminate stops the process identified by pid and its whole process group
// (pgid == pid, courtesy of setsid in launch_bg): SIGTERM first, then, if
// anything in the group is still running after grace, SIGKILL to the group.
// Returns nil once the leader AND every group member are gone; returns an
// error only if something survives the SIGKILL escalation.
//
// Waiting for the group, not just the leader, is the point. The pidfile holds
// the `sh DuneSandboxServer.sh` wrapper, which dies on SIGTERM at once, while
// the UE5 binary beside it enters PreShutdown and may stay there: a Deep
// Desert did for over ten minutes, still in the farm on its partition. Waiting
// on the leader alone returned immediately, freed the port slot, never sent
// the SIGKILL, and left that server running untracked.
//
// Safe to call on an already-dead pid or on pid <= 0 (both are no-ops).
func Terminate(pid int, grace time.Duration) error {
	if gone(pid) {
		return nil
	}
	sendSignal(pid, syscall.SIGTERM)
	if waitExit(pid, grace) {
		return nil
	}
	sendSignal(pid, syscall.SIGKILL)
	// SIGKILL is immediate, but reaping by init takes a beat; give it a
	// short bounded window so callers can rely on "gone" on return.
	if waitExit(pid, 2*time.Second) {
		return nil
	}
	return errors.New("proc: pid " + strconv.Itoa(pid) + " survived SIGKILL")
}

// sendSignal sends sig to the process and, when safe, to its process group.
// Group delivery (negative pid) sweeps up setsid'd children; ESRCH from
// either target is ignored because a leaderless or already-gone pid is not
// an error here. The pid > 1 guard makes a group kill of init (-1 == "every
// process") impossible.
func sendSignal(pid int, sig syscall.Signal) {
	_ = syscall.Kill(pid, sig)
	if pid > 1 {
		_ = syscall.Kill(-pid, sig)
	}
}

// waitExit polls until the process and its group are gone or the deadline
// passes.
func waitExit(pid int, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for {
		if gone(pid) {
			return true
		}
		if time.Now().After(deadline) {
			return gone(pid)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// gone reports whether pid and every running member of its process group
// have exited.
func gone(pid int) bool {
	return !Alive(pid) && !groupRunning(pid)
}

// groupRunning reports whether any non-zombie process has pgid as its process
// group. It reads /proc rather than probing kill(-pgid, 0), which also answers
// for zombies: an orphan waiting on a slow reaper is dead for our purposes.
// pgid <= 1 is never a UE5 group and always reports false.
func groupRunning(pgid int) bool {
	if pgid <= 1 {
		return false
	}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return false
	}
	want := strconv.Itoa(pgid)
	for _, e := range entries {
		name := e.Name()
		if name == "" || name[0] < '0' || name[0] > '9' {
			continue
		}
		b, err := os.ReadFile("/proc/" + name + "/stat")
		if err != nil {
			continue // exited while we looked
		}
		s := string(b)
		rparen := strings.LastIndexByte(s, ')')
		if rparen < 0 {
			continue
		}
		// After the comm field: [0]=state (field 3), [2]=pgrp (field 5).
		f := strings.Fields(s[rparen+1:])
		if len(f) > 2 && f[2] == want && f[0] != "Z" && f[0] != "X" {
			return true
		}
	}
	return false
}

// ReadPidFile reads a pid written by scripts/lib.sh's write_pid: a single
// decimal integer, possibly with a trailing newline. Returns 0 when the
// file is missing, empty, or unparseable — callers treat 0 as "no pid".
func ReadPidFile(path string) int {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil || pid <= 0 {
		return 0
	}
	return pid
}

// StartTime returns a process's start time — field 22 of /proc/<pid>/stat,
// in clock ticks since boot. Paired with the pid it is a stable identity the
// kernel never reuses: a recycled pid carries a different start time, so a
// stale pidfile pointing at a since-recycled pid can be detected. ok is false
// when pid is not a live process (its stat file is gone or unreadable).
func StartTime(pid int) (uint64, bool) {
	if pid <= 0 {
		return 0, false
	}
	b, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return 0, false
	}
	// Field 2 (comm) is parenthesised and may itself contain spaces or ')';
	// skip past the LAST ')' so the remaining whitespace-separated fields are
	// unambiguous. After it, field 3 (state) is index 0, so field 22
	// (starttime) is index 19.
	s := string(b)
	rparen := strings.LastIndexByte(s, ')')
	if rparen < 0 || rparen+1 >= len(s) {
		return 0, false
	}
	fields := strings.Fields(s[rparen+1:])
	const startTimeIdx = 22 - 3 // fields[0] == stat field 3 (state)
	if len(fields) <= startTimeIdx {
		return 0, false
	}
	st, err := strconv.ParseUint(fields[startTimeIdx], 10, 64)
	if err != nil {
		return 0, false
	}
	return st, true
}

// SameProcess reports whether pid is alive AND still the same incarnation
// whose start time was recorded as startTime. A zero startTime ("no recorded
// identity") returns false so a strict identity check fails closed; callers
// that want best-effort liveness for an unrecorded process use Alive instead.
func SameProcess(pid int, startTime uint64) bool {
	if startTime == 0 {
		return false
	}
	st, ok := StartTime(pid)
	return ok && st == startTime
}
