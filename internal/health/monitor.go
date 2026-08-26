package health

import (
	"context"
	"io"
	"net/http"
	"sync"
	"time"

	"drmt/internal/config"
	"drmt/internal/logger"
)

type ServerStatus struct {
	Status       string  `json:"status"`
	Message      string  `json:"message"`
	LatencyMS    float64 `json:"latency_ms"`
	QueryTime    string  `json:"query_time"`
	ResponseTime string  `json:"response_time"`
	LastSuccessfulTime string `json:"last_successful_time"`
}

type Monitor struct {
	mu       sync.RWMutex
	statuses map[string]ServerStatus
	servers  map[string]string
	client   *http.Client
}

func NewMonitor() *Monitor {
	return &Monitor{
		statuses: make(map[string]ServerStatus),
		servers: config.ServersToCheck,
		client: &http.Client{
			Timeout: 2 * time.Second, // Strict 2-second timeout as requested
		},
	}
}

func getKolkataTime() string {
	loc, err := time.LoadLocation("Asia/Kolkata")
	if err != nil {
		loc = time.Local // Fallback if tzdata isn't available
	}
	return time.Now().In(loc).Format("2006-01-02 15:04:05")
}

func (m *Monitor) Start(ctx context.Context) {
	logger.Health.Info("Starting Health Monitor service (1 minute interval)...")

	// Run immediately on startup
	m.pingAll()

	ticker := time.NewTicker(1 * time.Minute)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			logger.Health.Info("Health Monitor stopping...")
			return
		case <-ticker.C:
			m.pingAll()
		}
	}
}

func (m *Monitor) pingAll() {
	logger.Health.Info("🔄 Pinging servers for health check...")
	var wg sync.WaitGroup

	for name, url := range m.servers {
		wg.Add(1)
		go m.pingServer(&wg, name, url) // Concurrent execution
	}
	
	wg.Wait() // Wait for all goroutines to finish
}

func (m *Monitor) pingServer(wg *sync.WaitGroup, name, url string) {
	defer wg.Done()

	queryTime := getKolkataTime()
	start := time.Now()

	req, _ := http.NewRequest("GET", url, nil)
	resp, err := m.client.Do(req)
	
	latency := float64(time.Since(start).Milliseconds())
	responseTime := getKolkataTime()

	// Get previous status to preserve LastSuccessfulTime if it goes down
	m.mu.RLock()
	prev, exists := m.statuses[name]
	m.mu.RUnlock()

	var data ServerStatus
	data.QueryTime = queryTime
	data.ResponseTime = responseTime
	data.LatencyMS = latency
	if exists {
		data.LastSuccessfulTime = prev.LastSuccessfulTime
	}

	if err != nil {
		data.Status = "DOWN"
		data.Message = err.Error()
	} else {
		defer resp.Body.Close()
		bodyBytes, _ := io.ReadAll(resp.Body)
		msg := string(bodyBytes)
		
		// Truncate massively long HTML error responses
		if len(msg) > 150 {
			msg = msg[:150] + "..."
		}

		if resp.StatusCode == 200 {
			data.Status = "UP"
			data.LastSuccessfulTime = responseTime // Update to now
		} else {
			data.Status = "DOWN"
		}
		data.Message = msg
	}

	// Thread-safe map write
	m.mu.Lock()
	m.statuses[name] = data
	m.mu.Unlock()

	logger.Health.Info("Updated health:%s -> %s (%.2f ms)", name, data.Status, data.LatencyMS)
}

func (m *Monitor) GetStatuses() map[string]ServerStatus {
	m.mu.RLock()
	defer m.mu.RUnlock()

	// Return a clean copy to prevent race conditions during JSON marshalling
	copy := make(map[string]ServerStatus)
	for k, v := range m.statuses {
		copy[k] = v
	}
	return copy
}
