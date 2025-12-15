import logging
import numpy as np
import pandas as pd

from config.config_manager import ConfigManager
from config.trading_mode import TradingMode
from core.bot_management.event_bus import EventBus, Events
from core.grid_management.grid_manager import GridManager
from core.order_handling.balance_tracker import BalanceTracker
from core.order_handling.order_manager import OrderManager
from core.services.exchange_interface import ExchangeInterface
from strategies.plotter import Plotter
from strategies.trading_performance_analyzer import TradingPerformanceAnalyzer
from .trading_strategy_interface import TradingStrategyInterface

class GridTradingStrategy(TradingStrategyInterface):
    # Set to 0 for Real-Time WebSocket streaming
    TICKER_REFRESH_INTERVAL = 0  

    def __init__(
        self,
        config_manager: ConfigManager,
        event_bus: EventBus,
        exchange_service: ExchangeInterface,
        grid_manager: GridManager,
        order_manager: OrderManager,
        balance_tracker: BalanceTracker,
        trading_performance_analyzer: TradingPerformanceAnalyzer,
        trading_mode: TradingMode,
        trading_pair: str,
        plotter: Plotter | None = None,
    ):
        super().__init__(config_manager, balance_tracker)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.event_bus = event_bus
        self.exchange_service = exchange_service
        self.grid_manager = grid_manager
        self.order_manager = order_manager
        self.trading_performance_analyzer = trading_performance_analyzer
        self.trading_mode = trading_mode
        self.trading_pair = trading_pair
        self.plotter = plotter
        self.data = self._initialize_historical_data()
        self.live_trading_metrics = []
        self._running = True

    def _initialize_historical_data(self) -> pd.DataFrame | None:
        if self.trading_mode != TradingMode.BACKTEST:
            return None
        try:
            timeframe, start_date, end_date = self._extract_config()
            return self.exchange_service.fetch_ohlcv(self.trading_pair, timeframe, start_date, end_date)
        except Exception as e:
            self.logger.error(f"Failed to initialize data for backtest: {e}")
            return None

    def _extract_config(self) -> tuple[str, str, str]:
        timeframe = self.config_manager.get_timeframe()
        start_date = self.config_manager.get_start_date()
        end_date = self.config_manager.get_end_date()
        return timeframe, start_date, end_date

    def initialize_strategy(self):
        self.grid_manager.initialize_grids_and_levels()

    async def stop(self, sell_assets: bool = False):
        self._running = False
        
        if sell_assets and self.trading_mode != TradingMode.BACKTEST:
            self.logger.info("?? Emergency stop triggered: Cancelling orders and liquidating assets.")
            try:
                # 1. Get current price for liquidation estimate
                current_price = await self.exchange_service.get_current_price(self.trading_pair)
                
                # 2. Cancel all pending grid orders
                await self.order_manager.cancel_all_open_orders()
                
                # 3. Sell everything to USDT
                await self.order_manager.liquidate_positions(current_price)
                
            except Exception as e:
                self.logger.error(f"Error during emergency cleanup: {e}")

        await self.exchange_service.close_connection()
        self.logger.info("Trading execution stopped.")

    async def restart(self):
        if not self._running:
            self.logger.info("Restarting trading session.")
            await self.run()

    async def run(self):
        self._running = True
        trigger_price = self.grid_manager.get_trigger_price()

        if self.trading_mode == TradingMode.BACKTEST:
            await self._run_backtest(trigger_price)
            self.logger.info("Ending backtest simulation")
            self._running = False
        else:
            await self._run_live_or_paper_trading(trigger_price)

    async def _run_live_or_paper_trading(self, trigger_price: float):
        self.logger.info(f"Starting {'live' if self.trading_mode == TradingMode.LIVE else 'paper'} trading")
        
        # --- MODIFIED BALANCE SYNC LOGIC ---
        self.logger.info("?? Synchronizing Wallet Balances with Exchange...")
        try:
            balances = await self.exchange_service.get_balance()
            base_currency, quote_currency = self.trading_pair.split('/')
            
            # 1. Get Actual Wallet Balances
            actual_crypto_balance = float(balances.get(base_currency, {}).get('free', 0.0))
            actual_fiat_balance = float(balances.get(quote_currency, {}).get('free', 0.0))
            
            # 2. Get User's Investment Limit
            investment_amount = self.config_manager.get_investment_amount()
            self.logger.info(f"   ?? Wallet Has: {actual_fiat_balance} {quote_currency}")
            self.logger.info(f"   ?? User Allocated: {investment_amount} {quote_currency}")

            # 3. Validate Funds
            if actual_fiat_balance < investment_amount:
                self.logger.error(
                    f"? INSUFFICIENT FUNDS: Wallet has {actual_fiat_balance} {quote_currency}, "
                    f"but strategy requires {investment_amount} {quote_currency}."
                )
                self._running = False
                return

            # 4. Cap the Bot's Balance to the Investment Amount
            # We ignore any extra money in the wallet so the bot doesn't touch it.
            effective_fiat_balance = investment_amount
            
            # NOTE: For safety, we usually start with 0 crypto in the bot's internal tracker 
            # unless we specifically want to use existing bags. 
            # Here we pass the wallet's crypto, but the bot will primarily use the allocated USDT.
            effective_crypto_balance = actual_crypto_balance

            self.logger.info(f"   ? Bot Initialized with: {effective_fiat_balance} {quote_currency} (Capped at investment)")

            res = self.balance_tracker.setup_balances(
                effective_fiat_balance, 
                effective_crypto_balance, 
                self.exchange_service
            )
            if res is not None and hasattr(res, '__await__'):
                await res

        except Exception as e:
            self.logger.error(f"Failed to refresh balances: {e}", exc_info=True)
            self._running = False
            return
        # -----------------------------------

        last_price: float | None = None
        grid_orders_initialized = False

        async def on_ticker_update(current_price):
            nonlocal last_price, grid_orders_initialized
            try:
                if not self._running:
                    self.logger.info("Trading stopped; halting price updates.")
                    return

                account_value = self.balance_tracker.get_total_balance_value(current_price)
                self.live_trading_metrics.append((pd.Timestamp.now(), account_value, current_price))

                grid_orders_initialized = await self._initialize_grid_orders_once(
                    current_price,
                    trigger_price,
                    grid_orders_initialized,
                    last_price,
                )

                if not grid_orders_initialized:
                    last_price = current_price
                    return

                if await self._handle_take_profit_stop_loss(current_price):
                    return

                last_price = current_price

            except Exception as e:
                self.logger.error(f"Error during ticker update: {e}", exc_info=True)

        try:
            await self.exchange_service.listen_to_ticker_updates(
                self.trading_pair,
                on_ticker_update,
                self.TICKER_REFRESH_INTERVAL,
            )
        except Exception as e:
            self.logger.error(f"Error in live/paper trading loop: {e}", exc_info=True)
        finally:
            self.logger.info("Exiting live/paper trading loop.")

    async def _initialize_grid_orders_once(
        self,
        current_price: float,
        trigger_price: float,
        grid_orders_initialized: bool,
        last_price: float | None = None,
    ) -> bool:
        if grid_orders_initialized:
            return True

        self.logger.info(
            f"?? Immediate Start Triggered! Current Price: {current_price} (Grid Center: {trigger_price})"
        )

        self.logger.info("?? Cleaning up any existing open orders before start...")
        try:
            await self.order_manager.cancel_all_open_orders()
        except Exception as e:
            self.logger.warning(f"Cleanup warning: {e}")

        self.grid_manager.update_zones_based_on_price(current_price)

        try:
            await self.order_manager.perform_initial_purchase(current_price)
            self.logger.info("Initial purchase complete. Placing grid orders...")
            await self.order_manager.initialize_grid_orders(current_price)
            return True
            
        except Exception as e:
            self.logger.error(f"?? CRITICAL: Initialization Failed. Stopping Strategy. Error: {e}")
            self._running = False
            return False

    async def _run_backtest(self, trigger_price: float) -> None:
        if self.data is None:
            self.logger.error("No data available for backtesting.")
            return
        self.logger.info("Starting backtest simulation")
        self.data["account_value"] = np.nan
        self.close_prices = self.data["close"].values
        high_prices = self.data["high"].values
        low_prices = self.data["low"].values
        timestamps = self.data.index
        self.data.loc[timestamps[0], "account_value"] = self.balance_tracker.get_total_balance_value(price=self.close_prices[0])
        grid_orders_initialized = False
        last_price = None
        for i, (current_price, high_price, low_price, timestamp) in enumerate(zip(self.close_prices, high_prices, low_prices, timestamps, strict=False)):
            grid_orders_initialized = await self._initialize_grid_orders_once(current_price, trigger_price, grid_orders_initialized, last_price)
            if not grid_orders_initialized:
                self.data.loc[timestamps[i], "account_value"] = self.balance_tracker.get_total_balance_value(price=current_price)
                last_price = current_price
                continue
            await self.order_manager.simulate_order_fills(high_price, low_price, timestamp)
            if await self._handle_take_profit_stop_loss(current_price): break
            self.data.loc[timestamp, "account_value"] = self.balance_tracker.get_total_balance_value(current_price)
            last_price = current_price

    def generate_performance_report(self) -> tuple[dict, list]:
        if self.trading_mode == TradingMode.BACKTEST:
            return self.trading_performance_analyzer.generate_performance_summary(self.data, self.close_prices[0], self.balance_tracker.get_adjusted_fiat_balance(), self.balance_tracker.get_adjusted_crypto_balance(), self.close_prices[-1], self.balance_tracker.total_fees)
        else:
            if not self.live_trading_metrics: return {}, []
            live_data = pd.DataFrame(self.live_trading_metrics, columns=["timestamp", "account_value", "price"])
            live_data.set_index("timestamp", inplace=True)
            return self.trading_performance_analyzer.generate_performance_summary(live_data, live_data.iloc[0]["price"], self.balance_tracker.get_adjusted_fiat_balance(), self.balance_tracker.get_adjusted_crypto_balance(), live_data.iloc[-1]["price"], self.balance_tracker.total_fees)

    def plot_results(self) -> None:
        if self.trading_mode == TradingMode.BACKTEST: self.plotter.plot_results(self.data)
        else: self.logger.info("Plotting is not available for live/paper trading mode.")

    async def _handle_take_profit_stop_loss(self, current_price: float) -> bool:
        if await self._evaluate_tp_or_sl(current_price):
            self.logger.info("Take-profit or stop-loss triggered, ending trading session.")
            await self.event_bus.publish(Events.STOP_BOT, "TP or SL hit.")
            return True
        return False

    async def _evaluate_tp_or_sl(self, current_price: float) -> bool:
        if self.balance_tracker.crypto_balance == 0: return False
        return await self._handle_take_profit(current_price) or await self._handle_stop_loss(current_price)

    async def _handle_take_profit(self, current_price: float) -> bool:
        if self.config_manager.is_take_profit_enabled() and current_price >= self.config_manager.get_take_profit_threshold():
            self.logger.info(f"Take-profit triggered at {current_price}. Executing TP order...")
            await self.order_manager.execute_take_profit_or_stop_loss_order(current_price=current_price, take_profit_order=True)
            return True
        return False

    async def _handle_stop_loss(self, current_price: float) -> bool:
        if self.config_manager.is_stop_loss_enabled() and current_price <= self.config_manager.get_stop_loss_threshold():
            self.logger.info(f"Stop-loss triggered at {current_price}. Executing SL order...")
            await self.order_manager.execute_take_profit_or_stop_loss_order(current_price=current_price, stop_loss_order=True)
            return True
        return False

    def get_formatted_orders(self):
        return self.trading_performance_analyzer.get_formatted_orders()
