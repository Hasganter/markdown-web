import logging
import sys

from src.local.config import effective_settings as config
from src.log.handler import SQLiteHandler, LokiHandler

def setup_logging(console_level: int = logging.INFO) -> None:
    """
    Configures the root logger for the application.
    This sets up handlers for console, SQLite, and optionally Loki,
    clearing any previously configured handlers to prevent duplication.

    :param console_level: The logging level for the console output (e.g., logging.INFO).
    """
    # Ensure logs directory exists
    config.LOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    # Set root level to lowest to capture all messages for handler filtering
    root_logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # --- Console Handler ---
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)-8s - [%(name)s] - %(message)s')
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # --- SQLite Handler (always enabled) ---
    try:
        sqlite_handler = SQLiteHandler(db_path=config.LOG_DB_PATH)
        sqlite_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(sqlite_handler)
    except Exception as e:
        # Use the already-configured console handler to log this failure
        root_logger.error(f"Failed to initialize SQLite logging handler: {e}. Logging to DB will be disabled.")

    # --- Loki Handler (conditional) ---
    if config.LOKI_ENABLED:
        try:
            loki_handler = LokiHandler(url=config.LOKI_URL, org_id=config.LOKI_ORG_ID)
            loki_handler.setLevel(logging.INFO) # Avoid spamming Loki with DEBUG logs
            root_logger.addHandler(loki_handler)
            # Use root logger to announce, since this setup is for all loggers.
            root_logger.info(f"Grafana Loki logging handler initialized for {config.LOKI_URL}.")
        except Exception as e:
            root_logger.error(f"Failed to initialize Grafana Loki logging handler: {e}")
