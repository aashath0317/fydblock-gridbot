import logging

import numpy as np

from config.config_manager import ConfigManager
from strategies.spacing_type import SpacingType
from strategies.strategy_type import StrategyType

from ..order_handling.order import Order, OrderSide
from .grid_level import GridCycleState, GridLevel


class GridManager:
    def __init__(
        self,
        config_manager: ConfigManager,
        strategy_type: StrategyType,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.config_manager: ConfigManager = config_manager
        self.strategy_type: StrategyType = strategy_type
        self.price_grids: list[float]
        self.central_price: float
        self.sorted_buy_grids: list[float]
        self.sorted_sell_grids: list[float]
        self.grid_levels: dict[float, GridLevel] = {}

    def initialize_grids_and_levels(self) -> None:
        """
        Initializes the grid levels and assigns their respective states.
        """
        self.price_grids, self.central_price = self._calculate_price_grids_and_central_price()

        if self.strategy_type == StrategyType.SIMPLE_GRID:
            # Initial static assignment
            self.sorted_buy_grids = [price_grid for price_grid in self.price_grids if price_grid <= self.central_price]
            self.sorted_sell_grids = [price_grid for price_grid in self.price_grids if price_grid > self.central_price]
            self.grid_levels = {
                price: GridLevel(
                    price,
                    GridCycleState.READY_TO_BUY if price <= self.central_price else GridCycleState.READY_TO_SELL,
                )
                for price in self.price_grids
            }

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            self.sorted_buy_grids = self.price_grids[:-1]  # All except the top grid
            self.sorted_sell_grids = self.price_grids[1:]  # All except the bottom grid
            self.grid_levels = {
                price: GridLevel(
                    price,
                    GridCycleState.READY_TO_BUY_OR_SELL
                    if price != self.price_grids[-1]
                    else GridCycleState.READY_TO_SELL,
                )
                for price in self.price_grids
            }
        self.logger.info(f"Grids and levels initialized. Central price: {self.central_price}")

    def update_zones_based_on_price(self, current_price: float) -> None:
        """
        Re-aligns the Buy/Sell zones based on the actual Current Market Price.
        FIX: Ensures we do not overwrite states of active/busy grids.
        """
        if self.strategy_type != StrategyType.SIMPLE_GRID:
            return

        self.logger.info(f"?? Re-aligning grid zones to Current Price: {current_price}")

        self.sorted_buy_grids = []
        self.sorted_sell_grids = []

        for price in self.price_grids:
            grid_level = self.grid_levels[price]

            # 1. Determine ideal state based on price
            ideal_state = GridCycleState.READY_TO_BUY if price < current_price else GridCycleState.READY_TO_SELL

            # 2. Assign to Buy/Sell lists for OrderManager
            if price < current_price:
                self.sorted_buy_grids.append(price)
            else:
                self.sorted_sell_grids.append(price)

            # 3. FIX: Only update state if the grid is IDLE.
            # If it's WAITING_FOR_FILL, it has a live order. Do NOT touch it.
            if grid_level.state in [GridCycleState.READY_TO_BUY, GridCycleState.READY_TO_SELL]:
                grid_level.state = ideal_state
            else:
                self.logger.info(f"   Skipping state update for busy grid {price} (State: {grid_level.state})")

        self.logger.info(f"   ? New Buy Grids: {len(self.sorted_buy_grids)}")
        self.logger.info(f"   ? New Sell Grids: {len(self.sorted_sell_grids)}")

    def get_trigger_price(self) -> float:
        return self.central_price

    def get_order_size_for_grid_level(
        self,
        total_balance: float,
        current_price: float,
    ) -> float:
        total_grids = len(self.grid_levels)
        if total_grids == 0:
            return 0.0
        order_size = total_balance / total_grids / current_price
        return order_size

    def get_initial_order_quantity(
        self,
        current_fiat_balance: float,
        current_crypto_balance: float,
        current_price: float,
    ) -> float:
        current_crypto_value_in_fiat = current_crypto_balance * current_price
        total_portfolio_value = current_fiat_balance + current_crypto_value_in_fiat

        sell_grid_count = len([p for p in self.price_grids if p > current_price])
        total_grid_count = len(self.price_grids)

        if total_grid_count == 0:
            return 0.0

        target_crypto_ratio = sell_grid_count / total_grid_count
        target_crypto_value = total_portfolio_value * target_crypto_ratio

        fiat_to_allocate = target_crypto_value - current_crypto_value_in_fiat
        fiat_to_allocate = max(0, min(fiat_to_allocate, current_fiat_balance))

        return fiat_to_allocate / current_price

    def pair_grid_levels(
        self,
        source_grid_level: GridLevel,
        target_grid_level: GridLevel,
        pairing_type: str,
    ) -> None:
        if pairing_type == "buy":
            source_grid_level.paired_buy_level = target_grid_level
            target_grid_level.paired_sell_level = source_grid_level
            self.logger.info(
                f"Paired sell grid level {source_grid_level.price} with buy grid level {target_grid_level.price}.",
            )
        elif pairing_type == "sell":
            source_grid_level.paired_sell_level = target_grid_level
            target_grid_level.paired_buy_level = source_grid_level
            self.logger.info(
                f"Paired buy grid level {source_grid_level.price} with sell grid level {target_grid_level.price}.",
            )
        else:
            raise ValueError(f"Invalid pairing type: {pairing_type}. Must be 'buy' or 'sell'.")

    def get_paired_sell_level(
        self,
        buy_grid_level: GridLevel,
    ) -> GridLevel | None:
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            sorted_prices = sorted(self.price_grids)
            try:
                idx = sorted_prices.index(buy_grid_level.price)
                if idx + 1 < len(sorted_prices):
                    sell_price = sorted_prices[idx + 1]
                    return self.grid_levels[sell_price]
            except ValueError:
                pass
            return None

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            sorted_prices = sorted(self.price_grids)
            current_index = sorted_prices.index(buy_grid_level.price)
            if current_index + 1 < len(sorted_prices):
                paired_sell_price = sorted_prices[current_index + 1]
                return self.grid_levels[paired_sell_price]
            return None
        return None

    def get_grid_level_below(self, grid_level: GridLevel) -> GridLevel | None:
        sorted_levels = sorted(self.grid_levels.keys())
        current_index = sorted_levels.index(grid_level.price)
        if current_index > 0:
            lower_price = sorted_levels[current_index - 1]
            return self.grid_levels[lower_price]
        return None

    def mark_order_pending(
        self,
        grid_level: GridLevel,
        order: Order,
    ) -> None:
        grid_level.add_order(order)
        if order.side == OrderSide.BUY:
            grid_level.state = GridCycleState.WAITING_FOR_BUY_FILL
            self.logger.info(f"Buy order placed and marked as pending at grid level {grid_level.price}.")
        elif order.side == OrderSide.SELL:
            grid_level.state = GridCycleState.WAITING_FOR_SELL_FILL
            self.logger.info(f"Sell order placed and marked as pending at grid level {grid_level.price}.")

    def complete_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> None:
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            if order_side == OrderSide.BUY:
                grid_level.state = GridCycleState.READY_TO_SELL
                self.logger.info(
                    f"Buy order completed at grid level {grid_level.price}. Transitioning to READY_TO_SELL.",
                )
                if grid_level.paired_sell_level:
                    grid_level.paired_sell_level.state = GridCycleState.READY_TO_SELL

            elif order_side == OrderSide.SELL:
                # FIX: Prevent race condition.
                if grid_level.state == GridCycleState.WAITING_FOR_BUY_FILL:
                    self.logger.info(
                        f"Sell order completed at {grid_level.price}, but level is already "
                        f"WAITING_FOR_BUY_FILL (claimed by neighbor). Keeping existing state."
                    )
                else:
                    grid_level.state = GridCycleState.READY_TO_BUY
                    self.logger.info(
                        f"Sell order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY.",
                    )

                if grid_level.paired_buy_level:
                    grid_level.paired_buy_level.state = GridCycleState.READY_TO_BUY

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            if order_side == OrderSide.BUY:
                grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
                self.logger.info(
                    f"Buy order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY_OR_SELL.",
                )
                if grid_level.paired_sell_level:
                    grid_level.paired_sell_level.state = GridCycleState.READY_TO_SELL

            elif order_side == OrderSide.SELL:
                grid_level.state = GridCycleState.READY_TO_BUY_OR_SELL
                self.logger.info(
                    f"Sell order completed at grid level {grid_level.price}. Transitioning to READY_TO_BUY_OR_SELL.",
                )
                if grid_level.paired_buy_level:
                    grid_level.paired_buy_level.state = GridCycleState.READY_TO_BUY
        else:
            self.logger.error("Unexpected strategy type")

    def can_place_order(
        self,
        grid_level: GridLevel,
        order_side: OrderSide,
    ) -> bool:
        if self.strategy_type == StrategyType.SIMPLE_GRID:
            if order_side == OrderSide.BUY:
                # 1. Self Check
                if grid_level.state != GridCycleState.READY_TO_BUY:
                    return False

                # 2. FIX: Check Neighbor (Paired Sell Level)
                # If the level where we want to sell is busy, we cannot buy yet.
                # This prevents "Grid Overlap" warnings and stuck bags.
                paired_sell = self.get_paired_sell_level(grid_level)
                if paired_sell and paired_sell.state == GridCycleState.WAITING_FOR_SELL_FILL:
                    return False

                return True

            elif order_side == OrderSide.SELL:
                return grid_level.state == GridCycleState.READY_TO_SELL

        elif self.strategy_type == StrategyType.HEDGED_GRID:
            if order_side == OrderSide.BUY:
                return grid_level.state in {GridCycleState.READY_TO_BUY, GridCycleState.READY_TO_BUY_OR_SELL}
            elif order_side == OrderSide.SELL:
                return grid_level.state in {GridCycleState.READY_TO_SELL, GridCycleState.READY_TO_BUY_OR_SELL}

        return False

    def _extract_grid_config(self) -> tuple[float, float, int, str]:
        bottom_range = self.config_manager.get_bottom_range()
        top_range = self.config_manager.get_top_range()
        num_grids = self.config_manager.get_num_grids()
        spacing_type = self.config_manager.get_spacing_type()
        return bottom_range, top_range, num_grids, spacing_type

    def _calculate_price_grids_and_central_price(self) -> tuple[list[float], float]:
        bottom_range, top_range, num_grids, spacing_type = self._extract_grid_config()

        # Ensure an odd number of grid lines to include a center point
        points_to_generate = num_grids
        if num_grids % 2 == 0:
            points_to_generate = num_grids + 1
            self.logger.info(f"   ?? Grid count is even. Generating {points_to_generate} lines.")

        self.logger.info(f"   ?? Lower Band: {bottom_range}")
        self.logger.info(f"   ?? Upper Band: {top_range}")

        if spacing_type == SpacingType.ARITHMETIC:
            grids = np.linspace(bottom_range, top_range, points_to_generate)
            central_price = (top_range + bottom_range) / 2

        elif spacing_type == SpacingType.GEOMETRIC:
            grids = []
            if points_to_generate <= 1:
                grids = [bottom_range]
                central_price = bottom_range
            else:
                ratio = (top_range / bottom_range) ** (1 / (points_to_generate - 1))
                current_price = bottom_range
                for _ in range(points_to_generate):
                    grids.append(current_price)
                    current_price *= ratio
                central_index = len(grids) // 2
                central_price = grids[central_index]
        else:
            raise ValueError(f"Unsupported spacing type: {spacing_type}")

        return list(grids), central_price
