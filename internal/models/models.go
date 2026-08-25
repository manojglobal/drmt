package models

import "time"

type User struct {
	ID           int       `db:"id" json:"id"`
	Username     string    `db:"username" json:"username"`
	PasswordHash string    `db:"password_hash" json:"-"`
	CreatedAt    time.Time `db:"created_at" json:"created_at"`
}

type Session struct {
	Token     string    `db:"token" json:"token"`
	UserID    int       `db:"user_id" json:"user_id"`
	ExpiresAt time.Time `db:"expires_at" json:"expires_at"`
}

type Credential struct {
	ID                     int        `db:"id" json:"id"`
	Broker                 string     `db:"broker" json:"broker"`
	APIKey                 string     `db:"api_key" json:"api_key"`
	APISecret              string     `db:"api_secret" json:"-"`
	Fingerprint            string     `db:"fingerprint" json:"fingerprint"`
	ClientID               string     `db:"client_id" json:"client_id"`
	Password               string     `db:"password" json:"-"`
	TOTPSecret             string     `db:"totp_secret" json:"-"`
	APIKeyEncrypted        string     `db:"api_key_encrypted" json:"-"`
	AppCode                string     `db:"app_code" json:"-"`
	APISecretAlice         string     `db:"api_secret" json:"-"` // For Alice Blue
	AccessToken            string     `db:"access_token" json:"-"`
	AccessTokenCreatedAt   *time.Time `db:"access_token_created_at" json:"access_token_created_at,omitempty"`
	UserID                 int        `db:"user_id" json:"user_id"`
	StaticIP               string     `db:"static_ip" json:"static_ip"`
	Status                 string     `db:"status" json:"status"`
	LastError              string     `db:"last_error" json:"-"`
	RetryCount             int        `db:"retry_count" json:"retry_count"`
	CreatedAt              time.Time  `db:"created_at" json:"created_at"`
}

type Position struct {
	ID              string    `json:"id"`
	Broker          string    `json:"broker"`
	ClientID        string    `json:"client_id"`
	UserID          int       `json:"user_id"`
	Username        string    `json:"username"`
	Symbol          string    `json:"symbol"`
	Token           string    `json:"token"`
	Exchange        string    `json:"exchange"`
	ProductType     string    `json:"product_type"`
	OrderType       string    `json:"order_type"`
	Quantity        int       `json:"quantity"`
	AvgPrice        float64   `json:"avg_price"`
	LTP             float64   `json:"ltp"`
	PnL             float64   `json:"pnl"`
	TransactionType string    `json:"transaction"` // BUY, SELL, NONE
	UpdatedAt       time.Time `json:"updated_at"`
}

type ExitHistory struct {
	ID        int       `db:"id" json:"id"`
	Broker    string    `db:"broker" json:"broker"`
	Symbol    string    `db:"symbol" json:"symbol"`
	Status    string    `db:"status" json:"status"`
	Timestamp time.Time `db:"timestamp" json:"timestamp"`
}

type SystemLog struct {
	ID        int       `db:"id" json:"id"`
	Level     string    `db:"level" json:"level"`
	Message   string    `db:"message" json:"message"`
	Timestamp time.Time `db:"timestamp" json:"timestamp"`
}

type ExitedPosition struct {
	ID              int       `db:"id" json:"id"`
	PositionID      string    `db:"position_id" json:"position_id"`
	Broker          string    `db:"broker" json:"broker"`
	ClientID        string    `db:"client_id" json:"client_id"`
	Symbol          string    `db:"symbol" json:"symbol"`
	Token           string    `db:"token" json:"token"`
	Exchange        string    `db:"exchange" json:"exchange"`
	ProductType     string    `db:"product_type" json:"product_type"`
	OrderType       string    `db:"order_type" json:"order_type"`
	TransactionType string    `db:"transaction_type" json:"transaction"`
	Quantity        int       `db:"quantity" json:"quantity"`
	AvgPrice        float64   `db:"avg_price" json:"avg_price"`
	ExitPrice       float64   `db:"exit_price" json:"exit_price"`
	PnL             float64   `db:"pnl" json:"pnl"`
	ExitedAt        time.Time `db:"exited_at" json:"exited_at"`
}

