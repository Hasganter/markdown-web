import json
import time
import sqlite3
import logging
from pathlib import Path
from collections import namedtuple
from typing import List, Dict, Any, Tuple
from src.local.database.base import BaseDBManager

LogEntry = namedtuple('LogEntry', ['timestamp', 'level', 'module', 'message'])
log = logging.getLogger(__name__)


class LogDBManager(BaseDBManager):
    """
    Manages all interactions with the application's logging SQLite database.
    """

    def __init__(self, db_path: Path):
        """
        Initializes the LogDBManager.

        :param db_path: The path to the logging SQLite database file.
        """
        super().__init__(db_path, lock=None, enable_wal=False)

    def initialize_database(self) -> None:
        """
        Ensures all necessary log tables exist in the database.
        """
        try:
            # Main application log table from SQLiteHandler
            self.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    timestamp REAL PRIMARY KEY,
                    level TEXT,
                    module TEXT,
                    funcName TEXT,
                    lineno INTEGER,
                    message TEXT
                )
            ''')
            # Nginx access log table
            self.execute("""
                CREATE TABLE IF NOT EXISTS nginx_access_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    remote_addr TEXT,
                    request_method TEXT,
                    request_uri TEXT, 
                    status INTEGER,
                    body_bytes_sent INTEGER,
                    http_referer TEXT,
                    http_user_agent TEXT
                )
            """)
            log.debug("Log database tables created/verified.")
        except sqlite3.Error as e:
            log.critical(f"Could not create log database tables: {e}", exc_info=True)
            raise

    def insert_nginx_log(self, log_line: str) -> None:
        """
        Parses a JSON log line from Nginx and inserts it into the database.

        :param log_line: The raw JSON string from Nginx's stdout.
        """
        try:
            log_data = json.loads(log_line)
            self.execute(
                """
                INSERT INTO nginx_access_logs (timestamp, remote_addr, request_method, 
                request_uri, status, body_bytes_sent, http_referer, http_user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(), log_data.get('remote_addr'),
                    log_data.get('request_method'), log_data.get('request_uri'),
                    int(log_data.get('status', 0)), int(log_data.get('body_bytes_sent', 0)),
                    log_data.get('http_referer'), log_data.get('http_user_agent')
                )
            )
        except (json.JSONDecodeError, sqlite3.Error, KeyError) as e:
            log.error(f"Failed to process Nginx log line: '{log_line}'. Error: {e}")
    
    def insert_log_entry(self, timestamp: float, level: str, module: str, func_name: str, line_no: int, message: str) -> None:
        """
        Inserts a single log entry into the database.

        :param timestamp: The Unix timestamp of the log entry.
        :param level: The log level (e.g., 'INFO', 'ERROR').
        :param module: The module that generated the log.
        :param func_name: The function name that generated the log.
        :param line_no: The line number where the log was generated.
        :param message: The log message.
        """
        try:
            self.execute(
                '''INSERT OR IGNORE INTO logs (timestamp, level, module, funcName, lineno, message)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (timestamp, level, module, func_name, line_no, message)
            )
        except sqlite3.Error as e:
            log.error(f"Failed to insert log entry: {e}", exc_info=True)
            raise

    def insert_log_batch(self, log_entries: List[Dict[str, Any]]) -> None:
        """
        Inserts multiple log entries in a single transaction for better performance.

        :param log_entries: List of dictionaries containing log entry data.
                           Each dict should have keys: timestamp, level, module, funcName, lineno, message
        """
        if not log_entries:
            return
            
        try:
            # Convert dict entries to tuples in the correct order
            params = [(
                entry['timestamp'],
                entry['level'],
                entry['module'],
                entry['funcName'],
                entry['lineno'],
                entry['message']
            ) for entry in log_entries]
            
            self.execute_many(
                '''INSERT OR IGNORE INTO logs (timestamp, level, module, funcName, lineno, message)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                params
            )
        except sqlite3.Error as e:
            log.error(f"Failed to insert log batch of {len(log_entries)} entries: {e}", exc_info=True)
            raise

    def fetch_last_entries(self, limit: int) -> List[LogEntry]:
        """
        Fetches the most recent N log entries from the database.

        :param limit: The maximum number of log entries to retrieve.
        :return list: A list of LogEntry namedtuples.
        """
        entries = []
        try:
            rows = self.fetch_all(
                "SELECT timestamp, level, module, message FROM logs ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            )
            # Reverse the results to show oldest first.
            for row in reversed(rows):
                dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['timestamp']))
                entries.append(LogEntry(
                    timestamp=row['timestamp'], level=row['level'], module=row['module'],
                    message=f"{dt} - {row['level']:<8} - [{row['module']}] - {row['message']}"
                ))
        except sqlite3.Error as e:
            log.error(f"Failed to fetch log entries from database: {e}")
        return entries

    def get_recent_logs(self, count: int) -> List[LogEntry]:
        """
        Alias for fetch_last_entries for compatibility.

        :param count: The maximum number of log entries to retrieve.
        :return list: A list of LogEntry namedtuples.
        """
        return self.fetch_last_entries(count)

    def listen_for_updates(self, last_timestamp: float) -> Tuple[List[LogEntry], float]:
        """
        Polls the database for new logs since the last known timestamp.

        :param last_timestamp: The Unix timestamp of the last known log entry.
        :return tuple: A tuple containing a list of new LogEntry objects and the new latest timestamp.
        """
        new_entries = []
        new_last_ts = last_timestamp
        try:
            rows = self.fetch_all(
                "SELECT timestamp, level, module, message FROM logs WHERE timestamp > ? ORDER BY timestamp ASC",
                (last_timestamp,)
            )
            for row in rows:
                dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['timestamp']))
                entry = LogEntry(
                    timestamp=row['timestamp'], level=row['level'], module=row['module'],
                    message=f"{dt} - {row['level']:<8} - [{row['module']}] - {row['message']}"
                )
                new_entries.append(entry)
                new_last_ts = max(new_last_ts, entry.timestamp)
        except sqlite3.Error as e:
            log.error(f"Failed to poll log database for updates: {e}")

        return new_entries, new_last_ts
