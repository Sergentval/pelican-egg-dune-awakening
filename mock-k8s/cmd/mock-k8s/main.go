// Command mock-k8s is a drop-in replacement for CubeCoders' closed
// mock-k8s-go binary. It speaks just enough Kubernetes API surface for
// the Funcom Battlegroup Director to bring up and run a Dune: Awakening
// dedicated server battlegroup under Pelican Wings (no AMP required).
//
// Invocation matches scripts/start-mock-k8s.sh exactly:
//
//	mock-k8s <BASE_DIR>
//
// reading env:
//
//	K8S_MOCK_PORT             (default 6443)
//	K8S_POOL_SIZE             (default 25)
//	K8S_POOL_GAME_PORT_BASE   (default 7900)
//	K8S_POOL_IGW_PORT_BASE    (default 7950)
//	AMP_TOKEN                 (used as the ServiceAccount bearer token)
//	DUNE_WORLD_NAME, DUNE_BIND_IP, DUNE_PG_PORT, DUNE_BASE_DIR (for env propagation to UE5)
//
// Writes:
//
//	/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt,namespace}
//	$LOGS/mock-k8s.log (via stdout — handled by launch_bg)
//
// Prints "Mock Kubernetes API ready: success" to signal start-mock-k8s.sh's
// wait_for_port can stop polling.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/apigroup"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/battlegroup"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/coriolis"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/health"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/occupancy"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/ondemand"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/sa"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/server"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/spawner"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/stubs"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/traveldemand"
)

func main() {
	if err := run(); err != nil {
		// Match CubeCoders' log format so the existing
		// console.sh log-aggregator classifies our errors correctly.
		fmt.Fprintf(os.Stderr, "[mock-k8s] [ERROR] %s\n", err)
		os.Exit(1)
	}
}

func run() error {
	configureLogging()

	if len(os.Args) < 2 {
		return errors.New("usage: mock-k8s BASE_DIR")
	}
	baseDir := os.Args[1]

	// Resolve config from env with defaults that match
	// scripts/start-mock-k8s.sh.
	listenPort := envInt("K8S_MOCK_PORT", 6443)
	poolSize := envInt("K8S_POOL_SIZE", 25)
	gameBase := envInt("K8S_POOL_GAME_PORT_BASE", 7900)
	igwBase := envInt("K8S_POOL_IGW_PORT_BASE", 7950)
	ampToken := os.Getenv("AMP_TOKEN")
	if ampToken == "" {
		// We don't *require* AMP_TOKEN — we generate one if absent —
		// but log it so operators know what's going on.
		ampToken = "mock-k8s-token-" + randomHex(16)
		slog.Warn("AMP_TOKEN env var empty; using generated token", "token_len", len(ampToken))
	}

	slog.Info("mock-k8s starting",
		"base_dir", baseDir, "listen_port", listenPort,
		"pool_size", poolSize, "game_base", gameBase, "igw_base", igwBase)

	// 1. Generate self-signed TLS cert + key.
	certPEM, keyPEM, err := server.SelfSignedCert()
	if err != nil {
		return fmt.Errorf("tls: %w", err)
	}

	// 2. Write the in-cluster ServiceAccount mount so the Director's K8s
	// client can find token/ca.crt/namespace at the standard path.
	if err := sa.Write(sa.Files{
		Token:     ampToken,
		CACert:    certPEM,
		Namespace: "default",
	}); err != nil {
		return fmt.Errorf("write SA: %w", err)
	}
	slog.Info("wrote ServiceAccount mount", "path", sa.MountPath)

	// 3. Parse ondemand.ini for AlwaysWarmMaps + concurrency cap.
	ondemandPath := filepath.Join(baseDir, "server/state/ondemand.ini")
	cfg, err := ondemand.Load(ondemandPath)
	if err != nil {
		slog.Warn("ondemand.ini parse error; using defaults", "err", err)
	}
	slog.Info("on-demand config",
		"max_concurrent", cfg.MaxConcurrentInstances,
		"auto_stop", cfg.AutomaticStopDuration,
		"always_warm", cfg.AlwaysWarmMaps)
	warnInstanceBudget(cfg)

	// 4. Stand up the port pool.
	portPool, err := pool.New(gameBase, igwBase, poolSize)
	if err != nil {
		return fmt.Errorf("pool: %w", err)
	}

	// 5. Build the CRD stores and wire the spawner into ServerSetScale.
	sssStore := serversetscale.NewStore()
	spw := spawner.New(sssStore, portPool,
		filepath.Join(baseDir, "scripts/start-ue5.sh"),
		baseDir)
	sssStore.OnSpecChange = spw.OnSpecChange

	// 5a. Re-adopt UE5 instances that survived a mock-k8s restart (same
	// ports, same pids) BEFORE the AlwaysWarm pre-spawn below, so a map
	// that is already running is not double-spawned and its ports stay
	// stable across the restart.
	spw.Restore()

	// 5b. BattleGroup store: pre-populated from world-template.yaml (the
	// Director GETs this at startup to learn world layout). Soft-failure
	// path: if the template is missing we still serve discovery so the
	// Director can at least 404 cleanly rather than crash on TLS.
	//
	// In the same pass we extract every map referenced in the BattleGroup
	// spec and pre-create one ServerSetScale per map. The Director does
	// NOT create ServerSetScales itself — it expects them to already
	// exist and only ever patches spec.replicas on them. Without these
	// pre-created records the Director logs an endless stream of "No
	// scaling resource found for map X, is scaling enabled in IGWO?"
	bgStore := battlegroup.NewStore()
	worldName := os.Getenv("DUNE_WORLD_NAME")
	if worldName != "" {
		templatePath := filepath.Join(baseDir, "server/scripts/setup/templates/world-template.yaml")
		bg, err := battlegroup.LoadFromFile(templatePath, worldName, buildPlaceholders())
		if err != nil {
			slog.Warn("BattleGroup template load failed; serving empty", "path", templatePath, "err", err)
		} else {
			bgStore.Put(bg)
			slog.Info("BattleGroup pre-populated from template",
				"name", worldName, "template", templatePath)

			// Build map-name → (canonical SSS name, partition) lookup
			// used by the LazyCreate handler below. We do NOT bulk
			// pre-create the SSS items here: the Director's
			// ListServerSetScales call deserializes every item and
			// inserts it into a Dictionary keyed by some string
			// (probably spec.mapName); if that key resolves null for
			// even one item the whole call throws ArgumentNullException
			// and Director crashes on startup. Returning an empty list
			// and lazy-creating per-GET sidesteps the crash entirely.
			mapInfos := battlegroup.ExtractMapPartitions(bg)
			warmSet := make(map[string]struct{}, len(cfg.AlwaysWarmMaps))
			for _, m := range cfg.AlwaysWarmMaps {
				warmSet[m] = struct{}{}
			}
			lazy := serversetscale.LazyCreator{
				WorldName: worldName,
				Maps:      make(map[string]serversetscale.LazyMapInfo, len(mapInfos)),
			}
			for _, mi := range mapInfos {
				replicas := int64(0)
				if _, warm := warmSet[mi.MapName]; warm {
					replicas = 1
				}
				canonical := battlegroup.ServerSetScaleName(worldName, mi.MapName)
				lazy.Maps[canonical] = serversetscale.LazyMapInfo{
					MapName:     mi.MapName,
					PartitionID: mi.PartitionID,
					Replicas:    replicas,
				}
			}
			sssStore.LazyCreator = &lazy

			// Materialise every recipe, not just the always-warm ones. The
			// Director can only scale a map it sees in LIST, and it never asks
			// for one by name on its own, so an unmaterialised mission map
			// reads "(servers: [], num: 0)" on every travel-queue pass and the
			// player's request expires 300s later with nowhere to go. Non-warm
			// recipes carry replicas 0, so this creates records, not servers.
			if n := sssStore.MaterializeAll("default"); n > 0 {
				slog.Info("ServerSetScale records materialised for travel routing", "created", n)
			}

			slog.Info("ServerSetScale lazy-create wired",
				"map_count", len(mapInfos), "always_warm", len(warmSet))

			// Trigger lazy-create for every AlwaysWarmMap right now so
			// their UE5 instances spawn at boot instead of waiting for
			// the Director to ask about them. The Director only GETs
			// on-demand maps by name; the always-warm set is mock-k8s's
			// responsibility per ondemand.ini.
			for _, mapName := range cfg.AlwaysWarmMaps {
				canonical := battlegroup.ServerSetScaleName(worldName, mapName)
				if _, ok := sssStore.GetOrLazyCreate("default", canonical); !ok {
					slog.Warn("AlwaysWarm pre-spawn skipped — map not in BattleGroup spec",
						"map", mapName, "canonical", canonical)
					continue
				}
				slog.Info("AlwaysWarm pre-spawn fired", "map", mapName, "canonical", canonical)
			}
		}
	} else {
		slog.Warn("DUNE_WORLD_NAME env unset; BattleGroup store will be empty")
	}

	// 6. Build the HTTP mux. The two igw.funcom.com handlers are mounted
	// on the SAME prefix /apis/igw.funcom.com/v1/; the dispatcher below
	// picks based on which resource plural appears in the URL.
	mux := http.NewServeMux()
	mux.HandleFunc("/version", apigroup.HandleVersion)
	mux.HandleFunc("/api", apigroup.HandleAPI)
	mux.HandleFunc("/api/v1", apigroup.HandleCoreV1)
	mux.HandleFunc("/apis", apigroup.HandleAPIs)
	mux.HandleFunc("/apis/igw.funcom.com/v1", apigroup.HandleIGWv1)

	sssHandler := serversetscale.Handler(sssStore)
	bgHandler := battlegroup.Handler(bgStore)
	statsStore := stubs.NewStatsStore()
	bgdsHandler := stubs.BattlegroupDirectorStatsHandler(statsStore)
	ssHandler := stubs.ServerSetsHandler(sssStore)
	mux.HandleFunc("/apis/igw.funcom.com/v1/", func(w http.ResponseWriter, r *http.Request) {
		// Resource-plural dispatch. Order matters: longer/more-specific
		// prefixes are checked first so the short ones don't false-match
		// (e.g. "/serversets" is a substring of "/serversetscales").
		switch {
		case strings.Contains(r.URL.Path, "/battlegroupdirectorstats"):
			bgdsHandler(w, r)
		case strings.Contains(r.URL.Path, "/serversetscales"):
			sssHandler(w, r)
		case strings.Contains(r.URL.Path, "/serversets"):
			ssHandler(w, r)
		case strings.Contains(r.URL.Path, "/battlegroups"):
			bgHandler(w, r)
		default:
			sssHandler(w, r)
		}
	})
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/livez", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("/status", health.StatusHandler(spw.Snapshot))
	mux.HandleFunc("/metrics", health.MetricsHandler(spw.Snapshot))

	handler := server.LogMiddleware(mux, 4096)

	addr := "0.0.0.0:" + strconv.Itoa(listenPort)
	srv, err := server.New(addr, certPEM, keyPEM, handler)
	if err != nil {
		return fmt.Errorf("new server: %w", err)
	}

	// 7. Emit the marker scripts/start-mock-k8s.sh's wait_for_port +
	// the CubeCoders console.sh log aggregator look for.
	slog.Info("Mock Kubernetes API ready: success", "addr", addr)

	// 8. Run until SIGTERM/SIGINT.
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer cancel()
	reconcileInterval := parseReconcileInterval(os.Getenv("MOCK_K8S_RECONCILE_INTERVAL"))
	slog.Info("self-healing reconcile", "interval", reconcileInterval, "enabled", reconcileInterval > 0)
	go spw.Reconcile(ctx, reconcileInterval)

	// Restart the Deep Desert after each Coriolis boundary (#119). The game
	// applies a new cycle only when a server boots, and Funcom's operator,
	// which restarts servers on a schedule, is exactly what mock-k8s replaces.
	cw := coriolis.New([]coriolis.Source{
		coriolis.NewSpawnerSource(spw, baseDir, coriolisMaps), // dimension 0
		coriolis.NewDimensionSource(baseDir, coriolisMaps),    // dimensions 1..N
	}, parseCoriolisDelay(os.Getenv("MOCK_K8S_CORIOLIS_DELAY")))
	go cw.Run(ctx.Done(), parseDurationEnv("MOCK_K8S_CORIOLIS_INTERVAL", os.Getenv("MOCK_K8S_CORIOLIS_INTERVAL"), time.Minute))

	// Start an instanced map when a player asks to travel there. The Director
	// only routes to a group that already has a server and never creates the
	// first one, so without this a mission or hub travel request sits in the
	// queue until it expires 300s later. MaxConcurrentInstances finally means
	// something: it caps how many maps demand may start.
	if worldName != "" {
		scaler := &demandScaler{store: sssStore, spawner: spw, world: worldName}
		w := traveldemand.New(directorLogPath(baseDir), scaler, cfg.MaxConcurrentInstances)
		go w.Run(ctx.Done(), parseTravelWatchInterval(os.Getenv("MOCK_K8S_TRAVEL_WATCH_INTERVAL")))

		// And give the slot back. Without this the watcher can only ever fill
		// the budget: a map it starts stays up forever, and once
		// MaxConcurrentInstances is reached every later travel request is
		// refused until the next restart. AutomaticStopDuration has been in
		// ondemand.ini all along, parsed and applied by nobody.
		r := traveldemand.NewReaper(scaler, occupancy.NewReader(baseDir),
			cfg.AutomaticStopDuration, cfg.AlwaysWarmMaps)
		go r.Run(ctx.Done(), parseReapInterval(os.Getenv("MOCK_K8S_REAP_INTERVAL")))
	}
	if err := server.Run(ctx, srv); err != nil {
		return fmt.Errorf("serve: %w", err)
	}
	slog.Info("mock-k8s shutdown complete")
	return nil
}

// directorLogPath is where console.sh's launch_bg sends the Director's output.
func directorLogPath(baseDir string) string {
	return filepath.Join(baseDir, "logs", "director.log")
}

// parseTravelWatchInterval reads the poll interval for the travel-demand
// watcher. Default 2s: a player is already waiting when the line appears, and
// the read is a seek to a known offset, so a tight interval costs nothing. "off"
// (or any non-positive value) disables the watcher.
func parseTravelWatchInterval(raw string) time.Duration {
	return parseDurationEnv("MOCK_K8S_TRAVEL_WATCH_INTERVAL", raw, 2*time.Second)
}

// warnInstanceBudget says out loud how much room on-demand travel actually
// has. MaxConcurrentInstances is a budget over EVERY instance, always-warm
// ones included, and the arithmetic is easy to get wrong quietly: one
// reporter kept six maps warm against a budget of eight, so two dungeons
// could exist at a time on a server with thirty of them. Nothing said so
// until players could not get in.
func warnInstanceBudget(cfg ondemand.Config) {
	warm := len(cfg.AlwaysWarmMaps)
	headroom := cfg.MaxConcurrentInstances - warm
	switch {
	case headroom <= 0:
		slog.Warn("instance budget is already spent by the always-warm maps: no player can travel to any other map",
			"max_concurrent", cfg.MaxConcurrentInstances, "always_warm", warm,
			"fix", "raise MaxConcurrentInstances or shorten AlwaysWarmMaps in server/state/ondemand.ini")
	case headroom <= 2:
		slog.Warn("instance budget leaves very little room for travel",
			"max_concurrent", cfg.MaxConcurrentInstances, "always_warm", warm,
			"on_demand_slots", headroom,
			"fix", "raise MaxConcurrentInstances or shorten AlwaysWarmMaps in server/state/ondemand.ini")
	default:
		slog.Info("instance budget", "on_demand_slots", headroom,
			"max_concurrent", cfg.MaxConcurrentInstances, "always_warm", warm)
	}
}

// parseReapInterval reads how often to look for maps nobody is on. Default
// 30s: the decision itself is a ten-minute timer, so polling faster buys
// nothing and each poll costs a psql round-trip. "off" disables reaping.
func parseReapInterval(raw string) time.Duration {
	return parseDurationEnv("MOCK_K8S_REAP_INTERVAL", raw, 30*time.Second)
}

// coriolisMaps are the maps the Coriolis storm wipes. Only these need a
// restart at the boundary; Hagga and the dungeons read the cycle too but have
// nothing to reshape.
var coriolisMaps = []string{"DeepDesert_1"}

// parseCoriolisDelay reads how long after the boundary to recycle. Unlike the
// intervals, zero is not "off" here: it would restart in the very second the
// game handles the end of the cycle. Anything non-positive falls back to the
// default, loudly.
func parseCoriolisDelay(raw string) time.Duration {
	const def = 2 * time.Minute
	d := parseDurationEnv("MOCK_K8S_CORIOLIS_DELAY", raw, def)
	if d <= 0 {
		slog.Warn("MOCK_K8S_CORIOLIS_DELAY must be positive, using default", "value", raw, "default", def)
		return def
	}
	return d
}

// parseDurationEnv reads a watcher's duration setting: empty means fallback,
// "off"/"0"/"disabled" (or any non-positive duration) means 0, which disables
// the watcher, and anything unparseable warns and falls back.
func parseDurationEnv(name, raw string, fallback time.Duration) time.Duration {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return fallback
	case "off", "0", "disabled":
		return 0
	}
	if d, err := time.ParseDuration(strings.TrimSpace(raw)); err == nil {
		if d < 0 {
			return 0
		}
		return d
	}
	slog.Warn("unparseable duration setting, using default", "var", name, "value", raw, "default", fallback)
	return fallback
}

// demandScaler is the traveldemand.Scaler over our store and spawner.
type demandScaler struct {
	store   *serversetscale.Store
	spawner *spawner.Spawner
	world   string
}

func (d *demandScaler) LiveInstances() int { return d.spawner.Snapshot().Instances.Tracked }

// ScaledUpMaps and ScaleToZero are the reaper's half of the same adapter: the
// watcher raises a map, the reaper lowers it, and both go through the store so
// the spawner hears about it the same way.
func (d *demandScaler) ScaledUpMaps() []string { return d.store.ScaledUpMapNames("default") }

func (d *demandScaler) ScaleToZero(mapName string) error {
	canonical := battlegroup.ServerSetScaleName(d.world, mapName)
	if _, _, ok := d.store.ScaleToZero("default", canonical); !ok {
		return fmt.Errorf("no ServerSetScale for map %q", mapName)
	}
	return nil
}

// ScaleToOne raises a map to one replica, which the store hands to the
// spawner via OnSpecChange, and reports whether this call is what started it.
// GetOrLazyCreate first, so a map that somehow has no record yet still gets
// one; ScaleUpTo then does the whole read-compare-write under the store's
// lock, which is both what makes it idempotent — a map already at one or more
// is left exactly as it is, so a second travel request never disturbs a live
// instance — and what makes the spawner hear about it at once instead of on
// the next reconcile sweep.
func (d *demandScaler) ScaleToOne(mapName string) (bool, error) {
	canonical := battlegroup.ServerSetScaleName(d.world, mapName)
	if _, ok := d.store.GetOrLazyCreate("default", canonical); !ok {
		return false, fmt.Errorf("no ServerSetScale for map %q (not in the BattleGroup template?)", mapName)
	}
	_, changed, ok := d.store.ScaleUpTo("default", canonical, 1)
	if !ok {
		return false, fmt.Errorf("could not scale ServerSetScale %q", canonical)
	}
	return changed, nil
}

// buildPlaceholders snapshots the env vars the world-template.yaml expects.
// Anything unset gets a sensible default — the BattleGroup CR is mostly
// metadata for the Director; missing values just leave the literal
// "{KEY}" in place (Director treats those as opaque strings).
func buildPlaceholders() battlegroup.Placeholders {
	return battlegroup.Placeholders{
		"WORLD_NAME":          envOr("DUNE_WORLD_NAME", "dune-world"),
		"WORLD_UNIQUE_NAME":   envOr("DUNE_WORLD_NAME", "dune-world"),
		"WORLD_REGION":        envOr("DUNE_REGION", "Europe"),
		"WORLD_DUNE_PASS":     envOr("DUNE_DB_PASS", "dune"),
		"WORLD_POSTGRES_PASS": envOr("DUNE_PG_SUPER_PASS", "seabass"),
		"WORLD_IMAGE_TAG":     envOr("DUNE_RELEASE_VERSION", "self-hosted"),
		"FLS_SECRET":          envOr("DUNE_RMQ_SEC", ""),
	}
}

func envOr(name, fallback string) string {
	v := os.Getenv(name)
	if v == "" {
		return fallback
	}
	return v
}

// parseReconcileInterval reads the reconcile-loop interval. Empty/unparseable
// values fall back to 30s; "0", "off", or a non-positive duration disables the
// loop. Surrounding whitespace is tolerated.
func parseReconcileInterval(v string) time.Duration {
	const def = 30 * time.Second
	v = strings.TrimSpace(v)
	switch strings.ToLower(v) {
	case "":
		return def
	case "off", "false", "no":
		return 0
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	if d <= 0 {
		return 0
	}
	return d
}

// envInt reads an int env var, returning fallback when unset/invalid.
func envInt(name string, fallback int) int {
	v := os.Getenv(name)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func randomHex(n int) string {
	const hex = "0123456789abcdef"
	out := make([]byte, n*2)
	for i := range out {
		out[i] = hex[i%16]
	}
	return string(out)
}

func configureLogging() {
	// Plain-text logger that prefixes [mock-k8s] [LEVEL] to mirror what
	// the CubeCoders scripts do. console.sh's awk classifier already
	// handles this format.
	h := &cubecodersHandler{}
	slog.SetDefault(slog.New(h))
}

// cubecodersHandler is a slog.Handler that prints `[mock-k8s] [LEVEL] msg key=value ...`
// to stdout. Compatible with console.sh's classify() regex.
type cubecodersHandler struct{}

func (h *cubecodersHandler) Enabled(_ context.Context, _ slog.Level) bool { return true }
func (h *cubecodersHandler) Handle(_ context.Context, r slog.Record) error {
	level := "INFO"
	switch r.Level {
	case slog.LevelDebug:
		level = "DEBUG"
	case slog.LevelWarn:
		level = "WARN"
	case slog.LevelError:
		level = "ERROR"
	}
	line := "[mock-k8s] [" + level + "] " + r.Message
	r.Attrs(func(a slog.Attr) bool {
		line += " " + a.Key + "=" + formatVal(a.Value)
		return true
	})
	line += "\n"
	_, _ = os.Stdout.WriteString(line)
	return nil
}
func (h *cubecodersHandler) WithAttrs(_ []slog.Attr) slog.Handler { return h }
func (h *cubecodersHandler) WithGroup(_ string) slog.Handler      { return h }

func formatVal(v slog.Value) string {
	switch v.Kind() {
	case slog.KindString:
		return v.String()
	case slog.KindInt64:
		return strconv.FormatInt(v.Int64(), 10)
	case slog.KindBool:
		if v.Bool() {
			return "true"
		}
		return "false"
	default:
		return fmt.Sprintf("%v", v.Any())
	}
}
