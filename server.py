import asyncio
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any

# --- Import Core Modules ---
from core.bot_management.grid_trading_bot import GridTradingBot
from core.bot_management.event_bus import EventBus
from core.bot_management.notification.notification_handler import NotificationHandler
from config.config_validator import ConfigValidator
from adapter.config_adapter import DictConfigManager

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FydEngine")

app = FastAPI()

# Store active bots: { bot_id: { "bot": GridTradingBot, "task": asyncio.Task } }
active_instances: Dict[int, Dict[str, Any]] = {}

# --- Data Models ---
class StrategyConfig(BaseModel):
    upper_price: float
    lower_price: float
    grids: int
    investment: float
    spacing: Optional[str] = "geometric"

class BotRequest(BaseModel):
    bot_id: int
    user_id: int
    exchange: str
    pair: str
    api_key: str
    api_secret: str
    mode: str = "live"  # live, paper
    strategy: StrategyConfig

class BacktestRequest(BaseModel):
    exchange: str
    pair: str
    startDate: str
    endDate: str
    capital: float
    upperPrice: float
    lowerPrice: float
    gridSize: int
    timeframe: str = "1h"

# --- Helper: Map Request to Bot Config ---
def create_config(exchange, pair, api_key, api_secret, mode, strategy_settings, trading_settings):
    base, quote = pair.split('/')
    return {
        "exchange": {
            "name": exchange.lower(),
            "trading_fee": 0.001,
            "trading_mode": mode
        },
        "credentials": {
            "api_key": api_key,
            "api_secret": api_secret
        },
        "pair": {
            "base_currency": base,
            "quote_currency": quote
        },
        "trading_settings": trading_settings,
        "grid_strategy": {
            "type": "simple_grid",
            "spacing": strategy_settings.get('spacing', 'geometric'),
            "num_grids": strategy_settings['grids'],
            "range": {
                "top": strategy_settings['upper_price'],
                "bottom": strategy_settings['lower_price']
            }
        },
        "risk_management": {
            "take_profit": {"enabled": False},
            "stop_loss": {"enabled": False}
        },
        "logging": {
            "log_level": "INFO",
            "log_to_file": False
        }
    }

# --- API Endpoints ---

@app.get("/")
def health_check():
    return {"status": "online", "active_bots": len(active_instances)}

@app.post("/start")
async def start_bot(req: BotRequest):
    if req.bot_id in active_instances:
        raise HTTPException(status_code=400, detail="Bot already running")

    # Prepare Config
    trading_settings = {
        "initial_balance": req.strategy.investment,
        "timeframe": "1m"
    }
    
    strategy_settings = {
        "grids": req.strategy.grids,
        "upper_price": req.strategy.upper_price,
        "lower_price": req.strategy.lower_price,
        "spacing": req.strategy.spacing
    }

    mode_str = "paper_trading" if req.mode == 'paper' else "live"
    
    config_dict = create_config(
        req.exchange, req.pair, req.api_key, req.api_secret, 
        mode_str, strategy_settings, trading_settings
    )

    try:
        # Initialize Components
        validator = ConfigValidator()
        config_manager = DictConfigManager(config_dict, validator)
        event_bus = EventBus()
        # Live bots might need notifications, passed as None for now (can be expanded later)
        notification_handler = NotificationHandler(event_bus, None, config_manager.get_trading_mode())

        bot = GridTradingBot(
            config_path="memory",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=event_bus,
            no_plot=True
        )

        # Start Async Task
        task = asyncio.create_task(bot.run())
        
        active_instances[req.bot_id] = {
            "bot": bot,
            "task": task,
            "event_bus": event_bus
        }

        logger.info(f"Bot {req.bot_id} started successfully.")
        return {"status": "started", "bot_id": req.bot_id}

    except Exception as e:
        logger.error(f"Failed to start bot {req.bot_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop/{bot_id}")
async def stop_bot(bot_id: int):
    if bot_id not in active_instances:
        raise HTTPException(status_code=404, detail="Bot not found")

    instance = active_instances[bot_id]
    bot = instance["bot"]
    
    # Graceful Shutdown
    await bot._stop()
    
    # Wait for the background task to finish
    try:
        await asyncio.wait_for(instance["task"], timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning(f"Bot {bot_id} stop timed out, forcing removal.")
    except Exception as e:
        logger.error(f"Error stopping bot {bot_id}: {e}")

    del active_instances[bot_id]
    return {"status": "stopped"}

@app.post("/backtest")
async def run_backtest(req: BacktestRequest):
    """
    Executes a backtest by initializing a GridTradingBot in 'backtest' mode.
    """
    try:
        # 1. Prepare Configuration
        trading_settings = {
            "initial_balance": req.capital,
            "timeframe": req.timeframe,
            "period": {
                "start_date": req.startDate,
                "end_date": req.endDate
            },
            # historical_data_file must be None to force fetching from Exchange via CCXT
            "historical_data_file": None 
        }

        strategy_settings = {
            "grids": req.gridSize,
            "upper_price": req.upperPrice,
            "lower_price": req.lowerPrice,
            "spacing": "geometric"
        }

        # API keys are passed as dummy values since backtesting primarily needs public data,
        # but the bot structure requires them to be present.
        config_dict = create_config(
            req.exchange, req.pair, "dummy_key", "dummy_secret", 
            "backtest", strategy_settings, trading_settings
        )

        # 2. Initialize Bot Components
        validator = ConfigValidator()
        config_manager = DictConfigManager(config_dict, validator)
        event_bus = EventBus()
        # Notifications disabled for backtest
        notification_handler = NotificationHandler(event_bus, None, config_manager.get_trading_mode())

        bot = GridTradingBot(
            config_path="memory",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=event_bus,
            no_plot=True # Important: Disable plotting for API calls
        )

        # 3. Run Sync (Await the result directly)
        logger.info(f"Starting backtest for {req.pair}...")
        result = await bot.run()
        
        return result

    except Exception as e:
        logger.error(f"Backtest failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
