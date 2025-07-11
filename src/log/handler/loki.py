import os
import sys
import logging
import requests
import threading
from typing import Optional
from collections import deque
from src.local import app_globals
from typing import Deque, Dict, Any


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
        self.flush_interval = app_globals.LOG_BUFFER_FLUSH_INTERVAL
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
