import json
import logging
import sqlite3
import time
from collections import namedtuple
from multiprocessing.synchronize import Lock as LockType
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

from src.local.config import effective_settings as config

logger = logging.getLogger(__name__)

LogEntry = namedtuple('LogEntry', ['timestamp', 'level', 'module', 'message'])


class LogDBManager:
    """
    Manages all interactions with the application's logging SQLite database.
    """

    def __init__(self, db_path: Path):
        """
        Initializes the LogDBManager.

        :param db_path: The path to the logging SQLite database file.
        """
        self.db_path = db_path

    def _get_connection(self) -> sqlite3.Connection:
        """
        Creates and returns a new database connection.

        :return sqlite3.Connection: A new connection object.
        """
        return sqlite3.connect(self.db_path, timeout=10)

    def initialize_database(self) -> None:
        """
        Ensures all necessary log tables exist in the database.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                # Main application log table from SQLiteHandler
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        timestamp REAL PRIMARY KEY, level TEXT, module TEXT,
                        funcName TEXT, lineno INTEGER, message TEXT
                    )
                """)
                # Nginx access log table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS nginx_access_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, remote_addr TEXT,
                        request_method TEXT, request_uri TEXT, status INTEGER,
                        body_bytes_sent INTEGER, http_referer TEXT, http_user_agent TEXT
                    )
                """)
                conn.commit()
                logger.info("Log database tables created/verified.")
        except sqlite3.Error as e:
            logger.critical(f"Could not create log database tables: {e}", exc_info=True)
            raise

    def insert_nginx_log(self, log_line: str) -> None:
        """
        Parses a JSON log line from Nginx and inserts it into the database.

        :param log_line: The raw JSON string from Nginx's stdout.
        """
        try:
            log_data = json.loads(log_line)
            with self._get_connection() as conn:
                conn.execute(
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
                conn.commit()
        except (json.JSONDecodeError, sqlite3.Error, KeyError) as e:
            logger.error(f"Failed to process Nginx log line: '{log_line}'. Error: {e}")

    def fetch_last_entries(self, limit: int) -> List[LogEntry]:
        """
        Fetches the most recent N log entries from the database.

        :param limit: The maximum number of log entries to retrieve.
        :return list: A list of LogEntry namedtuples.
        """
        entries = []
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT timestamp, level, module, message FROM logs ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                )
                # Reverse the results to show oldest first.
                for row in reversed(cursor.fetchall()):
                    dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['timestamp']))
                    entries.append(LogEntry(
                        timestamp=row['timestamp'], level=row['level'], module=row['module'],
                        message=f"{dt} - {row['level']:<8} - [{row['module']}] - {row['message']}"
                    ))
        except sqlite3.Error as e:
            logger.error(f"Failed to fetch log entries from database: {e}")
        return entries

    def listen_for_updates(self, last_timestamp: float) -> Tuple[List[LogEntry], float]:
        """
        Polls the database for new logs since the last known timestamp.

        :param last_timestamp: The Unix timestamp of the last known log entry.
        :return tuple: A tuple containing a list of new LogEntry objects and the new latest timestamp.
        """
        new_entries = []
        new_last_ts = last_timestamp
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT timestamp, level, module, message FROM logs WHERE timestamp > ? ORDER BY timestamp ASC",
                    (last_timestamp,)
                )
                for row in cursor.fetchall():
                    dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['timestamp']))
                    entry = LogEntry(
                        timestamp=row['timestamp'], level=row['level'], module=row['module'],
                        message=f"{dt} - {row['level']:<8} - [{row['module']}] - {row['message']}"
                    )
                    new_entries.append(entry)
                    new_last_ts = max(new_last_ts, entry.timestamp)
        except sqlite3.Error as e:
            logger.error(f"Failed to poll log database for updates: {e}")

        return new_entries, new_last_ts


class ContentDBManager:
    """
    Manages all interactions with the content SQLite database.
    """

    def __init__(self, db_path: Path, lock: Optional[LockType] = None):
        """
        Initializes the ContentDBManager.

        :param db_path: The path to the content SQLite database file.
        :param lock: An optional multiprocessing.Lock for write operations.
        """
        self.db_path = db_path
        self.lock = lock

    def _get_connection(self) -> sqlite3.Connection:
        """
        Creates and returns a new database connection, enabling WAL mode.

        WAL (Write-Ahead Logging) is crucial for allowing concurrent reads
        (from the web server) and writes (from the content converter).

        :return sqlite3.Connection: A new connection object.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def initialize_database(self) -> None:
        """
        Initializes the content DB by creating the 'pages' table if needed.
        """
        if self.lock:
            with self.lock:
                self._create_pages_table()
        else:
            self._create_pages_table()

    def _create_pages_table(self):
        try:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS pages (
                        path_key TEXT PRIMARY KEY,
                        source_sha256 TEXT NOT NULL,
                        html_content TEXT NOT NULL,
                        title TEXT NOT NULL,
                        last_updated REAL NOT NULL,
                        allowed_methods TEXT DEFAULT 'GET'
                    )
                """)
                conn.commit()
            logger.info(f"Content database '{self.db_path}' initialized/checked in WAL mode.")
        except sqlite3.Error as e:
            logger.critical(f"Error initializing content database: {e}", exc_info=True)
            raise

    def get_page_hash(self, path_key: str) -> Optional[str]:
        """
        Retrieves the stored SHA256 hash for a given page from the database.

        :param path_key: The unique key for the page ('subdomain:path').
        :return str or None: The SHA256 hash or None if not found.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT source_sha256 FROM pages WHERE path_key = ?", (path_key,))
                row = cursor.fetchone()
                return row[0] if row else None
        except sqlite3.Error as e:
            logger.error(f"Error fetching SHA for page {path_key}: {e}")
            return None

    def _execute_write(self, query: str, params: tuple):
        """Helper for executing thread-safe write operations."""
        if not self.lock:
            raise RuntimeError("Database lock not provided to ContentDBManager for write operation.")
        with self.lock:
            try:
                with self._get_connection() as conn:
                    conn.execute(query, params)
                    conn.commit()
            except sqlite3.Error as e:
                logger.error(f"DB write operation failed for query '{query[:50]}...': {e}")
                raise

    def update_page(self, path_key: str, source_hash: str, html: str, title: str, methods: List[str]) -> None:
        """
        Inserts or replaces a page's data in the content database.

        :param path_key: The unique key for the page.
        :param source_hash: The SHA256 hash of the page's source content.
        :param html: The final HTML content to store.
        :param title: The page title.
        :param methods: A list of allowed HTTP methods.
        """
        methods_str = ",".join(m.strip().upper() for m in methods)
        query = """
            INSERT OR REPLACE INTO pages 
            (path_key, source_sha256, html_content, title, last_updated, allowed_methods)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        params = (path_key, source_hash, html, title, time.time(), methods_str)
        self._execute_write(query, params)
        logger.info(f"Page {path_key} updated in DB (Methods: {methods_str}).")

    def delete_page(self, path_key: str) -> None:
        """
        Deletes a page's record from the content database.

        :param path_key: The unique key for the page to delete.
        """
        query = "DELETE FROM pages WHERE path_key = ?"
        self._execute_write(query, (path_key,))
        logger.info(f"Page {path_key} removed from DB.")

    @staticmethod
    def get_subdomain_from_path(path: Path) -> Optional[str]:
        """
        Determines the subdomain name from a given file path.
        
        :param path: The path to check.
        :return: The subdomain name if it's a subdomain path, otherwise None.
        """
        try:
            relative_path = path.resolve().relative_to(config.ROOT_INDEX_DIR.resolve())
            if relative_path.parts and relative_path.parts[0].startswith('.'):
                subdomain_dir_name = relative_path.parts[0]
                if subdomain_dir_name != '.assets':
                    return subdomain_dir_name[1:]
        except ValueError:
            pass # Path is not within the root index.
        return None

    @staticmethod
    def get_path_key(dir_path: Path, subdomain: Optional[str]) -> str:
        """
        Generates a unique database key from a directory path and subdomain.
        
        :param dir_path: The content directory path.
        :param subdomain: The associated subdomain name, if any.
        :return str: The generated path key (e.g., 'main:/about', 'blog:/posts/intro').
        """
        subdomain_root = config.ROOT_INDEX_DIR / f".{subdomain}" if subdomain else config.ROOT_INDEX_DIR
        try:
            path_root = dir_path.resolve().relative_to(subdomain_root.resolve())
        except ValueError:
            path_root = Path(".") # Should not happen if logic is correct
            
        domain_part = subdomain or "main"
        # Convert path to a clean, URL-like string
        web_path_segment = "/" if path_root == Path(".") else "/" + str(path_root).replace("\\", "/")
        # Don't strip the root path '/'
        path_key = f"{domain_part}:{web_path_segment.rstrip('/') if len(web_path_segment) > 1 else '/'}"
        return path_key

    def discover_content_directories(self) -> List[Tuple[Path, Optional[str]]]:
        """
        Scans the entire content source directory to find all processable directories.
        
        :return list: A list of tuples, where each tuple is (directory_path, subdomain_name).
        """
        tasks = []
        root_dir = config.ROOT_INDEX_DIR
        
        # Main domain content
        for item in root_dir.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                tasks.append((item, None)) # Top-level folder
                # Recursively find all subdirectories
                tasks.extend([(p, None) for p in item.rglob("*") if p.is_dir()])
        # Add root index itself if it has a content file
        if any((root_dir / f).exists() for f in ["index.md", "index.html"]):
             tasks.append((root_dir, None))

        # Subdomain content
        for item in root_dir.iterdir():
            if item.is_dir() and item.name.startswith('.') and item.name != ".assets":
                subdomain_name = item.name[1:]
                # Add subdomain root itself
                tasks.append((item, subdomain_name))
                # Recursively find all subdirectories within the subdomain
                tasks.extend([(p, subdomain_name) for p in item.rglob("*") if p.is_dir()])
        
        return sorted(list(set(tasks)), key=lambda x: str(x[0]))

    def get_canonical_content_file(self, dir_path: Path, subdomain: Optional[str]) -> Optional[Path]:
        """
        Determines the canonical content file (.md or .html) for a given directory.

        It checks for 'index.md/html' at root levels, and '{dir_name}.md/html' otherwise.
        It prefers .html over .md if both exist.

        :param dir_path: The directory to check.
        :param subdomain: The subdomain this directory belongs to.
        :return: A Path object to the content file if found, otherwise None.
        """
        subdomain_root = config.ROOT_INDEX_DIR / f".{subdomain}" if subdomain else None
        is_domain_root = (dir_path.resolve() == config.ROOT_INDEX_DIR.resolve() or 
                          (subdomain_root and dir_path.resolve() == subdomain_root.resolve()))

        base_name = "index" if is_domain_root else dir_path.name
        
        html_file = dir_path / (base_name + ".html")
        if html_file.exists():
            return html_file
            
        md_file = dir_path / (base_name + ".md")
        if md_file.exists():
            return md_file
            
        return None
