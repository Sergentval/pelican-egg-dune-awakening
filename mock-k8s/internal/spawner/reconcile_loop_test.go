package spawner

import (
	"os/exec"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
)

// liveSleeper starts a real process and returns its pid + start-time identity.
func liveSleeper(t *testing.T) (pid int, st uint64) {
	t.Helper()
	cmd := exec.Command("sleep", "300")
	if err := cmd.Start(); err != nil {
		t.Fatalf("start sleeper: %v", err)
	}
	pid = cmd.Process.Pid
	go func() { _ = cmd.Wait() }()
	t.Cleanup(func() { _ = syscall.Kill(pid, syscall.SIGKILL) })
	st, _ = proc.StartTime(pid)
	return pid, st
}

func TestSweep_ReapsDeadKeepsLive(t *testing.T) {
	spw, _ := newBareSpawner(t)

	// Live instance (should be kept).
	livePID, liveST := liveSleeper(t)
	aLive, _ := spw.pool.Acquire()
	// Dead instance (should be reaped): start then kill.
	deadPID, deadST := liveSleeper(t)
	_ = syscall.Kill(deadPID, syscall.SIGKILL)
	for i := 0; i < 200 && proc.Alive(deadPID); i++ {
		time.Sleep(10 * time.Millisecond)
	}
	aDead, _ := spw.pool.Acquire()

	spw.mu.Lock()
	spw.instances["default/m"] = []instance{
		{Suffix: "p" + itoa(aLive.Index), MapName: "Survival_1", Allocation: aLive, PID: livePID, StartTime: liveST},
		{Suffix: "p" + itoa(aDead.Index), MapName: "Survival_1", Allocation: aDead, PID: deadPID, StartTime: deadST},
	}
	spw.mu.Unlock()

	spw.sweep()

	spw.mu.Lock()
	got := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if got != 1 {
		t.Fatalf("after sweep: %d instances tracked, want 1 (dead reaped, live kept)", got)
	}
	if used, _, _ := spw.pool.Stats(); used != 1 {
		t.Errorf("after sweep: pool used = %d, want 1 (dead slot released)", used)
	}
	if spw.reapedTotal != 1 {
		t.Errorf("reapedTotal = %d, want 1", spw.reapedTotal)
	}
	// One reap records exactly one failure (failures==1 means retry next tick).
	spw.mu.Lock()
	failures := spw.backoff["default/m"].failures
	spw.mu.Unlock()
	if failures != 1 {
		t.Errorf("backoff failures = %d, want 1 after one reap", failures)
	}
}

func TestSweep_ReapsPhantomSkipsStarting(t *testing.T) {
	spw, _ := newBareSpawner(t)

	closed := make(chan struct{})
	close(closed)               // capture finished, never got a pid -> phantom
	open := make(chan struct{}) // still starting

	aPhantom, _ := spw.pool.Acquire()
	aStarting, _ := spw.pool.Acquire()
	spw.mu.Lock()
	spw.instances["default/m"] = []instance{
		{Suffix: "p" + itoa(aPhantom.Index), MapName: "Survival_1", Allocation: aPhantom, PID: 0, pidReady: closed},
		{Suffix: "p" + itoa(aStarting.Index), MapName: "Survival_1", Allocation: aStarting, PID: 0, pidReady: open},
	}
	spw.mu.Unlock()

	spw.sweep()

	spw.mu.Lock()
	got := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if got != 1 {
		t.Fatalf("after sweep: %d instances, want 1 (phantom reaped, starting kept)", got)
	}
}
