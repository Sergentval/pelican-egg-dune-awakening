package serversetscale

import "testing"

// #10: a 3-level MOCK_K8S_LIST_OMIT path must drop the nested leaf (the old
// split-on-first-dot logic silently no-op'd it).
func TestList_OmitThreeLevelPath(t *testing.T) {
	withEnv(t, true, "metadata.labels.existing")
	s := NewStore()
	seedWithLabels(t, s, "sietch-survival", "Survival_1") // labels has "existing"

	it := listItems(t, s)[0].(map[string]any)
	md := it["metadata"].(map[string]any)
	labels, ok := md["labels"].(map[string]any)
	if !ok {
		t.Fatal("item lost its labels entirely")
	}
	if _, present := labels["existing"]; present {
		t.Errorf("3-level omit did not drop metadata.labels.existing: %v", labels)
	}
	// Sibling labels must survive — only the named leaf is removed.
	if _, present := labels["igw.funcom.com/map-name"]; !present {
		t.Errorf("3-level omit over-deleted; map-name label gone: %v", labels)
	}
}
