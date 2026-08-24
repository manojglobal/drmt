package credentials

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"

	"drmt/internal/config"
	"drmt/internal/models"
	"github.com/google/uuid"
)

type FlattradeGenerator struct {
	client *http.Client
}

func NewFlattradeGenerator(proxyURL string) *FlattradeGenerator {
	return &FlattradeGenerator{
		client: NewHTTPClientWithProxy(proxyURL),
	}
}

func (g *FlattradeGenerator) BrokerName() string {
	return config.BrokerFlattrade
}

func (g *FlattradeGenerator) GenerateToken(cred *models.Credential) (string, error) {
	userID := cred.ClientID
	password := cred.Password
	totpSecret := cred.TOTPSecret

	payload := map[string]string{
		"uid":           userID,
		"pwd":           SHA256Hash(password),
		"factor2":       GenerateTOTP(totpSecret),
		"source":        config.FlattradeSource,
		"addldivinf":    config.FlattradeAddlDivInfo,
		"app_inst_id":   uuid.New().String(),
		"ipaddr":        "127.0.0.1",
	}

	req, err := http.NewRequest("POST", config.FlattradeLoginURL, strings.NewReader(jsonEncode(payload)))
	if err != nil {
		return "", fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("User-Agent", config.FlattradeUserAgent)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", config.FlattradeOrigin)
	req.Header.Set("Referer", config.FlattradeReferer)
	req.Header.Set("device", config.FlattradeDevice)
	req.Header.Set("source", config.FlattradeSource)

	resp, err := g.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("request failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read body: %w", err)
	}

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return "", fmt.Errorf("unmarshal: %w", err)
	}

	quickauth, ok := result["quickauthresp"].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("invalid response structure")
	}

	if stat, ok := quickauth["stat"].(string); !ok || stat != "Ok" {
		emsg := ""
		if e, ok := quickauth["emsg"].(string); ok {
			emsg = e
		}
		return "", fmt.Errorf("login failed: %s", emsg)
	}

	accessToken, ok := quickauth["susertoken"].(string)
	if !ok || accessToken == "" {
		return "", fmt.Errorf("no access token in response")
	}

	return accessToken, nil
}

func jsonEncode(v interface{}) string {
	b, _ := json.Marshal(v)
	return string(b)
}
func (g *FlattradeGenerator) ValidateToken(cred *models.Credential) bool {
	url := "https://emergeapi.flattrade.in/getPositonBook"
	
	payloadMap := map[string]string{
		"uid":   cred.ClientID,
		"actid": cred.ClientID,
		"jkey":  cred.AccessToken,
	}
	payloadBytes, _ := json.Marshal(payloadMap)

	req, err := http.NewRequest("POST", url, strings.NewReader(string(payloadBytes)))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0")
	req.Header.Set("source", "WEB")
	req.Header.Set("device", "Chrome")
	req.Header.Set("Origin", "https://emerge.flattrade.in")
	req.Header.Set("Referer", "https://emerge.flattrade.in/")

	resp, err := g.client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return false
	}

	body, _ := io.ReadAll(resp.Body)
	log.Printf("Flattrade ValidateToken for %s response: %s", cred.ClientID, string(body))
	
	var result struct {
		Status string `json:"status"`
		ErrMsg string `json:"errMsg"`
	}

	if err := json.Unmarshal(body, &result); err != nil {
		return false
	}

	if result.Status == "S" {
		return true
	}
	
	// Handle Flattrade "No data" correctly without failing
	if strings.Contains(strings.ToLower(result.ErrMsg), "no data") {
		return true
	}

	return false
}
