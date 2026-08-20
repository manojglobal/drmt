package credentials

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"drmt/internal/config"
)

type DropboxClient struct {
	httpClient   *http.Client
	appKey       string
	appSecret    string
	refreshToken string
	accessToken  string
	tokenExpiry  time.Time
}

type tokenResponse struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	ExpiresIn   int    `json:"expires_in"`
}

func NewDropboxClient(cfg *config.Config) *DropboxClient {
	return &DropboxClient{
		httpClient:   &http.Client{Timeout: 30 * time.Second},
		appKey:       cfg.DropboxAppKey,
		appSecret:    cfg.DropboxAppSecret,
		refreshToken: cfg.DropboxRefreshToken,
	}
}

func (c *DropboxClient) ensureValidToken() error {
	if c.accessToken != "" && time.Now().Before(c.tokenExpiry.Add(-30*time.Second)) {
		return nil
	}

	data := url.Values{}
	data.Set("grant_type", "refresh_token")
	data.Set("refresh_token", c.refreshToken)
	data.Set("client_id", c.appKey)
	data.Set("client_secret", c.appSecret)

	req, err := http.NewRequest("POST", "https://api.dropbox.com/oauth2/token", strings.NewReader(data.Encode()))
	if err != nil {
		return fmt.Errorf("create token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("token request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("token refresh failed: %s - %s", resp.Status, string(body))
	}

	var tokenResp tokenResponse
	if err := json.NewDecoder(resp.Body).Decode(&tokenResp); err != nil {
		return fmt.Errorf("decode token response: %w", err)
	}

	c.accessToken = tokenResp.AccessToken
	c.tokenExpiry = time.Now().Add(time.Duration(tokenResp.ExpiresIn) * time.Second)
	return nil
}

func (c *DropboxClient) DownloadCredentials(localDir string) (string, error) {
	if err := c.ensureValidToken(); err != nil {
		return "", fmt.Errorf("ensure valid token: %w", err)
	}

	localPath := filepath.Join(localDir, config.CredentialsFileName)
	if err := os.MkdirAll(localDir, 0755); err != nil {
		return "", fmt.Errorf("create local dir: %w", err)
	}

	req, err := http.NewRequest("POST", "https://content.dropboxapi.com/2/files/download", nil)
	if err != nil {
		return "", fmt.Errorf("create download request: %w", err)
	}

	req.Header.Set("Authorization", "Bearer "+c.accessToken)
	req.Header.Set("Dropbox-API-Arg", fmt.Sprintf(`{"path": "%s"}`, config.DropboxFilePath))

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("download request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("download failed: %s - %s", resp.Status, string(body))
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read response body: %w", err)
	}

	if err := os.WriteFile(localPath, body, 0644); err != nil {
		return "", fmt.Errorf("write local file: %w", err)
	}

	return localPath, nil
}