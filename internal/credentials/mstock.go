package credentials

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"drmt/internal/config"
	"drmt/internal/models"
)

type MStockGenerator struct {
	client *http.Client
}

func NewMStockGenerator(proxyURL string) *MStockGenerator {
	return &MStockGenerator{
		client: NewHTTPClientWithProxy(proxyURL),
	}
}

func (g *MStockGenerator) BrokerName() string {
	return config.BrokerMStock
}

func (g *MStockGenerator) GenerateToken(cred *models.Credential) (string, error) {
	clientCode := cred.ClientID
	password := cred.Password
	apiKey := cred.APIKey
	totpSecret := strings.ReplaceAll(strings.TrimSpace(cred.TOTPSecret), " ", "")

	// -----------------------------------------
	// 1. Login
	// -----------------------------------------
	loginURL := "https://api.mstock.trade/openapi/typeb/connect/login"
	loginPayload := map[string]string{
		"clientcode": clientCode,
		"password":   password,
		"totp":       "",
		"state":      "",
	}

	loginReq, err := http.NewRequest("POST", loginURL, strings.NewReader(jsonEncode(loginPayload)))
	if err != nil {
		return "", fmt.Errorf("create login request: %w", err)
	}

	loginReq.Header.Set("X-Mirae-Version", "1")
	loginReq.Header.Set("Content-Type", "application/json")
	loginReq.Header.Set("User-Agent", "Mozilla/5.0")
	if cred.StaticIP != "" {
		loginReq.Header.Set("X-Static-IP", cred.StaticIP)
	}

	loginResp, err := g.client.Do(loginReq)
	if err != nil {
		return "", fmt.Errorf("login request failed: %w", err)
	}
	defer loginResp.Body.Close()

	loginBody, _ := io.ReadAll(loginResp.Body)

	var loginResult map[string]interface{}
	if err := json.Unmarshal(loginBody, &loginResult); err != nil {
		return "", fmt.Errorf("unmarshal login response: %w", err)
	}

	// Status can be string "true" or boolean true depending on MStock API quirks, handle safely
	statusObj := loginResult["status"]
	statusStr := fmt.Sprintf("%v", statusObj)
	if statusStr != "true" {
		return "", fmt.Errorf("login failed: %v", loginResult["message"])
	}

	data, ok := loginResult["data"].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("invalid login response structure: no data object")
	}

	// Based on Python implementation, we extract refreshToken
	refreshToken, _ := data["refreshToken"].(string)
	if refreshToken == "" {
		// Fallback just in case they put it in jwtToken
		refreshToken, _ = data["jwtToken"].(string)
	}
	if refreshToken == "" {
		return "", fmt.Errorf("no refreshToken found in login response")
	}

	// -----------------------------------------
	// 2. Verify TOTP
	// -----------------------------------------
	currentTOTP := GenerateTOTP(totpSecret)

	verifyURL := "https://api.mstock.trade/openapi/typeb/session/verifytotp"
	verifyPayload := map[string]string{
		"refreshToken": refreshToken,
		"totp":         currentTOTP,
	}

	verifyReq, err := http.NewRequest("POST", verifyURL, strings.NewReader(jsonEncode(verifyPayload)))
	if err != nil {
		return "", fmt.Errorf("create verify request: %w", err)
	}

	verifyReq.Header.Set("X-Mirae-Version", "1")
	verifyReq.Header.Set("X-PrivateKey", apiKey)
	verifyReq.Header.Set("Content-Type", "application/json")
	verifyReq.Header.Set("User-Agent", "Mozilla/5.0")
	if cred.StaticIP != "" {
		verifyReq.Header.Set("X-Static-IP", cred.StaticIP)
	}

	verifyResp, err := g.client.Do(verifyReq)
	if err != nil {
		return "", fmt.Errorf("verify request failed: %w", err)
	}
	defer verifyResp.Body.Close()

	verifyBody, _ := io.ReadAll(verifyResp.Body)

	var verifyResult map[string]interface{}
	if err := json.Unmarshal(verifyBody, &verifyResult); err != nil {
		return "", fmt.Errorf("unmarshal verify response: %w", err)
	}

	vStatusObj := verifyResult["status"]
	vStatusStr := fmt.Sprintf("%v", vStatusObj)
	if vStatusStr != "true" {
		return "", fmt.Errorf("verify failed: %v", verifyResult["message"])
	}

	vData, ok := verifyResult["data"].(map[string]interface{})
	if !ok {
		return "", fmt.Errorf("invalid verify response structure: no data object")
	}

	accessToken, _ := vData["jwtToken"].(string)
	if accessToken == "" {
		return "", fmt.Errorf("no jwtToken in verify response")
	}

	return accessToken, nil
}

func (g *MStockGenerator) ValidateToken(cred *models.Credential) bool {
	// Call a lightweight endpoint to check if the token works
	url := "https://api.mstock.trade/openapi/typeb/portfolio/positions"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return false
	}
	req.Header.Set("X-Mirae-Version", "1")
	req.Header.Set("X-PrivateKey", cred.APIKey)
	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	if cred.StaticIP != "" {
		req.Header.Set("X-Static-IP", cred.StaticIP)
	}

	resp, err := g.client.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		return false
	}

	body, _ := io.ReadAll(resp.Body)
	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return false
	}

	statusStr := fmt.Sprintf("%v", result["status"])
	return statusStr == "true"
}
