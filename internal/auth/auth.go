package auth

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"

	"drmt/internal/models"
	"github.com/jmoiron/sqlx"
	"golang.org/x/crypto/bcrypt"
)

// HashPassword creates a bcrypt hash of the password
func HashPassword(password string) (string, error) {
	bytes, err := bcrypt.GenerateFromPassword([]byte(password), 14)
	return string(bytes), err
}

// CheckPasswordHash compares a password with a hash
func CheckPasswordHash(password, hash string) bool {
	err := bcrypt.CompareHashAndPassword([]byte(hash), []byte(password))
	return err == nil
}

// GenerateToken creates a secure random hex string for session tokens
func GenerateToken() (string, error) {
	bytes := make([]byte, 32)
	if _, err := rand.Read(bytes); err != nil {
		return "", err
	}
	return hex.EncodeToString(bytes), nil
}

// CreateSession inserts a new session into the database and returns the token
func CreateSession(db *sqlx.DB, userID int) (string, error) {
	token, err := GenerateToken()
	if err != nil {
		return "", err
	}

	// Session valid for 24 hours
	expiresAt := time.Now().Add(24 * time.Hour)

	_, err = db.Exec("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
		token, userID, expiresAt)
	if err != nil {
		return "", err
	}

	return token, nil
}

// ValidateSession checks if a token is valid and returns the associated User
func ValidateSession(db *sqlx.DB, token string) (*models.User, error) {
	var user models.User
	query := `
		SELECT u.id, u.username, u.password_hash, u.created_at
		FROM users u
		JOIN sessions s ON u.id = s.user_id
		WHERE s.token = ? AND s.expires_at > ?
	`
	err := db.Get(&user, query, token, time.Now())
	if err != nil {
		return nil, fmt.Errorf("invalid or expired session")
	}

	return &user, nil
}

// DeleteSession removes a session from the database
func DeleteSession(db *sqlx.DB, token string) error {
	_, err := db.Exec("DELETE FROM sessions WHERE token = ?", token)
	return err
}
