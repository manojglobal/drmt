const { useState, useEffect, useMemo, useRef } = React;

/* ─── Utility: fetch with timeout ─── */
const fetchWithTimeout = async (resource, options = {}) => {
    const { timeout = 30000 } = options;
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);
    try {
        const response = await fetch(resource, { ...options, signal: controller.signal });
        clearTimeout(id);
        return response;
    } catch (error) {
        clearTimeout(id);
        throw error;
    }
};

/* ─── Hook: debounce ─── */
function useDebounce(value, delay) {
    const [debouncedValue, setDebouncedValue] = useState(value);
    useEffect(() => {
        const handler = setTimeout(() => setDebouncedValue(value), delay);
        return () => clearTimeout(handler);
    }, [value, delay]);
    return debouncedValue;
}

/* ═══════════════════════════════════════════
   ROOT APP – auth gate
   ═══════════════════════════════════════════ */
function App() {
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        fetchWithTimeout('/api/me', { timeout: 30000 })
            .then(res => {
                if (res.ok) return res.json();
                throw new Error('Not authenticated');
            })
            .then(data => setUser(data))
            .catch(() => setUser(null))
            .finally(() => setLoading(false));
    }, []);

    if (loading) {
        return (
            <div className="loading-screen">
                <div className="loading-spinner"></div>
                <div>Loading Disaster Recovery Management Tool...</div>
            </div>
        );
    }

    return user ? <Dashboard user={user} onLogout={() => setUser(null)} /> : <Login onLogin={setUser} />;
}

/* ═══════════════════════════════════════════
   LOGIN PAGE
   ═══════════════════════════════════════════ */
function Login({ onLogin }) {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [submitting, setSubmitting] = useState(false);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');
        setSubmitting(true);
        try {
            const res = await fetchWithTimeout('/api/login', {
                timeout: 30000,
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            if (res.ok) {
                const meRes = await fetchWithTimeout('/api/me', { timeout: 30000 });
                const userData = await meRes.json();
                onLogin(userData);
            } else {
                setError('Invalid username or password');
            }
        } catch (err) {
            setError('Unable to connect to server');
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <div className="login-container">
            <div className="login-box" data-aos="fade-up">
                <div className="login-header">
                    <div className="brand-dot" style={{ backgroundColor: "var(--danger)", boxShadow: "0 0 8px var(--danger)" }}></div>
                    <h2>DRMT</h2>
                </div>
                <p className="login-subtitle">Sign in to access position monitor & emergency controls</p>
                {error && <div className="error-alert">{error}</div>}
                <form onSubmit={handleSubmit}>
                    <div className="form-group">
                        <label>Username</label>
                        <input
                            type="text"
                            placeholder="Enter username"
                            value={username}
                            onChange={e => setUsername(e.target.value)}
                            required
                        />
                    </div>
                    <div className="form-group">
                        <label>Password</label>
                        <input
                            type="password"
                            placeholder="Enter password"
                            value={password}
                            onChange={e => setPassword(e.target.value)}
                            required
                        />
                    </div>
                    <button type="submit" disabled={submitting} className="btn-primary">
                        {submitting ? 'Authenticating...' : 'Sign In'}
                    </button>
                </form>
            </div>
        </div>
    );
}

/* ═══════════════════════════════════════════
   SERVER HEALTH MONITOR  (nav pills)
   ═══════════════════════════════════════════ */
const ServerHealthMonitor = () => {
    const [servers, setServers] = React.useState({});

    React.useEffect(() => {
        const fetchHealth = () => {
            fetchWithTimeout('/api/health', {
                timeout: 30000,
                headers: { 'Authorization': `Bearer ${localStorage.getItem('drmt_token')}` }
            })
                .then(res => res.json())
                .then(data => { if (data) setServers(data); })
                .catch(err => console.error("Health check error:", err));
        };
        fetchHealth();
        const interval = setInterval(fetchHealth, 5000);
        return () => clearInterval(interval);
    }, []);

    const serverList = Object.entries(servers).slice(0, 3);
    if (serverList.length === 0) return null;

    return (
        <div className="health-monitor-group">
            {serverList.map(([name, data]) => {
                const isUp = data.status === 'UP';
                const pingTime = data.query_time ? data.query_time.split(' ')[1] : '--:--:--';
                const successTime = data.last_successful_time ? data.last_successful_time.split(' ')[1] : '--:--:--';
                return (
                    <div key={name} className={`health-pill ${isUp ? 'status-up' : 'status-down'}`} title={data.message || 'Status OK'}>
                        <span className="health-dot" />
                        <div className="health-details">
                            <span className="health-name">{name}</span>
                            <span className="health-times">Ping: {pingTime} | OK: {successTime}</span>
                        </div>
                    </div>
                );
            })}
        </div>
    );
};

/* ═══════════════════════════════════════════
   ACTION LOG WIDGET  (fixed, hover-expand)
   ═══════════════════════════════════════════ */
const ActionLogWidget = () => {
    const [activeTab, setActiveTab] = React.useState('orders');
    const [logs, setLogs] = React.useState([]);

    React.useEffect(() => {
        const fetchLogs = () => {
            fetchWithTimeout(`/api/logs?type=${activeTab}`, {
                timeout: 10000,
                headers: { 'Authorization': `Bearer ${localStorage.getItem('drmt_token')}` }
            })
                .then(res => res.json())
                .then(data => { if (data) setLogs(data); })
                .catch(() => { });
        };
        fetchLogs();
        const interval = setInterval(fetchLogs, 3000);
        return () => clearInterval(interval);
    }, [activeTab]);

    const getLogIcon = (level, msg) => {
        const m = msg.toLowerCase();
        if (level === 'ERROR' || m.includes('fail') || m.includes('error')) return '🔴';
        if (m.includes('success') || m.includes('placed') || m.includes('verified')) return '🟢';
        if (m.includes('in progress') || m.includes('dispatch') || m.includes('starting') || m.includes('booting')) return '🔄';
        if (level === 'WARN') return '⚠️';
        return 'ℹ️';
    };

    const [isOpen, setIsOpen] = React.useState(false);

    return (
        <div className="action-log-widget">
            <button className="action-log-toggle" onClick={() => setIsOpen(!isOpen)} title="Terminal log ">
                <span style={{ fontFamily: 'monospace', fontWeight: 'bold' }}>&gt;_</span>
                <span>Terminal</span>
                <span className="live-dot" style={{ marginLeft: '4px', background: 'var(--green)', boxShadow: '0 0 5px var(--green)' }} />
            </button>

            {isOpen && (
                <div className="action-log-dropdown">
                    <div className="action-log-header">
                        <div className="action-log-title">
                            Action Log
                        </div>
                        <div className="live-indicator">
                            <span className="live-dot" /> LIVE
                        </div>
                    </div>
                    <div className="action-log-tabs">
                        <button className={`action-log-tab ${activeTab === 'orders' ? 'active' : ''}`} onClick={() => setActiveTab('orders')}>Orders</button>
                        <button className={`action-log-tab ${activeTab === 'auth' ? 'active' : ''}`} onClick={() => setActiveTab('auth')}>Auth</button>
                        <button className={`action-log-tab ${activeTab === 'system' ? 'active' : ''}`} onClick={() => setActiveTab('system')}>System</button>
                    </div>
                    <div className="action-log-content">
                        {logs.length === 0 ? (
                            <div style={{ padding: '16px', textAlign: 'center', color: 'var(--text-dim)', fontSize: '11px' }}>
                                No recent activity
                            </div>
                        ) : (
                            logs.map((log, i) => (
                                <div key={i} className="log-entry">
                                    <div className="log-icon">{getLogIcon(log.level, log.message)}</div>
                                    <div className="log-details">
                                        <div className="log-message">{log.message}</div>
                                        <div className="log-meta">{log.timestamp}</div>
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                </div>
            )}
        </div>
    );
};


/* ═══════════════════════════════════════════
   MAIN DASHBOARD
   ═══════════════════════════════════════════ */
const CLIENT_NAME_MAP = {
    'P645M1': 'Manoj mStock Demo',
    'P645M2': 'Jagdeesh Sawant',
    'P645M6': 'Rinku Singh'
};

const getDisplayUsername = (pos) => {
    if (!pos) return '';
    return pos.username || CLIENT_NAME_MAP[pos.client_id] || pos.client_id || pos.user_id || '';
};

function Dashboard({ user, onLogout }) {
    // CODE FLIP FOR TESTING: Change to 'true' to force 5s polling regardless of market hours or tab visibility
    const TEST_MODE_FORCE_POLL = true;

    // const [isVisible, setIsVisible]       = useState(!document.hidden);
    const [pollingStatus, setPollingStatus] = useState('Active');

    useEffect(() => {
        const handleVisibility = () => setIsVisible(!document.hidden);
        document.addEventListener('visibilitychange', handleVisibility);
        return () => document.removeEventListener('visibilitychange', handleVisibility);
    }, []);

    /* ── Core State ── */
    const [toasts, setToasts] = useState([]);
    const [positions, setPositions] = useState([]);
    const [lastSync, setLastSync] = useState(null);
    const [activeTab, setActiveTab] = useState('running');

    const [isFetchingPositions, setIsFetchingPositions] = useState(false);
    const [isExporting, setIsExporting] = useState(false);
    const [isLoggingOut, setIsLoggingOut] = useState(false);

    /* ── Theme ── */
    const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'dark');
    useEffect(() => {
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
    }, [theme]);

    /* ── Filters (Running) ── */
    const [searchQuery, setSearchQuery] = useState('');
    const debouncedSearchQuery = useDebounce(searchQuery, 500);
    const [selectedBroker, setSelectedBroker] = useState('ALL');
    const [selectedUser, setSelectedUser] = useState('ALL');
    const [selectedTradeType, setSelectedTradeType] = useState('ALL');

    /* ── Selection & Pagination ── */
    const [selectedIds, setSelectedIds] = useState(new Set());
    const [currentPage, setCurrentPage] = useState(1);
    const pageSize = 50;

    /* ── Modal & Exit ── */
    const [modalConfig, setModalConfig] = useState(null);
    const [isExiting, setIsExiting] = useState(false);
    const isActionLocked = isExiting || isExporting || isLoggingOut;

    /* ── Toast helper ── */
    const addToast = (message, type = 'info') => {
        const id = Date.now() + Math.random();
        setToasts(prev => [...prev, { id, message, type }]);
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 5000);
    };

    /* ── Logout ── */
    const handleLogout = async () => {
        await fetch('/api/logout', { method: 'POST' });
        onLogout();
    };

    /* ── WebSocket / Live Positions ── */
    const [wsConnected, setWsConnected] = useState(false);
    const reconnectAttempts = useRef(0);

    useEffect(() => {
        let isClosed = false;
        let isPostMarket = false;

        if (!TEST_MODE_FORCE_POLL) {
            try {
                const istTime = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
                const day = istTime.getDay();
                const mins = istTime.getHours() * 60 + istTime.getMinutes();
                isClosed = (day === 0 || day === 6 || mins < 530 || mins > 1435);
                isPostMarket = (mins >= 960 && mins <= 1435);
            } catch (e) {
                isClosed = false;
            }
        }

        if (isClosed && !TEST_MODE_FORCE_POLL) {
            setPollingStatus("Paused (Market Closed)");
            return;
        }

        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProtocol}//${window.location.host}/ws/positions`;

        let socket = null;
        let pingTimer = null;

        const connect = () => {
            if (isActionLocked) return;
            setPollingStatus("Connecting...");
            socket = new WebSocket(wsUrl);
            socket.binaryType = "arraybuffer";

            socket.onopen = () => {
                setWsConnected(true);
                setPollingStatus("Connected (Live)");
                reconnectAttempts.current = 0;
                pingTimer = setInterval(() => {
                    if (socket && socket.readyState === WebSocket.OPEN) {
                        socket.send(new TextEncoder().encode(JSON.stringify({ type: "ping" })));
                    }
                }, 15000);
            };

            socket.onmessage = (event) => {
                try {
                    let textData;
                    if (typeof event.data === 'string') {
                        textData = event.data;
                    } else {
                        textData = new TextDecoder("utf-8").decode(event.data);
                    }
                    const msg = JSON.parse(textData);
                    if (msg.type === "positions") {
                        const realPos = (msg.data || []).filter(p => p && p.broker && !p.broker.toUpperCase().includes('FAKE'));
                        setPositions(realPos);
                        setLastSync(new Date().toLocaleTimeString());
                    } else if (msg.type === "status") {
                        setWsConnected(msg.connected);
                    } else if (msg.type === "error") {
                        addToast(msg.message || "WebSocket Error", "error");
                    }
                } catch (e) {
                    console.error("WS parse error", e);
                }
            };

            socket.onclose = () => {
                setWsConnected(false);
                setPollingStatus("Disconnected / Connection Lost");
                clearInterval(pingTimer);
                const attempt = reconnectAttempts.current;
                const maxDelay = 30000;
                const delay = Math.min(1000 * Math.pow(2, attempt), maxDelay);
                if (!isActionLocked) {
                    setTimeout(connect, delay);
                    reconnectAttempts.current += 1;
                }
            };

            socket.onerror = (err) => console.error("WS error", err);
        };

        connect();

        return () => {
            if (socket) socket.close();
            if (pingTimer) clearInterval(pingTimer);
        };
    }, [isActionLocked, TEST_MODE_FORCE_POLL]);

    /* ── Dynamic dropdown options (Running) ── */
    const brokersList = useMemo(() => {
        const set = new Set();
        positions.forEach(p => { if (p.broker) set.add(p.broker); });
        return Array.from(set).sort();
    }, [positions]);

    const usersList = useMemo(() => {
        const set = new Set();
        positions.forEach(p => {
            const u = getDisplayUsername(p);
            if (u) set.add(u);
        });
        return Array.from(set).sort();
    }, [positions]);

    /* ── Filtered positions ── */
    const filteredPositions = useMemo(() => {
        return positions.filter(pos => {
            if (selectedBroker !== 'ALL' && pos.broker !== selectedBroker) return false;
            const displayName = getDisplayUsername(pos);
            if (selectedUser !== 'ALL' && displayName !== selectedUser && pos.client_id !== selectedUser) return false;
            if (selectedTradeType !== 'ALL') {
                const side = (pos.transaction_type || pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                if (side !== selectedTradeType) return false;
            }
            if (debouncedSearchQuery.trim() !== '') {
                const q = debouncedSearchQuery.toLowerCase().trim();
                const matchSymbol = (pos.symbol || '').toLowerCase().includes(q);
                const matchClient = (displayName + ' ' + (pos.client_id || '')).toLowerCase().includes(q);
                const matchBroker = (pos.broker || '').toLowerCase().includes(q);
                const matchExchange = (pos.exchange || '').toLowerCase().includes(q);
                const matchToken = (pos.token || '').toLowerCase().includes(q);
                if (!matchSymbol && !matchClient && !matchBroker && !matchExchange && !matchToken) return false;
            }
            return true;
        });
    }, [positions, selectedBroker, selectedUser, selectedTradeType, debouncedSearchQuery]);

    /* Reset to page 1 on filter change */
    useEffect(() => { setCurrentPage(1); }, [searchQuery, selectedBroker, selectedUser, selectedTradeType]);

    /* Pagination */
    const totalPages = Math.max(1, Math.ceil(filteredPositions.length / pageSize));
    const paginatedPositions = useMemo(() => {
        const start = (currentPage - 1) * pageSize;
        return filteredPositions.slice(start, start + pageSize);
    }, [filteredPositions, currentPage, pageSize]);

    /* ── Selection handlers ── */
    const handleSelectRow = (id) => {
        const newSet = new Set(selectedIds);
        newSet.has(id) ? newSet.delete(id) : newSet.add(id);
        setSelectedIds(newSet);
    };

    const isAllPageSelected = paginatedPositions.length > 0 && paginatedPositions.every(p => selectedIds.has(p.id));

    const handleSelectAllPage = (e) => {
        const newSet = new Set(selectedIds);
        if (e.target.checked) {
            paginatedPositions.forEach(p => newSet.add(p.id));
        } else {
            paginatedPositions.forEach(p => newSet.delete(p.id));
        }
        setSelectedIds(newSet);
    };

    const handleCancelSelection = () => setSelectedIds(new Set());

    /* ── Close / Close-All ── */
    const handleCloseAction = () => {
        if (selectedIds.size > 0) {
            const count = selectedIds.size;
            setModalConfig({
                title: "Confirm Market Exit",
                message: `Are you sure you want to Market Exit ${count} selected position${count > 1 ? 's' : ''}?\n\nThis will instantly submit Market orders to the respective broker(s) to close these positions.`,
                onConfirm: () => executeExit(positions.filter(p => selectedIds.has(p.id))),
                onCancel: () => setModalConfig(null)
            });
        } else {
            if (filteredPositions.length === 0) {
                addToast("No running positions available to close.", "info");
                return;
            }
            const count = filteredPositions.length;
            setModalConfig({
                title: "Confirm Close All Positions",
                message: `⚠️ WARNING: Are you sure you want to Market Exit ALL ${count} currently displayed position${count > 1 ? 's' : ''}?\n\nThis will immediately place Market orders across all brokers to flatten these positions.`,
                onConfirm: () => executeExit(filteredPositions),
                onCancel: () => setModalConfig(null)
            });
        }
    };

    const executeExit = async (positionsToExit) => {
        setModalConfig(null);
        setIsExiting(true);
        try {
            const res = await fetchWithTimeout('/api/exit', {
                timeout: 30000,
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions: positionsToExit })
            });
            if (res.ok) {
                const data = await res.json();
                setSelectedIds(new Set());
                addToast(data.message || `Processed exit for ${positionsToExit.length} position(s)`, "success");
                if (data.results && data.results.length > 0) {
                    data.results.forEach(r => {
                        if (r.success) {
                            addToast(`✅ ${r.broker} - ${r.symbol}: Placed successfully`, 'success');
                        } else {
                            addToast(`❌ ${r.broker} - ${r.symbol} Failed: ${r.message}`, 'error');
                        }
                    });
                }
            } else {
                addToast("Failed to dispatch exit commands (Server Error).", "error");
            }
        } catch (err) {
            if (err.name === "AbortError") {
                addToast("Execution timed out (30s)!", "error");
            } else {
                addToast("Error communicating with server.", "error");
            }
        } finally {
            setIsExiting(false);
        }
    };

    /* ══════════════════════════════════════
       EXECUTED TAB STATE
       ══════════════════════════════════════ */
    const [executedPositions, setExecutedPositions] = useState([]);
    const [executedLoading, setExecutedLoading] = useState(false);
    const [availableDates, setAvailableDates] = useState([]);

    const todayISO = useMemo(() => new Date().toISOString().split('T')[0], []);
    const defaultFromISO = useMemo(() => {
        const d = new Date();
        d.setDate(d.getDate() - 7);
        return d.toISOString().split('T')[0];
    }, []);

    const [fromDate, setFromDate] = useState(defaultFromISO);
    const [toDate, setToDate] = useState(todayISO);
    const [executedSearchQuery, setExecutedSearchQuery] = useState('');
    const debouncedExecutedSearchQuery = useDebounce(executedSearchQuery, 500);
    const [executedBroker, setExecutedBroker] = useState('ALL');
    const [executedUser, setExecutedUser] = useState('ALL');
    const [executedTradeType, setExecutedTradeType] = useState('ALL');
    const [executedPage, setExecutedPage] = useState(1);

    /* Date dropdown options */
    const dateOptions = useMemo(() => {
        const map = new Map();
        const today = new Date();
        for (let i = 0; i < 30; i++) {
            const d = new Date(today);
            d.setDate(today.getDate() - i);
            const iso = d.toISOString().split('T')[0];
            const label = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
            map.set(iso, i === 0 ? `Today (${label})` : i === 1 ? `Yesterday (${label})` : label);
        }
        availableDates.forEach(dt => { if (!map.has(dt)) map.set(dt, dt); });
        return Array.from(map.entries()).map(([value, label]) => ({ value, label }));
    }, [availableDates]);

    /* Fetch available dates */
    useEffect(() => {
        fetch('/api/exited/dates')
            .then(res => res.ok ? res.json() : [])
            .then(data => { if (Array.isArray(data) && data.length > 0) setAvailableDates(data); })
            .catch(() => { });
    }, []);

    /* Fetch executed positions */
    const fetchExecutedPositions = async () => {
        setExecutedLoading(true);
        try {
            const params = new URLSearchParams();
            if (fromDate) params.append('from_date', fromDate);
            if (toDate) params.append('to_date', toDate);
            if (executedBroker !== 'ALL') params.append('broker', executedBroker);
            if (executedUser !== 'ALL') params.append('client_id', executedUser);

            const res = await fetchWithTimeout(`/api/exited?${params.toString()}`, { timeout: 30000 });
            if (res.ok) {
                const data = await res.json();
                setExecutedPositions((data.data || []).filter(p => p && p.broker && !p.broker.toUpperCase().includes('FAKE')));
            }
        } catch (err) {
            console.error("Failed to fetch executed positions:", err);
            if (err.name === "AbortError") {
                addToast("Fetch executed positions timed out", "error");
            } else {
                addToast("Failed to fetch executed positions", "error");
            }
        } finally {
            setExecutedLoading(false);
        }
    };

    /* Export to CSV */
    const exportToCSV = async () => {
        if (!filteredExecutedPositions || filteredExecutedPositions.length === 0) {
            addToast("No data to export", "warning");
            return;
        }
        setIsExporting(true);
        await new Promise(resolve => setTimeout(resolve, 100));
        try {
            const headers = ["USERID", "BROKER", "INSTRUMENT", "EXCHANGE", "PRODUCT", "BUY/SELL", "QUANTITY", "ENTRY PRICE", "EXIT PRICE", "PNL", "EXITED AT"];
            const csvRows = [headers.join(",")];
            filteredExecutedPositions.forEach(pos => {
                const side = (pos.transaction_type || pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                const exitDateStr = pos.exited_at ? new Date(pos.exited_at).toLocaleString('en-GB') : 'N/A';
                const row = [
                    getDisplayUsername(pos),
                    pos.broker || "",
                    pos.symbol || "",
                    pos.exchange || "",
                    pos.product_type || "",
                    side,
                    pos.quantity || 0,
                    (pos.avg_price || 0).toFixed(2),
                    (pos.exit_price || 0).toFixed(2),
                    (pos.pnl || 0).toFixed(2),
                    `"${exitDateStr}"`
                ];
                csvRows.push(row.join(","));
            });
            const csvString = csvRows.join("\n");
            const blob = new Blob([csvString], { type: "text/csv;charset=utf-8;" });
            const url = URL.createObjectURL(blob);
            const link = document.createElement("a");
            link.setAttribute("href", url);
            link.setAttribute("download", `drmt_executed_orders_${new Date().toISOString().split('T')[0]}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            addToast(`Exported ${filteredExecutedPositions.length} positions to CSV`, "success");
        } finally {
            setIsExporting(false);
        }
    };

    useEffect(() => {
        if (activeTab === 'executed') fetchExecutedPositions();
    }, [activeTab, fromDate, toDate, executedBroker, executedUser]);

    /* Filter executed positions */
    const filteredExecutedPositions = useMemo(() => {
        return executedPositions.filter(pos => {
            if (executedTradeType !== 'ALL') {
                const side = (pos.transaction_type || pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                if (side !== executedTradeType) return false;
            }
            if (debouncedExecutedSearchQuery.trim() !== '') {
                const q = debouncedExecutedSearchQuery.toLowerCase().trim();
                const matchSymbol = (pos.symbol || '').toLowerCase().includes(q);
                const matchClient = (getDisplayUsername(pos) + ' ' + (pos.client_id || '')).toLowerCase().includes(q);
                const matchBroker = (pos.broker || '').toLowerCase().includes(q);
                const matchExchange = (pos.exchange || '').toLowerCase().includes(q);
                const matchToken = (pos.token || '').toLowerCase().includes(q);
                if (!matchSymbol && !matchClient && !matchBroker && !matchExchange && !matchToken) return false;
            }
            return true;
        });
    }, [executedPositions, executedTradeType, debouncedExecutedSearchQuery]);

    useEffect(() => { setExecutedPage(1); }, [executedSearchQuery, executedTradeType, fromDate, toDate, executedBroker, executedUser]);

    const totalExecutedPages = Math.max(1, Math.ceil(filteredExecutedPositions.length / pageSize));
    const paginatedExecutedPositions = useMemo(() => {
        const start = (executedPage - 1) * pageSize;
        return filteredExecutedPositions.slice(start, start + pageSize);
    }, [filteredExecutedPositions, executedPage, pageSize]);

    /* Executed dropdown lists */
    const executedBrokersList = useMemo(() => {
        const set = new Set();
        executedPositions.forEach(p => { if (p.broker) set.add(p.broker); });
        brokersList.forEach(b => set.add(b));
        return Array.from(set).sort();
    }, [executedPositions, brokersList]);

    const executedUsersList = useMemo(() => {
        const set = new Set();
        executedPositions.forEach(p => {
            const u = getDisplayUsername(p);
            if (u) set.add(u);
        });
        usersList.forEach(u => set.add(u));
        return Array.from(set).sort();
    }, [executedPositions, usersList]);

    const adminInitial = (user?.username || 'A').charAt(0).toUpperCase();

    /* ══════════════════════════════════════
       RENDER
       ══════════════════════════════════════ */
    return (
        <div className="app-layout">

            {/* ── Toast Notifications ── */}
            <div className="toast-container">
                {toasts.map(t => (
                    <div key={t.id} className={`toast toast-${t.type}`}>{t.message}</div>
                ))}
            </div>

            {/* ── Confirmation Modal ── */}
            {modalConfig && (
                <div className="modal-overlay">
                    <div className="modal-content" data-aos="zoom-in" data-aos-duration="200">
                        <h3>{modalConfig.title}</h3>
                        <p className="modal-message">{modalConfig.message}</p>
                        <div className="modal-actions">
                            <button onClick={modalConfig.onCancel} className="btn-cancel">Cancel</button>
                            <button onClick={modalConfig.onConfirm} className="btn-confirm-exit">Confirm Exit</button>
                        </div>
                    </div>
                </div>
            )}

            {/* ══════════════════════════════════
                TOP NAVIGATION BAR
                ══════════════════════════════════ */}
            <header className="top-nav">
                <div className="nav-left">
                    <h1 className="brand-title">DRMT<br />Dashboard</h1>

                    <div className="nav-last-sync">
                        <span className="sync-label">Last Sync:</span>
                        <span className="sync-time">{lastSync || 'Waiting...'}</span>
                    </div>

                    <div className={`nav-connection-pill ${wsConnected ? 'connected' : 'disconnected'}`}>
                        <span className="live-dot" />
                        <span className="pill-text">{wsConnected ? 'Connected\n(Live)' : 'Disconnected\n(Lost)'}</span>
                    </div>
                </div>

                <div className="nav-right">
                    <ServerHealthMonitor />

                    <button
                        className="btn-outline"
                        title="Refresh Credentials"
                        onClick={async () => {
                            try {
                                const res = await fetchWithTimeout("/api/credentials/refresh", { method: "POST" });
                                const data = await res.json();
                                addToast(data.message || "Credentials Refreshed", "success");
                            } catch (e) {
                                addToast("Refresh failed", "error");
                            }
                        }}
                    >Refresh</button>

                    <button
                        className="icon-btn"
                        onClick={() => setTheme(t => t === 'dark' ? 'light' : 'dark')}
                        title="Toggle Light/Dark Mode"
                    >
                        {theme === 'dark' ? (
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <circle cx="12" cy="12" r="5" />
                                <line x1="12" y1="1" x2="12" y2="3" />
                                <line x1="12" y1="21" x2="12" y2="23" />
                                <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
                                <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
                                <line x1="1" y1="12" x2="3" y2="12" />
                                <line x1="21" y1="12" x2="23" y2="12" />
                                <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
                                <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
                            </svg>
                        ) : (
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                            </svg>
                        )}
                    </button>

                    <div style={{ position: 'relative', zIndex: 200, height: '36px' }}>
                        <ActionLogWidget />
                    </div>

                    <div className="user-profile" title={`Logged in as ${user.username}`}>
                        <div className="avatar-circle">{adminInitial}</div>
                        <button
                            className="logout-link-btn"
                            disabled={isActionLocked}
                            onClick={async () => { setIsLoggingOut(true); await handleLogout(); setIsLoggingOut(false); }}
                            title="Sign Out"
                        >
                            Sign Out
                        </button>
                    </div>
                </div>
            </header>

            {/* ══════════════════════════════════
                MAIN CONTENT
                ══════════════════════════════════ */}
            <main className="main-content">
                {/* PNL SUMMARY SECTION (Visible in both tabs) */}
                {(() => {
                    const totalPnl = positions.reduce((s, p) => s + (p.pnl || 0), 0);
                    const inProfit = positions.filter(p => (p.pnl || 0) > 0).length;
                    const inLoss = positions.filter(p => (p.pnl || 0) < 0).length;
                    const pnlCls = totalPnl > 0 ? 'positive' : (totalPnl < 0 ? 'negative' : 'neutral');
                    return (
                        <div className="hero-section" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '12px' }}>
                            <div className="stat-card" style={{ backgroundColor: 'var(--bg-panel)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-lg)', padding: '6px 12px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '2px' }}>
                                <span className="stat-label" style={{ fontSize: '11px', fontWeight: 'bold', color: 'var(--text-secondary)' }}>TOTAL PNL</span>
                                <span className={`stat-value tabular ${pnlCls}`} style={{ fontSize: '20px', fontWeight: 'bold' }}>
                                    {totalPnl > 0 ? '+' : ''}{totalPnl.toFixed(2)}
                                </span>
                            </div>
                            <div className="stat-card" style={{ backgroundColor: 'var(--bg-panel)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-lg)', padding: '6px 12px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '2px' }}>
                                <span className="stat-label" style={{ fontSize: '11px', fontWeight: 'bold', color: 'var(--text-secondary)' }}>OPEN POSITIONS</span>
                                <span className="stat-value tabular neutral" style={{ fontSize: '20px', fontWeight: 'bold', color: 'var(--text-primary, #fff)' }}>{positions.length}</span>
                            </div>
                            <div className="stat-card" style={{ backgroundColor: 'var(--bg-panel)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-lg)', padding: '6px 12px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '2px' }}>
                                <span className="stat-label" style={{ fontSize: '11px', fontWeight: 'bold', color: 'var(--text-secondary)' }}>IN PROFIT</span>
                                <span className="stat-value tabular positive" style={{ fontSize: '20px', fontWeight: 'bold', color: 'var(--success, var(--green, #22c55e))' }}>{inProfit}</span>
                            </div>
                            <div className="stat-card" style={{ backgroundColor: 'var(--bg-panel)', border: '1px solid var(--border-color)', borderRadius: 'var(--radius-lg)', padding: '6px 12px', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '2px' }}>
                                <span className="stat-label" style={{ fontSize: '11px', fontWeight: 'bold', color: 'var(--text-secondary)' }}>IN LOSS</span>
                                <span className="stat-value tabular negative" style={{ fontSize: '20px', fontWeight: 'bold', color: 'var(--danger, var(--red, #ef4444))' }}>{inLoss}</span>
                            </div>
                        </div>
                    );
                })()}




                {/* ══ RUNNING TAB ══ */}
                {activeTab === 'running' ? (
                    <>


                        {/* Unified Control Bar */}
                        <div className="control-bar" data-aos="fade-up" data-aos-delay="200">
                            <div className="tabs-group">
                                <button
                                    className={`tab-btn ${activeTab === 'running' ? 'active' : ''}`}
                                    onClick={() => !isActionLocked && setActiveTab('running')}
                                    disabled={isActionLocked}
                                >
                                    Running
                                </button>
                                <button
                                    className={`tab-btn ${activeTab === 'executed' ? 'active' : ''}`}
                                    onClick={() => !isActionLocked && setActiveTab('executed')}
                                    disabled={isActionLocked}
                                >
                                    Executed
                                </button>
                            </div>

                            <div className="filters-group">
                                <div className="search-wrapper">
                                    <svg className="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                        <circle cx="11" cy="11" r="8" />
                                        <line x1="21" y1="21" x2="16.65" y2="16.65" />
                                    </svg>
                                    <input
                                        type="text"
                                        className="filter-input"
                                        placeholder="Search instrument..."
                                        value={searchQuery}
                                        onChange={e => setSearchQuery(e.target.value)}
                                        disabled={isActionLocked}
                                    />
                                </div>

                                <select className="filter-select" value={selectedBroker} onChange={e => setSelectedBroker(e.target.value)} disabled={isActionLocked}>
                                    <option value="ALL">All Brokers</option>
                                    {brokersList.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>

                                <select className="filter-select" value={selectedUser} onChange={e => setSelectedUser(e.target.value)} disabled={isActionLocked}>
                                    <option value="ALL">All Users</option>
                                    {usersList.map(u => <option key={u} value={u}>{u}</option>)}
                                </select>

                                <select className="filter-select" value={selectedTradeType} onChange={e => setSelectedTradeType(e.target.value)} disabled={isActionLocked}>
                                    <option value="ALL">All Trade Types</option>
                                    <option value="BUY">BUY</option>
                                    <option value="SELL">SELL</option>
                                </select>
                            </div>

                            <div className="actions-group">
                                <button
                                    className="btn-close-action"
                                    onClick={handleCloseAction}
                                    disabled={isActionLocked || (filteredPositions.length === 0 && selectedIds.size === 0)}
                                >
                                    {isExiting
                                        ? 'Dispatching...'
                                        : (selectedIds.size > 0 ? `Close Selected (${selectedIds.size})` : 'Close All')
                                    }
                                </button>
                                <button
                                    className="btn-cancel-action"
                                    onClick={handleCancelSelection}
                                    disabled={isActionLocked || selectedIds.size === 0}
                                >
                                    Clear Selection
                                </button>
                            </div>
                        </div>

                        {/* Monitor Container */}
                        <div className="monitor-container" data-aos="fade-up" data-aos-delay="250">
                            <div className="table-meta-bar">
                                <div className="positions-count-text">
                                    Showing <strong>{paginatedPositions.length}</strong> of {filteredPositions.length} positions
                                </div>
                                <div className="pagination-controls">
                                    <button className="page-nav-btn" onClick={() => setCurrentPage(p => Math.max(1, p - 1))} disabled={currentPage <= 1}>&lt;</button>
                                    <span className="page-indicator">Page {currentPage} / {totalPages}</span>
                                    <button className="page-nav-btn" onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))} disabled={currentPage >= totalPages}>&gt;</button>
                                </div>
                            </div>

                            <div className="table-responsive">
                                <table className="compact-table">
                                    <thead>
                                        <tr>
                                            <th className="th-checkbox">
                                                <input type="checkbox" checked={isAllPageSelected} onChange={handleSelectAllPage} />
                                            </th>
                                            <th>User &uarr;</th>
                                            <th>Broker</th>
                                            <th>Instrument</th>
                                            <th>Exchange</th>
                                            <th>Product</th>
                                            <th>Side</th>
                                            <th className="text-right">Qty</th>
                                            <th className="text-right">Entry</th>
                                            <th className="text-right">PnL &darr;</th>
                                            <th className="text-right">LTP</th>
                                            <th className="text-center">Action</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {paginatedPositions.length === 0 ? (
                                            <tr>
                                                <td colSpan="12" className="empty-table-cell">
                                                    {positions.length === 0 ? 'No running positions found.' : 'No positions match filters.'}
                                                </td>
                                            </tr>
                                        ) : (
                                            paginatedPositions.map(pos => {
                                                const isSelected = selectedIds.has(pos.id);
                                                const side = (pos.transaction_type || pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                                                const pnlVal = pos.pnl || 0;
                                                const ltpVal = pos.ltp || pos.avg_price || 0;

                                                return (
                                                    <tr key={pos.id} className={isSelected ? 'row-selected' : ''} onClick={e => {
                                                        if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') handleSelectRow(pos.id);
                                                    }}>
                                                        <td className="td-checkbox" onClick={e => e.stopPropagation()}>
                                                            <input type="checkbox" checked={isSelected} onChange={() => handleSelectRow(pos.id)} />
                                                        </td>
                                                        <td>{getDisplayUsername(pos)}</td>
                                                        <td>{pos.broker}</td>
                                                        <td className="font-semibold">{pos.symbol}</td>
                                                        <td>{pos.exchange}</td>
                                                        <td>{pos.product_type}</td>
                                                        <td>
                                                            <span className={`side-badge ${side === 'BUY' ? 'side-buy' : 'side-sell'}`}>{side}</span>
                                                        </td>
                                                        <td className="text-right tabular">{pos.quantity}</td>
                                                        <td className="text-right tabular">{(pos.avg_price || 0).toFixed(2)}</td>
                                                        <td className={`text-right tabular ${pnlVal >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                                                            {pnlVal >= 0 ? `+${pnlVal.toFixed(2)}` : pnlVal.toFixed(2)}
                                                        </td>
                                                        <td className="text-right tabular">{ltpVal.toFixed(2)}</td>
                                                        <td className="text-center" onClick={e => e.stopPropagation()}>
                                                            <button
                                                                className="btn-action-close"
                                                                disabled={isActionLocked}
                                                                onClick={e => {
                                                                    e.stopPropagation();
                                                                    setModalConfig({
                                                                        title: "Confirm Market Exit",
                                                                        message: `Are you sure you want to Market Exit this position (${pos.symbol})?\n\nThis will instantly submit a Market order to close this position.`,
                                                                        onConfirm: () => executeExit([pos]),
                                                                        onCancel: () => setModalConfig(null)
                                                                    });
                                                                }}
                                                            >
                                                                Close
                                                            </button>
                                                        </td>
                                                    </tr>
                                                );
                                            })
                                        )}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </>
                ) : (
                    /* ══ EXECUTED TAB ══ */
                    <>
                        {/* Unified Control Bar for Executed */}
                        <div className="control-bar" data-aos="fade-up" data-aos-delay="0">
                            <div className="tabs-group">
                                <button
                                    className={`tab-btn ${activeTab === 'running' ? 'active' : ''}`}
                                    onClick={() => !isActionLocked && setActiveTab('running')}
                                    disabled={isActionLocked}
                                >
                                    Running
                                </button>
                                <button
                                    className={`tab-btn ${activeTab === 'executed' ? 'active' : ''}`}
                                    onClick={() => !isActionLocked && setActiveTab('executed')}
                                    disabled={isActionLocked}
                                >
                                    Executed
                                </button>
                            </div>

                            <div className="filters-group">
                                <div className="search-wrapper">
                                    <svg className="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                        <circle cx="11" cy="11" r="8" />
                                        <line x1="21" y1="21" x2="16.65" y2="16.65" />
                                    </svg>
                                    <input
                                        type="text"
                                        className="filter-input"
                                        placeholder="Search executed instrument..."
                                        value={executedSearchQuery}
                                        onChange={e => setExecutedSearchQuery(e.target.value)}
                                        disabled={isActionLocked}
                                    />
                                </div>

                                <div className="date-filter-group">
                                    <label className="filter-label">From:</label>
                                    <select className="filter-select" value={fromDate} onChange={e => setFromDate(e.target.value)}>
                                        {dateOptions.map(opt => <option key={'from_' + opt.value} value={opt.value}>{opt.label}</option>)}
                                    </select>
                                </div>

                                <div className="date-filter-group">
                                    <label className="filter-label">To:</label>
                                    <select className="filter-select" value={toDate} onChange={e => setToDate(e.target.value)}>
                                        {dateOptions.map(opt => <option key={'to_' + opt.value} value={opt.value}>{opt.label}</option>)}
                                    </select>
                                </div>

                                <select className="filter-select" value={executedBroker} onChange={e => setExecutedBroker(e.target.value)} disabled={isActionLocked}>
                                    <option value="ALL">All Brokers</option>
                                    {executedBrokersList.map(b => <option key={b} value={b}>{b}</option>)}
                                </select>

                                <select className="filter-select" value={executedUser} onChange={e => setExecutedUser(e.target.value)} disabled={isActionLocked}>
                                    <option value="ALL">All Users</option>
                                    {executedUsersList.map(u => <option key={u} value={u}>{u}</option>)}
                                </select>

                                <select className="filter-select" value={executedTradeType} onChange={e => setExecutedTradeType(e.target.value)} disabled={isActionLocked}>
                                    <option value="ALL">All Trade Types</option>
                                    <option value="BUY">BUY</option>
                                    <option value="SELL">SELL</option>
                                </select>
                            </div>


                        </div>

                        {/* Monitor Container Executed */}
                        <div className="monitor-container" data-aos="fade-up" data-aos-delay="100">
                            <div className="table-meta-bar">
                                <div className="positions-count-text">
                                    {executedLoading ? (
                                        <span>Loading executed positions...</span>
                                    ) : (
                                        <span>Showing <strong>{paginatedExecutedPositions.length}</strong> of {filteredExecutedPositions.length} executed positions</span>
                                    )}
                                </div>
                                <div className="table-meta-right" style={{ display: 'flex', alignItems: 'center', gap: '24px' }}>
                                    <div className="actions-group" style={{ display: 'flex', gap: '8px' }}>
                                        <button className="btn-outline" onClick={fetchExecutedPositions} title="Refresh Executed Positions">
                                            🔄 Refresh
                                        </button>
                                        <button className="btn-outline" onClick={exportToCSV} title="Export CSV" style={{ borderColor: 'var(--success)', color: 'var(--success)' }}>
                                            ⬇ Export CSV
                                        </button>
                                    </div>
                                    <div className="pagination-controls">
                                        <button className="page-nav-btn" onClick={() => setExecutedPage(p => Math.max(1, p - 1))} disabled={executedPage <= 1}>&lt;</button>
                                        <span className="page-indicator">Page {executedPage} / {totalExecutedPages}</span>
                                        <button className="page-nav-btn" onClick={() => setExecutedPage(p => Math.min(totalExecutedPages, p + 1))} disabled={executedPage >= totalExecutedPages}>&gt;</button>
                                    </div>
                                </div>
                            </div>

                            <div className="table-responsive">
                                <table className="compact-table">
                                    <thead>
                                        <tr>
                                            <th>User &uarr;</th>
                                            <th>Broker</th>
                                            <th>Instrument</th>
                                            <th>Exchange</th>
                                            <th>Product</th>
                                            <th>Side</th>
                                            <th className="text-right">Qty</th>
                                            <th className="text-right">Entry</th>
                                            <th className="text-right">Exit Price</th>
                                            <th className="text-right">PnL &darr;</th>
                                            <th>Exited At</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {executedLoading ? (
                                            <tr>
                                                <td colSpan="11" className="empty-table-cell">Loading executed positions from database...</td>
                                            </tr>
                                        ) : paginatedExecutedPositions.length === 0 ? (
                                            <tr>
                                                <td colSpan="11" className="empty-table-cell">No successful exits found for the selected date range.</td>
                                            </tr>
                                        ) : (
                                            paginatedExecutedPositions.map(pos => {
                                                const side = (pos.transaction_type || pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                                                const pnlVal = pos.pnl || 0;
                                                const exitDateStr = pos.exited_at ? new Date(pos.exited_at).toLocaleString('en-GB', {
                                                    day: '2-digit', month: 'short', year: 'numeric',
                                                    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true
                                                }) : 'N/A';

                                                return (
                                                    <tr key={'exec_' + pos.id}>
                                                        <td>{getDisplayUsername(pos)}</td>
                                                        <td>{pos.broker}</td>
                                                        <td className="font-semibold">{pos.symbol}</td>
                                                        <td>{pos.exchange}</td>
                                                        <td>{pos.product_type}</td>
                                                        <td>
                                                            <span className={`side-badge ${side === 'BUY' ? 'side-buy' : 'side-sell'}`}>{side}</span>
                                                        </td>
                                                        <td className="text-right tabular">{pos.quantity}</td>
                                                        <td className="text-right tabular">{(pos.avg_price || 0).toFixed(2)}</td>
                                                        <td className="text-right tabular">{(pos.exit_price || 0).toFixed(2)}</td>
                                                        <td className={`text-right tabular ${pnlVal >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                                                            {pnlVal >= 0 ? `+${pnlVal.toFixed(2)}` : pnlVal.toFixed(2)}
                                                        </td>
                                                        <td className="tabular">{exitDateStr}</td>
                                                    </tr>
                                                );
                                            })
                                        )}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </>
                )}
            </main>
        </div>
    );
}

/* ═══════════════════════════════════════════
   BOOTSTRAP
   ═══════════════════════════════════════════ */
const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
