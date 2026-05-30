package proc

import (
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// TestMain lets individual tests re-exec this test binary as a controllable
// child process:
//
//	PROC_TEST_MODE=block        → default signal disposition, blocks forever
//	                              (SIGTERM kills it — the graceful path)
//	PROC_TEST_MODE=ignore-term  → ignores SIGTERM and blocks (only SIGKILL
//	                              can stop it — the escalation path)
//
// Using a single-process child (no shell, no children) keeps the signal
// behaviour unambiguous: there is no reparented grandchild to leak.
func TestMain(m *testing.M) {
	mode := os.Getenv("PROC_TEST_MODE")
	if mode == "block" || mode == "ignore-term" {
		if mode == "ignore-term" {
			signal.Ignore(syscall.SIGTERM)
		}
		// Announce readiness only AFTER the signal disposition is installed,
		// so the parent never races a SIGTERM against a not-yet-initialised
		// child — re-execing a -race test binary can take well over 100ms.
		if ready := os.Getenv("PROC_TEST_READY"); ready != "" {
			_ = os.WriteFile(ready, []byte("ok"), 0o644)
		}
		select {}
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
