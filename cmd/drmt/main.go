package main

import (
	"context"
	"io/fs"
	"net/http"
	"os"

	"drmt"
	"drmt/internal/api"
	"drmt/internal/auth"
	"drmt/internal/config"
	"drmt/internal/credentials"
	"drmt/internal/logger"
	"drmt/internal/health"
	"drmt/internal/execution"
	"drmt/internal/persistence"
	"drmt/internal/position"
)

func main() {
	logger.System.Info("Starting DRMT V2 Server...")

	// 1. Load configuration
	cfg := config.Load()
	logger.System.Info("[INIT] Configuration loaded")

	// 2. Initialize SQLite database using embedded migrations
	logger.System.Info("[INIT] Connecting to SQLite Database (data/drmt.db)...")
	os.MkdirAll("data", 0755)
	db, err := persistence.InitDB("data/drmt.db", drmt.AssetsFS)
	if err != nil {
		logger.System.Error("Failed to initialize database: %v", err)
	}
	logger.System.Info("[INIT] SQLite Database initialized successfully")

	// 3. Ensure a default admin user exists
	logger.System.Info("[INIT] Verifying Admin User...")
	var count int
	db.Get(&count, "SELECT COUNT(*) FROM users")
	if count == 0 {
		hash, _ := auth.HashPassword("admin")
		db.Exec("INSERT INTO users (username, password_hash) VALUES (?, ?)", "admin", hash)
		logger.System.Info("[INIT] Created default admin user (admin/admin). Please change this in production.")
	} else {
		logger.System.Info("[INIT] Admin user verified")
	}

	// 4. Initialize credentials from DB directly (Dropbox bypassed)
	logger.System.Info("[INIT] Starting Credential Token Validation & Generation...")
	if err := credentials.InitializeCredentials(db, cfg); err != nil {
		logger.System.Info("[INIT] Warning: Credential initialization failed: %v", err)
	}
	logger.System.Info("[INIT] Credential initialization phase complete")

	// 5. Initialize Position Manager & Ingestion Layer
	logger.System.Info("[INIT] Booting Position Manager & Polling Adapters...")
	ctx := context.Background()
	posManager := position.NewManager()
	go posManager.Start(ctx)

	logger.System.Info("[INIT] Booting Server Health Monitor...")
	healthMonitor := health.NewMonitor()
	go healthMonitor.Start(ctx)

	// Start broker adapters
	mstockAdapter := position.NewMStockAdapter(db, cfg.ProxyURL)
	go mstockAdapter.Start(ctx, posManager.Feed)

	aliceAdapter := position.NewAliceBlueAdapter(db, cfg.ProxyURL)
	go aliceAdapter.Start(ctx, posManager.Feed)

	flattradeAdapter := position.NewFlattradeAdapter(db, cfg.ProxyURL)
	go flattradeAdapter.Start(ctx, posManager.Feed)
	logger.System.Info("[INIT] All Broker Polling Adapters dispatched")
	
	// Initialize Mass Execution Engine
	logger.System.Info("[INIT] Starting Mass Execution Engine...")
	execEngine := execution.NewEngine(db, cfg.ProxyURL)
	logger.System.Info("[INIT] Mass Execution Engine ready")

	// 6. Setup Routes
	logger.System.Info("[INIT] Configuring HTTP API Routes...")
	mux := http.NewServeMux()

	// API Routes
	mux.HandleFunc("/api/login", api.LoginHandler(db))
	mux.HandleFunc("/api/logout", api.LogoutHandler(db))
	mux.HandleFunc("/api/me", api.AuthMiddleware(db, api.MeHandler()))
	mux.HandleFunc("/api/positions", api.AuthMiddleware(db, api.PositionsHandler(posManager)))
	mux.HandleFunc("/api/positions/exit", api.AuthMiddleware(db, api.PositionsExitHandler(db, posManager, execEngine)))
	mux.HandleFunc("/api/health/servers", api.AuthMiddleware(db, api.ServersHealthHandler(healthMonitor)))
	mux.HandleFunc("/api/positions/exited", api.AuthMiddleware(db, api.ExitedPositionsHandler(db)))
	mux.HandleFunc("/api/positions/exited/dates", api.AuthMiddleware(db, api.ExitedDatesHandler(db)))
	logger.System.Info("[INIT] HTTP API Routes mapped successfully")

	// 6. Serve Embedded React Frontend
	logger.System.Info("[INIT] Mounting Embedded React Frontend...")
	webSub, err := fs.Sub(drmt.AssetsFS, "web")
	if err != nil {
		logger.System.Error("Failed to sub embedded web FS: %v", err)
	}
	mux.Handle("/", http.FileServer(http.FS(webSub)))
	logger.System.Info("[INIT] Frontend mounted successfully")

	// 7. Start Server
	logger.System.Info("[INIT] === DRMT V2 BOOT SEQUENCE COMPLETE ===")
	logger.System.Info("[INIT] DRMT V2 Server listening on :3100")
	if err := http.ListenAndServe(":3100", mux); err != nil {
		logger.System.Error("Server stopped: %v", err)
	}
}