package position

import (
	"context"
	"fmt"
"sort"
	"log"
	"sync"
	"time"

	"drmt/internal/models"
)

// Manager is the thread-safe central repository for live positions.
type Manager struct {
	mu        sync.RWMutex
	positions map[string]models.Position
	Feed      chan models.Position
}

func NewManager() *Manager {
	return &Manager{
		positions: make(map[string]models.Position),
		Feed:      make(chan models.Position, 1000), // 1000 buffer for bursts
	}
}

// Start listens on the Feed channel and safely writes to the map.
func (m *Manager) Start(ctx context.Context) {
	log.Println("Starting central Position Manager...")
	for {
		select {
		case <-ctx.Done():
			log.Println("Position Manager stopping...")
			return
		case pos := <-m.Feed:
			pos.UpdatedAt = time.Now()

			if pos.ID == "" {
				pos.ID = fmt.Sprintf("%s:%s:%s", pos.Broker, pos.ClientID, pos.Symbol)
			}

			m.mu.Lock()
			if pos.Quantity == 0 {
				delete(m.positions, pos.ID)
			} else {
				m.positions[pos.ID] = pos
			}
			m.mu.Unlock()
		}
	}
}

// GetAll returns a snapshot of all current positions.
func (m *Manager) GetAll() []models.Position {
	m.mu.RLock()
	defer m.mu.RUnlock()

	var result []models.Position
	for _, p := range m.positions {
		result = append(result, p)
	}

	// Sort to ensure stable UI rendering
	sort.Slice(result, func(i, j int) bool {
		return result[i].ID < result[j].ID
	})

	return result
}

// GetByClient returns positions specific to a client ID.
func (m *Manager) GetByClient(clientID string) []models.Position {
	m.mu.RLock()
	defer m.mu.RUnlock()

	var result []models.Position
	for _, p := range m.positions {
		if p.ClientID == clientID {
			result = append(result, p)
		}
	}

	// Sort to ensure stable UI rendering
	sort.Slice(result, func(i, j int) bool {
		return result[i].ID < result[j].ID
	})

	return result
}

// GetByID returns a specific position by its unique ID
func (m *Manager) GetByID(id string) (models.Position, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	
	pos, exists := m.positions[id]
	return pos, exists
}
