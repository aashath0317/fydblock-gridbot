from datetime import UTC, datetime
import logging

import pandas as pd

from config.trading_mode import TradingMode
from core.bot_management.event_bus import EventBus, Events
from core.bot_management.notification.notification_content import NotificationType
from core.bot_management.notification.notification_handler import NotificationHandler
from strategies.strategy_type import StrategyType

from ..grid_management.grid_level import GridLevel
from ..grid_management.grid_manager import GridManager
from ..order_handling.balance_tracker import BalanceTracker
from ..order_handling.order_book import OrderBook
from ..validation.order_validator import OrderValidator
from .exceptions import OrderExecutionFailedError
from .execution_strategy.order_execution_strategy_interface import (
    OrderExecutionStrategyInterface,
)
from .order import Order, OrderSide, OrderStatus


class OrderManager:
    def __init__(
        self,
        grid_manager: GridManager,
        order_validator: OrderValidator,
        balance_tracker: BalanceTracker,
        order_book: OrderBook,
        event_bus: EventBus,
        order_execution_strategy: OrderExecutionStrategyInterface,
        notification_handler: NotificationHandler,
        trading_mode: TradingMode,
        trading_pair: str,
        strategy_type: StrategyType,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.grid_manager = grid_manager
        self.order_validator = order_validator
        self.balance_tracker = balance_tracker
        self.order_book = order_book
        self.event_bus = event_bus
        self.order_execution_strategy = order_execution_strategy
        self.notification_handler = notification_handler
        self.trading_mode: TradingMode = trading_mode
        self.trading_pair = trading_pair
        self.strategy_type: StrategyType = strategy_type
        self.event_bus.subscribe(Events.ORDER_FILLED, self._on_order_filled)
        self.event_bus.subscribe(Events.ORDER_CANCELLED, self._on_order_cancelled)

    async def initialize_grid_orders(
        self,
        current_price: float,
    ):
        """
        Places initial buy orders for grid levels below the current price.
        """
        # --- 1. Place BUY Orders ---
        for price in self.grid_manager.sorted_buy_grids:
            if price >= current_price:
                self.logger.info(f"Skipping grid level at price: {price} for BUY order: Above current price.")
                continue

            grid_level = self.grid_manager.grid_levels[price]
            total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price)

            if self.grid_manager.can_place_order(grid_level, OrderSide.BUY):
                try:
                    adjusted_buy_order_quantity = self.order_validator.adjust_and_validate_buy_quantity(
                        balance=self.balance_tracker.balance,
                        order_quantity=order_quantity,
                        price=price,
                    )

                    self.logger.info(
                        f"Placing initial buy limit order at grid level {price} for "
                        f"{adjusted_buy_order_quantity} {self.trading_pair}.",
                    )
                    
                    order = await self.order_execution_strategy.execute_limit_order(
                        order_side=OrderSide.BUY,
                        pair=self.trading_pair,
                        amount=adjusted_buy_order_quantity,
                        price=price,
                    )

                    if order is None:
                        self.logger.error(f"Failed to place buy order at {price}: No order returned.")
                        continue

                    self.balance_tracker.reserve_funds_for_buy(adjusted_buy_order_quantity * price)
                    self.grid_manager.mark_order_pending(grid_level, order)
                    self.order_book.add_order(order, grid_level)

                except OrderExecutionFailedError as e:
                    self.logger.error(f"Failed to initialize buy order at grid level {price} - {e!s}", exc_info=True)
                    if "InsufficientFunds" in str(e) or "InsufficientCrypto" in str(e):
                        raise e 
                    
                except Exception as e:
                    self.logger.error(
                        f"Unexpected error during buy order initialization at grid level {price}: {e}",
                        exc_info=True,
                    )

        # --- 2. Place SELL Orders ---
        for price in self.grid_manager.sorted_sell_grids:
            if price <= current_price:
                self.logger.info(
                    f"Skipping grid level at price: {price} for SELL order: Below or equal to current price.",
                )
                continue

            grid_level = self.grid_manager.grid_levels[price]
            total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
            order_quantity = self.grid_manager.get_order_size_for_grid_level(total_balance_value, current_price)

            if self.grid_manager.can_place_order(grid_level, OrderSide.SELL):
                try:
                    adjusted_sell_order_quantity = self.order_validator.adjust_and_validate_sell_quantity(
                        crypto_balance=self.balance_tracker.crypto_balance,
                        order_quantity=order_quantity,
                    )

                    self.logger.info(
                        f"Placing initial sell limit order at grid level {price} for "
                        f"{adjusted_sell_order_quantity} {self.trading_pair}.",
                    )
                    
                    order = await self.order_execution_strategy.execute_limit_order(
                        order_side=OrderSide.SELL,
                        pair=self.trading_pair,
                        amount=adjusted_sell_order_quantity,
                        price=price,
                    )

                    if order is None:
                        self.logger.error(f"Failed to place sell order at {price}: No order returned.")
                        continue

                    self.balance_tracker.reserve_funds_for_sell(adjusted_sell_order_quantity)
                    self.grid_manager.mark_order_pending(grid_level, order)
                    self.order_book.add_order(order, grid_level)

                except OrderExecutionFailedError as e:
                    self.logger.error(f"Failed to initialize sell order at grid level {price} - {e!s}", exc_info=True)
                    if "InsufficientFunds" in str(e) or "InsufficientCrypto" in str(e):
                        raise e

                except Exception as e:
                    self.logger.error(
                        f"Unexpected error during sell order initialization at grid level {price}: {e}",
                        exc_info=True,
                    )

    async def _on_order_cancelled(
        self,
        order: Order,
    ) -> None:
        await self.notification_handler.async_send_notification(
            NotificationType.ORDER_CANCELLED,
            order_details=str(order),
        )

    async def _on_order_filled(
        self,
        order: Order,
    ) -> None:
        try:
            grid_level = self.order_book.get_grid_level_for_order(order)

            if not grid_level:
                self.logger.warning(
                    f"Could not handle Order completion - No grid level found for the given filled order {order}",
                )
                return

            await self._handle_order_completion(order, grid_level)

        except OrderExecutionFailedError as e:
            self.logger.error(f"Failed while handling filled order - {e!s}", exc_info=True)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
            )

        except Exception as e:
            self.logger.error(f"Error while handling filled order {order.identifier}: {e}", exc_info=True)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_FAILED,
                error_details=f"Failed handling filled order. {e}",
            )

    async def _handle_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        if order.side == OrderSide.BUY:
            await self._handle_buy_order_completion(order, grid_level)

        elif order.side == OrderSide.SELL:
            await self._handle_sell_order_completion(order, grid_level)

    async def _handle_buy_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        self.logger.info(f"Buy order completed at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, OrderSide.BUY)
        paired_sell_level = self.grid_manager.get_paired_sell_level(grid_level)

        if paired_sell_level and self.grid_manager.can_place_order(paired_sell_level, OrderSide.SELL):
            await self._place_sell_order(grid_level, paired_sell_level, order.filled)
        else:
            self.logger.warning(
                f"No valid sell grid level found for buy grid level {grid_level}. Skipping sell order placement.",
            )

    async def _handle_sell_order_completion(
        self,
        order: Order,
        grid_level: GridLevel,
    ) -> None:
        self.logger.info(f"Sell order completed at grid level {grid_level}.")
        self.grid_manager.complete_order(grid_level, OrderSide.SELL)
        paired_buy_level = self._get_or_create_paired_buy_level(grid_level)

        if paired_buy_level:
            await self._place_buy_order(grid_level, paired_buy_level, order.filled)
        else:
            self.logger.error(f"Failed to find or create a paired buy grid level for grid level {grid_level}.")

    def _get_or_create_paired_buy_level(self, sell_grid_level: GridLevel) -> GridLevel | None:
        paired_buy_level = sell_grid_level.paired_buy_level

        if paired_buy_level and self.grid_manager.can_place_order(paired_buy_level, OrderSide.BUY):
            self.logger.info(f"Found valid paired buy level {paired_buy_level} for sell level {sell_grid_level}.")
            return paired_buy_level

        fallback_buy_level = self.grid_manager.get_grid_level_below(sell_grid_level)

        if fallback_buy_level:
            self.logger.info(f"Paired fallback buy level {fallback_buy_level} with sell level {sell_grid_level}.")
            return fallback_buy_level

        self.logger.warning(f"No valid fallback buy level found below sell level {sell_grid_level}.")
        return None

    async def _place_buy_order(
        self,
        sell_grid_level: GridLevel,
        buy_grid_level: GridLevel,
        quantity: float,
    ) -> None:
        adjusted_quantity = self.order_validator.adjust_and_validate_buy_quantity(
            self.balance_tracker.balance,
            quantity,
            buy_grid_level.price,
        )
        
        buy_order = await self.order_execution_strategy.execute_limit_order(
            order_side=OrderSide.BUY,
            pair=self.trading_pair,
            amount=adjusted_quantity,
            price=buy_grid_level.price,
        )

        if buy_order:
            self.grid_manager.pair_grid_levels(sell_grid_level, buy_grid_level, pairing_type="buy")
            self.balance_tracker.reserve_funds_for_buy(buy_order.amount * buy_grid_level.price)
            self.grid_manager.mark_order_pending(buy_grid_level, buy_order)
            self.order_book.add_order(buy_order, buy_grid_level)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_PLACED,
                order_details=str(buy_order),
            )
        else:
            self.logger.error(f"Failed to place buy order at grid level {buy_grid_level}")

    async def _place_sell_order(
        self,
        buy_grid_level: GridLevel,
        sell_grid_level: GridLevel,
        quantity: float,
    ) -> None:
        adjusted_quantity = self.order_validator.adjust_and_validate_sell_quantity(
            self.balance_tracker.crypto_balance,
            quantity,
        )
        
        sell_order = await self.order_execution_strategy.execute_limit_order(
            order_side=OrderSide.SELL,
            pair=self.trading_pair,
            amount=adjusted_quantity,
            price=sell_grid_level.price,
        )

        if sell_order:
            self.grid_manager.pair_grid_levels(buy_grid_level, sell_grid_level, pairing_type="sell")
            self.balance_tracker.reserve_funds_for_sell(sell_order.amount)
            self.grid_manager.mark_order_pending(sell_grid_level, sell_order)
            self.order_book.add_order(sell_order, sell_grid_level)
            await self.notification_handler.async_send_notification(
                NotificationType.ORDER_PLACED,
                order_details=str(sell_order),
            )
        else:
            self.logger.error(f"Failed to place sell order at grid level {sell_grid_level}.")

    async def perform_initial_purchase(
        self,
        current_price: float,
    ) -> None:
        """
        Dynamically calculates and purchases the required Crypto amount based on 
        the number of grid levels above the current price (Sell Grids).
        """
        grid_prices = self.grid_manager.grid_levels.keys()
        total_grid_count = len(grid_prices)

        if total_grid_count == 0:
            self.logger.warning("No grids defined. Skipping initial purchase.")
            return

        sell_grids = [p for p in grid_prices if p > current_price]
        sell_grid_count = len(sell_grids)
        
        total_balance_value = self.balance_tracker.get_total_balance_value(current_price)
        value_per_grid = total_balance_value / total_grid_count
        target_crypto_value = value_per_grid * sell_grid_count

        self.logger.info("📊 Initial Purchase Analysis:")
        self.logger.info(f"   Total Equity: {total_balance_value:.2f}")
        self.logger.info(f"   Grids Above Price (Sell): {sell_grid_count} / {total_grid_count}")
        self.logger.info(f"   Target Crypto Value: {target_crypto_value:.2f} ({(sell_grid_count/total_grid_count)*100:.1f}%)")

        current_crypto_value = self.balance_tracker.crypto_balance * current_price
        shortfall_value = target_crypto_value - current_crypto_value

        if shortfall_value > 10.0: 
            # FIX: Added 1% buffer to account for fees and slippage
            amount_to_buy = (shortfall_value / current_price) * 1.01
            self.logger.info(f"   📉 Shortfall detected. Buying {amount_to_buy:.4f} SOL ({shortfall_value:.2f} USDT + buffer)...")

            try:
                adjusted_quantity = self.order_validator.adjust_and_validate_buy_quantity(
                    self.balance_tracker.balance,
                    amount_to_buy,
                    current_price
                )

                buy_order = await self.order_execution_strategy.execute_market_order(
                    order_side=OrderSide.BUY, 
                    pair=self.trading_pair,
                    amount=adjusted_quantity,
                    price=current_price,
                )
                
                self.logger.info(f"✅ Initial purchase successful: {buy_order}")
                self.order_book.add_order(buy_order)
                
                if self.trading_mode != TradingMode.BACKTEST:
                    self.balance_tracker.update_after_initial_purchase(initial_order=buy_order)
                else:
                    await self._simulate_fill(buy_order, buy_order.timestamp)

                await self.notification_handler.async_send_notification(
                    NotificationType.ORDER_PLACED,
                    order_details=f"Rebalanced Portfolio: Bought {amount_to_buy:.4f} SOL",
                )

            except Exception as e:
                self.logger.error(f"Failed to execute initial purchase: {e}")
                raise e
        else:
            self.logger.info(f"   ✅ Sufficient Crypto Held ({current_crypto_value:.2f} vs Target {target_crypto_value:.2f}). No buy needed.")

    async def execute_take_profit_or_stop_loss_order(
        self,
        current_price: float,
        take_profit_order: bool = False,
        stop_loss_order: bool = False,
    ) -> None:
        if not (take_profit_order or stop_loss_order):
            return

        event = "Take profit" if take_profit_order else "Stop loss"
        try:
            quantity = self.balance_tracker.crypto_balance
            
            order = await self.order_execution_strategy.execute_market_order(
                order_side=OrderSide.SELL,
                pair=self.trading_pair,
                amount=quantity,
                price=current_price,
            )

            if not order:
                raise Exception("Order execution returned None")

            self.order_book.add_order(order)
            await self.notification_handler.async_send_notification(
                NotificationType.TAKE_PROFIT_TRIGGERED if take_profit_order else NotificationType.STOP_LOSS_TRIGGERED,
                order_details=str(order),
            )
            self.logger.info(f"{event} triggered at {current_price} and sell order executed.")

        except Exception as e:
            self.logger.error(f"Failed to execute {event}: {e}")

    async def simulate_order_fills(
        self,
        high_price: float,
        low_price: float,
        timestamp: int | pd.Timestamp,
    ) -> None:
        timestamp_val = int(timestamp.timestamp()) if isinstance(timestamp, pd.Timestamp) else int(timestamp)
        pending_orders = self.order_book.get_open_orders()
        crossed_buy_levels = [level for level in self.grid_manager.sorted_buy_grids if low_price <= level <= high_price]
        crossed_sell_levels = [
            level for level in self.grid_manager.sorted_sell_grids if low_price <= level <= high_price
        ]

        self.logger.debug(
            f"Simulating fills: High {high_price}, Low {low_price}, Pending orders: {len(pending_orders)}",
        )
        self.logger.debug(f"Crossed buy levels: {crossed_buy_levels}, Crossed sell levels: {crossed_sell_levels}")

        for order in pending_orders:
            if (order.side == OrderSide.BUY and order.price in crossed_buy_levels) or (
                order.side == OrderSide.SELL and order.price in crossed_sell_levels
            ):
                await self._simulate_fill(order, timestamp_val)

    async def _simulate_fill(
        self,
        order: Order,
        timestamp: int,
    ) -> None:
        order.filled = order.amount
        order.remaining = 0.0
        order.status = OrderStatus.CLOSED
        order.timestamp = timestamp
        order.last_trade_timestamp = timestamp
        timestamp_in_seconds = timestamp / 1000 if timestamp > 10**10 else timestamp
        formatted_timestamp = datetime.fromtimestamp(timestamp_in_seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        self.logger.info(
            f"Simulated fill for {order.side.value.upper()} order at price {order.price} "
            f"with amount {order.amount}. Filled at timestamp {formatted_timestamp}",
        )
        await self.event_bus.publish(Events.ORDER_FILLED, order)
