import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any

from adapter.config_adapter import DictConfigManager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from config.config_validator import ConfigValidator
from core.bot_management.event_bus import EventBus

# --- Import Core Modules ---
from core.bot_management.grid_trading_bot import GridTradingBot
from core.bot_management.notification.notification_handler import NotificationHandler

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FydEngine")

# Store active bots: { bot_id: { "bot": GridTradingBot, "task": asyncio.Task } }
active_instances: dict[int, dict[str, Any]] = {}


# --- Data Models ---
class StrategyConfig(BaseModel):
    upper_price: float
    lower_price: float
    grids: int
    spacing: str | None = "geometric"
    # Optional fallback for compatibility
    investment: float | None = None


class BotRequest(BaseModel):
    bot_id: int
    user_id: int
    exchange: str
    pair: str
    api_key: str
    api_secret: str
    passphrase: str | None = None
    mode: str = "live"
    strategy: StrategyConfig
    # Primary location for investment
    investment: float = 0.0


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
def create_config(exchange, pair, api_key, api_secret, passphrase, mode, strategy_settings, trading_settings):
    base, quote = pair.split("/")
    return {
        "exchange": {"name": exchange.lower(), "trading_fee": 0.001, "trading_mode": mode},
        "credentials": {"api_key": api_key, "api_secret": api_secret, "password": passphrase},
        "pair": {"base_currency": base, "quote_currency": quote},
        "trading_settings": trading_settings,
        # Inject investment at root for ConfigManager
        "investment": trading_settings.get("initial_balance", 0.0),
        "grid_strategy": {
            "type": "simple_grid",
            "spacing": strategy_settings.get("spacing", "geometric"),
            "num_grids": strategy_settings["grids"],
            "range": {"top": strategy_settings["upper_price"], "bottom": strategy_settings["lower_price"]},
            # Also inject here for safety
            "investment": trading_settings.get("initial_balance", 0.0),
        },
        "risk_management": {
            "take_profit": {"enabled": False, "threshold": 0.0},
            "stop_loss": {"enabled": False, "threshold": 0.0},
        },
        "logging": {"log_level": "INFO", "log_to_file": False},
    }


# --- Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    logger.info("?? Server shutting down. Cleaning up active bots...")
    tasks = []
    for bot_id, instance in active_instances.items():
        bot = instance["bot"]
        logger.info(f"Stopping Bot {bot_id} and liquidating assets...")
        tasks.append(bot._stop(sell_assets=True))

    if tasks:
        await asyncio.gather(*tasks)
    logger.info("All bots stopped and assets liquidated.")


app = FastAPI(lifespan=lifespan)

# --- API Endpoints ---


@app.get("/")
def health_check():
    return {"status": "online", "active_bots": len(active_instances)}


@app.post("/start")
async def start_bot(req: BotRequest):
    if req.bot_id in active_instances:
        raise HTTPException(status_code=400, detail="Bot already running")

    # --- 1. RESOLVE INVESTMENT AMOUNT ---
    final_investment = req.investment

    # Fallback: If root investment is 0, check inside strategy (backward compatibility)
    if final_investment == 0 and req.strategy.investment is not None:
        final_investment = req.strategy.investment

    logger.info(f"?? Starting Bot {req.bot_id} | Investment: {final_investment} USDT")

    # --- 2. Prepare Config ---
    trading_settings = {
        "initial_balance": final_investment,
        "investment": final_investment,
        "timeframe": "1m",
        "period": {"start_date": "2024-01-01T00:00:00Z", "end_date": "2070-01-01T00:00:00Z"},
        "historical_data_file": None,
    }

    strategy_settings = {
        "grids": req.strategy.grids,
        "upper_price": req.strategy.upper_price,
        "lower_price": req.strategy.lower_price,
        "spacing": req.strategy.spacing,
    }

    mode_str = "paper_trading" if req.mode == "paper" else "live"

    config_dict = create_config(
        req.exchange,
        req.pair,
        req.api_key,
        req.api_secret,
        req.passphrase,
        mode_str,
        strategy_settings,
        trading_settings,
    )

    try:
        # Initialize Components
        validator = ConfigValidator()
        # This will fail if initial_balance is missing or invalid
        config_manager = DictConfigManager(config_dict, validator)

        event_bus = EventBus()
        notification_handler = NotificationHandler(event_bus, None, config_manager.get_trading_mode())

        bot = GridTradingBot(
            config_path="memory",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=event_bus,
            no_plot=True,
            bot_id=req.bot_id,
        )

        task = asyncio.create_task(bot.run())

        active_instances[req.bot_id] = {"bot": bot, "task": task, "event_bus": event_bus}

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

    await bot._stop(sell_assets=True)

    try:
        await asyncio.wait_for(instance["task"], timeout=5.0)
    except TimeoutError:
        logger.warning(f"Bot {bot_id} stop timed out, forcing removal.")
    except Exception as e:
        logger.error(f"Error stopping bot {bot_id}: {e}")

    del active_instances[bot_id]
    return {"status": "stopped"}


@app.post("/backtest")
async def run_backtest(req: BacktestRequest):
    try:
        trading_settings = {
            "initial_balance": req.capital,
            "investment": req.capital,
            "timeframe": req.timeframe,
            "period": {"start_date": req.startDate, "end_date": req.endDate},
            "historical_data_file": None,
        }

        strategy_settings = {
            "grids": req.gridSize,
            "upper_price": req.upperPrice,
            "lower_price": req.lowerPrice,
            "spacing": "geometric",
        }

        config_dict = create_config(
            req.exchange, req.pair, "dummy_key", "dummy_secret", None, "backtest", strategy_settings, trading_settings
        )

        validator = ConfigValidator()
        config_manager = DictConfigManager(config_dict, validator)
        event_bus = EventBus()
        notification_handler = NotificationHandler(event_bus, None, config_manager.get_trading_mode())

        bot = GridTradingBot(
            config_path="memory",
            config_manager=config_manager,
            notification_handler=notification_handler,
            event_bus=event_bus,
            no_plot=True,
        )

        logger.info(f"Starting backtest for {req.pair}...")
        result = await bot.run()
        return result

    except Exception as e:
        logger.error(f"Backtest failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
