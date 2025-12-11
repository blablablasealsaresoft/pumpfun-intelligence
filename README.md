<div align="center">

# 🚀 Pump.fun Intelligence Platform

**Professional Solana Trading Intelligence System**

*Detect smart money clusters BEFORE tokens pump.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Solana](https://img.shields.io/badge/Solana-Mainnet-green.svg)](https://solana.com/)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://docker.com/)

---

**Stop trading blind. Start trading with intelligence.**

</div>

---

## 📋 Table of Contents

- [Overview](#-overview)
- [How It Works](#-how-it-works)
- [System Architecture](#-system-architecture)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Configuration](#-configuration)
- [API Reference](#-api-reference)
- [Trading Flows](#-trading-flows)
- [Database Schema](#-database-schema)
- [Project Structure](#-project-structure)
- [Pro Tips](#-pro-tips)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)

---

## 💡 Overview

### The Problem

```
❌ You see a token pumping  →  Already too late
❌ You buy at the peak      →  Lose money  
❌ You miss the 10x         →  Watch from sidelines
```

### The Solution

```
✅ Platform detects 15 wallets (73% win rate) buying $TOKEN
                              ↓
✅ Alert sent to your Telegram in <10 seconds
                              ↓
✅ You buy 5 minutes BEFORE the pump
                              ↓
✅ Token pumps 3x in 30 minutes
                              ↓
                        💰 PROFIT
```

> **The 5-15 minute time advantage is the difference between profit and loss.**

---

## 🔄 How It Works

### Cluster Detection Pipeline

```mermaid
flowchart LR
    A[Monitor Service] --> B[Fetch Transactions]
    B --> C[Run Detection]
    C --> D[Score Clusters]
    D --> E{Score ≥ 50?}
    E -->|Yes| F[Save to DB]
    E -->|No| G[Log Only]
    F --> H[Get Token Data]
    H --> I[Send Alert]
    I --> J[Auto-Trade?]
    J -->|Yes| K[Execute]
    J -->|No| L[Manual]
```

### Detailed Detection Flow

```mermaid
flowchart TD
    subgraph Collection["📡 Data Collection"]
        A[Monitor Service<br/>Every 60s] --> B[Fetch Solana TXs]
        B --> C[Parse Data]
    end

    subgraph Detection["🧠 Intelligence"]
        C --> D{3 Algorithms}
        D --> E[Temporal]
        D --> F[Amount]
        D --> G[Accumulation]
        E --> H[Aggregate]
        F --> H
        G --> H
    end

    subgraph Scoring["📊 Scoring"]
        H --> I[Calculate Score]
        I --> J{Score ≥ 50?}
        J -->|No| K[Monitor]
        J -->|Yes| L[Save]
    end

    subgraph Action["⚡ Action"]
        L --> M[DexScreener]
        M --> N{Score ≥ 70?}
        N -->|Yes| O[STRONG BUY]
        N -->|No| P[BUY]
        O --> Q[Alert]
        P --> Q
    end

    style O fill:#22c55e,color:#fff
    style P fill:#3b82f6,color:#fff
```

### Scoring System

| Score | Signal | Action |
|:-----:|:------:|:-------|
| **70-100** | 🟢 STRONG_BUY | Immediate action recommended |
| **50-69** | 🔵 BUY | Monitor for entry point |
| **0-49** | ⚪ MONITOR | Track for potential movement |

**Score Components:**
- Base score from wallet count
- Bonus for smart money wallets (60%+ win rate)
- Bonus for high total volume
- Bonus for tight time clustering (<5 min)
- Bonus for amount similarity (coordinated)

---

## 🏗 System Architecture

### High-Level Overview

```mermaid
graph TB
    subgraph External["External Services"]
        SOL[(Solana RPC)]
        DEX[(DexScreener)]
        JITO[(Jito Engine)]
        TG[(Telegram)]
    end

    subgraph API["API Layer"]
        MAIN[Flask Server]
        HEALTH["/health"]
        TOKEN["/api/token"]
        CLUSTER["/api/clusters"]
    end

    subgraph Services["Background Services"]
        MON[Monitor]
        GEYSER[Geyser]
        KOL[KOL Watcher]
    end

    subgraph Trading["Trading Engine"]
        EXEC[Executor]
        POS[Position Mgr]
        SNIPE[Sniper]
    end

    subgraph Intel["Intelligence"]
        CLUST[Clustering]
        SAFETY[Safety Check]
    end

    subgraph Storage["Storage"]
        DB[(SQLite)]
        CACHE[(Cache)]
    end

    SOL <--> MAIN
    SOL <--> MON
    DEX <--> MAIN
    JITO <--> EXEC
    TG <--> MON

    MON --> CLUST
    CLUST --> DB
    EXEC --> POS
    SAFETY --> EXEC

    style CLUST fill:#8b5cf6,color:#fff
    style EXEC fill:#f59e0b,color:#fff
    style SAFETY fill:#ef4444,color:#fff
```

### Service Communication

```mermaid
sequenceDiagram
    participant U as User
    participant T as Telegram
    participant M as Monitor
    participant C as Clustering
    participant D as DexScreener
    participant S as Solana

    loop Every 60s
        M->>S: Fetch TXs
        S-->>M: Data
        M->>C: Analyze
        C-->>M: Clusters
        
        alt Score ≥ 50
            M->>D: Get token
            D-->>M: Metrics
            M->>T: Alert
            T-->>U: 🔔 Notification
        end
    end
```

---

## ✨ Features

### 🧠 Smart Money Detection

Three sophisticated algorithms detect coordinated wallet activity:

```mermaid
graph LR
    subgraph Algorithms
        A[⏱️ Temporal]
        B[💰 Amount]
        C[📈 Accumulation]
    end

    subgraph Metrics
        D[Wallet Count]
        E[Smart Money %]
        F[Volume]
        G[Time Window]
    end

    A --> D
    A --> G
    B --> F
    C --> E

    D --> H[🎯 Score]
    E --> H
    F --> H
    G --> H

    style H fill:#22c55e,color:#fff
```

| Algorithm | What It Detects | Signal Strength |
|:----------|:----------------|:---------------:|
| **Temporal** | Wallets buying within 5-min windows | High |
| **Amount Similarity** | Coordinated bot/insider buys | Medium |
| **Early Accumulation** | Mass buying before volume spike | High |

### 🚀 Graduation Detection

```mermaid
flowchart LR
    A[Pump.fun Token] -->|Migration| B[Raydium Pool]
    B --> C{Monitor 24-48h}
    C --> D[2-5x Pump<br/>75% Success]
    D --> E[🔔 STRONG BUY]

    style E fill:#22c55e,color:#fff
```

### 🛡️ Safety System

```mermaid
flowchart TD
    A[Token] --> B{Liquidity ≥ $5K?}
    B -->|No| X[❌ Reject]
    B -->|Yes| C{Pool Age OK?}
    C -->|No| X
    C -->|Yes| D{Price Stable?}
    D -->|No| X
    D -->|Yes| E[Risk Check]

    E --> F[TokenSniffer]
    E --> G[RugCheck]
    E --> H[GoPlus]

    F --> I{All Pass?}
    G --> I
    H --> I

    I -->|No| X
    I -->|Yes| J[✅ Safe]

    style J fill:#22c55e,color:#fff
    style X fill:#ef4444,color:#fff
```

### ⚡ Execution Paths

```mermaid
flowchart TD
    A[Trade Signal] --> B{Jito?}
    
    B -->|Yes| C[Jito Bundle]
    C --> D{Success?}
    D -->|Yes| OK[✅ Done]
    D -->|No| E[Fallback]
    
    B -->|No| F{Raydium?}
    F -->|Yes| G[Direct AMM]
    G --> H{Success?}
    H -->|Yes| OK
    H -->|No| I[Jupiter]
    
    F -->|No| I
    E --> F
    I --> J{Success?}
    J -->|Yes| OK
    J -->|No| K[Retry]
    K --> B

    style OK fill:#22c55e,color:#fff
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Solana RPC endpoint
- Telegram Bot Token (optional)

### Docker (Recommended)

```bash
# Clone
git clone https://github.com/yourusername/pumpfun-intelligence.git
cd pumpfun-intelligence

# Configure
cp backend/.env.example backend/.env
# Edit .env with your settings

# Start
docker-compose up -d

# Logs
docker-compose logs -f
```

### Manual Installation

```bash
# Clone
git clone https://github.com/yourusername/pumpfun-intelligence.git
cd pumpfun-intelligence/backend

# Virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac

# Install
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env

# Run API
python src/main_integrated.py

# Run Monitor (new terminal)
python src/monitor_service.py
```

### Verify

```bash
curl http://localhost:5000/health
```

```json
{
  "status": "healthy",
  "features": {
    "database": true,
    "dexscreener": true,
    "clustering": true,
    "telegram": true
  }
}
```

---

## ⚙️ Configuration

### Environment Variables

```mermaid
mindmap
  root((Config))
    Core
      SOLANA_RPC_URL
      WALLET_KEYPAIR_PATH
    Features
      AUTO_TRADE_ENABLED
      DRY_RUN
      ENABLE_JITO_BUNDLES
    Safety
      ENABLE_TOKEN_SAFETY_CHECKS
      REQUIRE_MINT_RENOUNCED
    Trading
      MAX_TRADE_SOL
      TAKE_PROFIT_PCT
      STOP_LOSS_PCT
    Alerts
      TELEGRAM_BOT_TOKEN
      TELEGRAM_CHAT_ID
```

### Core Settings

```bash
# RPC
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
FALLBACK_RPC_1=https://backup-rpc.com

# Wallet (choose one)
WALLET_KEYPAIR_PATH=/path/to/keypair.json
WALLET_PRIVATE_KEY=base58_key
```

### Feature Flags

```bash
AUTO_TRADE_ENABLED=true    # Auto trading
DRY_RUN=false              # Simulate only
KILL_SWITCH=false          # Emergency stop
ENABLE_JITO_BUNDLES=false  # Fast execution
ENABLE_RAYDIUM_DIRECT=true # Direct swaps
```

### Safety Settings

```bash
ENABLE_TOKEN_SAFETY_CHECKS=true
REQUIRE_MINT_RENOUNCED=true
REQUIRE_FREEZE_RENOUNCED=true
MIN_LIQUIDITY_USD=5000
MAX_PRICE_IMPACT_BPS=500
```

### Trading Parameters

```bash
MAX_TRADE_SOL=1.0
DEFAULT_SLIPPAGE_BPS=100
TAKE_PROFIT_PCT=100    # 2x target
STOP_LOSS_PCT=25       # 25% max loss
TRAILING_STOP_PCT=15   # Trail by 15%
MAX_HOLD_MINUTES=120
```

### Telegram Setup

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

**How to get credentials:**

1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy bot token
3. Message your bot
4. Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Copy `chat_id`

---

## 📡 API Reference

### Endpoints

```mermaid
graph LR
    subgraph Health
        A[GET /health]
        B[GET /metrics]
    end

    subgraph Analysis
        C[GET /api/wallet/:addr]
        D[GET /api/token/:addr]
        E[GET /api/smart-money]
    end

    subgraph Clusters
        F[POST /api/clusters/detect]
        G[GET /api/clusters/active]
    end

    subgraph Alerts
        H[POST /api/telegram/alert/cluster]
    end
```

### Reference

| Method | Endpoint | Description |
|:------:|:---------|:------------|
| GET | `/health` | Health check |
| GET | `/api/wallet/<addr>` | Wallet analysis |
| GET | `/api/token/<addr>` | Token data |
| POST | `/api/clusters/detect` | Detect clusters |
| GET | `/api/clusters/active` | Active clusters |
| GET | `/api/smart-money` | Top wallets |
| GET | `/metrics` | Prometheus metrics |

### Examples

```bash
# Token analysis
curl http://localhost:5000/api/token/TOKEN_ADDRESS

# Detect clusters
curl -X POST http://localhost:5000/api/clusters/detect \
  -H "Content-Type: application/json" \
  -d '{"hours": 1}'

# Active clusters
curl http://localhost:5000/api/clusters/active

# Smart money
curl "http://localhost:5000/api/smart-money?limit=50"
```

---

## 🔄 Trading Flows

### Auto-Trade Decision

```mermaid
flowchart TD
    A[Cluster Found] --> B{Score ≥ 70?}
    B -->|No| C[Alert Only]
    B -->|Yes| D{Auto Enabled?}
    
    D -->|No| C
    D -->|Yes| E{Safety OK?}
    
    E -->|No| F[⚠️ Block]
    E -->|Yes| G{Liquidity OK?}
    
    G -->|No| F
    G -->|Yes| H{Under Cap?}
    
    H -->|No| I[Skip]
    H -->|Yes| J[Size Position]
    
    J --> K[Execute]
    K --> L{Success?}
    
    L -->|Yes| M[✅ Open Position]
    L -->|No| N[Retry/Fail]

    M --> O[Start Manager]

    style M fill:#22c55e,color:#fff
    style F fill:#f59e0b,color:#fff
```

### Position Exit Logic

```mermaid
flowchart TD
    A[Position Manager] --> B{Check Price}
    
    B --> C{Hit TP?}
    C -->|Yes| D[🎯 Take Profit]
    
    C -->|No| E{Hit SL?}
    E -->|Yes| F[🛑 Stop Loss]
    
    E -->|No| G{Trail Active?}
    G -->|Yes| H{New High?}
    H -->|Yes| I[Update Trail]
    H -->|No| J{Trail Hit?}
    J -->|Yes| K[📉 Trail Exit]
    
    G -->|No| L{Time Limit?}
    J -->|No| L
    I --> L
    
    L -->|Yes| M[⏰ Time Exit]
    L -->|No| N{Rug Signal?}
    
    N -->|Yes| O[🚨 Emergency]
    N -->|No| B

    D --> P[Log PnL]
    F --> P
    K --> P
    M --> P
    O --> P

    style D fill:#22c55e,color:#fff
    style F fill:#ef4444,color:#fff
    style O fill:#ef4444,color:#fff
```

---

## 💾 Database Schema

```mermaid
erDiagram
    WALLETS ||--o{ CLUSTER_WALLETS : participates
    WALLETS ||--o{ TRANSACTIONS : executes
    TOKENS ||--o{ CLUSTERS : has
    TOKENS ||--o{ TRANSACTIONS : involves
    CLUSTERS ||--o{ CLUSTER_WALLETS : contains

    WALLETS {
        int id PK
        string address UK
        int total_transactions
        int win_rate
        bool is_smart_money
        timestamp last_active
    }
    
    TOKENS {
        int id PK
        string address UK
        string symbol
        string name
        int price_usd
        int liquidity_usd
        int volume_24h
    }
    
    CLUSTERS {
        int id PK
        string token_address FK
        string cluster_type
        int wallet_count
        int cluster_score
        string status
    }
    
    CLUSTER_WALLETS {
        int id PK
        int cluster_id FK
        string wallet_address FK
    }
    
    TRANSACTIONS {
        int id PK
        string signature UK
        string wallet_address FK
        string token_address FK
        string tx_type
        int amount_sol
    }
```

---

## 📁 Project Structure

```
pumpfun-intelligence/
├── backend/
│   ├── src/
│   │   ├── main_integrated.py       # 🌐 Flask API
│   │   ├── monitor_service.py       # 👁️ Real-time monitor
│   │   ├── clustering_service.py    # 🧠 Detection engine
│   │   ├── database.py              # 💾 SQLite
│   │   ├── dexscreener_api.py       # 📊 Token data
│   │   ├── telegram_service.py      # 📱 Notifications
│   │   ├── solana_api.py            # 🔗 Blockchain
│   │   ├── executor.py              # ⚡ Trade execution
│   │   ├── position_manager.py      # 💼 Positions
│   │   ├── raydium_direct/          # 🔄 AMM swaps
│   │   │   ├── pool_parser.py
│   │   │   ├── amm_math.py
│   │   │   └── ix_builder.py
│   │   ├── trading/                 # 💰 Trading utils
│   │   │   ├── sizing.py
│   │   │   ├── fee_tuner.py
│   │   │   ├── token_safety.py
│   │   │   └── metrics.py
│   │   └── risk_sources.py          # 🛡️ Risk checks
│   ├── logs/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
├── docker-compose.yaml
└── README.md
```

---

## 💡 Pro Tips

| # | Tip | Why |
|:-:|:----|:----|
| 1 | **Act Fast** | 5-15 minute window |
| 2 | **Trust 70+ Scores** | Strong signals |
| 3 | **Watch Graduations** | Most profitable |
| 4 | **Avoid < $5K Liquidity** | Rug risk |
| 5 | **Follow Smart Money** | 60%+ win rates |
| 6 | **Use Telegram Alerts** | Don't miss trades |
| 7 | **Take Profits at 2-3x** | Don't be greedy |
| 8 | **Always Use Stop Loss** | Protect capital |
| 9 | **Start Small** | Test first |
| 10 | **Track Everything** | Analyze performance |

---

## 📈 Roadmap

```mermaid
timeline
    title Development Progress
    
    section Done ✅
        Core : Cluster detection
             : DexScreener
             : Telegram alerts
             : Database
             : Jito bundles
             : Position manager
    
    section Building 🔄
        Phase 2 : Frontend dashboard
                : Advanced analytics
    
    section Planned 📋
        Phase 3 : Backtesting
                : Mobile app
                : Multi-chain
```

---

## 🤝 Contributing

```mermaid
gitGraph
    commit id: "Fork"
    branch feature
    commit id: "Branch"
    commit id: "Code"
    commit id: "Test"
    checkout main
    merge feature id: "PR"
    commit id: "Merged ✅"
```

1. Fork the repository
2. Create feature branch: `git checkout -b feature/name`
3. Make changes
4. Test thoroughly
5. Submit pull request

---

## ⚠️ Disclaimer

> **Educational and informational purposes only.**
> 
> Cryptocurrency trading involves substantial risk. This platform provides signals and automation but does not guarantee profits.
> 
> **Always:**
> - Do your own research (DYOR)
> - Never invest more than you can afford to lose
> - Test with small amounts first

---

## 📝 License

MIT License - See [LICENSE](LICENSE) file.

---

## 📞 Support

- **Issues**: GitHub Issues
- **Docs**: `/docs` folder
- **Community**: Telegram (coming soon)

---

<div align="center">

**Stop trading blind. Start trading with intelligence.** 🚀💰

*Built with intelligence. Designed for profit.*

</div>
