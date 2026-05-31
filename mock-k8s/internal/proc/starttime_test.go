package proc

import (
	"os/exec"
	"syscall"
	"testing"
	"time"
)

func TestStartTime_BogusPid(t *testing.T) {
	for _, pid := range []int{0, -1, -99} {
		if _, ok := StartTime(pid); ok {
			t.Errorf("StartTime(%d) ok = true, want false", pid)
		}
	}
}

func TestStartTime_StableAndIdentity(t *testing.T) {
	cmd := exec.Command("sleep", "60")
	if err := cmd.Start(); err != nil {
		t.Fatalf("start sleep: %v", err)
	}
	pid := cmd.Process.Pid
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })

	st, ok := StartTime(pid)
	if !ok || st == 0 {
		t.Fatalf("StartTime(%d) = %d, %v; want nonzero, true", pid, st, ok)
	}
	if st2, _ := StartTime(pid); st2 != st {
		t.Errorf("StartTime not stable: %d vs %d", st, st2)
	}

	if !SameProcess(pid, st) {
		t.Errorf("SameProcess(pid, correct start-time) = false")
	}
	if SameProcess(pid, st+1) {
		t.Errorf("SameProcess(pid, wrong start-time) = true; should detect a recycled pid")
	}
	if !SameProcess(pid, 0) {
		t.Errorf("SameProcess(pid, 0) = false; want liveness fallback")
	}

	// After the process is fully reaped, identity must report not-same.
	_ = syscall.Kill(pid, syscall.SIGKILL)
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && Alive(pid) {
		time.Sleep(10 * time.Millisecond)
	}
	if SameProcess(pid, st) {
		t.Errorf("SameProcess(reaped pid, st) = true, want false")
	}
}
