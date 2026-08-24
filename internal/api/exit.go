package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sync"

	"drmt/internal/execution"
	"drmt/internal/models"
	"drmt/internal/position"
)

type ExitRequest struct {
	PositionIDs []string `json:"position_ids"`
}

type ExitResult struct {
	PositionID string `json:"position_id"`
	Symbol     string `json:"symbol"`
	Broker     string `json:"broker"`
	Success    bool   `json:"success"`
	Message    string `json:"message"`
}

type ExitResponse struct {
	Success bool         `json:"success"`
	Message string       `json:"message"`
	Results []ExitResult `json:"results"`
}

func PositionsExitHandler(posManager *position.Manager, engine *execution.Engine) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req ExitRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Bad request", http.StatusBadRequest)
			return
		}

		var wg sync.WaitGroup
		var mu sync.Mutex
		results := make([]ExitResult, 0, len(req.PositionIDs))

		for _, id := range req.PositionIDs {
			pos, exists := posManager.GetByID(id)
			if !exists {
				continue
			}
			
			wg.Add(1)
			go func(p models.Position) {
				defer wg.Done()
				err := engine.ExecuteExit(p)
				
				mu.Lock()
				defer mu.Unlock()
				if err != nil {
					results = append(results, ExitResult{PositionID: p.ID, Symbol: p.Symbol, Broker: p.Broker, Success: false, Message: err.Error()})
				} else {
					results = append(results, ExitResult{PositionID: p.ID, Symbol: p.Symbol, Broker: p.Broker, Success: true, Message: "Order placed successfully"})
				}
			}(pos)
		}
		
		wg.Wait()

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(ExitResponse{
			Success: true, 
			Message: fmt.Sprintf("Processed %d exit(s)", len(results)),
			Results: results,
		})
	}
}
