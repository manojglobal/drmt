package api

import (
	"encoding/json"
	"net/http"
	"time"

	"drmt/internal/auth"
	"drmt/internal/models"
	"github.com/jmoiron/sqlx"
)

type LoginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type LoginResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// LoginHandler authenticates the admin and sets the session cookie
func LoginHandler(db *sqlx.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var req LoginRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Bad request", http.StatusBadRequest)
			return
		}

		// Find user
		var user models.User
		err := db.Get(&user, "SELECT id, username, password_hash, created_at FROM users WHERE username = ?", req.Username)
		if err != nil {
			http.Error(w, "Invalid credentials", http.StatusUnauthorized)
			return
		}

		// Check password
		if !auth.CheckPasswordHash(req.Password, user.PasswordHash) {
			http.Error(w, "Invalid credentials", http.StatusUnauthorized)
			return
		}

		// Create Session
		token, err := auth.CreateSession(db, user.ID)
		if err != nil {
			http.Error(w, "Internal server error", http.StatusInternalServerError)
			return
		}

		// Set HttpOnly cookie
		http.SetCookie(w, &http.Cookie{
			Name:     "drmt_session",
			Value:    token,
			Path:     "/",
			Expires:  time.Now().Add(24 * time.Hour),
			HttpOnly: true,
			SameSite: http.SameSiteStrictMode,
		})

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(LoginResponse{Success: true, Message: "Logged in successfully"})
	}
}

// LogoutHandler destroys the session and clears the cookie
func LogoutHandler(db *sqlx.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		cookie, err := r.Cookie("drmt_session")
		if err == nil {
			auth.DeleteSession(db, cookie.Value)
		}

		http.SetCookie(w, &http.Cookie{
			Name:     "drmt_session",
			Value:    "",
			Path:     "/",
			MaxAge:   -1,
			HttpOnly: true,
			SameSite: http.SameSiteStrictMode,
		})

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(LoginResponse{Success: true, Message: "Logged out successfully"})
	}
}

// MeHandler returns the currently authenticated user
func MeHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user := GetUserFromContext(r.Context())
		if user == nil {
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(user)
	}
}

// PositionsHandler returns all live positions from the Position Manager
func PositionsHandler(posManager interface {
	GetAll() []models.Position
}) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(posManager.GetAll())
	}
}
