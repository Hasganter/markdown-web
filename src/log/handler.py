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
from src.local.database import LogDBManager

class LokiHandler(logging.Handler):
    """
    A custom logging handler that sends logs to a Grafana Loki instance
    in batches using a background thread.
    """
    def __init__(self, url: str, org_id: Optional[str] = None):
        """
        Initializes the Loki handler.

        :param url: The base URL of the Loki instance.
        :param org_id: The tenant ID for Loki (e.g., 'X-Scope-OrgID').
        """
        super().__init__()
        self.url = f"{url.rstrip('/')}/loki/api/v1/push"
        self.org_id = org_id
        self.log_buffer: Deque[Dict[str, Any]] = deque()
        self.buffer_lock = threading.Lock()
        self.flush_interval = config.LOG_BUFFER_FLUSH_INTERVAL
        self.batch_size = 200 # Flush when buffer reaches this size (MB)
        
        # Get hostname in a cross-platform way
        self.hostname = os.getenv('COMPUTERNAME') or os.getenv('HOSTNAME')
        if not self.hostname:
            try:
                import socket
                self.hostname = socket.gethostname()
            except ImportError:
                self.hostname = 'unknown-host'

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
        # Final flush on stop, ensuring any remaining logs are sent
        self.flush()

    def emit(self, record: logging.LogRecord) -> None:
        """
        Formats a log record and adds it to the internal buffer.
        If the buffer exceeds the batch size, it triggers a flush.

        :param record: The log record to be processed.
        """
        try:
            # For subprocess logs, the message is already the full line.
            if record.name.startswith('proc.'):
                msg = record.getMessage()
                # Try to extract the process name for a better label
                logger_name = record.name.split('.')[-1]
            else:
                msg = self.format(record)
                logger_name = record.name

            log_entry = {
                "stream": {
                    "job": "python-app",
                    "level": record.levelname.lower(),
                    "hostname": self.hostname,
                    "logger": logger_name,
                },
                "values": [
                    [str(int(record.created * 1e9)), msg]
                ]
            }
            with self.buffer_lock:
                self.log_buffer.append(log_entry)
                # Check buffer size inside the lock to prevent race conditions
                if len(self.log_buffer) >= self.batch_size:
                    self._flush_locked()
        except Exception as e:
            print(f"ERROR: LokiHandler failed to process a log record: {e}", file=sys.stderr)

    def _flush_locked(self) -> None:
        """
        Sends the buffered logs to Loki. This method assumes the buffer lock is already held.
        It handles the HTTP POST request and error reporting.
        """
        if not self.log_buffer:
            return

        # Make a copy of the buffer and clear the original inside the lock
        logs_to_send = list(self.log_buffer)
        self.log_buffer.clear()
        
        # Release the lock before making a blocking network call
        self.buffer_lock.release()
        
        try:
            payload = {"streams": logs_to_send}
            headers = {'Content-Type': 'application/json'}
            if self.org_id:
                headers['X-Scope-OrgID'] = self.org_id

            response = requests.post(self.url, json=payload, headers=headers, timeout=5)
            # 204 No Content is the success status for Loki push
            if response.status_code != 204:
                # Use a specific logger to avoid recursive loop if Loki handler is on root
                logging.getLogger("LokiHandler.Internal").error(
                    f"Loki returned non-204 status: {response.status_code} - {response.text}"
                )
        except requests.RequestException as e:
            print(f"CRITICAL: Failed to send {len(logs_to_send)} logs to Loki: {e}", file=sys.stderr)
        except Exception as e:
            print(f"CRITICAL: An unexpected error occurred in LokiHandler flush: {e}", file=sys.stderr)
        finally:
            # Re-acquire the lock
            self.buffer_lock.acquire()

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
            # Wait for the flush thread to finish its last loop
            self.flush_thread.join(timeout=self.flush_interval + 2)
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
        self.logDB = LogDBManager(self.db_path)
        self.logDB.initialize_database()  # Ensure log tables are created
        self._start_flush_thread()
        self._start_db_size_check_thread()

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
        # For subprocess logs, we need to ensure all required fields exist
        # even if they are not part of a standard LogRecord.
        if record.name.startswith('proc.'):
            module = record.name.split('.')[-1]
            func_name = 'stdout' if record.levelno == logging.INFO else 'stderr'
            lineno = 0
        else:
            module = record.module
            func_name = record.funcName
            lineno = record.lineno

        log_entry = {
            "timestamp": record.created,
            "level": record.levelname,
            "module": module,
            "funcName": func_name,
            "lineno": lineno,
            "message": record.getMessage()
        }
        with self.buffer_lock:
            self.log_buffer.append(log_entry)
            if len(self.log_buffer) >= config.LOG_BUFFER_SIZE:
                self._flush_locked()

    def _flush_locked(self) -> None:
        """
        Writes the buffered logs to the SQLite database using LogDBManager. 
        Assumes the buffer lock is held.
        
        :raises sqlite3.Error: If there is an error during the database write operation.
        """
        if not self.log_buffer:
            return

        entries_to_write = list(self.log_buffer)
        self.log_buffer.clear()

        # Release lock before DB operation
        self.buffer_lock.release()
        try:
            # Use LogDBManager's batch insert method for better performance
            self.logDB.insert_log_batch(entries_to_write)
        except sqlite3.Error as e:
            print(f"Error writing logs to DB: {e}. Log entries: {len(entries_to_write)}")
            # Optional: Add failed logs back to the buffer for retry
            # with self.buffer_lock:
            #     self.log_buffer.extend(entries_to_write)
        finally:
            # Re-acquire lock
            self.buffer_lock.acquire()

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
        logger = logging.getLogger(__name__) # Use specific logger to avoid recursion
        try:
            if not self.db_path.exists():
                return
            file_size_bytes = self.db_path.stat().st_size
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
        # Final flush must be called after threads are stopped
        self.flush()
        super().close()
