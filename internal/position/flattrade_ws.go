package position

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"github.com/gorilla/websocket"
	"github.com/jmoiron/sqlx"

	"drmt/internal/config"
	"drmt/internal/credentials"
	"drmt/internal/models"
)

type FlattradeWSAdapter struct {
	db       *sqlx.DB
	proxyURL string
}

func NewFlattradeWSAdapter(db *sqlx.DB, proxyURL string) *FlattradeWSAdapter {
	return &FlattradeWSAdapter{
		db:       db,
		proxyURL: proxyURL,
	}
}

func (a *FlattradeWSAdapter) Start(ctx context.Context, feed chan<- models.Position) {
	log.Println("Starting Flattrade WebSocket adapter...")

	var creds []models.Credential
	err := a.db.Select(&creds, "SELECT * FROM credentials WHERE broker = ? AND status = 'active'", config.BrokerFlattrade)
	if err != nil {
		log.Printf("Flattrade WS Adapter DB error: %v", err)
		return
	}

	for _, cred := range creds {
		if cred.AccessToken == "" {
			continue
		}
		go a.connectAndListen(ctx, cred, feed)
	}
}

func (a *FlattradeWSAdapter) connectAndListen(ctx context.Context, cred models.Credential, feed chan<- models.Position) {
	url := "wss://piconnect.flattrade.in/PiConnectWSAPI/"
	
	// Reconnection loop
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		err := a.listen(ctx, url, cred, feed)
		if err != nil {
			log.Printf("Flattrade WS disconnected for %s: %v. Reconnecting in 5s...", cred.ClientID, err)
			
			// If it's an auth failure or policy violation, try refreshing the token
			if err.Error() != "" {
				log.Printf("Flattrade auth/connection failed for %s, triggering token refresh...", cred.ClientID)
				refreshErr := credentials.RefreshAccessToken(a.db, &cred, a.proxyURL)
				if refreshErr != nil {
					log.Printf("Flattrade token refresh failed for %s: %v. Stopping WS loop.", cred.ClientID, refreshErr)
					return // Exit goroutine, don't keep reconnecting
				}
				log.Printf("Flattrade token successfully refreshed for %s, resuming WS loop.", cred.ClientID)
			}
			
			time.Sleep(5 * time.Second)
		}
	}
}

func (a *FlattradeWSAdapter) listen(ctx context.Context, url string, cred models.Credential, feed chan<- models.Position) error {
	dialer := websocket.DefaultDialer
	conn, _, err := dialer.Dial(url, nil)
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}
	defer conn.Close()

	// 1. Initial Connection / Auth
	connectMsg := map[string]interface{}{
		"t":           "a",
		"uid":         cred.ClientID,
		"actid":       cred.ClientID,
		"source":      "API",
		"accesstoken": cred.AccessToken,
	}
	if err := conn.WriteJSON(connectMsg); err != nil {
		return fmt.Errorf("write auth: %w", err)
	}

	// 2. Read loop
	errChan := make(chan error, 1)
	go func() {
		for {
			_, message, err := conn.ReadMessage()
			if err != nil {
				errChan <- err
				return
			}

			var msg map[string]interface{}
			if err := json.Unmarshal(message, &msg); err != nil {
				continue
			}

			msgType := fmt.Sprintf("%v", msg["t"])

			// Check auth ack
			if msgType == "ak" {
				if fmt.Sprintf("%v", msg["s"]) == "Ok" {
					log.Printf("Flattrade WS Authenticated for %s", cred.ClientID)
					
					// Subscribe to positions now
					subMsg := map[string]interface{}{
						"t":     "p",
						"actid": cred.ClientID,
					}
					conn.WriteJSON(subMsg)
				} else {
					errChan <- fmt.Errorf("auth failed: %v", msg)
					return
				}
				continue
			}

			// Heartbeat or ack
			if msgType == "tk" || msgType == "pk" || msgType == "ck" {
				continue
			}

			// Position Update
			if msgType == "pm" {
				a.processPositionMessage(cred, msg, feed)
			}
		}
	}()

	select {
	case <-ctx.Done():
		return nil
	case err := <-errChan:
		return err
	}
}

func (a *FlattradeWSAdapter) processPositionMessage(cred models.Credential, msg map[string]interface{}, feed chan<- models.Position) {
	netQtyStr := fmt.Sprintf("%v", msg["netqty"])
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

	symbol := fmt.Sprintf("%v", msg["instname"])
	if symbol == "" || symbol == "<nil>" {
		symbol = fmt.Sprintf("%v", msg["tsym"])
	}

	avgPriceStr := fmt.Sprintf("%v", msg["netavgprc"])
	if avgPriceStr == "" || avgPriceStr == "<nil>" {
		avgPriceStr = fmt.Sprintf("%v", msg["buyavgprc"]) // fallback
	}
	var avgPrice float64
	fmt.Sscanf(avgPriceStr, "%f", &avgPrice)
	
	pnlStr := fmt.Sprintf("%v", msg["rpnl"])
	var pnl float64
	fmt.Sscanf(pnlStr, "%f", &pnl)

	pos := models.Position{
		Broker:          config.BrokerFlattrade,
		ClientID:        cred.ClientID,
		UserID:          cred.UserID,
		Symbol:          symbol,
		Exchange:        fmt.Sprintf("%v", msg["exch"]),
		ProductType:     fmt.Sprintf("%v", msg["prd"]),
		Quantity:        netQty,
		AvgPrice:        avgPrice,
		PnL:             pnl,
		TransactionType: txType,
	}

	feed <- pos
}
