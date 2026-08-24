package credentials

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"time"

	"drmt/internal/models"
)

type RawCredentialRow struct {
	ID           string
	UserID       string
	FirstName    string
	LastName     string
	BrokerID     string
	Broker       string
	BrokerCreds  string
	StaticIP     string
}

type ParsedCredential struct {
	models.Credential
	BrokerCredsMap map[string]interface{}
}

func ParseCSV(filePath string) ([]ParsedCredential, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return nil, fmt.Errorf("open csv: %w", err)
	}
	defer file.Close()

	reader := csv.NewReader(file)
	reader.FieldsPerRecord = -1 // Allow variable fields

	// Read header
	header, err := reader.Read()
	if err != nil {
		return nil, fmt.Errorf("read header: %w", err)
	}

	// Find column indices
	colIdx := make(map[string]int)
	for i, h := range header {
		colIdx[strings.TrimSpace(h)] = i
	}

	requiredCols := []string{"id", "user_id", "broker", "broker_creds", "static_ip"}
	for _, c := range requiredCols {
		if _, ok := colIdx[c]; !ok {
			return nil, fmt.Errorf("missing required column: %s", c)
		}
	}

	var credentials []ParsedCredential

	for {
		record, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("read record: %w", err)
		}

		if len(record) <= colIdx["broker_creds"] {
			continue
		}

		credsJSON := strings.TrimSpace(record[colIdx["broker_creds"]])
		if credsJSON == "" {
			continue
		}

		var credsMap map[string]interface{}
		if err := json.Unmarshal([]byte(credsJSON), &credsMap); err != nil {
			return nil, fmt.Errorf("parse broker_creds json for user_id %s: %w", record[colIdx["user_id"]], err)
		}

		userID, _ := strconv.Atoi(record[colIdx["user_id"]])

		cred := ParsedCredential{
			Credential: models.Credential{
				UserID:    userID,
				Broker:    strings.TrimSpace(record[colIdx["broker"]]),
				StaticIP:  strings.TrimSpace(record[colIdx["static_ip"]]),
				Status:    "pending",
				CreatedAt: time.Now(),
			},
			BrokerCredsMap: credsMap,
		}

		// Extract fields from broker_creds JSON
		extractCredentialFields(&cred, credsMap)

		// Compute fingerprint from broker + client_id
		if cred.ClientID != "" {
			cred.Fingerprint = computeFingerprint(cred.Broker, cred.ClientID)
		}

		credentials = append(credentials, cred)
	}

	return credentials, nil
}

func extractCredentialFields(cred *ParsedCredential, m map[string]interface{}) {
	getString := func(key string) string {
		if v, ok := m[key]; ok {
			if s, ok := v.(string); ok {
				return strings.TrimSpace(s)
			}
		}
		return ""
	}

	// Common fields for all brokers
	cred.ClientID = getString("client_id")
	cred.Password = getString("password")
	cred.AccessToken = getString("access_token")
	cred.AccessTokenCreatedAt = parseTime(getString("access_token_created_at"))

	// Strict per-broker mapping to prevent key collisions
	switch cred.Broker {
	case "MSTOCK":
		cred.TOTPSecret = getString("totp_secret")
		cred.APIKeyEncrypted = getString("api_key")
		cred.APIKey = getString("api_key")

	case "FLATTRADE":
		cred.TOTPSecret = getString("totp_secret_key")

	case "ALICE BLUE":
		cred.TOTPSecret = getString("totp")
		cred.AppCode = getString("app_code")
		cred.APISecret = getString("api_secret")
		
		// Some ALICE BLUE rows might use api_key instead of api_secret
		if cred.APISecret == "" {
			cred.APISecret = getString("api_key")
		}
	}
}

func parseTime(s string) *time.Time {
	if s == "" {
		return nil
	}
	layouts := []string{
		"2006-01-02 15:04:05",
		"2006-01-02T15:04:05Z",
		time.RFC3339,
	}
	for _, layout := range layouts {
		if t, err := time.Parse(layout, s); err == nil {
			return &t
		}
	}
	return nil
}

func computeFingerprint(broker, clientID string) string {
	// Simple deterministic fingerprint: broker:client_id
	return broker + ":" + clientID
}