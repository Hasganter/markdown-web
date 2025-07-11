import time
import sqlite3
import logging
from pathlib import Path
from src.local import app_globals
from typing import List, Optional, Tuple
from src.local.database.base import BaseDBManager
from multiprocessing.synchronize import Lock as LockType

log = logging.getLogger(__name__)


class ContentDBManager(BaseDBManager):
    """
    Manages all interactions with the content SQLite database.
    """

    def __init__(self, db_path: Path, lock: LockType = None):
        """
        Initializes the ContentDBManager.

        :param db_path: The path to the content SQLite database file.
        :param lock: A multiprocessing.Lock for write operations.
        """
        super().__init__(db_path, lock=lock, enable_wal=True)

    def initialize_database(self):
        try:
            self.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    path_key TEXT PRIMARY KEY,
                    source_sha256 TEXT NOT NULL,
                    html_content TEXT NOT NULL,
                    title TEXT NOT NULL,
                    last_updated REAL NOT NULL,
                    allowed_methods TEXT DEFAULT 'GET'
                )
            """)
            log.info(f"Content database '{self.db_path}' initialized/checked in WAL mode.")
        except sqlite3.Error as e:
            log.critical(f"Error initializing content database: {e}", exc_info=True)
            raise

    def get_page_hash(self, path_key: str) -> Optional[str]:
        """
        Retrieves the stored SHA256 hash for a given page from the database.

        :param path_key: The unique key for the page ('subdomain:path').
        :return str or None: The SHA256 hash or None if not found.
        """
        try:
            row = self.fetch_one("SELECT source_sha256 FROM pages WHERE path_key = ?", (path_key,))
            return row['source_sha256'] if row else None
        except sqlite3.Error as e:
            log.error(f"Error fetching SHA for page {path_key}: {e}")
            return None

    def get_page(self, path_key: str) -> Optional[sqlite3.Row]:
        """
        Retrieves a complete page record from the database.

        :param path_key: The unique key for the page ('subdomain:path').
        :return: A sqlite3.Row object with all page data or None if not found.
        """
        try:
            return self.fetch_one("SELECT * FROM pages WHERE path_key = ?", (path_key,))
        except sqlite3.Error as e:
            log.error(f"Error fetching page {path_key}: {e}")
            return None

    def page_exists(self, path_key: str) -> bool:
        """
        Checks if a page exists in the database.

        :param path_key: The unique key for the page ('subdomain:path').
        :return: True if the page exists, False otherwise.
        """
        try:
            row = self.fetch_one("SELECT 1 FROM pages WHERE path_key = ? LIMIT 1", (path_key,))
            return row is not None
        except sqlite3.Error as e:
            log.error(f"Error checking if page {path_key} exists: {e}")
            return False

    def get_all_pages(self) -> List[sqlite3.Row]:
        """
        Retrieves all pages from the database.

        :return: A list of sqlite3.Row objects containing all page data.
        """
        try:
            return self.fetch_all("SELECT * FROM pages ORDER BY path_key")
        except sqlite3.Error as e:
            log.error(f"Error fetching all pages: {e}")
            return []

    def get_pages_by_subdomain(self, subdomain: Optional[str] = None) -> List[sqlite3.Row]:
        """
        Retrieves pages for a specific subdomain.

        :param subdomain: The subdomain to filter by. Use None for main domain.
        :return: A list of sqlite3.Row objects containing page data for the subdomain.
        """
        try:
            domain_part = subdomain or "main"
            return self.fetch_all("SELECT * FROM pages WHERE path_key LIKE ? ORDER BY path_key", 
                                  (f"{domain_part}:%",))
        except sqlite3.Error as e:
            log.error(f"Error fetching pages for subdomain {subdomain}: {e}")
            return []

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
        self.execute(query, params)
        log.info(f"Page {path_key} updated in DB (Methods: {methods_str}).")

    def delete_page(self, path_key: str) -> None:
        """
        Deletes a page's record from the content database.

        :param path_key: The unique key for the page to delete.
        """
        query = "DELETE FROM pages WHERE path_key = ?"
        self.execute(query, (path_key,))
        log.info(f"Page {path_key} removed from DB.")

    @staticmethod
    def get_subdomain_from_path(path: Path) -> Optional[str]:
        """
        Determines the subdomain name from a given file path.
        
        :param path: The path to check.
        :return: The subdomain name if it's a subdomain path, otherwise None.
        """
        try:
            relative_path = path.resolve().relative_to(app_globals.ROOT_INDEX_DIR.resolve())
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
        subdomain_root = app_globals.ROOT_INDEX_DIR / f".{subdomain}" if subdomain else app_globals.ROOT_INDEX_DIR
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
        root_dir = app_globals.ROOT_INDEX_DIR
        
        # Add root index itself if it has a content file
        if any((root_dir / f).exists() for f in ["index.md", "index.html"]):
             tasks.append((root_dir, None))

        # Process all directories
        for item in root_dir.iterdir():
            if not item.is_dir():
                continue
                
            if item.name.startswith('.'):
                # Subdomain content (skip .assets)
                if item.name != ".assets":
                    subdomain_name = item.name[1:]
                    # Add subdomain root itself
                    tasks.append((item, subdomain_name))
                    # Recursively find all subdirectories within the subdomain
                    tasks.extend([(p, subdomain_name) for p in item.rglob("*") if p.is_dir()])
            else:
                # Main domain content
                tasks.append((item, None)) # Top-level folder
                # Recursively find all subdirectories
                tasks.extend([(p, None) for p in item.rglob("*") if p.is_dir()])
        
        return sorted(set(tasks), key=lambda x: str(x[0]))

    def get_canonical_content_file(self, dir_path: Path, subdomain: Optional[str]) -> Optional[Path]:
        """
        Determines the canonical content file (.md or .html) for a given directory.

        It checks for 'index.md/html' at root levels, and '{dir_name}.md/html' otherwise.
        It prefers .html over .md if both exist.

        :param dir_path: The directory to check.
        :param subdomain: The subdomain this directory belongs to.
        :return: A Path object to the content file if found, otherwise None.
        """
        subdomain_root = app_globals.ROOT_INDEX_DIR / f".{subdomain}" if subdomain else None
        is_domain_root = (dir_path.resolve() == app_globals.ROOT_INDEX_DIR.resolve() or 
                          (subdomain_root and dir_path.resolve() == subdomain_root.resolve()))

        base_name = "index" if is_domain_root else dir_path.name
        
        html_file = dir_path / (base_name + ".html")
        if html_file.exists():
            return html_file
            
        md_file = dir_path / (base_name + ".md")
        if md_file.exists():
            return md_file
            
        return None
