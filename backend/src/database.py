"""
Database layer for Pump.fun Intelligence Platform
SQLite implementation (easy deployment, no MySQL required)
"""

import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'pumpfun_intelligence.db')

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Return rows as dictionaries
    return conn

def init_database():
    """Initialize database schema"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Wallets table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL UNIQUE,
            chain TEXT NOT NULL DEFAULT 'solana',
            label TEXT,
            total_trades INTEGER DEFAULT 0,
            profitable_trades INTEGER DEFAULT 0,
            win_rate INTEGER DEFAULT 0,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Transactions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_hash TEXT NOT NULL UNIQUE,
            chain TEXT NOT NULL DEFAULT 'solana',
            wallet_address TEXT NOT NULL,
            token_address TEXT NOT NULL,
            transaction_type TEXT NOT NULL CHECK(transaction_type IN ('buy', 'sell', 'transfer')),
            amount TEXT NOT NULL,
            amount_usd INTEGER NOT NULL,
            timestamp TIMESTAMP NOT NULL,
            block_number TEXT NOT NULL,
            FOREIGN KEY (wallet_address) REFERENCES wallets(address)
        )
    ''')
    
    # Tokens table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL UNIQUE,
            chain TEXT NOT NULL DEFAULT 'solana',
            symbol TEXT,
            name TEXT,
            price_usd INTEGER DEFAULT 0,
            liquidity_usd INTEGER DEFAULT 0,
            volume_24h INTEGER DEFAULT 0,
            unique_wallets_24h INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Clusters table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT NOT NULL,
            cluster_type TEXT NOT NULL CHECK(cluster_type IN ('temporal', 'smart_money', 'early_accumulation')),
            wallet_count INTEGER DEFAULT 0,
            total_volume_usd INTEGER DEFAULT 0,
            cluster_score INTEGER DEFAULT 0,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active' CHECK(status IN ('active', 'dissolved', 'triggered'))
        )
    ''')
    
    # Cluster wallets table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cluster_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id INTEGER NOT NULL,
            wallet_address TEXT NOT NULL,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cluster_id) REFERENCES clusters(id),
            FOREIGN KEY (wallet_address) REFERENCES wallets(address)
        )
    ''')
    
    # User preferences table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_chat_id TEXT UNIQUE,
            tracked_wallets TEXT,
            alert_settings TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create indexes for performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_wallet ON transactions(wallet_address)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_token ON transactions(token_address)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions(timestamp)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_clusters_token ON clusters(token_address)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_clusters_status ON clusters(status)')
    
    conn.commit()
    conn.close()
    print(f"[Database] Initialized at {DB_PATH}")

# Wallet operations

def upsert_wallet(address: str, chain: str = 'solana', label: Optional[str] = None,
                  total_trades: int = 0, profitable_trades: int = 0, win_rate: int = 0):
    """Insert or update wallet"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO wallets (address, chain, label, total_trades, profitable_trades, win_rate, last_active)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(address) DO UPDATE SET
            label = excluded.label,
            total_trades = excluded.total_trades,
            profitable_trades = excluded.profitable_trades,
            win_rate = excluded.win_rate,
            last_active = CURRENT_TIMESTAMP
    ''', (address, chain, label, total_trades, profitable_trades, win_rate))
    
    conn.commit()
    conn.close()

def get_wallet(address: str) -> Optional[Dict]:
    """Get wallet by address"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM wallets WHERE address = ?', (address,))
    row = cursor.fetchone()
    conn.close()
    
    return dict(row) if row else None

def get_top_wallets(limit: int = 100) -> List[Dict]:
    """Get top wallets by win rate"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM wallets 
        WHERE total_trades >= 10 
        ORDER BY win_rate DESC, total_trades DESC 
        LIMIT ?
    ''', (limit,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

# Transaction operations

def insert_transaction(tx_hash: str, chain: str, wallet_address: str, token_address: str,
                       transaction_type: str, amount: str, amount_usd: int, 
                       timestamp: datetime, block_number: str):
    """Insert transaction"""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute('''
            INSERT INTO transactions 
            (tx_hash, chain, wallet_address, token_address, transaction_type, 
             amount, amount_usd, timestamp, block_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (tx_hash, chain, wallet_address, token_address, transaction_type,
              amount, amount_usd, timestamp, block_number))
        
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # Transaction already exists
    finally:
        conn.close()

def get_recent_transactions(hours: int = 24) -> List[Dict]:
    """Get recent transactions"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM transactions 
        WHERE timestamp >= datetime('now', '-' || ? || ' hours')
        ORDER BY timestamp DESC
    ''', (hours,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

def get_wallet_transactions(wallet_address: str, limit: int = 100) -> List[Dict]:
    """Get transactions for a wallet"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM transactions 
        WHERE wallet_address = ? 
        ORDER BY timestamp DESC 
        LIMIT ?
    ''', (wallet_address, limit))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

# Token operations

def upsert_token(address: str, chain: str = 'solana', symbol: Optional[str] = None,
                 name: Optional[str] = None, price_usd: int = 0, liquidity_usd: int = 0,
                 volume_24h: int = 0, unique_wallets_24h: int = 0):
    """Insert or update token"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO tokens 
        (address, chain, symbol, name, price_usd, liquidity_usd, volume_24h, unique_wallets_24h, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(address) DO UPDATE SET
            symbol = excluded.symbol,
            name = excluded.name,
            price_usd = excluded.price_usd,
            liquidity_usd = excluded.liquidity_usd,
            volume_24h = excluded.volume_24h,
            unique_wallets_24h = excluded.unique_wallets_24h,
            updated_at = CURRENT_TIMESTAMP
    ''', (address, chain, symbol, name, price_usd, liquidity_usd, volume_24h, unique_wallets_24h))
    
    conn.commit()
    conn.close()

def get_token(address: str) -> Optional[Dict]:
    """Get token by address"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM tokens WHERE address = ?', (address,))
    row = cursor.fetchone()
    conn.close()
    
    return dict(row) if row else None

# Cluster operations

def insert_cluster(token_address: str, cluster_type: str, wallet_count: int,
                   total_volume_usd: int, cluster_score: int) -> int:
    """Insert cluster and return cluster ID"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO clusters 
        (token_address, cluster_type, wallet_count, total_volume_usd, cluster_score)
        VALUES (?, ?, ?, ?, ?)
    ''', (token_address, cluster_type, wallet_count, total_volume_usd, cluster_score))
    
    cluster_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return cluster_id

def insert_cluster_wallet(cluster_id: int, wallet_address: str):
    """Add wallet to cluster"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO cluster_wallets (cluster_id, wallet_address)
        VALUES (?, ?)
    ''', (cluster_id, wallet_address))
    
    conn.commit()
    conn.close()

def get_active_clusters() -> List[Dict]:
    """Get all active clusters"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM clusters 
        WHERE status = 'active' 
        ORDER BY cluster_score DESC, detected_at DESC
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]

def get_cluster_wallets(cluster_id: int) -> List[str]:
    """Get wallet addresses in a cluster"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT wallet_address FROM cluster_wallets 
        WHERE cluster_id = ?
    ''', (cluster_id,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [row['wallet_address'] for row in rows]

def update_cluster_status(cluster_id: int, status: str):
    """Update cluster status"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE clusters SET status = ? WHERE id = ?
    ''', (status, cluster_id))
    
    conn.commit()
    conn.close()

# User preferences

def upsert_user_preferences(telegram_chat_id: str, tracked_wallets: List[str] = None,
                             alert_settings: Dict = None):
    """Insert or update user preferences"""
    conn = get_db()
    cursor = conn.cursor()
    
    tracked_wallets_json = json.dumps(tracked_wallets or [])
    alert_settings_json = json.dumps(alert_settings or {})
    
    cursor.execute('''
        INSERT INTO user_preferences 
        (telegram_chat_id, tracked_wallets, alert_settings, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(telegram_chat_id) DO UPDATE SET
            tracked_wallets = excluded.tracked_wallets,
            alert_settings = excluded.alert_settings,
            updated_at = CURRENT_TIMESTAMP
    ''', (telegram_chat_id, tracked_wallets_json, alert_settings_json))
    
    conn.commit()
    conn.close()

def get_user_preferences(telegram_chat_id: str) -> Optional[Dict]:
    """Get user preferences"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT * FROM user_preferences WHERE telegram_chat_id = ?
    ''', (telegram_chat_id,))
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        prefs = dict(row)
        prefs['tracked_wallets'] = json.loads(prefs['tracked_wallets'] or '[]')
        prefs['alert_settings'] = json.loads(prefs['alert_settings'] or '{}')
        return prefs
    
    return None

# Initialize database on module import
if not os.path.exists(DB_PATH):
    init_database()

