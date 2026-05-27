// Package ondemand parses $STATE/ondemand.ini, the admin-editable file
// that controls always-warm map list, max concurrent instances, and idle
// shutdown timing.
//
// File format (set by scripts/prestart.sh):
//
//	; comment line
//	MaxConcurrentInstances=8
//	AutomaticStopDuration=10m
//	AlwaysWarmMaps=Survival_1,Overmap,DeepDesert_1
//
// Top-level key=value (no INI sections). Spaces around `=` are ignored.
// Unknown keys are silently dropped — keeps us forward-compatible with
// upstream additions.
package ondemand

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds the parsed ondemand.ini settings with safe defaults.
type Config struct {
	MaxConcurrentInstances int
	AutomaticStopDuration  time.Duration
	AlwaysWarmMaps         []string
}

// Defaults returns a Config matching what scripts/prestart.sh seeds.
func Defaults() Config {
	return Config{
		MaxConcurrentInstances: 8,
		AutomaticStopDuration:  10 * time.Minute,
		AlwaysWarmMaps:         []string{"Survival_1", "Overmap", "DeepDesert_1"},
	}
}

// Load parses path and merges over Defaults(). Missing file is not an
// error — we return Defaults(). Malformed lines log via fmt.Errorf
// (caller decides whether to fail boot or continue).
func Load(path string) (Config, error) {
	cfg := Defaults()
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return cfg, nil
		}
		return cfg, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	lineNum := 0
	for scanner.Scan() {
		lineNum++
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, ";") || strings.HasPrefix(line, "#") {
			continue
		}
		eq := strings.IndexByte(line, '=')
		if eq < 0 {
			continue // tolerate stray text
		}
		key := strings.TrimSpace(line[:eq])
		val := strings.TrimSpace(line[eq+1:])
		switch key {
		case "MaxConcurrentInstances":
			if n, err := strconv.Atoi(val); err == nil && n > 0 {
				cfg.MaxConcurrentInstances = n
			}
		case "AutomaticStopDuration":
			if d, err := parseDurationLoose(val); err == nil && d > 0 {
				cfg.AutomaticStopDuration = d
			}
		case "AlwaysWarmMaps":
			maps := strings.Split(val, ",")
			cfg.AlwaysWarmMaps = cfg.AlwaysWarmMaps[:0]
			for _, m := range maps {
				m = strings.TrimSpace(m)
				if m != "" {
					cfg.AlwaysWarmMaps = append(cfg.AlwaysWarmMaps, m)
				}
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return cfg, fmt.Errorf("scan %s: %w", path, err)
	}
	return cfg, nil
}

// parseDurationLoose accepts "10m", "30s", "1h30m", or a bare integer
// (treated as seconds — matches AMP's permissive parsing).
func parseDurationLoose(s string) (time.Duration, error) {
	if n, err := strconv.Atoi(s); err == nil {
		return time.Duration(n) * time.Second, nil
	}
	return time.ParseDuration(s)
}
