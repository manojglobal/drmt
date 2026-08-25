package api

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"

	"github.com/jmoiron/sqlx"

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

func PositionsExitHandler(db *sqlx.DB, posManager *position.Manager, engine *execution.Engine) http.HandlerFunc {
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

					// Record ONLY successfully exited positions in SQLite
					exitPrice := p.LTP
					if exitPrice == 0 {
						exitPrice = p.AvgPrice
					}
					tranType := p.TransactionType
					if tranType == "" {
						if p.Quantity > 0 {
							tranType = "BUY"
						} else {
							tranType = "SELL"
						}
					}

					_, insertErr := db.Exec(`
						INSERT INTO exited_positions (
							position_id, broker, client_id, symbol, token, exchange,
							product_type, order_type, transaction_type, quantity,
							avg_price, exit_price, pnl, exited_at
						) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
					`, p.ID, p.Broker, p.ClientID, p.Symbol, p.Token, p.Exchange,
						p.ProductType, p.OrderType, tranType, p.Quantity,
						p.AvgPrice, exitPrice, p.PnL)

					if insertErr != nil {
						log.Printf("[DB ERROR] Failed to record exited position for %s (%s): %v", p.ClientID, p.Symbol, insertErr)
					} else {
						log.Printf("💾 [DB] Successfully saved exited position: %s (%s, %s)", p.Symbol, p.ClientID, p.Broker)
					}
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

func ExitedPositionsHandler(db *sqlx.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		fromDate := r.URL.Query().Get("from_date")
		toDate := r.URL.Query().Get("to_date")
		broker := r.URL.Query().Get("broker")
		clientID := r.URL.Query().Get("client_id")

		query := "SELECT * FROM exited_positions WHERE 1=1"
		var args []interface{}

		if fromDate != "" {
			query += " AND date(exited_at) >= date(?)"
			args = append(args, fromDate)
		}
		if toDate != "" {
			query += " AND date(exited_at) <= date(?)"
			args = append(args, toDate)
		}
		if broker != "" && broker != "ALL" {
			query += " AND broker = ?"
			args = append(args, broker)
		}
		if clientID != "" && clientID != "ALL" {
			query += " AND client_id = ?"
			args = append(args, clientID)
		}

		query += " ORDER BY exited_at DESC, id DESC"

		var exited []models.ExitedPosition
		err := db.Select(&exited, query, args...)
		if err != nil {
			log.Printf("Failed to query exited_positions: %v", err)
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode([]models.ExitedPosition{})
			return
		}

		if exited == nil {
			exited = []models.ExitedPosition{}
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(exited)
	}
}

func ExitedDatesHandler(db *sqlx.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var dates []string
		err := db.Select(&dates, "SELECT DISTINCT strftime('%Y-%m-%d', exited_at) as dt FROM exited_positions ORDER BY dt DESC")
		if err != nil || dates == nil {
			dates = []string{}
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(dates)
	}
}

