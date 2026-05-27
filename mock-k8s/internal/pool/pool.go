// Package pool allocates contiguous game/IGW port pairs for UE5 instances.
//
// The Funcom Director controls scale-out by patching spec.replicas on a
// ServerSetScale CR; mock-k8s reconciles by spawning enough UE5 processes
// to match. Each UE5 instance binds one UDP game port (clients connect
// there) and one TCP IGW port (Director ↔ UE5 IPC).
//
// Pools are configured at startup with a base + size:
//
//	game ports: [K8S_POOL_GAME_PORT_BASE, K8S_POOL_GAME_PORT_BASE + K8S_POOL_SIZE)
//	IGW  ports: [K8S_POOL_IGW_PORT_BASE,  K8S_POOL_IGW_PORT_BASE  + K8S_POOL_SIZE)
//
// We hand out the next free index in each pool; releasing returns it.
package pool

import (
	"fmt"
	"sync"
)

// Pool is a fixed-size set of integer port pairs. Safe for concurrent use.
type Pool struct {
	mu      sync.Mutex
	gameBase int
	igwBase  int
	size     int
	used     []bool // index → in-use
}

// New constructs a Pool of size port pairs starting at gameBase / igwBase.
// Returns an error if size is non-positive or the ranges overlap with
// well-known privileged ports (<1024).
func New(gameBase, igwBase, size int) (*Pool, error) {
	if size <= 0 {
		return nil, fmt.Errorf("pool size must be positive, got %d", size)
	}
	if gameBase < 1024 || igwBase < 1024 {
		return nil, fmt.Errorf("port bases must be ≥1024, got game=%d igw=%d", gameBase, igwBase)
	}
	if (gameBase < igwBase+size) && (igwBase < gameBase+size) {
		return nil, fmt.Errorf("game pool [%d..%d) overlaps IGW pool [%d..%d)",
			gameBase, gameBase+size, igwBase, igwBase+size)
	}
	return &Pool{
		gameBase: gameBase,
		igwBase:  igwBase,
		size:     size,
		used:     make([]bool, size),
	}, nil
}

// Allocation is a single port pair belonging to one UE5 instance.
type Allocation struct {
	Index    int // slot 0..size-1
	GamePort int
	IGWPort  int
}

// Acquire returns the next free Allocation. Errors if the pool is full.
func (p *Pool) Acquire() (Allocation, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for i, used := range p.used {
		if !used {
			p.used[i] = true
			return Allocation{Index: i, GamePort: p.gameBase + i, IGWPort: p.igwBase + i}, nil
		}
	}
	return Allocation{}, fmt.Errorf("pool exhausted (%d/%d used)", p.size, p.size)
}

// AcquireSpecific reserves the requested index. Used when restoring state
// after a mock-k8s restart (the Director re-creates resources with their
// previous partition assignments). Returns an error if the slot is taken
// or out of range.
func (p *Pool) AcquireSpecific(index int) (Allocation, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if index < 0 || index >= p.size {
		return Allocation{}, fmt.Errorf("index %d out of range [0,%d)", index, p.size)
	}
	if p.used[index] {
		return Allocation{}, fmt.Errorf("index %d already in use", index)
	}
	p.used[index] = true
	return Allocation{Index: index, GamePort: p.gameBase + index, IGWPort: p.igwBase + index}, nil
}

// Release returns a slot to the free list. Idempotent — releasing an
// already-free slot is a no-op.
func (p *Pool) Release(index int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if index >= 0 && index < p.size {
		p.used[index] = false
	}
}

// Stats reports utilization for diagnostics.
func (p *Pool) Stats() (used, free, total int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, u := range p.used {
		if u {
			used++
		}
	}
	return used, p.size - used, p.size
}
