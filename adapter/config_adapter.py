from config.config_manager import ConfigManager
from strategies.spacing_type import SpacingType
from strategies.strategy_type import StrategyType
from config.trading_mode import TradingMode

class DictConfigManager(ConfigManager):
    """
    A ConfigManager that accepts a dictionary directly instead of loading a file.
    Used for API-based bot initialization.
    """
    def __init__(self, config_dict, config_validator):
        self.config = config_dict
        self.config_validator = config_validator
        # Skip file loading, validate directly
        self.config_validator.validate(self.config)

    # Override to return keys injected from the API
    def get_api_key(self):
        return self.config.get("credentials", {}).get("api_key")

    def get_api_secret(self):
        return self.config.get("credentials", {}).get("api_secret")
