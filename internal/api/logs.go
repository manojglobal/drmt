package api

import (
	"encoding/json"
	"net/http"
	"drmt/internal/logger"
)

func LogsHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		
		logType := r.URL.Query().Get("type")
		var history []logger.LogEntry

		switch logType {
		case "auth":
			history = logger.Auth.GetHistory()
		case "orders":
			history = logger.Exit.GetHistory()
		case "system":
			history = logger.System.GetHistory()
		default:
			// Combine all if no type specified (limited to 100 total)
			history = append(history, logger.Auth.GetHistory()...)
			history = append(history, logger.Exit.GetHistory()...)
			history = append(history, logger.System.GetHistory()...)
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(history)
	}
}
