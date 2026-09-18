# DRMT V2 Migration Plan: Go → Python (asyncio) — ULTIMATE EDITION

> **Status:** Fully Verified · Includes Core Engine Architecture & 100% Go Feature Parity

---

## 1. Executive Summary

This plan migrates DRMT V2 from Go 1.25 to Python 3.11+ using `asyncio` + `aiohttp.web` (the Chronos Engine pattern). 
The entire architecture is built around a **central Core Engine** that strictly manages a shared worker pool, database connections, global rate limits, and semaphores, eliminating all unmanaged tasks and schedulers.

> **Chronos Engine Integration:** Position streaming uses persistent WebSockets (Zombie WebSocket pattern) with a `while True` polling loop, diff-before-push comparison, and Redis cache to survive frontend refreshes.

---

## 2. Async Core Engine & Concurrency Architecture

**Core Engine (`core/engine.py`) is the centralized master component.** No endpoint, DB call, or background task can bypass it. 

### 2.1 Master Worker Pool (No Scheduler)
*   **Architecture:** All critical system/API requests must be asynchronous and executed through the Core Engine's `asyncio.Queue` worker pool.
*   **No Scheduler:** API requests use a pooling mechanism. When an API request finishes, the next eligible user request starts immediately—no tick-based delay (`asyncio.sleep` is banned for orchestration). Only rate-limits, semaphores, or resource availability cause waiting.
*   **HTTP Server Integration:** The `aiohttp.web` server (NOT FastAPI) uses the exact same worker/thread pool managed by the Core Engine. Endpoints push tasks to the Core Engine and `await` the response. `aiohttp.web.Application` is used as the ASGI/HTTP server, with `aiohttp.web.WebSocketResponse` handling all WebSocket connections.
*   **Backpressure:** Bounded queues prevent uncontrolled threads/tasks/connections.

### 2.2 Centralized Rate Limiting & Semaphores
*   **Rate-Limiting Middleware:** A dedicated Master Middleware intercepts all incoming requests.
*   **Global Per-User Limits:** Implements global rate limiting per user with concurrency-safe state (Token Bucket + `asyncio.Lock`) and appropriate async delays.
*   **Strict Semaphores:** All external broker APIs must acquire their respective `asyncio.Semaphore` managed by the Core Engine. Resources must always be safely released via `try/finally` or `async with`.

### 2.3 Database Pooling
*   **DB Pool:** Database calls must run through a dedicated `aiosqlite` DB connection pool managed by the Core Engine, strictly without blocking the async event loop.
*   **Redis Cache:** A `redis.asyncio` connection pool managed by the Core Engine. Stores the last known position snapshot per user. On frontend reconnect/refresh, serves cached data immediately before the first broker poll completes.

### 2.4 Orchestration & Graceful Shutdown
*   **Configuration:** Supports worker size, concurrency, per-user rate limits, API/DB pool limits, and timeouts.
*   **Lifecycle:** The Core Engine centrally manages startup, error handling, cancellation handling, resource cleanup, logging/metrics, and graceful shutdown (intercepting SIGINT/TERM).

---

## 3. Implementation Plan (2 Phases)

### Phase 1 — Core Infrastructure, Data Layer & Broker Auth
*All foundational requirements: database, security, master orchestration, and establishing secure broker sessions.*

**1.1 Data Layer & Models**
*   **DB Setup:** `aiosqlite` with WAL mode and `PRAGMA foreign_keys=ON` running inside the Core Engine's DB Pool.
*   **Migrations:** Run `001_init.sql`, then `002_credentials_extended.sql` (wrapped in `try/except` to gracefully ignore "duplicate column name" errors).
*   **Pydantic Models:** `User`, `Session`, `Credential` (excluding passwords/secrets from JSON), `Position`, `ExitHistory`, `SystemLog`, `ExitedPosition`.

**1.2 Security & Master Middlewares**
*   **Auth Middleware:** Validates the `drmt_session` cookie. Cookie MUST be set with `HttpOnly` and `SameSite=Strict`. Logout MUST explicitly set `MaxAge=-1`.
*   **Rate-Limiting Middleware:** Intercepts every request and strictly enforces the Core Engine's per-user limits.
*   **Crypto Helpers (`credentials/crypto.py`):**
    *   `bcrypt` (cost=14), MD5, SHA256.
    *   `pyotp.TOTP(secret).now()`
    *   **AliceBlue AES-CBC Password Encryption:**
        ```python
        def encrypt_password_alice(password: str, enc_key: str) -> str:
            salt = os.urandom(8)
            key, iv = get_cryptojs_key_iv(enc_key, salt) # MD5 loop to 48 bytes
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return base64.b64encode(b"Salted__" + salt + cipher.encrypt(pad...)).decode()
        ```

**1.3 Token Generation & Dedup (`credentials/`)**
*   **Proxy Client:** Wraps `aiohttp` to send `{method, url, source_ip, headers, json}` to `DRMT_PROXY_URL`. Strips `X-Static-IP` before forwarding to the broker.
*   **MStock (2-step login):** Strips spaces from TOTP (`cred.totp_secret.replace(" ", "")`). Extracts `refreshToken` -> sends to `verifytotp` -> extracts `jwtToken`.
*   **Flattrade (SHA256):** Hashes password via `sha256`. Token extracted from `quickauthresp.susertoken`. Validates successfully even if response is `"no data"`.
*   **AliceBlue (6-step login):** Requires special Auth header `"Bearer <client_id> WEB <temp_token>"`. Final checkSum: `sha256(client_id + auth_code + api_secret)`.
*   **Dedup Engine:** Groups by `broker : client_id : static_ip`. Uses UPSERT SQL. MUST explicitly mark all other credentials in the group as `"skipped"` when saving a working credential.

---

**1.4 Utilities & Cross-Cutting Concerns (`utils/`)**

*   **Async Error Email (`utils/email.py`):**
    *   A single reusable `async def send_error_email(subject, body)` function.
    *   Uses `aiohttp.ClientSession` to call an email API (e.g., SendGrid or Mailgun) asynchronously.
    *   MUST NOT block the main application flow. Called via `asyncio.create_task(send_error_email(...))` so errors are reported without delaying response.
    *   Integrated into the logger: every ERROR-level log entry automatically fires this function.
    *   Config: `EMAIL_API_KEY`, `EMAIL_TO`, `EMAIL_FROM` in `.env`.

*   **CSV Access with asyncio.Lock (`utils/csv_helper.py`):**
    *   A module-level `_csv_lock = asyncio.Lock()` prevents concurrent CSV reads.
    *   All CSV reads use `async with _csv_lock:` to ensure thread-safe access.
    *   Lock is always released — even on exceptions — because `async with` guarantees this.
    *   Uses `aiofiles` for non-blocking file I/O.


### Phase 2 — Trading Engine, REST API, Observability & Orchestration
*The integration and delivery phase: handling active positions, order dispatch, API routing, health monitoring, and finalizing the app lifecycle.*

**2.1 Async Position Adapters — Chronos Engine Pattern (`position/`)**
*   **Chronos `while True` Polling Loop (per user):** Each authenticated user has one dedicated async task spawned by the Core Engine. The loop polls the broker, sends data to the WebSocket, then sleeps 1 second:
    ```python
    async def _polling_loop(user_id, ws, cred):
        await asyncio.sleep(random.uniform(0, 1.0))  # Thundering-Herd Jitter
        last_snapshot = await redis.get(f"positions:{user_id}")  # Stale Cache Fix
        if last_snapshot:
            await ws.send_json({"type": "positions", "data": last_snapshot})
        while True:
            try:
                new_data = await broker_poll(cred)   # calls MStock / AliceBlue / Flattrade
                if new_data != last_snapshot:        # Diff Before Push
                    await asyncio.wait_for(          # Backpressure guard
                        ws.send_json({"type": "positions", "data": new_data}),
                        timeout=3.0
                    )
                    await redis.set(f"positions:{user_id}", new_data)
                    last_snapshot = new_data
            except WebSocketDisconnect:
                break                                # Zombie WebSocket: clean exit
            except Exception:
                pass                                 # Glass Jaw: never crash the loop
            await asyncio.sleep(1)                   # 1s sleep after every poll
    ```
*   **Non-Overlapping:** By design, `await broker_poll()` followed by `await asyncio.sleep(1)` means two polls structurally cannot overlap for the same user.
*   **Zombie WebSocket Detection:** A 30-second heartbeat check. If no ping from the frontend within 30s, the loop exits and the asyncio task is cancelled and cleaned up.
*   **Auto-Refresh:** On 401/403, trigger `refresh_token()` via Core Engine (3 retries, 2s/4s backoff).
*   **AliceBlue EC920:** Hardcoded to return silently (means no positions, NOT a token error).
*   **Flattrade Dual-Schema Fallback:** If struct unmarshal fails, check dict schema for `stat == "Not_Ok"` before triggering refresh.
*   **Position Manager Rules:**
    *   **ID Gen:** Generate missing IDs as `f"{pos.broker}:{pos.client_id}:{pos.symbol}"`.
    *   **Zero-Qty:** If `quantity == 0`, actively delete from the in-memory dictionary.
    *   Requires `get_by_client()` sorting logic.

**2.2 Execution Engine (Mass Exit)**
*   **MStock Instruments:** Caches `OpenAPIScripMaster` on boot, keeping EQ over non-EQ duplicates.
*   **Order Throttling:** Every exit order MUST acquire the Core Engine `Semaphore(20)`.
*   **MStock Exit (via Proxy):** Maps product types (NFO/BFO/NCR → `CARRYFORWARD`). Truncates `ordertag` to `uuid[:15]`.
*   **AliceBlue Web Exit (Direct):** Uses hardcoded `deviceNumber: "57a9f9b93ffbd16d3e4be86448a7a472"`.
*   **Flattrade Exit (Direct):** Generates `uuid4()` for `app_inst_id` and `remarks`.

**2.3 HTTP & WebSocket Routes (`api/`) — `aiohttp.web` (NOT FastAPI)**
*   All routes are `aiohttp.web` handlers (not FastAPI). Endpoints do NOT execute async logic directly; they push tasks to the Core Engine.
*   **WebSocket Route (`GET /ws/positions`):** Authenticates the connection using the `drmt_session` cookie, then calls `core_engine.spawn_user()` to start the Chronos `while True` polling loop for that user.
*   **Unique User Management (Single WS per user):** The Core Engine MUST maintain a `dict[user_id -> asyncio.Task]`. If the same user opens a new WebSocket, the previous task is cancelled and cleaned up before starting a fresh one. No duplicate active connections allowed.
*   **Connection Status Push:** On successful WebSocket handshake, the server sends `{"type": "status", "connected": true}`. The React frontend shows a **green** indicator. On disconnect, the frontend shows **red**.
*   **Frontend JS Integration:** Frontend connects via `new WebSocket("/ws/positions")`, listens for `{"type": "positions", "data": [...]}` messages to update the table. Sends `{"type": "ping"}` every 15s to prevent Zombie WebSocket cleanup.
*   **Auto-Reconnect with Backoff:** If the WebSocket connection drops, the frontend automatically retries using exponential backoff: 1s → 2s → 4s → 8s → max 30s. Status indicator turns **red** during disconnect and **green** after successful reconnect.
*   **Credential Refresh Button:** A "Refresh Credentials" button on the dashboard triggers `POST /api/credentials/refresh`. The backend re-runs the Dedup Engine + token validation for all credentials in SQLite — no app restart required.
*   **Redis Cache on Reconnect:** When the frontend reconnects, the server immediately pushes the cached position data from Redis before the first broker poll completes, eliminating the blank-screen flash.
*   **Exit Handler Fallbacks:**
    *   `exit_price = pos.ltp if pos.ltp != 0 else pos.avg_price`
    *   `tran_type = "BUY" if pos.quantity > 0 else "SELL"` (if missing)
*   **Exited Filter Rule:** If `broker` or `client_id` filter is exactly `"ALL"`, skip applying the SQL filter. Records exits in IST (`localtime`).

**2.4 Observability**
*   **Logger:** 5 async service loggers. Prepends history (index `[0]`), async writes to `app_` and `error_` log files.
*   **Health Monitor:** ServerStatus requires 5 fields. Truncates server response bodies to 150 chars. Pings immediately on boot via Core Engine task.

**2.5 Final Orchestration & Validation (`main.py`)**
*   `aiohttp.web` Application startup: Boots `CoreEngine` -> Initializes DB Pool -> Initializes Redis Pool -> Seeds Admin User -> Registers Route Handlers.
*   Serve React SPA static files via `aiohttp.web.static()` (replaces `FastAPI.StaticFiles`).
*   On shutdown: cancel all per-user polling tasks, close `aiohttp.ClientSession`, flush Redis, close DB pool.
*   **Validation:** Ensure full market-hours test passes and rollback process (docker stop -> run Go binary) completes in < 30s.


## 4. Concurrency PoC Reference
(Archived from `first_poc.py`)
*   **Target:** `https://ifconfig.me/ip`
*   **Tasks:** 1,000 total
*   **Mechanisms Validated:** 
    *   `asyncio.Semaphore(20)` for task-level mass exit throttling (protects broker limits).
    *   `aiohttp.TCPConnector(limit=50, limit_per_host=10)` for global connection capping.
    *   `try/except aiohttp errors` to prevent event-loop crashes on timeout.
*   **Reasoning:** PoC proved 50 concurrent connections are stable. Divided by 3 brokers = ~10-15 per host. Mass exit restricted to 20 concurrent to protect against order placement rate-limits.

---

## 5. Complete Feature Parity Checklist

| Feature | Go | Python | Status |
| :--- | :--- | :--- | :--- |
| MStock polling (proxy) | ✅ | ✅ | Included |
| AliceBlue polling (direct) | ✅ | ✅ | Included |
| AliceBlue EC920 silent skip | ✅ | ✅ | Included |
| Flattrade polling (proxy POST) | ✅ | ✅ | Included |
| Flattrade dual-schema fallback | ✅ | ✅ | Included |
| Position auto-ID generation | ✅ | ✅ | Included |
| Qty==0 → delete position | ✅ | ✅ | Included |
| `get_by_client()` | ✅ | ✅ | Included |
| Mass exit via gather | ✅ | ✅ | Included |
| exit_price fallback to avg_price | ✅ | ✅ | Included |
| transaction_type fallback | ✅ | ✅ | Included |
| `exited_at = datetime('now','localtime')` | ✅ | ✅ | Included |
| Exited filter: broker/client "ALL" skip | ✅ | ✅ | Included |
| Token auto-refresh on 401 | ✅ | ✅ | Included |
| Token refresh 3-retry backoff (2s, 4s) | ✅ | ✅ | Included |
| Refresh: UPDATE by fingerprint | ✅ | ✅ | Included |
| MStock 2-step login (refreshToken→jwtToken) | ✅ | ✅ | Included |
| MStock TOTP secret space-strip | ✅ | ✅ | Included |
| Flattrade SHA256 password | ✅ | ✅ | Included |
| Flattrade login token: `quickauthresp.susertoken` | ✅ | ✅ | Included |
| Flattrade validate "no data" → True | ✅ | ✅ | Included |
| AliceBlue 6-step login | ✅ | ✅ | Included |
| AliceBlue AES-CBC password encrypt | ✅ | ✅ | Included |
| AliceBlue TOTP auth header format | ✅ | ✅ | Included |
| AliceBlue checksum = SHA256(id+code+secret) | ✅ | ✅ | Included |
| DedupEngine group key includes static_ip | ✅ | ✅ | Included |
| DedupEngine save_working: others→skipped | ✅ | ✅ | Included |
| DedupEngine UPSERT by fingerprint | ✅ | ✅ | Included |
| MStock instruments EQ-dedup | ✅ | ✅ | Included |
| MStock product type mapping (NFO/BFO/NCR) | ✅ | ✅ | Included |
| MStock order `ordertag` = uuid[:15] | ✅ | ✅ | Included |
| AliceBlue `deviceNumber` hardcoded | ✅ | ✅ | Included |
| Proxy transport full schema | ✅ | ✅ | Included |
| X-Static-IP stripped from proxy headers | ✅ | ✅ | Included |
| Health: ping immediately on start | ✅ | ✅ | Included |
| Health: 5 fields in ServerStatus | ✅ | ✅ | Included |
| Health: body truncated to 150 chars | ✅ | ✅ | Included |
| Logger: newest-first history | ✅ | ✅ | Included |
| Logger: error_ prefix for ERROR files | ✅ | ✅ | Included |
| Session cookie HttpOnly+SameSite=Strict | ✅ | ✅ | Included |
| Logout: cookie MaxAge=-1 | ✅ | ✅ | Included |
| DB migration 002: ignore duplicate column | ✅ | ✅ | Included |
| `os.makedirs("data")` on boot | ✅ | ✅ | Included |
| WS connection status (green/red) | ➕ New | ✅ | Added |
| Auto-reconnect with backoff | ➕ New | ✅ | Added |
| Unique user / single WS per user | ➕ New | ✅ | Added |
| Credential Refresh Button (frontend) | ➕ New | ✅ | Added |
| Async error email notifications | ➕ New | ✅ | Added |
| asyncio.Lock for CSV reads | ➕ New | ✅ | Added |

---

## 6. Dead Code — Do NOT Port
*   `executeAliceBlueAPI()` (uses deprecated `a3` subdomain).
*   `position/flattrade_ws.go` (WebSocket polling abandoned for HTTP).
*   `credentials/dropbox.go` & `parser.go` (SQLite is the only source of truth).
*   `internal/broker/` & `internal/events/` (empty packages).
