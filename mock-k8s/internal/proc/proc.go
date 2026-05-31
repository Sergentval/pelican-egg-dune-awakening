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

// Terminate stops the process identified by pid: SIGTERM first, then, if it
// is still alive after grace, SIGKILL. It signals the process group too
// (pgid == pid, courtesy of setsid in launch_bg) so any UE5 child processes
// are swept up. Returns nil once the process is gone; returns an error only
// if it somehow survives the SIGKILL escalation.
//
// Safe to call on an already-dead pid or on pid <= 0 (both are no-ops).
func Terminate(pid int, grace time.Duration) error {
	if !Alive(pid) {
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

// waitExit polls Alive until the process is gone or the deadline passes.
func waitExit(pid int, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for {
		if !Alive(pid) {
			return true
		}
		if time.Now().After(deadline) {
			return !Alive(pid)
		}
		time.Sleep(50 * time.Millisecond)
	}
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
