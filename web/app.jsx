const { useState, useEffect, useMemo } = React;


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

function useDebounce(value, delay) {
    const [debouncedValue, setDebouncedValue] = useState(value);
    useEffect(() => {
        const handler = setTimeout(() => {
            setDebouncedValue(value);
        }, delay);
        return () => clearTimeout(handler);
    }, [value, delay]);
    return debouncedValue;
}

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
            <div className="login-box">
                <div className="login-header">
                    <div className="brand-dot"></div>
                    <h2>Disaster Recovery Management Tool</h2>
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
                            onChange={(e) => setUsername(e.target.value)}
                            required
                        />
                    </div>
                    <div className="form-group">
                        <label>Password</label>
                        <input
                            type="password"
                            placeholder="Enter password"
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
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


const ServerHealthMonitor = () => {
    const [servers, setServers] = React.useState({});

    React.useEffect(() => {
        const fetchHealth = () => {
            fetchWithTimeout('/api/health/servers', { timeout: 30000,
                headers: { 'Authorization': `Bearer ${localStorage.getItem('drmt_token')}` }
            })
            .then(res => res.json())
            .then(data => {
                if (data) setServers(data);
            })
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
                        <span className="health-dot"></span>
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
            .then(data => {
                if (data) setLogs(data);
            })
            .catch(err => {});
        };
        fetchLogs();
        const interval = setInterval(fetchLogs, 3000); // 3-second live sync
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

    return (
        <div className="action-log-widget">
            <div className="action-log-header">
                <div className="action-log-title">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <line x1="8" y1="6" x2="21" y2="6"></line>
                        <line x1="8" y1="12" x2="21" y2="12"></line>
                        <line x1="8" y1="18" x2="21" y2="18"></line>
                        <line x1="3" y1="6" x2="3.01" y2="6"></line>
                        <line x1="3" y1="12" x2="3.01" y2="12"></line>
                        <line x1="3" y1="18" x2="3.01" y2="18"></line>
                    </svg>
                    Action Log
                </div>
                <div className="live-indicator">
                    <span className="live-dot"></span> LIVE
                </div>
            </div>
            <div className="action-log-tabs">
                <button className={`action-log-tab ${activeTab === 'orders' ? 'active' : ''}`} onClick={() => setActiveTab('orders')}>Orders</button>
                <button className={`action-log-tab ${activeTab === 'auth' ? 'active' : ''}`} onClick={() => setActiveTab('auth')}>Auth</button>
                <button className={`action-log-tab ${activeTab === 'system' ? 'active' : ''}`} onClick={() => setActiveTab('system')}>System</button>
            </div>
            <div className="action-log-content">
                {logs.length === 0 ? (
                    <div style={{ padding: '20px', textAlign: 'center', color: '#64748B', fontSize: '11px' }}>
                        No logs available for {activeTab}.
                    </div>
                ) : (
                    logs.map(log => (
                        <div key={log.id} className="log-entry">
                            <span className="log-icon">{getLogIcon(log.level, log.message)}</span>
                            <div className="log-details">
                                <div className="log-message">{log.message}</div>
                                <div className="log-meta">
                                    <span>{log.module}</span>
                                    <span>{log.timestamp.split(' ')[1]}</span>
                                </div>
                            </div>
                        </div>
                    ))
                )}
            </div>
        </div>
    );
};

function Dashboard({ user, onLogout }) {
    const [toasts, setToasts] = useState([]);
    const [positions, setPositions] = useState([]);
    const [lastSync, setLastSync] = useState(null);
    const [activeTab, setActiveTab] = useState('running');

    const [isFetchingPositions, setIsFetchingPositions] = useState(false);
    const [isExporting, setIsExporting] = useState(false);
    const [isLoggingOut, setIsLoggingOut] = useState(false);



    // Filters
    const [searchQuery, setSearchQuery] = useState('');
    const debouncedSearchQuery = useDebounce(searchQuery, 500);
    const [selectedBroker, setSelectedBroker] = useState('ALL');
    const [selectedUser, setSelectedUser] = useState('ALL');
    const [selectedTradeType, setSelectedTradeType] = useState('ALL');

    // Selection & Pagination
    const [selectedIds, setSelectedIds] = useState(new Set());
    const [currentPage, setCurrentPage] = useState(1);
    const pageSize = 50;

    // Modals & Execution
    const [modalConfig, setModalConfig] = useState(null);
    const [isExiting, setIsExiting] = useState(false);
    const isActionLocked = isExiting || isExporting || isLoggingOut;



    const addToast = (message, type = 'info') => {
        const id = Date.now() + Math.random();
        setToasts(prev => [...prev, { id, message, type }]);
        setTimeout(() => {
            setToasts(prev => prev.filter(t => t.id !== id));
        }, 5000);
    };

    const handleLogout = async () => {
        await fetch('/api/logout', { method: 'POST' });
        onLogout();
    };

    // Polling live positions every 5 seconds
    useEffect(() => {
        const fetchPositions = async () => {
            if (isActionLocked) return;
            setIsFetchingPositions(true);
            try {
                const res = await fetchWithTimeout('/api/positions', { timeout: 30000 });
                if (res.ok) {
                    const data = await res.json();
                    setPositions(data || []);
                    setLastSync(new Date().toLocaleTimeString());
                }
            } catch (err) {
                console.error("Error fetching positions", err);
                if (err.name === 'AbortError') {
                    addToast("Position sync timed out. Retrying...", "warning");
                }
            } finally {
                setIsFetchingPositions(false);
            }
        };

        fetchPositions();
        const interval = setInterval(fetchPositions, 5000);
        return () => clearInterval(interval);
    }, []);

    // Extract dynamic dropdown options
    const brokersList = useMemo(() => {
        const set = new Set();
        positions.forEach(p => { if (p.broker) set.add(p.broker); });
        return Array.from(set).sort();
    }, [positions]);

    const usersList = useMemo(() => {
        const set = new Set();
        positions.forEach(p => { if (p.client_id) set.add(p.client_id); });
        return Array.from(set).sort();
    }, [positions]);

    // Combined filtering logic
    const filteredPositions = useMemo(() => {
        return positions.filter(pos => {
            // Broker filter
            if (selectedBroker !== 'ALL' && pos.broker !== selectedBroker) {
                return false;
            }
            // User / Client ID filter
            if (selectedUser !== 'ALL' && pos.client_id !== selectedUser) {
                return false;
            }
            // Trade Type filter
            if (selectedTradeType !== 'ALL') {
                const side = (pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                if (side !== selectedTradeType) return false;
            }
            // Text Search filter (symbol, token, client_id, broker, exchange, product)
            if (debouncedSearchQuery.trim() !== '') {
                const q = debouncedSearchQuery.toLowerCase().trim();
                const matchSymbol = (pos.symbol || '').toLowerCase().includes(q);
                const matchClient = (pos.client_id || '').toLowerCase().includes(q);
                const matchBroker = (pos.broker || '').toLowerCase().includes(q);
                const matchExchange = (pos.exchange || '').toLowerCase().includes(q);
                const matchToken = (pos.token || '').toLowerCase().includes(q);
                if (!matchSymbol && !matchClient && !matchBroker && !matchExchange && !matchToken) {
                    return false;
                }
            }
            return true;
        });
    }, [positions, selectedBroker, selectedUser, selectedTradeType, debouncedSearchQuery]);

    // Reset pagination to page 1 on filter change
    useEffect(() => {
        setCurrentPage(1);
    }, [searchQuery, selectedBroker, selectedUser, selectedTradeType]);

    // Pagination calculations
    const totalPages = Math.max(1, Math.ceil(filteredPositions.length / pageSize));
    const paginatedPositions = useMemo(() => {
        const start = (currentPage - 1) * pageSize;
        return filteredPositions.slice(start, start + pageSize);
    }, [filteredPositions, currentPage, pageSize]);

    // Selection handlers
    const handleSelectRow = (id) => {
        const newSet = new Set(selectedIds);
        if (newSet.has(id)) {
            newSet.delete(id);
        } else {
            newSet.add(id);
        }
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

    const handleCancelSelection = () => {
        setSelectedIds(new Set());
    };

    // Close / Close All Action Logic
    const handleCloseAction = () => {
        if (selectedIds.size > 0) {
            // Close Selected
            const count = selectedIds.size;
            setModalConfig({
                title: "Confirm Market Exit",
                message: `Are you sure you want to Market Exit ${count} selected position${count > 1 ? 's' : ''}?\n\nThis will instantly submit Market orders to the respective broker(s) to close these positions.`,
                onConfirm: () => executeExit(Array.from(selectedIds)),
                onCancel: () => setModalConfig(null)
            });
        } else {
            // Close All Filtered Running Positions
            if (filteredPositions.length === 0) {
                addToast("No running positions available to close.", "info");
                return;
            }
            const count = filteredPositions.length;
            const allIds = filteredPositions.map(p => p.id);
            setModalConfig({
                title: "Confirm Close All Positions",
                message: `⚠️ WARNING: Are you sure you want to Market Exit ALL ${count} currently displayed position${count > 1 ? 's' : ''}?\n\nThis will immediately place Market orders across all brokers to flatten these positions.`,
                onConfirm: () => executeExit(allIds),
                onCancel: () => setModalConfig(null)
            });
        }
    };

    const executeExit = async (idsToExit) => {
        setModalConfig(null);
        setIsExiting(true);

        try {
            const res = await fetchWithTimeout('/api/positions/exit', {
timeout: 30000,
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ position_ids: idsToExit })
            });

            if (res.ok) {
                const data = await res.json();
                setSelectedIds(new Set());
                addToast(data.message || `Processed exit for ${idsToExit.length} position(s)`, "success");

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
            if(err.name === "AbortError") { addToast("Execution timed out (30s)!", "error"); } else { addToast("Error communicating with server.", "error"); }
        } finally {
            setIsExiting(false);
        }
    };

    // Executed Tab State
    const [executedPositions, setExecutedPositions] = useState([]);
    const [executedLoading, setExecutedLoading] = useState(false);
    const [availableDates, setAvailableDates] = useState([]);
    
    // Default fromDate = 7 days ago, toDate = today
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

    // Date options list (generated past 30 days + any DB dates)
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
        availableDates.forEach(dt => {
            if (!map.has(dt)) {
                map.set(dt, dt);
            }
        });
        return Array.from(map.entries()).map(([value, label]) => ({ value, label }));
    }, [availableDates]);

    // Fetch available dates from API
    useEffect(() => {
        fetch('/api/positions/exited/dates')
            .then(res => res.ok ? res.json() : [])
            .then(data => {
                if (Array.isArray(data) && data.length > 0) {
                    setAvailableDates(data);
                }
            })
            .catch(() => {});
    }, []);

    // Fetch executed positions when tab is executed or date filters change
    const fetchExecutedPositions = async () => {
        setExecutedLoading(true);
        try {
            const params = new URLSearchParams();
            if (fromDate) params.append('from_date', fromDate);
            if (toDate) params.append('to_date', toDate);
            if (executedBroker !== 'ALL') params.append('broker', executedBroker);
            if (executedUser !== 'ALL') params.append('client_id', executedUser);

            const res = await fetchWithTimeout(`/api/positions/exited?${params.toString()}`, { timeout: 30000 });
            if (res.ok) {
                const data = await res.json();
                setExecutedPositions(data || []);
            }
        } catch (err) {
            console.error("Failed to fetch executed positions:", err);
            if(err.name === "AbortError") { addToast("Fetch executed positions timed out", "error"); } else { addToast("Failed to fetch executed positions", "error"); }
        } finally {
            setExecutedLoading(false);
        }
    };

    const exportToCSV = async () => {
        if (!filteredExecutedPositions || filteredExecutedPositions.length === 0) {
            addToast("No data to export", "warning");
            return;
        }
        setIsExporting(true);
        // Small delay to allow UI to render spinner
        await new Promise(resolve => setTimeout(resolve, 100));

        try {
            const headers = ["USERID", "BROKER", "INSTRUMENT", "EXCHANGE", "PRODUCT", "BUY/SELL", "QUANTITY", "ENTRY PRICE", "EXIT PRICE", "PNL", "EXITED AT"];
            const csvRows = [headers.join(",")];
            
            filteredExecutedPositions.forEach(pos => {
                const side = (pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                const exitDateStr = pos.exited_at ? new Date(pos.exited_at).toLocaleString('en-GB') : 'N/A';
                
                const row = [
                    pos.client_id || pos.user_id || "",
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

            const csvString = csvRows.join("");
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
        if (activeTab === 'executed') {
            fetchExecutedPositions();
        }
    }, [activeTab, fromDate, toDate, executedBroker, executedUser]);

    // Filter executed positions by search query and trade type
    const filteredExecutedPositions = useMemo(() => {
        return executedPositions.filter(pos => {
            if (executedTradeType !== 'ALL') {
                const side = (pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                if (side !== executedTradeType) return false;
            }
            if (debouncedExecutedSearchQuery.trim() !== '') {
                const q = debouncedExecutedSearchQuery.toLowerCase().trim();
                const matchSymbol = (pos.symbol || '').toLowerCase().includes(q);
                const matchClient = (pos.client_id || '').toLowerCase().includes(q);
                const matchBroker = (pos.broker || '').toLowerCase().includes(q);
                const matchExchange = (pos.exchange || '').toLowerCase().includes(q);
                const matchToken = (pos.token || '').toLowerCase().includes(q);
                if (!matchSymbol && !matchClient && !matchBroker && !matchExchange && !matchToken) {
                    return false;
                }
            }
            return true;
        });
    }, [executedPositions, executedTradeType, debouncedExecutedSearchQuery]);

    // Reset pagination on filter change
    useEffect(() => {
        setExecutedPage(1);
    }, [executedSearchQuery, executedTradeType, fromDate, toDate, executedBroker, executedUser]);

    const totalExecutedPages = Math.max(1, Math.ceil(filteredExecutedPositions.length / pageSize));
    const paginatedExecutedPositions = useMemo(() => {
        const start = (executedPage - 1) * pageSize;
        return filteredExecutedPositions.slice(start, start + pageSize);
    }, [filteredExecutedPositions, executedPage, pageSize]);

    // Extract dropdown lists for executed positions
    const executedBrokersList = useMemo(() => {
        const set = new Set();
        executedPositions.forEach(p => { if (p.broker) set.add(p.broker); });
        brokersList.forEach(b => set.add(b));
        return Array.from(set).sort();
    }, [executedPositions, brokersList]);

    const executedUsersList = useMemo(() => {
        const set = new Set();
        executedPositions.forEach(p => { if (p.client_id) set.add(p.client_id); });
        usersList.forEach(u => set.add(u));
        return Array.from(set).sort();
    }, [executedPositions, usersList]);

    const adminInitial = (user?.username || 'A').charAt(0).toUpperCase();

    return (
        <div className="app-layout">
            {/* Toast Alerts */}
            <div className="toast-container">
                {toasts.map(t => (
                    <div key={t.id} className={`toast toast-${t.type}`}>
                        {t.message}
                    </div>
                ))}
            </div>

            {/* Confirmation Modal */}
            {modalConfig && (
                <div className="modal-overlay">
                    <div className="modal-content">
                        <h3>{modalConfig.title}</h3>
                        <p className="modal-message">{modalConfig.message}</p>
                        <div className="modal-actions">
                            <button onClick={modalConfig.onCancel} className="btn-cancel">Cancel</button>
                            <button onClick={modalConfig.onConfirm} className="btn-confirm-exit">Confirm Exit</button>
                        </div>
                    </div>
                </div>
            )}

            {/* Top Navigation Header */}
            <header className="top-nav">
                <div className="nav-left">
                    <span className="brand-dot"></span>
                    <h1 className="brand-title">Disaster Recovery Management Tool</h1>
                </div>
                
                <div className="nav-right">
                    <ServerHealthMonitor />

                    {/* Placeholder Bell Notification */}
                    <button className="icon-btn bell-btn" title="Notifications">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path>
                            <path d="M13.73 21a2 2 0 0 1-3.46 0"></path>
                        </svg>
                        <span className="bell-badge"></span>
                    </button>

                    {/* Admin Avatar */}
                    <div className="user-profile" title={`Logged in as ${user.username}`}>
                        <div className="avatar-circle">
                            {adminInitial}
                        </div>
                        <button onClick={async () => { setIsLoggingOut(true); await handleLogout(); setIsLoggingOut(false); }}
disabled={isActionLocked} className="logout-link-btn" title="Sign Out">
                            Sign Out
                        </button>
                    </div>
                </div>
            </header>

            {/* Main Content View */}
            <main className="main-content">
                <ActionLogWidget />
                {/* Page Title & Subtitle */}
                <div className="page-header">
                    <h2 className="page-title">Position Monitor</h2>
                    <div className="last-sync-text" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        {isFetchingPositions && <div className="loading-spinner" style={{ width: '12px', height: '12px', borderWidth: '2px', borderTopColor: '#3B82F6' }}></div>}
                        <span>Last Sync: {lastSync || 'Waiting for first sync...'}</span>
                    </div>
                </div>

                {/* Tabs */}
                <div className="tabs-bar">
                    <button
                        className={`tab-btn ${activeTab === 'running' ? 'active' : ''}`}
                        onClick={() => !isActionLocked && setActiveTab('running')}
disabled={isActionLocked}
                    >
                        Running
                    </button>
                    {/* <button
                        className={`tab-btn ${activeTab === 'pending' ? 'active' : ''}`}
                        onClick={() => setActiveTab('pending')}
                    >
                        Pending Exit
                    </button> */}
                    <button
                        className={`tab-btn ${activeTab === 'executed' ? 'active' : ''}`}
                        onClick={() => !isActionLocked && setActiveTab('executed')}
disabled={isActionLocked}
                    >
                        Executed
                    </button>
                </div>

                {activeTab === 'running' ? (
                    <div className="monitor-container">
                        {/* Filters Bar */}
                        <div className="filters-bar">
                            <div className="search-input-wrapper">
                                <svg className="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                    <circle cx="11" cy="11" r="8"></circle>
                                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                                </svg>
                                <input
                                    type="text"
                                    className="filter-input search-input"
disabled={isActionLocked}
                                    placeholder="Search instrument..."
                                    value={searchQuery}
                                    onChange={(e) => setSearchQuery(e.target.value)}
                                />
                            </div>

                            <select
                                className="filter-select"
disabled={isActionLocked}
                                value={selectedBroker}
                                onChange={(e) => setSelectedBroker(e.target.value)}
                            >
                                <option value="ALL">All Brokers</option>
                                {brokersList.map(b => (
                                    <option key={b} value={b}>{b}</option>
                                ))}
                            </select>

                            <select
                                className="filter-select"
disabled={isActionLocked}
                                value={selectedUser}
                                onChange={(e) => setSelectedUser(e.target.value)}
                            >
                                <option value="ALL">All Users</option>
                                {usersList.map(u => (
                                    <option key={u} value={u}>{u}</option>
                                ))}
                            </select>

                            <select
                                className="filter-select"
disabled={isActionLocked}
                                value={selectedTradeType}
                                onChange={(e) => setSelectedTradeType(e.target.value)}
                            >
                                <option value="ALL">All Trade Types</option>
                                <option value="BUY">BUY</option>
                                <option value="SELL">SELL</option>
                            </select>
                        </div>

                        {/* Action Buttons: Close / Close All + Cancel */}
                        <div className="actions-bar">
                            <button
                                onClick={handleCloseAction}
                                disabled={isActionLocked || (filteredPositions.length === 0 && selectedIds.size === 0)}
                                className="btn-close-action"
                            >
                                {isExiting
                                    ? 'Dispatching...'
                                    : (selectedIds.size > 0 ? `Close (${selectedIds.size})` : 'Close All')
                                }
                            </button>

                            <button
                                onClick={handleCancelSelection}
                                disabled={isActionLocked || selectedIds.size === 0}
                                className={`btn-cancel-action ${selectedIds.size === 0 ? 'disabled' : ''}`}
                            >
                                Cancel
                            </button>
                        </div>

                        {/* Meta Count & Pagination */}
                        <div className="table-meta-bar">
                            <div className="positions-count-text">
                                Showing <strong>{paginatedPositions.length}</strong> of {filteredPositions.length} positions
                            </div>

                            <div className="pagination-controls">
                                <button
                                    className="page-nav-btn"
                                    onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                                    disabled={currentPage <= 1}
                                    title="Previous Page"
                                >
                                    &lt;
                                </button>
                                <span className="page-indicator">
                                    Page {currentPage} / {totalPages}
                                </span>
                                <button
                                    className="page-nav-btn"
                                    onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                                    disabled={currentPage >= totalPages}
                                    title="Next Page"
                                >
                                    &gt;
                                </button>
                            </div>
                        </div>

                        {/* Compact Table */}
                        
                        <div className="table-responsive" style={{position: 'relative'}}>


                            <table className="compact-table">
                                <thead>
                                    <tr>
                                        <th className="th-checkbox">
                                            <input
                                                type="checkbox"
                                                checked={isAllPageSelected}
                                                onChange={handleSelectAllPage}
                                                aria-label="Select all on current page"
                                            />
                                        </th>
                                        <th>USERID</th>
                                        <th>BROKER</th>
                                        <th>INSTRUMENT</th>
                                        <th>EXCHANGE</th>
                                        <th>PRODUCT</th>
                                        <th>BUY/SELL</th>
                                        <th className="text-right">QUANTITY</th>
                                        <th className="text-right">ENTRY PRICE</th>
                                        <th className="text-right">PNL</th>
                                        <th className="text-right">LTP</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {paginatedPositions.length === 0 ? (
                                        <tr>
                                            <td colSpan="11" className="empty-table-cell">
                                                {positions.length === 0
                                                    ? 'No running positions found across active broker accounts.'
                                                    : 'No positions match the selected search and filter criteria.'
                                                }
                                            </td>
                                        </tr>
                                    ) : (
                                        paginatedPositions.map((pos) => {
                                            const isSelected = selectedIds.has(pos.id);
                                            const side = (pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                                            const pnlVal = pos.pnl || 0;
                                            const ltpVal = pos.ltp || pos.avg_price || 0;

                                            return (
                                                <tr
                                                    key={pos.id}
                                                    className={isSelected ? 'row-selected' : ''}
                                                    onClick={(e) => {
                                                        if (e.target.tagName !== 'INPUT') {
                                                            handleSelectRow(pos.id);
                                                        }
                                                    }}
                                                >
                                                    <td className="td-checkbox" onClick={(e) => e.stopPropagation()}>
                                                        <input
                                                            type="checkbox"
                                                            checked={isSelected}
                                                            onChange={() => handleSelectRow(pos.id)}
                                                        />
                                                    </td>
                                                    <td className="cell-userid">{pos.client_id || pos.user_id}</td>
                                                    <td className="cell-broker">{pos.broker}</td>
                                                    <td className="cell-symbol">{pos.symbol}</td>
                                                    <td className="cell-exchange">{pos.exchange}</td>
                                                    <td className="cell-product">{pos.product_type}</td>
                                                    <td className="cell-side">
                                                        <span className={`side-badge ${side === 'BUY' ? 'side-buy' : 'side-sell'}`}>
                                                            {side}
                                                        </span>
                                                    </td>
                                                    <td className="text-right cell-quantity">{pos.quantity}</td>
                                                    <td className="text-right cell-price">{(pos.avg_price || 0).toFixed(2)}</td>
                                                    <td className={`text-right cell-pnl ${pnlVal >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                                                        {pnlVal >= 0 ? `+${pnlVal.toFixed(2)}` : pnlVal.toFixed(2)}
                                                    </td>
                                                    <td className="text-right cell-ltp">{ltpVal.toFixed(2)}</td>
                                                </tr>
                                            );
                                        })
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                ) : (
                    /* activeTab === 'executed' */
                    <div className="monitor-container">
                        {/* Executed Positions Filters Bar with Two Date Range Dropdowns */}
                        <div className="filters-bar">
                            <div className="search-input-wrapper">
                                <svg className="search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                    <circle cx="11" cy="11" r="8"></circle>
                                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                                </svg>
                                <input
                                    type="text"
                                    className="filter-input search-input"
disabled={isActionLocked}
                                    placeholder="Search executed instrument..."
                                    value={executedSearchQuery}
                                    onChange={(e) => setExecutedSearchQuery(e.target.value)}
                                />
                            </div>

                            {/* Dropdown 1: From Date */}
                            <div className="date-filter-group">
                                <label className="filter-label">From Date:</label>
                                <select
                                    className="filter-select date-select"
                                    value={fromDate}
                                    onChange={(e) => setFromDate(e.target.value)}
                                >
                                    {dateOptions.map(opt => (
                                        <option key={'from_' + opt.value} value={opt.value}>
                                            {opt.label}
                                        </option>
                                    ))}
                                </select>
                            </div>

                            {/* Dropdown 2: To Date */}
                            <div className="date-filter-group">
                                <label className="filter-label">To Date:</label>
                                <select
                                    className="filter-select date-select"
                                    value={toDate}
                                    onChange={(e) => setToDate(e.target.value)}
                                >
                                    {dateOptions.map(opt => (
                                        <option key={'to_' + opt.value} value={opt.value}>
                                            {opt.label}
                                        </option>
                                    ))}
                                </select>
                            </div>

                            <select
                                className="filter-select"
disabled={isActionLocked}
                                value={executedBroker}
                                onChange={(e) => setExecutedBroker(e.target.value)}
                            >
                                <option value="ALL">All Brokers</option>
                                {executedBrokersList.map(b => (
                                    <option key={b} value={b}>{b}</option>
                                ))}
                            </select>

                            <select
                                className="filter-select"
disabled={isActionLocked}
                                value={executedUser}
                                onChange={(e) => setExecutedUser(e.target.value)}
                            >
                                <option value="ALL">All Users</option>
                                {executedUsersList.map(u => (
                                    <option key={u} value={u}>{u}</option>
                                ))}
                            </select>

                            <select
                                className="filter-select"
disabled={isActionLocked}
                                value={executedTradeType}
                                onChange={(e) => setExecutedTradeType(e.target.value)}
                            >
                                <option value="ALL">All Trade Types</option>
                                <option value="BUY">BUY</option>
                                <option value="SELL">SELL</option>
                            </select>

                            <button
                                onClick={fetchExecutedPositions}
                                className="btn-refresh-executed"
                                title="Refresh Executed Positions"
                            >
                                🔄 Refresh
                            </button>
                            <button
                                onClick={exportToCSV}
                                className="btn-refresh-executed"
                                title="Export current filtered view to CSV"
                                style={{ borderColor: '#10b981', color: '#10b981' }}
                            >
                                
                            </button>
                        </div>

                        {/* Executed Positions Meta Count & Pagination */}
                        <div className="table-meta-bar">
                            <div className="positions-count-text">
                                {executedLoading ? (
                                    <span>Loading executed positions...</span>
                                ) : (
                                    <span>Showing <strong>{paginatedExecutedPositions.length}</strong> of {filteredExecutedPositions.length} executed positions</span>
                                )}
                            </div>

                            <div className="pagination-controls">
                                <button
                                    className="page-nav-btn"
                                    onClick={() => setExecutedPage(p => Math.max(1, p - 1))}
                                    disabled={executedPage <= 1}
                                    title="Previous Page"
                                >
                                    &lt;
                                </button>
                                <span className="page-indicator">
                                    Page {executedPage} / {totalExecutedPages}
                                </span>
                                <button
                                    className="page-nav-btn"
                                    onClick={() => setExecutedPage(p => Math.min(totalExecutedPages, p + 1))}
                                    disabled={executedPage >= totalExecutedPages}
                                    title="Next Page"
                                >
                                    &gt;
                                </button>
                            </div>
                        </div>

                        {/* Executed Positions Table */}
                        
                        <div className="table-responsive" style={{position: 'relative'}}>


                            <table className="compact-table">
                                <thead>
                                    <tr>
                                        <th>USERID</th>
                                        <th>BROKER</th>
                                        <th>INSTRUMENT</th>
                                        <th>EXCHANGE</th>
                                        <th>PRODUCT</th>
                                        <th>BUY/SELL</th>
                                        <th className="text-right">QUANTITY</th>
                                        <th className="text-right">ENTRY PRICE</th>
                                        <th className="text-right">EXIT PRICE</th>
                                        <th className="text-right">PNL</th>
                                        <th>EXITED AT</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {executedLoading ? (
                                        <tr>
                                            <td colSpan="11" className="empty-table-cell">
                                                Loading executed positions from database...
                                            </td>
                                        </tr>
                                    ) : paginatedExecutedPositions.length === 0 ? (
                                        <tr>
                                            <td colSpan="11" className="empty-table-cell">
                                                No successful exits found for the selected date range ({fromDate} to {toDate}).
                                            </td>
                                        </tr>
                                    ) : (
                                        paginatedExecutedPositions.map((pos) => {
                                            const side = (pos.transaction || (pos.quantity > 0 ? 'BUY' : 'SELL')).toUpperCase();
                                            const pnlVal = pos.pnl || 0;
                                            const exitDateStr = pos.exited_at ? new Date(pos.exited_at).toLocaleString('en-GB', {
                                                day: '2-digit',
                                                month: 'short',
                                                year: 'numeric',
                                                hour: '2-digit',
                                                minute: '2-digit',
                                                second: '2-digit',
                                                hour12: true
                                            }) : 'N/A';

                                            return (
                                                <tr key={'exec_' + pos.id}>
                                                    <td className="cell-userid">{pos.client_id || pos.user_id}</td>
                                                    <td className="cell-broker">{pos.broker}</td>
                                                    <td className="cell-symbol">{pos.symbol}</td>
                                                    <td className="cell-exchange">{pos.exchange}</td>
                                                    <td className="cell-product">{pos.product_type}</td>
                                                    <td className="cell-side">
                                                        <span className={`side-badge ${side === 'BUY' ? 'side-buy' : 'side-sell'}`}>
                                                            {side}
                                                        </span>
                                                    </td>
                                                    <td className="text-right cell-quantity">{pos.quantity}</td>
                                                    <td className="text-right cell-price">{(pos.avg_price || 0).toFixed(2)}</td>
                                                    <td className="text-right cell-price">{(pos.exit_price || 0).toFixed(2)}</td>
                                                    <td className={`text-right cell-pnl ${pnlVal >= 0 ? 'pnl-positive' : 'pnl-negative'}`}>
                                                        {pnlVal >= 0 ? `+${pnlVal.toFixed(2)}` : pnlVal.toFixed(2)}
                                                    </td>
                                                    <td className="cell-timestamp">{exitDateStr}</td>
                                                </tr>
                                            );
                                        })
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                )}
            </main>
        </div>
    );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
