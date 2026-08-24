package execution

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"sync"
)

type MStockInstrument struct {
	Token          string `json:"token"`
	Name           string `json:"name"`
	InstrumentType string `json:"instrumenttype"`
	ExchSeg        string `json:"exch_seg"`
}

var mstockInstrumentCache = make(map[string]MStockInstrument)
var mstockCacheMutex sync.RWMutex

func LoadMStockInstruments() error {
	url := "https://api.mstock.trade/openapi/typeb/instruments/OpenAPIScripMaster"
	log.Println("Fetching MStock Instrument Master...")

	resp, err := http.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("bad status fetching mstock instruments: %d", resp.StatusCode)
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}

	var data []MStockInstrument
	if err := json.Unmarshal(body, &data); err != nil {
		return err
	}

	mstockCacheMutex.Lock()
	defer mstockCacheMutex.Unlock()

	for _, item := range data {
		if item.Token == "" {
			continue
		}

		existing, ok := mstockInstrumentCache[item.Token]
		if !ok {
			mstockInstrumentCache[item.Token] = item
			continue
		}

		if existing.InstrumentType == "EQ" {
			continue
		}

		if item.InstrumentType == "EQ" {
			mstockInstrumentCache[item.Token] = item
		}
	}
	log.Printf("Loaded %d MStock instruments into cache", len(mstockInstrumentCache))
	return nil
}

func GetMStockInstrument(token string) (MStockInstrument, bool) {
	mstockCacheMutex.RLock()
	defer mstockCacheMutex.RUnlock()
	inst, ok := mstockInstrumentCache[token]
	return inst, ok
}
