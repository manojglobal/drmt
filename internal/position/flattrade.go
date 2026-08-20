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
	"drmt/internal/models"
)

type FlattradeAdapter struct {
	db       *sqlx.DB
	client   *http.Client
	interval time.Duration
}

func NewFlattradeAdapter(db *sqlx.DB, proxyURL string) *FlattradeAdapter {
	client := &http.Client{Timeout: 10 * time.Second}

	return &FlattradeAdapter{
		db:       db,
		client:   client,
		interval: 3 * time.Second, // Poll every 3 seconds
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
	url := "https://piconnect.flattrade.in/PiConnectAPI/PositionBook"
	
	jData := fmt.Sprintf(`{"uid":"%s","actid":"%s"}`, cred.ClientID, cred.ClientID)
	payload := fmt.Sprintf("jData=%s&jKey=%s", jData, cred.AccessToken)

	req, err := http.NewRequest("POST", url, strings.NewReader(payload))
	if err != nil {
		return
	}

	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", "Mozilla/5.0")

	resp, err := a.client.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return
	}

	body, _ := io.ReadAll(resp.Body)
	
	// Flattrade returns an array of objects for success, or a single object for failure
	// We'll try to unmarshal as array first
	var result []map[string]interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		// Could be a failure object like {"stat":"Not_Ok", "emsg": ...}
		return
	}

	for _, pMap := range result {
		if pMap["stat"] != "Ok" {
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
