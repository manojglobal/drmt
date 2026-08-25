package execution

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jmoiron/sqlx"

	"drmt/internal/config"
	"drmt/internal/credentials"
	"drmt/internal/models"
)

type Engine struct {
	db           *sqlx.DB
	proxyURL     string
	client       *http.Client // proxy client (for MStock)
	directClient *http.Client // direct client (for Flattrade)
}

func NewEngine(db *sqlx.DB, proxyURL string) *Engine {

	go func() {
		if err := LoadMStockInstruments(); err != nil {
			log.Printf("Warning: failed to load MStock instruments: %v", err)
		}
	}()

	return &Engine{
		db:           db,
		proxyURL:     proxyURL,
		client:       credentials.NewHTTPClientWithProxy(proxyURL),
		directClient: &http.Client{Timeout: config.HTTPTimeoutSeconds * time.Second},
	}
}

func (e *Engine) ExecuteExit(pos models.Position) error {
	if pos.Quantity == 0 {
		return nil
	}

	switch pos.Broker {
	case config.BrokerFlattrade:
		return e.executeFlattrade(pos)
	case config.BrokerMStock:
		return e.executeMStock(pos)
	case config.BrokerAliceBlue:
		return e.executeAliceBlueWeb(pos)
	default:
		return fmt.Errorf("unknown broker: %s", pos.Broker)
	}
}

func (e *Engine) executeFlattrade(pos models.Position) error {
	var creds []models.Credential
	err := e.db.Select(&creds, "SELECT * FROM credentials WHERE client_id = ? AND broker = ?", pos.ClientID, config.BrokerFlattrade)
	if err != nil || len(creds) == 0 {
		return fmt.Errorf("credentials not found for flattrade %s", pos.ClientID)
	}
	cred := creds[0]

	apiURL := "https://emergeapi.flattrade.in/placeOrder"

	tranType := "S"
	if pos.Quantity < 0 {
		tranType = "B"
	}
	absQty := int(math.Abs(float64(pos.Quantity)))
	
	prd := pos.ProductType
	if prd == "" {
		prd = "I"
	}

	orderUUID := uuid.New().String()

	payloadMap := map[string]string{
		"uid":         cred.ClientID,
		"actid":       cred.ClientID,
		"jkey":        cred.AccessToken,
		"app_inst_id": orderUUID,
		"exch":        pos.Exchange,
		"tsym":        pos.Symbol,
		"qty":         fmt.Sprintf("%d", absQty),
		"prc":         "0",
		"prd":         prd,
		"rawPrd":      prd,
		"trantype":    tranType,
		"prctyp":      "MKT",
		"ret":         "DAY",
		"price_val":   "0",
		"amo":         "NO",
		"remarks":     orderUUID,
	}

	payloadBytes, _ := json.Marshal(payloadMap)

	req, err := http.NewRequest("POST", apiURL, bytes.NewBuffer(payloadBytes))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "Mozilla/5.0")
	req.Header.Set("source", "WEB")
	req.Header.Set("device", "Chrome")
	req.Header.Set("Origin", "https://emerge.flattrade.in")
	req.Header.Set("Referer", "https://emerge.flattrade.in/")

	resp, err := e.directClient.Do(req)
	if err != nil {
		return fmt.Errorf("flattrade place order network error: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	
	log.Printf("[FLATTRADE EXIT] Response for %s: %s", cred.ClientID, string(body))
	
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("flattrade returned status %d: %s", resp.StatusCode, string(body))
	}

	var data map[string]interface{}
	json.Unmarshal(body, &data)

	if data["status"] == "S" {
		log.Printf("[FLATTRADE EXIT SUCCESS] Order %s placed for %s", data["norenordno"], pos.Symbol)
		return nil
	}

	errStr := fmt.Sprintf("%v", data["errMsg"])
	if strings.Contains(errStr, "Session Expired") || strings.Contains(errStr, "Invalid Session") {
		go credentials.RefreshAccessToken(e.db, &cred, e.proxyURL)
	}

	return fmt.Errorf("flattrade order rejected: %s", errStr)
}

func (e *Engine) executeMStock(pos models.Position) error {
	var creds []models.Credential
	err := e.db.Select(&creds, "SELECT * FROM credentials WHERE client_id = ? AND broker = ?", pos.ClientID, config.BrokerMStock)
	if err != nil || len(creds) == 0 {
		return fmt.Errorf("credentials not found for mstock %s", pos.ClientID)
	}
	cred := creds[0]

	apiURL := "https://api.mstock.trade/openapi/typeb/orders/regular"

	tranType := "SELL"
	if pos.Quantity < 0 {
		tranType = "BUY"
	}
	absQty := int(math.Abs(float64(pos.Quantity)))

	orderUUID := uuid.New().String()

	inst, ok := GetMStockInstrument(pos.Token)
	if !ok {
		return fmt.Errorf("mstock instrument not found in cache for token %s", pos.Token)
	}

	prd := pos.ProductType
	if prd == "HOLDING" || prd == "CNC" || prd == "DELIVERY" {
		if inst.ExchSeg == "NFO" || inst.ExchSeg == "BFO" || inst.ExchSeg == "NCR" {
			prd = "CARRYFORWARD"
		} else {
			prd = "DELIVERY"
		}
	} else {
		if inst.ExchSeg == "NFO" || inst.ExchSeg == "BFO" || inst.ExchSeg == "NCR" {
			prd = "CARRYFORWARD"
		} else {
			prd = "INTRADAY"
		}
	}

	payload := map[string]interface{}{
		"variety":           "NORMAL",
		"tradingsymbol":     inst.Name,
		"symboltoken":       inst.Token,
		"exchange":          inst.ExchSeg,
		"transactiontype":   tranType,
		"ordertype":         "MARKET",
		"quantity":          fmt.Sprintf("%d", absQty),
		"producttype":       prd,
		"price":             "0",
		"triggerprice":      "0",
		"squareoff":         "0",
		"stoploss":          "0",
		"trailingStopLoss":  "",
		"disclosedquantity": "0",
		"duration":          "DAY",
		"ordertag":          orderUUID[:15],
	}

	payloadBytes, _ := json.Marshal(payload)
	log.Printf("🚀 [MStock] Sending place_order payload: %s", string(payloadBytes))

	req, err := http.NewRequest("POST", apiURL, bytes.NewBuffer(payloadBytes))
	if err != nil {
		return err
	}
	
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Mirae-Version", "1")
	req.Header.Set("X-PrivateKey", cred.APIKey)
	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	if cred.StaticIP != "" {
		req.Header.Set("X-Static-IP", cred.StaticIP)
	}

	resp, err := e.client.Do(req)
	if err != nil {
		return fmt.Errorf("mstock place order network error: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	log.Printf("[MSTOCK EXIT] Response for %s: %s", cred.ClientID, string(body))

	var respData map[string]interface{}
	var arrData []map[string]interface{}
	if err := json.Unmarshal(body, &arrData); err == nil && len(arrData) > 0 {
		respData = arrData[0]
	} else {
		json.Unmarshal(body, &respData)
	}

	statusVal := fmt.Sprintf("%v", respData["status"])
	msgVal := strings.ToUpper(fmt.Sprintf("%v", respData["message"]))
	if statusVal == "true" || msgVal == "SUCCESS" {
		log.Printf("[MSTOCK EXIT SUCCESS] Order placed for %s: %v", pos.Symbol, respData["data"])
		return nil
	}

	errStr := fmt.Sprintf("%v", respData["message"])
	errCode := fmt.Sprintf("%v", respData["errorcode"])
	errType := fmt.Sprintf("%v", respData["error_type"])

	lowerErr := strings.ToLower(errStr)
	lowerCode := strings.ToLower(errCode)

	if resp.StatusCode == 401 || resp.StatusCode == 403 ||
		errType == "TokenException" ||
		strings.HasPrefix(errCode, "IA") ||
		strings.HasPrefix(errCode, "MA") ||
		strings.Contains(lowerErr, "expired") ||
		strings.Contains(lowerErr, "invalid") ||
		strings.Contains(lowerErr, "token") ||
		strings.Contains(lowerCode, "ia401") ||
		strings.Contains(lowerCode, "ia403") ||
		strings.Contains(lowerCode, "ma401") ||
		strings.Contains(lowerCode, "ma403") {
		log.Printf("[MSTOCK EXIT] Token exception detected (%s, %s, %s). Triggering token refresh...", errStr, errCode, errType)
		go credentials.RefreshAccessToken(e.db, &cred, e.proxyURL)
	}

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("mstock returned status %d: %s", resp.StatusCode, string(body))
	}

	return fmt.Errorf("mstock order rejected: %s (errorcode: %s)", errStr, errCode)
}

func (e *Engine) executeAliceBlueWeb(pos models.Position) error {
	var creds []models.Credential
	err := e.db.Select(&creds, "SELECT * FROM credentials WHERE client_id = ? AND broker = ?", pos.ClientID, config.BrokerAliceBlue)
	if err != nil || len(creds) == 0 {
		return fmt.Errorf("credentials not found for aliceblue %s", pos.ClientID)
	}
	cred := creds[0]

	apiURL := "https://antdrn.aliceblueonline.com/omk/od-rest/orders/web/execute"

	tranType := "S"
	if pos.Quantity < 0 {
		tranType = "B"
	}
	absQty := int(math.Abs(float64(pos.Quantity)))

	orderUUID := uuid.New().String()
	
	prd := pos.ProductType
	if prd == "DELIVERY" || prd == "LONGTERM" || prd == "CNC" {
		prd = "CNC"
	} else if prd == "INTRADAY" || prd == "MIS" {
		prd = "MIS"
	}

	payloadArr := []map[string]interface{}{
		{
			"deviceNumber":     "57a9f9b93ffbd16d3e4be86448a7a472",
			"exchange":         pos.Exchange,
			"token":            pos.Token,
			"tradingSymbol":    pos.Symbol,
			"ltp":              "",
			"qty":              fmt.Sprintf("%d", absQty),
			"price":            0.0,
			"product":          prd,
			"priceType":        "MKT",
			"orderType":        "Regular",
			"transType":        tranType,
			"ret":              "DAY",
			"triggerPrice":     0,
			"disclosedQty":     "",
			"mktProtection":    "",
			"target":           "",
			"stopLoss":         "",
			"trailingStopLoss": "",
			"orderTag":         orderUUID,
		},
	}

	payloadBytes, _ := json.Marshal(payloadArr)

	req, err := http.NewRequest("POST", apiURL, bytes.NewBuffer(payloadBytes))
	if err != nil {
		return err
	}
	
	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "*/*")
	req.Header.Set("Accept-Encoding", "identity")
	req.Header.Set("Accept-Language", "en")
	req.Header.Set("Origin", "https://ant.aliceblueonline.com")
	req.Header.Set("Referer", "https://ant.aliceblueonline.com/")
	req.Header.Set("Sec-Fetch-Mode", "cors")
	req.Header.Set("Sec-Fetch-Site", "same-site")
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

	resp, err := e.directClient.Do(req)
	if err != nil {
		return fmt.Errorf("aliceblue web place order network error: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	log.Printf("[ALICEBLUE WEB EXIT] Response for %s: %s", cred.ClientID, string(body))

	if resp.StatusCode != http.StatusOK {
		if resp.StatusCode == 401 {
			go credentials.RefreshAccessToken(e.db, &cred, e.proxyURL)
		}
		return fmt.Errorf("aliceblue web returned status %d: %s", resp.StatusCode, string(body))
	}

	var data []map[string]interface{}
	err = json.Unmarshal(body, &data)
	if err == nil && len(data) > 0 {
		stat, _ := data[0]["stat"].(string)
		status, _ := data[0]["status"].(string)
		msg, _ := data[0]["message"].(string)
		if strings.EqualFold(stat, "ok") || strings.EqualFold(status, "ok") || strings.EqualFold(msg, "success") {
			log.Printf("[ALICEBLUE WEB EXIT SUCCESS] Order placed for %s: %v", pos.Symbol, data[0]["result"])
			return nil
		}
		
		errStr := fmt.Sprintf("%v", data[0]["emsg"])
		if errStr == "" || errStr == "<nil>" {
			errStr = fmt.Sprintf("%v", data[0]["message"])
		}
		lowerErr := strings.ToLower(errStr)
		if strings.Contains(errStr, "401") || strings.Contains(lowerErr, "unauthorized") || strings.Contains(errStr, "Invalid Session") || strings.Contains(errStr, "Session Expired") {
			go credentials.RefreshAccessToken(e.db, &cred, e.proxyURL)
		}
		return fmt.Errorf("aliceblue web order rejected: %s", errStr)
	}

	// Token expiry check for non-array responses
	var singleData map[string]interface{}
	if err := json.Unmarshal(body, &singleData); err == nil {
		stat, _ := singleData["stat"].(string)
		status, _ := singleData["status"].(string)
		msg, _ := singleData["message"].(string)
		if strings.EqualFold(stat, "ok") || strings.EqualFold(status, "ok") || strings.EqualFold(msg, "success") {
			log.Printf("[ALICEBLUE WEB EXIT SUCCESS] Order placed for %s: %v", pos.Symbol, singleData["result"])
			return nil
		}
		errStr := fmt.Sprintf("%v", singleData["message"])
		lowerErr := strings.ToLower(errStr)
		if strings.Contains(errStr, "401") || strings.Contains(lowerErr, "unauthorized") || strings.Contains(errStr, "Invalid Session") || strings.Contains(errStr, "Session Expired") {
			go credentials.RefreshAccessToken(e.db, &cred, e.proxyURL)
		}
		return fmt.Errorf("aliceblue web order rejected: %s", errStr)
	}

	return fmt.Errorf("aliceblue web order rejected: %s", string(body))
}

func (e *Engine) executeAliceBlueAPI(pos models.Position) error {
	var creds []models.Credential
	err := e.db.Select(&creds, "SELECT * FROM credentials WHERE client_id = ? AND broker = ?", pos.ClientID, config.BrokerAliceBlue)
	if err != nil || len(creds) == 0 {
		return fmt.Errorf("credentials not found for aliceblue %s", pos.ClientID)
	}
	cred := creds[0]

	url := "https://a3.aliceblueonline.com/open-api/od/v1/orders/placeorder"

	tranType := "SELL"
	if pos.Quantity < 0 {
		tranType = "BUY"
	}
	absQty := int(math.Abs(float64(pos.Quantity)))

	orderUUID := uuid.New().String()
	
	prd := pos.ProductType
	if prd == "CNC" {
		prd = "LONGTERM"
	}

	payloadArr := []map[string]interface{}{
		{
			"exchange":                pos.Exchange,
			"instrumentId":            pos.Token,
			"transactionType":         tranType,
			"quantity":                absQty,
			"product":                 prd,
			"orderComplexity":         "REGULAR",
			"orderType":               "MARKET",
			"validity":                "DAY",
			"price":                   "0",
			"slLegPrice":              "0",
			"targetLegPrice":          "0",
			"slTriggerPrice":          "0",
			"disclosedQuantity":       "",
			"marketProtectionPercent": "",
			"deviceId":                "DRMT_ENGINE",
			"trailingSlAmount":        "",
			"apiOrderSource":          "",
			"algoId":                  "",
			"orderTag":                orderUUID,
		},
	}

	payloadBytes, _ := json.Marshal(payloadArr)

	req, err := http.NewRequest("POST", url, bytes.NewBuffer(payloadBytes))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+cred.AccessToken)
	req.Header.Set("User-Agent", "Mozilla/5.0")

	resp, err := e.directClient.Do(req)
	if err != nil {
		return fmt.Errorf("aliceblue place order network error: %v", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	log.Printf("[ALICEBLUE EXIT] Response for %s: %s", cred.ClientID, string(body))

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("aliceblue returned status %d: %s", resp.StatusCode, string(body))
	}

	var data map[string]interface{}
	json.Unmarshal(body, &data)

	stat, _ := data["stat"].(string)
	status, _ := data["status"].(string)
	msg, _ := data["message"].(string)
	if strings.EqualFold(stat, "ok") || strings.EqualFold(status, "ok") || strings.EqualFold(msg, "success") {
		log.Printf("[ALICEBLUE EXIT SUCCESS] Order placed for %s", pos.Symbol)
		return nil
	}

	// Token expiry check
	errStr := fmt.Sprintf("%v", data["message"])
	lowerErr := strings.ToLower(errStr)
	if strings.Contains(errStr, "401") || strings.Contains(lowerErr, "unauthorized") || strings.Contains(errStr, "Invalid Session") || strings.Contains(errStr, "Session Expired") {
		go credentials.RefreshAccessToken(e.db, &cred, e.proxyURL)
	}

	return fmt.Errorf("aliceblue order rejected: %s", errStr)
}

