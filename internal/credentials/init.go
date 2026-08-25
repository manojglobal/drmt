package credentials

import (
	"fmt"
	"drmt/internal/logger"

	"drmt/internal/config"
	"drmt/internal/models"
	"github.com/jmoiron/sqlx"
)

type Initializer struct {
	db      *sqlx.DB
	cfg     *config.Config
	dropbox *DropboxClient
	dedup   *DedupEngine
}

func NewInitializer(db *sqlx.DB, cfg *config.Config) *Initializer {
	dropbox := NewDropboxClient(cfg)
	aliceGen := NewAliceBlueGenerator(cfg.ProxyURL)
	flattradeGen := NewFlattradeGenerator(cfg.ProxyURL)
	mstockGen := NewMStockGenerator(cfg.ProxyURL)
	dedup := NewDedupEngine(db, cfg.ProxyURL, cfg.StaticIP, aliceGen, flattradeGen, mstockGen)

	return &Initializer{
		db:      db,
		cfg:     cfg,
		dropbox: dropbox,
		dedup:   dedup,
	}
}

func (i *Initializer) Run() error {
	logger.Auth.Info("Starting credential initialization (DB Only)...")

	// 1. Fetch from DB instead of Dropbox/CSV
	var dbCreds []models.Credential
	if err := i.db.Select(&dbCreds, "SELECT * FROM credentials"); err != nil {
		return fmt.Errorf("fetch credentials from db: %w", err)
	}

	var creds []ParsedCredential
	for _, c := range dbCreds {
		creds = append(creds, ParsedCredential{
			Credential: c,
		})
	}
	
	logger.Auth.Info("Loaded %d credential entries from DB", len(creds))

	// 2. Run deduplication and token generation
	if err := i.dedup.ProcessCredentials(creds); err != nil {
		return fmt.Errorf("process credentials: %w", err)
	}
	logger.Auth.Info("Credential processing completed")

	// 3. Log summary
	i.logSummary()

	return nil
}

func (i *Initializer) logSummary() {
	var stats struct {
		Total       int
		Active      int
		Failed      int
		Skipped     int
		MaxExceeded int
	}

	rows, err := i.db.Queryx(`
		SELECT status, COUNT(*) as count FROM credentials GROUP BY status
	`)
	if err != nil {
		logger.Auth.Info("Failed to get summary: %v", err)
		return
	}
	defer rows.Close()

	for rows.Next() {
		var status string
		var count int
		if err := rows.Scan(&status, &count); err != nil {
			continue
		}
		switch status {
		case "active":
			stats.Active = count
		case "failed", "max_retries_exceeded":
			stats.Failed += count
		case "skipped":
			stats.Skipped = count
		case "max_credential_sets_exceeded":
			stats.MaxExceeded = count
		}
		stats.Total += count
	}

	logger.Auth.Info("Credential Summary: Total=%d, Active=%d, Failed=%d, Skipped=%d, MaxExceeded=%d",
		stats.Total, stats.Active, stats.Failed, stats.Skipped, stats.MaxExceeded)
}

func InitializeCredentials(db *sqlx.DB, cfg *config.Config) error {
	init := NewInitializer(db, cfg)
	return init.Run()
}