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

	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/apigroup"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/battlegroup"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/directorstats"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/ondemand"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/pool"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/sa"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/server"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/serversetscale"
	"github.com/Sergentval/pelican-egg-dune-awakening/mock-k8s/internal/spawner"
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
	dsHandler := directorstats.Handler(directorstats.NewStore())
	// Dispatch by resource plural. Order matters: "battlegroupdirectorstats"
	// contains "battlegroups" as a substring, so the directorstats branch
	// MUST be checked before the battlegroups branch.
	mux.HandleFunc("/apis/igw.funcom.com/v1/", func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.Contains(r.URL.Path, "/battlegroupdirectorstats"):
			dsHandler(w, r)
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
	if err := server.Run(ctx, srv); err != nil {
		return fmt.Errorf("serve: %w", err)
	}
	slog.Info("mock-k8s shutdown complete")
	return nil
}

// buildPlaceholders snapshots the env vars the world-template.yaml expects.
// Anything unset gets a sensible default — the BattleGroup CR is mostly
// metadata for the Director; missing values just leave the literal
// "{KEY}" in place (Director treats those as opaque strings).
func buildPlaceholders() battlegroup.Placeholders {
	return battlegroup.Placeholders{
		"WORLD_NAME":         envOr("DUNE_WORLD_NAME", "dune-world"),
		"WORLD_UNIQUE_NAME":  envOr("DUNE_WORLD_NAME", "dune-world"),
		"WORLD_REGION":       envOr("DUNE_REGION", "Europe"),
		"WORLD_DUNE_PASS":    envOr("DUNE_DB_PASS", "dune"),
		"WORLD_POSTGRES_PASS": envOr("DUNE_PG_SUPER_PASS", "seabass"),
		"WORLD_IMAGE_TAG":    envOr("DUNE_RELEASE_VERSION", "self-hosted"),
		"FLS_SECRET":         envOr("DUNE_RMQ_SEC", ""),
	}
}

func envOr(name, fallback string) string {
	v := os.Getenv(name)
	if v == "" {
		return fallback
	}
	return v
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
