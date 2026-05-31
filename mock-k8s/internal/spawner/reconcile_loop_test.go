package spawner

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
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
	if proc.Alive(deadPID) {
		t.Fatal("deadPID still alive after SIGKILL; cannot test reaping")
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
	spw.mu.Lock()
	reapedTotal := spw.reapedTotal
	spw.mu.Unlock()
	if reapedTotal != 1 {
		t.Errorf("reapedTotal = %d, want 1", reapedTotal)
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

// makeFakeUE5Script writes a start-ue5.sh stub that backgrounds a real sleeper
// and records its pid where capturePID expects it. Returns the script path.
func makeFakeUE5Script(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "start-ue5.sh")
	// $1=BASE $2=MAP $3=SUFFIX ; write runtime/pids/ue5-<map>-<suffix>.pid
	body := "#!/bin/bash\nset -e\nsleep 300 &\nPID=$!\nmkdir -p \"$1/runtime/pids\"\necho $PID > \"$1/runtime/pids/ue5-$2-$3.pid\"\n"
	if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return p
}

func newLoopSpawner(t *testing.T) (*Spawner, *fakeClock) {
	t.Helper()
	base := t.TempDir()
	store := serversetscale.NewStore()
	pl, err := pool.New(7900, 7950, 5)
	if err != nil {
		t.Fatal(err)
	}
	// Use a fake script that actually launches a trackable process.
	spw := New(store, pl, makeFakeUE5Script(t), base)
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	spw.now = clk.Now
	spw.startedAt = clk.Now()
	return spw, clk
}

func sssObj(name, mapName string, replicas int64) serversetscale.Object {
	return serversetscale.Object{
		Metadata: serversetscale.Metadata{Namespace: "default", Name: name},
		Spec:     map[string]any{"mapName": mapName, "replicas": replicas, "partitionId": int64(1)},
	}
}

func TestReconcileUp_SpawnsToDesired(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	obj := sssObj("m", "Survival_1", 2)

	spw.reconcileMu.Lock()
	n := spw.reconcileUpLocked(obj, true)
	spw.reconcileMu.Unlock()
	spw.Wait() // let capturePID finish

	if n != 2 {
		t.Errorf("spawned %d, want 2", n)
	}
	spw.mu.Lock()
	got := len(spw.instances["default/m"])
	spw.mu.Unlock()
	if got != 2 {
		t.Errorf("tracked %d, want 2", got)
	}
}

func TestReconcileUp_RespectsBackoff(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	obj := sssObj("m", "Survival_1", 1)

	// Put the map in backoff (failures=2 -> 1m window).
	spw.recordFailure("default/m")
	spw.recordFailure("default/m")

	spw.reconcileMu.Lock()
	withBackoff := spw.reconcileUpLocked(obj, true) // must NOT spawn
	spw.reconcileMu.Unlock()
	if withBackoff != 0 {
		t.Errorf("respectBackoff=true spawned %d, want 0 (map in backoff)", withBackoff)
	}

	spw.reconcileMu.Lock()
	ignoreBackoff := spw.reconcileUpLocked(obj, false) // Director patch honored
	spw.reconcileMu.Unlock()
	spw.Wait()
	if ignoreBackoff != 1 {
		t.Errorf("respectBackoff=false spawned %d, want 1", ignoreBackoff)
	}
}

func TestReconcileUp_NoOpWhenAtDesired(t *testing.T) {
	spw, _ := newLoopSpawner(t)
	obj := sssObj("m", "Survival_1", 0) // desired==current==0
	spw.reconcileMu.Lock()
	n := spw.reconcileUpLocked(obj, true)
	spw.reconcileMu.Unlock()
	if n != 0 {
		t.Errorf("spawned %d at desired==current, want 0", n)
	}
}
