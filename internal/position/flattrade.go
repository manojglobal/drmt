package position

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/jmoiron/sqlx"

	"drmt/internal/config"
	"drmt/internal/credentials"
	"drmt/internal/models"
)

type FlattradeAdapter struct {
	db       *sqlx.DB
	client   *http.Client
	interval time.Duration
	proxyURL string
}

func NewFlattradeAdapter(db *sqlx.DB, proxyURL string) *FlattradeAdapter {
	client := credentials.NewHTTPClientWithProxy(proxyURL)
	client.Timeout = 10 * time.Second

	return &FlattradeAdapter{
		db:       db,
		client:   client,
		interval: 5 * time.Second, // Poll every 5 seconds
		proxyURL: proxyURL,
	}
}

func (a *FlattradeAdapter) Start(ctx context.Context, feed chan<- models.Position) {
	log.Println("Starting Flattrade polling adapter...")

	ticker := time.NewTicker(a.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Println("Flattrade adapter stopping...")
			return
		case <-ticker.C:
			a.poll(feed)
		}
	}
}

func (a *FlattradeAdapter) poll(feed chan<- models.Position) {
	var creds []models.Credential

	err := a.db.Select(&creds, "SELECT * FROM credentials WHERE broker = ? AND status = 'active'", config.BrokerFlattrade)
	if err != nil {
		log.Printf("Flattrade Adapter DB error: %v", err)
		return
	}

	for _, cred := range creds {
		if cred.AccessToken == "" {
			continue
		}

		go a.fetchPositionsForCred(cred, feed)
	}
}

func (a *FlattradeAdapter) fetchPositionsForCred(cred models.Credential, feed chan<- models.Position) {
	url := "https://emergeapi.flattrade.in/getPositonBook"
	
	payloadMap := map[string]string{
		"uid":   cred.ClientID,
		"actid": cred.ClientID,
		"jkey":  cred.AccessToken,
	}
	payloadBytes, _ := json.Marshal(payloadMap)

	req, err := http.NewRequest("POST", url, strings.NewReader(string(payloadBytes)))
	if err != nil {
		log.Printf("[FLATTRADE Debug] Request creation error: %v", err)
		return
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0")
	req.Header.Set("source", "WEB")
	req.Header.Set("device", "Chrome")
	req.Header.Set("Origin", "https://emerge.flattrade.in")
	req.Header.Set("Referer", "https://emerge.flattrade.in/")

	resp, err := a.client.Do(req)
	if err != nil {
		log.Printf("[FLATTRADE Debug] Network error for %s: %v", cred.ClientID, err)
		return
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 401 || resp.StatusCode == 403 {
		log.Printf("Flattrade API auth failed (HTTP %d) for %s, triggering refresh...", resp.StatusCode, cred.ClientID)
		go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
		return
	} else if resp.StatusCode != http.StatusOK {
		log.Printf("[FLATTRADE Debug] Non-200 status for %s: %d, body: %s", cred.ClientID, resp.StatusCode, string(body))
		return
	}
	
	var result struct {
		Status          string                   `json:"status"`
		ErrMsg          string                   `json:"errMsg"`
		PositionBookArr []map[string]interface{} `json:"positionBookArr"`
	}

	if err := json.Unmarshal(body, &result); err != nil {
		log.Printf("[FLATTRADE Debug] Unmarshal error for %s: %v. Body: %s", cred.ClientID, err, string(body))
		
		// Fallback for completely different failure schema (just in case)
		var failResult map[string]interface{}
		if err2 := json.Unmarshal(body, &failResult); err2 == nil {
			if failResult["stat"] == "Not_Ok" {
				log.Printf("Flattrade API failed for %s, triggering refresh...", cred.ClientID)
				go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
			}
		}
		return
	}

	if result.Status != "S" {
		log.Printf("Flattrade WEB API returned error for %s: %s, triggering refresh...", cred.ClientID, result.ErrMsg)
		go credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
		return
	}

	for _, pMap := range result.PositionBookArr {
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

		symbol := fmt.Sprintf("%v", pMap["tsym"])

		avgPriceStr := fmt.Sprintf("%v", pMap["netavgprc"])
		var avgPrice float64
		fmt.Sscanf(avgPriceStr, "%f", &avgPrice)
		
		pnlStr := fmt.Sprintf("%v", pMap["rpnl"])
		var pnl float64
		fmt.Sscanf(pnlStr, "%f", &pnl)

		pos := models.Position{
			Broker:          config.BrokerFlattrade,
			ClientID:        cred.ClientID,
			UserID:          cred.UserID,
			Symbol:          symbol,
			Exchange:        fmt.Sprintf("%v", pMap["exch"]),
			ProductType:     fmt.Sprintf("%v", pMap["prd"]),
			Quantity:        netQty,
			AvgPrice:        avgPrice,
			PnL:             pnl,
			TransactionType: txType,
		}

		feed <- pos
	}
}
