// Package battlegroup loads and serves the BattleGroup CR that the Funcom
// Battlegroup Director treats as the source of truth for world layout
// (which maps exist, how partitions are sliced, image tags, DB creds, …).
//
// In a real Kubernetes deployment, the AMP module installs a BattleGroup
// resource per world. Here we read the same template Funcom ships in the
// depot under server/scripts/setup/templates/world-template.yaml,
// substitute the per-server placeholders, normalize metadata to the
// namespace/name the Director queries, and expose it via the mock-k8s
// CRD endpoints.
//
// The template is verbose (~60 KB) but mostly opaque to us: we treat
// every field as an unstructured map[string]any and let the Director's
// internal YAML schema validate. We only touch the few fields we have
// to: metadata.name, metadata.namespace, and any string anywhere in the
// document containing a {PLACEHOLDER}.
package battlegroup

import (
	"fmt"
	"os"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// Placeholders is the set of {KEY} → value substitutions applied to
// every string in the loaded template.
type Placeholders map[string]string

// LoadFromFile reads path, performs placeholder substitution, and
// returns the resulting unstructured object plus its derived name and
// namespace. namespace defaults to "default" (matching the Director's
// query URL) regardless of what the template specifies. name is forced
// to the canonical world-name (so the Director can GET it by that key).
func LoadFromFile(path string, name string, p Placeholders) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read template: %w", err)
	}

	// Substitute placeholders BEFORE parsing — that way a {WORLD_NAME}
	// inside a key, value, or map index all get replaced uniformly,
	// without us having to walk the YAML node tree.
	substituted := substitute(string(raw), p)

	var obj map[string]any
	if err := yaml.Unmarshal([]byte(substituted), &obj); err != nil {
		return nil, fmt.Errorf("parse template: %w", err)
	}

	// Force metadata.name and metadata.namespace to what the Director
	// actually queries. The template hints at funcom-seabass-<world>;
	// the real Director uses `default`. Don't fight the client.
	meta, ok := obj["metadata"].(map[string]any)
	if !ok {
		meta = map[string]any{}
		obj["metadata"] = meta
	}
	meta["name"] = name
	meta["namespace"] = "default"

	// kind in the template is "BattleGroup" (Pascal-case G). Keep it as
	// written so the Director's deserializer recognizes the type. If
	// it's missing, set it.
	if _, ok := obj["kind"].(string); !ok {
		obj["kind"] = "BattleGroup"
	}
	if _, ok := obj["apiVersion"].(string); !ok {
		obj["apiVersion"] = "igw.funcom.com/v1"
	}

	// The Director's BattlegroupUtils.Igwo.Battlegroup type has
	// [Required] on the .status field. world-template.yaml ships only
	// spec, so deserialization throws JsonException("missing required
	// properties: status") on the first GET. Inject a minimal default
	// status that mirrors what a real controller would publish after
	// reconciling. Empty maps/slices satisfy the deserializer's
	// [Required] check regardless of what nested fields its type has.
	if _, ok := obj["status"].(map[string]any); !ok {
		obj["status"] = defaultBattleGroupStatus()
	}

	return obj, nil
}

// defaultBattleGroupStatus is the placeholder status block we inject
// when the template omits one. The shape mirrors a typical K8s CRD
// status: a phase, observedGeneration, an empty conditions list, plus
// the common Funcom-side fields we've seen in the binary's strings.
// All values are non-null so .NET's System.Text.Json [Required]
// validation passes.
func defaultBattleGroupStatus() map[string]any {
	return map[string]any{
		"phase":               "Running",
		"observedGeneration":  int64(1),
		"conditions":          []any{},
		"partitions":          []any{},
		"battlegroupRevision": int64(0),
		"serverSetScales":     []any{},
	}
}

// MapInfo carries the (mapName → partitionId) mapping extracted from
// the BattleGroup spec. UE5 instances read their partitionId from the
// -PartitionIndex command-line arg and use it to claim a row in
// dune.farm_state — mismatched ids trigger Director's
// "partition ID N doesn't match desired partition M" warnings and may
// break travel routing once instances start handing players off.
type MapInfo struct {
	MapName     string
	PartitionID int64
}

// ExtractMapPartitions walks the BattleGroup spec for every `map:` entry and
// returns each map name paired with its real partition id, ordered by
// ascending partition id (which reproduces the authoritative worldPartitions
// order, so "first" → "main world").
//
// The same map name appears more than once in world-template.yaml: under
// .spec...worldPartitions[] (a list-of-maps carrying the real partitions[].id)
// and again under the server-set spec (a bare-scalar partitions list with no
// id field). We take the MAX partition id seen per map: max(realID, 0) ==
// realID regardless of which occurrence the (map-iteration-order-randomized)
// walk visits first, so the result is deterministic. A "first occurrence wins"
// dedup here was a per-process coin flip that, when it lost, assigned every map
// partition 0 — making every UE5 instance claim IGW server-index 0 and
// crash-loop.
//
// Multi-partition maps collapse to their first partition id. If the spec
// changes between versions, callers should treat the returned list as
// authoritative: never invent partition ids.
func ExtractMapPartitions(obj map[string]any) []MapInfo {
	best := make(map[string]int64)
	walkForMapPartitions(obj, best)
	out := make([]MapInfo, 0, len(best))
	for name, pid := range best {
		out = append(out, MapInfo{MapName: name, PartitionID: pid})
	}
	// Deterministic order independent of Go's randomized map iteration:
	// ascending partition id, map name as a stable tiebreak (only maps that
	// share an id — the 0 placeholder bucket, in practice — need the tiebreak).
	sort.Slice(out, func(i, j int) bool {
		if out[i].PartitionID != out[j].PartitionID {
			return out[i].PartitionID < out[j].PartitionID
		}
		return out[i].MapName < out[j].MapName
	})
	return out
}

// patternMapKeys are BattleGroup spec keys whose `map` field holds a REGULAR
// EXPRESSION rather than a literal map name. Funcom's 2026-08-12 depot added
// .spec.restartScalers[], which matches whole families of maps at once:
//
//	restartScalers:
//	- map: ^SH_.*
//	  size: 1
//
// Descending into these would register ServerSetScales named after the pattern
// (`<world>-^sh-.*`) that can never back a real UE5 instance.
var patternMapKeys = map[string]struct{}{
	"restartScalers": {},
}

// regexMetachars are the characters that mark a `map` value as a pattern.
// Funcom map names are alphanumerics and underscores (Survival_1, Overmap,
// DLC_Story_LostHarvest_EcolabA), so none of these can appear in a literal
// name — rejecting on them cannot drop a real map, while still catching any
// future pattern-bearing key we don't yet know to skip by name.
const regexMetachars = `^$.*+?()[]{}|\`

// isLiteralMapName reports whether s is a real map name rather than a scaling
// directive's regular expression.
func isLiteralMapName(s string) bool {
	return s != "" && !strings.ContainsAny(s, regexMetachars)
}

// walkForMapPartitions accumulates the largest partition id seen for each map
// name into best. Taking the max is what makes extraction order-independent
// (see ExtractMapPartitions). A map whose only occurrences yield 0 is still
// recorded (via best's zero value), so no referenced map is dropped.
func walkForMapPartitions(v any, best map[string]int64) {
	switch t := v.(type) {
	case map[string]any:
		if mapName, ok := t["map"].(string); ok && isLiteralMapName(mapName) {
			pid := firstPartitionID(t["partitions"])
			if prev, seen := best[mapName]; !seen || pid > prev {
				best[mapName] = pid
			}
		}
		for key, child := range t {
			if _, isPattern := patternMapKeys[key]; isPattern {
				continue
			}
			walkForMapPartitions(child, best)
		}
	case []any:
		for _, child := range t {
			walkForMapPartitions(child, best)
		}
	}
}

// firstPartitionID returns the .id field of the first element in a
// partitions array, defaulting to 0 if absent. YAML decoders surface
// integers as int / int64 / float64 depending on size, so we accept
// all three.
func firstPartitionID(v any) int64 {
	arr, ok := v.([]any)
	if !ok || len(arr) == 0 {
		return 0
	}
	first, ok := arr[0].(map[string]any)
	if !ok {
		return 0
	}
	switch id := first["id"].(type) {
	case int:
		return int64(id)
	case int64:
		return id
	case float64:
		return int64(id)
	}
	return 0
}

// ExtractMaps walks a parsed BattleGroup object looking for every
// `map: "..."` string value anywhere in the tree. The result is a
// deduplicated, stable-ordered list of map names referenced by the
// BattleGroup spec (e.g. ["Survival_1", "Overmap", "DeepDesert_1",
// "SH_Arrakeen", ...]).
//
// Deprecated: prefer ExtractMapPartitions, which returns the real
// partitionId from the spec instead of forcing callers to invent one.
// Retained because some call sites only need the name list.
func ExtractMaps(obj map[string]any) []string {
	seen := make(map[string]struct{})
	var maps []string
	walkForMaps(obj, seen, &maps)
	return maps
}

func walkForMaps(v any, seen map[string]struct{}, out *[]string) {
	switch t := v.(type) {
	case map[string]any:
		if m, ok := t["map"].(string); ok && m != "" {
			if _, dupe := seen[m]; !dupe {
				seen[m] = struct{}{}
				*out = append(*out, m)
			}
		}
		for _, child := range t {
			walkForMaps(child, seen, out)
		}
	case []any:
		for _, child := range t {
			walkForMaps(child, seen, out)
		}
	}
}

// ServerSetScaleName returns the canonical resource name the Director
// uses to GET a per-map ServerSetScale: <worldname>-<mapname>, with the
// map name lowercased and underscores rewritten to hyphens (DNS-1123).
//
// Example: world="sh-de0bccaa2501bf22-jrqtjf", map="SH_Arrakeen"
//
//	→ "sh-de0bccaa2501bf22-jrqtjf-sh-arrakeen"
func ServerSetScaleName(worldName, mapName string) string {
	low := []byte{}
	for i := 0; i < len(mapName); i++ {
		c := mapName[i]
		switch {
		case c >= 'A' && c <= 'Z':
			low = append(low, c+32)
		case c == '_':
			low = append(low, '-')
		default:
			low = append(low, c)
		}
	}
	return worldName + "-" + string(low)
}

// substitute does a single-pass {KEY}→value replace. Doing it on the
// raw YAML string (before parsing) means we don't have to walk a deep
// nested tree — strings.ReplaceAll is O(n·k) where k is the placeholder
// count (~7), which is negligible against a 60 KB file.
func substitute(text string, p Placeholders) string {
	for k, v := range p {
		text = strings.ReplaceAll(text, "{"+k+"}", v)
	}
	return text
}
