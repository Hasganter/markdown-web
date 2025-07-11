import sqlite3
import logging
from pathlib import Path
from multiprocessing import Lock
from contextlib import contextmanager
from typing import List, Optional, Tuple, Any, Generator
from multiprocessing.synchronize import Lock as LockType

log = logging.getLogger(__name__)


class BaseDBManager:
    """
    Base class for database managers, providing common functionality.
    """

    def __init__(self, db_path: Path, lock: Optional[LockType] = Lock(), enable_wal: bool = False):
        """
        Initializes the base database manager.

        :param db_path: The path to the SQLite database file.
        :param lock: An optional multiprocessing.Lock for thread-safe write operations.
        :param enable_wal: Whether to enable WAL (Write-Ahead Logging) mode.
        """
        self.db_path = db_path
        self.lock = lock
        self.enable_wal = enable_wal

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager that creates and returns a new database connection,
        acquiring the lock for thread-safe operations if available.

        :return Generator[sqlite3.Connection, None, None]: A generator yielding a database connection.
        """
        if self.lock:
            self.lock.acquire()
        conn = sqlite3.connect(self.db_path, timeout=10)
        if self.enable_wal:
            conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
        finally:
            conn.close()
            if self.lock:
                self.lock.release()

    def execute(self, sql: str, params: Optional[Tuple[Any, ...]] = None) -> Any:
        """
        Executes a raw SQL command on the database.

        :param sql: The SQL command to execute.
        :param params: Optional parameters for the SQL command.
        :return: The result of the query.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(sql, params or ())
                conn.commit()
                return cursor.fetchall()
        except sqlite3.Error as e:
            log.error(f"Database operation failed: {e}")
            raise

    def execute_many(self, sql: str, params: List[Tuple[Any, ...]]) -> None:
        """
        Executes a batch of SQL commands.

        :param sql: The SQL command to execute.
        :param params: A list of tuples containing parameters for each command.
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany(sql, params)
                conn.commit()
        except sqlite3.Error as e:
            log.error(f"Batch database operation failed: {e}")
            raise

    def fetch_all(self, sql: str, params: Optional[Tuple[Any, ...]] = None) -> List[sqlite3.Row]:
        """
        Fetches all rows from a query.

        :param sql: The SQL command to execute.
        :param params: Optional parameters for the SQL command.
        :return: A list of sqlite3.Row objects.
        """
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(sql, params or ())
                return cursor.fetchall()
        except sqlite3.Error as e:
            log.error(f"Failed to fetch data: {e}")
            raise
    
    def fetch_one(self, sql: str, params: Optional[Tuple[Any, ...]] = None) -> Optional[sqlite3.Row]:
        """
        Fetches a single row from a query.

        :param sql: The SQL command to execute.
        :param params: Optional parameters for the SQL command.
        :return: A sqlite3.Row object or None if no rows found.
        """
        try:
            with self._get_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(sql, params or ())
                return cursor.fetchone()
        except sqlite3.Error as e:
            log.error(f"Failed to fetch data: {e}")
            raise
