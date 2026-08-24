package credentials

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"crypto/md5"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"drmt/internal/config"
	"drmt/internal/models"
)

type AliceBlueGenerator struct {
	client        *http.Client
	commonHeaders map[string]string
}

func NewAliceBlueGenerator(proxyURL string) *AliceBlueGenerator {
	return &AliceBlueGenerator{
		client: &http.Client{Timeout: config.HTTPTimeoutSeconds * time.Second},
		commonHeaders: map[string]string{
			"Content-Type": "application/json",
			"User-Agent":   "Mozilla/5.0",
		},
	}
}

func (g *AliceBlueGenerator) BrokerName() string {
	return config.BrokerAliceBlue
}

func (g *AliceBlueGenerator) GenerateToken(cred *models.Credential) (string, error) {
	clientID := cred.ClientID
	password := cred.Password
	totpSecret := cred.TOTPSecret
	appCode := cred.AppCode
	apiSecret := cred.APISecret

	// Step 1: Verify User
	verifyUserURL := config.AliceBlueBaseURL + config.AliceBlueVerifyUser
	resp, err := g.postJSON(verifyUserURL, map[string]string{"userId": clientID})
	if err != nil {
		return "", fmt.Errorf("verify user: %w", err)
	}
	if !g.isStatusOk(resp) {
		return "", fmt.Errorf("verify user failed: %s", g.getMsg(resp))
	}

	// Step 2: Get Enc Key
	encKeyURL := config.AliceBlueBaseURL + config.AliceBlueGetEncKey
	resp, err = g.postJSON(encKeyURL, map[string]string{"userId": clientID})
	if err != nil {
		return "", fmt.Errorf("get enc key: %w", err)
	}
	if !g.isStatusOk(resp) {
		return "", fmt.Errorf("enc key failed: %s", g.getMsg(resp))
	}
	encKey := g.extractEncKey(resp)

	// Step 3: Validate Password
	encryptedPwd := encryptPasswordAlice(password, encKey)
	validateURL := config.AliceBlueBaseURL + config.AliceBlueValidatePwd
	resp, err = g.postJSON(validateURL, map[string]string{
		"userId":   clientID,
		"userData": encryptedPwd,
		"source":   "WEB",
	})
	if err != nil {
		return "", fmt.Errorf("validate password: %w", err)
	}
	if !g.isStatusOk(resp) {
		return "", fmt.Errorf("password validation failed: %s", g.getMsg(resp))
	}
	tempToken := g.extractTempToken(resp)

	// Step 4: Verify TOTP
	currentTOTP := GenerateTOTP(totpSecret)
	deviceID := MD5Hash(clientID)

	totpURL := config.AliceBlueBaseURL + config.AliceBlueVerifyTOTP
	resp, err = g.postJSONWithAuth(totpURL, map[string]string{
		"userId":       clientID,
		"totp":         currentTOTP,
		"source":       "WEB",
		"deviceId":     deviceID,
		"deviceNumber": deviceID,
	}, fmt.Sprintf("Bearer %s WEB %s", clientID, tempToken))
	if err != nil {
		return "", fmt.Errorf("verify totp: %w", err)
	}
	if !g.isStatusOk(resp) {
		return "", fmt.Errorf("totp failed: %s", g.getMsg(resp))
	}
	finalAccessToken := g.extractFinalAccessToken(resp)

	// Step 5: SSO Check
	ssoURL := config.AliceBlueBaseURL + config.AliceBlueSSOAuthorize
	resp, err = g.postJSONWithAuth(ssoURL, map[string]string{
		"userId": clientID,
		"vendor": appCode,
	}, "Bearer "+finalAccessToken)
	if err != nil {
		return "", fmt.Errorf("sso check: %w", err)
	}
	if !g.isStatusOk(resp) {
		return "", fmt.Errorf("sso failed: %s", g.getMsg(resp))
	}
	redirectURL := g.extractRedirectURL(resp)
	authCode := g.extractAuthCode(redirectURL)

	// Step 6: Final User Session
	checksum := SHA256Hash(clientID + authCode + apiSecret)
	finalURL := config.AliceBlueBaseURL + config.AliceBlueGetUserDetails
	resp, err = g.postJSON(finalURL, map[string]string{"checkSum": checksum})
	if err != nil {
		return "", fmt.Errorf("final user session: %w", err)
	}
	if !g.isStatOk(resp) {
		return "", fmt.Errorf("final session failed: %s", g.getMsg(resp))
	}
	userSession := g.extractUserSession(resp)

	return userSession, nil
}

func (g *AliceBlueGenerator) postJSON(urlStr string, payload map[string]string) (map[string]interface{}, error) {
	return g.doRequest("POST", urlStr, payload, "")
}

func (g *AliceBlueGenerator) postJSONWithAuth(urlStr string, payload map[string]string, authHeader string) (map[string]interface{}, error) {
	return g.doRequest("POST", urlStr, payload, authHeader)
}

func (g *AliceBlueGenerator) doRequest(method, urlStr string, payload map[string]string, authHeader string) (map[string]interface{}, error) {
	jsonData, _ := json.Marshal(payload)
	req, err := http.NewRequest(method, urlStr, bytes.NewReader(jsonData))
	if err != nil {
		return nil, err
	}

	for k, v := range g.commonHeaders {
		req.Header.Set(k, v)
	}
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}

	resp, err := g.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (g *AliceBlueGenerator) isStatusOk(resp map[string]interface{}) bool {
	if status, ok := resp["status"].(string); ok {
		return status == "Ok"
	}
	if stat, ok := resp["stat"].(string); ok {
		return stat == "Ok"
	}
	return false
}

func (g *AliceBlueGenerator) isStatOk(resp map[string]interface{}) bool {
	if stat, ok := resp["stat"].(string); ok {
		return stat == "Ok"
	}
	return false
}

func (g *AliceBlueGenerator) getMsg(resp map[string]interface{}) string {
	if msg, ok := resp["emsg"].(string); ok {
		return msg
	}
	if msg, ok := resp["message"].(string); ok {
		return msg
	}
	return "unknown error"
}

func (g *AliceBlueGenerator) extractEncKey(resp map[string]interface{}) string {
	if result, ok := resp["result"].([]interface{}); ok && len(result) > 0 {
		if m, ok := result[0].(map[string]interface{}); ok {
			if encKey, ok := m["encKey"].(string); ok {
				return encKey
			}
		}
	}
	return ""
}

func (g *AliceBlueGenerator) extractTempToken(resp map[string]interface{}) string {
	if result, ok := resp["result"].([]interface{}); ok && len(result) > 0 {
		if m, ok := result[0].(map[string]interface{}); ok {
			if token, ok := m["token"].(string); ok {
				return token
			}
		}
	}
	return ""
}

func (g *AliceBlueGenerator) extractFinalAccessToken(resp map[string]interface{}) string {
	if result, ok := resp["result"].([]interface{}); ok && len(result) > 0 {
		if m, ok := result[0].(map[string]interface{}); ok {
			if token, ok := m["accessToken"].(string); ok {
				return token
			}
		}
	}
	return ""
}

func (g *AliceBlueGenerator) extractRedirectURL(resp map[string]interface{}) string {
	if result, ok := resp["result"].([]interface{}); ok && len(result) > 0 {
		if m, ok := result[0].(map[string]interface{}); ok {
			if url, ok := m["redirectUrl"].(string); ok {
				return url
			}
		}
	}
	return ""
}

func (g *AliceBlueGenerator) extractAuthCode(redirectURL string) string {
	parsed, err := url.Parse(redirectURL)
	if err != nil {
		return ""
	}
	authCode := parsed.Query().Get("authCode")
	if authCode == "" {
		authCode = parsed.Query().Get("auth_code")
	}
	return authCode
}

func (g *AliceBlueGenerator) extractUserSession(resp map[string]interface{}) string {
	if session, ok := resp["userSession"].(string); ok {
		return session
	}
	return ""
}

func encryptPasswordAlice(password, encKey string) string {
	salt := make([]byte, 8)
	if _, err := io.ReadFull(rand.Reader, salt); err != nil {
		return ""
	}
	key, iv := getCryptoJSKeyIV(encKey, salt)
	block, err := aes.NewCipher(key)
	if err != nil {
		return ""
	}
	padded := pkcs7Pad([]byte(password), aes.BlockSize)
	encrypted := make([]byte, len(padded))
	mode := cipher.NewCBCEncrypter(block, iv)
	mode.CryptBlocks(encrypted, padded)
	final := append([]byte("Salted__"), append(salt, encrypted...)...)
	return base64.StdEncoding.EncodeToString(final)
}

func getCryptoJSKeyIV(passphrase string, salt []byte) ([]byte, []byte) {
	passwordBytes := []byte(passphrase)
	data := []byte{}
	last := []byte{}
	for len(data) < 48 {
		h := md5.New()
		h.Write(last)
		h.Write(passwordBytes)
		h.Write(salt)
		last = h.Sum(nil)
		data = append(data, last...)
	}
	return data[:32], data[32:48]
}

func pkcs7Pad(data []byte, blockSize int) []byte {
	padding := blockSize - len(data)%blockSize
	padText := bytes.Repeat([]byte{byte(padding)}, padding)
	return append(data, padText...)
}
func (g *AliceBlueGenerator) ValidateToken(cred *models.Credential) bool {
	// Simple HTTP hit to verify
	url := config.AliceBlueBaseURL + "/open-api/od/v1/positions"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return false
	}
	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0")

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
	
	return result["status"] == "Ok"
}
