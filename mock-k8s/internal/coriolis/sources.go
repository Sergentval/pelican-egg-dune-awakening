package coriolis

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/proc"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/spawner"
)

// A Deep Desert runs as several servers, started by two different launchers:
//
//   - dimension 0 (partition 8) is a mock-k8s instance, spawned from its
//     ServerSetScale like every warm map;
//   - dimensions 1..N (DUNE_DD_DIMENSIONS, default 3) are started by
//     start-ue5-dimensions.sh / spawn-dimension.sh, outside mock-k8s, each
//     at a canonical port with its own pidfile.
//
// Each dimension reads the Coriolis cycle at its own boot, so each needs its
// own restart. One source per launcher.

// Recycler is the spawner side: list a map's instances, restart one in place.
type Recycler interface {
	InstancesOf(mapName string) []spawner.InstanceRef
	// Recycle restarts one instance and reports whether a replacement was
	// started.
	Recycle(key, suffix string) (bool, error)
}

// SpawnerSource targets the mock-k8s instances of maps.
type SpawnerSource struct {
	rec     Recycler
	baseDir string
	maps    []string
}

func NewSpawnerSource(rec Recycler, baseDir string, maps []string) *SpawnerSource {
	return &SpawnerSource{rec: rec, baseDir: baseDir, maps: append([]string(nil), maps...)}
}

func (s *SpawnerSource) Targets() []Target {
	var out []Target
	for _, m := range s.maps {
		for _, ref := range s.rec.InstancesOf(m) {
			out = append(out, Target{
				Group:   ref.Key,
				LogPath: filepath.Join(s.baseDir, "logs", "ue5-"+m+"-"+ref.Suffix+".log"),
				Live:    ref.PID > 0,
				key:     ref.Key,
				suffix:  ref.Suffix,
			})
		}
	}
	return out
}

func (s *SpawnerSource) Recycle(t Target) (bool, error) {
	return s.rec.Recycle(t.key, t.suffix)
}

// dimPidfile is the pidfile spawn-dimension.sh / start-ue5-dimensions.sh
// write: runtime/pids/ue5-<MAP>-dim<D>-p<PARTITION>.pid.
var dimPidfile = regexp.MustCompile(`^ue5-(.+)-dim([0-9]+)-p([0-9]+)\.pid$`)

// DimensionSource targets the dimensional servers (dimension_index > 0) of
// maps. A dimension has a pidfile only while it is up, so a parked or downed
// sietch is never a target.
type DimensionSource struct {
	baseDir string
	maps    map[string]bool
	// run executes one admin-publish verb; a seam for tests.
	run func(verb, partition string) error
	// alive reports whether pid is running; a seam for tests.
	alive func(pid int) bool
}

func NewDimensionSource(baseDir string, maps []string) *DimensionSource {
	set := make(map[string]bool, len(maps))
	for _, m := range maps {
		set[m] = true
	}
	d := &DimensionSource{baseDir: baseDir, maps: set, alive: proc.Alive}
	d.run = d.adminPublish
	return d
}

func (d *DimensionSource) Targets() []Target {
	entries, err := os.ReadDir(filepath.Join(d.baseDir, "runtime", "pids"))
	if err != nil {
		return nil // no pids dir yet: nothing is running
	}
	var out []Target
	for _, e := range entries {
		m := dimPidfile.FindStringSubmatch(e.Name())
		if m == nil || !d.maps[m[1]] {
			continue
		}
		pid := proc.ReadPidFile(filepath.Join(d.baseDir, "runtime", "pids", e.Name()))
		out = append(out, Target{
			Group:     "dim:" + m[3],
			LogPath:   filepath.Join(d.baseDir, "logs", "ue5-"+m[1]+"-dim"+m[2]+"-p"+m[3]+".log"),
			Live:      pid > 0 && d.alive(pid),
			partition: m[3],
		})
	}
	return out
}

// Recycle takes the dimension down and brings it back through the same
// admin-publish verbs the panel's sietch controls use. dimension-down only
// returns once the process is gone (SIGKILL after 10 s), so the respawn can
// never share the partition with the old server. dimension-up returns at once
// and spawns in the background.
func (d *DimensionSource) Recycle(t Target) (bool, error) {
	if _, err := strconv.Atoi(t.partition); err != nil {
		return false, fmt.Errorf("dimension target without a partition id: %q", t.partition)
	}
	if err := d.run("dimension-down", t.partition); err != nil {
		return false, fmt.Errorf("dimension-down %s: %w", t.partition, err)
	}
	if err := d.run("dimension-up", t.partition); err != nil {
		// The dimension is now OFFLINE. Say so loudly: nothing else will bring
		// it back until an admin (or the DD autoscaler) does.
		return false, fmt.Errorf("dimension %s was stopped but dimension-up failed; it stays offline until brought up from the panel: %w", t.partition, err)
	}
	return true, nil
}

func (d *DimensionSource) adminPublish(verb, partition string) error {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "bash", filepath.Join(d.baseDir, "scripts", "admin-publish.sh"), verb, partition)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(lastLine(string(out))))
	}
	return nil
}

func lastLine(s string) string {
	s = strings.TrimRight(s, "\n")
	if i := strings.LastIndex(s, "\n"); i >= 0 {
		return s[i+1:]
	}
	return s
}
