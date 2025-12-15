Here is a complete, professional `README.md` file tailored for your **Fydblock** project. It highlights the advanced features we just implemented (Database Persistence, Smart Recovery, OKX Pagination) and provides clear setup instructions.

You can copy-paste this directly into your GitHub repository.

-----

# Fydblock Grid Trading Engine 🚀

**Fydblock** is a high-performance, self-hosted **Grid Trading Bot** engine designed for cryptocurrency markets. Built for stability and precision, it automates "buy low, sell high" strategies with enterprise-grade features like **SQLite persistence**, **anti-flicker protection**, and **multi-exchange support** (Binance, OKX, Bybit).

Unlike simple bots, Fydblock features a robust **Integrity Watchdog** that constantly reconciles local state with the exchange to prevent order drift, duplicates, or "zombie" orders.

-----

## 🌟 Key Features

  * **🛡️ Robust Persistence:** Uses **SQLite** to track every order state locally. Your bot remembers its exact grid positions even after a crash or restart.
  * **⚡ Anti-Flicker & Integrity Check:** A smart background "Watchdog" loop constantly verifies order status against the exchange, automatically restoring missing orders or cancelling zombies—without spamming the API.
  * **🧹 Smart Clean Start:** Automatically detects and wipes old orders from previous sessions before starting, ensuring a clean grid every time.
  * **💸 Real-Time Profit Sync:** Automatically calculates gross/net profit and syncs data to your backend dashboard.
  * **🔄 Multi-Exchange Support:** Built on **CCXT Pro**, supporting:
      * **Binance** (Spot & Futures)
      * **OKX** (Deep pagination support included)
      * **Bybit**
      * **Kraken**
  * **📉 Dynamic Grid Management:** Automatically calculates geometric or arithmetic grids based on your investment cap and available balance.

-----

## 🛠️ Installation

### 1\. Prerequisites

  * **Python 3.10+**
  * **Redis** (Required for the Event Bus)
  * **Node.js Backend** (Optional, for dashboard data sync)

### 2\. Clone & Install

```bash
git clone https://github.com/yourusername/fydblock-engine.git
cd fydblock-engine

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3\. Database Setup

No manual setup required\! The bot automatically initializes `bot_data.db` (SQLite) in the `core/storage/` directory on the first run.

-----

## ⚙️ Configuration

Configure your bot using environment variables in a `.env` file:

```env
# Exchange Credentials
EXCHANGE_API_KEY=your_api_key
EXCHANGE_SECRET_KEY=your_secret_key
EXCHANGE_PASSWORD=your_passphrase  # Required for OKX/KuCoin

# Bot Settings
TRADING_MODE=paper_trading       # or 'live' or 'backtest'
EXCHANGE_NAME=okx                # binance, bybit, okx
BASE_CURRENCY=SOL
QUOTE_CURRENCY=USDT
INITIAL_BALANCE=1000.0           # Investment Cap
```

-----

## 🚀 Usage

### Run the Bot

```bash
python main.py
```

### Docker Support

Deploy easily with Docker Compose:

```bash
docker-compose up --build -d
```

-----

## 🏗️ Architecture & Core Components

The system is built on a modular **Event-Driven Architecture**:

1.  **`GridTradingBot`**: The main controller. Manages the lifecycle (Start/Stop/Restart) and initializes components.
2.  **`OrderManager`**: The "Brain". Decides when to place orders. It checks the **SQLite DB** first to prevent duplicates.
3.  **`LiveExchangeService`**: Handles low-level API calls. Includes smart **pagination loops** to fetch \>100 orders from exchanges like OKX without hitting limit errors.
4.  **`BalanceTracker`**: Keeps real-time track of Fiat/Crypto holdings, accounting for exchange fees.
5.  **`Integrity Watchdog`**: A background task that runs every 5 seconds to reconcile the DB state with the Exchange state.

-----

## 🔧 Troubleshooting Common Issues

### 1\. "Parameter limit error" on OKX

  * **Cause:** Trying to fetch too many orders in a single API call (e.g., asking for 500 when the limit is 100).
  * **Solution:** Fydblock's `LiveExchangeService` now includes a custom pagination loop that fetches orders in batches of 100 until all data is retrieved.

### 2\. "Duplicate Orders" (e.g., 120 orders instead of 80)

  * **Cause:** Bot restarts without clearing the previous session's orders.
  * **Solution:** The `OrderManager` performs an **Aggressive Clean Start**, verifying 0 open orders on the exchange before placing a single new grid.

### 3\. "Insufficient Funds" Spam

  * **Cause:** The Integrity Watchdog trying to refill gaps when the wallet is empty.
  * **Solution:** The reconciliation logic now checks `has_fiat` and `has_crypto` flags before attempting recovery, preventing error loops.

-----

## 🤝 Contributing

Contributions are welcome\! Please open an issue or submit a Pull Request.

1.  Fork the repo
2.  Create your feature branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request

-----

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
