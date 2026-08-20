package api

import (
	"context"
	"net/http"

	"drmt/internal/auth"
	"drmt/internal/models"
	"github.com/jmoiron/sqlx"
)

type contextKey string

const UserContextKey = contextKey("user")

// AuthMiddleware protects routes by validating the session cookie
func AuthMiddleware(db *sqlx.DB, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie("drmt_session")
		if err != nil {
			http.Error(w, "Unauthorized: No session cookie", http.StatusUnauthorized)
			return
		}

		user, err := auth.ValidateSession(db, cookie.Value)
		if err != nil {
			// Clear invalid cookie
			http.SetCookie(w, &http.Cookie{
				Name:     "drmt_session",
				Value:    "",
				Path:     "/",
				MaxAge:   -1,
				HttpOnly: true,
			})
			http.Error(w, "Unauthorized: Invalid or expired session", http.StatusUnauthorized)
			return
		}

		// Inject user into request context
		ctx := context.WithValue(r.Context(), UserContextKey, user)
		next.ServeHTTP(w, r.WithContext(ctx))
	}
}

// GetUserFromContext retrieves the authenticated user from the request context
func GetUserFromContext(ctx context.Context) *models.User {
	user, ok := ctx.Value(UserContextKey).(*models.User)
	if !ok {
		return nil
	}
	return user
}
