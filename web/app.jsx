const { useState, useEffect } = React;

function App() {
    const [user, setUser] = useState(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        fetch('/api/me')
            .then(res => {
                if (res.ok) {
                    return res.json();
                }
                throw new Error('Not authenticated');
            })
            .then(data => setUser(data))
            .catch(() => setUser(null))
            .finally(() => setLoading(false));
    }, []);

    if (loading) return <div>Loading DRMT...</div>;

    return user ? <Dashboard user={user} onLogout={() => setUser(null)} /> : <Login onLogin={setUser} />;
}

function Login({ onLogin }) {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');
        
        try {
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password })
            });
            
            if (res.ok) {
                const meRes = await fetch('/api/me');
                const userData = await meRes.json();
                onLogin(userData);
            } else {
                setError('Invalid credentials');
            }
        } catch (err) {
            setError('Login failed');
        }
    };

    return (
        <div className="login-container">
            <div className="login-box">
                <h2>DRMT Login</h2>
                {error && <div className="error">{error}</div>}
                <form onSubmit={handleSubmit}>
                    <input 
                        type="text" 
                        placeholder="Username" 
                        value={username}
                        onChange={(e) => setUsername(e.target.value)}
                        required
                    />
                    <input 
                        type="password" 
                        placeholder="Password" 
                        value={password}
                        onChange={(e) => setPassword(e.target.value)}
                        required
                    />
                    <button type="submit">Login</button>
                </form>
            </div>
        </div>
    );
}

function Dashboard({ user, onLogout }) {
    const handleLogout = async () => {
        await fetch('/api/logout', { method: 'POST' });
        onLogout();
    };

    return (
        <div className="dashboard-container">
            <header className="dashboard-header">
                <h2>DRMT V2</h2>
                <div>
                    <span>Admin: {user.username}</span>
                    <button onClick={handleLogout} className="logout-btn">Logout</button>
                </div>
            </header>
            <div className="dashboard-content">
                <LivePositions />
            </div>
        </div>
    );
}

function LivePositions() {
    const [positions, setPositions] = useState([]);
    const [lastUpdate, setLastUpdate] = useState(null);

    useEffect(() => {
        const fetchPositions = async () => {
            try {
                const res = await fetch('/api/positions');
                if (res.ok) {
                    const data = await res.json();
                    setPositions(data || []);
                    setLastUpdate(new Date().toLocaleTimeString());
                }
            } catch (err) {
                console.error("Error fetching positions", err);
            }
        };

        fetchPositions();
        const interval = setInterval(fetchPositions, 1000); // 1-second polling

        return () => clearInterval(interval);
    }, []);

    // Derived states
    const totalPnL = positions.reduce((acc, pos) => acc + (pos.pnl || 0), 0);

    return (
        <div className="positions-view">
            <div className="stats-bar">
                <div><strong>Total Open Positions:</strong> {positions.length}</div>
                <div><strong>Global PnL:</strong> <span className={totalPnL >= 0 ? 'profit' : 'loss'}>₹{totalPnL.toFixed(2)}</span></div>
                <div style={{fontSize: '0.8em', color: '#888'}}>Last Updated: {lastUpdate || 'Waiting...'}</div>
            </div>

            <div className="table-container">
                <table className="positions-table">
                    <thead>
                        <tr>
                            <th>Broker</th>
                            <th>Client ID</th>
                            <th>Symbol</th>
                            <th>Exchange</th>
                            <th>Type</th>
                            <th>Quantity</th>
                            <th>Avg Price</th>
                            <th>LTP</th>
                            <th>PnL</th>
                        </tr>
                    </thead>
                    <tbody>
                        {positions.length === 0 ? (
                            <tr>
                                <td colSpan="9" style={{textAlign: 'center', padding: '2rem'}}>
                                    No live positions found. (Adapters running, awaiting valid tokens...)
                                </td>
                            </tr>
                        ) : (
                            positions.map((pos) => (
                                <tr key={pos.id}>
                                    <td>{pos.broker}</td>
                                    <td>{pos.client_id}</td>
                                    <td>{pos.symbol}</td>
                                    <td>{pos.exchange}</td>
                                    <td className={`tx-${pos.transaction?.toLowerCase()}`}>{pos.transaction}</td>
                                    <td>{pos.quantity}</td>
                                    <td>{pos.avg_price?.toFixed(2)}</td>
                                    <td>{pos.ltp?.toFixed(2)}</td>
                                    <td className={pos.pnl >= 0 ? 'profit' : 'loss'}>{pos.pnl?.toFixed(2)}</td>
                                </tr>
                            ))
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
