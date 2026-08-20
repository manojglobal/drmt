package position

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"time"

	"github.com/jmoiron/sqlx"

	"drmt/internal/config"
	"drmt/internal/credentials"
	"drmt/internal/models"
)

type AliceBlueAdapter struct {
	db       *sqlx.DB
	client   *http.Client
	interval time.Duration
	proxyURL string
}

func NewAliceBlueAdapter(db *sqlx.DB, proxyURL string) *AliceBlueAdapter {
	client := &http.Client{Timeout: 10 * time.Second}

	return &AliceBlueAdapter{
		db:       db,
		client:   client,
		interval: 3 * time.Second, // Poll every 3 seconds
		proxyURL: proxyURL,
	}
}

func (a *AliceBlueAdapter) Start(ctx context.Context, feed chan<- models.Position) {
	log.Println("Starting AliceBlue polling adapter...")

	ticker := time.NewTicker(a.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Println("AliceBlue adapter stopping...")
			return
		case <-ticker.C:
			a.poll(feed)
		}
	}
}

func (a *AliceBlueAdapter) poll(feed chan<- models.Position) {
	var creds []models.Credential

	err := a.db.Select(&creds, "SELECT * FROM credentials WHERE broker = ? AND status = 'active'", config.BrokerAliceBlue)
	if err != nil {
		log.Printf("AliceBlue Adapter DB error: %v", err)
		return
	}

	for _, cred := range creds {
		if cred.AccessToken == "" {
			continue
		}

		go a.fetchPositionsForCred(cred, feed)
	}
}

func (a *AliceBlueAdapter) fetchPositionsForCred(cred models.Credential, feed chan<- models.Position) {
	url := config.AliceBlueBaseURL + "/open-api/od/v1/positions"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return
	}

	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0")

	resp, err := a.client.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		log.Printf("AliceBlue auth failed (HTTP %d) for %s, triggering token refresh...", resp.StatusCode, cred.ClientID)
		go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
		return
	} else if resp.StatusCode != http.StatusOK {
		return
	}

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return
	}

	if result["status"] != "Ok" {
		log.Printf("AliceBlue API failed for %s: %s, triggering refresh...", cred.ClientID, string(body))
		go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
		return
	}

	dataObj, ok := result["result"].([]interface{})
	if !ok {
		return
	}

	for _, item := range dataObj {
		pMap, ok := item.(map[string]interface{})
		if !ok {
			continue
		}

		netQtyStr := fmt.Sprintf("%v", pMap["netQuantity"])
		var netQty int
		fmt.Sscanf(netQtyStr, "%d", &netQty)

		var txType string
		if netQty > 0 {
			txType = "BUY"
		} else if netQty < 0 {
			txType = "SELL"
		} else {
			txType = "NONE"
		}

		symbol := fmt.Sprintf("%v", pMap["tradingSymbol"])

		avgPriceStr := fmt.Sprintf("%v", pMap["netAveragePrice"])
		var avgPrice float64
		fmt.Sscanf(avgPriceStr, "%f", &avgPrice)
		
		pnlStr := fmt.Sprintf("%v", pMap["realizedPnl"])
		var pnl float64
		fmt.Sscanf(pnlStr, "%f", &pnl)

		pos := models.Position{
			Broker:          config.BrokerAliceBlue,
			ClientID:        cred.ClientID,
			UserID:          cred.UserID,
			Symbol:          symbol,
			Exchange:        fmt.Sprintf("%v", pMap["exchange"]),
			ProductType:     fmt.Sprintf("%v", pMap["product"]),
			Quantity:        netQty,
			AvgPrice:        avgPrice,
			PnL:             pnl,
			TransactionType: txType,
		}

		feed <- pos
	}
}
