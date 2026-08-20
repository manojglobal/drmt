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

type MStockAdapter struct {
	db       *sqlx.DB
	client   *http.Client
	interval time.Duration
	proxyURL string
}

func NewMStockAdapter(db *sqlx.DB, proxyURL string) *MStockAdapter {
	// Simple HTTP client (you can enhance with proxy later if needed)
	client := &http.Client{Timeout: 10 * time.Second}
	
	return &MStockAdapter{
		db:       db,
		client:   client,
		interval: 3 * time.Second, // Poll every 3 seconds
		proxyURL: proxyURL,
	}
}

func (a *MStockAdapter) Start(ctx context.Context, feed chan<- models.Position) {
	log.Println("Starting MStock polling adapter...")
	
	ticker := time.NewTicker(a.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Println("MStock adapter stopping...")
			return
		case <-ticker.C:
			a.poll(feed)
		}
	}
}

func (a *MStockAdapter) poll(feed chan<- models.Position) {
	var creds []models.Credential
	
	// Fetch all active MStock credentials
	err := a.db.Select(&creds, "SELECT * FROM credentials WHERE broker = ? AND status = 'active'", config.BrokerMStock)
	if err != nil {
		log.Printf("MStock Adapter DB error: %v", err)
		return
	}

	for _, cred := range creds {
		if cred.AccessToken == "" {
			continue
		}
		
		go a.fetchPositionsForCred(cred, feed)
	}
}

func (a *MStockAdapter) fetchPositionsForCred(cred models.Credential, feed chan<- models.Position) {
	url := "https://api.mstock.trade/openapi/typeb/portfolio/positions"
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return
	}

	req.Header.Set("X-Mirae-Version", "1")
	req.Header.Set("X-PrivateKey", cred.APIKey)
	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("User-Agent", "Mozilla/5.0")

	resp, err := a.client.Do(req)
	if err != nil {
		log.Printf("MStock request error: %v", err)
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	
	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		log.Printf("MStock auth failed (HTTP %d) for %s, triggering token refresh...", resp.StatusCode, cred.ClientID)
		// Trigger refresh
		go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
		return
	} else if resp.StatusCode != http.StatusOK {
		log.Printf("MStock non-200 status %d: %s", resp.StatusCode, string(body))
		return
	}

	var result map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		log.Printf("MStock JSON parse error: %v", err)
		return
	}

	statusStr := fmt.Sprintf("%v", result["status"])
	if statusStr != "true" {
		log.Printf("MStock API failed for %s: %s, triggering refresh...", cred.ClientID, string(body))
		go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
		return
	}

	dataObj, ok := result["data"].([]interface{})
	if !ok {
		return
	}

	for _, item := range dataObj {
		pMap, ok := item.(map[string]interface{})
		if !ok {
			continue
		}

		netQtyStr := fmt.Sprintf("%v", pMap["netqty"])
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

		symbol := fmt.Sprintf("%v", pMap["symbolname"])
		if symbol == "" {
			symbol = fmt.Sprintf("%v", pMap["tradingsymbol"])
		}

		avgPriceStr := fmt.Sprintf("%v", pMap["buyavgprice"])
		var avgPrice float64
		fmt.Sscanf(avgPriceStr, "%f", &avgPrice)

		pos := models.Position{
			Broker:          config.BrokerMStock,
			ClientID:        cred.ClientID,
			UserID:          cred.UserID,
			Symbol:          symbol,
			Exchange:        fmt.Sprintf("%v", pMap["exchange"]),
			ProductType:     fmt.Sprintf("%v", pMap["producttype"]),
			Quantity:        netQty,
			AvgPrice:        avgPrice,
			TransactionType: txType,
		}

		feed <- pos
	}
}
