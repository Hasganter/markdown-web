import os
import sys
import logging
import sqlite3
import threading
from typing import Optional
import requests
from collections import deque
from pathlib import Path
from typing import Deque, List, Dict, Any
from src.local.config import effective_settings as config

class LokiHandler(logging.Handler):
    """
    A custom logging handler that sends logs to a Grafana Loki instance
    in batches using a background thread.
    """
    def __init__(self, url: str, org_id: Optional[str] = None):
        """
        Initializes the Loki handler.

        :param url: The base URL of the Loki instance.
        """
        super().__init__()
        self.url = f"{url}/loki/api/v1/push"
        self.org_id = org_id
        self.log_buffer: Deque[Dict[str, Any]] = deque()
        self.buffer_lock = threading.Lock()
        self.flush_interval = config.LOG_BUFFER_FLUSH_INTERVAL
        self.batch_size = 200 # Flush when buffer reaches this size
        self.hostname = os.uname().nodename if hasattr(os, 'uname') else 'windows-host'
        self.stop_event = threading.Event()
        self.flush_thread = threading.Thread(target=self._periodic_flush, daemon=True)
        self.flush_thread.name = "LokiFlushThread"
        self.flush_thread.start()

    def _periodic_flush(self) -> None:
        """
        Periodically flushes the log buffer. This runs in a background thread.
        The final flush is called when the handler is closed.
        """
        while not self.stop_event.wait(self.flush_interval):
            self.flush()
        self.flush() # Final flush on stop

    def emit(self, record: logging.LogRecord) -> None:
        """
        Formats a log record and adds it to the internal buffer.
        If the buffer exceeds the batch size, it triggers a flush.

        :param record: The log record to be processed.
        """
        try:
            msg = record.getMessage()
        except Exception:
            msg = "Could not format log message."

        log_entry = {
            "stream": {
                "job": "python-app",
                "level": record.levelname.lower(),
                "hostname": self.hostname,
                "logger": record.name,
                "module": record.module,
                "function": record.funcName
            },
            "values": [
                [str(int(record.created * 1e9)), msg]
            ]
        }
        with self.buffer_lock:
            self.log_buffer.append(log_entry)
            if len(self.log_buffer) >= self.batch_size:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """
        Sends the buffered logs to Loki. This method assumes the buffer lock is already held.
        It handles the HTTP POST request and error reporting.
        """
        if not self.log_buffer:
            return

        logs_to_send = list(self.log_buffer)
        self.log_buffer.clear()
        
        payload = {"streams": logs_to_send}
        
        headers = {'Content-Type': 'application/json'}
        if self.org_id:
            headers['X-Scope-OrgID'] = self.org_id

        try:
            response = requests.post(self.url, json=payload, headers=headers, timeout=3)
            response.raise_for_status()
        except requests.RequestException as e:
            # If sending fails, we can't log to Loki. Print to stderr as a last resort.
            print(f"CRITICAL: Failed to send {len(logs_to_send)} logs to Loki: {e}", file=sys.stderr)
        except Exception as e:
            print(f"CRITICAL: An unexpected error occurred in LokiHandler flush: {e}", file=sys.stderr)

    def flush(self) -> None:
        """
        Public method to trigger a manual flush of the log buffer in a thread-safe manner.
        """
        with self.buffer_lock:
            self._flush_locked()

    def close(self) -> None:
        """
        Shuts down the handler, ensuring all buffered logs are flushed and threads are joined.
        """
        self.stop_event.set()
        if self.flush_thread.is_alive():
            self.flush_thread.join()
        super().close()


class SQLiteHandler(logging.Handler):
    """
    A custom logging handler that writes logs to a SQLite database
    in batches using a background thread.
    """
    def __init__(self, db_path: Path):
        """
        Initializes the SQLite handler.

        :param db_path: The path to the SQLite database file.
        """
        super().__init__()
        self.db_path = db_path
        self.log_buffer: List[Dict[str, Any]] = []
        self.buffer_lock = threading.Lock()
        self.flush_thread: Optional[threading.Thread] = None
        self.db_size_check_thread: Optional[threading.Thread] = None
        self.db_size_check_interval = config.LOG_DB_SIZE_CHECK_INTERVAL_SECONDS
        self.max_db_size_mb = config.MAX_LOG_DB_SIZE_MB
        self.stop_event = threading.Event()
        self._ensure_table()
        self._start_flush_thread()
        self._start_db_size_check_thread()

    def _ensure_table(self) -> None:
        """
        Ensures that the 'logs' table exists in the database, creating it if necessary.
        
        :raises sqlite3.Error: If the table cannot be created or accessed.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS logs (
                        timestamp REAL PRIMARY KEY,
                        level TEXT,
                        module TEXT,
                        funcName TEXT,
                        lineno INTEGER,
                        message TEXT
                    )
                ''')
                conn.commit()
        except sqlite3.Error as e:
            print(f"CRITICAL: Could not create or access log table in {self.db_path}: {e}")

    def _start_flush_thread(self) -> None:
        """Starts the background thread that periodically flushes logs to the database."""
        self.flush_thread = threading.Thread(target=self._periodic_flush, daemon=True)
        self.flush_thread.name = "SQLiteFlushThread"
        self.flush_thread.start()

    def _periodic_flush(self) -> None:
        """
        Periodically flushes the log buffer. This runs in a background thread.
        """
        while not self.stop_event.wait(config.LOG_BUFFER_FLUSH_INTERVAL):
            self.flush()
        self.flush() # Final flush on stop

    def emit(self, record: logging.LogRecord) -> None:
        """
        Formats a log record and adds it to the internal buffer for batch writing.

        :param record: The log record to be processed.
        """
        log_entry = {
            "timestamp": record.created,
            "level": record.levelname,
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
            "message": record.getMessage()
        }
        with self.buffer_lock:
            self.log_buffer.append(log_entry)
            if len(self.log_buffer) >= config.LOG_BUFFER_SIZE:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """
        Writes the buffered logs to the SQLite database. Assumes the buffer lock is held.
        
        :raises sqlite3.Error: If there is an error during the database write operation.
        """
        if not self.log_buffer:
            return

        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            cursor = conn.cursor()
            entries_to_write = list(self.log_buffer)
            self.log_buffer.clear()

            cursor.executemany('''
                INSERT OR IGNORE INTO logs (timestamp, level, module, funcName, lineno, message)
                VALUES (:timestamp, :level, :module, :funcName, :lineno, :message)
            ''', entries_to_write)
            conn.commit()
        except sqlite3.Error as e:
            print(f"Error writing logs to DB: {e}. Log entries: {len(entries_to_write)}")
        finally:
            if conn:
                conn.close()

    def flush(self) -> None:
        """Public method to trigger a manual flush of the log buffer."""
        with self.buffer_lock:
            self._flush_locked()

    def _start_db_size_check_thread(self) -> None:
        """Starts the periodic database size check thread."""
        self.db_size_check_thread = threading.Thread(target=self._periodic_db_size_check, daemon=True)
        self.db_size_check_thread.name = "LogDbSizeCheckThread"
        self.db_size_check_thread.start()

    def _check_db_file_size(self) -> None:
        """Checks the log database file size and logs a warning if it exceeds the limit."""
        logger = logging.getLogger(__name__)
        try:
            if not os.path.exists(self.db_path):
                return
            file_size_bytes = os.path.getsize(self.db_path)
            file_size_mb = file_size_bytes / (1024 * 1024)

            if file_size_mb > self.max_db_size_mb:
                logger.warning(
                    f"Log database file '{self.db_path}' size ({file_size_mb:.2f} MB) "
                    f"exceeds configured limit ({self.max_db_size_mb} MB)."
                )
        except FileNotFoundError:
            logger.debug(f"Log database file '{self.db_path}' not found during size check.")
        except Exception as e:
            logger.error(f"Error checking log database file size for '{self.db_path}': {e}")

    def _periodic_db_size_check(self) -> None:
        """Periodically calls the database size check method."""
        while not self.stop_event.wait(self.db_size_check_interval):
            self._check_db_file_size()

    def close(self) -> None:
        """
        Shuts down the handler, ensuring all threads are joined and buffers are flushed.
        """
        self.stop_event.set()
        if self.flush_thread and self.flush_thread.is_alive():
            self.flush_thread.join()
        if self.db_size_check_thread and self.db_size_check_thread.is_alive():
            self.db_size_check_thread.join()
        self.flush() # Final flush
        super().close()
