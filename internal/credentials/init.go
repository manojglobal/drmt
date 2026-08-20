package credentials

import (
	"fmt"
	"log"

	"drmt/internal/config"
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
	log.Println("Starting credential initialization...")

	// 1. Download credentials from Dropbox
	localPath, err := i.dropbox.DownloadCredentials(config.LocalCredentialsDir)
	if err != nil {
		return fmt.Errorf("download credentials from Dropbox: %w", err)
	}
	log.Printf("Downloaded credentials to: %s", localPath)

	// 2. Parse CSV
	creds, err := ParseCSV(localPath)
	if err != nil {
		return fmt.Errorf("parse CSV: %w", err)
	}
	log.Printf("Parsed %d credential entries", len(creds))

	// 3. Run deduplication and token generation
	if err := i.dedup.ProcessCredentials(creds); err != nil {
		return fmt.Errorf("process credentials: %w", err)
	}
	log.Println("Credential processing completed")

	// 4. Log summary
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
		log.Printf("Failed to get summary: %v", err)
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

	log.Printf("Credential Summary: Total=%d, Active=%d, Failed=%d, Skipped=%d, MaxExceeded=%d",
		stats.Total, stats.Active, stats.Failed, stats.Skipped, stats.MaxExceeded)
}

func InitializeCredentials(db *sqlx.DB, cfg *config.Config) error {
	init := NewInitializer(db, cfg)
	return init.Run()
}