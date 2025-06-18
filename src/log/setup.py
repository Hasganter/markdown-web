import logging
import sys

from src.local.config import effective_settings as config
from src.log.handler import SQLiteHandler, LokiHandler

class SubprocessLogFilter(logging.Filter):
    """
    This filter identifies logs coming from the subprocess logger
    and prevents them from being formatted again by the console.
    """
    def filter(self, record):
        # The 'proc.' prefix is used by log_process_output in app_process.py
        return not record.name.startswith('proc.')

class MainFormatter(logging.Formatter):
    """A custom formatter to handle regular logs and raw subprocess logs."""
    
    def format(self, record):
        # If the log is from a subprocess, just return the raw message.
        if record.name.startswith('proc.'):
            return record.getMessage()
        
        # Otherwise, use the default formatting.
        # Temporarily change the format string for the superclass call.
        original_format = self._style._fmt
        self._style._fmt = '%(asctime)s - %(levelname)-8s - [%(name)s] - %(message)s'
        formatted_message = super().format(record)
        self._style._fmt = original_format
        return formatted_message

def setup_logging(console_level: int = logging.INFO) -> None:
    """
    Configures the root logger for the application.
    This sets up handlers for console, SQLite, and optionally Loki,
    clearing any previously configured handlers to prevent duplication.

    :param console_level: The logging level for the console output (e.g., logging.INFO).
    """
    config.LOG_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    # Set root level to lowest to capture all messages for handler filtering
    root_logger.setLevel(logging.DEBUG)
    
    # Clear any existing handlers to prevent re-adding them on re-runs
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # --- Console Handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(MainFormatter())
    root_logger.addHandler(console_handler)
    
    # --- SQLite Handler (always enabled for all levels) ---
    try:
        sqlite_handler = SQLiteHandler(db_path=config.LOG_DB_PATH)
        sqlite_handler.setLevel(logging.DEBUG)
        # Subprocess logs need to be stored, so we don't filter them here.
        root_logger.addHandler(sqlite_handler)
    except Exception as e:
        root_logger.error(f"Failed to initialize SQLite logging handler: {e}. Logging to DB will be disabled.")

    # --- Loki Handler (conditional) ---
    if config.LOKI_ENABLED:
        try:
            loki_handler = LokiHandler(url=config.LOKI_URL, org_id=config.LOKI_ORG_ID)
            loki_handler.setLevel(logging.INFO) # Avoid spamming Loki with DEBUG logs
            root_logger.addHandler(loki_handler)
            root_logger.info(f"Grafana Loki logging handler initialized for {config.LOKI_URL}.")
        except Exception as e:
            root_logger.error(f"Failed to initialize Grafana Loki logging handler: {e}")
