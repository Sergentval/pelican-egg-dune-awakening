package battlegroup

import (
	"reflect"
	"testing"
)

// buildDuplicateMapSpec mirrors the real world-template.yaml shape: every map
// name appears twice under sibling keys of the same parent map node —
//
//   - worldPartitions[]: the AUTHORITATIVE source. partitions is a list of
//     maps, each carrying an "id" — firstPartitionID returns the real id.
//   - serverSets[]: partitions is a list of bare SCALARS with no "id" field —
//     firstPartitionID returns 0.
//
// Because the two lists live under sibling keys of a map[string]any, which
// subtree the walk descends into first is Go's randomized map-iteration order.
// A "first occurrence wins" dedup therefore returns the real id or 0 at random
// per process — the boot-time coin flip that made every UE5 instance claim
// IGW server-index 0 and crash-loop.
func buildDuplicateMapSpec() map[string]any {
	return map[string]any{
		"spec": map[string]any{
			"worldPartitions": []any{
				map[string]any{"map": "Survival_1", "partitions": []any{
					map[string]any{"dimension": 0, "id": 1},
				}},
				map[string]any{"map": "Overmap", "partitions": []any{
					map[string]any{"dimension": 0, "id": 2},
				}},
				map[string]any{"map": "DeepDesert_1", "partitions": []any{
					map[string]any{"dimension": 0, "id": 8},
					map[string]any{"dimension": 1, "id": 101},
				}},
			},
			"serverSets": []any{
				map[string]any{"map": "Survival_1", "partitions": []any{1}},
				map[string]any{"map": "Overmap", "partitions": []any{2}},
				map[string]any{"map": "DeepDesert_1", "partitions": []any{8}},
			},
		},
	}
}

// TestExtractMapPartitions_PrefersRealIDDeterministically is the regression
// test for the index-0 crash-loop: a map that appears with both a real id and
// a 0 placeholder must always resolve to the real id, regardless of which
// occurrence the randomized walk happens to visit first.
func TestExtractMapPartitions_PrefersRealIDDeterministically(t *testing.T) {
	want := map[string]int64{"Survival_1": 1, "Overmap": 2, "DeepDesert_1": 8}
	const runs = 500
	for i := 0; i < runs; i++ {
		got := ExtractMapPartitions(buildDuplicateMapSpec())
		ids := make(map[string]int64, len(got))
		for _, mi := range got {
			ids[mi.MapName] = mi.PartitionID
		}
		for name, id := range want {
			if ids[name] != id {
				t.Fatalf("run %d: map %q got partitionId=%d, want %d "+
					"(non-deterministic dedup picked the 0-valued occurrence)",
					i, name, ids[name], id)
			}
		}
	}
}

// TestExtractMapPartitions_StableOrderAcrossRuns guards the documented
// "stable ordering" contract: the returned slice must be byte-for-byte
// identical across runs so the operator's "first → main world" mental model
// and any order-sensitive downstream wiring never depend on map iteration.
func TestExtractMapPartitions_StableOrderAcrossRuns(t *testing.T) {
	first := ExtractMapPartitions(buildDuplicateMapSpec())
	for i := 0; i < 200; i++ {
		got := ExtractMapPartitions(buildDuplicateMapSpec())
		if !reflect.DeepEqual(got, first) {
			t.Fatalf("run %d: output not stable across runs\n first=%v\n got  =%v", i, first, got)
		}
	}
}

// TestExtractMapPartitions_OrderedByPartitionID asserts the deterministic
// ordering is by ascending partition id (matching the authoritative
// worldPartitions order), with map name as a stable tiebreak.
func TestExtractMapPartitions_OrderedByPartitionID(t *testing.T) {
	got := ExtractMapPartitions(buildDuplicateMapSpec())
	want := []MapInfo{
		{MapName: "Survival_1", PartitionID: 1},
		{MapName: "Overmap", PartitionID: 2},
		{MapName: "DeepDesert_1", PartitionID: 8},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// TestExtractMapPartitions_SingleOccurrence is the simple-spec sanity check:
// one map, one partition, returned with its real id.
func TestExtractMapPartitions_SingleOccurrence(t *testing.T) {
	obj := map[string]any{
		"spec": map[string]any{
			"worldPartitions": []any{
				map[string]any{"map": "Survival_1", "partitions": []any{
					map[string]any{"id": 1},
				}},
			},
		},
	}
	got := ExtractMapPartitions(obj)
	want := []MapInfo{{MapName: "Survival_1", PartitionID: 1}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// TestExtractMapPartitions_IgnoresRestartScalerPatterns covers the shape
// Funcom shipped in the 2026-08-12 depot: `.spec.restartScalers[]` is a
// scaling directive whose `map` field holds a REGEX, not a map name —
//
//	restartScalers:
//	- map: ^SH_.*
//	  size: 1
//
// Walking it like a map reference registers ServerSetScales named after the
// pattern (`<world>-^sh-.*`), which are not valid object names and can never
// correspond to a real UE5 instance. Only literal map names may be extracted.
func TestExtractMapPartitions_IgnoresRestartScalerPatterns(t *testing.T) {
	obj := map[string]any{
		"spec": map[string]any{
			"restartScalers": []any{
				map[string]any{"map": "^SH_.*", "size": 1},
				map[string]any{"map": "^(.*_)Story_.*", "size": 1},
			},
			"worldPartitions": []any{
				map[string]any{"map": "Survival_1", "partitions": []any{
					map[string]any{"id": 1},
				}},
				map[string]any{"map": "SH_Arrakeen", "partitions": []any{
					map[string]any{"id": 3},
				}},
			},
		},
	}
	want := []MapInfo{
		{MapName: "Survival_1", PartitionID: 1},
		{MapName: "SH_Arrakeen", PartitionID: 3},
	}
	got := ExtractMapPartitions(obj)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v (restartScalers regex entries must not become maps)", got, want)
	}
}

// TestExtractMapPartitions_IgnoresRegexLikeMapNames is the defensive half of
// the restartScalers fix: any future key carrying a pattern rather than a
// literal name is rejected on the value itself. Funcom map names are
// alphanumeric plus underscore; a name carrying regex metacharacters is a
// pattern by construction.
func TestExtractMapPartitions_IgnoresRegexLikeMapNames(t *testing.T) {
	obj := map[string]any{
		"spec": map[string]any{
			"someFutureScaler": []any{
				map[string]any{"map": "^CB_Overland_.*$"},
				map[string]any{"map": "Story_(A|B)"},
				map[string]any{"map": "DeepDesert_[0-9]+"},
			},
			"worldPartitions": []any{
				map[string]any{"map": "Overmap", "partitions": []any{
					map[string]any{"id": 2},
				}},
			},
		},
	}
	want := []MapInfo{{MapName: "Overmap", PartitionID: 2}}
	got := ExtractMapPartitions(obj)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v (regex-shaped map names must not be extracted)", got, want)
	}
}

// TestExtractMapPartitions_ZeroOnlyMapRetained ensures a map that never
// carries a real id is still listed (with id 0), preserving the prior
// "enumerate every referenced map" behavior rather than silently dropping it.
func TestExtractMapPartitions_ZeroOnlyMapRetained(t *testing.T) {
	obj := map[string]any{
		"spec": map[string]any{
			"serverSets": []any{
				map[string]any{"map": "OnlyScalar", "partitions": []any{7}}, // scalar -> id 0
			},
		},
	}
	got := ExtractMapPartitions(obj)
	want := []MapInfo{{MapName: "OnlyScalar", PartitionID: 0}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v (zero-id map must still be enumerated)", got, want)
	}
}
