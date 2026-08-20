package credentials

import (
	"fmt"
	"log"
	"sync"
	"time"

	"drmt/internal/config"
	"drmt/internal/models"
	"github.com/jmoiron/sqlx"
)

type DedupEngine struct {
	db        *sqlx.DB
	proxyURL  string
	staticIP  string
	tokenGens map[string]TokenGenerator
	mu        sync.Mutex
}

type TokenGenerator interface {
	GenerateToken(cred *models.Credential) (string, error)
	ValidateToken(cred *models.Credential) bool
	BrokerName() string
}

type CredentialGroup struct {
	Broker       string
	ClientID     string
	StaticIP     string
	Credentials  []*models.Credential
	WorkingCred  *models.Credential
	Error        error
}

func NewDedupEngine(db *sqlx.DB, proxyURL, staticIP string, generators ...TokenGenerator) *DedupEngine {
	genMap := make(map[string]TokenGenerator)
	for _, g := range generators {
		genMap[g.BrokerName()] = g
	}
	return &DedupEngine{
		db:        db,
		proxyURL:  proxyURL,
		staticIP:  staticIP,
		tokenGens: genMap,
	}
}

func (e *DedupEngine) ProcessCredentials(creds []ParsedCredential) error {
	groups := e.groupCredentials(creds)

	for _, group := range groups {
		if len(group.Credentials) > config.MaxCredentialSetsPerClient {
			e.markGroupMaxExceeded(group)
			continue
		}

		if err := e.processGroup(group); err != nil {
			fmt.Printf("Warning: process group %s/%s failed: %v\n", group.Broker, group.ClientID, err)
			continue
		}
	}

	return nil
}

func (e *DedupEngine) groupCredentials(creds []ParsedCredential) []*CredentialGroup {
	groupMap := make(map[string]*CredentialGroup)

	for i := range creds {
		c := &creds[i]
		key := c.Broker + ":" + c.ClientID + ":" + c.StaticIP

		if _, ok := groupMap[key]; !ok {
			groupMap[key] = &CredentialGroup{
				Broker:   c.Broker,
				ClientID: c.ClientID,
				StaticIP: c.StaticIP,
			}
		}
		groupMap[key].Credentials = append(groupMap[key].Credentials, &c.Credential)
	}

	groups := make([]*CredentialGroup, 0, len(groupMap))
	for _, g := range groupMap {
		groups = append(groups, g)
	}
	return groups
}

func (e *DedupEngine) processGroup(group *CredentialGroup) error {
	gen, ok := e.tokenGens[group.Broker]
	if !ok {
		group.Error = fmt.Errorf("no token generator for broker: %s", group.Broker)
		for _, cred := range group.Credentials {
			cred.Status = "skipped"
			cred.LastError = group.Error.Error()
			e.saveCredential(cred)
		}
		return nil
	}

	// Phase 1: Try existing access tokens
	for _, cred := range group.Credentials {
		if cred.AccessToken != "" {
			if e.validateToken(gen, cred) {
				group.WorkingCred = cred
				e.saveWorkingCredential(group, cred)
				return nil
			}
		}
	}

	// Phase 2: Generate new tokens for each credential (max 1 attempt each per rules)
	for _, cred := range group.Credentials {
		token, err := gen.GenerateToken(cred)
		if err == nil && token != "" {
			cred.AccessToken = token
			now := time.Now()
			cred.AccessTokenCreatedAt = &now
			cred.Status = "active"
			cred.RetryCount = 0
			cred.LastError = ""

			group.WorkingCred = cred
			e.saveWorkingCredential(group, cred)
			return nil
		}

		cred.RetryCount++
		cred.LastError = err.Error()
		log.Printf("Failed to generate token for %s/%s: %v", group.Broker, group.ClientID, err)
		cred.Status = "failed"
		e.saveCredential(cred)
	}

	// All failed
	group.Error = fmt.Errorf("all credentials failed for %s/%s", group.Broker, group.ClientID)
	for _, cred := range group.Credentials {
		cred.Status = "max_retries_exceeded"
		e.saveCredential(cred)
	}

	return group.Error
}

func (e *DedupEngine) validateToken(gen TokenGenerator, cred *models.Credential) bool {
	if cred.AccessToken == "" {
		return false
	}
	return gen.ValidateToken(cred)
}

func (e *DedupEngine) saveWorkingCredential(group *CredentialGroup, cred *models.Credential) {
	for _, c := range group.Credentials {
		if c.ClientID == cred.ClientID && c.UserID == cred.UserID && c.StaticIP == cred.StaticIP {
			c.AccessToken = cred.AccessToken
			c.AccessTokenCreatedAt = cred.AccessTokenCreatedAt
			c.Status = "active"
			c.RetryCount = 0
			c.LastError = ""
		} else {
			c.Status = "skipped"
		}
		e.saveCredential(c)
	}
}

func (e *DedupEngine) saveCredential(cred *models.Credential) {
	query := `
		INSERT INTO credentials (broker, api_key, api_secret, fingerprint, client_id, password, totp_secret,
			api_key_encrypted, app_code, access_token, access_token_created_at, user_id, static_ip, status, last_error, retry_count, created_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(fingerprint) DO UPDATE SET
			api_key = excluded.api_key,
			api_secret = excluded.api_secret,
			client_id = excluded.client_id,
			password = excluded.password,
			totp_secret = excluded.totp_secret,
			api_key_encrypted = excluded.api_key_encrypted,
			app_code = excluded.app_code,
			access_token = excluded.access_token,
			access_token_created_at = excluded.access_token_created_at,
			user_id = excluded.user_id,
			static_ip = excluded.static_ip,
			status = excluded.status,
			last_error = excluded.last_error,
			retry_count = excluded.retry_count
	`

	e.db.Exec(query,
		cred.Broker, cred.APIKey, cred.APISecret, cred.Fingerprint,
		cred.ClientID, cred.Password, cred.TOTPSecret,
		cred.APIKeyEncrypted, cred.AppCode,
		cred.AccessToken, cred.AccessTokenCreatedAt,
		cred.UserID, cred.StaticIP, cred.Status, cred.LastError, cred.RetryCount, cred.CreatedAt,
	)
}

func (e *DedupEngine) markGroupMaxExceeded(group *CredentialGroup) {
	for _, cred := range group.Credentials {
		cred.Status = "max_credential_sets_exceeded"
		cred.LastError = fmt.Sprintf("More than %d credential sets for client_id %s", config.MaxCredentialSetsPerClient, cred.ClientID)
		e.saveCredential(cred)
	}
}