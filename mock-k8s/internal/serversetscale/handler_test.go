package serversetscale

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// withEnv routes the handler's env reads through a fake for one test.
func withEnv(t *testing.T, enable bool, omit string) {
	t.Helper()
	old := osLookupEnv
	osLookupEnv = func(name string) string {
		switch name {
		case "MOCK_K8S_LIST_ENABLE":
			if enable {
				return "1"
			}
		case "MOCK_K8S_LIST_OMIT":
			return omit
		}
		return ""
	}
	t.Cleanup(func() { osLookupEnv = old })
}

func seedBare(t *testing.T, s *Store, name, mapName string) {
	t.Helper()
	if _, ok := s.Create(Object{
		Metadata: Metadata{Namespace: "default", Name: name},
		Spec:     map[string]any{"mapName": mapName, "battlegroupName": "w", "replicas": int64(1)},
	}); !ok {
		t.Fatalf("seed %s failed", name)
	}
}

func seedWithLabels(t *testing.T, s *Store, name, mapName string) {
	t.Helper()
	if _, ok := s.Create(Object{
		Metadata: Metadata{Namespace: "default", Name: name, Labels: map[string]string{"existing": "x"}},
		Spec:     map[string]any{"mapName": mapName, "battlegroupName": "w", "replicas": int64(1)},
	}); !ok {
		t.Fatalf("seed %s failed", name)
	}
}

func listItems(t *testing.T, s *Store) []any {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/apis/igw.funcom.com/v1/namespaces/default/serversetscales", nil)
	rec := httptest.NewRecorder()
	Handler(s)(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("LIST status = %d, want 200", rec.Code)
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("decode LIST: %v\nbody: %s", err, rec.Body.String())
	}
	items, _ := out["items"].([]any)
	return items
}

func TestList_EmptyByDefault(t *testing.T) {
	withEnv(t, false, "")
	s := NewStore()
	seedBare(t, s, "sietch-survival", "Survival_1")
	if items := listItems(t, s); len(items) != 0 {
		t.Fatalf("default LIST should be empty even with objects present, got %d", len(items))
	}
}

func TestList_EnabledShapesItemsUniformly(t *testing.T) {
	withEnv(t, true, "")
	s := NewStore()
	seedBare(t, s, "sietch-survival", "Survival_1") // no labels on the seed

	items := listItems(t, s)
	if len(items) != 1 {
		t.Fatalf("enabled LIST items = %d, want 1", len(items))
	}
	it := items[0].(map[string]any)

	md, ok := it["metadata"].(map[string]any)
	if !ok {
		t.Fatal("item has no metadata")
	}
	labels, ok := md["labels"].(map[string]any)
	if !ok {
		t.Fatal("LIST item missing labels — uniform shaping not applied")
	}
	if labels["igw.funcom.com/map-name"] != "Survival_1" {
		t.Errorf("map-name label = %v, want Survival_1", labels["igw.funcom.com/map-name"])
	}
	if labels["igw.funcom.com/battlegroup-name"] != "w" {
		t.Errorf("battlegroup-name label = %v, want w", labels["igw.funcom.com/battlegroup-name"])
	}
	if _, ok := it["status"].(map[string]any); !ok {
		t.Error("LIST item missing status")
	}
	if _, ok := it["spec"].(map[string]any); !ok {
		t.Error("LIST item missing spec")
	}
}

func TestList_OmitTopLevelField(t *testing.T) {
	withEnv(t, true, "status")
	s := NewStore()
	seedBare(t, s, "sietch-survival", "Survival_1")

	it := listItems(t, s)[0].(map[string]any)
	if _, present := it["status"]; present {
		t.Errorf("MOCK_K8S_LIST_OMIT=status did not drop status: %v", it)
	}
}

func TestList_OmitNestedField(t *testing.T) {
	withEnv(t, true, "metadata.labels")
	s := NewStore()
	seedWithLabels(t, s, "sietch-survival", "Survival_1")

	it := listItems(t, s)[0].(map[string]any)
	md := it["metadata"].(map[string]any)
	if _, present := md["labels"]; present {
		t.Errorf("MOCK_K8S_LIST_OMIT=metadata.labels did not drop labels: %v", md)
	}
}

func TestList_OmitDeeplyNestedField(t *testing.T) {
	// A multi-level path (more than one dot) must descend one map level per
	// dot and drop the leaf key, not silently no-op. The bisection knob is
	// useless if it can only reach one level deep.
	withEnv(t, true, "metadata.labels.existing")
	s := NewStore()
	seedWithLabels(t, s, "sietch-survival", "Survival_1") // seeds label existing=x

	it := listItems(t, s)[0].(map[string]any)
	md := it["metadata"].(map[string]any)
	labels, ok := md["labels"].(map[string]any)
	if !ok {
		t.Fatal("labels map was removed; only the leaf key should be dropped")
	}
	if _, present := labels["existing"]; present {
		t.Errorf("MOCK_K8S_LIST_OMIT=metadata.labels.existing did not drop the nested key: %v", labels)
	}
	// Sibling derived label must survive — only the targeted leaf is dropped.
	if _, present := labels["igw.funcom.com/map-name"]; !present {
		t.Errorf("deep omit removed a sibling label: %v", labels)
	}
}

func TestList_DoesNotMutateStoredObject(t *testing.T) {
	withEnv(t, true, "")
	s := NewStore()
	seedWithLabels(t, s, "sietch-survival", "Survival_1")

	_ = listItems(t, s) // ensureUniformItem must clone, not mutate, the store

	obj, ok := s.Get("default", "sietch-survival")
	if !ok {
		t.Fatal("stored object vanished")
	}
	if _, leaked := obj.Metadata.Labels["igw.funcom.com/map-name"]; leaked {
		t.Errorf("LIST leaked a derived label into the stored object: %v", obj.Metadata.Labels)
	}
	if len(obj.Metadata.Labels) != 1 {
		t.Errorf("stored object labels mutated: %v", obj.Metadata.Labels)
	}
}
