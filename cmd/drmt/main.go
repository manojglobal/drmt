package main

import (
	"context"
	"io/fs"
	"log"
	"net/http"
	"os"

	"drmt"
	"drmt/internal/api"
	"drmt/internal/auth"
	"drmt/internal/config"
	"drmt/internal/credentials"
	"drmt/internal/execution"
	"drmt/internal/persistence"
	"drmt/internal/position"
)

func main() {
	log.Println("Starting DRMT V2 Server...")

	// 1. Load configuration
	cfg := config.Load()
	log.Println("[INIT] Configuration loaded")

	// 2. Initialize SQLite database using embedded migrations
	log.Println("[INIT] Connecting to SQLite Database (data/drmt.db)...")
	os.MkdirAll("data", 0755)
	db, err := persistence.InitDB("data/drmt.db", drmt.AssetsFS)
	if err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	log.Println("[INIT] SQLite Database initialized successfully")

	// 3. Ensure a default admin user exists
	log.Println("[INIT] Verifying Admin User...")
	var count int
	db.Get(&count, "SELECT COUNT(*) FROM users")
	if count == 0 {
		hash, _ := auth.HashPassword("admin")
		db.Exec("INSERT INTO users (username, password_hash) VALUES (?, ?)", "admin", hash)
		log.Println("[INIT] Created default admin user (admin/admin). Please change this in production.")
	} else {
		log.Println("[INIT] Admin user verified")
	}

	// 4. Initialize credentials from DB directly (Dropbox bypassed)
	log.Println("[INIT] Starting Credential Token Validation & Generation...")
	if err := credentials.InitializeCredentials(db, cfg); err != nil {
		log.Printf("[INIT] Warning: Credential initialization failed: %v", err)
	}
	log.Println("[INIT] Credential initialization phase complete")

	// 5. Initialize Position Manager & Ingestion Layer
	log.Println("[INIT] Booting Position Manager & Polling Adapters...")
	ctx := context.Background()
	posManager := position.NewManager()
	go posManager.Start(ctx)

	// Start broker adapters
	mstockAdapter := position.NewMStockAdapter(db, cfg.ProxyURL)
	go mstockAdapter.Start(ctx, posManager.Feed)

	aliceAdapter := position.NewAliceBlueAdapter(db, cfg.ProxyURL)
	go aliceAdapter.Start(ctx, posManager.Feed)

	flattradeAdapter := position.NewFlattradeAdapter(db, cfg.ProxyURL)
	go flattradeAdapter.Start(ctx, posManager.Feed)
	log.Println("[INIT] All Broker Polling Adapters dispatched")
	
	// Initialize Mass Execution Engine
	log.Println("[INIT] Starting Mass Execution Engine...")
	execEngine := execution.NewEngine(db, cfg.ProxyURL)
	log.Println("[INIT] Mass Execution Engine ready")

	// 6. Setup Routes
	log.Println("[INIT] Configuring HTTP API Routes...")
	mux := http.NewServeMux()

	// API Routes
	mux.HandleFunc("/api/login", api.LoginHandler(db))
	mux.HandleFunc("/api/logout", api.LogoutHandler(db))
	mux.HandleFunc("/api/me", api.AuthMiddleware(db, api.MeHandler()))
	mux.HandleFunc("/api/positions", api.AuthMiddleware(db, api.PositionsHandler(posManager)))
	mux.HandleFunc("/api/positions/exit", api.AuthMiddleware(db, api.PositionsExitHandler(db, posManager, execEngine)))
	mux.HandleFunc("/api/positions/exited", api.AuthMiddleware(db, api.ExitedPositionsHandler(db)))
	mux.HandleFunc("/api/positions/exited/dates", api.AuthMiddleware(db, api.ExitedDatesHandler(db)))
	log.Println("[INIT] HTTP API Routes mapped successfully")

	// 6. Serve Embedded React Frontend
	log.Println("[INIT] Mounting Embedded React Frontend...")
	webSub, err := fs.Sub(drmt.AssetsFS, "web")
	if err != nil {
		log.Fatalf("Failed to sub embedded web FS: %v", err)
	}
	mux.Handle("/", http.FileServer(http.FS(webSub)))
	log.Println("[INIT] Frontend mounted successfully")

	// 7. Start Server
	log.Println("[INIT] === DRMT V2 BOOT SEQUENCE COMPLETE ===")
	log.Println("[INIT] DRMT V2 Server listening on :3100")
	if err := http.ListenAndServe(":3100", mux); err != nil {
		log.Fatalf("Server stopped: %v", err)
	}
}