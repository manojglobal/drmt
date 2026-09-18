# DRMT V2 — PROJECT CONTEXT

> **Disaster Recovery Management Tool** — A self-contained Python backend + embedded React frontend that monitors live trading positions across multiple Indian stock brokers and provides a single "emergency exit" button to flatten them with a market order.

---

## 1. Project Purpose & Overview

DRMT V2 is an internal trading operations tool used by **Quantivo** (an algorithmic trading firm). Its core job is:

1. **Aggregate live open positions** from multiple broker accounts simultaneously (MStock, AliceBlue, Flattrade).
2. **Display them in a real-time dashboard** (React SPA served from Python itself).
3. **Allow bulk or selective emergency exit** — clicking "Close All" or selecting specific positions fires market orders to the relevant broker APIs to flatten those positions immediately.
4. **Track exit history** — successfully placed exits are recorded in SQLite for audit / post-trade analysis.
5. **Monitor broker credentials** — automatically generates (via TOTP + password login) and refreshes broker access tokens on startup and on 401 errors.
6. **Monitor server health** — pings configured trading platform servers (Quantivo test/demo) every minute.

This tool is designed for disaster recovery — if an algorithmic strategy misbehaves, the operator can log in and immediately exit all positions across all connected broker accounts from a single dashboard.

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| **Language** | Python 1.25 |
| **HTTP Server** | Python standard `net/http` (no frameworks) |
| **Database** | SQLite (via `modernc.org/sqlite`, WAL mode) |
| **ORM/Query** | `github.com/jmoiron/sqlx` |
| **WebSocket** | `github.com/gorilla/websocket` (Flattrade WS — unused in production) |
| **Auth Hashing** | `golang.org/x/crypto/bcrypt` |
| **TOTP** | `github.com/pquerna/otp` |
| **UUIDs** | `github.com/google/uuid` |
| **Frontend** | React 18 (via CDN), Babel standalone (in-browser JSX) |
| **Styling** | Vanilla CSS (`web/style.css`) |
| **Deployment** | Single binary (all web assets embedded via `go:embed`) or Docker |

**No build step is required for the frontend** — React and Babel are loaded from `unpkg.com` CDN. The `app.jsx` is parsed by Babel in the browser at runtime.

---

## 3. Folder / File Structure

```
drmt_v2/
├── cmd/
│   └── drmt/
│       └── main.py              # Entry point — wires everything together
├── 
│   ├── api/                     # HTTP handlers (REST API)
│   │   ├── exit.py              # POST /api/positions/exit + exited positions GET/DATE handlers
│   │   ├── handlers.py          # Login, Logout, Me, Positions handlers
│   │   ├── health.py            # GET /api/health/servers
│   │   ├── logs.py              # GET /api/logs
│   │   └── middleware.py        # AuthMiddleware (session cookie validation)
│   ├── auth/
│   │   └── auth.py              # bcrypt password, session token CRUD in SQLite
│   ├── broker/                  # EMPTY — placeholder only
│   ├── config/
│   │   ├── config.py            # Config struct + Load() from env/.env file
│   │   └── constants.py         # Broker names, API URLs, limits, ServersToCheck map
│   ├── credentials/             # Broker login automation & token lifecycle
│   │   ├── aliceblue.py         # AliceBlue 6-step web login + token generation
│   │   ├── crypto.py            # TOTP, SHA256, MD5 helpers
│   │   ├── dedup.py             # DedupEngine: groups creds, validates or regenerates tokens
│   │   ├── dropbox.py           # DropboxClient — reads CSV from Dropbox (BYPASSED)
│   │   ├── flattrade.py         # Flattrade login (SHA256 password + TOTP) token generation
│   │   ├── httpclient.py        # RESTProxyTransport + NewHTTPClientWithProxy
│   │   ├── init.py              # InitializeCredentials() — entry point for credential boot
│   │   ├── mstock.py            # MStock 2-step login (password + TOTP verify) token generation
│   │   ├── parser.py            # ParsedCredential struct (wraps models.Credential)
│   │   └── refresh.py           # RefreshAccessToken() — on-demand token refresh (3 retries)
│   ├── events/                  # EMPTY — placeholder only
│   ├── execution/
│   │   ├── engine.py            # Engine: ExecuteExit() — dispatches market orders per broker
│   │   └── mstock_instruments.py # LoadMStockInstruments() — token->symbol/exchange cache
│   ├── health/
│   │   └── monitor.py           # Monitor: pings ServersToCheck every 1 minute
│   ├── logger/
│   │   └── logger.py            # ServiceLogger: console + hourly rotating file logs + in-memory ring buffer
│   ├── models/
│   │   └── models.py            # All shared structs: User, Session, Credential, Position, ExitHistory, SystemLog, ExitedPosition
│   ├── persistence/
│   │   └── db.py                # InitDB() — opens SQLite, enables WAL + FK, runs migrations
│   └── position/
│       ├── adapter.py           # BrokerAdapter interface
│       ├── aliceblue.py         # AliceBlueAdapter — polls /positions every 5s
│       ├── flattrade.py         # FlattradeAdapter — polls /getPositonBook every 5s
│       ├── flattrade_ws.py      # FlattradeWSAdapter — WebSocket (NOT used in main.py)
│       ├── manager.py           # Manager: thread-safe in-memory position store; Feed channel
│       └── mstock.py            # MStockAdapter — polls /portfolio/positions every 5s
├── migrations/
│   ├── 001_init.sql             # Base schema: users, sessions, credentials, positions, exit_history, system_logs, exited_positions
│   └── 002_credentials_extended.sql # Adds broker-specific columns to credentials table
├── web/
│   ├── index.html               # SPA shell — loads React CDN + Babel + app.jsx
│   ├── app.jsx                  # Entire React frontend (1225 lines, single file)
│   └── style.css                # All CSS (dark theme, 19KB)
├── data/
│   ├── drmt.db                  # SQLite database (live data — DO NOT delete)
│   ├── drmt.db-shm              # SQLite WAL shared memory
│   ├── drmt.db-wal              # SQLite WAL log
│   └── logs/                   # Hourly rotating log files (per logger service)
├── static/
│   └── plato_client_credentials.csv  # CSV with broker credentials (legacy, now loaded from DB)
├── scripts/                     # Standalone Python test/debug scripts (not part of main binary)
│   ├── test_aliceblue.py
│   ├── test_flattrade.py
│   ├── test_flattrade_csv_token.py
│   ├── test_flattrade_pos.py
│   ├── test_flattrade_positions.py
│   └── update_db.py
├── embed.py                     # go:embed directive for web/* and migrations/*
├── requirements.txt                       # Module: drmt, Python 1.25
├── go.sum
├── Dockerfile                   # Multi-stage: golang:1.25-alpine -> alpine:latest
├── docker-compose.yml           # Maps port 8000, mounts ./data volume, TZ=Asia/Kolkata
├── drmt                         # Pre-built Linux binary (committed to repo)
├── drmt.exe / drmt-windows.exe  # Pre-built Windows binaries
├── server.log                   # Stdout log from running server
├── AGENTS.md / CLAUDE.md        # GitNexus AI code intelligence instructions
└── .gitnexus/                   # GitNexus index
```

---

## 4. Pages & Routes

### Backend HTTP Routes (registered in `main.py`)

| Method | Path | Handler | Auth Required |
|---|---|---|---|
| POST | `/api/login` | `LoginHandler` | No |
| POST | `/api/logout` | `LogoutHandler` | No |
| GET | `/api/me` | `MeHandler` | Yes |
| GET | `/api/positions` | `PositionsHandler` | Yes |
| POST | `/api/positions/exit` | `PositionsExitHandler` | Yes |
| GET | `/api/health/servers` | `ServersHealthHandler` | Yes |
| GET | `/api/logs` | `LogsHandler` | Yes |
| GET | `/api/positions/exited` | `ExitedPositionsHandler` | Yes |
| GET | `/api/positions/exited/dates` | `ExitedDatesHandler` | Yes |
| GET | `/` (catch-all) | Embedded file server -> `web/` directory | No |

### Frontend Views (single-page, in `web/app.jsx`)

The SPA has two states:
1. **Login Screen** — shown when not authenticated (no valid session cookie)
2. **Dashboard** — shown when authenticated

The Dashboard has two tabs:
- **Running** — live positions table, polling every 5s (market hours) or 15s (post-market)
- **Executed** — historical exits from SQLite, filterable by date range/broker/client/trade type, exportable as CSV

---

## 5. Components & Their Relationships

### Backend Architecture

```
main.py
├── config.Load()                          -> loads .env / env vars
├── persistence.InitDB()                   -> SQLite + migrations
├── auth.HashPassword() / db.Exec()        -> ensures admin user exists
├── credentials.InitializeCredentials()   -> validates/generates tokens at boot
├── position.NewManager() -> asyncio.create_task(Start())   -> central position feed aggregator
├── health.NewMonitor() -> asyncio.create_task(Start())     -> pings external servers
├── position.NewMStockAdapter() -> asyncio.create_task(Start())    -> polls MStock every 5s
├── position.NewAliceBlueAdapter() -> asyncio.create_task(Start()) -> polls AliceBlue every 5s
├── position.NewFlattradeAdapter() -> asyncio.create_task(Start()) -> polls Flattrade every 5s
├── execution.NewEngine()                  -> mass exit engine
└── http.NewServeMux()                     -> routes all API + frontend
```

### Frontend React Components (in `web/app.jsx`)

```
App                                 # Root: checks /api/me, renders Login or Dashboard
├── Login                           # Login form, POST /api/login
└── Dashboard                       # Main view after auth
    ├── ServerHealthMonitor         # Polls /api/health/servers every 5s, renders status pills in header
    ├── ActionLogWidget             # Polls /api/logs every 3s, shows Orders/Auth/System tabs
    └── (inline JSX)
        ├── Toast notifications     # Ephemeral success/error/warning toasts (5s TTL)
        ├── Confirmation Modal      # "Are you sure?" before any exit
        ├── Top navigation header   # Brand title + ServerHealthMonitor + Bell + User avatar + Sign Out
        ├── Running Tab             # Live positions table
        │   ├── Filters bar         # Search, Broker, User, Trade type
        │   ├── Action buttons      # "Close (N)" or "Close All", Cancel
        │   ├── Pagination          # 50 per page
        │   └── Positions table     # USERID, BROKER, INSTRUMENT, EXCHANGE, PRODUCT, BUY/SELL, QTY, ENTRY PRICE, PNL, LTP
        └── Executed Tab            # Historical exits from DB
            ├── Filters bar         # Search, From Date, To Date, Broker, User, Trade type, Refresh, Export CSV
            ├── Pagination          # 50 per page
            └── Executed table      # USERID, BROKER, INSTRUMENT, EXCHANGE, PRODUCT, BUY/SELL, QTY, ENTRY PRICE, EXIT PRICE, PNL, EXITED AT
```

---

## 6. Features & Business Logic

### 6.1 Credential Initialization (`credentials/`)

At startup, `InitializeCredentials()` is called:
1. Reads all credentials from the `credentials` SQLite table.
2. Groups them by `(broker, client_id, static_ip)` key.
3. For each group (max 4 credentials per client; otherwise flagged `max_credential_sets_exceeded`):
   - **Phase 1**: Try existing `access_token` — call broker's position endpoint to validate.
   - **Phase 2**: If no valid token, call `GenerateToken()` for each credential until one succeeds.
4. Saves status (`active`, `failed`, `skipped`, `max_retries_exceeded`) back to DB.

Status flow: `pending` -> `active` | `failed` | `skipped` | `max_retries_exceeded` | `max_credential_sets_exceeded`

### 6.2 Position Polling (`position/`)

Three polling adapters run concurrently in goroutines:
- **MStockAdapter**: Polls `https://api.mstock.trade/openapi/typeb/portfolio/positions` every 5s via proxy.
- **AliceBlueAdapter**: Polls `https://ant.aliceblueonline.com/open-api/od/v1/positions` every 5s directly.
- **FlattradeAdapter**: POSTs to `https://emergeapi.flattrade.in/getPositonBook` every 5s via proxy.

All adapters:
- Fetch all `active` credentials for their broker from DB.
- For each credential, spawn a goroutine to fetch positions.
- Push parsed `models.Position` structs into `posManager.Feed` channel.
- On 401/403 responses, trigger `credentials.RefreshAccessToken()` asynchronously.

**FlattradeWSAdapter** (`position/flattrade_ws.py`) exists but is **NOT wired** in `main.py`. The HTTP polling adapter is used instead.

### 6.3 Position Manager (`position/manager.py`)

- Receives from `Feed` channel (buffered 1000).
- Stores positions in `map[string]models.Position` keyed by `"{Broker}:{ClientID}:{Symbol}"`.
- Quantity == 0 -> position is deleted (closed position).
- Thread-safe via `sync.RWMutex`.
- Returns sorted snapshot via `GetAll()`.

### 6.4 Exit Execution (`execution/engine.py`)

`ExecuteExit(pos)` dispatches broker-specific market order:
- **Flattrade**: `POST https://emergeapi.flattrade.in/placeOrder` — uses `jkey` (access token) in JSON payload, `prctyp=MKT`.
- **MStock**: `POST https://api.mstock.trade/openapi/typeb/orders/regular` — uses `Authorization: Bearer <token>` + `X-PrivateKey`. Requires instrument lookup from in-memory cache (`LoadMStockInstruments()`).
- **AliceBlue**: `POST https://antdrn.aliceblueonline.com/omk/od-rest/orders/web/execute` — uses `Authorization: Bearer <token>`.

On 401/session errors, triggers async `RefreshAccessToken()` for that credential.

Note: `executeAliceBlueAPI()` (using `a3.aliceblueonline.com`) is dead code — `executeAliceBlueWeb()` is the active implementation.

### 6.5 Exit History Recording (`api/exit.py`)

On successful exit only:
- Inserts a row into `exited_positions` table with position details.
- `exit_price` = LTP (or `avg_price` if LTP is 0).
- `exited_at = datetime('now', 'localtime')`.

### 6.6 Token Refresh (`credentials/refresh.py`)

`RefreshAccessToken(db, cred, proxyURL)`:
- Up to 3 retries with exponential backoff (2s, 4s).
- On success: updates `access_token`, `access_token_created_at`, `status='active'`, clears `last_error` in DB.
- On all failures: updates `status='failed'`, stores `last_error`.

### 6.7 Polling Behavior (Frontend)

The frontend intelligently throttles polling:

| Condition | Polling interval |
|---|---|
| Tab hidden | Paused |
| Market closed (weekend or outside 08:50-23:55 IST) | Paused |
| Post-market (16:00-23:55 IST), Running tab | 15s |
| Active market hours, Running tab | 5s |
| Executed tab | 30s |
| `TEST_MODE_FORCE_POLL = true` (hardcoded flag in Dashboard) | Always 5s |

---

## 7. Data / API Flow

### Startup Flow

```
main.py starts
 |
 +-- config.Load()         -> reads .env / environment variables
 +-- persistence.InitDB()  -> opens data/drmt.db, runs SQL migrations
 +-- create admin user     -> INSERT INTO users IF count=0
 +-- credentials.InitializeCredentials()
 |    +-- reads credentials from DB -> validates/generates tokens -> saves back
 +-- position.NewManager() -> goroutine (listens on Feed channel)
 +-- health.NewMonitor()   -> goroutine (pings every 1 min)
 +-- MStockAdapter         -> goroutine (polls every 5s -> writes to Feed)
 +-- AliceBlueAdapter      -> goroutine (polls every 5s -> writes to Feed)
 +-- FlattradeAdapter      -> goroutine (polls every 5s -> writes to Feed)
 +-- execution.NewEngine() -> loads MStock instruments cache (goroutine)
 +-- http.ListenAndServe(":3100", mux)
```

### Position Fetch Flow

```
Browser -> GET /api/positions -> PositionsHandler -> posManager.GetAll() -> JSON response
```

### Exit Flow

```
Browser -> POST /api/positions/exit { position_ids: [...] }
         -> PositionsExitHandler
         -> for each id: posManager.GetByID(id)
         -> goroutine: engine.ExecuteExit(pos)
              -> switch pos.Broker:
                   FLATTRADE  -> POST emergeapi.flattrade.in/placeOrder
                   MSTOCK     -> POST api.mstock.trade/.../orders/regular
                   ALICE BLUE -> POST antdrn.aliceblueonline.com/.../execute
         -> on success: INSERT INTO exited_positions
         -> wg.Wait() -> JSON response with per-position results
```

### Login Flow

```
Browser -> POST /api/login { username, password }
         -> LoginHandler
         -> SELECT user WHERE username=?
         -> bcrypt.CompareHashAndPassword
         -> auth.CreateSession() -> INSERT INTO sessions (token, user_id, expires_at)
         -> Set-Cookie: drmt_session=<token>; HttpOnly; SameSite=Strict; Expires=24h
```

---

## 8. Authentication

- **Session-based**: HttpOnly cookie `drmt_session` containing a 64-character hex token.
- **24-hour expiry** stored in SQLite `sessions` table.
- `AuthMiddleware` validates the cookie on every protected route by querying `sessions JOIN users` checking `expires_at > now()`.
- Invalid/expired sessions: cookie is cleared, 401 returned.
- Default user on fresh DB: `admin / admin` (created at boot if `users` table is empty).
- **WARNING**: Default credentials must be changed before production deployment.

---

## 9. UI/UX & Design System

- **Dark theme** with a trading-terminal aesthetic.
- Color scheme: dark background (#0D1117 / #111827 region), blue accents, green for BUY / PnL positive, red for SELL / PnL negative.
- All CSS is in `web/style.css` (19KB, vanilla CSS, no framework).
- Key CSS classes:
  - `.app-layout`, `.top-nav`, `.main-content` — page skeleton
  - `.health-pill`, `.health-dot`, `.status-up`, `.status-down` — server status indicators
  - `.action-log-widget`, `.log-entry` — live log panel
  - `.compact-table`, `.side-badge`, `.pnl-positive`, `.pnl-negative` — data table
  - `.toast`, `.toast-success`, `.toast-error`, `.toast-warning` — notifications
  - `.modal-overlay`, `.modal-content` — confirmation dialog
  - `.btn-primary`, `.btn-close-action`, `.btn-cancel-action` — action buttons
- `fetchWithTimeout()` utility: wraps `fetch()` with AbortController (default 30s timeout).
- `useDebounce()` hook: 500ms debounce on search inputs.

---

## 10. External Integrations

### Broker APIs

| Broker | Position Fetch | Order Placement | Auth Method |
|---|---|---|---|
| **MStock** | `GET api.mstock.trade/openapi/typeb/portfolio/positions` | `POST .../orders/regular` | `Bearer <jwtToken>` + `X-PrivateKey` |
| **AliceBlue** | `GET ant.aliceblueonline.com/open-api/od/v1/positions` | `POST antdrn.aliceblueonline.com/omk/od-rest/orders/web/execute` | `Bearer <userSession>` |
| **Flattrade** | `POST emergeapi.flattrade.in/getPositonBook` | `POST emergeapi.flattrade.in/placeOrder` | `jkey` in JSON payload |

### Proxy Server (Quantivo)

- `DefaultProxyURL = "https://demoapi.quantivo.in/proxy"`
- A custom `RESTProxyTransport` in `httpclient.py` wraps all MStock and Flattrade requests.
- The proxy is a REST API that forwards HTTP requests on behalf of the caller; supports `source_ip` for IP whitelisting.
- AliceBlue uses direct HTTP (no proxy).

### Dropbox (BYPASSED)

- `credentials/dropbox.py` — `DropboxClient` can download `plato_client_credentials.csv` from Dropbox.
- **Currently bypassed**: `init.py` reads directly from SQLite DB instead.
- Dead code in the active flow.

### MStock Instruments Cache

- `execution/mstock_instruments.py` — `LoadMStockInstruments()` builds a `token -> {Name, ExchSeg}` map.
- Required to resolve `pos.Token` -> correct `tradingsymbol` and `exchange` when placing MStock orders.
- Loaded asynchronously on engine startup. If it fails, MStock exits will fail with "instrument not found in cache".

### Health Check Servers

Configured in `config/constants.py`:
```go
var ServersToCheck = map[string]string{
    "test_server": "https://testpts.quantivo.in/api/health",
    "demo_server": "https://demopts.quantivo.in/api/health",
}
```
Pinged every 1 minute with a 2-second timeout. Results shown in the UI header.

---

## 11. Important Configurations

### Environment Variables / `.env` file

The app searches for `.env` in (in order):
1. Same directory as the executable
2. Parent directory of the executable
3. `/opt/drmt/backend/.env`
4. Current working directory

| Variable | Default | Purpose |
|---|---|---|
| `DRMT_DROPBOX_APP_KEY` | `7yn83e37zk7108e` | Dropbox API key (bypassed) |
| `DRMT_DROPBOX_APP_SECRET` | `yneynsmqfcsudzo` | Dropbox API secret (bypassed) |
| `DRMT_DROPBOX_REFRESH_TOKEN` | (hardcoded long token) | Dropbox refresh token (bypassed) |
| `DRMT_PROXY_URL` | `https://demoapi.quantivo.in/proxy` | Proxy server URL |
| `DRMT_STATIC_IP` | `""` | Static IP for requests requiring IP whitelisting |

### Key Tunables (in `constants.py`)

| Constant | Value | Meaning |
|---|---|---|
| `MaxCredentialSetsPerClient` | 4 | Max credential entries per broker+client combo |
| `MaxTokenGenRetries` | 3 | Token generation retries |
| `HTTPTimeoutSeconds` | 20 | HTTP client timeout |
| `HTTPMaxRetries` | 5 | Defined but NOT actively used in any retry loop |
| Position polling interval | 5s | Hardcoded in each adapter constructor |
| Health monitor interval | 1 min | Hardcoded in `monitor.py` |
| Session TTL | 24h | Hardcoded in `auth.py` |

---

## 12. Database Schema

**SQLite** file at `data/drmt.db` (WAL mode, foreign keys enabled).

### `users`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| username | TEXT UNIQUE NOT NULL | |
| password_hash | TEXT NOT NULL | bcrypt |
| created_at | DATETIME | |

### `sessions`
| Column | Type | Notes |
|---|---|---|
| token | TEXT PK | 64-char hex |
| user_id | INTEGER | FK -> users.id |
| expires_at | DATETIME | 24h from creation |

### `credentials`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| broker | TEXT | "MSTOCK" / "ALICE BLUE" / "FLATTRADE" |
| api_key | TEXT | Broker API key |
| api_secret | TEXT | Broker API secret |
| fingerprint | TEXT UNIQUE | Deduplication/upsert key |
| client_id | TEXT | Broker client/user ID |
| password | TEXT | **Plaintext** broker login password |
| totp_secret | TEXT | TOTP secret for 2FA |
| api_key_encrypted | TEXT | MStock-specific field |
| app_code | TEXT | AliceBlue vendor app code |
| access_token | TEXT | Active Session Cookie (HttpOnly)/session token |
| access_token_created_at | DATETIME | |
| user_id | INTEGER | Reference to Plato platform user |
| static_ip | TEXT | IP to route via proxy |
| status | TEXT | pending/active/failed/skipped/max_retries_exceeded/max_credential_sets_exceeded |
| last_error | TEXT | Last error message |
| retry_count | INTEGER | |
| created_at | DATETIME | |

### `exited_positions`
| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | |
| position_id | TEXT | Composite key from live position |
| broker | TEXT | |
| client_id | TEXT | |
| symbol | TEXT | |
| token | TEXT | Broker instrument token |
| exchange | TEXT | NSE/BSE/NFO/BFO |
| product_type | TEXT | INTRADAY/CARRYFORWARD/CNC/etc |
| order_type | TEXT | |
| transaction_type | TEXT | BUY/SELL |
| quantity | INTEGER | |
| avg_price | REAL | Entry price |
| exit_price | REAL | LTP at time of exit |
| pnl | REAL | |
| exited_at | DATETIME | localtime |

**Also exists but NOT actively used**: `positions`, `exit_history`, `system_logs` (created by migration 001, no active code writes to them).

---

## 13. Dependencies

```
github.com/google/uuid v1.6.0            -- order tags / unique IDs for orders
github.com/gorilla/websocket v1.5.3      -- Flattrade WebSocket (reference, not used in production)
github.com/jmoiron/sqlx v1.4.0           -- SQL query helpers
github.com/mattn/go-sqlite3 v1.14.22     -- CGO SQLite (used in Dockerfile build stage)
github.com/pquerna/otp v1.5.0            -- TOTP generation for broker 2FA
golang.org/x/crypto v0.55.0             -- bcrypt for admin password hashing
modernc.org/sqlite v1.56.0              -- Pure-Python SQLite (used at runtime; no CGO needed for local builds)
```

---

## 14. Logging System

`logger/logger.py` provides named loggers:

| Logger | Service Name | Used For |
|---|---|---|
| `logger.Auth` | `drmt.auth` | Credential init, token generation |
| `logger.Exit` | `drmt.exit_order_flow` | Order placement, exit results |
| `logger.Monitor` | `drmt.order_monitoring` | Position manager events |
| `logger.System` | `drmt.system_startup` | Server boot, config |
| `logger.Health` | `drmt.health_monitor` | Server health pings |

Each logger:
- Writes to `stdout` in Python-style format: `2026-01-01 12:00:00 | INFO     | file.py:123 | Message`
- Writes to hourly rotating files at `data/logs/<service_name>/app_YYYY-MM-DD_HH.log` (errors -> `error_*.log`)
- Keeps last 100 entries in an in-memory ring buffer (thread-safe via `sync.RWMutex`)

The `/api/logs?type=orders|auth|system` API returns this in-memory buffer to the frontend's ActionLogWidget.

---

## 15. Known Issues, TODOs & Incomplete Features

1. **`FlattradeWSAdapter` exists but is unused** — `main.py` uses `FlattradeAdapter` (HTTP polling). The WS adapter in `position/flattrade_ws.py` is functional but was replaced.

2. **`broker/` and `events/` are empty** — placeholder packages with no code.

3. **Dropbox integration is bypassed** — `credentials/dropbox.py` + `credentials/parser.py` exist but `init.py` reads directly from SQLite. Dead code in the current flow.

4. **`positions`, `exit_history`, `system_logs` tables are unused** — created by migration 001 but no active code writes to them.

5. **Bell notification button is a placeholder** — the bell icon in the header has no functionality.

6. **"Pending Exit" tab is commented out** — exists in `app.jsx` JSX comments (lines 798-803) but is disabled.

7. **LTP (Last Traded Price) is always 0** — position adapters do not fetch real-time LTP. The Running tab column always shows 0. The exit handler falls back to `avg_price` when LTP=0.

8. **`executeAliceBlueAPI()` in `engine.py` is dead code** — `executeAliceBlueWeb()` is the active implementation (lines 400-489 are unused).

9. **`HTTPMaxRetries = 5` and `HTTPRetryBaseDelayMs = 1000` are defined but never used** — no retry loop uses these constants.

10. **Test dummy positions** in `position/mstock.py` (lines 122-165) are commented out but still present.

11. **`TEST_MODE_FORCE_POLL = false`** hardcoded flag in `app.jsx` Dashboard — set to `true` to bypass market hours check during development.

12. **MStock instruments cache failure** — if `LoadMStockInstruments()` fails at startup, all MStock exits will fail with "instrument not found in cache". No retry/fallback exists.

13. **Export CSV button is empty** — the export button in Executed tab (app.jsx line 1099-1106) has no visible label text; it's an invisible but clickable button.

14. **No multi-user support** — only one admin user. The `users` table supports it structurally, but the UI assumes a single admin.

---

## 16. Critical Files and How Everything Is Connected

```
embed.py
  +-- embeds web/* and migrations/* into the binary at compile time

main.py/main.py
  +-- wires all packages together
  +-- registers all HTTP routes on mux

models/models.py
  +-- shared data types used by ALL packages

config/config.py + constants.py
  +-- read by ALL packages for URLs, limits, broker names

persistence/db.py
  +-- called once at boot; *sqlx.DB passed to api/, credentials/, execution/, position/

auth/auth.py
  +-- called by api/handlers.py (login/logout) and api/middleware.py (validate)

credentials/
  +-- init.py -> dedup.py -> [aliceblue|flattrade|mstock].py -> GenerateToken()
  +-- refresh.py -> called by ALL position adapters and engine on auth errors
  +-- httpclient.py -> RESTProxyTransport used by credential generators + engine + MStock/Flattrade adapters

position/
  +-- manager.py -> central data store; passed to api/handlers.py and api/exit.py
  +-- [mstock|aliceblue|flattrade].py -> push positions to manager.Feed
  +-- adapter.py -> BrokerAdapter interface (all three implement it)

execution/engine.py
  +-- reads credentials from DB per position
  +-- uses httpclient.py for proxy routing
  +-- called by api/exit.py

logger/logger.py
  +-- global instances: Auth, Exit, Monitor, System, Health
  +-- read by api/logs.py for /api/logs endpoint

web/app.jsx
  +-- talks ONLY to /api/* endpoints
  +-- no direct broker access from browser
```

---

## 17. How to Run and Safely Modify

### Running Locally (Development)

```bash
cd /opt/plato/plato/drmt_v2

# Option 1: Run pre-built binary directly
./drmt

# Option 2: Build and run from source
./drmt_server

# Option 3: Docker
docker-compose up --build
```

Server starts on **port 8000**. Visit `http://localhost:3100`. Default credentials: `admin / admin`.

### Building for Production

```bash
# Linux

# Windows cross-compile
```

The Dockerfile uses CGO (`github.com/mattn/go-sqlite3`) for the build stage. For local builds without Docker, no CGO is needed — `modernc.org/sqlite` (pure Python) is used at runtime.

### Adding a New Broker

1. Add broker name constant to `config/constants.py`.
2. Add API URL constants to `constants.py`.
3. Create `credentials/<broker>.py` implementing `TokenGenerator` interface (`GenerateToken`, `ValidateToken`, `BrokerName`).
4. Register the generator in `credentials/init.py` -> `NewInitializer()`.
5. Create `position/<broker>.py` with a polling adapter implementing `BrokerAdapter` interface.
6. Wire the adapter in `main.py/main.py`.
7. Add exit logic to `execution/engine.py` -> `ExecuteExit()` switch case.

### Adding a New API Endpoint

1. Create handler function in `api/` (new file or extend existing).
2. Register the route in `main.py/main.py` with `mux.HandleFunc(...)`.
3. Wrap with `api.AuthMiddleware(db, handler)` if authentication is required.

### Modifying the Frontend

1. Edit `web/app.jsx` directly — no build step needed.
2. Bump the version query param in `web/index.html` (e.g., `app.jsx?v=14`) to bust browser cache.
3. The Python binary embeds the `web/` directory at compile time — rebuild the binary to pick up frontend changes.

### Adding Credentials

Insert directly into the SQLite database (`data/drmt.db`):

```sql
INSERT INTO credentials (
    broker, api_key, api_secret, fingerprint, client_id,
    password, totp_secret, app_code, user_id, static_ip, status
) VALUES (
    'MSTOCK', '<api_key>', '<api_secret>', '<unique_fingerprint>', '<client_id>',
    '<password>', '<totp_secret>', '', 1, '', 'pending'
);
```

The `fingerprint` must be unique (used as the upsert key). Restart DRMT to initialize the new credential.

### Logs

- Console: `stdout` (see `server.log` for recent runs)
- Files: `data/logs/<service>/app_YYYY-MM-DD_HH.log`
- In-browser: ActionLogWidget shows last 100 entries from each logger category

---

## 18. Security Notes

- Broker passwords are stored **in plaintext** in the SQLite `credentials` table. Known limitation.
- The default admin password is `admin` — **change it** via direct DB update before any real deployment:
  ```sql
  UPDATE users SET password_hash = '<bcrypt_hash>' WHERE username = 'admin';
  ```
- The `drmt_session` cookie is `HttpOnly` + `SameSite=Strict` — protected against XSS/CSRF.
- The Dropbox tokens in `config.py` are hardcoded defaults — rotate and store in `.env` instead.
- Broker `api_secret` and `totp_secret` are stored unencrypted in SQLite.

---

*Generated by Antigravity on 2026-09-03 from full source analysis of `/opt/plato/plato/drmt_v2`.*
