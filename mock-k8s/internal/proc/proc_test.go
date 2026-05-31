package proc

import (
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// TestMain lets individual tests re-exec this test binary as a controllable
// child process:
//
//	PROC_TEST_MODE=block         → default signal disposition, blocks forever
//	                               (SIGTERM kills it — the graceful path)
//	PROC_TEST_MODE=ignore-term   → ignores SIGTERM and blocks (only SIGKILL
//	                               can stop it — the escalation path)
//	PROC_TEST_MODE=group-leader  → started Setpgid:true so pgid == its pid
//	                               (mirroring setsid in launch_bg); ignores
//	                               SIGTERM and forks one ignore-term grandchild
//	                               in the same process group, then writes the
//	                               grandchild pid to PROC_TEST_READY. Used by
//	                               TestTerminate_GroupSweep to prove kill(-pid).
func TestMain(m *testing.M) {
	switch os.Getenv("PROC_TEST_MODE") {
	case "block":
		// SIGTERM kills it — the graceful path.
		if ready := os.Getenv("PROC_TEST_READY"); ready != "" {
			_ = os.WriteFile(ready, []byte("ok"), 0o644)
		}
		// NOT select{}: with no other goroutines an empty select trips Go's
		// "all goroutines are asleep" deadlock detector without -race.
		time.Sleep(time.Hour)
		os.Exit(0)

	case "ignore-term":
		signal.Ignore(syscall.SIGTERM)
		// Announce readiness only AFTER the signal disposition is installed,
		// so the parent never races a SIGTERM against a not-yet-initialised
		// child — re-execing a -race test binary can take well over 100ms.
		if ready := os.Getenv("PROC_TEST_READY"); ready != "" {
			_ = os.WriteFile(ready, []byte("ok"), 0o644)
		}
		time.Sleep(time.Hour)
		os.Exit(0)

	case "group-leader":
		// pgid == our pid (parent set Setpgid:true). We and our grandchild
		// both ignore SIGTERM, so Terminate must escalate to SIGKILL; the
		// grandchild is reachable only via the group kill kill(-pid) — the
		// path under test.
		signal.Ignore(syscall.SIGTERM)
		gcReady := os.Getenv("PROC_TEST_GC_READY")
		gc := exec.Command(os.Args[0])
		gc.Env = append(os.Environ(), "PROC_TEST_MODE=ignore-term", "PROC_TEST_READY="+gcReady)
		// No Setpgid — the grandchild inherits this process's group.
		if err := gc.Start(); err != nil {
			fmt.Fprintf(os.Stderr, "group-leader: start grandchild: %v\n", err)
			os.Exit(1)
		}
		go func() { _ = gc.Wait() }()
		// Wait for the grandchild to install its disposition before we
		// advertise our pid, so the test never SIGTERMs it before it is ready.
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			if _, err := os.Stat(gcReady); err == nil {
				break
			}
			time.Sleep(10 * time.Millisecond)
		}
		if _, err := os.Stat(gcReady); err != nil {
			fmt.Fprintln(os.Stderr, "group-leader: grandchild not ready within 10s")
			os.Exit(1)
		}
		if ready := os.Getenv("PROC_TEST_READY"); ready != "" {
			_ = os.WriteFile(ready, []byte(strconv.Itoa(gc.Process.Pid)), 0o644)
		}
		time.Sleep(time.Hour)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func startChild(t *testing.T, mode string) int {
	t.Helper()
	ready := filepath.Join(t.TempDir(), "ready")
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), "PROC_TEST_MODE="+mode, "PROC_TEST_READY="+ready)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start child (%s): %v", mode, err)
	}
	pid := cmd.Process.Pid
	// Reap in the background so a terminated child doesn't linger as a
	// zombie — a zombie still answers signal 0, which would make Alive lie.
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })

	// Block until the child has installed its signal disposition.
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(ready); err == nil {
			return pid
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("child (%s) not ready within 5s", mode)
	return 0
}

// startGroupLeaderChild re-execs the test binary with Setpgid:true so it
// becomes a process-group leader (pgid == its pid, mirroring setsid in
// launch_bg). That child ignores SIGTERM and forks one grandchild (also
// ignore-term) in the same group. Both die only on SIGKILL, so Terminate must
// exhaust its grace and escalate — and the grandchild is reachable only via
// kill(-pid). Returns both pids; the caller asserts both are gone.
func startGroupLeaderChild(t *testing.T) (childPID, grandchildPID int) {
	t.Helper()
	dir := t.TempDir()
	childReady := filepath.Join(dir, "child-ready") // content = grandchild pid
	gcReady := filepath.Join(dir, "gc-ready")       // grandchild disposition installed

	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(),
		"PROC_TEST_MODE=group-leader",
		"PROC_TEST_READY="+childReady,
		"PROC_TEST_GC_READY="+gcReady,
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true} // pgid == child.Pid
	if err := cmd.Start(); err != nil {
		t.Fatalf("start group-leader child: %v", err)
	}
	childPID = cmd.Process.Pid
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() {
		// Nuke the whole group in case the test fails before Terminate fires.
		_ = syscall.Kill(-childPID, syscall.SIGKILL)
		_ = syscall.Kill(childPID, syscall.SIGKILL)
	})

	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(childReady); err == nil {
			if gcPID, convErr := strconv.Atoi(strings.TrimSpace(string(b))); convErr == nil && gcPID > 0 {
				return childPID, gcPID
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("group-leader child not ready within 10s")
	return 0, 0
}

func waitGone(pid int, d time.Duration) bool {
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if !Alive(pid) {
			return true
		}
		time.Sleep(10 * time.Millisecond)
	}
	return !Alive(pid)
}

func TestAlive_BogusPidsAreDead(t *testing.T) {
	for _, pid := range []int{0, -1, -42} {
		if Alive(pid) {
			t.Errorf("Alive(%d) = true, want false", pid)
		}
	}
}

func TestAlive_TracksProcessLifecycle(t *testing.T) {
	pid := startChild(t, "block")
	if !Alive(pid) {
		t.Fatalf("Alive(%d) = false right after start, want true", pid)
	}
	_ = syscall.Kill(pid, syscall.SIGKILL)
	if !waitGone(pid, 2*time.Second) {
		t.Fatalf("process %d still Alive 2s after SIGKILL", pid)
	}
}

func TestTerminate_GracefulSIGTERM(t *testing.T) {
	pid := startChild(t, "block")
	start := time.Now()
	if err := Terminate(pid, 3*time.Second); err != nil {
		t.Fatalf("Terminate returned error: %v", err)
	}
	if Alive(pid) {
		t.Fatalf("process %d still Alive after Terminate", pid)
	}
	// A process that honours SIGTERM should die well before the grace
	// period — proving we didn't just wait the whole window then SIGKILL.
	if elapsed := time.Since(start); elapsed > 2500*time.Millisecond {
		t.Errorf("Terminate took %v; expected fast SIGTERM exit", elapsed)
	}
}

func TestTerminate_EscalatesToSIGKILL(t *testing.T) {
	pid := startChild(t, "ignore-term")
	// Confirm the child really ignores SIGTERM before we test escalation.
	_ = syscall.Kill(pid, syscall.SIGTERM)
	time.Sleep(150 * time.Millisecond)
	if !Alive(pid) {
		t.Fatalf("child died on SIGTERM; cannot test escalation")
	}
	start := time.Now()
	if err := Terminate(pid, 300*time.Millisecond); err != nil {
		t.Fatalf("Terminate returned error: %v", err)
	}
	if Alive(pid) {
		t.Fatalf("process %d survived Terminate (no SIGKILL escalation)", pid)
	}
	if elapsed := time.Since(start); elapsed < 250*time.Millisecond {
		t.Errorf("Terminate took %v; expected to wait the grace period before SIGKILL", elapsed)
	}
}

func TestTerminate_AlreadyDeadIsNoError(t *testing.T) {
	pid := startChild(t, "block")
	_ = syscall.Kill(pid, syscall.SIGKILL)
	if !waitGone(pid, 2*time.Second) {
		t.Fatalf("setup: process %d did not die", pid)
	}
	if err := Terminate(pid, time.Second); err != nil {
		t.Errorf("Terminate(dead pid) = %v, want nil", err)
	}
}

// TestTerminate_GroupSweep proves Terminate's kill(-pid, sig) sweeps up
// setsid'd children. Topology mirrors UE5 via start-ue5.sh → launch_bg:
//
//	test process
//	  └─ child       (Setpgid:true → pgroup leader, ignores SIGTERM)
//	       └─ grandchild  (inherits pgroup, ignores SIGTERM)
//
// Both ignore SIGTERM, so Terminate escalates to SIGKILL. sendSignal sends it
// to both the leader and the group (-pid); the grandchild — which a directed
// SIGKILL to the leader alone would leave running — is reaped by the group
// kill. If the child instead honoured SIGTERM, Terminate would return as soon
// as the leader died and never SIGKILL the group, leaking the grandchild.
func TestTerminate_GroupSweep(t *testing.T) {
	childPID, gcPID := startGroupLeaderChild(t)

	if !Alive(childPID) {
		t.Fatalf("child %d not alive at test start", childPID)
	}
	if !Alive(gcPID) {
		t.Fatalf("grandchild %d not alive at test start", gcPID)
	}

	if err := Terminate(childPID, 300*time.Millisecond); err != nil {
		t.Fatalf("Terminate(%d): %v", childPID, err)
	}

	if Alive(childPID) {
		t.Errorf("group leader %d still alive after Terminate", childPID)
	}
	// The key assertion: the grandchild is gone, proving kill(-pid, SIGKILL).
	if !waitGone(gcPID, 2*time.Second) {
		t.Errorf("grandchild %d still alive 2s after Terminate; kill(-pid) did not sweep the process group", gcPID)
	}
}

func TestReadPidFile(t *testing.T) {
	dir := t.TempDir()

	good := filepath.Join(dir, "ue5.pid")
	if err := os.WriteFile(good, []byte("1234\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := ReadPidFile(good); got != 1234 {
		t.Errorf("ReadPidFile(good) = %d, want 1234", got)
	}

	if got := ReadPidFile(filepath.Join(dir, "missing.pid")); got != 0 {
		t.Errorf("ReadPidFile(missing) = %d, want 0", got)
	}

	garbage := filepath.Join(dir, "garbage.pid")
	if err := os.WriteFile(garbage, []byte("not-a-pid"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := ReadPidFile(garbage); got != 0 {
		t.Errorf("ReadPidFile(garbage) = %d, want 0", got)
	}
}
