package credentials

import (
	"fmt"
	"log"
	"time"

	"drmt/internal/config"
	"drmt/internal/models"
	"github.com/jmoiron/sqlx"
)

// RefreshAccessToken attempts to regenerate the token for a specific credential up to 3 times.
func RefreshAccessToken(db *sqlx.DB, cred *models.Credential, proxyURL string) error {
	var gen TokenGenerator

	switch cred.Broker {
	case config.BrokerMStock:
		gen = NewMStockGenerator(proxyURL)
	case config.BrokerAliceBlue:
		gen = NewAliceBlueGenerator(proxyURL)
	case config.BrokerFlattrade:
		gen = NewFlattradeGenerator(proxyURL)
	default:
		return fmt.Errorf("unsupported broker for token refresh: %s", cred.Broker)
	}

	var lastErr error
	for i := 1; i <= 3; i++ {
		log.Printf("[%s/%s] Token refresh attempt %d/3...", cred.Broker, cred.ClientID, i)
		token, err := gen.GenerateToken(cred)
		if err == nil && token != "" {
			cred.AccessToken = token
			now := time.Now()
			cred.AccessTokenCreatedAt = &now
			
			// Persist new token in the database
			_, dbErr := db.Exec("UPDATE credentials SET access_token = ?, access_token_created_at = ?, status = 'active', last_error = '' WHERE fingerprint = ?", token, now, cred.Fingerprint)
			if dbErr != nil {
				return fmt.Errorf("failed to save refreshed token to DB: %w", dbErr)
			}
			log.Printf("[%s/%s] Token successfully refreshed and saved to DB", cred.Broker, cred.ClientID)
			return nil
		}
		
		lastErr = err
		log.Printf("[%s/%s] Refresh attempt %d failed: %v", cred.Broker, cred.ClientID, i, err)
		
		if i < 3 {
			// Backoff before retry
			time.Sleep(time.Second * time.Duration(i*2))
		}
	}

	// If we exhausted all 3 retries, mark as failed in DB
	db.Exec("UPDATE credentials SET status = 'failed', last_error = ? WHERE fingerprint = ?", lastErr.Error(), cred.Fingerprint)
	return fmt.Errorf("all 3 token refresh attempts failed: %v", lastErr)
}
