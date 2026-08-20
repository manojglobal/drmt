package persistence

import (
	"embed"
	"fmt"
	"strings"

	"github.com/jmoiron/sqlx"
	_ "modernc.org/sqlite"
)

// InitDB initializes the SQLite database, enables WAL mode, and runs migrations.
func InitDB(dbPath string, migrationsFS embed.FS) (*sqlx.DB, error) {
	// Open connection to SQLite
	db, err := sqlx.Connect("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("failed to connect to database: %w", err)
	}

	// Enable Write-Ahead Logging (WAL) for high concurrency
	_, err = db.Exec("PRAGMA journal_mode=WAL;")
	if err != nil {
		return nil, fmt.Errorf("failed to enable WAL mode: %w", err)
	}

	// Enable foreign keys
	_, err = db.Exec("PRAGMA foreign_keys = ON;")
	if err != nil {
		return nil, fmt.Errorf("failed to enable foreign keys: %w", err)
	}

	// Read initialization migration
	initSQL, err := migrationsFS.ReadFile("migrations/001_init.sql")
	if err != nil {
		return nil, fmt.Errorf("failed to read 001_init.sql: %w", err)
	}

	// Execute migration
	_, err = db.Exec(string(initSQL))
	if err != nil {
		return nil, fmt.Errorf("failed to execute 001_init.sql: %w", err)
	}

	// Read and execute extended credentials migration
	extSQL, err := migrationsFS.ReadFile("migrations/002_credentials_extended.sql")
	if err != nil {
		return nil, fmt.Errorf("failed to read 002_credentials_extended.sql: %w", err)
	}

	_, err = db.Exec(string(extSQL))
	if err != nil && !strings.Contains(err.Error(), "duplicate column name") {
		return nil, fmt.Errorf("failed to execute 002_credentials_extended.sql: %w", err)
	}

	return db, nil
}
