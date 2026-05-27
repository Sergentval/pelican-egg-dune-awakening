// Package serversetscale implements the ServerSetScale CRD storage that
// the Funcom Battlegroup Director treats as its scale-out API.
//
// In real K8s, the Director would post a ServerSetScale resource per map
// (Survival_1, Overmap, DeepDesert_1, …) with spec.replicas = desired
// concurrent UE5 instances. A controller (here, the Spawner package)
// reconciles by starting/stopping UE5 processes and writing status back.
//
// This package owns the data; the Spawner is wired in via the
// OnSpecChange callback. Keeping storage and reconciliation decoupled
// means future test code can poke the store directly without spawning
// real processes.
package serversetscale

import (
	"strconv"
	"sync"
	"time"
)

// Object is a single ServerSetScale resource. Field names match the
// JSON wire format K8s clients expect (lowercase first letter, no
// underscores). Unmarshaling is forgiving — we accept anything Director
// sends and only act on the fields we care about (spec.replicas,
// metadata.name, metadata.namespace).
type Object struct {
	APIVersion string         `json:"apiVersion,omitempty"`
	Kind       string         `json:"kind,omitempty"`
	Metadata   Metadata       `json:"metadata"`
	Spec       map[string]any `json:"spec,omitempty"`
	Status     map[string]any `json:"status,omitempty"`
}

// Metadata is the subset of metav1.ObjectMeta we honor.
type Metadata struct {
	Name              string            `json:"name"`
	Namespace         string            `json:"namespace"`
	UID               string            `json:"uid,omitempty"`
	ResourceVersion   string            `json:"resourceVersion,omitempty"`
	Generation        int64             `json:"generation,omitempty"`
	CreationTimestamp string            `json:"creationTimestamp,omitempty"`
	Labels            map[string]string `json:"labels,omitempty"`
	Annotations       map[string]string `json:"annotations,omitempty"`
}

// Event is what we push to watchers. EventType is K8s-conventional:
// ADDED / MODIFIED / DELETED.
type Event struct {
	Type   string `json:"type"`
	Object Object `json:"object"`
}

// LazyMapInfo carries the fields needed to materialize a ServerSetScale
// the first time the Director GETs it by name. Populated by main.go
// from the BattleGroup spec at startup.
type LazyMapInfo struct {
	MapName     string
	PartitionID int64
	Replicas    int64
}

// LazyCreator is consulted on Get-by-name when the requested resource
// doesn't exist yet. If LazyCreator.Maps has an entry for the looked-up
// canonical name, the store auto-creates the SSS with the recorded
// spec — making the GET succeed instead of 404'ing.
//
// This sidesteps the Director's startup crash where ListServerSetScales
// throws ArgumentNullException on a fully pre-populated list, while
// still satisfying the per-map by-name GETs Director does at startup.
type LazyCreator struct {
	WorldName string
	Maps      map[string]LazyMapInfo // canonical SSS name → spec source
}

// Store is an in-memory ServerSetScale registry plus a fan-out of change
// events to live watchers. Safe for concurrent use.
type Store struct {
	mu              sync.Mutex
	objects         map[key]Object // (namespace, name) → object
	resourceVersion int64
	watchers        map[chan Event]struct{}

	// OnSpecChange fires when a spec update is committed. The Spawner
	// reads spec.replicas off obj and reconciles. Calling Update from
	// inside this callback is supported (status writes don't re-fire
	// OnSpecChange, only spec writes do).
	OnSpecChange func(obj Object)

	// LazyCreator, when non-nil, lets GetOrLazyCreate materialize a
	// ServerSetScale on first by-name GET. Used to avoid the
	// pre-populated list shape that crashes Director.
	LazyCreator *LazyCreator
}

type key struct{ namespace, name string }

// NewStore returns an empty Store.
func NewStore() *Store {
	return &Store{
		objects:  make(map[key]Object),
		watchers: make(map[chan Event]struct{}),
	}
}

// List returns a snapshot of all objects in the given namespace. Pass
// empty namespace to list across all namespaces.
func (s *Store) List(namespace string) []Object {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Object
	for k, v := range s.objects {
		if namespace != "" && k.namespace != namespace {
			continue
		}
		out = append(out, v)
	}
	return out
}

// Get returns one object by (namespace, name).
func (s *Store) Get(namespace, name string) (Object, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	o, ok := s.objects[key{namespace, name}]
	return o, ok
}

// GetOrLazyCreate returns the object if it exists; otherwise, if
// LazyCreator has a recipe for this name, materializes it on the fly
// and returns the freshly-created object. Used by the handler's
// by-name GET to satisfy the Director's startup queries without
// pre-populating the LIST (which crashes Director's deserializer).
func (s *Store) GetOrLazyCreate(namespace, name string) (Object, bool) {
	if obj, ok := s.Get(namespace, name); ok {
		return obj, true
	}
	s.mu.Lock()
	lc := s.LazyCreator
	s.mu.Unlock()
	if lc == nil {
		return Object{}, false
	}
	info, ok := lc.Maps[name]
	if !ok {
		return Object{}, false
	}
	// Build the SSS shape with the labels Director's deserializer
	// reads as Dictionary keys when iterating ListServerSetScales. The
	// k8s labels `igw.funcom.com/map-name` and
	// `igw.funcom.com/battlegroup-name` show up in the Director
	// binary's strings as resource identifiers — populating them is
	// the most likely cure for the null-key Dictionary.Add crash.
	obj := Object{
		Metadata: Metadata{
			Name:      name,
			Namespace: namespace,
			Labels: map[string]string{
				"igw.funcom.com/battlegroup-name": lc.WorldName,
				"igw.funcom.com/map-name":         info.MapName,
			},
		},
		Spec: map[string]any{
			"mapName":         info.MapName,
			"battlegroupName": lc.WorldName,
			"partitionId":     info.PartitionID,
			"replicas":        info.Replicas,
		},
	}
	created, ok := s.Create(obj)
	return created, ok
}

// CurrentResourceVersion returns the latest server-wide resourceVersion
// so list responses can advertise where a subsequent watch should
// resume from.
func (s *Store) CurrentResourceVersion() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strconv.FormatInt(s.resourceVersion, 10)
}

// Create inserts a new object, stamping resourceVersion/uid/creationTimestamp.
// Returns the stored object and an "exists" error sentinel-shape if the
// (namespace, name) is taken (handlers translate to HTTP 409).
func (s *Store) Create(obj Object) (Object, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := key{obj.Metadata.Namespace, obj.Metadata.Name}
	if _, exists := s.objects[k]; exists {
		return Object{}, false
	}
	s.resourceVersion++
	obj.APIVersion = "igw.funcom.com/v1"
	obj.Kind = "ServerSetScale"
	obj.Metadata.UID = newUID()
	obj.Metadata.ResourceVersion = strconv.FormatInt(s.resourceVersion, 10)
	obj.Metadata.Generation = 1
	obj.Metadata.CreationTimestamp = time.Now().UTC().Format(time.RFC3339)
	if obj.Status == nil {
		obj.Status = map[string]any{
			"observedGeneration": int64(0),
			"completedReplicas":  int64(0),
		}
	}
	s.objects[k] = obj
	s.broadcast(Event{Type: "ADDED", Object: obj})
	cb := s.OnSpecChange
	s.mu.Unlock()
	if cb != nil {
		cb(obj)
	}
	s.mu.Lock()
	return obj, true
}

// Update replaces an existing object's spec/metadata.labels and stamps a
// new resourceVersion. The status subresource has its own write path
// (UpdateStatus) so callers don't accidentally clobber controller state.
// Increments generation when spec changes.
func (s *Store) Update(obj Object) (Object, bool) {
	s.mu.Lock()
	k := key{obj.Metadata.Namespace, obj.Metadata.Name}
	prev, exists := s.objects[k]
	if !exists {
		s.mu.Unlock()
		return Object{}, false
	}
	s.resourceVersion++
	obj.Metadata.UID = prev.Metadata.UID
	obj.Metadata.ResourceVersion = strconv.FormatInt(s.resourceVersion, 10)
	obj.Metadata.CreationTimestamp = prev.Metadata.CreationTimestamp
	specChanged := !mapsEqualish(prev.Spec, obj.Spec)
	if specChanged {
		obj.Metadata.Generation = prev.Metadata.Generation + 1
	} else {
		obj.Metadata.Generation = prev.Metadata.Generation
	}
	// Preserve status — clients usually PUT only spec.
	if obj.Status == nil {
		obj.Status = prev.Status
	}
	s.objects[k] = obj
	s.broadcast(Event{Type: "MODIFIED", Object: obj})
	cb := s.OnSpecChange
	s.mu.Unlock()
	if cb != nil && specChanged {
		cb(obj)
	}
	return obj, true
}

// UpdateStatus writes only the status subresource. Does NOT increment
// generation and does NOT fire OnSpecChange.
func (s *Store) UpdateStatus(namespace, name string, status map[string]any) (Object, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := key{namespace, name}
	obj, exists := s.objects[k]
	if !exists {
		return Object{}, false
	}
	s.resourceVersion++
	obj.Metadata.ResourceVersion = strconv.FormatInt(s.resourceVersion, 10)
	obj.Status = status
	s.objects[k] = obj
	s.broadcast(Event{Type: "MODIFIED", Object: obj})
	return obj, true
}

// Patch merges in a JSON merge-patch into the existing object. Returns
// the new object. Used for both spec and status patches; we sniff for
// .status in the patch and treat that as a status-only update.
func (s *Store) Patch(namespace, name string, patch map[string]any, statusSubresource bool) (Object, bool) {
	s.mu.Lock()
	k := key{namespace, name}
	obj, exists := s.objects[k]
	if !exists {
		s.mu.Unlock()
		return Object{}, false
	}
	s.resourceVersion++
	obj.Metadata.ResourceVersion = strconv.FormatInt(s.resourceVersion, 10)
	specChanged := false
	if statusSubresource {
		if obj.Status == nil {
			obj.Status = map[string]any{}
		}
		mergeInto(obj.Status, patch)
	} else {
		if patchSpec, ok := patch["spec"].(map[string]any); ok {
			if obj.Spec == nil {
				obj.Spec = map[string]any{}
			}
			before := cloneMap(obj.Spec)
			mergeInto(obj.Spec, patchSpec)
			if !mapsEqualish(before, obj.Spec) {
				specChanged = true
				obj.Metadata.Generation++
			}
		}
		if patchStatus, ok := patch["status"].(map[string]any); ok {
			if obj.Status == nil {
				obj.Status = map[string]any{}
			}
			mergeInto(obj.Status, patchStatus)
		}
		if patchMeta, ok := patch["metadata"].(map[string]any); ok {
			if labels, ok := patchMeta["labels"].(map[string]any); ok {
				if obj.Metadata.Labels == nil {
					obj.Metadata.Labels = map[string]string{}
				}
				for k, v := range labels {
					if vs, ok := v.(string); ok {
						obj.Metadata.Labels[k] = vs
					}
				}
			}
		}
	}
	s.objects[k] = obj
	s.broadcast(Event{Type: "MODIFIED", Object: obj})
	cb := s.OnSpecChange
	s.mu.Unlock()
	if cb != nil && specChanged {
		cb(obj)
	}
	return obj, true
}

// Delete removes an object. Returns true if it existed.
func (s *Store) Delete(namespace, name string) (Object, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	k := key{namespace, name}
	obj, exists := s.objects[k]
	if !exists {
		return Object{}, false
	}
	delete(s.objects, k)
	s.resourceVersion++
	s.broadcast(Event{Type: "DELETED", Object: obj})
	return obj, true
}

// Subscribe registers a channel that will receive every subsequent
// change event. Caller must drain promptly; broadcast is non-blocking
// and drops events if the buffer fills (slow watchers hurt only
// themselves).
func (s *Store) Subscribe(buf int) chan Event {
	if buf < 1 {
		buf = 64
	}
	ch := make(chan Event, buf)
	s.mu.Lock()
	s.watchers[ch] = struct{}{}
	s.mu.Unlock()
	return ch
}

// Unsubscribe deregisters and closes a watcher channel.
func (s *Store) Unsubscribe(ch chan Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.watchers[ch]; ok {
		delete(s.watchers, ch)
		close(ch)
	}
}

// broadcast must be called with s.mu held.
func (s *Store) broadcast(ev Event) {
	for ch := range s.watchers {
		select {
		case ch <- ev:
		default:
			// Drop — slow watcher. K8s clients re-list on disconnect.
		}
	}
}

func cloneMap(m map[string]any) map[string]any {
	if m == nil {
		return nil
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

// mergeInto performs a JSON merge-patch (RFC 7396) of src into dst,
// in-place. Null values in src delete the key; nested maps recurse.
func mergeInto(dst, src map[string]any) {
	for k, v := range src {
		if v == nil {
			delete(dst, k)
			continue
		}
		if srcMap, ok := v.(map[string]any); ok {
			if dstMap, ok := dst[k].(map[string]any); ok {
				mergeInto(dstMap, srcMap)
				continue
			}
		}
		dst[k] = v
	}
}

// mapsEqualish is a cheap structural equality for the purpose of detecting
// "did spec change?". Uses JSON-comparable types only (maps, slices,
// strings, numbers, bools). False negatives (saying "changed" when
// equal) are harmless — we'd just bump generation unnecessarily.
func mapsEqualish(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, va := range a {
		vb, ok := b[k]
		if !ok {
			return false
		}
		if !valuesEqualish(va, vb) {
			return false
		}
	}
	return true
}

func valuesEqualish(a, b any) bool {
	switch av := a.(type) {
	case map[string]any:
		bv, ok := b.(map[string]any)
		if !ok {
			return false
		}
		return mapsEqualish(av, bv)
	case []any:
		bv, ok := b.([]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !valuesEqualish(av[i], bv[i]) {
				return false
			}
		}
		return true
	default:
		return a == b
	}
}

func newUID() string {
	// 16 random bytes, hex-encoded, in 8-4-4-4-12 layout to look like a UUIDv4.
	// Cryptographic strength isn't required — K8s clients only check uniqueness.
	var b [16]byte
	for i := range b {
		// math/rand is fine; we don't need unpredictability.
		b[i] = byte((uidCounter >> (8 * (i % 8))) ^ uint64(i*131))
		uidCounter = uidCounter*6364136223846793005 + 1442695040888963407
	}
	const hex = "0123456789abcdef"
	out := make([]byte, 36)
	pos := 0
	for i, x := range b {
		out[pos] = hex[x>>4]
		out[pos+1] = hex[x&0x0f]
		pos += 2
		if i == 3 || i == 5 || i == 7 || i == 9 {
			out[pos] = '-'
			pos++
		}
	}
	return string(out)
}

// uidCounter is the LCG state used by newUID. Package-level so successive
// calls produce distinct UIDs even within the same nanosecond.
var uidCounter uint64 = 0xdeadbeefcafe1234
